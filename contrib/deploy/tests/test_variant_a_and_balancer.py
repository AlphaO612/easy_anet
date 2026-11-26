"""Tests for Variant A live sync, nginx/compose hardening, profile-service DELETEs, and multi-exit balancer."""
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

CONTRIB = Path(__file__).resolve().parents[2]
DEPLOY = CONTRIB / "deploy"
sys.path.insert(0, str(DEPLOY / "scripts"))

import topology
import vless_meter
import wg_sync
import wizard
import wizard_https
import wizard_node

FLOW = runpy.run_path(str(DEPLOY / "scripts/flow-router.py"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


profile_service = load_module("profile_service_variant_a", DEPLOY / "images/profile-service.py")
setup_node_api = load_module("setup_node_api_variant_a", DEPLOY / "distribution/setup-node-api.py")
issue_ingress = load_module("issue_ingress_variant_a", CONTRIB / "chain/issue-ingress.py")


class ConfigAndIngressTests(unittest.TestCase):
    def test_nginx_conf_dynamic_vless_usage_and_variant_a_routes(self):
        conf = (DEPLOY / "images/nginx.conf").read_text()
        self.assertIn("map $http_x_forwarded_proto $forwarded_proto", conf)
        self.assertIn("proxy_set_header X-Forwarded-Proto $forwarded_proto;", conf)
        self.assertIn("location ^~ /api/v1/subscriptions/", conf)
        self.assertIn("location ~ ^/api/v1/ingress-sync/[A-Za-z0-9_-]+$", conf)
        self.assertIn("client_max_body_size 64k;", conf)
        # Ensure vless-usage uses dynamic resolver, not static proxy_pass
        usage_block = conf.split("location ~ ^/api/v1/vless-usage/profile-[1-9][0-9]*$")[1].split("\n        }")[0]
        self.assertIn("resolver 127.0.0.11 valid=10s;", usage_block)
        self.assertIn("set $profile_backend http://vless-profiles:8093;", usage_block)
        self.assertIn("proxy_pass $profile_backend$request_uri;", usage_block)

    def test_webui_yaml_passes_catalog_env_to_auth(self):
        compose = (DEPLOY / "compose/webui.yaml").read_text()
        self.assertIn("ANET_VLESS_CATALOG_PATH: /srv/anet-vless-catalog/index.json", compose)
        self.assertIn("ANET_SUBSCRIPTION_ORIGIN_PATH: /srv/anet-vless-catalog/subscription-origin", compose)
        self.assertIn("ANET_STARTER_PROFILES_PATH: /srv/anet-profiles/index.json", compose)

    def test_wizard_https_proxies_all_variant_a_endpoints(self):
        spec = dict(domain="vpn.example.test", installation="0123456789", entries=["entry-1"],
                    owner="anet", subscription_port=18088)
        conf = wizard_https.nginx_site(spec, True).decode()
        self.assertIn('location ~ "^/api/v1/vless-subscriptions/[A-Za-z0-9_-]{43}$"', conf)
        self.assertIn('location ~ "^/api/v1/subscriptions/[A-Za-z0-9_-]{32,128}$"', conf)
        self.assertIn('location ~ "^/api/v1/vless-usage/profile-[1-9][0-9]*$"', conf)
        self.assertIn('location ~ "^/api/v1/ingress-sync/[A-Za-z0-9_-]+$"', conf)
        self.assertIn("client_max_body_size 32k;", conf)
        self.assertIn("client_max_body_size 64k;", conf)
        self.assertNotIn("vless-devices", conf)

    def test_setup_node_api_routes_and_origin_persistence(self):
        conf = setup_node_api.config("nodes.example.test", 8088, True)
        self.assertIn("vless-subscriptions/[A-Za-z0-9_-]+$", conf)
        self.assertIn("subscriptions/[A-Za-z0-9_-]+$", conf)
        self.assertIn("vless-usage/profile-[1-9][0-9]*$", conf)
        self.assertIn("ingress-sync/[A-Za-z0-9_-]+$", conf)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_dir = root / "public-api"
            catalog = root / "vless-catalog"
            catalog.mkdir()
            (catalog / "index.json").write_text("{}")
            written = setup_node_api.persist_origin(
                "nodes.example.test", 8088, public_api_dir=api_dir, catalog_dirs=[catalog]
            )
            self.assertEqual((api_dir / "8088.origin").read_text(), "https://nodes.example.test\n")
            self.assertEqual((catalog / "subscription-origin").read_text(), "https://nodes.example.test\n")
            self.assertEqual(len(written), 2)

    def test_ingress_sh_wg_24_and_issue_ingress_vless_tag(self):
        script = (CONTRIB / "chain/ingress.sh").read_text()
        self.assertIn("-s 10.79.0.0/24 -o \"$output\" -j ACCEPT", script)
        self.assertIn("-s 10.79.0.0/24 -o \"$output\" -j MASQUERADE", script)
        self.assertIn("ip addr add 10.79.0.1/24 dev wg-profile", script)
        self.assertIn("chmod 0600 /run/server.json", script)
        self.assertIn("chmod 0400 /run/tls.pem /run/tls.key", script)
        vless_files = issue_ingress.vless("vpn.example.test", 443, "1.1.1.1")
        cfg = json.loads(vless_files["server/server.json"])
        self.assertEqual(cfg["inbounds"][0]["tag"], "vless-in")

    def test_wizard_sources_bundles_wg_sync_and_flow_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "runtime.tar.gz"
            wizard.sources(archive_path)
            with tarfile.open(archive_path) as tar:
                names = set(tar.getnames())
            for expected in (
                "deploy/scripts/wg_sync.py",
                "deploy/scripts/flow-router.py",
                "deploy/scripts/flow-agent.py",
                "deploy/scripts/flow-compose.py",
                "deploy/scripts/flow_adapter.py",
                "deploy/scripts/flow_node.py",
                "deploy/scripts/managed-flow.py",
            ):
                self.assertIn(expected, names)


class ProfileServiceHardeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.ids = [str(uuid.uuid4()) for _ in range(4)]
        row = dict(id="profile-1", devices=self.ids, entry="vless.example.org", exit="exit", port=48001)
        catalog = wizard_node.publish_device_catalog(root, [row], "https://vless.example.org")
        self.db_path = root / "state.sqlite"
        with mock.patch.object(profile_service, "DB", self.db_path):
            profile_service.init()
        self.user_id = str(uuid.uuid4())
        self.patches = [
            mock.patch.object(profile_service, "DB", self.db_path),
            mock.patch.object(profile_service, "CATALOG", catalog / "index.json"),
            mock.patch.object(profile_service, "account_exists", return_value=True),
            mock.patch.object(profile_service, "authenticate", return_value=None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        server = profile_service.ThreadingHTTPServer(("127.0.0.1", 0), profile_service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer admin"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        with urlopen(Request(self.origin + path, data=data, headers=headers, method=method), timeout=5) as res:
            return json.loads(res.read())

    def test_wal_and_device_and_group_deletion_frees_slot(self):
        with contextlib.closing(profile_service.connect()) as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            fk = db.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(fk, 1)

        group = self.req(profile_service.ADMIN, "POST", {"user_id": self.user_id})["id"]
        rows = self.req(f"{profile_service.ADMIN}/{group}/profiles", "POST",
                        {"name": "Мой Телефон", "route": "profile-1"})
        first_uri = rows[0]["uri"]
        self.req(f"{profile_service.ADMIN}/{group}/profiles", "POST",
                 {"name": "Ноутбук", "route": "profile-1"})

        # Delete via path with URL-encoded UTF-8 name
        after_del = self.req(
            f"{profile_service.ADMIN}/{group}/profiles/{quote('Мой Телефон', safe='')}?route=profile-1",
            "DELETE",
        )
        self.assertEqual([r["name"] for r in after_del], ["Ноутбук"])

        # Re-issue reuses freed credential slot
        reissued = self.req(f"{profile_service.ADMIN}/{group}/profiles", "POST",
                            {"name": "Планшет", "route": "profile-1"})
        tablet = next(r for r in reissued if r["name"] == "Планшет")
        self.assertEqual(tablet["uri"].split("#")[0], first_uri.split("#")[0])

        # Delete via JSON body on /profiles
        after_json_del = self.req(
            f"{profile_service.ADMIN}/{group}/profiles", "DELETE",
            {"name": "Ноутбук", "route": "profile-1"},
        )
        self.assertEqual([r["name"] for r in after_json_del], ["Планшет"])

        # Delete entire group cascades devices and links
        deleted = self.req(f"{profile_service.ADMIN}/{group}", "DELETE")
        self.assertEqual(deleted["deleted"], group)
        self.assertEqual(self.req(profile_service.ADMIN)["groups"], [])
        with contextlib.closing(profile_service.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM account_links").fetchone()[0], 0)

    def test_sanitize_path_redacts_subscription_token(self):
        token = "a" * 43
        sanitized = profile_service.sanitize_path(profile_service.PUBLIC + token)
        self.assertNotIn(token, sanitized)
        self.assertEqual(sanitized, profile_service.PUBLIC + "<redacted>")

    def test_account_exists_prefers_internal_status_with_qr_fallback(self):
        # Stop the class-level mock for account_exists
        class FakeResp:
            def __init__(self, status, body=b""):
                self.status = status
                self._body = body
            def read(self, *_):
                return self._body

        calls = []
        class FakeConn:
            def __init__(self, *args, **kwargs):
                self._path = None
            def request(self, method, path, headers=None):
                calls.append((method, path, headers))
                self._path = path
            def getresponse(self):
                if "/internal/users/" in self._path:
                    return FakeResp(404)
                return FakeResp(200)
            def close(self):
                pass

        with mock.patch.object(profile_service.http.client, "HTTPConnection", FakeConn):
            # Test real function directly (bypassing setUp patch by calling original? Use reload or direct check)
            orig = load_module("profile_service_direct", DEPLOY / "images/profile-service.py")
            with mock.patch.object(orig.http.client, "HTTPConnection", FakeConn):
                self.assertTrue(orig.account_exists("u-1"))
        self.assertEqual(len(calls), 2)
        self.assertIn("/api/v1/internal/users/u-1/status", calls[0][1])
        self.assertIn("/api/v1/config/qr/u-1", calls[1][1])


class LiveSyncTests(unittest.TestCase):
    def test_vless_live_sync_adu_and_rmu(self):
        with tempfile.TemporaryDirectory() as tmp:
            server_json = Path(tmp) / "server.json"
            starter_id = str(uuid.uuid4())
            old_id = str(uuid.uuid4())
            keep_id = str(uuid.uuid4())
            new_id = str(uuid.uuid4())
            cfg = {"inbounds": [{"protocol": "vless", "settings": {"clients": [
                {"id": starter_id, "email": vless_meter.STARTER},
                {"id": old_id, "email": "old@anet.invalid"},
                {"id": keep_id, "email": "keep@anet.invalid"},
            ]}}]}
            vless_meter.configure(cfg, cfg["inbounds"][0]["settings"]["clients"])
            server_json.write_text(json.dumps(cfg))

            calls = []
            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs.get("input")))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(vless_meter, "_find_ingress_container", return_value="cid-123"), \
                 mock.patch.object(vless_meter.subprocess, "run", side_effect=fake_run):
                diff = vless_meter.sync_xray_clients("proj", server_json, [
                    {"uuid": keep_id, "email": "keep@anet.invalid"},
                    {"uuid": new_id, "email": "new@anet.invalid"},
                ])

            self.assertEqual(diff["added"], [new_id])
            self.assertEqual(diff["removed"], [old_id])
            self.assertEqual(len(calls), 2)
            self.assertIn("rmu", calls[0][0])
            self.assertIn("old@anet.invalid", calls[0][0])
            self.assertIn("adu", calls[1][0])
            adu_payload = json.loads(calls[1][1])
            self.assertEqual(adu_payload["inbounds"][0]["tag"], "vless-in")
            self.assertEqual(adu_payload["inbounds"][0]["settings"]["clients"][0]["id"], new_id)

            persisted = json.loads(server_json.read_text())
            ids = {c["id"] for c in persisted["inbounds"][0]["settings"]["clients"]}
            self.assertEqual(ids, {starter_id, keep_id, new_id})

    def test_wg_sync_transfer_and_live_peers(self):
        def make_key(byte_val):
            return base64.b64encode(bytes([byte_val]) * 32).decode("ascii")

        starter_pub = make_key(1)
        old_pub = make_key(2)
        new_pub = make_key(3)
        transfer = f"{starter_pub}\t100\t200\n{old_pub}\t50\t75\n"
        parsed = wg_sync.parse_transfer(transfer)
        self.assertEqual(parsed, {starter_pub: [100, 200], old_pub: [50, 75]})

        state = {}
        wg_sync.accumulate(state, "gen-1", {starter_pub: [100, 200]})
        wg_sync.accumulate(state, "gen-1", {starter_pub: [150, 260]})
        wg_sync.accumulate(state, "gen-2", {starter_pub: [40, 10]})
        self.assertEqual(state["totals"][starter_pub], [190, 270])

        with tempfile.TemporaryDirectory() as tmp:
            wg_conf = Path(tmp) / "wg.conf"
            wg_conf.write_text(
                f"[Interface]\nPrivateKey = {make_key(9)}\nListenPort = 51820\n\n"
                f"[Peer]\nPublicKey = {starter_pub}\nAllowedIPs = 10.79.0.2/32\n\n"
                f"[Peer]\nPublicKey = {old_pub}\nAllowedIPs = 10.79.0.3/32\n"
            )
            calls = []
            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(wg_sync, "_find_ingress_container", return_value="wg-cid"), \
                 mock.patch.object(wg_sync.subprocess, "run", side_effect=fake_run):
                diff = wg_sync.sync_wg_peers(
                    "proj", wg_conf,
                    [{"public_key": new_pub, "allowed_ips": "10.79.0.4/32"}],
                    starter_public_key=starter_pub,
                )

            self.assertEqual(diff["added"], [new_pub])
            self.assertEqual(diff["removed"], [old_pub])
            self.assertIn(["docker", "exec", "wg-cid", "wg", "set", "wg-profile", "peer", old_pub, "remove"], calls)
            self.assertIn(
                ["docker", "exec", "wg-cid", "wg", "set", "wg-profile", "peer", new_pub, "allowed-ips", "10.79.0.4/32"],
                calls,
            )
            _, persisted_peers = wg_sync.parse_wg_conf(wg_conf.read_text())
            self.assertEqual(set(persisted_peers), {starter_pub, new_pub})
            self.assertEqual(persisted_peers[new_pub]["allowed_ips"], "10.79.0.4/32")

            # Reject server IP 10.79.0.1/32
            with self.assertRaises(ValueError):
                wg_sync.validate_allowed_ip("10.79.0.1/32")


class MultiExitBalancerTests(unittest.TestCase):
    def setUp(self):
        self.paths = {
            "eu1": dict(entry="e", exit="x1", transport="quic", external_ip="198.51.100.1", weight=3),
            "eu2": dict(entry="e", exit="x2", transport="quic", external_ip="198.51.100.2", weight=1),
        }
        self.metrics = {
            "eu1": dict(up=True, expires=200, latency_ms=20, loss=0.0, mbps=80),
            "eu2": dict(up=True, expires=200, latency_ms=60, loss=0.0, mbps=40),
        }

    def test_weighted_and_balanced_distribution_and_nftables_numgen(self):
        profile = dict(entry="e", paths=["eu1", "eu2"], strategy="weighted", weights={"eu1": 70, "eu2": 30})
        cfg = dict(paths=self.paths, profiles={"p": profile})
        topology.validate(cfg)
        decision = topology.decide(cfg, dict(profile="p", metrics=self.metrics), 100)
        self.assertEqual(decision["selected"], "eu1")
        self.assertEqual(decision["weights"], {"eu1": 70, "eu2": 30})
        self.assertEqual(decision["distribution"], [("eu1", 70), ("eu2", 30)])

        binding = dict(profile="p", user="alice", source="10.77.0.0/24",
                       paths={"eu1": dict(table=1001, interface="anet-up1"),
                              "eu2": dict(table=1002, interface="anet-up2")})
        nft, events = FLOW["render"](cfg, binding, self.metrics, 100, {})
        self.assertIn("ct mark != 0 meta mark set ct mark return", nft)
        self.assertIn("ct mark set numgen random mod 100 map { 0-69 : 1001, 70-99 : 1002 } meta mark set ct mark return", nft)
        self.assertEqual(events["(default)"]["weights"], {"eu1": 70, "eu2": 30})

        # Balanced / least-load gives higher weight to faster lower-latency exit
        profile["strategy"] = "balanced"
        del profile["weights"]
        bal = topology.decide(cfg, dict(profile="p", metrics=self.metrics), 100)
        self.assertGreater(bal["weights"]["eu1"], bal["weights"]["eu2"])
        self.assertEqual(len(bal["distribution"]), 2)

    def test_fail_mode_degrade_vs_fail_closed_and_strict_ip_guard(self):
        profile = dict(entry="e", paths=["eu1", "eu2"], strategy="weighted", fail_mode="degrade")
        cfg = dict(paths=self.paths, profiles={"p": profile})
        topology.validate(cfg)
        # Empty metrics (metrics server unreachable) -> degraded keeps routing
        decision = topology.decide(cfg, dict(profile="p", metrics={}), 100)
        self.assertTrue(decision["degraded"])
        self.assertEqual(decision["action"], "route")
        self.assertEqual(set(decision["eligible"]), {"eu1", "eu2"})

        binding = dict(profile="p", user="alice", source="10.77.0.0/24",
                       paths={"eu1": dict(table=1001, interface="anet-up1"),
                              "eu2": dict(table=1002, interface="anet-up2")})
        nft, _ = FLOW["render"](cfg, binding, {}, 100, {})
        self.assertIn("1001 timeout 15s", nft)
        self.assertIn("1002 timeout 15s", nft)
        self.assertIn("numgen random mod", nft)

        # strict-ip rejects fail-open
        bad_cfg = dict(paths=self.paths, profiles={
            "s": dict(entry="e", paths=["eu1"], strategy="strict-ip", external_ip="198.51.100.1", fail_mode="fail-open")
        })
        with self.assertRaisesRegex(ValueError, "strict-ip requires fail-closed"):
            topology.validate(bad_cfg)

    def test_probe_local_paths_and_minimal_toml_parser(self):
        def fake_probe(host, port, iface, timeout):
            return 12.5 if host == "198.51.100.1" else None

        res = topology.probe_local_paths(self.paths, timeout=1.0, tcp_probe=fake_probe, now=1000)
        self.assertTrue(res["eu1"]["up"])
        self.assertEqual(res["eu1"]["latency_ms"], 12.5)
        self.assertFalse(res["eu2"]["up"])

        parsed = topology.parse_toml_minimal(
            '[main]\nmanual_routing = true\ntun_name = "anet-up1"\n'
            'dns_server_list = ["1.1.1.1", "8.8.8.8"]\n'
            '[[servers]]\nname = "exit"\ndsn = "quic://198.51.100.1:48000"\n'
        )
        self.assertTrue(parsed["main"]["manual_routing"])
        self.assertEqual(parsed["main"]["tun_name"], "anet-up1")
        self.assertEqual(parsed["main"]["dns_server_list"], ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(parsed["servers"][0]["name"], "exit")


if __name__ == "__main__":
    unittest.main()
