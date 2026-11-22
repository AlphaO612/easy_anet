"""Exercise enrollment against a disposable migrated API / database."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class NodeEnrollment(unittest.TestCase):
    def request(self, path, headers, body=None, method="POST"):
        data = json.dumps(body).encode() if body is not None else b""
        request = Request(
            os.environ["ANET_TEST_API"].rstrip("/") + path,
            data=None if method == "GET" else data, method=method,
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urlopen(request, timeout=10) as response:
                result = response.read()
                return response.status, json.loads(result) if result else None
        except HTTPError as rejection:
            return rejection.code, None

    def test_single_use_concurrent_exchange(self):
        admin = {"Authorization": "Bearer " + os.environ["ANET_TEST_ADMIN_TOKEN"]}
        status, node = self.request("/servers", admin, {
            "name": "enrollment-test", "address": "localhost", "public_key": "",
        })
        self.assertEqual(status, 200)
        self.assertEqual(node["address"], "localhost")
        node_id = node["id"]
        invite_path = f"/servers/{node_id}/invitations"
        enroll_path = f"/control/nodes/{node_id}/enroll"

        def issue():
            status, invitation = self.request(invite_path, admin)
            self.assertEqual(status, 200)
            return invitation["token"]

        def exchange(token):
            return self.request(enroll_path, {"X-Enrollment-Token": token})

        first = issue()
        second = issue()
        self.assertEqual(exchange(first)[0], 401)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(exchange, [second, second]))
        self.assertEqual(sorted(status for status, _ in results), [200, 401])
        credential = next(value["token"] for status, value in results if status == 200)
        self.assertNotEqual(credential, second)
        final_invite = issue()
        status, _ = self.request("/control/nodes/heartbeat", {"X-Node-Token": credential}, {
            "node_id": node_id, "version": "test", "uptime_seconds": 1,
            "active_connections": 0, "accepting_connections": True,
        })
        self.assertEqual(status, 204)
        status, _ = self.request(f"/servers/{node_id}/credentials", admin)
        self.assertEqual(status, 200)
        self.assertEqual(exchange(final_invite)[0], 401)


    def test_revision_conflict_and_stale_ack(self):
        admin = {"Authorization": "Bearer " + os.environ["ANET_TEST_ADMIN_TOKEN"]}
        status, node = self.request("/servers", admin, {
            "name": "revision-test", "address": "localhost", "public_key": "",
        })
        self.assertEqual(status, 200)
        node_id = node["id"]
        status, credential = self.request(f"/servers/{node_id}/credentials", admin)
        self.assertEqual(status, 200)
        agent = {"X-Node-Token": credential["token"]}
        panel_path = f"/servers/{node_id}/probe-revision"
        agent_path = f"/control/nodes/{node_id}/probe-revision"
        status, peer = self.request("/servers", admin, {
            "name": "revision-peer", "address": "localhost", "public_key": "",
        })
        self.assertEqual(status, 200)
        capacity = {"target_node": peer["id"], "module": "https_capacity",
                    "target": "https://localhost:9444/anet/capacity.bin"}
        invalid = dict(capacity, target="https://localhost:9444/not-capacity")
        self.assertEqual(self.request(panel_path, admin, {"base_revision": 0, "edges": [invalid]}, "PUT")[0], 400)
        payload = {"base_revision": 0, "edges": [
            capacity
        ]}
        self.assertEqual(self.request(agent_path, {}, method="GET")[0], 400)
        self.assertEqual(self.request(agent_path, {"X-Node-Token": "invalid"}, method="GET")[0], 401)
        self.assertEqual(self.request("/probe-links", {}, method="GET")[0], 401)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request(panel_path, admin, payload, "PUT"), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        status, actual = self.request(agent_path, agent, method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(actual["signature"])
        self.assertEqual(actual["edges"], payload["edges"])
        self.assertIn(self.request("/probe-links", agent, method="GET")[0], (401, 403))
        status, graph = self.request("/probe-links", admin, method="GET")
        self.assertEqual(status, 200)
        links = [link for link in graph["links"] if link["source_node"] == node_id]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["edge"], payload["edges"][0])
        self.assertEqual((links[0]["desired"], links[0]["applied"]), (1, 0))
        self.assertIsNone(links[0].get("probe_bytes_per_second"))
        ack = agent_path + "/result"
        self.assertIn(self.request(ack, agent, {"revision": 1, "error": None})[0], (200, 204))
        status, graph = self.request("/probe-links", admin, method="GET")
        self.assertEqual(status, 200)
        links = [link for link in graph["links"] if link["source_node"] == node_id]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["edge"], payload["edges"][0])
        self.assertEqual((links[0]["desired"], links[0]["applied"]), (1, 1))
        self.assertEqual(self.request(panel_path, admin, {"base_revision": 1, "edges": []}, "PUT")[0], 200)
        self.assertEqual(self.request(ack, agent, {"revision": 1, "error": None})[0], 409)
        self.assertIn(self.request(ack, agent, {"revision": 2, "error": "disk full"})[0], (200, 204))
        status, state = self.request(panel_path, admin, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual((state["desired"], state["applied"], state["error"]), (2, 1, "disk full"))
        status, history = self.request(f"/servers/{node_id}/probe-history", admin, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual([entry["manifest"]["revision"] for entry in history], [2, 1])
        self.assertTrue(all(entry["operator_id"] and entry["published_at"] for entry in history))


if __name__ == "__main__":
    unittest.main()
