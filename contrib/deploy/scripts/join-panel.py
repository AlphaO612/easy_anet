"""Enroll a dedicated standalone node as a managed exit using a one-use invitation."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from uuid import UUID

from topology import NoRedirect, atomic_write, read


def enroll(state, settings):
    api = settings["api"].rstrip("/")
    site = urlsplit(api)
    if site.scheme != "https" and not (site.scheme == "http" and site.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("remote enrollment requires HTTPS")
    node = str(UUID(settings["node_id"]))
    path = state / "server.toml"
    raw = path.read_text()
    cfg = tomllib.loads(raw)
    def register_key(token):
        body = dict(public_key=(state / "server-public-key").read_text().strip(), port=read(state / "node.json")["port"])
        request = Request(f"{api}/control/nodes/{node}/identity", method="PUT", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-Node-Token": token})
        with build_opener(NoRedirect).open(request, timeout=12):
            pass

    if "control_plane" in cfg:
        if cfg["control_plane"]["node_id"] != node or cfg["control_plane"]["url"] != api:
            raise ValueError("existing node belongs to a different panel or identity")
        register_key(cfg["control_plane"]["token"])
        return
    cache = state / "enrollment.json"
    if not cache.exists():
        token = Path(settings["invitation_file"]).read_text().strip()
        req = Request(f"{api}/control/nodes/{node}/enroll", data=b"{}", headers={
            "X-Enrollment-Token": token, "Content-Type": "application/json"})
        with build_opener(NoRedirect).open(req, timeout=12) as reply:
            credential = json.load(reply)
        if credential["node_id"] != node or len(credential["token"]) < 32:
            raise ValueError("unexpected enrollment response")
        atomic_write(cache, json.dumps(dict(api=api, **credential)))
    credential = read(cache)
    if credential["node_id"] != node or credential["api"] != api:
        raise ValueError("enrollment belongs to a different node")
    register_key(credential["token"])
    # Local standalone clients keep their keys. This exit additionally
    # verifies assigned uplinks against the panel using its scoped credential.
    for key, value in [("auth_servers", [api]), ("auth_server_token", credential["token"])]:
        raw, count = re.subn(rf"(?m)^{key} = .*?$", lambda _: f"{key} = {json.dumps(value)}", raw)
        if count != 1:
            raise ValueError(f"expected one generated {key} setting")
    raw += f'\n[control_plane]\nnode_id = "{node}"\nurl = {json.dumps(api)}\ntoken = {json.dumps(credential["token"])}\n'
    tomllib.loads(raw)
    with tempfile.NamedTemporaryFile("w", dir=state, delete=False) as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
        os.fchmod(out.fileno(), 0o444)
    (state / ".enrollment-pending").touch(mode=0o600)
    os.replace(out.name, path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("settings", type=Path)
    args = parser.parse_args()
    enroll(args.state, read(args.settings))
