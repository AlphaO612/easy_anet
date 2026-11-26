#!/usr/bin/env python3
"""Issue a single WireGuard or VLESS ingress profile into a new private directory."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import uuid


def wg_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    raw = serialization.Encoding.Raw
    key = X25519PrivateKey.generate()
    private = key.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    return tuple(base64.b64encode(value).decode("ascii") for value in (private, public))


def wireguard(host, port, dns):
    server, server_pub = wg_pair()
    client, client_pub = wg_pair()
    server_config = f"""[Interface]
PrivateKey = {server}
ListenPort = 51820

[Peer]
PublicKey = {client_pub}
AllowedIPs = 10.79.0.2/32
"""
    client_config = f"""[Interface]
PrivateKey = {client}
Address = 10.79.0.2/32
DNS = {dns}
MTU = 1280

[Peer]
PublicKey = {server_pub}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {host}:{port}
PersistentKeepalive = 25
"""
    return {"server/wg.conf": server_config, "client.conf": client_config}


def vless(host, port, dns):
    credential = str(uuid.uuid4())
    stream = {"network": "tcp", "security": "tls", "tlsSettings": {"minVersion": "1.2",
              "certificates": [{"certificateFile": "/run/tls.pem", "keyFile": "/run/tls.key"}]}}
    config = {
        "log": {"loglevel": "warning", "access": "none"},
        "dns": {"servers": [str(dns)], "queryStrategy": "UseIPv4"},
        "inbounds": [{"tag": "vless-in", "listen": "0.0.0.0", "port": 8443, "protocol": "vless",
                      "settings": {"clients": [{"id": credential}], "decryption": "none"},
                      "streamSettings": stream}],
        "outbounds": [{"protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}}],
    }
    uri = f"vless://{credential}@{host}:{port}?encryption=none&security=tls&type=tcp&sni={host}\n"
    return {"server/server.json": json.dumps(config, indent=2) + "\n", "client.uri": uri}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", choices=("wireguard", "vless"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--host", required=True, help="Endpoint DNS hostname or IPv4")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--dns", required=True, type=ipaddress.IPv4Address)
    args = parser.parse_args()
    labels = args.host.split(".")
    if len(args.host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x) for x in labels):
        parser.error("invalid endpoint hostname")
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if args.dns.is_loopback or args.dns.is_unspecified or args.dns.is_multicast:
        parser.error("DNS must be reachable via the selected egress")
    generate = wireguard if args.protocol == "wireguard" else vless
    files = generate(args.host, args.port, args.dns)
    os.umask(0o077)
    args.directory.mkdir(mode=0o700)
    try:
        (args.directory / "server").mkdir(mode=0o700)
        for name, data in files.items():
            with (args.directory / name).open("x") as output:
                output.write(data)
    except BaseException:
        shutil.rmtree(args.directory)
        raise


if __name__ == "__main__":
    main()
