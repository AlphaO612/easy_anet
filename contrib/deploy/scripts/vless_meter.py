"""Sample non-resetting Xray counters, send cumulative totals, and live-sync VLESS clients."""
import base64
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
import uuid
import zipfile

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


def _query_live_users(cid, tag):
    """Query live in-memory VLESS clients from Xray HandlerService."""
    try:
        out = subprocess.check_output(
            ["docker", "exec", "-u", "17777:17777", cid, "xray", "api", "inbounduser",
             "--server=unix://" + SOCKET, "-tag", tag],
            text=True, timeout=10,
        )
        data = json.loads(out or "{}")
        if not isinstance(data, dict) or "users" not in data:
            return None
        users = {}
        for row in data.get("users", []):
            email = (row.get("email") or "").strip()
            acc = row.get("account") or {}
            uid = (acc.get("id") or "").strip()
            if uid:
                users[uid] = {"id": uid, "email": email, "level": int(row.get("level") or 0)}
        return users
    except Exception:
        return None


def sync_xray_clients(project, server_json_path, desired_clients, *, preserve_starter=True):
    """Live-sync Xray VLESS clients via HandlerService (adu/rmu) and persist atomically."""
    server_path = Path(server_json_path)
    config = json.loads(server_path.read_text())
    inbound, = [item for item in config["inbounds"] if item.get("protocol") == "vless"]
    tag = inbound.setdefault("tag", INBOUND_TAG)
    clients = inbound.setdefault("settings", {}).setdefault("clients", [])
    desired = _normalize_desired_clients(desired_clients)

    cid = _find_ingress_container(project)
    live_users = _query_live_users(cid, tag)

    starter_clients = [c for c in clients if preserve_starter and c.get("email") == STARTER and c.get("id") not in desired]
    starter_ids = {c["id"] for c in starter_clients}
    if live_users is not None:
        # Preserve any starter present in live Xray memory too
        for uid, u in live_users.items():
            if preserve_starter and u.get("email") == STARTER and uid not in desired and uid not in starter_ids:
                starter_clients.append(u)
                starter_ids.add(uid)
        existing = {uid: u for uid, u in live_users.items() if uid not in starter_ids}
    else:
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

    remove_emails = [c["email"] for c in to_remove if c.get("email")]
    if remove_emails:
        subprocess.run(
            ["docker", "exec", "-u", "17777:17777", cid, "xray", "api", "rmu",
             "--server=unix://" + SOCKET, "-tag", tag, *remove_emails],
            check=True, capture_output=True, text=True, timeout=10,
        )
    if to_add:
        payload = {
            "inbounds": [{
                "tag": tag,
                "listen": inbound.get("listen", "0.0.0.0"),
                "port": int(inbound.get("port", 8443)),
                "protocol": "vless",
                "settings": {"decryption": "none", "clients": to_add},
            }]
        }
        res = subprocess.run(
            ["docker", "exec", "-i", "-u", "17777:17777", cid, "sh", "-ec",
             "cat > /anet-usage/adu.json && xray api adu --server=unix://" + SOCKET + " /anet-usage/adu.json; rc=$?; rm -f /anet-usage/adu.json; exit $rc"],
            input=json.dumps(payload), check=True, capture_output=True, text=True, timeout=10,
        )
        combined = (res.stdout or "") + (res.stderr or "")
        if "Added 0 user(s)" in combined or "failed to build config" in combined:
            raise RuntimeError(f"xray api adu failed to add clients: {combined.strip()}")

    inbound["settings"]["clients"] = starter_clients + [desired[uid] for uid in sorted(desired)]
    configure(config, inbound["settings"]["clients"] or [{"id": "", "email": STARTER}])
    atomic_write(server_path, json.dumps(config, indent=2) + "\n")
    return {"added": [c["id"] for c in to_add], "removed": [c["id"] for c in to_remove]}


def _extract_exit_host(uplink_toml_path):
    p = Path(uplink_toml_path)
    if not p.is_file():
        return ""
    text = p.read_text(errors="ignore")
    m = re.search(r'(?m)^\s*(?:endpoint|host|address)\s*=\s*"([^"]+)"', text)
    if not m:
        return ""
    raw = m.group(1).strip()
    if raw.startswith("[") and "]:" in raw:
        return raw[1:raw.index("]:")]
    return raw.rsplit(":", 1)[0] if ":" in raw else raw


