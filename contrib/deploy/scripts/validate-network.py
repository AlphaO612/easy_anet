"""Check resolved Compose network parameters before recording first-install state."""
import ipaddress
import json
from pathlib import Path
import sys


def settings(services, profile):
    network = services["network"]
    env = network["environment"]
    subnet = ipaddress.IPv4Network(env["TUN_SUBNET"])
    interface = ipaddress.IPv4Interface(env["TUN_ADDRESS"])
    if subnet.prefixlen > 29 or interface.ip != subnet[2] or interface.network != subnet:
        raise ValueError("TUN_ADDRESS must be the server address (subnet base + 2) with the TUN_SUBNET prefix")

    def published(target, protocol):
        ports = [p["published"] for p in network["ports"]
                 if int(p["target"]) == target and p["protocol"] == protocol]
        if not ports or len(set(ports)) != 1 or not 1 <= int(ports[0]) <= 65535:
            raise ValueError(f"expected one fixed published port for {target}/{protocol}")
        return int(ports[0])

    native_port = published(4519, "udp")
    if profile == "anet-easy":
        protocol, = services["ingress"]["command"]
        if protocol not in ("wireguard", "vless"):
            raise ValueError("INGRESS_PROTOCOL must be wireguard or vless")
        target, transport = (51820, "udp") if protocol == "wireguard" else (8443, "tcp")
        published(target, transport)
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                print("Uplink TOML was not preflighted. Python 3.11 or tomli enables this check.", file=sys.stderr)
                return native_port, subnet
        volume = next(v for v in services["uplink"]["volumes"] if v["target"] == "/etc/anet/uplink.toml")
        with Path(volume["source"]).open("rb") as source:
            main = tomllib.load(source).get("main", {})
        if main.get("manual_routing") is not True or main.get("tun_name") != "anet-uplink":
            raise ValueError("uplink must specify manual_routing=true and tun_name=anet-uplink")
    return native_port, subnet


if __name__ == "__main__":
    try:
        print(*settings(json.load(sys.stdin)["services"], sys.argv[1]))
    except (ValueError, KeyError, OSError, StopIteration, TypeError) as error:
        sys.exit(f"Invalid resolved Compose network: {error}")
