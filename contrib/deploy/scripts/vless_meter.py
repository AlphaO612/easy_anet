"""Sample non-resetting Xray counters, send cumulative totals, and live-sync VLESS clients."""
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
import uuid

from topology import NoRedirect, atomic_write

SOCKET = "/anet-usage/stats.sock"
INBOUND_TAG = "vless-in"
STARTER = "anet-starter@anet.invalid"
COUNTER = re.compile(r"user>>>(anet-device-([0-9]+)@anet\.invalid|anet-starter@anet\.invalid)>>>traffic>>>(uplink|downlink)")
USER_COUNTER = re.compile(r"user>>>([^>]+)>>>traffic>>>(uplink|downlink)")


def _flock(lock):
    if fcntl is not None:
        fcntl.flock(lock, fcntl.LOCK_EX)


def _validate_endpoint(endpoint):
    if not endpoint:
        return
    site = urlsplit(endpoint)
    if (not (site.scheme == "https" and site.hostname or site.scheme == "http" and site.hostname == "127.0.0.1")
            or site.username or site.password or site.query or site.fragment):
        raise ValueError("Для счётчиков укажите HTTPS-адрес панели либо локальный порт на первой VPS.")


def _find_ingress_container(project):
    ps = ["docker", "ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project,
          "--filter", "label=com.docker.compose.service=ingress"]
    cid, = subprocess.check_output(ps, text=True, timeout=6).split()
    return cid


def configure(config, clients):
    if not clients[0].get("email"):
        clients[0]["email"] = STARTER
    config["stats"] = {}
    stats = config.setdefault("policy", {}).setdefault("levels", {}).setdefault("0", {})
    stats.update(statsUserUplink=True, statsUserDownlink=True)
    config["api"] = {"tag": "anet-stats", "services": ["StatsService", "HandlerService"]}
    inbounds = config["inbounds"]
    for row in inbounds:
        if row.get("protocol") == "vless" and not row.get("tag"):
            row["tag"] = INBOUND_TAG
    if not any(row.get("tag") == "anet-stats-in" for row in inbounds):
        inbounds.append({"tag": "anet-stats-in", "protocol": "dokodemo-door", "listen": SOCKET,
                         "settings": {"network": "unix", "address": "127.0.0.1", "port": 1}})
    rules = config.setdefault("routing", {}).setdefault("rules", [])
    if not any(rule.get("outboundTag") == "anet-stats" for rule in rules):
        rules.insert(0, {"type": "field", "inboundTag": ["anet-stats-in"], "outboundTag": "anet-stats"})


def catalog_id(starter, devices):
    return hashlib.sha256("\0".join([starter, *devices]).encode()).hexdigest()


def _query_stats(project):
    cid = _find_ingress_container(project)
    inspect = ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid]
    started = subprocess.check_output(inspect, text=True, timeout=6).strip()
    command = ["docker", "exec", "-u", "17777:17777", cid, "xray", "api", "statsquery",
               "--server=unix://" + SOCKET, "-pattern", "user>>>"]
    stats = json.loads(subprocess.check_output(command, timeout=10))
    if started != subprocess.check_output(inspect, text=True, timeout=6).strip():
        raise ValueError("Xray restarted during sample")
    return cid + "/" + started, stats.get("stat", [])


def sample_all(project):
    generation, rows = _query_stats(project)
    slot_counters = {}
    user_counters = {}
    for stat in rows:
        name = stat.get("name", "")
        value = int(stat.get("value", 0))
        match = COUNTER.fullmatch(name)
        if match:
            slot = match[2] if match[2] is not None else "starter"
            if slot == "starter" or int(slot) < 128:
                pair = slot_counters.setdefault(slot, [0, 0])
                pair[0 if match[3] == "uplink" else 1] = value
        user_match = USER_COUNTER.fullmatch(name)
        if user_match:
            email, direction = user_match[1], user_match[2]
            pair = user_counters.setdefault(email, [0, 0])
            pair[0 if direction == "uplink" else 1] = value
    return generation, slot_counters, user_counters


def sample(project):
    generation, slot_counters, _ = sample_all(project)
    return generation, slot_counters


def accumulate(saved, generation, counters, *, totals_key="totals", last_key="last", gen_key="generation"):
    previous = saved.get(last_key, {}) if saved.get(gen_key) == generation else {}
    totals = saved.setdefault(totals_key, {})
    for slot, pair in counters.items():
        last = previous.get(slot, [0, 0])
        current = totals.setdefault(slot, [0, 0])
        for i, value in enumerate(pair):
            if value < 0:
                raise ValueError("invalid Xray counter")
            current[i] += value - last[i] if value >= last[i] else value
    saved[gen_key] = generation
    saved[last_key] = {**previous, **counters}


def _normalize_desired_clients(desired_clients):
    normalized = {}
    for entry in desired_clients:
        if not isinstance(entry, dict):
            raise ValueError("invalid VLESS client entry")
        raw_uuid = entry.get("uuid") or entry.get("id")
        if not isinstance(raw_uuid, str):
            raise ValueError("missing VLESS client uuid")
        uid = str(uuid.UUID(raw_uuid))
        email = entry.get("email") or uid
        if not isinstance(email, str) or not email.strip() or len(email) > 128 or any(ord(c) < 32 for c in email):
            raise ValueError("invalid VLESS client email")
        normalized[uid] = {"id": uid, "email": email.strip(), "level": 0}
    return normalized


