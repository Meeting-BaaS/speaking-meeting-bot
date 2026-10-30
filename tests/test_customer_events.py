import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from utils.customer_events import (
    EVENT_HEADER,
    EVENT_UTTERANCE,
    SIGNATURE_HEADER,
    CustomerEventSink,
    build_event,
    post_event,
    sign_body,
    verify_signature,
)


class SignatureTest(unittest.TestCase):
    def test_signature_round_trip(self):
        body = b'{"a":1}'
        header = sign_body("secret", body)
        self.assertTrue(header.startswith("sha256="))
        self.assertTrue(verify_signature("secret", body, header))
        self.assertFalse(verify_signature("other", body, header))
        self.assertFalse(verify_signature("secret", body, None))

    def test_envelope_prefers_the_meetingbaas_id(self):
        envelope = build_event(EVENT_UTTERANCE, {"text": "hi"}, bot_id="c1", meetingbaas_bot_id="mb1")
        self.assertEqual(envelope["bot_id"], "mb1")
        self.assertEqual(envelope["client_id"], "c1")
        self.assertEqual(envelope["event"], EVENT_UTTERANCE)
        self.assertIn("event_id", envelope)


class _Receiver:
    def __init__(self, fail_first: int = 0):
        self.fail_first = fail_first
        self.received = []

    async def handle(self, request: web.Request):
        body = await request.read()
        self.received.append((dict(request.headers), body))
        if self.fail_first > 0:
            self.fail_first -= 1
            return web.Response(status=503)
        return web.Response(status=204)


class DeliveryTest(unittest.TestCase):
    def _run(self, coroutine):
        return asyncio.run(coroutine)

    def test_posts_signed_body_and_retries_server_errors(self):
        async def scenario():
            receiver = _Receiver(fail_first=1)
            app = web.Application()
            app.router.add_post("/hook", receiver.handle)
            async with TestServer(app) as server:
                url = str(server.make_url("/hook"))
                envelope = build_event(EVENT_UTTERANCE, {"text": "hello"}, bot_id="c1")
                ok = await post_event(url, envelope, secret="s3", retry_delays=(0.01,))
                self.assertTrue(ok)
                self.assertEqual(len(receiver.received), 2)
                headers, body = receiver.received[-1]
                self.assertEqual(json.loads(body)["data"]["text"], "hello")
                self.assertEqual(headers[EVENT_HEADER], EVENT_UTTERANCE)
                self.assertTrue(verify_signature("s3", body, headers[SIGNATURE_HEADER]))

        self._run(scenario())

    def test_client_errors_are_not_retried(self):
        async def scenario():
            calls = []

            async def reject(request):
                calls.append(1)
                return web.Response(status=400)

            app = web.Application()
            app.router.add_post("/hook", reject)
            async with TestServer(app) as server:
                ok = await post_event(
                    str(server.make_url("/hook")),
                    build_event("x", {}, bot_id="c1"),
                    retry_delays=(0.01, 0.01),
                )
                self.assertFalse(ok)
                self.assertEqual(len(calls), 1)

        self._run(scenario())

    def test_sink_is_inert_without_a_url_and_flushes_on_close(self):
        async def scenario():
            off = CustomerEventSink(None, bot_id="c1")
            self.assertFalse(off.enabled)
            self.assertIsNone(await off.emit("x", {}))
            await off.close()

            receiver = _Receiver()
            app = web.Application()
            app.router.add_post("/hook", receiver.handle)
            async with TestServer(app) as server:
                sink = CustomerEventSink(str(server.make_url("/hook")), "k", bot_id="c1", meetingbaas_bot_id="mb")
                envelope = await sink.emit(EVENT_UTTERANCE, {"text": "one"})
                self.assertEqual(envelope["bot_id"], "mb")
                await sink.close()
                self.assertEqual(len(receiver.received), 1)
                self.assertTrue(await sink.send(EVENT_UTTERANCE, {"text": "two"}))
                self.assertEqual(len(receiver.received), 2)

        self._run(scenario())
