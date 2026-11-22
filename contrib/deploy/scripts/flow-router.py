"""Install per-connection IPv4 routing policy in one dedicated rootless ANet namespace."""
import argparse
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

from flow_node import NodeAPI
from topology import api_read, decide, observed_metrics, read, validate


def context(topology, binding):
    profile = topology["profiles"][binding["profile"]]
    paths = binding["paths"]
    if set(profile["paths"]) != set(paths):
        raise ValueError("bindings must cover every permitted path")
    marks = [p["table"] for p in paths.values()]
    if any(type(m) is not int or not 1000 <= m <= 8999 for m in marks) or len(set(marks)) != len(marks):
        raise ValueError("path tables must be unique integers from 1000 to 8999")
    for path in paths.values():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", path["interface"]) or path["interface"] == "anet-server":
            raise ValueError("invalid output interface")
        if "gateway" in path:
            ipaddress.IPv4Address(path["gateway"])
    if len({p["interface"] for p in paths.values()}) != len(paths):
        raise ValueError("each path must use its own egress interface")
    net = ipaddress.IPv4Network(binding["source"])
    if "dns_listen" in binding and ipaddress.IPv4Address(binding["dns_listen"]) not in net:
        raise ValueError("DNS listener must be in the profile source subnet")
    if "dns_upstream" in binding:
        addr = ipaddress.IPv4Address(binding["dns_upstream"])
        if ("dns_listen" not in binding or binding.get("dns_path") not in paths or addr.is_loopback
                or addr.is_unspecified or addr.is_multicast or addr == ipaddress.IPv4Address(binding["dns_listen"])):
            raise ValueError("DNS upstream requires a unicast resolver, local listener and bound dns_path")
    return profile, paths, net


def profile_revoked(profile, binding):
    return ("user" in profile and profile["user"] != binding["user"]) or (
        "groups" in profile and not set(profile["groups"]).intersection(binding.get("groups", [])))


