"""Round-trip an isolated panel database and node state into a second Compose project."""
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEPLOY = Path(__file__).resolve().parents[1]


def request(api, path, *, method="GET", body=None, headers=None, raw=False):
    req = urllib.request.Request(api + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = response.read()
        return data if raw or not data else json.loads(data)


def run(*args, env=None, stdin=None, stdout=subprocess.PIPE):
    return subprocess.run(args, check=True, env=env, stdin=stdin, stdout=stdout).stdout


def snapshot(state):
    return {str(f.relative_to(state)): (f.stat().st_mode & 0o777, hashlib.sha256(f.read_bytes()).digest())
            for f in state.rglob("*") if f.is_file()}


def rejected(status, api, resource, **kwargs):
    try:
        request(api, resource, **kwargs)
    except urllib.error.HTTPError as exc:
        assert exc.code == status, (resource, exc.code)
    else:
        raise AssertionError("Unexpected success for " + resource)


def check_migrated_profile(api, admin, node_id, credential, revision):
    entry = {"X-Node-Token": credential}
    node_api = f"/control/nodes/{node_id}"
    published = request(api, node_api + "/profile-topology", headers=entry)
    assert published["revision"] == revision
    topology = json.loads(published["manifest"])
    exit_node = request(api, "/servers", headers=admin, method="POST",
                        body={"address": "exit.example", "name": "recovery-exit", "public_key": ""})["id"]
    invite = request(api, f"/servers/{exit_node}/invitations", headers=admin, method="POST", body={})
    exit_token = request(api, f"/control/nodes/{exit_node}/enroll", method="POST", body={},
                         headers={"X-Enrollment-Token": invite["token"]})["token"]
    generate = runpy.run_path(str(DEPLOY / "scripts/init-state.py"))["signing_identity"]
    exit_key = base64.b64encode(generate()[1]).decode()
    request(api, f"/control/nodes/{exit_node}/identity", method="PUT", headers={"X-Node-Token": exit_token},
            body={"public_key": exit_key, "port": 4519})
    topology["paths"]["west"]["exit"] = exit_node
    revision = request(api, "/profile-topology", method="PUT", headers=admin,
                       body={"revision": revision, "manifest": json.dumps(topology)})["revision"]
    pub = generate()[1]
    uplink = request(api, node_api + "/uplink", method="PUT", headers=entry, body={
        "profile": "home", "path": "west", "revision": revision, "public_key": base64.b64encode(pub).decode()})
    assert uplink == {"address": "exit.example", "port": 4519, "public_key": exit_key}
    handshake = {"fingerprint": base64.b64encode(hashlib.sha256(pub).digest()[:16]).decode(),
                 "node_id": exit_node}
    def allowed(token, body):
        return request(api, "/check_access", method="POST", body=body, headers={"X-Auth-Key": token})["allowed"]
    assert allowed(exit_token, handshake) is True
    assert allowed(credential, dict(handshake, node_id=node_id)) is False
    rejected(401, api, "/check_access", method="POST", body=handshake, headers={"X-Auth-Key": credential})

    download = f"/servers/{node_id}/profile-config?profile=home"
    decisions = {"default": {"selected": "west", "strategy": "prefer-route", "eligible": ["west"]}}
    report = {"profile": "home", "revision": revision,
              "result": json.dumps({"status": "applied", "decisions": decisions})}
    error = dict(report, result=json.dumps({"status": "error", "error": "test failure"}))
    secret = {"profile": "home", "revision": revision, "config": secrets.token_urlsafe(32)}
    request(api, node_api + "/profile-config", method="PUT", headers=entry, body=secret)
    rejected(409, api, download, headers=admin)
    request(api, node_api + "/profile-result", method="PUT", headers=entry, body=error)
    rejected(409, api, download, headers=admin)
    for _ in range(2):
        request(api, node_api + "/profile-result", method="PUT", headers=entry, body=report)
    rows = request(api, "/profile-results", headers=admin)
    assert any(row["node_id"] == node_id and row["report"] == report for row in rows)
    history = request(api, f"/servers/{node_id}/profile-history?profile=home", headers=admin)
    assert [event["report"] for event in history] == [report, error]
    assert request(api, download, headers=admin) == secret
    request(api, "/profile-topology", method="PUT", headers=admin,
            body={"revision": revision, "manifest": json.dumps({"paths": {}, "profiles": {}})})
    rejected(404, api, download, headers=admin)
    rejected(409, api, node_api + "/profile-result", method="PUT", headers=entry, body=report)
    rejected(409, api, node_api + "/profile-config", method="PUT", headers=entry, body=secret)
    assert allowed(exit_token, handshake) is False
    print("Migrated database requires an applied report to download a current profile. "
          "Scoped handshake and revocation passed.")


def verify():
    # mkdtemp supplies an owner-only working directory on the controller.
    with tempfile.TemporaryDirectory(prefix="anet-restore-") as tmp, ExitStack() as cleanup:
        root = Path(tmp)
        project = "anet-recovery-" + secrets.token_hex(5)
        state = root / "source"
        run(sys.executable, DEPLOY / "scripts/init-state.py", state, "restore.example", "4519", "webui")
        run(sys.executable, DEPLOY / "scripts/init-probe-signing.py", state)

        def compose(label):
            prefix = ["docker", "compose", "--project-name", project + "-" + label,
                      "--env-file", "/dev/null", "-f", str(DEPLOY / "compose/compose.yaml"),
                      "-f", str(DEPLOY / "compose/webui.yaml"), "-f", str(DEPLOY / "compose/probe-management.yaml")]
            env = {**os.environ, "STATE_DIR": str(root / label), "PANEL_BIND": "127.0.0.1",
                   "PANEL_PORT": "0", "PROBE_VOLUME": project + "-" + label + "-probes",
                   "ANET_OFFLINE_BUNDLE": "1"}
            if label == "restored" and os.environ.get("RESTORE_AUTH_IMAGE"):
                env["AUTH_IMAGE"] = os.environ["RESTORE_AUTH_IMAGE"]
            def execute(*args, **kwargs):
                return run(*prefix, *args, env=env, **kwargs)
            cleanup.callback(execute, "down", "--volumes")
            return execute, prefix, env

        def endpoint(execute):
            return "http://" + execute("port", "webui", "8080").decode().strip() + "/api/v1"

        def login(url, path):
            password = (path / "admin-password").read_text().strip()
            return {"Authorization": "Bearer " + request(url, "/login", method="POST",
                    body={"login": "admin", "password": password})["access_token"]}

        source, prefix, env = compose("source")
        run(sys.executable, DEPLOY / "scripts/register-local-node.py", state, *prefix, env=env)
        api = endpoint(source)
        admin = login(api, state)
        node, = request(api, "/servers", headers=admin)
        user = request(api, "/add", method="POST", headers=admin,
                       body={"uid": "restore-test", "server_ids": [node["id"]]})
        config_path = "/config/" + user["id"]
        key = user["private_key"].encode()
        assert key and key in request(api, config_path, raw=True)
        topology = {"paths": {"west": {"entry": node["id"], "exit": node["id"], "transport": "quic",
                                       "external_ip": "192.0.2.1"}},
                    "profiles": {"home": {"entry": node["id"], "paths": ["west"], "strategy": "prefer-route"}}}
        revision = request(api, "/profile-topology", method="PUT", headers=admin,
                           body={"revision": 0, "manifest": json.dumps(topology)})
        incident = request(api, "/operator-incidents", method="POST", headers=admin,
                           body={"observer": "restore-test", "direction": "entry to exit",
                                 "transport": "wireguard", "evidence": "restore sentinel"})
        source("stop", "webui", "auth")
        archive, dump = root / "state.tar", root / "database.dump"
        run("tar", "-C", state, "-cpf", archive, ".")
        with dump.open("wb") as out:
            source("exec", "-T", "db", "pg_dump", "-Fc", "-U", "anet", "-d", "anet", stdout=out)
        source("stop", "db")
        assert dump.stat().st_size > 0
        restored = root / "restored"
        restored.mkdir(mode=0o700)
        run("tar", "-C", restored, "-xpf", archive)
        assert snapshot(restored) == snapshot(state)
        target, _, _ = compose("restored")
        target("up", "--pull", "never", "--no-build", "-d", "--wait", "db")
        with dump.open("rb") as inp:
            target("exec", "-T", "db", "pg_restore", "--single-transaction", "--exit-on-error",
                   "-U", "anet", "-d", "anet", stdin=inp)
        target("up", "--pull", "never", "--no-build", "-d", "--wait", "webui")
        api = endpoint(target)
        admin = login(api, restored)
        assert request(api, "/profile-topology", headers=admin) == revision
        assert incident in request(api, "/operator-incidents", headers=admin)
        restored_node, = request(api, "/servers", headers=admin)
        assert restored_node["id"] == node["id"]
        assert restored_node["public_key"] == (restored / "server-public-key").read_text().strip()
        assert key in request(api, config_path, raw=True)
        control = (restored / "server.toml").read_text().split("[control_plane]")[1]
        credential = next(json.loads(line.partition("=")[2]) for line in control.splitlines()
                          if line.startswith("token ="))
        request(api, "/control/nodes/heartbeat", method="POST", headers={"X-Node-Token": credential},
                body={"node_id": node["id"], "version": "restore-test", "uptime_seconds": 1,
                      "active_connections": 0, "accepting_connections": True})
        if os.environ.get("RESTORE_AUTH_IMAGE"):
            check_migrated_profile(api, admin, node["id"], credential, revision["revision"])
        print("Recovered node keys and file modes, admin login, topology revision, incident, "
              "client private key and enrolled node heartbeat on a new database volume.")


if __name__ == "__main__":
    os.umask(0o077)
    verify()