def sync_xray_clients(project, server_json_path, desired_clients, *, preserve_starter=True):
    """Live-sync Xray VLESS clients via HandlerService (adu/rmu) and persist atomically."""
    server_path = Path(server_json_path)
    config = json.loads(server_path.read_text())
    inbound, = [item for item in config["inbounds"] if item.get("protocol") == "vless"]
    tag = inbound.setdefault("tag", INBOUND_TAG)
    clients = inbound.setdefault("settings", {}).setdefault("clients", [])
    desired = _normalize_desired_clients(desired_clients)

    starter_clients = [c for c in clients if preserve_starter and c.get("email") == STARTER and c.get("id") not in desired]
    starter_ids = {c["id"] for c in starter_clients}
    existing = {c["id"]: c for c in clients if c.get("id") not in starter_ids}

    to_remove = []
    to_add = []
    for uid, current in existing.items():
        target = desired.get(uid)
        if target is None:
            to_remove.append(current)
        elif current.get("email") != target["email"]:
            to_remove.append(current)
            to_add.append(target)
    for uid, target in desired.items():
        if uid not in existing:
            to_add.append(target)

    if not to_add and not to_remove:
        return {"added": [], "removed": []}

    cid = _find_ingress_container(project)
    remove_emails = [c["email"] for c in to_remove if c.get("email")]
    if remove_emails:
        subprocess.run(
            ["docker", "exec", "-u", "17777:17777", cid, "xray", "api", "rmu",
             "--server=unix://" + SOCKET, "-tag", tag, *remove_emails],
            check=True, capture_output=True, text=True, timeout=10,
        )
    if to_add:
        payload = {"inbounds": [{"tag": tag, "protocol": "vless", "settings": {"clients": to_add}}]}
        subprocess.run(
            ["docker", "exec", "-i", "-u", "17777:17777", cid, "xray", "api", "adu",
             "--server=unix://" + SOCKET, "/dev/stdin"],
            input=json.dumps(payload), check=True, capture_output=True, text=True, timeout=10,
        )

    inbound["settings"]["clients"] = starter_clients + [desired[uid] for uid in sorted(desired)]
    configure(config, inbound["settings"]["clients"] or [{"id": "", "email": STARTER}])
    atomic_write(server_path, json.dumps(config, indent=2) + "\n")
    return {"added": [c["id"] for c in to_add], "removed": [c["id"] for c in to_remove]}


def collect(settings):
    with (settings.parent / "meter.lock").open("a") as lock:
        _flock(lock)
        config = json.loads(settings.read_text())
        state = settings.parent / "meter-state.json"
        saved = json.loads(state.read_text()) if state.exists() else {}
        if saved.get("catalog") != config["catalog"]:
            saved = {"catalog": config["catalog"], "source": secrets.token_hex(16)}
        generation, counters, user_counters = sample_all(config["project"])
        accumulate(saved, generation, counters)
        accumulate(saved, generation, user_counters, totals_key="user_totals", last_key="user_last", gen_key="user_generation")
        atomic_write(state, json.dumps(saved))
        if config.get("endpoint"):
            message = {k: saved[k] for k in ("source", "catalog", "totals")}
            request = Request(config["endpoint"], data=json.dumps(message).encode(), headers={
                "Content-Type": "application/json", "X-Usage-Token": config["token"]})
            with build_opener(NoRedirect).open(request, timeout=8):
                pass
        if config.get("sync_endpoint"):
            sync_message = {
                "protocol": "vless",
                "source": saved["source"],
                "totals": saved.get("user_totals", {}),
            }
            sync_request = Request(config["sync_endpoint"], data=json.dumps(sync_message).encode(), headers={
                "Content-Type": "application/json", "X-Usage-Token": config["token"]})
            with build_opener(NoRedirect).open(sync_request, timeout=8) as response:
                raw = response.read(256 * 1024)
            if raw:
                reply = json.loads(raw)
                desired = reply.get("active_vless_clients") if isinstance(reply, dict) else None
                server_json = config.get("server_json") or str(settings.parent / "adapter/server/server.json")
                if isinstance(desired, list) and Path(server_json).is_file():
                    sync_xray_clients(config["project"], server_json, desired)


def systemd_arg(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("Недопустимый путь для systemd.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def install(directory, project, endpoint, starter, devices, *, sync_endpoint="", server_json=None):
    _validate_endpoint(endpoint)
    _validate_endpoint(sync_endpoint)
    auth = directory / "meter-token"
    if not auth.exists():
        atomic_write(auth, secrets.token_urlsafe(32))
    token = auth.read_text()
    identity = catalog_id(starter, devices)
    settings = directory / "meter-settings.json"
    payload = dict(project=project, token=token, catalog=identity, endpoint=endpoint)
    if sync_endpoint:
        payload["sync_endpoint"] = sync_endpoint
    if server_json:
        payload["server_json"] = str(server_json)
    atomic_write(settings, json.dumps(payload))
    units = Path.home() / ".config/systemd/user"
    units.mkdir(mode=0o700, exist_ok=True, parents=True)
    name = project + "-usage"
    service = f"""[Unit]
Description=VLESS traffic counters for {project}
After=docker.service
[Service]
Type=oneshot
UMask=0077
NoNewPrivileges=yes
Environment=DOCKER_HOST=unix://%t/docker.sock
ExecStart=/usr/bin/python3 {systemd_arg(Path(__file__).resolve())} {systemd_arg(settings)}
"""
    timer = f"""[Unit]
Description=Sample VLESS traffic for {project}
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
    return {"token_hash": hashlib.sha256(token.encode()).hexdigest(), "catalog": identity,
            "starter_hash": hashlib.sha256(starter.encode()).hexdigest()}


if __name__ == "__main__":
    os.umask(0o077)
    collect(Path(sys.argv[1]))
