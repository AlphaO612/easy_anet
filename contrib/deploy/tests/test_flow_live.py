"""Check flow-router.render against the Linux forwarding plane in disposable rootless containers."""
from contextlib import ExitStack
import ipaddress
import json
import os
from pathlib import Path
import runpy
import secrets
import socket
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve()
DEST = "198.18.197.40"
OTHER = "198.18.197.41"
PORT = 29574


def echo():
    def udp(local):
        with socket.socket(type=socket.SOCK_DGRAM) as sock:
            sock.bind((local, PORT))
            while True:
                data, peer = sock.recvfrom(256)
                print(data.decode(), flush=True)
                sock.sendto(peer[0].encode() + b" " + data, peer)
    for local in (DEST, OTHER):
        threading.Thread(target=udp, args=(local,), daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("", PORT))
        sock.listen()
        while True:
            conn, peer = sock.accept()
            with conn:
                data = conn.recv(256)
                print(data.decode(), flush=True)
                conn.sendall(peer[0].encode() + b" " + data)


def probe(proto, dest, sport, expected, nonce):
    with socket.socket(type=socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.bind(("", int(sport)))
        try:
            sock.connect((dest, PORT))
            sock.sendall(nonce.encode())
            data = sock.recv(256)
        except (TimeoutError, ConnectionError):
            if expected == "drop":
                return
            raise
        assert data == (expected + " " + nonce).encode(), (data, expected)


def docker(*args, stdin=None):
    p = subprocess.run(["docker", *args], input=stdin, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(f"docker {args[0]}: {p.stderr.strip()}")
    return p.stdout.strip()


def acceptance():
    if "name=rootless" not in docker("info", "--format", "{{json .SecurityOptions}}"):
        raise RuntimeError("select a rootless Docker daemon")
    sys.path.insert(0, str(HERE.parents[1] / "scripts"))
    render = runpy.run_path(str(HERE.parents[1] / "scripts/flow-router.py"))["render"]
    image = os.environ["NETNS_IMAGE"]
    python = os.environ["PYTHON_IMAGE"]
    prefix = "flow-qa-" + secrets.token_hex(4)
    with ExitStack() as cleanup:
        def network(suffix):
            name = prefix + "-" + suffix
            docker("network", "create", name)
            cleanup.callback(docker, "network", "rm", name)
            return name

        lan, one, two = (network(label) for label in ("lan", "one", "two"))

        def start(role, bridge, img, *args, forwarding=False):
            name = prefix + "-" + role
            sysctls = ["--sysctl", "net.ipv4.ip_forward=1"] if forwarding else []
            docker("run", "-d", "--network", bridge, "--name", name, "--cap-drop", "ALL", "--cap-add",
                   "NET_ADMIN", "--pids-limit", "64", "--memory", "80m", "--cpus", "0.4", *sysctls,
                   "-v", f"{HERE}:/test.py:ro", "--entrypoint", args[0], "--user", "0", img, *args[1:])
            cleanup.callback(docker, "rm", "-f", name)
            return name

        router = start("router", lan, image, "sleep", "infinity", forwarding=True)
        client = start("client", lan, python, "sleep", "infinity")
        receiver = start("receiver", one, python, "sh", "-ec",
                         f"ip addr add {DEST}/32 dev lo; ip addr add {OTHER}/32 dev lo; exec python3 /test.py echo")
        for container, bridge in ((router, one), (router, two), (receiver, two)):
            docker("network", "connect", bridge, container)

        def address(container, bridge):
            return json.loads(docker("inspect", container))[0]["NetworkSettings"]["Networks"][bridge]["IPAddress"]

        def ip(container, *args):
            return docker("exec", container, "ip", *args)

        def interface(addr):
            for dev in json.loads(ip(router, "-j", "-4", "addr")):
                if any(a["local"] == addr for a in dev["addr_info"]):
                    return dev["ifname"]
            raise ValueError(addr)

        ingress = address(router, lan)
        old = interface(ingress)
        ip(router, "link", "set", old, "down")
        ip(router, "link", "set", old, "name", "anet-server")
        ip(router, "link", "set", "anet-server", "up")
        for dest in (DEST, OTHER):
            ip(client, "route", "replace", dest + "/32", "via", ingress)

        paths, bindings, outputs = {}, {}, {}
        for idx, (name, bridge) in enumerate((("one", one), ("two", two))):
            outputs[name] = address(router, bridge)
            iface, gateway, table = interface(outputs[name]), address(receiver, bridge), 1401 + idx
            bindings[name] = dict(interface=iface, gateway=gateway, table=table)
            paths[name] = dict(entry="entry", exit=name, transport="tcp", external_ip=outputs[name])
            ip(router, "route", "add", "unreachable", "default", "metric", "32767", "table", str(table))
            ip(router, "route", "add", "default", "via", gateway, "dev", iface, "metric", "10", "table", str(table))
            ip(router, "rule", "add", "pref", str(11000 + table), "iif", "anet-server",
               "fwmark", str(table), "lookup", str(table))
        # A working main-table path makes any fallback leak observable at the receiver.
        ip(router, "route", "add", str(ipaddress.ip_network(DEST + "/31", strict=False)), "via", address(receiver, one))
        binding = dict(profile="alice", user="alice", source=address(client, lan) + "/32", paths=bindings)
        rule = dict(id="cidr", cidr=DEST + "/32", allow=["two"])
        profile = dict(entry="entry", strategy="prefer-route", paths=list(paths), rules=[rule])
        topology = dict(paths=paths, profiles={"alice": profile})
        previous = {}
        docker("exec", router, "nft", "add", "table", "ip", "anet_flow")

        def apply(live, ttl=40):
            nonlocal previous
            now = time.time()
            metrics = {p: dict(up=True, expires=now + ttl) for p in live}
            batch, events = render(topology, binding, metrics, now, previous)
            docker("exec", "-i", router, "nft", "-f", "-", stdin="delete table ip anet_flow\n" + batch)
            previous = events

        def check(proto, dest, port, path):
            nonce = secrets.token_hex(8)
            docker("exec", client, "python3", "/test.py", "probe", proto, dest, str(port),
                   outputs.get(path, "drop"), nonce)
            if path == "drop" and nonce in docker("logs", receiver):
                raise RuntimeError("packet reached receiver during blackout")

        apply(paths)
        check("tcp", OTHER, 0, "one")
        check("udp", DEST, 41101, "two")
        rule["allow"] = ["one"]
        apply(paths)
        check("udp", DEST, 41101, "two")
        check("udp", DEST, 41102, "one")
        rule["allow"] = list(paths)
        apply(["two"])
        check("tcp", OTHER, 0, "two")
        check("udp", DEST, 41102, "drop")
        apply([])
        check("udp", OTHER, 41103, "drop")
        check("tcp", DEST, 0, "drop")
        apply(["one"], ttl=2)
        check("udp", DEST, 41104, "one")
        time.sleep(2.2)
        check("udp", DEST, 41104, "drop")
        print("CIDR TCP/UDP, two egress addresses, UDP pinning, failover, blackout and lease expiry passed")


if __name__ == "__main__":
    if sys.argv[1:2] == ["echo"]:
        echo()
    elif sys.argv[1:2] == ["probe"]:
        probe(*sys.argv[2:])
    else:
        acceptance()
