import contextlib
import io
import os
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
import wizard
from wizard_tls import dns_name, verify_listener


class VlessWizard(unittest.TestCase):
    def test_plan_requires_explicit_consent_and_separates_endpoints(self):
        for template_input, template in ((["1"], "Студия"), (["3", "example-user"], "GitHub · example-user")):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = root / "bundle"
                bundle.mkdir()
                (bundle / "manifest.json").write_text("{}")
                (bundle / "images.tar").touch()
                answers = ["1", "1", "1", "1", "1", "192.0.2.20", "", "0", "1", "0", "vpn.example.org",
                           "", "vpn@example.org", *template_input, "198.51.100.15", "", "root", "2", ""]
                run = subprocess.run([DEPLOY / "install.sh", "--plain"], env=dict(os.environ, HOME=tmp,
                                     ANET_BUNDLE_DIR=str(bundle)), input="\n".join(answers) + "\n",
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("vpn.example.org:48000/tcp", run.stdout)
                self.assertIn("https://vpn.example.org/", run.stdout)
                self.assertIn(template, run.stdout)
                self.assertNotIn("Этап 3/6", run.stdout)
                self.assertFalse((root / ".local/state/anet-wizard/pending.json").exists())
                self.assertFalse(dns_name("192.0.2.20"))

    def test_helper_runs_privileged_with_pin_and_separate_email_input(self):
        class Host:
            node = dict(user="deploy", tls_mode="acme", tls_domain="example.org", email="private@example.org",
                        template="github", github="team")
            calls = []

            def run(self, script, **options):
                self.calls.append((script, options))

        host = Host()
        with contextlib.redirect_stderr(io.StringIO()):
            wizard.setup_vless(host, "install", "123456789a", ["entry-1", "entry-3"])
        interactive = [cmd for cmd, opt in host.calls if opt.get("tty")]
        self.assertEqual(len(interactive), 1)
        args = shlex.split(interactive[0])
        self.assertEqual(args[:3], ["sudo", "sh", "-ec"])
        subprocess.run(["sh", "-n"], input=args[3], text=True, check=True)
        self.assertIn('sha256sum -c --status', args[3])
        self.assertIn('--entry entry-1 --entry entry-3', args[3])
        self.assertNotIn("private@example.org", str([cmd for cmd, _ in host.calls]))
        self.assertEqual(sum(opt.get("input") == b"private@example.org\n" for _, opt in host.calls), 1)

    def test_real_handshake_verifies_sni_and_rejects_wrong_name(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            cert, key = Path(tmp) / "fullchain.pem", Path(tmp) / "privkey.pem"
            subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "ec", "-pkeyopt",
                            "ec_paramgen_curve:P-256", "-days", "1", "-subj", "/CN=vpn.example.org",
                            "-addext", "subjectAltName=DNS:vpn.example.org", "-out", cert, "-keyout", key],
                           capture_output=True, check=True)
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(cert, key)
            names = []
            server.set_servername_callback(lambda sock, name, ctx: names.append(name))
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)

            def serve():
                for _ in range(2):
                    with listener.accept()[0] as stream:
                        try:
                            server.wrap_socket(stream, server_side=True).close()
                        except ssl.SSLError:
                            pass

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                port = listener.getsockname()[1]
                verify_listener(port, "vpn.example.org", cert, public_trust=False)
                with self.assertRaises(ssl.SSLCertVerificationError):
                    verify_listener(port, "wrong.example.org", cert, public_trust=False)
            finally:
                thread.join(timeout=6)
            self.assertEqual(names, ["vpn.example.org", "wrong.example.org"])


if __name__ == "__main__":
    unittest.main()
