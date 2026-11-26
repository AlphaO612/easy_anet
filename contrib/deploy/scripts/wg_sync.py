"""Live-sync WireGuard multi-peer (/24) ingress peers and report per-peer transfer totals."""
import base64
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from topology import NoRedirect, atomic_write

WG_INTERFACE = "wg-profile"
WG_SUBNET = ipaddress.IPv4Network("10.79.0.0/24")
WG_SERVER_IP = ipaddress.IPv4Address("10.79.0.1")
WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def _flock(lock):
    if fcntl is not None:
        fcntl.flock(lock, fcntl.LOCK_EX)


def _validate_endpoint(endpoint):
    if not endpoint:
        return
    site = urlsplit(endpoint)
    if (not (site.scheme == "https" and site.hostname or site.scheme == "http" and site.hostname == "127.0.0.1")
            or site.username or site.password or site.query or site.fragment):
        raise ValueError("Для синхронизации WireGuard укажите HTTPS-адрес панели либо локальный порт на первой VPS.")


def _find_ingress_container(project):
    ps = ["docker", "ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project,
          "--filter", "label=com.docker.compose.service=ingress"]
    cid, = subprocess.check_output(ps, text=True, timeout=6).split()
    return cid


def validate_wg_key(key):
    if not isinstance(key, str) or not WG_KEY_RE.fullmatch(key):
        raise ValueError("invalid WireGuard public key")
    raw = base64.b64decode(key, validate=True)
    if len(raw) != 32:
        raise ValueError("invalid WireGuard key length")
    return key


def validate_allowed_ip(allowed_ips):
    if not isinstance(allowed_ips, str):
        raise ValueError("invalid WireGuard AllowedIPs")
    net = ipaddress.IPv4Network(allowed_ips.strip(), strict=True)
    if net.prefixlen != 32 or net.network_address not in WG_SUBNET or net.network_address in (
            WG_SERVER_IP, WG_SUBNET.network_address, WG_SUBNET.broadcast_address):
        raise ValueError(f"WireGuard peer IP {allowed_ips} must be a host /32 inside {WG_SUBNET}")
    return str(net)


def parse_transfer(output):
    counters = {}
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) != 3:
            continue
        pubkey, rx_raw, tx_raw = parts
        if not WG_KEY_RE.fullmatch(pubkey):
            continue
        rx, tx = int(rx_raw), int(tx_raw)
        if rx < 0 or tx < 0:
            raise ValueError("invalid WireGuard transfer counter")
        counters[pubkey] = [rx, tx]
    return counters


def sample(project):
    cid = _find_ingress_container(project)
    inspect = ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid]
    started = subprocess.check_output(inspect, text=True, timeout=6).strip()
    output = subprocess.check_output(
        ["docker", "exec", cid, "wg", "show", WG_INTERFACE, "transfer"],
        text=True, timeout=10,
    )
    if started != subprocess.check_output(inspect, text=True, timeout=6).strip():
        raise ValueError("WireGuard ingress restarted during sample")
    return cid + "/" + started, parse_transfer(output)


def accumulate(saved, generation, counters):
    previous = saved.get("last", {}) if saved.get("generation") == generation else {}
    totals = saved.setdefault("totals", {})
    for pubkey, pair in counters.items():
        last = previous.get(pubkey, [0, 0])
        current = totals.setdefault(pubkey, [0, 0])
        for i, value in enumerate(pair):
            if value < 0:
                raise ValueError("invalid WireGuard transfer counter")
            current[i] += value - last[i] if value >= last[i] else value
    saved["generation"] = generation
    saved["last"] = {**previous, **counters}


