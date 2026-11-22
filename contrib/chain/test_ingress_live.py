"""Run against a disposable rootless entry namespace and tunnel.py echo target."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PORT = 29571
PROBE = """
import secrets, socket, sys
addr, expected = sys.argv[1:]
for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
    with socket.socket(type=kind) as sock:
        sock.settimeout(4)
        sock.connect((addr, 29571))
        message = secrets.token_hex(12).encode() + b'\\n'
        sock.sendall(message)
        reply = sock.makefile('rb').readline(1024) if kind == socket.SOCK_STREAM else sock.recv(1024)
        if reply != expected.encode() + b' ' + message: raise ValueError('unexpected payload or source IP')
        print(kind.name, 'exit', expected, flush=True)
"""


OUTAGE = """
import socket, sys
for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
    with socket.socket(type=kind) as sock:
        sock.settimeout(2)
        try:
            sock.connect((sys.argv[1], 29571))
            sock.sendall(b'outage-probe' if kind == socket.SOCK_DGRAM else b'outage-probe\\n')
            if sock.recv(1024): raise SystemExit('received traffic during uplink outage')
        except (OSError, TimeoutError):
            pass
"""


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]}: {result.stderr.strip()}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("entry-container", "entry-ip", "network", "target-ip", "exit-ip"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--path", required=True, choices=["direct", "chain"])
    parser.add_argument("--outage-uplink", help="disposable uplink container to stop and restart")
    parser.add_argument("--target-container", help="local tunnel.py echo container to inspect for leaks")
    args = parser.parse_args()
    if bool(args.outage_uplink) != bool(args.target_container) or (args.outage_uplink and args.path != "chain"):
        parser.error("outage checks require both a chain uplink and its local echo receiver")
    if "name=rootless" not in docker("info", "--format", "{{json .SecurityOptions}}").stdout:
        raise RuntimeError("a rootless daemon is required")
    image = os.environ["INGRESS_IMAGE"]
    python = os.environ["PYTHON_IMAGE"]
    prefix = "adapter-qa-" + secrets.token_hex(4)
    os.umask(0o077)
    for protocol in ("wireguard", "vless"):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp) / "profile"
            subprocess.run([sys.executable, str(HERE / "issue-ingress.py"), protocol, str(root), "--host",
                            args.entry_ip, "--port", "51820" if protocol == "wireguard" else "8443",
                            "--dns", "1.1.1.1"], check=True)

            def start(role, *command):
                name = f"{prefix}-{protocol}-{role}"
                docker("run", "-d", "--name", name, "--cap-drop", "ALL", "--pids-limit", "96",
                       "--memory", "160m", "--cpus", "0.5", *command)
                stack.callback(docker, "rm", "-f", name, check=False)
                stack.callback(docker, "stop", "--time", "2", name, check=False)
                return name

            server = root / "server"
            if protocol == "vless":
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                                "-subj", "/CN=adapter.test", "-addext", "subjectAltName=DNS:adapter.test",
                                "-keyout", str(server / "tls.key"), "-out", str(server / "tls.pem")],
                               check=True, stderr=subprocess.DEVNULL)
            capabilities = [value for cap in ("NET_ADMIN", "NET_RAW", "CHOWN", "SETPCAP", "SETGID", "SETUID")
                            for value in ("--cap-add", cap)]
            adapter = start("server", "--network", "container:" + args.entry_container, *capabilities,
                            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/run:mode=0755",
                            "-v", f"{server}:/input:ro", "-e", "INGRESS_PATH=" + args.path, image, protocol)

            if protocol == "wireguard":
                # Only the echo target uses the VPN, keeping the outer handshake on the test LAN.
                lines = (root / "client.conf").read_text().splitlines()
                (root / "wg.conf").write_text("\n".join(line for line in lines if not line.startswith(
                    ("Address =", "DNS =", "MTU ="))) + "\n")
                client = start("client", "--network", args.network, "--cap-add", "NET_ADMIN", "--entrypoint", "sh",
                               "-v", f"{root}/wg.conf:/wg.conf:ro", image, "-ec",
                               'ip link add wg-test type wireguard; wg setconf wg-test /wg.conf; '
                               'ip addr add 10.79.0.2/32 dev wg-test; ip link set wg-test mtu 1280 up; '
                               'ip route replace "$1/32" dev wg-test; exec sleep infinity', "client", args.target_ip)
                target = args.target_ip
            else:
                uri = urlsplit((root / "client.uri").read_text().strip())
                config = {"inbounds": [{"listen": "127.0.0.1", "port": PORT, "protocol": "dokodemo-door",
                          "settings": {"address": args.target_ip, "port": PORT, "network": "tcp,udp"}}],
                          "outbounds": [{"protocol": "vless", "settings": {"vnext": [
                              {"address": uri.hostname, "port": uri.port, "users": [
                                  {"id": uri.username, "encryption": "none"}]}]}, "streamSettings": {
                                      "network": "tcp", "security": "tls", "tlsSettings": {"serverName": "adapter.test"}}}]}
                (root / "xray.json").write_text(json.dumps(config))
                client = start("client", "--network", args.network, "--entrypoint", "xray",
                               "-e", "SSL_CERT_FILE=/test.pem", "-v", f"{server}/tls.pem:/test.pem:ro",
                               "-v", f"{root}/xray.json:/xray.json:ro", image, "run", "-config", "/xray.json")
                target = "127.0.0.1"

            def verify():
                for attempt in range(5):
                    result = docker("run", "--rm", "--cap-drop", "ALL", "--network", "container:" + client,
                                    python, "python3", "-c", PROBE, target, args.exit_ip, check=False)
                    if result.returncode == 0:
                        print(protocol, args.path, result.stdout.strip(), flush=True)
                        break
                    if attempt == 4:
                        for name in (adapter, client):
                            state = docker("inspect", "-f", "{{.State.Status}}:{{.State.ExitCode}}", name).stdout.strip()
                            print(name, state, file=sys.stderr)
                        raise RuntimeError(f"{protocol} {args.path} probe failed: {result.stderr.strip()}")
                    time.sleep(1)

            verify()
            if args.outage_uplink:
                before = docker("logs", args.target_container).stdout.count("outage-probe")
                docker("stop", "--time", "3", args.outage_uplink)
                try:
                    docker("run", "--rm", "--cap-drop", "ALL", "--network", "container:" + client,
                           python, "python3", "-c", OUTAGE, target)
                    after = docker("logs", args.target_container).stdout.count("outage-probe")
                    if before != after:
                        raise RuntimeError("UDP reached receiver during uplink outage")
                finally:
                    docker("start", args.outage_uplink)
                verify()
                print(protocol, "uplink outage and recovery passed", flush=True)


if __name__ == "__main__":
    main()
