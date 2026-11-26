"""Apply one wizard assignment on its VPS. Credentials enter and leave on pipes."""
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
import re
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import uuid

import vless_meter
import wg_sync
from urllib.parse import urlsplit

from bundle import load_bundle
from topology import atomic_write
from wizard_inventory import sync_exits
from wizard_tls import verify_listener

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
CHAIN = DEPLOY.parent / "chain"
initialize = runpy.run_path(str(HERE / "init-state.py"))["initialize"]


def command(args, **kwargs):
    subprocess.run(args, check=True, stdout=sys.stderr, **kwargs)


def provision(job):
    root = Path.home() / ".local/state/anet-wizard" / job["installation"]
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Каталог установки должен быть приватным")
    with (root / ".lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if job["action"] == "images":
            if not Path("/dev/net/tun").is_char_device():
                raise ValueError("Требуется /dev/net/tun")
            if b"name=rootless" not in subprocess.check_output(["docker", "info", "--format", "{{json .SecurityOptions}}"]):
                raise ValueError("Требуется rootless Docker")
            bundle = Path(job["bundle"]).expanduser()
            refs = load_bundle(bundle, job["profile"])
            atomic_write(root / "images.json", json.dumps(refs))
            return {}
        return service(root, job)


def publish_catalog(directory, rows):
    # Docker maps the nginx UID into a subordinate host UID. The parent
    # directory stays 0700, only the bind-mounted subtree is traversable.
    catalog = directory / "profiles"
    files = catalog / "files"
    files.mkdir(parents=True, exist_ok=True)
    catalog.chmod(0o755)
    files.chmod(0o755)
    index = []
    for row in rows:
        if not re.fullmatch(r"profile-[1-9][0-9]*", row["id"]):
            raise ValueError("Неверный идентификатор профиля")
        ext = {"wireguard": "conf", "vless": "uri", "anet": "toml"}[row["protocol"]]
        path = files / (row["id"] + "." + ext)
        atomic_write(path, row["config"])
        path.chmod(0o444)
        item = {key: row[key] for key in ("id", "protocol", "entry", "exit", "port")}
        if row.get("exit_public_key"):
            item["exit_public_key"] = row["exit_public_key"]
        if row.get("wg_server_public_key"):
            item["wg_server_public_key"] = row["wg_server_public_key"]
        index.append(item)
    atomic_write(catalog / "index.json", json.dumps(index))
    (catalog / "index.json").chmod(0o444)
    return catalog


def vless_devices(server_dir):
    """Pre-authorize distinct device credentials on this route; preserve IDs across retries."""
    path = server_dir / "server.json"
    config = json.loads(path.read_text())
    inbound, = [item for item in config["inbounds"] if item["protocol"] == "vless"]
    clients = inbound["settings"]["clients"]
    prefix = "anet-device-"
    by_email = {item.get("email"): item["id"] for item in clients}
    devices = []
    before = json.dumps(config)
    vless_meter.configure(config, clients)
    for slot in range(128):
        email = f"{prefix}{slot}@anet.invalid"
        device = by_email.get(email)
        if device is None:
            device = str(uuid.uuid4())
            clients.append({"id": device, "email": email})
        devices.append(device)
    if json.dumps(config) != before:
        atomic_write(path, json.dumps(config))
    return devices


def publish_device_catalog(directory, rows, origin):
    catalog = directory / "vless-catalog"
    catalog.mkdir(exist_ok=True)
    catalog.chmod(0o755)
    routes = {row["id"]: {k: row[k] for k in ("entry", "exit", "port", "devices", "usage") if k in row}
              for row in rows if row.get("devices")}
    wireguard_routes = {
        row["id"]: {k: row[k] for k in ("entry", "exit", "port", "wg_server_public_key", "usage") if k in row}
        for row in rows if row.get("protocol") == "wireguard" and (row.get("wg_server_public_key") or row.get("usage"))
    }
    payload = {"origin": origin, "routes": routes}
    if wireguard_routes:
        payload["wireguard_routes"] = wireguard_routes
    path = catalog / "index.json"
    atomic_write(path, json.dumps(payload))
    path.chmod(0o444)
    return catalog


def service(root, job):
    action, name = job["action"], job["name"]
    directory = root / name
    directory.mkdir(mode=0o700, exist_ok=True)
    state = directory / "node"
    panel = action == "panel"
    network = "10.78.0" if action == "exit" else "10.77.0"
    subnet = network + ".0/24"
    initialize(state, job["host"], job["native_port"], "webui" if panel else "standalone", subnet)
    env = dict(os.environ)
    env.update(json.loads((root / "images.json").read_text()), STATE_DIR=str(state),
               VPN_PORT=str(job["native_port"]), VPN_BIND="0.0.0.0", TUN_SUBNET=subnet,
               TUN_ADDRESS=network + ".2/24", ANET_OFFLINE_BUNDLE="1")
    project = "anet-" + root.name + "-" + name
    files = [DEPLOY / "compose/compose.yaml"]
    config = state / "client.toml"
    devices = []
    if panel:
        catalog = publish_catalog(directory, job["catalog"])
        private_catalog = publish_device_catalog(directory, job["catalog"], job.get("subscription_origin", ""))
        overlay = directory / "webui-mount.json"
        atomic_write(overlay, json.dumps({"services": {
            "webui": {"volumes": [
                {"type": "bind", "source": str(catalog), "target": "/srv/anet-profiles", "read_only": True}
            ]},
            "vless-profiles": {"volumes": [
                {"type": "bind", "source": str(private_catalog), "target": "/srv/anet-vless-catalog", "read_only": True}
            ]},
            "auth": {"volumes": [
                {"type": "bind", "source": str(catalog), "target": "/srv/anet-profiles", "read_only": True},
                {"type": "bind", "source": str(private_catalog), "target": "/srv/anet-vless-catalog", "read_only": True}
            ]}
        }}))
        files.extend([DEPLOY / "compose/webui.yaml", DEPLOY / "compose/probe-management.yaml", overlay])
        env.update(PROBE_VOLUME=project + "-probes", PANEL_PORT=str(job["panel_port"]), PANEL_BIND="127.0.0.1")
        runpy.run_path(str(HERE / "init-probe-signing.py"))["initialize"](state)
    elif action == "entry":
        files.append(DEPLOY / "compose/chain.yaml")
        uplink = directory / "uplink.toml"
        contents = job["uplink"] + '\n[main]\nmanual_routing = true\ntun_name = "anet-uplink"\n'
        if uplink.exists() and uplink.read_text() != contents:
            raise ValueError("Ключ выхода изменился. Проверьте сохранённое назначение.")
        if not uplink.exists():
            atomic_write(uplink, contents)
        env["UPLINK_CONFIG"] = str(uplink)
        protocol = job["protocol"]
        if protocol != "anet":
            target = directory / "adapter"
            if not target.exists():
                with tempfile.TemporaryDirectory(dir=directory) as tmp:
                    staged = Path(tmp) / "profile"
                    command([sys.executable, CHAIN / "issue-ingress.py", protocol, staged,
                             "--host", job["host"], "--port", str(job["port"]), "--dns", "1.1.1.1"])
                    staged.rename(target)
            files.append(CHAIN / "ingress.yaml")
            if protocol == "vless":
                if job.get("tls_mode"):
                    source = root / "tls" / job["host"]
                    cert = (source / "fullchain.pem").read_text()
                    key = (source / "privkey.pem").read_text()
                else:
                    cert, key = job["cert"], job["key"]
                server_dir = target / "server"
                devices = vless_devices(server_dir)
                for filename, contents in (("tls.pem", cert), ("tls.key", key)):
                    current = server_dir / filename
                    if not current.exists() or current.read_text() != contents:
                        atomic_write(current, contents)
                # Xray reads its pair at process startup. A new certificate
                # changes the Compose model even after a retry following a crash.
                overlay = directory / "tls.json"
                atomic_write(overlay, json.dumps({"services": {"ingress": {"labels": {
                    "anet.tls.chain": hashlib.sha256(cert.encode()).hexdigest(),
                    "anet.vless.clients": hashlib.sha256((server_dir / "server.json").read_bytes()).hexdigest()
                }, "tmpfs": ["/anet-usage:size=64k,uid=17777,gid=17777,mode=0700,noexec,nosuid,nodev"]}}}))
                files.append(overlay)
            env.update(VPN_BIND="127.0.0.1", VPN_PORT="0", INGRESS_PATH="chain", INGRESS_BIND="0.0.0.0",
                       INGRESS_PORT=str(job["port"]), INGRESS_PROTOCOL=protocol,
                       INGRESS_TARGET="51820/udp" if protocol == "wireguard" else "8443/tcp",
                       INGRESS_DIR=str(target / "server"))
            config = target / ("client.conf" if protocol == "wireguard" else "client.uri")
    cmd = ["docker", "compose", "--env-file", "/dev/null", "-p", project]
    for file in files:
        cmd += ["-f", str(file)]
    command([*cmd, "config", "--quiet"], env=env)
    if panel:
        command([sys.executable, HERE / "register-local-node.py", state, *cmd], env=env)
    if action == "entry":
        # Some supported Compose versions require a Dockerfile HEALTHCHECK
        # on *every* service for --wait, including the shell-less ANet client.
        command([*cmd, "up", "--pull", "never", "--no-build", "-d"], env=env)
        check = "ip -4 route show table 177 | grep -q '^default dev anet-uplink '"
        if job["protocol"] == "wireguard":
            check += " && ip link show dev wg-profile >/dev/null"
        tls_error = None
        expected = 4 if job["protocol"] == "anet" else 5
        for _ in range(60):
            probe = subprocess.run([*cmd, "exec", "-T", "network", "sh", "-ec", check],
                                   env=env, capture_output=True, timeout=10)
            if probe.returncode == 0:
                containers = subprocess.check_output([*cmd, "ps", "-q"], env=env, text=True).splitlines()
                if len(containers) == expected:
                    statuses = subprocess.check_output(["docker", "inspect", "--format",
                        "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                        *containers], env=env, text=True).splitlines()
                    if all(s.strip() in ("running", "running healthy") for s in statuses):
                        if job["protocol"] != "vless":
                            break
                        try:
                            verify_listener(job["port"], job["host"], server_dir / "tls.pem",
                                            public_trust=job.get("tls_mode") == "acme")
                        except (OSError, ValueError) as error:
                            tls_error = error
                        else:
                            break
            time.sleep(2)
        else:
            if tls_error:
                raise ValueError("VLESS TLS handshake failed: проверьте домен, срок действия и цепочку сертификата.") from tls_error
            raise ValueError("Вход запущен, но связь или входной протокол не готовы. Проверьте UDP-порт выхода и журналы Compose.")
    else:
        command([*cmd, "up", "--pull", "never", "--no-build", "-d", "--wait", "--wait-timeout", "180"], env=env)
    if action == "exit":
        return {"config": config.read_text(), "public_key": (state / "server-public-key").read_text().strip()}
    if panel:
        sync_exits(cmd, env, job.get("exits", []))
        return {"password_file": str(state / "admin-password")}
    result = {"config": config.read_text(), "devices": devices}
    if devices:
        starter = urlsplit(result["config"].strip()).username
        result["usage"] = vless_meter.install(
            directory, project, job.get("usage_endpoint", ""), starter, devices,
            sync_endpoint=job.get("sync_endpoint", ""),
            server_json=str(directory / "adapter/server/server.json"),
        )
    elif action == "entry" and job.get("protocol") == "wireguard":
        wg_conf = directory / "adapter/server/wg.conf"
        client_conf = directory / "adapter/client.conf"
        wg_server_pub = wg_sync.extract_server_public_key(wg_conf, client_conf)
        if wg_server_pub:
            result["wg_server_public_key"] = wg_server_pub
        if job.get("sync_endpoint"):
            starter_pub = wg_sync.extract_starter_client_public_key(wg_conf)
            result["usage"] = wg_sync.install(
                directory, project, job["sync_endpoint"], wg_server_pub,
                starter_public_key=starter_pub, wg_conf=wg_conf,
            )
    return result


if __name__ == "__main__":
    os.umask(0o077)
    try:
        print(json.dumps(provision(json.load(sys.stdin))))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
