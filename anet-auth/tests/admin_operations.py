"""Run against a disposable migrated admin API with ANET_TEST_API and ANET_TEST_ADMIN_TOKEN."""
import json
import os
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class AdminOperations(unittest.TestCase):
    def request(self, path, method="GET", body=None, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + os.environ["ANET_TEST_ADMIN_TOKEN"]
        req = Request(os.environ["ANET_TEST_API"].rstrip("/") + path, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as rejection:
            return rejection.code, None

    def test_incident_persists_with_operator(self):
        path = "/operator-incidents"
        self.assertEqual(self.request(path, token=False)[0], 401)
        body = dict(observer="local acceptance client", direction="entry → exit",
                    transport="wireguard", evidence="operator supplied timeout")
        status, row = self.request(path, "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(row["observation"], body)
        self.assertTrue(row["operator_id"] and row["reported_at"])
        status, rows = self.request(path)
        self.assertEqual(status, 200)
        self.assertIn(row, rows)
        self.assertEqual(self.request(path, "POST", dict(body, evidence=" "))[0], 400)

    def test_publish_and_compare_base_revision(self):
        path = "/profile-topology"
        self.assertEqual(self.request(path, token=False)[0], 401)
        status, current = self.request(path)
        self.assertEqual(status, 200)
        manifest = dict(paths={"entry":dict(entry="a",exit="a",transport="wg",external_ip="192.0.2.1")},
                        profiles={"alice":dict(entry="a",paths=["entry"],strategy="auto")})
        wanted = dict(revision=current["revision"], manifest=json.dumps(manifest))
        status, saved = self.request(path, "PUT", wanted)
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], current["revision"]+1)
        self.assertEqual(json.loads(saved["manifest"]), manifest)
        self.assertEqual(self.request(path, "PUT", wanted)[0], 409)
        self.assertEqual(self.request(path), (200, saved))


if __name__ == "__main__":
    unittest.main()