def probe_uplink_and_rtt(project, uplink_toml_path):
    """Check anet-uplink status in network container and measure direct TCP RTT/loss to exit."""
    uplink_up = None
    try:
        ps = ["docker", "ps", "-q", "--no-trunc",
              "--filter", "label=com.docker.compose.project=" + project,
              "--filter", "label=com.docker.compose.service=network"]
        net_cids = subprocess.check_output(ps, text=True, timeout=6).split()
        if net_cids:
            out = subprocess.check_output(
                ["docker", "exec", net_cids[0], "sh", "-c",
                 "ip -4 -o addr show anet-uplink 2>/dev/null; ip -4 route show table 177 2>/dev/null"],
                text=True, timeout=6,
            )
            uplink_up = (" inet " in out and "default dev anet-uplink" in out and "linkdown" not in out)
    except Exception:
        uplink_up = None

    latency_ms = None
    loss_pct = None
    exit_host = _extract_exit_host(uplink_toml_path)
    if exit_host:
        samples = []
        attempts = 3
        for _ in range(attempts):
            t0 = time.monotonic()
            try:
                with socket.create_connection((exit_host, 22), timeout=2.5):
                    samples.append((time.monotonic() - t0) * 1000.0)
            except OSError:
                pass
        loss_pct = round(((attempts - len(samples)) / attempts) * 100.0, 1)
        if samples:
            latency_ms = round(sum(samples) / len(samples), 2)

    uptime_seconds = None
    try:
        uptime_seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        pass
    return uplink_up, latency_ms, loss_pct, uptime_seconds


def _apply_site_bundle(docroot_path, mode, raw_bytes):
    """Atomically update nginx decoy docroot while strictly preserving .well-known."""
    root = Path(docroot_path)
    if not root.is_dir():
        return
    if mode in ("preset", "custom_html"):
        atomic_write(root / "index.html", raw_bytes.decode("utf-8", "replace"))
        os.chmod(root / "index.html", 0o644)
        for child in list(root.iterdir()):
            if child.name in (".well-known", "index.html"):
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        return
    if mode == "custom_zip":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            infos = zf.infolist()
            if len(infos) > 150:
                raise ValueError("ZIP bundle has too many files (>150)")
            total_size = 0
            extracted = {}
            for info in infos:
                if info.is_dir():
                    continue
                # Reject symlinks (Unix mode 0120000)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Symlinks are not allowed in site ZIP bundle")
                rel = Path(info.filename)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] == ".well-known":
                    raise ValueError(f"Unsafe path in site ZIP: {info.filename}")
                data = zf.read(info)
                total_size += len(data)
                if total_size > 12 * 1024 * 1024:
                    raise ValueError("Unpacked ZIP bundle exceeds 12 MiB")
                extracted[rel] = data
            if Path("index.html") not in extracted:
                raise ValueError("ZIP bundle must contain index.html at root")
            for child in list(root.iterdir()):
                if child.name == ".well-known":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            for rel, data in extracted.items():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                os.chmod(dest, 0o644)


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
            uplink_toml = config.get("uplink_toml") or str(settings.parent / "uplink.toml")
            uplink_up, latency_ms, loss_pct, uptime_sec = probe_uplink_and_rtt(config["project"], uplink_toml)
            sync_message = {
                "protocol": "vless",
                "source": saved["source"],
                "totals": saved.get("user_totals", {}),
                "uplink_up": uplink_up,
                "latency_ms": latency_ms,
                "loss_pct": loss_pct,
                "uptime_seconds": uptime_sec,
            }
            sync_request = Request(config["sync_endpoint"], data=json.dumps(sync_message).encode(), headers={
                "Content-Type": "application/json", "X-Usage-Token": config["token"]})
            with build_opener(NoRedirect).open(sync_request, timeout=10) as response:
                raw = response.read(256 * 1024)
            if raw:
                reply = json.loads(raw)
                desired = reply.get("active_vless_clients") if isinstance(reply, dict) else None
                server_json = config.get("server_json") or str(settings.parent / "adapter/server/server.json")
                if isinstance(desired, list) and Path(server_json).is_file():
                    sync_xray_clients(config["project"], server_json, desired)
                want_sha = reply.get("site_sha256") if isinstance(reply, dict) else None
                docroot = config.get("site_docroot")
                if want_sha and docroot and Path(docroot).is_dir() and saved.get("site_sha256") != want_sha:
                    site_req = Request(
                        config["sync_endpoint"].rstrip("/") + "/site",
                        headers={"X-Usage-Token": config["token"]},
                    )
                    with build_opener(NoRedirect).open(site_req, timeout=20) as site_resp:
                        site_payload = json.loads(site_resp.read(8 * 1024 * 1024))
                    raw_bundle = base64.b64decode(site_payload["bundle_base64"], validate=True)
                    if hashlib.sha256(raw_bundle).hexdigest() == site_payload["sha256"]:
                        _apply_site_bundle(docroot, site_payload["mode"], raw_bundle)
                        saved["site_sha256"] = site_payload["sha256"]
                        atomic_write(state, json.dumps(saved))


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
