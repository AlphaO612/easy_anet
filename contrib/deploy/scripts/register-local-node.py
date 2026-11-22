"""Register the first local node. Consume existing inventory only during bootstrap."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request


def pull_flags():
    return ["--pull", "never"] if os.environ.get("ANET_OFFLINE_BUNDLE") == "1" else []


def request(base, path, *, data=None, token=None, extra=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    headers.update(extra or {})
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body, headers=headers)
    # Bootstrap only calls the local Compose endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as response:
        return json.load(response)


def register(state, base, compose):
    config_path = state / "server.toml"
    old_config = config_path.read_text()
    login = {"login": "admin", "password": (state / "admin-password").read_text().strip()}
    try:
        session = request(base, "/login", data=login)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        with (state / "admin-password").open("rb") as password:
            subprocess.run([*compose, "exec", "-T", "auth", "/usr/local/bin/anet-auth",
                            "--add-su", "admin", "--password-file", "/dev/stdin"],
                           stdin=password, check=True)
        session = request(base, "/login", data=login)
    token = session["access_token"]
    metadata = json.loads((state / "node.json").read_text())
    key = (state / "server-public-key").read_text().strip()
    matches = [node for node in request(base, "/servers", token=token) if node["public_key"] == key]
    if len(matches) > 1:
        raise ValueError("multiple registered nodes share the local public key")
    node = matches[0] if matches else request(base, "/servers", token=token, data={
        "name": metadata["host"], "address": metadata["host"],
        "quic_port": metadata["port"], "public_key": key})
    node_id = node["id"]
    invite = request(base, f"/servers/{node_id}/invitations", token=token, data={})
    credential = request(base, f"/control/nodes/{node_id}/enroll", data={},
                         extra={"X-Enrollment-Token": invite["token"]})
    if credential["node_id"] != node_id:
        raise ValueError("enrollment response node ID mismatch")
    with tempfile.NamedTemporaryFile("w", dir=state, delete=False) as updated:
        updated.write(old_config + f'\n[control_plane]\nnode_id = {json.dumps(node_id)}\n'
                      f'url = "http://auth:3000/api/v1"\ntoken = {json.dumps(credential["token"])}\n')
        updated.flush()
        os.fsync(updated.fileno())
    os.chmod(updated.name, 0o444)
    os.replace(updated.name, config_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("compose", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    state, compose = args.state, args.compose
    if "[control_plane]" not in (state / "server.toml").read_text().splitlines():
        subprocess.run([*compose, "up", *pull_flags(), "--no-build", "-d", "--wait", "webui"], check=True)
        mapping = subprocess.check_output([*compose, "port", "webui", "8080"], text=True).strip()
        host, port = mapping.rsplit(":", 1)
        if host in ("0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        register(state, f"http://{host}:{int(port)}/api/v1", compose)
