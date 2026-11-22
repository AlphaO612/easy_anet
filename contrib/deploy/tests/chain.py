"""Rootless linux/amd64 end-to-end acceptance for the Compose entry and exit."""
import argparse
from contextlib import ExitStack
import json
import os
import shutil
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

from tunnel import docker, echo, INTERFACE, PORT, probe

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent


def send_during_outage(target):
    # UDP is observable even if a broken return path would hide a leak.
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode() + b"\0")
        for _ in range(3):
            sock.sendto(b"outage-probe", (target, PORT))
            time.sleep(0.2)


def wait_for_release(path):
    print(f"Ready. Create {path} to release the test services.", flush=True)
    deadline = time.monotonic() + 1200
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(1)


def verify_traffic(container, target_ip, expected_ip, target_id, python, blocked=False):
    probe_script = (HERE / "tunnel.py").read_text()
    action = ["blocked", target_ip] if blocked else ["probe", target_ip, expected_ip, target_id]
    last_error = ""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        network = "container:" + container
        route = docker("run", "--rm", "--network", network, "--user", "0", "--cap-drop", "ALL",
                       "--cap-add", "NET_ADMIN", "--entrypoint", "ip", os.environ["NETNS_IMAGE"], "route",
                       "replace", target_ip + "/32", "dev", INTERFACE, check=False)
        if route.returncode == 0:
            reply = docker("run", "--rm", "-i", "--network", network, "--cap-drop", "ALL", "--cap-add", "NET_RAW",
                           python, "python3", "-", *action,
                           input_text=probe_script, check=False)
            if reply.returncode == 0:
                print(reply.stdout, end="")
                return
            if blocked:
                raise RuntimeError(reply.stderr or "outage check failed")
            last_error = reply.stderr
        else:
            last_error = route.stderr
        time.sleep(1)
    print(last_error.strip(), file=sys.stderr)
    print(docker("logs", "--tail", "10", container, check=False).stderr, file=sys.stderr)
    raise RuntimeError(f"{container} failed to reach {target_ip} through {expected_ip}")


def check_public_exit(container, expected, python_image):
    script = (HERE / "tunnel.py").read_text()
    network = "container:" + container
    failures = []
    for host in ("api.ipify.org", "checkip.amazonaws.com"):
        for addr in dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)):
            docker("run", "--rm", "--network", network, "--user", "0", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                   "--entrypoint", "ip", os.environ["NETNS_IMAGE"], "route", "replace", addr + "/32", "dev", INTERFACE)
            reply = docker("run", "--rm", "-i", "--network", network, "--cap-drop", "ALL", "--cap-add", "NET_RAW",
                           python_image, "python3", "-", "public-exit", host, addr, expected, input_text=script, check=False)
            if reply.returncode == 0:
                print(reply.stdout, end="")
                return
            failures.append(reply.stderr.strip().splitlines()[-1])
    raise RuntimeError(f"public IPv4 verification failed: {failures}")


def remote_client(bundle, python_image, blocked=False, public_ip=None):
    """Exercise a remote exit without creating or publishing a local service."""
    def defaults():
        return [subprocess.check_output(["ip", family, "route", "show", "default"], text=True)
                for family in ("-4", "-6")]

    before = defaults()
    name = "anet-home-qa-" + secrets.token_hex(4)
    peer = json.loads((bundle / "peer.json").read_text())
    try:
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            cfg = Path(tmp) / "client.toml"
            cfg.write_text((bundle / "client.toml").read_text() +
                           '\n[main]\nmanual_routing = true\ntun_name = "anet-client"\n')
            # Rootful and rootless Docker can both read this individual mount.
            # The temporary parent directory is accessible only to its owner.
            cfg.chmod(0o444)
            cleanup.callback(docker, "rm", "-f", name, check=False)
            docker("run", "-d", "--name", name, "--network", "bridge", "--read-only",
                   "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                   "--user", "0", "--device", "/dev/net/tun", "--pids-limit", "96", "--memory", "192m",
                   "--cpus", "0.5", "-v", f"{cfg}:/client.toml:ro", os.environ["CLIENT_IMAGE"], "-c", "/client.toml")
            verify_traffic(name, peer["target"], peer["exit"], peer["id"], python_image, blocked)
            if public_ip:
                check_public_exit(name, public_ip, python_image)
            if blocked and peer.get("entry_target") != peer["target"] and "entry_target" in peer:
                verify_traffic(name, peer["entry_target"], None, None, python_image, True)
    finally:
        if defaults() != before:
            raise RuntimeError("home host default route changed during the test")
        print("Host IPv4 and IPv6 default routes matched before and after the test.", flush=True)


