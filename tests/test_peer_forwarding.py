import asyncio
import os
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.peer_forwarding import FORWARD_TIMEOUT, _payload, never_reached_peer
from core.admin_server import admin_host


class PeerForwardingTest(unittest.TestCase):
    def test_only_connection_failures_fall_back_to_local_hosting(self):
        refused = aiohttp.ClientConnectorError(
            mock.Mock(host="pod", port=7014, ssl=None), OSError("refused")
        )
        self.assertTrue(never_reached_peer(refused))
        self.assertTrue(never_reached_peer(ConnectionRefusedError()))
        self.assertFalse(never_reached_peer(asyncio.TimeoutError()))
        self.assertFalse(never_reached_peer(aiohttp.ClientPayloadError("cut")))
        self.assertEqual(FORWARD_TIMEOUT.sock_connect, 5)

    def test_non_json_peer_bodies_become_an_error_body(self):
        async def scenario():
            app = web.Application()
            app.router.add_get("/html", lambda r: web.Response(status=502, text="<html>bad gateway</html>"))
            app.router.add_get("/json", lambda r: web.json_response({"bot_id": "x"}))
            async with TestServer(app) as server, aiohttp.ClientSession() as session:
                async with session.get(server.make_url("/html")) as resp:
                    body = await _payload(resp)
                self.assertEqual(body["status"], "error")
                self.assertIn("502", body["message"])
                async with session.get(server.make_url("/json")) as resp:
                    self.assertEqual(await _payload(resp), {"bot_id": "x"})

        asyncio.run(scenario())


class AdminHostTest(unittest.TestCase):
    def test_loopback_outside_a_pod_all_interfaces_inside(self):
        with mock.patch.dict(os.environ, {"POD_NAME": ""}):
            self.assertEqual(admin_host(), "127.0.0.1")
        with mock.patch.dict(os.environ, {"POD_NAME": "speaking-bot-1"}):
            self.assertEqual(admin_host(), "0.0.0.0")
