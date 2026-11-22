"""Rootless standalone acceptance. Needs SERVER_IMAGE, CLIENT_IMAGE and NETNS_IMAGE."""
import argparse
import contextlib
import errno
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

PORT = 29571
INTERFACE = "anet-client"


def echo():
    def response(peer, data):
        identity = b""
        if data.startswith(b"identify "):
            identity = os.environ.get("ECHO_ID", "").encode() + b" "
        return peer[0].encode() + b" " + identity + data

    def udp():
        with socket.socket(type=socket.SOCK_DGRAM) as sock:
            sock.bind(("", PORT))
            while True:
                data, peer = sock.recvfrom(2048)
                if data == b"outage-probe":
                    print("outage-probe", flush=True)
                sock.sendto(response(peer, data), peer)
    threading.Thread(target=udp, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("", PORT))
        sock.listen()
        while True:
            conn, peer = sock.accept()
            with conn:
                with conn.makefile("rb") as f:
                    data = f.readline(1024)
                conn.sendall(response(peer, data))


def public_exit(host, target, expected):
    with socket.socket() as stream:
        stream.settimeout(6)
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode() + b"\0")
        stream.connect((target, 443))
        with ssl.create_default_context().wrap_socket(stream, server_hostname=host) as tls:
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            response = http.client.HTTPResponse(tls)
            response.begin()
            if response.status != 200:
                raise RuntimeError(f"{host} returned {response.status}")
            observed = ipaddress.IPv4Address(response.read(128).decode().strip())
            if observed != ipaddress.IPv4Address(expected):
                raise RuntimeError(f"expected public exit {expected}, received {observed}")
            print(f"{host} observed public IPv4 {observed}")


def blocked(target):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(type=kind) as sock:
            sock.settimeout(3)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode() + b"\0")
            try:
                sock.connect((target, PORT))
                sock.sendall(b"outage-probe")
                if kind == socket.SOCK_STREAM or sock.recv(2048):
                    raise RuntimeError(f"{kind.name} reached the echo service during the outage")
            except TimeoutError:
                pass
            except OSError as exc:
                if exc.errno not in (errno.ENETUNREACH, errno.EHOSTUNREACH):
                    raise
            print(kind.name, "blocked", target)


def probe(target, expected_peer, expected_id=None):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(type=kind) as sock:
            sock.settimeout(3)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode() + b"\0")
            sock.connect((target, PORT))
            nonce = secrets.token_hex(32).encode() + b"\n"
            if expected_id:
                nonce = b"identify " + nonce
            sock.sendall(nonce)
            identity = expected_id.encode() + b" " if expected_id else b""
            expected = expected_peer.encode() + b" " + identity + nonce
            if kind == socket.SOCK_STREAM:
                with sock.makefile("rb") as stream:
                    answer = stream.readline(1024)
            else:
                answer = sock.recv(2048)
            if answer != expected:
                raise RuntimeError(f"{kind.name} reply or exit address mismatch")
            print(kind.name, "source", sock.getsockname()[0], "exit", expected_peer)


def docker(*args, check=True, input_text=None):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, input=input_text)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result


def acceptance():
    if "name=rootless" not in docker("info", "--format", "{{json .SecurityOptions}}").stdout:
        raise RuntimeError("select a rootless Docker daemon")
    prefix = "anet-qa-" + secrets.token_hex(5)
    netns = os.environ["NETNS_IMAGE"]
    test_image = os.environ.get("PYTHON_IMAGE",
        "python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e")
    test_mount = f"{Path(__file__).resolve()}:/qa/tunnel.py:ro"
    limits = ["--pids-limit", "64", "--memory", "192m", "--cpus", "0.5"]
    with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as cleanup:
        network = prefix + "-lan"
        docker("network", "create", network)
        cleanup.callback(docker, "network", "rm", network, check=False)

        def start(role, *args):
            name = prefix + "-" + role
            cleanup.callback(docker, "rm", "-f", name, check=False)
            docker("run", "--name", name, "-d", *limits, *args)
            return name

        def address(name):
            return docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name).stdout.strip()

        holder = start("network", "--network", network, "--device", "/dev/net/tun",
                       "--user", "0", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                       "--cap-add", "SETPCAP", "--cap-add", "SETUID", "--cap-add", "SETGID",
                       "--read-only", "--sysctl", "net.ipv4.ip_forward=1", "-e", "IF_NAME=anet-server",
                       "-e", "TUN_ADDRESS=10.77.0.2/24", "-e", "TUN_SUBNET=10.77.0.0/24",
                       "-e", "TUN_MTU=1420", netns, "prepare-and-hold")
        exit_ip = address(holder)
        state = Path(temp) / "state"
        init = Path(__file__).resolve().parents[1] / "scripts/init-state.py"
        cmd = [sys.executable, str(init), str(state), exit_ip, "4519", "standalone"]
        subprocess.run(cmd, check=True)
        client_cfg = state / "client.toml"
        with client_cfg.open("a") as f:
            f.write('\n[main]\nmanual_routing = true\ntun_name = "anet-client"\n')
        def profile_hash():
            return hashlib.sha256(client_cfg.read_bytes()).digest()
        initial_hash = profile_hash()
        subprocess.run(cmd, check=True)
        if initial_hash != profile_hash():
            raise RuntimeError("repeated initialization changed the client profile")
        for _ in range(25):
            if docker("exec", holder, "ip", "link", "show", "anet-server", check=False).returncode == 0:
                break
            time.sleep(0.4)
        server = start("server", "--network", "container:" + holder, "--device", "/dev/net/tun",
                       "--read-only", "--user", "65532:65532", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges", "-e", "ANET_PRECONFIGURED_TUN=1",
                       "-v", f"{state}/server.toml:/etc/anet/server.toml:ro", os.environ["SERVER_IMAGE"])
        target = start("target", "--network", network, "--cap-drop", "ALL", "--read-only", "-v", test_mount,
                       test_image, "python3", "/qa/tunnel.py", "echo")
        client = start("client", "--network", network, "--device", "/dev/net/tun", "--user", "0",
                       "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "-v",
                       f"{client_cfg}:/etc/anet/client.toml:ro", os.environ["CLIENT_IMAGE"], "-c", "/etc/anet/client.toml")
        target_ip = address(target)
        for attempt in range(2):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                route = docker("run", "--rm", "--network", "container:" + client, "--user", "0",
                               "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--entrypoint", "ip",
                               netns, "route", "replace", target_ip + "/32", "dev", INTERFACE, check=False)
                if route.returncode == 0:
                    result = docker("run", "--rm", "--network", "container:" + client, "--cap-drop", "ALL",
                                    "--cap-add", "NET_RAW", "--read-only", "-v", test_mount, test_image,
                                    "python3", "/qa/tunnel.py", "probe", target_ip, exit_ip, check=False)
                    if result.returncode == 0:
                        print(result.stdout, end="")
                        break
                time.sleep(1)
            else:
                for name in (server, client):
                    print(docker("logs", "--tail", "12", name, check=False).stderr, file=sys.stderr)
                raise RuntimeError("tunnel readiness deadline exceeded")
            if attempt == 0:
                docker("restart", server)
        print("QUIC tunnel passed before and after server restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["accept", "echo", "probe", "blocked", "public-exit"])
    parser.add_argument("addresses", nargs="*")
    args = parser.parse_args()
    if args.action == "accept":
        acceptance()
    elif args.action == "public-exit":
        public_exit(*args.addresses)
    elif args.action == "blocked":
        blocked(*args.addresses)
    elif args.action == "echo":
        echo()
    else:
        probe(*args.addresses)
