"""Manage profile routing policies and preview a new flow using observed link metrics."""
from collections import Counter, defaultdict
import argparse
import difflib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from release import client_supported, record_issuance, verified_manifest
import time
import urllib.request
import urllib.parse


STRATEGIES = {"strict-ip", "prefer-route", "speed", "auto"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(config):
    paths = config["paths"]
    profiles = config["profiles"]
    require(isinstance(paths, dict) and isinstance(profiles, dict), "expected paths and profiles")
    for name, path in paths.items():
        require(all(isinstance(path[k], str) and path[k] for k in ("entry", "exit", "transport")), name)
        ipaddress.ip_address(path["external_ip"])
    outgoing = defaultdict(list)
    incoming = Counter()
    for path in paths.values():
        start, end = path["entry"], path["exit"]
        if path.get("probe"):
            require((path["probe"]["source_node"], path["probe"]["target_node"]) == (start, end),
                    "probe endpoints do not match path")
        if start != end:
            outgoing[start].append(end)
            incoming[start] += 0
            incoming[end] += 1
    ready = [n for n, count in incoming.items() if not count]
    visited = 0
    while ready:
        visited += 1
        for target in outgoing[ready.pop()]:
            incoming[target] -= 1
            if not incoming[target]:
                ready.append(target)
    require(visited == len(incoming), "cyclic route")
    for name, profile in profiles.items():
        require(profile["strategy"] in STRATEGIES, f"{name}: unknown strategy")
        require(set(profile) <= {"paths", "entry", "strategy", "external_ip", "user", "groups",
                                 "rules", "require_domain", "credential_id", "ingress", "dns_path"}, f"{name}: unknown profile field")
        if "groups" in profile:
            require(isinstance(profile["groups"], list) and all(isinstance(g, str) for g in profile["groups"]), name)
        members = profile["paths"]
        require(isinstance(members, list) and members and len(set(members)) == len(members), name)
        require(all(p in paths and paths[p]["entry"] == profile["entry"] for p in members), name)
        if profile["strategy"] == "strict-ip":
            ipaddress.ip_address(profile["external_ip"])
        for rule in profile.get("rules", []):
            require(isinstance(rule["id"], str) and rule["id"], f"{name}: rule ID required")
            require(set(rule) <= {"user", "group", "protocol", "domain", "cidr", "allow", "id"}, "unknown rule field")
            require(isinstance(rule["allow"], list) and set(rule["allow"]) <= set(members), name)
            if "cidr" in rule:
                ipaddress.ip_network(rule["cidr"])


def decide(config, session, now):
    profile = config["profiles"][session["profile"]]
    paths = config["paths"]
    allowed = profile["paths"]
    domain = None
    if session.get("dns_expires", 0) > now and len(set(session.get("domains", []))) == 1:
        domain = session["domains"][0].lower().rstrip(".")
    reason = "profile default"
    if "user" in profile and profile["user"] != session.get("user"):
        allowed, reason = [], "profile user mismatch"
    elif "groups" in profile and not set(profile["groups"]).intersection(session.get("groups", [])):
        allowed, reason = [], "profile group mismatch"
    elif profile.get("require_domain") and not domain:
        allowed, reason = [], "unknown domain"
    else:
        for rule in profile.get("rules", []):
            if any(rule[k] != session.get(k) for k in ("user", "protocol") if k in rule):
                continue
            if "group" in rule and rule["group"] not in session.get("groups", []):
                continue
            if "domain" in rule and rule["domain"].lower().rstrip(".") != domain:
                continue
            if "cidr" in rule and ipaddress.ip_address(session["destination"]) not in ipaddress.ip_network(rule["cidr"]):
                continue
            allowed, reason = rule["allow"], rule["id"]
            break
    metrics = session["metrics"]
    available = [p for p in allowed if metrics.get(p, {}).get("up") is True and metrics[p]["expires"] > now]
    if profile["strategy"] == "speed":
        available = [p for p in available if "mbps" in metrics[p]]
    if profile["strategy"] == "auto":
        available = [p for p in available if "loss" in metrics[p] and "latency_ms" in metrics[p]]
    previous = session.get("previous")
    old_ip = paths.get(previous, {}).get("external_ip")
    strategy = profile["strategy"]
    if strategy == "strict-ip":
        available = [p for p in available if paths[p]["external_ip"] == profile["external_ip"]]
    if strategy in {"prefer-route", "strict-ip"}:
        available.sort(key=lambda p: (p != previous, paths[p]["external_ip"] != old_ip))
    elif strategy == "auto":
        available.sort(key=lambda p: (metrics[p]["loss"], metrics[p]["latency_ms"], -metrics[p].get("mbps", 0), p != previous))
    else:
        available.sort(key=lambda p: (-metrics[p]["mbps"], p != previous))
        if previous in available and available[0] != previous:
            def sustained(p):
                samples = metrics[p].get("history", [])[-3:]
                baseline = metrics[previous].get("history", [])[-3:]
                return len(samples) == len(baseline) == 3 and all(
                    a["at"] == b["at"] and a["mbps"] > b["mbps"] and a["mbps"] >= b["mbps"] * 1.2
                    for a, b in zip(samples, baseline)
                ) and len({s["at"] for s in samples}) == 3
            candidates = [p for p in available if sustained(p)] if now - session["selected_at"] >= 60 else []
            chosen = next(iter(candidates), previous)
            available.remove(chosen)
            available.insert(0, chosen)
    chosen = next(iter(available), None)
    return dict(profile=session["profile"], rule=reason, strategy=strategy, previous=previous,
                eligible=available, selected=chosen, action="route" if chosen else "deny",
                previous_ip=old_ip, external_ip=paths[chosen]["external_ip"] if chosen else None)


def read(path):
    return json.loads(path.read_text())


def canonical(config):
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        try:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.replace(out.name, path)
        finally:
            Path(out.name).unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def api_request(base, path, token, *, publication=None):
    site = urllib.parse.urlparse(base)
    require(site.scheme == "https" or (site.scheme == "http" and site.hostname in
            ("localhost", "127.0.0.1", "::1")), "admin API requires HTTPS or loopback")
    request = urllib.request.Request(base.rstrip("/") + path, headers={"Authorization": "Bearer " + token})
    if publication is not None:
        request.method = "PUT"
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(publication).encode()
    with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
        return json.load(response)


def api_read(base, path, token):
    return api_request(base, path, token)


def observed_metrics(config, snapshot, now):
    require(snapshot["monitoring"] == "configured", "monitoring unavailable")
    metrics = {}
    for name, path in config["paths"].items():
        binding = path.get("probe")
        if not binding:
            continue
        for link in snapshot["links"]:
            edge = {"source_node": link["source_node"], **link["edge"]}
            if edge != binding or link["desired"] != link["applied"] or link["available"] is not True:
                continue
            seen = link.get("last_seen")
            if seen is None or not 0 <= now - seen < 45:
                continue
            metrics[name] = dict(up=True, expires=seen + 45)
            windows = link.get("capacity_windows", [])
            if len(windows) == 3 and all(value is not None for value in windows):
                metrics[name]["history"] = [dict(at=int(now) - 15 * (2 - i), mbps=value)
                                            for i, value in enumerate(windows)]
            for key in ("loss", "latency_ms", "mbps"):
                if link.get(key) is not None:
                    metrics[name][key] = link[key]
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    subs = parser.add_subparsers(dest="action", required=True)
    for action in ("apply", "diff", "preview"):
        subs.add_parser(action).add_argument("input", type=Path)
    subs.add_parser("export")
    issue = subs.add_parser("issue", help="issue a provisioned profile for the selected path")
    issue.add_argument("input", type=Path)
    issue.add_argument("credentials", type=Path, help="private JSON map: profile -> path -> config filename")
    issue.add_argument("output", type=Path)
    issue.add_argument("--release", type=Path, required=True, help="manifest for the deployed release")
    issue.add_argument("--history", type=Path, required=True, help="persistent issued-combination ledger")
    issue.add_argument("--client", required=True, help="exact released client ID and version")
    issue.add_argument("--config-schema", required=True)
    issue.add_argument("--api", required=True, help="admin API URL including /api/v1")
    issue.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action in ("apply", "diff"):
            candidate = read(args.input)
            validate(candidate)
            data = canonical(candidate)
            if args.action == "diff":
                before = canonical(read(args.store)) if args.store.exists() else ""
                print("".join(difflib.unified_diff(before.splitlines(True), data.splitlines(True),
                                                   fromfile=str(args.store), tofile=str(args.input))), end="")
            else:
                atomic_write(args.store, data)
        else:
            if args.action == "issue":
                token = args.token_file.read_text().strip()
                published = api_read(args.api, "/profile-topology", token)
                config = json.loads(published["manifest"])
                require(config == read(args.store), "local topology differs from published revision")
            else:
                config = read(args.store)
            validate(config)
            if args.action == "export":
                print(canonical(config), end="")
            else:
                session = read(args.input)
                profile = config["profiles"][session["profile"]]
                if args.action == "issue":
                    session["metrics"] = observed_metrics(config, api_read(args.api, "/probe-links", token), time.time())
                    manifest = verified_manifest(args.release)
                    supported = {p for p in profile["paths"] if client_supported(
                        manifest, args.client, args.config_schema, config["paths"][p]["transport"])}
                    require(supported, f"No verified path for {args.client} / {args.config_schema}. "
                            f"Verified alternatives: {manifest['verified_clients']}")
                    session["metrics"] = {p: m for p, m in session["metrics"].items() if p in supported}
                    require(not profile.get("require_domain") and not any(
                        {"domain", "cidr", "protocol"} & rule.keys() for rule in profile.get("rules", [])
                    ), "issuing a whole-session config cannot enforce destination or protocol rules")
                result = decide(config, session, int(time.time()))
                result["revision"] = hashlib.sha256(canonical(config).encode()).hexdigest()
                print(json.dumps(result, sort_keys=True))
                if args.action == "issue":
                    require(result["selected"] is not None, "no permitted route")
                    filename = read(args.credentials)[session["profile"]][result["selected"]]
                    data = (args.credentials.parent / filename).read_text()
                    record_issuance(args.history, session["profile"], args.client, args.config_schema,
                                    config["paths"][result["selected"]]["transport"], manifest["server_commit"])
                    atomic_write(args.output, data)
    except (KeyError, TypeError, ValueError, AttributeError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Invalid topology or session: {error}\n")


if __name__ == "__main__":
    main()
