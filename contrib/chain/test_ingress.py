import base64
import configparser
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


ISSUER = Path(__file__).with_name("issue-ingress.py")


def public(private):
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(private)).public_key()
    return base64.b64encode(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


class IngressProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "profile"

    def issue(self, protocol, host="gateway.example", port="9443"):
        return subprocess.run([sys.executable, str(ISSUER), protocol, str(self.profile), "--host",
                               host, "--port", port, "--dns", "1.1.1.1"], capture_output=True)

    def test_wireguard_pair_and_private_files(self):
        result = self.issue("wireguard")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")
        client = configparser.ConfigParser()
        client.read(self.profile / "client.conf")
        server = configparser.ConfigParser()
        server.read(self.profile / "server/wg.conf")
        for a, b in ((client, server), (server, client)):
            self.assertEqual(public(a["Interface"]["PrivateKey"]), b["Peer"]["PublicKey"])
        self.assertEqual(server["Peer"]["AllowedIPs"], client["Interface"]["Address"])
        for path in [self.profile, *self.profile.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_vless_tls_import_and_no_overwrite(self):
        self.assertEqual(self.issue("vless").returncode, 0)
        profile = (self.profile / "client.uri").read_text()
        uri = urlsplit(profile.strip())
        server = json.loads((self.profile / "server/server.json").read_text())["inbounds"][0]
        self.assertEqual(uri.username, server["settings"]["clients"][0]["id"])
        self.assertEqual(uri.port, 9443)
        self.assertEqual(parse_qs(uri.query)["security"], [server["streamSettings"]["security"]])
        self.assertEqual(parse_qs(uri.query)["sni"], [uri.hostname])
        self.assertNotEqual(self.issue("vless").returncode, 0)
        self.assertEqual((self.profile / "client.uri").read_text(), profile)

    def test_invalid_input_creates_no_secrets(self):
        for host, port in (("bad\nendpoint", "443"), ("vpn.example", "65536")):
            self.assertNotEqual(self.issue("wireguard", host, port).returncode, 0)
            self.assertFalse(self.profile.exists())


if __name__ == "__main__":
    unittest.main()
