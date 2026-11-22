from importlib.util import find_spec
from pathlib import Path
import runpy
import tempfile
import unittest

settings = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/validate-network.py"))["settings"]


class InstallerNetwork(unittest.TestCase):
    def setUp(self):
        self.net = dict(environment=dict(TUN_ADDRESS="10.77.0.2/24", TUN_SUBNET="10.77.0.0/24"),
                        ports=[dict(target=4519, protocol="udp", published="4520")])
        self.services = {"network": self.net}

    def test_legacy_and_mismatched_tunnel(self):
        self.assertEqual(settings(self.services, "standalone")[0], 4520)
        self.net["environment"]["TUN_SUBNET"] = "10.79.0.0/24"
        with self.assertRaises(ValueError):
            settings(self.services, "standalone")

    def test_full_ingress_and_uplink(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / "uplink.toml"
            self.services.update(ingress=dict(command=["wireguard"]), uplink=dict(volumes=[
                dict(target="/etc/anet/uplink.toml", source=str(cfg))]))
            with self.assertRaises(ValueError):
                settings(self.services, "anet-easy")
            self.net["ports"].append(dict(target=51820, protocol="udp", published="51111"))
            if not (find_spec("tomllib") or find_spec("tomli")):
                self.skipTest("TOML preflight requires Python 3.11 or tomli")
            cfg.write_text('[main]\ntun_name="anet-uplink"\n')
            with self.assertRaises(ValueError):
                settings(self.services, "anet-easy")
            cfg.write_text('[main]\ntun_name="anet-uplink"\nmanual_routing=true\n')
            self.assertEqual(settings(self.services, "anet-easy")[0], 4520)


if __name__ == "__main__":
    unittest.main()
