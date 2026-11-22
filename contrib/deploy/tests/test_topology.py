import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from topology import decide, observed_metrics, validate


class Selection(unittest.TestCase):
    def setUp(self):
        self.profile = dict(entry="a", paths=["tcp", "udp", "other"], strategy="prefer-route")
        self.config = dict(profiles={"alice": self.profile}, paths={
            name: dict(entry="a", exit=exit_name, transport=name, external_ip=ip)
            for name, exit_name, ip in (("tcp", "b", "192.0.2.1"), ("udp", "b", "192.0.2.1"),
                                        ("other", "c", "192.0.2.2"))
        })
        self.session = dict(profile="alice", previous="tcp", selected_at=0, metrics={
            name: dict(up=True, expires=200, mbps=speed, latency_ms=50, loss=0,
                       history=[dict(at=t, mbps=speed) for t in (80, 90, 99)])
            for name, speed in (("tcp", 10), ("udp", 1), ("other", 50))
        })
        validate(self.config)

    def select(self, strategy):
        self.profile["strategy"] = strategy
        return decide(self.config, self.session, 100)["selected"]

    def test_fallback(self):
        self.session["metrics"]["tcp"]["up"] = False
        self.profile["external_ip"] = "192.0.2.1"
        for strategy, expected in (("strict-ip", "udp"), ("prefer-route", "udp"),
                                   ("speed", "other"), ("auto", "other")):
            self.assertEqual(self.select(strategy), expected)
        self.session["metrics"]["udp"]["expires"] = 99
        self.assertIsNone(self.select("strict-ip"))
        self.assertEqual(self.select("prefer-route"), "other")

    def test_health_binding(self):
        binding = dict(source_node="a", target_node="b", module="tcp_connect", target="b:443")
        self.config["paths"]["tcp"]["probe"] = binding
        link = dict(source_node="a", edge={k: v for k, v in binding.items() if k != "source_node"},
                    desired=2, applied=2, available=True, last_seen=99)
        snapshot = dict(monitoring="configured", links=[link])
        self.assertEqual(set(observed_metrics(self.config, snapshot, 100)), {"tcp"})
        self.assertFalse(observed_metrics(self.config, snapshot, 145))
        link["applied"] = 1
        self.assertFalse(observed_metrics(self.config, snapshot, 100))

    def test_cycle_through_alternate_exit(self):
        self.config["paths"]["back"] = dict(entry="b", exit="a", transport="quic", external_ip="192.0.2.4")
        with self.assertRaisesRegex(ValueError, "cyclic route"):
            validate(self.config)
        self.config["paths"]["back"]["exit"] = "b"
        validate(self.config)

    def test_probe_cannot_mark_another_exit_healthy(self):
        self.config["paths"]["tcp"]["probe"] = dict(source_node="a", target_node="c",
                                                   module="tcp_connect", target="c:443")
        with self.assertRaisesRegex(ValueError, "probe endpoints"):
            validate(self.config)

    def test_hysteresis(self):
        self.assertEqual(self.select("speed"), "other")
        self.session["metrics"]["other"]["history"].pop()
        self.assertEqual(self.select("speed"), "tcp")

    def test_profile_owner(self):
        self.profile["user"] = "alice"
        self.assertIsNone(self.select("auto"))
        self.session["user"] = "alice"
        self.assertIsNotNone(self.select("auto"))

    def test_domain(self):
        self.profile.update(require_domain=True, rules=[dict(id="deny", domain="deny.test", allow=[])])
        self.session.update(domains=["deny.test"], dns_expires=101)
        self.assertIsNone(self.select("auto"))
        self.session.update(domains=["a.test", "b.test"])
        self.assertIsNone(self.select("auto"))
        self.session.update(domains=["a.test"], dns_expires=100)
        self.assertIsNone(self.select("auto"))


if __name__ == "__main__":
    unittest.main()
