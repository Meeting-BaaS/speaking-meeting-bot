import http.client
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from app.main import create_app
from app.peer_forwarding import owner_for
from core import drain
from core.admin_server import start_admin_server
from utils.pod_routing import (
    owner_ordinal,
    peer_base_url,
    peer_ordinals,
    pod_ordinal,
    pod_route_prefix,
    with_pod_prefix,
)

ENV = ("POD_NAME", "SPEAKING_REPLICAS", "SPEAKING_PEER_URL_TEMPLATE")


class PodRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        drain.reset()

    def test_single_process_is_a_no_op(self) -> None:
        self.assertIsNone(pod_ordinal())
        self.assertEqual(pod_route_prefix(), "")
        self.assertEqual(with_pod_prefix("wss://bots.example.com/"), "wss://bots.example.com")
        self.assertEqual(owner_ordinal("https://meet.google.com/abc-defg-hij"), 0)
        self.assertEqual(peer_ordinals(), [])

    def test_prefix_and_peers_follow_the_pod_name(self) -> None:
        os.environ["POD_NAME"] = "speaking-bot-2"
        os.environ["SPEAKING_REPLICAS"] = "3"
        os.environ["SPEAKING_PEER_URL_TEMPLATE"] = "http://speaking-bot-{ordinal}.speaking-bot-headless:7014/"
        self.assertEqual(pod_ordinal(), 2)
        self.assertEqual(with_pod_prefix("https://speaking.example.com"), "https://speaking.example.com/p2")
        self.assertEqual(peer_ordinals(), [0, 1])
        self.assertEqual(peer_base_url(1), "http://speaking-bot-1.speaking-bot-headless:7014")

    def test_same_meeting_lands_on_one_pod_whatever_the_url_variant(self) -> None:
        url = "https://meet.google.com/abc-defg-hij"
        owner = owner_ordinal(url, replicas=3)
        self.assertIn(owner, range(3))
        self.assertEqual(owner_ordinal(f"{url}?authuser=0", replicas=3), owner)
        self.assertEqual(owner_ordinal(url.upper(), replicas=3), owner)
        self.assertEqual(owner_ordinal(url, replicas=1), 0)

    def test_owner_for_is_none_on_the_owning_pod(self) -> None:
        url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x"
        os.environ["SPEAKING_REPLICAS"] = "4"
        owner = owner_ordinal(url, replicas=4)
        os.environ["POD_NAME"] = f"speaking-bot-{owner}"
        self.assertIsNone(owner_for(url))
        os.environ["POD_NAME"] = f"speaking-bot-{(owner + 1) % 4}"
        self.assertEqual(owner_for(url), owner)


class DrainTest(unittest.TestCase):
    def setUp(self) -> None:
        drain.reset()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        drain.reset()

    def test_ready_fails_and_bots_are_refused_while_draining(self) -> None:
        self.assertEqual(self.client.get("/ready").status_code, 200)
        drain.start_draining()
        ready = self.client.get("/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["status"], "draining")
        refused = self.client.post(
            "/bots",
            json={"meeting_url": "https://meet.google.com/abc-defg-hij"},
            headers={"x-meeting-baas-api-key": "k"},
        )
        self.assertEqual(refused.status_code, 503)
        # Liveness is untouched: the pod keeps serving its live bots.
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_forwarded_delete_of_a_foreign_bot_is_a_404(self) -> None:
        response = self.client.request(
            "DELETE",
            f"/bots/{uuid.uuid4()}",
            json={},
            headers={"x-meeting-baas-api-key": "k", "x-speaking-forwarded": "1"},
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_server_reports_and_drains(self) -> None:
        server = start_admin_server(0, host="127.0.0.1")
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/live-bots")
            self.assertEqual(conn.getresponse().read(), b"0")
            conn.request("POST", "/drain")
            self.assertIn(b'"draining": true', conn.getresponse().read())
            self.assertTrue(drain.is_draining())
            conn.request("GET", "/nope")
            self.assertEqual(conn.getresponse().status, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
