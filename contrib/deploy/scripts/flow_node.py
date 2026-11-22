"""Outgoing control channel for an enrolled flow agent."""
import base64
import json
import urllib.parse
import urllib.request
import uuid

from topology import NoRedirect


class NodeAPI:
    def __init__(self, base, config, signing_public):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        self.verifier = Ed25519PublicKey.from_public_bytes(base64.b64decode(signing_public.read_text().strip(), validate=True))
        site = urllib.parse.urlsplit(base)
        if site.scheme != "https" and not (site.scheme == "http" and site.hostname in {"127.0.0.1", "::1", "localhost"}):
            raise ValueError("node API requires HTTPS or loopback")
        with config.open("rb") as file:
            credential = tomllib.load(file)["control_plane"]
        self.id = str(uuid.UUID(credential["node_id"]))
        self.url = base.rstrip("/") + "/control/nodes/" + self.id
        self.headers = {"X-Node-Token": credential["token"], "Content-Type": "application/json"}

    def request(self, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.url + path, data=body, headers=self.headers,
                                         method="PUT" if body is not None else "GET")
        with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
            if path in {"/profile-result", "/profile-config"}:
                return None
            value = json.load(response)
            if path == "/profile-topology":
                payload = f"anet/flow/v1\n{self.id}\n{value['revision']}\n{value['manifest']}".encode()
                from cryptography.exceptions import InvalidSignature
                try:
                    self.verifier.verify(base64.b64decode(value["signature"], validate=True), payload)
                except InvalidSignature as exc:
                    raise ValueError("invalid topology signature") from exc
            return value
