"""Check per-device accounting across samples, report retries and Xray restarts."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
import vless_meter

spec = importlib.util.spec_from_file_location("profiles", DEPLOY / "images/profile-service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class Usage(unittest.TestCase):
    def test_repeated_samples_restart_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "a" * 43
            meter = {"token_hash": service.digest(token), "catalog": "fixture", "starter_hash": service.digest("original")}
            route = {"usage": meter, "devices": ["phone", "laptop"]}
            index = root / "catalog.json"
            index.write_text(json.dumps({"routes": {"profile-1": route}}))
            with patch.object(service, "CATALOG", index), patch.object(service, "DB", root / "db"):
                service.init()
                with self.assertRaises(service.Problem):
                    service.report_route("profile-1", "b" * 43)
                with self.assertRaises(service.Problem):
                    service.report_route("profile-2", token)
                verified = service.report_route("profile-1", token)
                state = {"catalog": meter["catalog"], "source": "1" * 32}
                with closing(service.connect()) as db, db:
                    self.assertIsNone(service.traffic_for(db, route, service.digest("phone")))
                    for generation, counters in [("pid-1", {"0": [10, 20], "starter": [3, 7]}),
                                                  ("pid-1", {"0": [20, 30]}), ("pid-1", {"0": [20, 30]}),
                                                  ("pid-2", {"0": [30, 40], "1": [5, 6]})]:
                        vless_meter.accumulate(state, generation, counters)
                        service.record_usage(db, verified, state)
                    service.record_usage(db, verified, state)
                    stale = dict(state, totals={"0": [1, 1]})
                    service.record_usage(db, verified, stale)
                    for key, expected in [("phone", (50, 70)), ("laptop", (5, 6)), ("original", (3, 7))]:
                        row = service.traffic_for(db, route, service.digest(key))
                        self.assertEqual((row["upload"], row["download"]), expected)
                    with self.assertRaises(service.Problem):
                        service.record_usage(db, verified, dict(state, catalog="old-catalog"))
                    with self.assertRaises(service.Problem):
                        service.record_usage(db, verified, dict(state, totals={"128": [0, 0]}))

    def test_config_uses_only_private_socket_and_scoped_api(self):
        clients = [{"id": "primary"}]
        config = {"inbounds": [{"protocol": "vless", "settings": {"clients": clients}}]}
        vless_meter.configure(config, clients)
        first = json.dumps(config)
        vless_meter.configure(config, clients)
        self.assertEqual(json.dumps(config), first)
        self.assertEqual(config["api"]["services"], ["StatsService", "HandlerService"])
        self.assertEqual(config["inbounds"][0]["tag"], "vless-in")
        self.assertEqual(config["inbounds"][1]["listen"], "/anet-usage/stats.sock")
        self.assertNotIn("port", config["inbounds"][1])
        self.assertEqual(clients[0]["email"], vless_meter.STARTER)


if __name__ == "__main__":
    unittest.main()
