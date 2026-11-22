"""Verify on-disk Xray provisioning and subscription isolation end to end through HTTP."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
from wizard_node import publish_device_catalog, vless_devices
import wizard_https

module = importlib.util.spec_from_file_location("profiles", DEPLOY / "images/profile-service.py")
service = importlib.util.module_from_spec(module)
module.loader.exec_module(service)


class Devices(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / "server.json"
        config.write_text(json.dumps({"inbounds": [{"protocol": "vless", "settings": {"clients": [{"id": "original"}]}}]}))
        self.ids = vless_devices(root)
        previous = config.read_bytes()
        self.assertEqual(self.ids, vless_devices(root))
        self.assertEqual(previous, config.read_bytes())
        self.assertEqual(json.loads(previous)["inbounds"][0]["settings"]["clients"][0]["id"], "original")
        self.assertEqual(len(set(self.ids)), 128)
        self.row = dict(id="profile-1", devices=self.ids, entry="vless.example.org", exit="exit", port=48001)
        self.root = root
        catalog = publish_device_catalog(root, [self.row], "https://vless.example.org")
        with mock.patch.object(service, "os") as os_mock:
            with mock.patch.object(service, "DB", root / "state.sqlite"):
                service.init()
        self.patches = [mock.patch.object(service, "DB", root / "state.sqlite"),
                        mock.patch.object(service, "CATALOG", catalog / "index.json"),
                        mock.patch.object(service, "authenticate", side_effect=self.auth)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def auth(header):
        if header != "Bearer admin":
            raise service.Problem(401, "Forbidden")

    def request(self, path, method="GET", body=None, admin=True):
        headers = {"Authorization": "Bearer admin"} if admin else {}
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.origin + path, data=data, headers=headers, method=method)
        with urlopen(req, timeout=5) as res:
            self.assertEqual(res.headers["Cache-Control"], "no-store")
            raw = res.read()
            return json.loads(raw) if "json" in res.headers["Content-Type"] else base64.b64decode(raw).decode()

    def test_groups_devices_scope_and_rotation(self):
        a = service.ADMIN
        self.assertFalse(any(k in json.dumps(self.request(a)) for k in self.ids))
        users = [self.request(a, "POST", {"name": n})["id"] for n in ("Алиса", "Борис")]
        def issue(user):
            return self.request(a + "/" + user + "/profiles", "POST", {"name": "Телефон", "route": "profile-1"})[0]["uri"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(issue, users))
        self.assertNotEqual(first, second)
        self.assertEqual(issue(users[0]), first)
        token_endpoint = a + "/" + users[0] + "/token"
        path = self.request(token_endpoint, "POST")["path"]
        self.assertEqual(self.request(path, admin=False).strip(), first)
        self.request(a + "/" + users[0] + "/profiles", "POST", {"name": "Ноутбук", "route": "profile-1"})
        active = self.request(path, admin=False)
        self.assertIn(first, active)
        self.assertNotIn(second, active)
        self.assertEqual(len(active.splitlines()), 2)
        with self.assertRaises(HTTPError) as unauth:
            self.request(a + "/" + users[1] + "/profiles", admin=False)
        self.assertEqual(unauth.exception.code, 401)
        new = self.request(token_endpoint, "POST")["path"]
        with self.assertRaises(HTTPError) as expired:
            self.request(path, admin=False)
        self.assertEqual(expired.exception.code, 404)
        self.request(token_endpoint, "DELETE")
        with self.assertRaises(HTTPError) as revoked:
            self.request(new, admin=False)
        self.assertEqual(revoked.exception.code, 404)
        on_disk = service.DB.read_bytes()
        self.assertNotIn(path.split("/")[-1].encode(), on_disk)
        self.assertFalse(any(k.encode() in on_disk for k in self.ids))

    def test_public_nginx_limits_subscription_to_single_path(self):
        spec = dict(domain="vpn.example.test", installation="0123456789", entries=["entry-1"], owner="anet")
        self.assertNotIn(b"proxy_pass", wizard_https.nginx_site(spec, True))
        conf = wizard_https.nginx_site(dict(spec, subscription_port=18088), True)
        self.assertIn(b'location ~ "^/api/v1/vless-subscriptions/[A-Za-z0-9_-]{43}$"', conf)
        self.assertIn(b"127.0.0.1:18088", conf)
        self.assertIn(b"access_log off;", conf)
        self.assertNotIn(b"vless-devices", conf)
        self.assertNotIn(b"proxy_pass", wizard_https.nginx_site(dict(spec, subscription_port=18088), False))