def parse_wg_conf(text):
    interface_lines = []
    peers = []
    section = None
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().lower()
            if name == "interface":
                section = "interface"
                current = None
            elif name == "peer":
                section = "peer"
                current = {}
                peers.append(current)
            else:
                section = None
                current = None
            continue
        if section == "interface":
            interface_lines.append(line)
        elif section == "peer" and current is not None and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            current[key.lower()] = value
    parsed_peers = {}
    for row in peers:
        pub = row.get("publickey")
        ips = row.get("allowedips")
        if pub and ips:
            parsed_peers[pub] = {"public_key": pub, "allowed_ips": ips}
    return interface_lines, parsed_peers


def render_wg_conf(interface_lines, peers):
    blocks = ["[Interface]\n" + "\n".join(interface_lines) + "\n"]
    for pub in sorted(peers):
        peer = peers[pub]
        blocks.append(f"[Peer]\nPublicKey = {peer['public_key']}\nAllowedIPs = {peer['allowed_ips']}\n")
    return "\n".join(blocks)


def extract_server_public_key(wg_conf_path, client_conf_path=None):
    wg_path = Path(wg_conf_path)
    if wg_path.is_file():
        interface_lines, _ = parse_wg_conf(wg_path.read_text())
        for line in interface_lines:
            if "=" in line:
                k, v = (part.strip() for part in line.split("=", 1))
                if k.lower() == "privatekey" and WG_KEY_RE.fullmatch(v):
                    try:
                        from cryptography.hazmat.primitives import serialization
                        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
                        priv = X25519PrivateKey.from_private_bytes(base64.b64decode(v, validate=True))
                        pub = priv.public_key().public_bytes(
                            serialization.Encoding.Raw, serialization.PublicFormat.Raw
                        )
                        return base64.b64encode(pub).decode("ascii")
                    except ImportError:
                        break
    client_path = Path(client_conf_path) if client_conf_path else wg_path.parent.parent / "client.conf"
    if client_path.is_file():
        _, peers = parse_wg_conf(client_path.read_text())
        if peers:
            return next(iter(peers))
    return ""


def extract_starter_client_public_key(wg_conf_path):
    wg_path = Path(wg_conf_path)
    if not wg_path.is_file():
        return ""
    _, peers = parse_wg_conf(wg_path.read_text())
    for pub, peer in peers.items():
        if peer["allowed_ips"].strip() == "10.79.0.2/32":
            return pub
    return ""


