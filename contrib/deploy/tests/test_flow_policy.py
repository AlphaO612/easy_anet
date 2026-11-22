import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
FLOW = runpy.run_path(str(DEPLOY / "scripts/flow-router.py"))


class Policy(unittest.TestCase):
    def setUp(self):
        path = dict(entry="a", exit="b", external_ip="192.0.2.1", transport="quic")
        self.profile = dict(entry="a", strategy="prefer-route", user="alice", paths=["p"], require_domain=True,
                            rules=[dict(id="domain", domain="example.org", allow=["p"])])
        self.topology = dict(paths={"p": path}, profiles={"home": self.profile})
        self.binding = dict(profile="home", user="alice", source="10.77.0.0/24", dns_listen="10.77.0.2",
                            dns_upstream="192.0.2.53", dns_path="p",
                            paths={"p": dict(table=1234, interface="anet-up1")})
        self.metrics = {"p": dict(up=True, expires=200)}

    def render(self):
        return FLOW["render"](self.topology, self.binding, self.metrics, 100, {})[0]

    def test_revoked_owner_invalidates_pinned_marks_and_dns_access(self):
        self.binding["user"] = "bob"
        batch = self.render()
        self.assertNotIn("1234 timeout", batch)
        self.assertNotIn("@dns_listener", batch)

    def test_subsecond_observation_cannot_select_an_unleased_path(self):
        self.metrics["p"]["expires"] = 100.5
        batch, decisions = FLOW["render"](self.topology, self.binding, self.metrics, 100, {})
        self.assertIsNone(decisions["(default)"]["selected"])
        self.assertNotIn("ct mark set 1234", batch)

    def test_expiring_dns_and_ambiguity(self):
        self.binding["dns_answers"] = [
            dict(address="192.0.2.8", domain="example.org", expires=102),
            dict(address="192.0.2.9", domain="example.org", expires=102),
            dict(address="192.0.2.9", domain="other.example", expires=120)]
        batch = self.render()
        self.assertEqual(batch.count("192.0.2.8 timeout 2s"), 2)
        self.assertNotIn("192.0.2.9", batch)
        self.assertIn("ip daddr != @known_dns drop", batch)
        self.assertIn('ip daddr @dns_listener fib daddr type local meta l4proto { tcp, udp } th dport 53 accept', batch)
        self.binding["dns_listen"] = "127.0.0.1"
        with self.assertRaises(ValueError):
            self.render()

    def test_dns_output_is_bound_to_one_live_egress(self):
        self.binding["dns_path"] = "missing"
        with self.assertRaises(ValueError):
            self.render()
        self.binding["dns_path"] = "p"
        batch = self.render()
        self.assertIn("ip daddr 192.0.2.53 meta l4proto { tcp, udp } th dport 53 meta mark 1234 meta mark @live accept", batch)
        self.assertIn('meta mark 1234 oifname != "anet-up1" drop', batch)
        self.assertIn("meta skgid 53153 ct direction original", batch)
        self.metrics.clear()
        self.assertNotIn("@dns_listener", self.render())

    def test_feed_is_reloaded_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "observations"
            self.binding["dns_answers_file"] = str(feed)
            record = dict(address="192.0.2.10", domain="example.org", expires=102)
            feed.write_text(json.dumps([record]))
            FLOW["load_dns_answers"](self.binding)
            self.assertEqual(self.binding["dns_answers"], [record])
            feed.write_text("truncated")
            with contextlib.redirect_stderr(io.StringIO()):
                FLOW["load_dns_answers"](self.binding)
            self.assertEqual(self.binding["dns_answers"], [])

    def test_controller_restart_keeps_namespace_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            def execute(shell, option, script, **kwargs):
                return subprocess.run([shell, option, script.replace("/run/anet-flow", tmp)],
                                      input=kwargs["stdin"], text=True, capture_output=True)
            FLOW["pin_binding"](execute, self.binding, self.topology)
            FLOW["pin_binding"](execute, self.binding, self.topology)
            self.binding["paths"]["p"]["table"] += 1
            with self.assertRaises(ValueError):
                FLOW["pin_binding"](execute, self.binding, self.topology)


if __name__ == "__main__":
    unittest.main()
