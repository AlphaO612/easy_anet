"""Install a supervised outgoing flow agent for the rootless Docker user."""
import argparse
import hashlib
import os
import re
from pathlib import Path
import subprocess
import sys

from topology import atomic_write, read


def quote(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("control character in unit argument")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def install(settings, check):
    settings = settings.resolve(strict=True)
    data = read(settings)
    for field in ("node_config", "signing_public", "state"):
        if not Path(data[field]).is_absolute():
            raise ValueError(f"{field} requires an absolute path")
    if not all(k in data["images"] for k in ("CLIENT_IMAGE", "SERVER_IMAGE", "NETNS_IMAGE")):
        raise ValueError("provide client, server and network images")
    for key, image in data["images"].items():
        if key not in {"CLIENT_IMAGE", "SERVER_IMAGE", "NETNS_IMAGE", "INGRESS_IMAGE", "DNS_IMAGE"}:
            raise ValueError("unknown image role")
        if not re.fullmatch(r"(?:[^\s@]+@)?sha256:[a-f0-9]{64}", image):
            raise ValueError(f"{key} must use an immutable image ID or digest")
    if check:
        return
    ident = hashlib.sha256(os.fsencode(Path(data["state"]).resolve())).hexdigest()[:16]
    name = "anet-flow-" + ident + ".service"
    directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    unit = f"""[Unit]
Description=ANet managed entry profiles
# Settings {hashlib.sha256(settings.read_bytes()).hexdigest()}
After=docker.service

[Service]
ExecStart={quote(sys.executable)} {quote(Path(__file__).resolve().with_name("flow-agent.py"))} {quote(settings)}
Environment=DOCKER_HOST=unix://%t/docker.sock
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""
    dest = directory / name
    changed = not dest.exists() or dest.read_text() != unit
    if changed:
        atomic_write(dest, unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", name], check=True)
    subprocess.run(["systemctl", "--user", "restart" if changed else "start", name], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    install(args.settings, args.check)
