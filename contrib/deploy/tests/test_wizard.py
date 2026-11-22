import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wizard
import wizard_node


class WizardTests(unittest.TestCase):
    def test_current_exit_and_multiple_inputs(self):
        answers = ["", "", "", "2", "entry.test", "", "deploy", "2", "1", "0", "2", "exit.test", ""]
        with patch("builtins.input", side_effect=answers):
            p = wizard.plan()
        self.assertEqual(1, p["local"])
        self.assertEqual(["wireguard", "anet", "anet"], [x["protocol"] for x in p["profiles"]])
        self.assertTrue(all(x["entry"] == 0 and x["exit"] == 1 for x in p["profiles"]))
        self.assertTrue(p["servers"][0]["password"])

    def test_worker_secrets_use_stdin(self):
        node = dict(user="deploy", host="entry.test", port=22, address="entry.test", local=False, password=True)
        host = wizard.Host(node, "/tmp/mock-socket")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"{}")) as run:
            wizard.job(host, "runtime", "123", action="entry", uplink="private-content")
        args, kwargs = run.call_args
        self.assertNotIn("private-content", str(args))
        self.assertIn(b"private-content", kwargs["input"])

    def test_catalog_and_runtime_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = "[Interface]\nPrivateKey = test\n"
            row = dict(id="profile-2", protocol="wireguard", entry="a", exit="b", port=48001, config=config)
            catalog = wizard_node.publish_catalog(root, [row])
            self.assertEqual(config, (catalog / "files/profile-2.conf").read_text())
            self.assertNotIn("config", json.loads((catalog / "index.json").read_text())[0])
            wizard.sources(root / "runtime.tar.gz")
            with tarfile.open(root / "runtime.tar.gz") as archive:
                self.assertIn("deploy/scripts/release.py", archive.getnames())
                self.assertNotIn("profiles/files/profile-2.conf", archive.getnames())


if __name__ == "__main__":
    unittest.main()
