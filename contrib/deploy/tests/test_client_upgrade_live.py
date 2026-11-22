"""Pass TCP and UDP over QUIC before and after replacing the server, using identical client TOML."""
import argparse
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from test_netns_matrix import inside, interface, run

HERE = Path(__file__).resolve()


def command(binary, library):
    if library:
        return [str(library / "ld-linux-x86-64.so.2"), "--library-path", str(library), str(binary)]
    return [str(binary)]


def start(stack, ns, cmd, logfile):
    log = stack.enter_context(logfile.open("wb"))
    proc = subprocess.Popen(inside(ns, *cmd), stdout=log, stderr=log)
    def stop():
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    stack.callback(stop)
    return proc


def run_pair(args, work, state, server, client, connected=None):
    cfg = state / "client.toml"
    saved = hashlib.sha256(cfg.read_bytes()).hexdigest()
    with ExitStack() as processes:
        srv = start(processes, "server", [*command(server, args.library), "--cfg", str(state/"server.toml")], work/"server.log")
        cli = start(processes, "client", [*command(client, args.library), "-c", str(cfg)], work/"client.log")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if srv.poll() is not None or cli.poll() is not None:
                raise RuntimeError(f"server {srv.poll()}, client {cli.poll()} during startup")
            route = subprocess.run(["ip", "-n", "client", "route", "replace", "192.0.2.2/32", "dev", "anet-client"],
                                   capture_output=True)
            if route.returncode == 0:
                probe = subprocess.run(inside("client", sys.executable, str(HERE.with_name("tunnel.py")),
                                              "probe", "192.0.2.2", "192.0.2.1"), capture_output=True, text=True)
                if probe.returncode == 0:
                    print(server.name, client.name, saved, probe.stdout.strip(), flush=True)
                    if connected:
                        connected()
                    break
            time.sleep(.2)
        else:
            raise RuntimeError(f"QUIC deadline expired for {server.name}, {client.name}")
        if hashlib.sha256(cfg.read_bytes()).hexdigest() != saved:
            raise RuntimeError("client profile changed during upgrade")


def prepare(work, template_path=None):
    run("mount", "--make-rprivate", "/")
    run("mount", "-t", "tmpfs", "tmpfs", "/run")
    for ns in ("server", "client", "receiver"):
        run("ip", "netns", "add", ns)
        run("ip", "-n", ns, "link", "set", "lo", "up")
    interface("client", "lan", "192.0.2.6", "server", "entry", "192.0.2.5")
    interface("server", "out", "192.0.2.1", "receiver", "target", "192.0.2.2")
    run(*inside("server", "sysctl", "-qw", "net.ipv4.ip_forward=1"))
    run(*inside("server", "nft", "-f", "-"), data=(
        'table ip test {\nchain nat { type nat hook postrouting priority srcnat; '
        'oifname "out" ip saddr 10.77.0.0/24 masquerade; }\n}\n'))
    state = work / "state"
    run(sys.executable, str(HERE.parents[1]/"scripts/init-state.py"), str(state), "192.0.2.5", "4519", "standalone")
    cfg = state / "client.toml"
    if template_path:
        template = template_path.read_text()
        replacements = {'dsn = "quic://127.0.0.1:4519"': 'dsn = "quic://192.0.2.5:4519"',
                        "manual_routing = false": "manual_routing = true"}
        for line in cfg.read_text().splitlines():
            key = line.partition(" = ")[0]
            if key in {"private_key", "server_pub_key"}:
                replacements[key + ' = ""'] = line
        for old, new in replacements.items():
            if template.count(old) != 1:
                raise ValueError("unexpected client template field: " + old.partition(" = ")[0])
            template = template.replace(old, new)
        cfg.write_text(template)
    else:
        with cfg.open("a") as file:
            file.write('\n[main]\nmanual_routing = true\ntun_name = "anet-client"\n')
    return state


def lab(args, work):
    state = prepare(work, args.template)
    with ExitStack() as processes:
        start(processes, "receiver", [sys.executable, str(HERE.with_name("tunnel.py")), "echo"], work/"echo.log")
        for client in args.client:
            for server in (args.before, args.after):
                run_pair(args, work, state, server, client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True, help="old server executable")
    parser.add_argument("--after", type=Path, required=True, help="candidate server executable")
    parser.add_argument("--client", type=Path, required=True, action="append", help="exact client executable")
    parser.add_argument("--template", type=Path, help="client.toml from the original release zip")
    parser.add_argument("--library", type=Path, help="optional x86-64 glibc runtime from the release image")
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.isolated:
        subprocess.run(["unshare", "-Urnm", sys.executable, str(HERE), *sys.argv[1:], "--isolated"], check=True,
                       env=dict(os.environ, UPGRADE_PARENT_NETNS=os.readlink("/proc/self/ns/net")))
        return
    if os.readlink("/proc/self/ns/net") == os.environ["UPGRADE_PARENT_NETNS"]:
        raise RuntimeError("expected private network namespace")
    with tempfile.TemporaryDirectory() as directory:
        lab(args, Path(directory))


if __name__ == "__main__":
    main()
