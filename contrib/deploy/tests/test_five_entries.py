"""Exercise asymmetric directed observations for the five-entry/two-exit topology."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from topology import decide, observed_metrics, validate


class FiveEntries(unittest.TestCase):
    def test_partial_outage_exit_loss_total_loss_and_recovery(self):
        config = {"paths": {}, "profiles": {}}
        links = []
        for entry in range(5):
            origin = f"entry-{entry}"
            members = []
            for exit_number in range(2):
                for transport, module in (("tcp", "tcp_connect"), ("https", "https_ready")):
                    destination = f"exit-{exit_number}"
                    path = f"{origin}-{destination}-{transport}"
                    target = f"{destination}:443" if transport == "tcp" else f"https://{destination}/ready"
                    edge = dict(target_node=destination, module=module, target=target)
                    config["paths"][path] = dict(entry=origin, exit=destination, transport=transport,
                        external_ip=f"192.0.2.{exit_number+1}", probe=dict(source_node=origin, **edge))
                    members.append(path)
                    links.append(dict(source_node=origin, edge=edge, available=True,
                                      desired=1, applied=1, last_seen=100))
            config["profiles"][origin] = dict(entry=origin, paths=members, strategy="prefer-route")
        validate(config)

        def choices():
            metrics = observed_metrics(config, dict(monitoring="configured", links=links), 101)
            return [decide(config, dict(profile=name, metrics=metrics), 101)["selected"]
                    for name in config["profiles"]]

        baseline = choices()
        links[0]["available"] = False
        self.assertEqual(choices(), ["entry-0-exit-0-https", *baseline[1:]])
        for link in links:
            if link["edge"]["target_node"] == "exit-0":
                link["available"] = False
        self.assertEqual(choices(), [f"entry-{i}-exit-1-tcp" for i in range(5)])
        for link in links:
            link["available"] = False
        self.assertEqual(choices(), [None] * 5)
        for link in links:
            link["available"] = True
        self.assertEqual(choices(), baseline)
