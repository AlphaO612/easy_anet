"""Run all four strategies through five Linux routers, two exits and the profile DNS service."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve()
DEPLOY = HERE.parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
from topology import observed_metrics, validate

SINK = "192.0.2.53"


def run(*args, data=None):
    return subprocess.run(args, input=data, text=True, check=True, stdout=subprocess.PIPE).stdout.strip()


def inside(ns, *args):
    return ("ip", "netns", "exec", ns, *args)


def interface(ns1, dev1, addr1, ns2, dev2, addr2):
    run("ip", "link", "add", "v1", "type", "veth", "peer", "name", "v2")
    for temporary, ns, dev, addr in (("v1", ns1, dev1, addr1), ("v2", ns2, dev2, addr2)):
        run("ip", "link", "set", temporary, "netns", ns)
        run("ip", "-n", ns, "link", "set", temporary, "name", dev)
        run("ip", "-n", ns, "address", "add", addr + "/30", "dev", dev)
        run("ip", "-n", ns, "link", "set", dev, "up")


def endpoint(log):
    import dns.message
    import dns.rrset

    def response(port, data, peer):
        if port == 53:
            query = dns.message.from_wire(data)
            answer = dns.message.make_response(query)
            answer.answer.append(dns.rrset.from_text(query.question[0].name, 25, "IN", "A", SINK))
            data = answer.to_wire()
            note = str(query.question[0].name)
        else:
            note = data.decode()
            data = peer.encode()
        with open(log, "a") as out:
            print(peer, note, file=out)
        return data

    class Udp(socketserver.DatagramRequestHandler):
        def handle(self):
            self.wfile.write(response(self.server.server_address[1], self.rfile.read(), self.client_address[0]))

    class Tcp(socketserver.StreamRequestHandler):
        def handle(self):
            port = self.server.server_address[1]
            if port == 53:
                size, = struct.unpack("!H", self.rfile.read(2))
                answer = response(port, self.rfile.read(size), self.client_address[0])
                self.wfile.write(struct.pack("!H", len(answer)) + answer)
            else:
                for payload in self.rfile:
                    self.wfile.write(response(port, payload.strip(), self.client_address[0]) + b"\n")

    for port in (53, 5400):
        for cls, handler in ((socketserver.ThreadingUDPServer, Udp), (socketserver.ThreadingTCPServer, Tcp)):
            server = cls((SINK, port), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
    print("ready", flush=True)
    threading.Event().wait()


def probe(kind, port, nonce):
    with socket.socket(type=socket.SOCK_STREAM if kind == "tcp" else socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.bind(("", int(port)))
        try:
            client.connect((SINK, 5400))
            client.sendall(nonce.encode() + (b"\n" if kind == "tcp" else b""))
            print(client.recv(128).decode().strip())
        except (TimeoutError, ConnectionError):
            print("drop")


def lab(work, processes):
    controller = runpy.run_path(str(DEPLOY / "scripts/flow-router.py"))
    run("mount", "--make-rprivate", "/")
    run("mount", "-t", "tmpfs", "tmpfs", "/run")
    routers = [f"entry{i}" for i in range(5)]
    exits = ["exit0", "exit1"]
    clients = [f"client{i}" for i in range(5)]
    for ns in [*routers, *exits, *clients, "sink"]:
        run("ip", "netns", "add", ns)
        run("ip", "-n", ns, "link", "set", "lo", "up")
        if ns in routers + exits:
            run(*inside(ns, "sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=2",
                        "net.ipv4.conf.default.rp_filter=2"))
    run("ip", "-n", "sink", "address", "add", SINK + "/32", "dev", "lo")
    public = ["198.18.0.1", "198.18.1.1"]
    for j, ns in enumerate(exits):
        peer = f"198.18.{j}.2"
        interface(ns, "public", public[j], "sink", ns, peer)
        run("ip", "-n", ns, "route", "add", SINK + "/32", "via", peer)
        run(*inside(ns, "nft", "-f", "-"), data=(
            "table ip measured {\nchain traffic { type filter hook forward priority filter; }\n}\n"))
        run(*inside(ns, "nft", "-f", "-"), data='table ip exit_nat { chain nat { type nat hook postrouting priority srcnat; oifname "public" masquerade; }\n}\n')

    def start(ns, *args, env=None):
        p = subprocess.Popen(inside(ns, *args), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def stop():
            p.terminate()
            p.wait(timeout=5)
            p.stdout.close()
            p.stdin.close()
        processes.callback(stop)
        return p

    log = work / "packets"
    assert start("sink", sys.executable, str(HERE), "endpoint", str(log)).stdout.readline().strip() == "ready"
    topology = dict(paths={}, profiles={})
    bindings, links = {}, []
    for i, (ns, client) in enumerate(zip(routers, clients)):
        listener = f"10.77.{i}.1"
        interface(ns, "anet-server", listener, client, "vpn", f"10.77.{i}.2")
        run("ip", "-n", client, "route", "add", "default", "via", listener)
        direct_peer = f"10.254.{i}.2"
        interface(ns, "direct", f"10.254.{i}.1", "sink", f"direct{i}", direct_peer)
        run("ip", "-n", ns, "route", "add", "default", "via", direct_peer)
        run("ip", "-n", "sink", "route", "add", f"10.77.{i}.0/30", "via", f"10.254.{i}.1")
        paths = {}
        for k in range(4):
            exit_id = k // 2
            name = f"{ns}-path{k}"
            peer = f"10.{i + 1}.{k}.2"
            dev, table = f"uplink{k}", str(1000 + k)
            interface(ns, dev, f"10.{i + 1}.{k}.1", exits[exit_id], f"in{i}p{k}", peer)
            run("ip", "-n", ns, "route", "add", "unreachable", "default", "metric", "32767", "table", table)
            run("ip", "-n", ns, "route", "add", "default", "via", peer, "dev", dev, "table", table)
            run("ip", "-n", ns, "rule", "add", "iif", "anet-server", "fwmark", table, "lookup", table)
            if k == 0:
                run("ip", "-n", ns, "rule", "add", "iif", "lo", "fwmark", table, "lookup", table)
            edge = dict(target_node=exits[exit_id], module="tcp_connect", target=peer + ":443")
            topology["paths"][name] = dict(entry=ns, exit=exits[exit_id], transport=f"path{k % 2}",
                external_ip=public[exit_id], probe=dict(source_node=ns, **edge))
            links.append(dict(source_node=ns, edge=edge, desired=7, applied=7, available=True,
                              mbps=40 - k, latency_ms=10 + k, loss=0))
            paths[name] = dict(interface=dev, table=int(table), gateway=peer)
            counter = f"e{i}p{k}"
            run(*inside(exits[exit_id], "nft", "-f", "-"), data=(
                f"add counter ip measured {counter}\n"
                f'add rule ip measured traffic iifname "in{i}p{k}" ip daddr {SINK} '
                f'meta l4proto {{ tcp, udp }} th dport 5400 counter name {counter}\n'))
        topology["profiles"][ns] = dict(entry=ns, paths=list(paths), strategy="prefer-route")
        bindings[ns] = dict(profile=ns, user=ns, source=f"10.77.{i}.0/30", paths=paths,
                            dns_listen=listener, dns_path=f"{ns}-path0", dns_upstream=SINK)
        run(*inside(ns, "nft", "add", "table", "ip", "anet_flow"))

    previous = {}
    def apply():
        now = time.time()
        for item in links:
            item["last_seen"] = now
        metrics = observed_metrics(topology, dict(monitoring="configured", links=links), now)
        for ns in routers:
            controller["load_dns_answers"](bindings[ns])
            batch, previous[ns] = controller["render"](topology, bindings[ns], metrics, now, previous.get(ns, {}))
            run(*inside(ns, "nft", "-f", "-"), data="delete table ip anet_flow\n" + batch)

    def received():
        counts = {}
        for ns in exits:
            data = run(*inside(ns, "nft", "-j", "list", "counters", "ip"))
            for row in json.loads(data)["nftables"]:
                if "counter" in row:
                    counter = row["counter"]
                    counts[counter["name"]] = counter["packets"]
        return counts

    count = 0
    def packets(expected, udp_port):
        nonlocal count
        def check(i):
            name = routers[i]
            path = expected[i]
            selected = previous[name]["(default)"]["selected"]
            assert selected == (None if path is None else f"{name}-path{path}"), (name, path, selected)
            for kind, port in (("tcp", 0), ("udp", udp_port + 100 * i)):
                nonce = f"sample-{count}-{i}-{kind}"
                got = run(*inside(clients[i], sys.executable, str(HERE), "probe", kind, str(port), nonce))
                want = "drop" if path is None else public[path // 2]
                assert got == want, (name, kind, got, want)
                if want == "drop":
                    assert nonce not in log.read_text().split(), nonce
        before = received()
        with ThreadPoolExecutor(5) as pool:
            list(pool.map(check, range(5)))
        after = received()
        active = {name for name in before if after[name] > before[name]}
        assert active == {f"e{i}p{path}" for i, path in enumerate(expected) if path is not None}, active
        count += 1

    for strategy in ("strict-ip", "prefer-route", "speed", "auto"):
        previous.clear()
        for profile in topology["profiles"].values():
            profile.update(strategy=strategy, external_ip=public[0])
        validate(topology)
        for stage, lost, expected in (
            ("baseline", set(), [0] * 5),
            ("transport", {0}, [1, 0, 0, 0, 0]),
            ("exit", {4 * i + k for i in range(5) for k in (0, 1)}, [None if strategy == "strict-ip" else 2] * 5),
            ("blackout", set(range(20)), [None] * 5),
            ("recover", set(), [0] * 5),
        ):
            for index, link in enumerate(links):
                entry, uplink = divmod(index, 4)
                run("ip", "-n", exits[uplink // 2], "link", "set", f"in{entry}p{uplink}",
                    "down" if index in lost else "up")
                info = json.loads(run("ip", "-n", routers[entry], "-j", "link", "show", f"uplink{uplink}"))[0]
                link["available"] = info["operstate"] == "UP"
                assert link["available"] == (index not in lost)
            apply()
            packets(expected, 46000 + count)
            print(strategy, stage, expected, flush=True)

    # Keep an existing UDP five-tuple on the old live path after an explicit policy change.
    ns = routers[0]
    def single(port, nonce):
        return run(*inside(clients[0], sys.executable, str(HERE), "probe", "udp", str(port), nonce))
    hold = start(clients[0], sys.executable, str(HERE), "hold")
    def persistent(nonce):
        hold.stdin.write(nonce + "\n")
        hold.stdin.flush()
        return hold.stdout.readline().strip()
    assert persistent("tcp-pin-original") == public[0]
    assert single(47000, "pin-original") == public[0]
    topology["profiles"][ns]["rules"] = [dict(id="changed", allow=[ns + "-path2"])]
    apply()
    assert persistent("tcp-pin-again") == public[0]
    assert single(47000, "pin-again") == public[0]
    assert single(47001, "new-flow") == public[1]
    links[0]["available"] = False
    apply()
    assert single(47000, "pin-lost") == "drop" and "pin-lost" not in log.read_text()
    links[0]["available"] = True
    apply()

    binding = bindings[ns]
    feed = work / "answers.json"
    env = dict(os.environ, DNS_SOURCE=binding["source"], DNS_LISTEN=binding["dns_listen"],
               DNS_UPSTREAM=SINK, DNS_ANSWERS_FILE=str(feed))
    proc = start(ns, sys.executable, str(DEPLOY / "profile-dns/server.py"), env=env)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError("DNS service exited")
        if feed.exists() and ":53 " in run(*inside(ns, "ss", "-lnt")):
            break
        time.sleep(.05)
    def query(proto, name):
        return subprocess.run(inside(clients[0], "dig", "+short", "+tries=1", "+time=1", "+" + proto,
                                     "@" + binding["dns_listen"], name, "A"), text=True, capture_output=True)
    binding["dns_answers_file"] = str(feed)
    topology["profiles"][ns].update(require_domain=True, rules=[
        dict(id="by-dns", domain="notcp.profile.test", allow=[f"{ns}-path2"])])
    apply()
    assert single(47100, "before-dns") == "drop" and "before-dns" not in log.read_text()
    for proto in ("notcp", "tcp"):
        domain = f"{proto}.profile.test"
        result = query(proto, domain)
        assert result.returncode == 0 and result.stdout.strip() == SINK, (result.stdout, result.stderr)
        assert f"{public[0]} {domain}." in log.read_text()
        assert any(r["domain"] == domain and r["address"] == SINK and time.time() < r["expires"] <= time.time() + 25
                   for r in json.loads(feed.read_text()))
        apply()
        if proto == "notcp":
            assert single(47101, "domain-match") == public[1]
        else:
            assert single(47102, "ambiguous-dns") == "drop" and "ambiguous-dns" not in log.read_text()
    links[0]["available"] = False
    apply()
    result = query("notcp", "denied.profile.test")
    assert result.returncode != 0 and "denied.profile.test" not in log.read_text()
    print("TCP/UDP pinning, DNS UDP/TCP, TTL, domain-based forwarding, ambiguity and blackout passed")


if __name__ == "__main__":
    if sys.argv[1:2] == ["endpoint"]:
        endpoint(sys.argv[2])
    elif sys.argv[1:] == ["hold"]:
        with socket.create_connection((SINK, 5400), timeout=2) as connection:
            with connection.makefile("rb") as replies:
                for line in sys.stdin:
                    connection.sendall(line.encode())
                    print(replies.readline().decode().strip(), flush=True)
    elif sys.argv[1:2] == ["probe"]:
        probe(*sys.argv[2:])
    elif sys.argv[1:] == ["isolated"]:
        if os.readlink("/proc/self/ns/net") == os.environ["MATRIX_PARENT_NETNS"] or os.getgid() != 53153:
            raise RuntimeError("expected private network namespace and resolver gid")
        with tempfile.TemporaryDirectory() as work, ExitStack() as processes:
            lab(Path(work), processes)
    else:
        subprocess.run(["unshare", "--user", "--map-user=0", "--map-group=53153", "--net", "--mount",
                        sys.executable, str(HERE), "isolated"], check=True,
                       env=dict(os.environ, MATRIX_PARENT_NETNS=os.readlink("/proc/self/ns/net")))
