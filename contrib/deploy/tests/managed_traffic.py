"""Confirm a panel-issued profile reaches a receiver on the expected rootless Docker egress."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

HELPER = Path(__file__).resolve().with_name("tunnel.py")


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def docker(*args):
    return run("docker", *args).stdout.strip()


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def netns(pid, *args, check=True):
    return run("sudo", "-n", "nsenter", "-t", str(pid), "-n", *args, check=check)


def verify(args):
    if "name=rootless" not in docker("info", "--format", "{{json .SecurityOptions}}"):
        raise RuntimeError("select the rootless Docker daemon of the isolated test host")
    peer = inspect(args.exit_holder)["NetworkSettings"]["Networks"][args.exit_network]["IPAddress"]
    prefix = "flow-test-" + secrets.token_hex(5)
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
        def start(name, *flags):
            name = prefix + "-" + name
            docker("run", "--pull", "never", "--name", name, "-d", "--read-only", "--memory", "160m",
                   "--pids-limit", "96", "--cap-drop", "ALL", *flags)
            cleanup.callback(run, "docker", "rm", "-f", name, check=False)
            return name

        receiver = start("receiver", "--network", args.exit_network, "--entrypoint", "sleep",
                         args.network_image, "infinity")
        target = inspect(receiver)
        address = target["NetworkSettings"]["Networks"][args.exit_network]["IPAddress"]
        echo = subprocess.Popen(["sudo", "-n", "nsenter", "-t", str(target["State"]["Pid"]), "-n",
                                 sys.executable, str(HELPER), "echo"])
        def stop_echo():
            if echo.poll() is None:
                run("sudo", "-n", "kill", "-TERM", str(echo.pid), check=False)
            echo.wait(timeout=5)
        cleanup.callback(stop_echo)

        # Preserve the credential byte-for-byte. Only the receiver's /32 is routed inside the test namespace.
        config = Path(tmp) / ("client.toml" if args.ingress == "native" else "in.conf")
        config.write_bytes(args.config.read_bytes())
        config.chmod(0o600)
        flags = ["--network", args.client_network, "--user", "0", "--cap-add", "NET_ADMIN",
                 "-v", f"{config}:/{config.name}:ro"]
        if args.ingress == "native":
            client = start("client", *flags, "--device", "/dev/net/tun", args.client_image, "-c", "/client.toml")
        else:
            client = start("client", *flags, "--tmpfs", "/run", "--entrypoint", "sh", args.client_image, "-ec",
                           'wg-quick strip /in.conf > /run/wg.conf; ip link add anet-client type wireguard; '
                           'wg setconf anet-client /run/wg.conf; '
                           'ip addr add "$(sed -n "s/^Address = //p" /in.conf)" dev anet-client; '
                           'ip link set anet-client mtu "$(sed -n "s/^MTU = //p" /in.conf)" up; exec sleep infinity')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = inspect(client)["State"]
            if not status["Running"]:
                raise RuntimeError(f"client exited with code {status['ExitCode']}")
            pid = status["Pid"]
            route = netns(pid, "ip", "route", "replace", address + "/32", "dev", "anet-client", check=False)
            if not route.returncode:
                probe = netns(pid, sys.executable, str(HELPER), "probe", address, peer, check=False)
                if not probe.returncode:
                    print(f"{args.ingress} receiver {address}\n{probe.stdout.strip()}", flush=True)
                    return
            time.sleep(1)
        raise RuntimeError("no matching TCP and UDP replies within the readiness deadline")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingress", choices=("native", "wireguard"), default="native")
    parser.add_argument("--config", type=Path, required=True, help="profile downloaded from the panel")
    for name in ("exit-holder", "exit-network", "client-network", "network-image", "client-image"):
        parser.add_argument("--" + name, required=True)
    verify(parser.parse_args())
