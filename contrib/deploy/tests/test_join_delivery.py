"""Run the release join helper against a fixture ZIP and a local authenticated TLS endpoint."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import tarfile
import tempfile
from threading import Thread
import unittest
import zipfile

HELPER = Path(__file__).resolve().parents[1] / "distribution/join-node.sh"


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/api/v1/node-healthz" else 404)
        self.end_headers()
        self.wfile.write(b"ANet node API")

    def log_message(self, *args):
        pass


@unittest.skipUnless(Path("/dev/net/tun").is_char_device(), "requires a Linux TUN device")
class JoinDelivery(unittest.TestCase):
    def test_checked_zip_hidden_invitation_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                            "-keyout", root / "tls.key", "-out", root / "tls.pem"], check=True, capture_output=True)
            server = HTTPServer(("127.0.0.1", 0), Health)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(root / "tls.pem", root / "tls.key")
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            scripts = {
                "contrib/deploy/scripts/rootless-docker.sh": b"exit 0\n",
                "contrib/deploy/install.sh": b"""set -eu
test "$VPN_PORT" = 4530
test "$1 $2" = '--profile standalone'
test "$5 $6" = '--host node.example'
test "$9" = '--join-panel'
python3 - "${10}" <<'CHECK'
import json,os,stat,sys
from pathlib import Path
join=Path(sys.argv[1]); data=json.loads(join.read_text()); invite=Path(data['invitation_file'])
assert stat.S_IMODE(invite.stat().st_mode)==0o600 and invite.read_text()=='x'*43
Path(os.environ['FIXTURE']+'/joined').write_text(str(join.parent))
CHECK
""",
            }
            with tarfile.open(root / "source.tar.gz", "w:gz") as package:
                for name, contents in scripts.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(contents)
                    package.addfile(entry, io.BytesIO(contents))
            archive = root / "anet-easy-linux-amd64.zip"
            with zipfile.ZipFile(archive, "w") as zipper:
                zipper.write(root / "source.tar.gz", "anet-easy/source.tar.gz")
                for name in ("images.tar", "manifest.json"):
                    zipper.writestr("anet-easy/" + name, b"fixture")
            (root / "SHA256SUMS").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
            for name, body in {
                    "curl": 'until [ "$1" = -o ]; do shift; done\ncp "$FIXTURE/${4##*/}" "$2"',
                    "id": 'echo 1000',
            }.items():
                (root / name).write_text("#!/bin/sh\n" + body)
                (root / name).chmod(0o700)
            env = dict(os.environ, PATH=str(root)+":"+os.environ["PATH"], FIXTURE=tmp,
                       XDG_CACHE_HOME=str(root / "cache"), SSL_CERT_FILE=str(root / "tls.pem"))
            payload = dict(host="node.example", port=4530, api=f"https://localhost:{server.server_port}/api/v1",
                           node_id="bf807d7b-4186-4a3d-a2cb-385fdc51d795", token="x"*43)
            code = base64.b64encode(json.dumps(payload).encode()).decode()
            for attempt in range(2):
                proc = subprocess.run(["bash", HELPER, "--stdin"], input=code+"\n", text=True,
                                      env=env, capture_output=True, timeout=20)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(Path((root / "joined").read_text()).exists())
                self.assertNotIn(code, proc.stdout + proc.stderr)
                self.assertEqual("Скачиваю ZIP" in proc.stdout, attempt == 0)
