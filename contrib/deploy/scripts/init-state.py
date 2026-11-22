"""Generate first-install material atomically. Existing state belongs to the operator."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile


def openssl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, stdout=subprocess.PIPE,
                          check=True).stdout


def signing_identity():
    pem = openssl("genpkey", "-algorithm", "ED25519")
    private = openssl("pkey", "-outform", "DER", data=pem)
    public = openssl("pkey", "-pubout", "-outform", "DER", data=pem)
    # RFC 8410 OneAsymmetricKey and SubjectPublicKeyInfo for Ed25519.
    if private[:-32].hex() != "302e020100300506032b657004220420" or public[:-32].hex() != "302a300506032b6570032100":
        raise ValueError("unexpected Ed25519 DER encoding")
    return base64.b64encode(private[-32:]).decode(), public[-32:]


def host_for_url(host):
    try:
        address = ipaddress.ip_address(host)
        return f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.split(".")):
            raise ValueError("expected a DNS name or IP address")
        return host


def initialize(path, host, port, profile, subnet="10.77.0.0/24"):
    network = ipaddress.IPv4Network(subnet)
    if network.prefixlen > 29:
        raise ValueError("tunnel subnet needs at least eight addresses")
    host = host_for_url(host)
    if not 1 <= port <= 65535:
        raise ValueError("port outside 1..65535")
    expected = {"profile": profile, "host": host, "port": port}
    if str(network) != "10.77.0.0/24":
        expected["subnet"] = str(network)
    metadata = path / "node.json"
    if path.exists():
        if json.loads(metadata.read_text()) != expected:
            raise ValueError("existing deployment has different settings; adjust existing node explicitly")
        for name in ["server.toml", "server-public-key"] + (
                ["auth.env", "db-password"] if profile == "webui" else ["client.toml"]):
            if not (path / name).is_file():
                raise ValueError(f"restore missing {name} from backup")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(dir=path.parent, prefix=".anet-init-"))
    try:
        client_private, client_public = signing_identity()
        server_private, server_public = signing_identity()
        fingerprint = base64.b64encode(hashlib.sha256(client_public).digest()[:16]).decode()
        backend_key = secrets.token_hex(32)
        cert = openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                       "-noenc", "-days", "365", "-subj", "/CN=alco", "-addext", "subjectAltName=DNS:alco",
                       "-addext", "basicConstraints=critical,CA:FALSE", "-keyout", str(temp / "tls.key")).decode()
        private = (temp / "tls.key").read_text()
        (temp / "tls.key").unlink()
        q = json.dumps
        auth_servers = ["http://auth:3000/api/v1"] if profile == "webui" else []
        # With WebUI every client must be authorized through the database.
        allowed = [fingerprint] if profile == "standalone" else []
        (temp / "server.toml").write_text(f'''[network]
net = "{network.network_address}"
mask = "{network.netmask}"
gateway = "{network[1]}"
self_ip = "{network[2]}"
if_name = "anet-server"
mtu = 1420

[server]
quic_bind_to = "0.0.0.0:4519"
ssh_bind_to = ""
vnc_bind_to = ""
ahttp_bind_to = ""
websocket_bind_to = ""

[authentication]
allowed_clients = {q(allowed)}
auth_servers = {q(auth_servers)}
auth_server_token = {q(backend_key)}

[crypto]
quic_cert = {q(cert)}
quic_key = {q(private)}
server_signing_key = {q(server_private)}
''')
        (temp / "server-public-key").write_text(base64.b64encode(server_public).decode() + "\n")
        if profile == "standalone":
            (temp / "client.toml").write_text(f'''[keys]
private_key = {q(client_private)}
server_pub_key = {q(base64.b64encode(server_public).decode())}

[[servers]]
name = "ANet"
dsn = {q(f"quic://{host}:{port}")}
''')
        else:
            (temp / "admin-password").write_text(secrets.token_urlsafe(32) + "\n")
            password = secrets.token_hex(32)
            (temp / "db-password").write_text(password + "\n")
            (temp / "auth.env").write_text(
                f"DATABASE_URL=postgres://anet:{password}@db:5432/anet\n"
                f"AUTH_BACKEND_KEY={backend_key}\nJWT_SECRET={secrets.token_hex(48)}\n"
                f"DB_ENCRYPTION_KEY={secrets.token_hex(32)}\n")
        (temp / "node.json").write_text(json.dumps(expected) + "\n")
        # Bind-mounted files must also be readable under the rootless UID map.
        # Their parent stays owner-only; Compose mounts individual files.
        mounted = {"server.toml", "auth.env", "db-password", "admin-password"}
        for file in temp.iterdir():
            file.chmod(0o444 if file.name in mounted else 0o600)
        try:
            temp.rename(path)
        except OSError:
            if not (path / "node.json").is_file():
                raise
            initialize(path, host.strip("[]"), port, profile, subnet)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("profile", choices=["standalone", "webui"])
    parser.add_argument("--subnet", default="10.77.0.0/24")
    args = parser.parse_args()
    os.umask(0o077)
    initialize(args.state, args.host, args.port, args.profile, args.subnet)
