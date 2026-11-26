"""Render the overlay for an isolated native ANet entry with several allowed uplinks."""
import argparse
import ipaddress
import json
from pathlib import Path
import re

from topology import load_toml


def overlay(binding):
    result = {"network": {"entrypoint": ["/flow-init.sh"], "command": [],
                          "tmpfs": ["/run/anet-flow:size=64k,mode=0700"],
                          "sysctls": {"net.ipv4.conf.all.rp_filter": "2", "net.ipv4.conf.default.rp_filter": "2",
                                      "net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"},
                          "healthcheck": {"test": ["CMD", "test", "-f", "/run/anet-flow/ready"]}}}
    if "dns_upstream" in binding:
        if binding.get("dns_path") not in binding["paths"]:
            raise ValueError("select a declared DNS uplink with dns_path")
        listen = ipaddress.IPv4Address(binding["dns_listen"])
        if listen not in ipaddress.IPv4Network(binding["source"]):
            raise ValueError("DNS listener outside profile network")
        file = Path(binding["dns_answers_file"])
        if not file.is_absolute() or file.name != "answers.json" or not file.parent.is_dir():
            raise ValueError("create a private observations directory and use its absolute answers.json path")
        result["network"]["sysctls"]["net.ipv4.ip_unprivileged_port_start"] = "53"
        result["profile-dns"] = {
            "image": "${DNS_IMAGE:?set DNS_IMAGE}", "network_mode": "service:network",
            "depends_on": {"network": {"condition": "service_healthy"}},
            "environment": {"DNS_SOURCE": binding["source"], "DNS_LISTEN": str(listen),
                            "DNS_UPSTREAM": str(ipaddress.IPv4Address(binding["dns_upstream"]))},
            "user": "0:53153", "cap_drop": ["ALL"], "read_only": True,
            "security_opt": ["no-new-privileges:true"], "restart": "unless-stopped",
            "pids_limit": 32, "mem_limit": "64m",
            "volumes": [{"type": "bind", "source": str(file.parent), "target": "/observations",
                         "bind": {"create_host_path": False}}]}
    for path in binding["paths"].values():
        if "gateway" in path:
            ipaddress.IPv4Address(path["gateway"])
            continue
        dev = path["interface"]
        if not re.fullmatch(r"anet-up[0-9]{1,4}", dev) or dev in result:
            raise ValueError(f"invalid or repeated uplink {dev}")
        file = Path(path["config"]).resolve(strict=True)
        config = load_toml(file)
        if config["main"]["tun_name"] != dev or config["main"].get("manual_routing") is not True:
            raise ValueError(f"{file} requires manual_routing and tun_name={dev}")
        result[dev] = {
            "image": "${CLIENT_IMAGE:?set CLIENT_IMAGE}", "network_mode": "service:network",
            "depends_on": {"network": {"condition": "service_healthy"}}, "devices": ["/dev/net/tun:/dev/net/tun:rw"],
            "user": "0:0", "cap_drop": ["ALL"], "cap_add": ["NET_ADMIN"],
            "security_opt": ["no-new-privileges:true"], "read_only": True, "restart": "unless-stopped",
            "pids_limit": 128, "mem_limit": "${CLIENT_MEMORY:-256m}", "cpus": "${CLIENT_CPUS:-1}",
            "command": ["-c", "/etc/anet/uplink.toml"],
            "volumes": [{"type": "bind", "source": str(file), "target": "/etc/anet/uplink.toml",
                         "read_only": True, "bind": {"create_host_path": False}}]}
    return {"services": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binding", type=Path)
    print(json.dumps(overlay(json.loads(parser.parse_args().binding.read_text())), indent=2))
