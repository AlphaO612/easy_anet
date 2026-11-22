"""Exercise resolver routing on two private Docker bridges, including a reachable main route."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import time

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
from topology import validate

RENDER = runpy.run_path(str(DEPLOY / "scripts/flow-router.py"))["render"]
IP = "192.0.2.53"
ECHO = """
import socket, sys, threading
def serve(kind):
    with socket.socket(type=kind) as sock:
        sock.bind(('192.0.2.53',53))
        if kind == socket.SOCK_STREAM: sock.listen()
        while True:
            if kind == socket.SOCK_DGRAM:
                message, addr = sock.recvfrom(128)
                print(message.decode(), flush=True)
                sock.sendto(addr[0].encode(), addr)
            else:
                conn, addr = sock.accept()
                with conn:
                    print(conn.recv(128).decode(), flush=True)
                    conn.sendall(addr[0].encode())
threading.Thread(target=serve, args=(socket.SOCK_DGRAM,),daemon=True).start()
serve(socket.SOCK_STREAM)
"""
PROBE = """
import socket, sys
for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
    with socket.socket(type=kind) as sock:
        sock.settimeout(1.5)
        try:
            sock.connect(('192.0.2.53',53))
            sock.sendall(sys.argv[2].encode())
            seen=sock.recv(128).decode()
            assert seen == sys.argv[1], (seen,sys.argv[1])
        except OSError:
            if sys.argv[1] != 'drop': raise
"""


def docker(*args, stdin=None):
    result = subprocess.run([os.environ.get("DOCKER_CLI", "docker"), *args], text=True, input=stdin, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]}: {result.stderr}")
    return result.stdout.strip()


def run():
    prefix = "flow-dns-" + secrets.token_hex(4)
    python, netns = os.environ["PYTHON_IMAGE"], os.environ["NETNS_IMAGE"]
    with ExitStack() as stack:
        bridges = [prefix + "-main", prefix + "-selected"]
        for bridge in bridges:
            docker("network", "create", bridge)
            stack.callback(docker, "network", "rm", bridge)

        def start(role, img, *cmd):
            name = prefix + "-" + role
            docker("run", "-d", "--name", name, "--network", bridges[0], "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                   "--user", "0", "--memory", "80m", "--pids-limit", "32", "--entrypoint", cmd[0], img, *cmd[1:])
            stack.callback(docker, "rm", "-f", name)
            docker("network", "connect", bridges[1], name)
            return name

        router = start("router", netns, "sleep", "infinity")
        receiver = start("receiver", python, "sh", "-ec", 'ip addr add 192.0.2.53/32 dev lo; exec python3 -c "$1"', "sh", ECHO)

        def address(name, bridge):
            return json.loads(docker("inspect", name))[0]["NetworkSettings"]["Networks"][bridge]["IPAddress"]

        def ip(*args):
            return docker("exec", router, "ip", *args)

        expected = address(router, bridges[1])
        iface = next(d["ifname"] for d in json.loads(ip("-j", "-4", "addr"))
                     if any(i["local"] == expected for i in d["addr_info"]))
        ip("route", "add", IP + "/32", "via", address(receiver, bridges[0]))
        ip("route", "add", "unreachable", "default", "metric", "32767", "table", "1234")
        ip("route", "add", "default", "via", address(receiver, bridges[1]), "dev", iface, "table", "1234")
        ip("rule", "add", "pref", "22234", "iif", "lo", "fwmark", "1234", "lookup", "1234")
        path = dict(entry="entry", exit="exit", transport="quic", external_ip=expected)
        profile = dict(entry="entry", paths=["path"], strategy="prefer-route")
        topology = dict(paths=dict(path=path), profiles=dict(profile=profile))
        binding = dict(profile="profile", user="user", source="10.77.0.0/24", dns_listen="10.77.0.2",
                       dns_path="path", dns_upstream=IP, paths=dict(path=dict(table=1234,interface=iface)))
        validate(topology)
        docker("exec", router, "nft", "add", "table", "ip", "anet_flow")

        def policy(live):
            now = time.time()
            batch, _ = RENDER(topology, binding, {"path": dict(up=live, expires=now + 40)}, now, {})
            docker("exec", "-i", router, "nft", "-f", "-", stdin="delete table ip anet_flow\n" + batch)

        def check(expected_peer):
            nonce = secrets.token_hex(16)
            docker("run", "--rm", "--user", "0:53153", "--cap-drop", "ALL", "--memory", "64m", "--pids-limit", "32",
                   "--network", "container:" + router, python, "python3", "-c", PROBE, expected_peer, nonce)
            if expected_peer == "drop" and nonce in docker("logs", receiver):
                raise RuntimeError("DNS traffic leaked through the main route")

        check_initial = (DEPLOY.parent / "docker" / "flow-init.sh").read_text().split("<<'NFT'\n", 1)[1].split("\nNFT", 1)[0]
        docker("exec", "-i", router, "nft", "-f", "-", stdin=check_initial + "\n")
        check("drop")
        policy(True)
        check(expected)
        policy(False)
        check("drop")
        policy(True)
        ip("route", "del", "default", "via", address(receiver, bridges[1]), "dev", iface, "table", "1234")
        check("drop")
        ip("rule", "del", "pref", "22234")
        check("drop")  # Main route works but POSTROUTING rejects the wrong final interface.
        print("UDP/TCP DNS used the selected egress and stopped on lease and route loss")


if __name__ == "__main__":
    run()
