import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release
import topology


class ProfileCompatibility(unittest.TestCase):
    def test_issue_and_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def save(name, value):
                (root / name).write_text(json.dumps(value))
            edge = dict(source_node="entry", target_node="exit", module="tcp_connect", target="exit:443")
            config = dict(paths={"route":dict(entry="entry",exit="exit",transport="wg",
                                              external_ip="192.0.2.1",probe=edge)},
                          profiles={"alice":dict(entry="entry",strategy="prefer-route",paths=["route"])})
            save("topology", config)
            save("session", dict(profile="alice"))
            save("keys", {"alice":{"route":"provisioned"}})
            (root/"provisioned").write_text("profile bytes")
            (root/"token").write_text("test")
            manifest = dict(format=1, server_commit="a"*40,
                            verified_clients=[dict(client="wg-ios-1",config_schema="wg-1",transport="wg")])
            save("release", manifest)
            links = dict(monitoring="configured", links=[dict(source_node="entry",
                         edge={k:v for k,v in edge.items() if k!="source_node"},
                         applied=1,desired=1,last_seen=99,available=True)])
            argv = ["topology", str(root/"topology"),"issue",str(root/"session"),str(root/"keys"),
                    str(root/"output"),"--release",str(root/"release"),"--history",str(root/"ledger"),
                    "--client","wg-ios-1","--config-schema","wg-1","--api","https://example.test/api/v1",
                    "--token-file",str(root/"token")]
            def issue():
                with patch.object(sys, "argv", argv), patch.object(topology.time, "time", return_value=100), \
                     patch.object(topology, "api_read", side_effect=[dict(manifest=json.dumps(config)), links]), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    topology.main()
            issue()
            self.assertEqual((root/"output").read_text(),"profile bytes")
            self.assertTrue(release.upgrade_report(manifest,root/"ledger")[0]["supported"])
            manifest["verified_clients"] = []
            save("release", manifest)
            (root/"output").unlink()
            with self.assertRaises(SystemExit):
                issue()
            self.assertFalse((root/"output").exists())
            self.assertFalse(release.upgrade_report(manifest,root/"ledger")[0]["supported"])


if __name__ == "__main__":
    unittest.main()