def sync_wg_peers(project, wg_conf_path, desired_peers, *, starter_public_key=""):
    """Live-apply WireGuard peer additions/updates/removals without dropping active tunnels."""
    wg_path = Path(wg_conf_path)
    interface_lines, existing = parse_wg_conf(wg_path.read_text())

    desired = {}
    used_ips = set()
    for row in desired_peers:
        if not isinstance(row, dict):
            raise ValueError("invalid WireGuard peer entry")
        pub = validate_wg_key(row.get("public_key", ""))
        ip = validate_allowed_ip(row.get("allowed_ips", ""))
        if ip in used_ips:
            raise ValueError(f"duplicate WireGuard AllowedIPs {ip}")
        used_ips.add(ip)
        desired[pub] = {"public_key": pub, "allowed_ips": ip}

    if starter_public_key and starter_public_key in existing and starter_public_key not in desired:
        starter_ip = existing[starter_public_key]["allowed_ips"]
        if starter_ip not in used_ips:
            desired[starter_public_key] = existing[starter_public_key]

    to_remove = [pub for pub in existing if pub not in desired]
    to_add = [peer for pub, peer in desired.items() if pub not in existing]
    to_update = [peer for pub, peer in desired.items()
                 if pub in existing and existing[pub]["allowed_ips"] != peer["allowed_ips"]]

    if not to_remove and not to_add and not to_update:
        return {"added": [], "updated": [], "removed": []}

    cid = _find_ingress_container(project)
    for pub in to_remove:
        subprocess.run(
            ["docker", "exec", cid, "wg", "set", WG_INTERFACE, "peer", pub, "remove"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    for peer in (*to_add, *to_update):
        subprocess.run(
            ["docker", "exec", cid, "wg", "set", WG_INTERFACE, "peer", peer["public_key"],
             "allowed-ips", peer["allowed_ips"]],
            check=True, capture_output=True, text=True, timeout=10,
        )

    atomic_write(wg_path, render_wg_conf(interface_lines, desired))
    return {
        "added": [p["public_key"] for p in to_add],
        "updated": [p["public_key"] for p in to_update],
        "removed": to_remove,
    }


def collect(settings):
    with (settings.parent / "wg-sync.lock").open("a") as lock:
        _flock(lock)
        config = json.loads(settings.read_text())
        state = settings.parent / "wg-sync-state.json"
        saved = json.loads(state.read_text()) if state.exists() else {}
        if not saved.get("source"):
            saved["source"] = secrets.token_hex(16)
        generation, counters = sample(config["project"])
        accumulate(saved, generation, counters)
        atomic_write(state, json.dumps(saved))
        if config.get("sync_endpoint"):
            message = {
                "protocol": "wireguard",
                "source": saved["source"],
                "wg_server_public_key": config.get("wg_server_public_key", ""),
                "totals": saved.get("totals", {}),
            }
            request = Request(config["sync_endpoint"], data=json.dumps(message).encode(), headers={
                "Content-Type": "application/json", "X-Usage-Token": config["token"]})
            with build_opener(NoRedirect).open(request, timeout=8) as response:
                raw = response.read(256 * 1024)
            if raw:
                reply = json.loads(raw)
                desired = reply.get("active_wg_peers") if isinstance(reply, dict) else None
                wg_conf = config.get("wg_conf") or str(settings.parent / "adapter/server/wg.conf")
                if isinstance(desired, list) and Path(wg_conf).is_file():
                    sync_wg_peers(config["project"], wg_conf, desired,
                                  starter_public_key=config.get("starter_public_key", ""))


def systemd_arg(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("Недопустимый путь для systemd.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def install(directory, project, sync_endpoint, wg_server_public_key, *, starter_public_key="", wg_conf=None):
    _validate_endpoint(sync_endpoint)
    auth = directory / "wg-sync-token"
    if not auth.exists():
        atomic_write(auth, secrets.token_urlsafe(32))
    token = auth.read_text()
    settings = directory / "wg-sync-settings.json"
    payload = dict(
        project=project,
        token=token,
        sync_endpoint=sync_endpoint,
        wg_server_public_key=wg_server_public_key,
        starter_public_key=starter_public_key,
        wg_conf=str(wg_conf or (directory / "adapter/server/wg.conf")),
    )
    atomic_write(settings, json.dumps(payload))
    units = Path.home() / ".config/systemd/user"
    units.mkdir(mode=0o700, exist_ok=True, parents=True)
    name = project + "-wg-sync"
    service = f"""[Unit]
Description=WireGuard peer sync and counters for {project}
After=docker.service
[Service]
Type=oneshot
UMask=0077
NoNewPrivileges=yes
Environment=DOCKER_HOST=unix://%t/docker.sock
ExecStart=/usr/bin/python3 {systemd_arg(Path(__file__).resolve())} {systemd_arg(settings)}
"""
    timer = f"""[Unit]
Description=Sync WireGuard peers for {project}
[Timer]
OnStartupSec=10s
OnUnitInactiveSec=30s
AccuracySec=2s
[Install]
WantedBy=timers.target
"""
    atomic_write(units / (name + ".service"), service)
    atomic_write(units / (name + ".timer"), timer)
    for args in (["daemon-reload"], ["enable", "--now", name + ".timer"]):
        subprocess.run(["systemctl", "--user", *args], check=True, stdout=sys.stderr)
    return {"token_hash": hashlib.sha256(token.encode()).hexdigest(), "wg_server_public_key": wg_server_public_key}


if __name__ == "__main__":
    os.umask(0o077)
    collect(Path(sys.argv[1]))
