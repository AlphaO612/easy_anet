"""Check ACME issuance orchestration and scoped renewal without contacting a public CA."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wizard_https as https


class WizardHTTPS(unittest.TestCase):
    spec = dict(domain="vpn.example.test", installation="0123456789", owner="vpn",
                entries=["entry-1", "entry-2"])

    def test_foreign_sites_and_listeners(self):
        own = Path("/etc/nginx/conf.d/our.conf")
        dump = (f"# configuration file {own}:\nserver_name vpn.example.test;\n"
                "# configuration file /etc/nginx/conf.d/existing.conf:\nserver_name\n  *.example.test;\n")
        with self.assertRaisesRegex(ValueError, "другой сайт"):
            https.check_names(dump, self.spec["domain"], own)
        https.check_names(dump.replace("*.example.test", "other.test"), self.spec["domain"], own)
        with patch.object(https, "run", return_value=subprocess.CompletedProcess([], 0,
                          'LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("apache2",pid=2,fd=3))')):
            with self.assertRaisesRegex(ValueError, "порт|443"):
                https.port_preflight()

    def test_owner_copies_only_restart_selected_vless_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("entry-1", "entry-2", "entry-3"):
                target = root / name / "adapter/server"
                target.mkdir(parents=True)
                if name != "entry-2":
                    (target / "server.json").touch()
            with patch.object(https, "run", return_value=subprocess.CompletedProcess([], 0, "abc\n")) as run:
                https.write_owner_copies(root, self.spec, b"public", b"secret")
            first = root / "entry-1/adapter/server/tls.key"
            self.assertEqual(first.read_bytes(), b"secret")
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "tls/vpn.example.test/privkey.pem").read_bytes(), b"secret")
            self.assertFalse((root / "entry-2/adapter/server/tls.key").exists())
            self.assertFalse((root / "entry-3/adapter/server/tls.key").exists())
            self.assertEqual(run.call_count, 2)
            self.assertIn("label=com.docker.compose.project=anet-0123456789-entry-1", run.call_args_list[0].args)
            self.assertEqual(run.call_args_list[1].args, ("docker", "restart", "abc"))

    def test_issuance_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as mocks:
            root = Path(tmp)
            for name in ("STATE", "WWW", "NGINX", "ACME"):
                mocks.enter_context(patch.object(https, name, root / name.lower()))
            https.ACME.mkdir()
            (https.ACME / "cli.ini").write_text("server = https://local-ca/directory\n")
            mocks.enter_context(patch.object(https, "trusted_directory",
                                side_effect=lambda path, mode=0o755: path.mkdir(mode, parents=True, exist_ok=True)))
            mocks.enter_context(patch.object(https, "addresses", return_value=["192.0.2.1"]))
            mocks.enter_context(patch.object(https, "port_preflight"))
            mocks.enter_context(patch.object(https.shutil, "which", return_value="/usr/bin/test"))
            mocks.enter_context(patch.object(https.os, "umask"))
            name = https.slot(self.spec)
            lineage = https.ACME / "live" / name
            commands = []
            def run(*args, **kwargs):
                commands.append(args)
                if args[0] == "certbot":
                    cfg = Path(args[args.index("--config") + 1])
                    self.assertIn("email = admin@example.test", cfg.read_text())
                    self.assertIn("server = https://local-ca/directory", cfg.read_text())
                    self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
                    self.assertFalse(any("admin@" in arg for arg in args))
                    lineage.mkdir(parents=True, exist_ok=True)
                    (lineage / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----\n")
                    (lineage / "fullchain.pem").write_bytes(b"chain")
                    (lineage / "privkey.pem").write_bytes(b"private")
                return subprocess.CompletedProcess(args, 0, "")
            mocks.enter_context(patch.object(https, "run", side_effect=run))
            probe = mocks.enter_context(patch.object(https, "verify_site"))
            copies = mocks.enter_context(patch.object(https, "sync_copies"))
            https.install(self.spec, "admin@example.test", "studio", "")
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(probe.call_args_list[1].args[-1], b"abc")
            copies.assert_called_once_with(self.spec, b"chain", b"private")
            self.assertIn(("systemctl", "enable", "--now", "certbot.timer"), commands)
            hook = (https.ACME / "renewal-hooks/deploy" / name).read_text()
            self.assertIn(str(lineage), hook)
            self.assertIn("/usr/bin/python3 -I", hook)
            self.assertNotIn("/home/", hook)
            conf = https.NGINX / (name + ".conf")
            html = https.WWW / name / "index.html"
            previous = (conf.read_bytes(), html.read_bytes())
            probe.side_effect = [None, ValueError("wrong TLS endpoint")]
            with self.assertRaisesRegex(ValueError, "wrong TLS"):
                https.install(self.spec, "admin@example.test", "journal", "")
            self.assertEqual(previous, (conf.read_bytes(), html.read_bytes()))
            self.assertEqual(json.loads((https.STATE / name / "site.json").read_text()), self.spec)

    def test_renew_for_other_lineage_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "renew.py"
            script.with_name("site.json").write_text(json.dumps(self.spec))
            with patch.object(https, "__file__", str(script)), patch.object(https.os, "geteuid", return_value=0), \
                    patch.object(https.sys, "argv", [str(script), "--renew"]), patch.dict(https.os.environ, RENEWED_LINEAGE="/other"), \
                    patch.object(https, "validate"), patch.object(https, "apply_cert") as apply:
                https.main()
                apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
