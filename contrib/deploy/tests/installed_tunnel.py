"""Probe an installed standalone/webui server with its issued QUIC client profile."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from tunnel import docker, INTERFACE


def acceptance(project, config, endpoint):
    network = project + "_vpn"
    holder, = docker("ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                     "--filter", "label=com.docker.compose.service=network").stdout.split()

    def address(container):
        details, = json.loads(docker("inspect", container).stdout)
        return details["NetworkSettings"]["Networks"][network]["IPAddress"]

    server_ip = address(holder)
    # Exercise the issued keys on the project's private bridge, including on a loopback-only VPS.
    # Only the lab destination and the route to the private echo service change.
    source = config.read_text()
    if not re.search(r'(?m)^dsn\s*=\s*"' + re.escape(endpoint) + r'"\s*$', source):
        raise ValueError("expected QUIC DSN missing from the issued profile")
    source = source.replace(f'"{endpoint}"', f'"quic://{server_ip}:4519"')
    if "[main]" in source:
        source, count = re.subn(r"(?m)^manual_routing\s*=\s*(?:false|true)\b", "manual_routing = true", source)
        if count != 1:
            raise ValueError("expected one manual_routing setting in issued [main] profile")
    else:
        source += f'\n[main]\nmanual_routing=true\ntun_name="{INTERFACE}"\n'

    image = os.environ.get("PYTHON_IMAGE", "python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e")
    client_image = os.environ["CLIENT_IMAGE"]
    netns = os.environ["NETNS_IMAGE"]
    mount = str(Path(__file__).resolve().with_name("tunnel.py")) + ":/qa/tunnel.py:ro"
    with contextlib.ExitStack() as cleanup:
        directory = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        profile = directory / "client.toml"
        profile.write_text(source)
        profile.chmod(0o444)
        prefix = "anet-traffic-" + secrets.token_hex(5)

        def start(role, *args):
            name = prefix + "-" + role
            cleanup.callback(docker, "rm", "-f", name, check=False)
            docker("run", "-d", "--name", name, "--network", network, "--pull", "never",
                   "--read-only", "--pids-limit", "64", "--cpus", "0.5", "--memory", "128m", *args)
            return name

        target = start("echo", "--cap-drop", "ALL", "-v", mount, image, "python3", "/qa/tunnel.py", "echo")
        client = start("client", "--device", "/dev/net/tun", "--user", "0", "--cap-drop", "ALL",
                       "--cap-add", "NET_ADMIN", "-v", f"{profile}:/client.toml:ro", client_image,
                       "-c", "/client.toml")
        target_ip = address(target)
        runner = ["run", "--rm", "--pull", "never", "--network", "container:" + client,
                  "--read-only", "--cap-drop", "ALL", "--user", "0"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            route = docker(*runner, "--cap-add", "NET_ADMIN", "--entrypoint", "ip", netns,
                           "route", "replace", target_ip + "/32", "dev", INTERFACE, check=False)
            if not route.returncode:
                result = docker(*runner, "--cap-add", "NET_RAW", "-v", mount, image,
                                "python3", "/qa/tunnel.py", "probe", target_ip, server_ip, check=False)
                if not result.returncode:
                    print(result.stdout, end="")
                    return
            time.sleep(1)
        raise RuntimeError("installed server did not return the TCP/UDP nonce with its expected exit address")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Compose project of the disposable test installation")
    parser.add_argument("client_config", type=Path, help="issued client.toml")
    parser.add_argument("endpoint", help="literal QUIC DSN in the issued profile")
    args = parser.parse_args()
    acceptance(args.project, args.client_config, args.endpoint)