def render(topology, binding, metrics, now, previous):
    profile, paths, net = context(topology, binding)
    # Domain evidence must come from DNS observed for this dedicated profile.
    # Ambiguous and expired answers cannot authorize a domain-specific path.
    domains = {}
    for answer in binding.get("dns_answers", []):
        remaining = math.floor(min(45, float(answer["expires"]) - now))
        if remaining > 0:
            ip = str(ipaddress.IPv4Address(answer["address"]))
            name = answer["domain"].lower().rstrip(".")
            names = domains.setdefault(ip, {})
            names[name] = max(remaining, names.get(name, 0))
    if profile_revoked(profile, binding):
        metrics = {}
    events = {}
    sets = []

    def address_set(name, entries):
        values = ", ".join(f"{ip} timeout {ttl}s" for ip, ttl in entries)
        sets.append(f"set {name} {{ type ipv4_addr; flags timeout; elements = {{ {values} }}; }}")
        return "@" + name

    unique = {ip: next(iter(names.items())) for ip, names in domains.items() if len(names) == 1}
    lines = ["set live { type mark; flags timeout;"]
    leases = {name: math.floor(min(45, metrics[name]["expires"] - now)) for name in paths
              if metrics.get(name, {}).get("up") and metrics[name]["expires"] >= now + 1
              and (profile["strategy"] != "strict-ip" or topology["paths"][name]["external_ip"] == profile["external_ip"])}
    # A new flow may only select a mark the kernel can lease for a whole second.
    metrics = {name: metrics[name] for name in leases}
    if leases:
        lines.append("elements = { " + ", ".join(f'{paths[name]["table"]} timeout {ttl}s'
                                                 for name, ttl in leases.items()) + " }")
    dns_match = None
    dns_path = binding.get("dns_path") if "dns_upstream" in binding else None
    if dns_path in leases:
        listener = address_set("dns_listener", [(str(ipaddress.IPv4Address(binding["dns_listen"])), leases[dns_path])])
        dns_match = (f'iifname "anet-server" ip saddr {net} ip daddr {listener} '
                     'fib daddr type local meta l4proto { tcp, udp } th dport 53')
    lines += ["}", "chain classify { type filter hook prerouting priority mangle; policy accept;",
              'iifname != "anet-server" return', f"ip saddr != {net} drop"]
    if dns_match:
        lines.append(dns_match + " return")
    lines.append("ct mark != 0 meta mark set ct mark return")

    def classify(rule, selector, allowed):
        sub = dict(profile, paths=allowed, rules=[], require_domain=False)
        session = dict(profile=binding["profile"], user=binding["user"], groups=binding.get("groups", []),
                       metrics=metrics, destination="0.0.0.0")
        last = previous.get(rule, {})
        session.update(previous=last.get("selected"), selected_at=last.get("since", now))
        decision = decide(dict(paths=topology["paths"], profiles={binding["profile"]: sub}), session, now)
        chosen = decision["selected"]
        events[rule] = {"selected": chosen, "since": last["since"] if last.get("selected") == chosen and "since" in last else now,
                        "strategy": profile["strategy"], "eligible": decision["eligible"]}
        action = f'ct mark set {paths[chosen]["table"]} meta mark set ct mark return' if chosen else "drop"
        lines.append((selector + " " + action).strip())

    if profile.get("require_domain"):
        known = [(ip, ttl) for ip, (_, ttl) in unique.items()]
        lines.append("ip daddr != " + address_set("known_dns", known) + " drop" if known else "drop")
    for idx, rule in enumerate(profile.get("rules", [])):
        if "user" in rule and rule["user"] != binding["user"]:
            continue
        if "group" in rule and rule["group"] not in binding.get("groups", []):
            continue
        selectors = []
        if "protocol" in rule:
            if rule["protocol"] not in ("tcp", "udp", "icmp"):
                raise ValueError("unsupported IP protocol")
            selectors.append("ip protocol " + rule["protocol"])
        network = ipaddress.IPv4Network(rule.get("cidr", "0.0.0.0/0"))
        if "domain" in rule:
            matches = [(ip, ttl) for ip, (name, ttl) in unique.items()
                       if name == rule["domain"].lower().rstrip(".") and ipaddress.IPv4Address(ip) in network]
            if not matches:
                continue
            selectors.append("ip daddr " + address_set(f"dns_{idx}", matches))
        elif "cidr" in rule:
            selectors.append("ip daddr " + str(network))
        classify(rule["id"], " ".join(selectors), rule["allow"])
    classify("(default)", "", profile["paths"])
    lines += ["}", "chain input { type filter hook input priority filter; policy accept;"]
    if dns_match:
        lines.append(dns_match + " accept")
    if "dns_listen" in binding:
        listen = str(ipaddress.IPv4Address(binding["dns_listen"]))
        lines.append(f"ip daddr {listen} fib daddr type local meta l4proto {{ tcp, udp }} th dport 53 drop")
    lines += ['iifname "anet-server" drop', "}",
              "chain forward { type filter hook forward priority filter; policy drop;"]
    for p in paths.values():
        mark, iface = p["table"], p["interface"]
        lines += [f'iifname "anet-server" oifname "{iface}" ct mark {mark} ct mark @live accept',
                  f'iifname "{iface}" oifname "anet-server" ct direction reply ct mark {mark} ct mark @live accept']
    lines += ["}", "chain dns_route { type route hook output priority mangle; policy accept;"]
    if dns_path:
        resolver = str(ipaddress.IPv4Address(binding["dns_upstream"]))
        mark = paths[dns_path]["table"]
        iface = paths[dns_path]["interface"]
        dns_query = f"ip daddr {resolver} meta l4proto {{ tcp, udp }} th dport 53"
        lines.append(f"meta skgid 53153 ct direction original {dns_query} meta mark set {mark}")
    lines += ["}", "chain dns_output { type filter hook output priority filter; policy accept;",
              "meta skgid != 53153 return",
              "ct direction reply meta l4proto { tcp, udp } th sport 53 accept"]
    if dns_path:
        lines.append(f"{dns_query} meta mark {mark} meta mark @live accept")
    lines += ["drop", "}", "chain dns_egress { type filter hook postrouting priority filter; policy accept;"]
    if dns_path:
        # OUTPUT retains the initial hook interface on reroute; POSTROUTING sees
        # the final device. Guard it before NAT or transmission.
        lines.append(f'{dns_query} meta mark {mark} oifname != "{iface}" drop')
    lines += ["}", "chain egress_nat { type nat hook postrouting priority 99; policy accept;"]
    if dns_path:
        lines.append(f'{dns_query} oifname "{iface}" meta mark {mark} masquerade')
    for p in paths.values():
        lines.append(f'ip saddr {net} oifname "{p["interface"]}" ct mark {p["table"]} masquerade')
    return "\n".join(["table ip anet_flow {", *sets, *lines, "}", "}"]) + "\n", events