def run(args):
    if "name=rootless" not in docker("info", "--format", "{{json .SecurityOptions}}").stdout:
        raise RuntimeError("select a rootless Docker daemon")
    prefix = "chain-" + secrets.token_hex(4)
    python = os.environ.get("PYTHON_IMAGE", "python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e")
    mount = f"{HERE}:/qa:ro"
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
        root = Path(tmp)
        docker("network", "create", prefix)
        cleanup.callback(docker, "network", "rm", prefix, check=False)

        def node(role, pool, is_chain=False, public=False):
            name = prefix + "-" + role
            state = root / role
            host = args.exit_host if public else name
            public_port = args.port if public else 0
            if args.command == "serve-entry":
                host = args.entry_host
                public_port = args.chain_port if is_chain else args.direct_port
            subprocess.run([sys.executable, str(DEPLOY / "scripts/init-state.py"), str(state),
                            host, str(public_port or 4519),
                            "standalone", "--subnet", pool + ".0/24"], check=True)
            overlay = root / (role + ".yaml")
            network = {"networks": {"vpn": {"aliases": [name]}}}
            public_udp = args.command == "serve-entry" or (public and args.transport == "quic")
            if public and args.transport == "ws":
                server_cfg = state / "server.toml"
                server_cfg.chmod(0o600)
                server_cfg.write_text(server_cfg.read_text().replace('websocket_bind_to = ""',
                                                                     'websocket_bind_to = "0.0.0.0:4520"'))
                server_cfg.chmod(0o444)
                client_cfg = state / "client.toml"
                client_cfg.write_text(client_cfg.read_text().replace(
                    f"quic://{args.exit_host}:{args.port}", f"ws://{args.exit_host}:{args.port}/socket"))
                network["ports"] = [f"{args.exit_host}:{args.port}:4520/tcp"]
            overlay.write_text(json.dumps({"networks": {"vpn": {"name": prefix, "external": True}},
                                           "services": {"network": network}}))
            env = dict(os.environ, STATE_DIR=str(state), TUN_ADDRESS=pool + ".2/24", TUN_SUBNET=pool + ".0/24",
                       VPN_BIND=host if public_udp else "127.0.0.1", VPN_PORT=str(public_port) if public_udp else "0",
                       UPLINK_CONFIG=str(root / "uplink.toml"))
            cmd = ["docker", "compose", "-p", name, "-f", str(DEPLOY / "compose/compose.yaml")]
            if is_chain:
                cmd += ["-f", str(DEPLOY / "compose/chain.yaml")]
            cmd += ["-f", str(overlay)]

            def compose(*args, check=True):
                return subprocess.run([*cmd, *args], env=env, stdout=subprocess.PIPE, text=True, check=check)
            cleanup.callback(compose, "down", "--timeout", "3", check=False)
            compose("up", "-d")
            holder = compose("ps", "-q", "network").stdout.strip()
            return state / "client.toml", compose, address(holder)

        def address(container):
            settings = json.loads(docker("inspect", container).stdout)[0]["NetworkSettings"]
            return settings["Networks"][prefix]["IPAddress"]

        def start(role, *args):
            name = prefix + "-" + role
            cleanup.callback(docker, "rm", "-f", name, check=False)
            docker("run", "-d", "--name", name, "--network", prefix, *args)
            return name

        def client(cfg, role):
            with cfg.open("a") as file:
                file.write('\n[main]\nmanual_routing = true\ntun_name = "anet-client"\n')
            return start(role, "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--device", "/dev/net/tun",
                         "--user", "0", "-v", f"{cfg}:/client.toml:ro", os.environ["CLIENT_IMAGE"], "-c", "/client.toml")

        local_id = secrets.token_hex(16)
        target = start("target", "--cap-drop", "ALL", "-e", f"ECHO_ID={local_id}", "-v", mount, python, "python3", "/qa/chain.py", "echo")
        target_ip = address(target)
        if args.command == "serve-exit":
            bundle = args.bundle
            bundle.mkdir(mode=0o700)
            profile, _, exit_ip = node("exit", "10.78.0", public=True)
            shutil.copyfile(profile, bundle / "client.toml")
            (bundle / "peer.json").write_text(json.dumps({"target": target_ip, "exit": exit_ip, "id": local_id}))
            wait_for_release(bundle / "stop")
            return
        if args.remote_exit:
            exit_profile = args.remote_exit / "client.toml"
            peer = json.loads((args.remote_exit / "peer.json").read_text())
            exit_ip, exit_target, exit_id = peer["exit"], peer["target"], peer["id"]
        else:
            exit_profile, _, exit_ip = node("exit", "10.78.0")
            exit_target, exit_id = target_ip, local_id
        (root / "uplink.toml").write_text(exit_profile.read_text() +
            '\n[main]\nmanual_routing = true\ntun_name = "anet-uplink"\n')
        direct_profile, _, direct_ip = node("direct", "10.77.0")
        entry_profile, chain, _ = node("entry", "10.77.0", True)
        if args.command == "serve-entry":
            bundle = args.bundle
            bundle.mkdir(mode=0o700)
            for role, profile, destination in (
                ("direct", direct_profile, {"target": target_ip, "exit": direct_ip, "id": local_id}),
                ("chain", entry_profile, {"target": exit_target, "exit": exit_ip, "id": exit_id, "entry_target": target_ip}),
            ):
                folder = bundle / role
                folder.mkdir(mode=0o700)
                shutil.copyfile(profile, folder / "client.toml")
                (folder / "peer.json").write_text(json.dumps(destination))
            (bundle / "services.json").write_text(json.dumps({"uplink": chain("ps", "-q", "uplink").stdout.strip(),
                                                              "target": target}))
            wait_for_release(bundle / "stop")
            if "outage-probe" in docker("logs", target).stdout:
                raise RuntimeError("entry echo received a chained packet during the uplink outage")
            return
        direct = client(direct_profile, "direct-client")
        chained = client(entry_profile, "chain-client")
        verify_traffic(direct, target_ip, direct_ip, local_id, python)
        verify_traffic(chained, exit_target, exit_ip, exit_id, python)
        chain("stop", "uplink")
        docker("run", "--rm", "--network", "container:" + chained, "--cap-drop", "ALL", "--cap-add", "NET_RAW",
               "-v", mount, python, "python3", "/qa/chain.py", "outage", target_ip)
        verify_traffic(direct, target_ip, direct_ip, local_id, python)
        chain("start", "uplink")
        verify_traffic(chained, exit_target, exit_ip, exit_id, python)
        if "outage-probe" in docker("logs", target).stdout:
            raise RuntimeError("entry leaked UDP traffic during the uplink outage")
        print(f"direct and chained TCP/UDP paths and uplink outage passed ({prefix}, {root})", flush=True)
        if args.hold_until:
            print(f"target={exit_target} exit={exit_ip}", flush=True)
            wait_for_release(args.hold_until)


