import importlib.util
import json
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location("node_install", Path(__file__).resolve().parents[1] / "images/node-install.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class InstallationApi(unittest.TestCase):
    def setUp(self):
        self.node = "5901089a-2431-4012-a7a0-f294aead3862"
        self.api = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        self.thread = Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.api.server_port}"

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        self.thread.join()

    def request(self, suffix, data=None):
        return urlopen(Request(self.base + "/api/v1/node-install/" + self.node + "/" + suffix,
                               json.dumps(data).encode() if data is not None else None,
                               {"Content-Type": "application/json"}), timeout=3)

    def test_auth_required_before_ssh(self):
        with patch.object(service, "ssh_target") as ssh:
            with self.assertRaises(HTTPError) as err:
                self.request("fingerprint", {})
            self.assertEqual(err.exception.code, 401)
            ssh.assert_not_called()
        with urlopen(self.base + "/health", timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_changed_pin_never_sends_password(self):
        with patch.object(service, "find_server", return_value={"id": self.node}), \
             patch.object(service, "ssh_target", return_value=("127.0.0.1", 22, "root")), \
             patch.object(service, "scan", return_value=("SHA256:replacement", "key")), \
             patch.object(service, "start") as start:
            with self.assertRaises(HTTPError) as err:
                self.request("start", {"fingerprint": "SHA256:previous", "password": "fixture"})
            self.assertEqual(err.exception.code, 409)
            start.assert_not_called()

    def test_internal_vps_uses_manual_workflow(self):
        with self.assertRaises(service.Failure):
            service.ssh_target({"address": "127.0.0.1"})

    def test_online_node_is_protected(self):
        with self.assertRaises(service.Failure) as error:
            service.start({"runtime": {"status": "online"}}, {}, (), "")
        self.assertEqual(error.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
