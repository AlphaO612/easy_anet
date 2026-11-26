"""Manage profile routing policies, local probing, and multi-exit load balancing."""
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import argparse
import difflib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
from release import client_supported, record_issuance, verified_manifest
import time
import urllib.request
import urllib.parse


STRATEGIES = {"strict-ip", "prefer-route", "speed", "auto", "weighted", "least-load", "balanced"}
FAIL_MODES = {"fail-closed", "fail-open", "degrade"}


def parse_toml_minimal(text):
    """Minimal TOML parser fallback for Python 3.10 environments without tomli."""
    root = {}
    current = root
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[[") and line.endswith("]]"):
            section = line[2:-2].strip()
            target = root
            parts = [p.strip() for p in section.split(".")]
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            arr = target.setdefault(parts[-1], [])
            item = {}
            arr.append(item)
            current = item
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            target = root
            for part in (p.strip() for p in section.split(".")):
                target = target.setdefault(part, {})
            current = target
            continue
        if "=" not in line:
            continue
        key, raw_val = (part.strip() for part in line.split("=", 1))
        # Strip inline comments outside quoted strings
        in_str = None
        escaped = False
        cleaned = []
        for ch in raw_val:
            if escaped:
                cleaned.append(ch)
                escaped = False
                continue
            if ch == "\\" and in_str == '"':
                cleaned.append(ch)
                escaped = True
                continue
            if ch in ('"', "'"):
                if in_str is None:
                    in_str = ch
                elif in_str == ch:
                    in_str = None
            if ch == "#" and in_str is None:
                break
            cleaned.append(ch)
        val_str = "".join(cleaned).strip()
        if val_str == "true":
            value = True
        elif val_str == "false":
            value = False
        else:
            try:
                value = json.loads(val_str)
            except ValueError:
                value = ast.literal_eval(val_str)
        current[key] = value
    return root


def load_toml(path):
    data = Path(path).read_bytes()
    try:
        import tomllib
        return tomllib.loads(data.decode("utf-8"))
    except ModuleNotFoundError:
        try:
            import tomli
            return tomli.loads(data.decode("utf-8"))
        except ModuleNotFoundError:
            return parse_toml_minimal(data.decode("utf-8"))


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
        if "weight" in path:
            w = path["weight"]
            require(type(w) is int and 1 <= w <= 1000, f"{name}: weight must be integer 1..1000")
        if "probe_target" in path:
            require(isinstance(path["probe_target"], str) and bool(path["probe_target"].strip()),
                    f"{name}: invalid probe_target")
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
    allowed_profile_fields = {
        "paths", "entry", "strategy", "external_ip", "user", "groups",
        "rules", "require_domain", "credential_id", "ingress", "dns_path",
        "weights", "fail_mode", "local_probe",
    }
    for name, profile in profiles.items():
        require(profile["strategy"] in STRATEGIES, f"{name}: unknown strategy")
        require(set(profile) <= allowed_profile_fields, f"{name}: unknown profile field")
        if "groups" in profile:
            require(isinstance(profile["groups"], list) and all(isinstance(g, str) for g in profile["groups"]), name)
        members = profile["paths"]
        require(isinstance(members, list) and members and len(set(members)) == len(members), name)
        require(all(p in paths and paths[p]["entry"] == profile["entry"] for p in members), name)
        if profile["strategy"] == "strict-ip":
            ipaddress.ip_address(profile["external_ip"])
        if "weights" in profile:
            weights = profile["weights"]
            require(isinstance(weights, dict) and set(weights) <= set(members), f"{name}: invalid weights keys")
            require(all(type(v) is int and 1 <= v <= 1000 for v in weights.values()),
                    f"{name}: weights must be integers 1..1000")
        if "fail_mode" in profile:
            require(profile["fail_mode"] in FAIL_MODES, f"{name}: invalid fail_mode")
            if profile["strategy"] == "strict-ip":
                require(profile["fail_mode"] == "fail-closed", f"{name}: strict-ip requires fail-closed")
        if "local_probe" in profile:
            require(isinstance(profile["local_probe"], bool), f"{name}: local_probe must be boolean")
        for rule in profile.get("rules", []):
            require(isinstance(rule["id"], str) and rule["id"], f"{name}: rule ID required")
            require(set(rule) <= {"user", "group", "protocol", "domain", "cidr", "allow", "id"}, "unknown rule field")
            require(isinstance(rule["allow"], list) and set(rule["allow"]) <= set(members), name)
            if "cidr" in rule:
                ipaddress.ip_network(rule["cidr"])


def _path_score(strategy, profile, paths, name, metrics):
    m = metrics.get(name, {})
    base_weight = float(profile.get("weights", {}).get(name, paths[name].get("weight", 1)))
    mbps = float(m.get("mbps") if m.get("mbps") is not None else 10.0)
    latency = max(float(m.get("latency_ms") if m.get("latency_ms") is not None else 50.0), 5.0)
    loss = max(float(m.get("loss") if m.get("loss") is not None else 0.0), 0.0)
    load = max(float(m.get("load", m.get("active_connections", 0.0)) or 0.0), 0.0)
    if strategy == "speed":
        return base_weight * max(mbps, 0.1)
    if strategy == "weighted":
        return base_weight
    return base_weight * max(mbps, 1.0) / ((1.0 + loss * 10.0) * latency * (1.0 + load))