if __name__ == "__main__":
    if sys.argv[1:2] == ["echo"]:
        echo()
    elif sys.argv[1:2] == ["probe"]:
        probe(*sys.argv[2:])
    elif sys.argv[1:2] == ["outage"]:
        send_during_outage(sys.argv[2])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command")
        standalone = commands.add_parser("accept")
        standalone.add_argument("--remote-exit", type=Path, help="bundle copied privately from serve-exit")
        standalone.add_argument("--hold-until", type=Path, help="keep entry available for companion probes")
        exit_server = commands.add_parser("serve-exit")
        exit_server.add_argument("bundle", type=Path, help="new private directory for transfer to entry")
        exit_server.add_argument("exit_host", help="test host's assigned IPv4 address")
        exit_server.add_argument("port", type=int, help="unused port for authenticated ANet")
        exit_server.add_argument("--transport", choices=["quic", "ws"], default="quic")
        entry = commands.add_parser("serve-entry", help="publish two test profiles for an external client")
        entry.add_argument("bundle", type=Path)
        entry.add_argument("remote_exit", type=Path)
        entry.add_argument("entry_host", help="assigned IPv4 address on the entry host")
        entry.add_argument("direct_port", type=int)
        entry.add_argument("chain_port", type=int)
        client = commands.add_parser("remote-client", help="test a remote exit from an isolated client namespace")
        client.add_argument("bundle", type=Path)
        client.add_argument("--python-image", required=True, help="local Python image used for TCP and UDP probes")
        client.add_argument("--public-exit", help="expected IPv4 as observed by a public HTTPS echo")
        client.add_argument("--blocked", action="store_true", help="require an authenticated TUN and no echo traffic")
        args = parser.parse_args(sys.argv[1:] or ["accept"])
        if args.command == "remote-client":
            remote_client(args.bundle, args.python_image, args.blocked, args.public_exit)
        else:
            run(args)
