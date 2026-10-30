import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.peer_forwarding import (
    FORWARD_TIMEOUT,
    _payload,
    forward_scoped_request,
    never_reached_peer,
)
from core.admin_server import admin_host


class PeerForwardingTest(unittest.TestCase):
    def test_only_connection_failures_fall_back_to_local_hosting(self):
        refused = aiohttp.ClientConnectorError(
            mock.Mock(host="pod", port=7014, ssl=None), OSError("refused")
        )
        self.assertTrue(never_reached_peer(refused))
        self.assertTrue(never_reached_peer(ConnectionRefusedError()))
        # sock_connect expiring is also pre-delivery; a read timeout is not.
        self.assertTrue(never_reached_peer(aiohttp.ConnectionTimeoutError()))
        self.assertFalse(never_reached_peer(aiohttp.ServerTimeoutError()))
        self.assertFalse(never_reached_peer(asyncio.TimeoutError()))
        self.assertFalse(never_reached_peer(aiohttp.ClientPayloadError("cut")))
        self.assertEqual(FORWARD_TIMEOUT.sock_connect, 5)

    def test_non_json_peer_bodies_become_an_error_body(self):
        async def scenario():
            app = web.Application()
            app.router.add_get("/html", lambda r: web.Response(status=502, text="<html>bad gateway</html>"))
            app.router.add_get("/json", lambda r: web.json_response({"bot_id": "x"}))
            app.router.add_get("/truncated", lambda r: web.Response(status=201, text='{"bot_id": "x'))
            async with TestServer(app) as server, aiohttp.ClientSession() as session:
                async with session.get(server.make_url("/html")) as resp:
                    body, status = await _payload(resp)
                self.assertEqual((body["status"], status), ("error", 502))
                async with session.get(server.make_url("/json")) as resp:
                    self.assertEqual(await _payload(resp), ({"bot_id": "x"}, 200))
                # A 201 the caller cannot read is not a success: no bot id came back.
                async with session.get(server.make_url("/truncated")) as resp:
                    body, status = await _payload(resp)
                self.assertEqual((body["status"], status), ("error", 502))

        asyncio.run(scenario())

    def test_per_bot_requests_are_asked_of_every_peer_until_one_knows_the_bot(self):
        async def scenario():
            asked = []

            async def peer(request):
                asked.append(request.match_info["ordinal"])
                if request.match_info["ordinal"] == "2" and request.match_info["bot_id"] == "b1":
                    return web.json_response({"tts_characters": 9}, status=200)
                return web.json_response({"message": "No live bot"}, status=404)

            app = web.Application()
            app.router.add_route("*", "/peer{ordinal}/bots/{bot_id}/{rest:.*}", peer)
            async with TestServer(app) as server:
                env = {
                    "POD_NAME": "speaking-bot-0",
                    "SPEAKING_REPLICAS": "3",
                    # Built by hand: yarl would percent-encode the braces.
                    "SPEAKING_PEER_URL_TEMPLATE": f"http://127.0.0.1:{server.port}/peer{{ordinal}}",
                }
                request = SimpleNamespace(headers={}, state=SimpleNamespace(api_key="k"))
                with mock.patch.dict(os.environ, env):
                    answer = await forward_scoped_request("GET", "/bots/b1/usage", None, request)
                    self.assertIsNotNone(answer)
                    self.assertEqual(answer.status_code, 200)
                    self.assertEqual(asked, ["1", "2"])
                    asked.clear()
                    nobody = await forward_scoped_request("POST", "/bots/zz/messages", b"{}", request)
                    self.assertIsNone(nobody)
                    self.assertEqual(asked, ["1", "2"])
                    forwarded = SimpleNamespace(
                        headers={"x-speaking-forwarded": "1"}, state=SimpleNamespace(api_key="k")
                    )
                    self.assertIsNone(await forward_scoped_request("GET", "/bots/b1/usage", None, forwarded))
                # A peer that cannot be reached may be the one hosting the bot:
                # that is a 502 to retry, not a 404.
                with mock.patch.dict(os.environ, {**env, "SPEAKING_REPLICAS": "4"}), mock.patch(
                    "app.peer_forwarding.peer_base_url",
                    side_effect=lambda n: "http://127.0.0.1:9/dead" if n == 3 else f"http://127.0.0.1:{server.port}/peer{n}",
                ):
                    unsure = await forward_scoped_request("POST", "/bots/zz/messages", b"{}", request)
                    self.assertIsNotNone(unsure)
                    self.assertEqual(unsure.status_code, 502)

        asyncio.run(scenario())


class AdminHostTest(unittest.TestCase):
    def test_loopback_outside_a_pod_all_interfaces_inside(self):
        with mock.patch.dict(os.environ, {"POD_NAME": ""}):
            self.assertEqual(admin_host(), "127.0.0.1")
        with mock.patch.dict(os.environ, {"POD_NAME": "speaking-bot-1"}):
            self.assertEqual(admin_host(), "0.0.0.0")