def load_dns_answers(binding):
    if "dns_answers_file" not in binding:
        return
    try:
        answers = read(Path(binding["dns_answers_file"]))
        if not isinstance(answers, list):
            raise ValueError("expected an array of observed answers")
        for row in answers:
            ipaddress.IPv4Address(row["address"])
            if not isinstance(row["domain"], str) or not row["domain"] or not math.isfinite(float(row["expires"])):
                raise ValueError("invalid DNS observation")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"DNS observations unavailable: {exc}", file=sys.stderr, flush=True)
        answers = []
    binding["dns_answers"] = answers


def pin_binding(execute, binding, topology):
    # A controller restart must retain the meaning of marks on active connections.
    # Link a completely written identity file exactly once in the holder's tmpfs.
    identity = {"profile": binding["profile"], "user": binding["user"], "groups": sorted(binding.get("groups", [])),
                "source": binding["source"],
                "paths": {name: [p["table"], p["interface"], p.get("gateway"),
                                 *[topology["paths"][name][key] for key in ("entry", "exit", "transport", "external_ip")]]
                          for name, p in binding["paths"].items()}}
    script = """
set -eu
cd /run/anet-flow
umask 077
temp=$(mktemp ./binding.XXXXXX)
trap 'rm -f "$temp"' EXIT
cat > "$temp"
ln "$temp" binding 2>/dev/null || cmp -s "$temp" binding
"""
    result = execute("sh", "-c", script,
                     stdin=json.dumps(identity, sort_keys=True), check=False)
    if result.returncode:
        raise ValueError("drain namespace before changing profile or path identity")


