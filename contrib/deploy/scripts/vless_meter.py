"""Sample non-resetting Xray counters and send cumulative totals for one entry route."""
import fcntl
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

from topology import NoRedirect, atomic_write

SOCKET = "/anet-usage/stats.sock"
STARTER = "anet-starter@anet.invalid"
COUNTER = re.compile(r"user>>>(anet-device-([0-9]+)@anet\.invalid|anet-starter@anet\.invalid)>>>traffic>>>(uplink|downlink)")


def configure(config, clients):
    if not clients[0].get("email"):
        clients[0]["email"] = STARTER
    config["stats"] = {}
    stats = config.setdefault("policy", {}).setdefault("levels", {}).setdefault("0", {})
    stats.update(statsUserUplink=True, statsUserDownlink=True)
    config["api"] = {"tag": "anet-stats", "services": ["StatsService"]}
    inbounds = config["inbounds"]
    if not any(row.get("tag") == "anet-stats-in" for row in inbounds):
        inbounds.append({"tag": "anet-stats-in", "protocol": "dokodemo-door", "listen": SOCKET,
                         "settings": {"network": "unix", "address": "127.0.0.1", "port": 1}})
    rules = config.setdefault("routing", {}).setdefault("rules", [])
    if not any(rule.get("outboundTag") == "anet-stats" for rule in rules):
        rules.insert(0, {"type": "field", "inboundTag": ["anet-stats-in"], "outboundTag": "anet-stats"})


def catalog_id(starter, devices):
    return hashlib.sha256("\0".join([starter, *devices]).encode()).hexdigest()


def sample(project):
    ps = ["docker", "ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project,
          "--filter", "label=com.docker.compose.service=ingress"]
    cid, = subprocess.check_output(ps, text=True, timeout=6).split()
    inspect = ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid]
    started = subprocess.check_output(inspect, text=True, timeout=6).strip()
    command = ["docker", "exec", "-u", "17777:17777", cid, "xray", "api", "statsquery",
               "--server=unix://" + SOCKET, "-pattern", "user>>>"]
    stats = json.loads(subprocess.check_output(command, timeout=10))
    if started != subprocess.check_output(inspect, text=True, timeout=6).strip():
        raise ValueError("Xray restarted during sample")
    counters = {}
    for stat in stats.get("stat", []):
        match = COUNTER.fullmatch(stat["name"])
        if not match:
            continue
        slot = match[2] if match[2] is not None else "starter"
        if slot != "starter" and int(slot) >= 128:
            continue
        pair = counters.setdefault(slot, [0, 0])
        pair[0 if match[3] == "uplink" else 1] = int(stat.get("value", 0))
    return cid + "/" + started, counters


def accumulate(saved, generation, counters):
    previous = saved.get("last", {}) if saved.get("generation") == generation else {}
    totals = saved.setdefault("totals", {})
    for slot, pair in counters.items():
        last = previous.get(slot, [0, 0])
        current = totals.setdefault(slot, [0, 0])
        for i, value in enumerate(pair):
            if value < 0:
                raise ValueError("invalid Xray counter")
            current[i] += value - last[i] if value >= last[i] else value
    saved["generation"] = generation
    saved["last"] = {**previous, **counters}


def collect(settings):
    with (settings.parent / "meter.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(settings.read_text())
        state = settings.parent / "meter-state.json"
        saved = json.loads(state.read_text()) if state.exists() else {}
        if saved.get("catalog") != config["catalog"]:
            saved = {"catalog": config["catalog"], "source": secrets.token_hex(16)}
        generation, counters = sample(config["project"])
        accumulate(saved, generation, counters)
        atomic_write(state, json.dumps(saved))
        if config["endpoint"]:
            message = {k: saved[k] for k in ("source", "catalog", "totals")}
            request = Request(config["endpoint"], data=json.dumps(message).encode(), headers={
                "Content-Type": "application/json", "X-Usage-Token": config["token"]})
            with build_opener(NoRedirect).open(request, timeout=8):
                pass


def systemd_arg(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("Недопустимый путь для systemd.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def install(directory, project, endpoint, starter, devices):
    if endpoint:
        site = urlsplit(endpoint)
        if not (site.scheme == "https" and site.hostname or site.scheme == "http" and site.hostname == "127.0.0.1") or site.username or site.password or site.query or site.fragment:
            raise ValueError("Для счётчиков укажите HTTPS-адрес панели либо локальный порт на первой VPS.")
    auth = directory / "meter-token"
    if not auth.exists():
        atomic_write(auth, secrets.token_urlsafe(32))
    token = auth.read_text()
    identity = catalog_id(starter, devices)
    settings = directory / "meter-settings.json"
    atomic_write(settings, json.dumps(dict(project=project, token=token, catalog=identity, endpoint=endpoint)))
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
