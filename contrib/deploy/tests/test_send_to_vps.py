"""Exercise the standalone key helpers with disposable local paths."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


DIST = Path(__file__).resolve().parents[1] / "distribution"


class KeyDelivery(unittest.TestCase):
    def test_standalone_assets_ship_the_same_authorization_script(self):
        shell = (DIST / "send-to-vps.sh").read_text().split("tr -d '\\r\\n'\n", 1)[1].split("\nAUTHORIZED_KEY\n", 1)[0]
        windows = (DIST / "send-to-vps.ps1").read_text(encoding="utf-8-sig").split("$authorize = @'\n", 1)[1].split("\n'@", 1)[0]
        self.assertEqual(shell, windows)

    def test_key_only_uses_no_bundle_and_fallback_preserves_install_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "send-to-vps.sh"
            shutil.copy(DIST / helper.name, helper)
            commands = root / "bin"
            commands.mkdir()
            for name, body in {
                "ssh-keygen": 'printf "ssh-ed25519 %068d test\\n" 1',
                "ssh": 'printf "%s\\n" "$*" >>"$TEST_CALLS"; if [ "$1" = -F ]; then exit "$VERIFY_RESULT"; fi',
                "scp": 'echo "scp $*" >>"$TEST_CALLS"',
            }.items():
                command = commands / name
                command.write_text("#!/bin/sh\n" + body + "\n")
                command.chmod(0o700)
            calls = root / "calls"
            def launch(key_only, result):
                calls.write_text("")
                return subprocess.run(["sh", str(helper)] + (["--key-only"] if key_only else []),
                                      text=True, capture_output=True, input="test.example\ndeploy\n2200\n",
                                      env={**os.environ, "HOME": str(root), "PATH": f"{commands}:{os.environ['PATH']}",
                                           "TEST_CALLS": str(calls), "VERIFY_RESULT": str(result)})
            check = launch(True, 0)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn("ssh -i", check.stdout)
            self.assertIn("sudo -H /bin/sh", calls.read_text())
            self.assertIn("PreferredAuthentications=publickey", calls.read_text())
            self.assertNotIn("scp ", calls.read_text())
            self.assertNotEqual(launch(True, 1).returncode, 0)
            for file in ("install.sh", "manifest.json", "images.tar", "source.tar.gz"):
                (root / file).write_text("fixture")
            normal = launch(False, 1)
            self.assertEqual(normal.returncode, 0, normal.stderr)
            self.assertIn("scp ", calls.read_text())
            self.assertIn("deploy@test.example cd anet-install.", calls.read_text())