def _compute_weights(strategy, profile, paths, available, metrics):
    if not available:
        return {}, []
    if strategy in {"strict-ip", "prefer-route"}:
        chosen = available[0]
        return {chosen: 100}, [(chosen, 100)]
    if strategy == "weighted":
        weights = {p: int(profile.get("weights", {}).get(p, paths[p].get("weight", 1))) for p in available}
        return weights, [(p, weights[p]) for p in available]
    raw_scores = {p: max(_path_score(strategy, profile, paths, p, metrics), 1e-6) for p in available}
    total = sum(raw_scores.values())
    weights = {p: max(1, int(round(100.0 * raw_scores[p] / total))) for p in available}
    return weights, [(p, weights[p]) for p in available]


def _parse_probe_target(path_info):
    target = path_info.get("probe_target")
    if not target and isinstance(path_info.get("probe"), dict):
        target = path_info["probe"].get("target")
    if not target:
        host = path_info.get("gateway") or path_info.get("external_ip")
        return (host, 443) if host else None
    target = str(target).strip()
    if "://" in target:
        parsed = urllib.parse.urlsplit(target)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return (host, port) if host else None
    if target.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", target)
        if match:
            return match.group(1), int(match.group(2) or 443)
    if ":" in target and target.count(":") == 1:
        host, port_str = target.rsplit(":", 1)
        if port_str.isdigit():
            return host, int(port_str)
    return target, 443


def _default_tcp_probe(host, port, interface, timeout):
    start = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if interface and hasattr(socket, "SO_BINDTODEVICE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode("ascii") + b"\0")
        sock.connect((host, port))
        return (time.monotonic() - start) * 1000.0
    except OSError:
        return None
    finally:
        sock.close()


def probe_local_paths(paths, timeout=2.0, *, tcp_probe=None, now=None):
    """Probe egress paths directly (TCP connect + RTT) without requiring Prometheus/Blackbox."""
    probe_fn = tcp_probe or _default_tcp_probe
    timestamp = time.time() if now is None else now
    if not paths:
        return {}

    def check(item):
        name, info = item
        endpoint = _parse_probe_target(info)
        if not endpoint:
            return name, dict(up=False, expires=timestamp + 45, loss=1.0, source="local_probe")
        host, port = endpoint
        rtt = probe_fn(host, port, info.get("interface"), timeout)
        if rtt is None or rtt < 0:
            return name, dict(up=False, expires=timestamp + 45, loss=1.0, source="local_probe")
        return name, dict(up=True, expires=timestamp + 45, latency_ms=round(float(rtt), 2),
                          loss=0.0, mbps=10.0, source="local_probe")

    workers = min(8, len(paths))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(check, paths.items()))


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
    strategy = profile["strategy"]
    fail_mode = profile.get("fail_mode", "fail-closed")
    degraded = False

    available = [p for p in allowed if metrics.get(p, {}).get("up") is True and metrics[p]["expires"] > now]
    if not available and allowed and fail_mode in {"fail-open", "degrade"} and strategy != "strict-ip":
        any_fresh = any(p in metrics and metrics[p].get("expires", 0) > now for p in allowed)
        if not any_fresh:
            fallback = session.get("fallback_paths", allowed)
            available = [p for p in allowed if p in fallback and metrics.get(p, {}).get("up") is not False]
            if available:
                degraded = True

    if not degraded:
        if strategy == "speed":
            available = [p for p in available if "mbps" in metrics[p]]
        if strategy == "auto":
            available = [p for p in available if "loss" in metrics[p] and "latency_ms" in metrics[p]]
    previous = session.get("previous")
    old_ip = paths.get(previous, {}).get("external_ip")
    if strategy == "strict-ip":
        available = [p for p in available if paths[p]["external_ip"] == profile["external_ip"]]
    if strategy in {"prefer-route", "strict-ip"}:
        available.sort(key=lambda p: (p != previous, paths[p]["external_ip"] != old_ip))
    elif strategy == "weighted":
        available.sort(key=lambda p: (
            -int(profile.get("weights", {}).get(p, paths[p].get("weight", 1))),
            metrics.get(p, {}).get("loss", 0),
            metrics.get(p, {}).get("latency_ms", 0),
            p != previous,
        ))
    elif strategy in {"least-load", "balanced"}:
        scores = {p: _path_score(strategy, profile, paths, p, metrics) for p in available}
        available.sort(key=lambda p: (-scores[p], metrics.get(p, {}).get("loss", 0),
                                      metrics.get(p, {}).get("latency_ms", 0), p != previous))
    elif strategy == "auto":
        available.sort(key=lambda p: (metrics.get(p, {}).get("loss", 0),
                                      metrics.get(p, {}).get("latency_ms", 0),
                                      -metrics.get(p, {}).get("mbps", 0), p != previous))
    else:
        available.sort(key=lambda p: (-metrics.get(p, {}).get("mbps", 0), p != previous))
        if not degraded and previous in available and available[0] != previous:
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
    weights, distribution = _compute_weights(strategy, profile, paths, available, metrics)
    return dict(profile=session["profile"], rule=reason, strategy=strategy, previous=previous,
                eligible=available, selected=chosen, action="route" if chosen else "deny",
                previous_ip=old_ip, external_ip=paths[chosen]["external_ip"] if chosen else None,
                weights=weights, distribution=distribution, degraded=degraded)


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
            for key in ("loss", "latency_ms", "mbps", "load", "active_connections"):
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