def run(args):
    def execute(*cmd, stdin=None, check=True):
        return subprocess.run(["docker", "exec", "-i", "--user", "0", args.container, *cmd],
                              input=stdin, text=True, capture_output=True, check=check, timeout=15)
    previous = {}
    last_revision = None
    last_report = 0
    while True:
        node = None
        published = None
        try:
            binding = read(args.binding)
            node = NodeAPI(args.api, args.node_config, args.signing_public) if args.node_config else None
            if node:
                fetch = node.request
            else:
                token = args.token_file.read_text().strip()
                def fetch(path):
                    return api_read(args.api, path, token)
            published = fetch("/profile-topology")
            topology = json.loads(published["manifest"])
            execute("test", "-e", "/run/anet-flow/ready")
            try:
                validate(topology)
                profile, paths, _ = context(topology, binding)
                if "revision" in binding and (
                    binding.get("credential_id") != profile.get("credential_id")
                    or binding.get("ingress", "native") != profile.get("ingress", "native")
                    or profile_revoked(profile, binding)
                ):
                    raise ValueError("entry credentials or profile owner changed")
                pin_binding(execute, binding, topology)
            except (KeyError, TypeError, ValueError):
                # An authoritative removal or incompatible replacement also revokes
                # pinned connections. A disconnected panel is handled by lease expiry.
                execute("nft", "-f", "-", stdin="""delete table ip anet_flow
table ip anet_flow {
    chain input { type filter hook input priority filter; policy accept; iifname "anet-server" drop; }
    chain forward { type filter hook forward priority filter; policy drop; }
    chain dns_output { type filter hook output priority filter; policy accept; meta skgid 53153 drop; }
}
""")
                raise
            metrics = {}
            snapshot = {"links": []}
            if not profile_revoked(profile, binding):
                snapshot = fetch("/probe-links")
                metrics = observed_metrics(topology, snapshot, time.time())
            for name, p in paths.items():
                table, dev = str(p["table"]), p["interface"]
                execute("ip", "-4", "route", "replace", "unreachable", "default", "metric", "32767", "table", table)
                ok = execute("ip", "-4", "route", "replace", "default",
                             *(["via", p["gateway"]] if "gateway" in p else []),
                             "dev", dev, "metric", "10", "table", table, check=False)
                if ok.returncode:
                    metrics.pop(name, None)
                pref = str(11000 + p["table"])
                rules = execute("ip", "-4", "rule", "show", "pref", pref).stdout
                if not rules.strip():
                    execute("ip", "-4", "rule", "add", "pref", pref, "iif", "anet-server",
                            "fwmark", table, "lookup", table)
                elif not re.search(rf"iif anet-server.*fwmark {hex(p['table'])}.*lookup {table}\b|fwmark {hex(p['table'])}.*iif anet-server.*lookup {table}\b", rules):
                    raise ValueError(f"routing priority {pref} already in use")
                if name == binding.get("dns_path") and "dns_upstream" in binding:
                    pref = str(21000 + p["table"])
                    rules = execute("ip", "-4", "rule", "show", "pref", pref).stdout
                    if not rules.strip():
                        execute("ip", "-4", "rule", "add", "pref", pref, "iif", "lo",
                                "fwmark", table, "lookup", table)
                    elif not (f"fwmark {hex(p['table'])}" in rules and "iif lo" in rules
                              and rules.strip().endswith("lookup " + table)):
                        raise ValueError(f"DNS routing priority {pref} already in use")
            load_dns_answers(binding)
            now = time.time()
            batch, events = render(topology, binding, metrics, now, previous)
            # A single netlink transaction replaces classifications and lease timers.
            execute("nft", "-f", "-", stdin="delete table ip anet_flow\n" + batch)
            changed = events != previous or published["revision"] != last_revision
            prior = previous
            previous = events
            last_revision = published["revision"]
            if changed:
                last_report = 0
            if changed or (node and now - last_report >= 30):
                evidence = {name: link for name in paths for link in snapshot["links"]
                            if {"source_node": link["source_node"], **link["edge"]} == topology["paths"][name].get("probe")}
                result = dict(status="applied", decisions=events, observations=evidence)
                if changed:
                    print(json.dumps(dict(at=now, revision=published["revision"], profile=binding["profile"],
                                          previous=prior, **result)), flush=True)
                if node and binding.get("revision", published["revision"]) == published["revision"]:
                    node.request("/profile-result", dict(profile=binding["profile"], revision=published["revision"],
                                                        result=json.dumps(result)))
                last_report = now
        except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            print(f"flow policy update failed: {exc}", file=sys.stderr, flush=True)
            if node and published and time.time() - last_report >= 5:
                try:
                    node.request("/profile-result", dict(profile=binding["profile"], revision=published["revision"],
                                 result=json.dumps(dict(status="error", error=str(exc)[:512]))))
                    last_report = time.time()
                    last_revision = None
                except (OSError, ValueError):
                    pass
        time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", help="network holder for this dedicated profile")
    parser.add_argument("binding", type=Path)
    parser.add_argument("--api", required=True)
    credential = parser.add_mutually_exclusive_group(required=True)
    credential.add_argument("--token-file", type=Path)
    credential.add_argument("--node-config", type=Path, help="enrolled server TOML with control_plane credential")
    parser.add_argument("--signing-public", type=Path, help="pinned panel Ed25519 public key")
    args = parser.parse_args()
    if args.node_config and not args.signing_public:
        parser.error("--node-config requires --signing-public")
    run(args)
