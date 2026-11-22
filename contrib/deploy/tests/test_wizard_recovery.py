from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wizard_recovery as recovery


class Recovery(unittest.TestCase):
    def setUp(self):
        self.server = dict(local=False, address="node.example", host="node.example", port=22,
                           user="deploy", password=True, status="active")
        self.plan = dict(local=0, servers=[dict(local=True, status="active", user="root", port=2222, address="first.example"), self.server],
                         profiles=[dict(entry=0, exit=1)])

    def test_password_only_in_private_session(self):
        secret = "fixture sensitive value"
        with recovery.password_environment(secret) as env:
            self.assertNotIn(secret, str(env))
            self.assertEqual(Path(env["ANET_ASKPASS_SOCKET"]).parent.stat().st_mode & 0o077, 0)
            reply = subprocess.check_output([env["SSH_ASKPASS"], "deploy@node.example's password:"], env=env)
        self.assertEqual(reply, (secret + "\n").encode())
        self.assertFalse(Path(env["ANET_ASKPASS_SOCKET"]).exists())
        with recovery.credential_session() as passwords, patch("getpass.getpass", return_value=secret) as prompt:
            recovery.collect_passwords([self.server], passwords)
            recovery.collect_passwords([self.server], passwords)
            prompt.assert_called_once()
        self.assertEqual(passwords, {})
        self.assertNotIn(secret, str(self.plan))

    def test_banner_timeout_reported_before_auth(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            ssh = shutil.which("ssh")
            if not ssh:
                self.skipTest("requires OpenSSH")
            with self.assertRaises(recovery.NodeConnectionError) as caught:
                recovery.connect(self.server, [ssh, "-o", "ConnectTimeout=1", "-p", str(port)], "127.0.0.1")
        self.assertIn("приветствие", str(caught.exception))
        self.assertFalse(caught.exception.auth_failed)

    def test_edit_cancel_can_exit_without_network_retry(self):
        ui = Mock()
        ui.menu.side_effect = [2, 0]
        apply = Mock(side_effect=recovery.NodeConnectionError(self.server, b"banner exchange"))
        edit = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp, patch("getpass.getpass", return_value="fixture"):
            saved = Path(tmp)/"pending.json"
            done = recovery.recovery_apply(self.plan, Path(tmp), saved, apply, edit, Mock(), ui)
        self.assertFalse(done)
        edit.assert_called_once_with(self.plan, saved, 1)
        apply.assert_called_once()

    def test_auth_retry_reprompts_only_failed_node(self):
        ui = Mock()
        ui.menu.return_value = 1
        apply = Mock(side_effect=[recovery.NodeConnectionError(self.server, b"Permission denied"), None])
        with tempfile.TemporaryDirectory() as tmp, patch("getpass.getpass", side_effect=["wrong", "correct"]) as prompt:
            self.assertTrue(recovery.recovery_apply(self.plan, Path(tmp), Path(tmp)/"plan", apply, Mock(), Mock(), ui))
        self.assertEqual(prompt.call_count, 2)
