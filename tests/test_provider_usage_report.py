import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from utils import provider_usage_report as report_module
from utils.provider_usage_report import (
    BOT_PROCESS_URL_ENV,
    build_report,
    claim_report,
    post_report,
    release_claim,
    schedule_provider_usage_report,
)

LEDGER = {
    "bot_id": "client-1",
    "meetingbaas_bot_id": "0d6c1f1e-4b4a-4e7a-9e2e-0c8f4a3b2d11",
    "stt_provider": "deepgram",
    "tts_provider": "cartesia",
    "llm_provider": "openai",
    "llm_model": "gpt-5.4-mini",
    "key_sources": {"llm": "caller", "stt": "platform", "tts": "platform"},
    "started_at": 1793000000.0,
    "ended_at": 1793001906.5,
    "duration_seconds": 1906.5,
    "stt_audio_seconds": 1903.25,
    "tts_characters": 2749,
    "tts_requests": 41,
    "llm_turns": 12,
    "llm_prompt_tokens": 58000,
    "llm_cached_prompt_tokens": 50000,
    "llm_completion_tokens": 1906,
    "llm_total_tokens": 59906,
    "models_seen": {"stt": "nova-3"},
    "owner_digest": "should-not-leave",
}


class BuildReportTest(unittest.TestCase):
    def test_copies_the_ledger_field_for_field_and_nothing_else(self):
        report = build_report(LEDGER)
        self.assertEqual(report["bot_id"], LEDGER["meetingbaas_bot_id"])
        self.assertEqual(report["source"], "speaking_bot")
        self.assertEqual(
            report["key_sources"],
            {"stt": "platform", "tts": "platform", "llm": "caller"},
        )
        self.assertEqual(report["stt_audio_seconds"], 1903.25)
        self.assertEqual(report["llm_cached_prompt_tokens"], 50000)
        self.assertEqual(report["started_at"], 1793000000.0)
        self.assertEqual(report["llm_model"], "gpt-5.4-mini")
        self.assertNotIn("owner_digest", report)
        self.assertNotIn("models_seen", report)

    def test_unknown_key_sources_count_as_platform(self):
        report = build_report({**LEDGER, "key_sources": {}})
        self.assertEqual(
            report["key_sources"],
            {"stt": "platform", "tts": "platform", "llm": "platform"},
        )

    def test_a_ledger_without_a_meetingbaas_id_is_not_reported(self):
        self.assertIsNone(build_report({**LEDGER, "meetingbaas_bot_id": None}))

    def test_bad_numbers_become_zero_or_null(self):
        report = build_report(
            {
                **LEDGER,
                "tts_characters": None,
                "stt_audio_seconds": -3,
                "started_at": "x",
            }
        )
        self.assertEqual(report["tts_characters"], 0)
        self.assertEqual(report["stt_audio_seconds"], 0.0)
        self.assertIsNone(report["started_at"])


class _Api:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.received = []

    async def handle(self, request: web.Request):
        self.received.append(json.loads(await request.read()))
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            return web.json_response({"success": True, "data": {"billing": "billed"}})
        return web.Response(status=status)


class PostReportTest(unittest.TestCase):
    def setUp(self):
        self.delays = mock.patch.object(
            report_module, "RETRY_DELAYS_SECS", (0.0, 0.0, 0.0)
        )
        self.delays.start()
        self.logs = []

    def tearDown(self):
        self.delays.stop()

    def _serve(self, api, coro):
        async def run():
            app = web.Application()
            app.router.add_post("/provider-usage", api.handle)
            async with TestServer(app) as server:
                return await coro(str(server.make_url("")).rstrip("/"))

        return asyncio.run(run())

    def test_posts_the_report_and_retries_server_errors(self):
        api = _Api([503, 200])
        delivered = self._serve(
            api, lambda url: post_report(url, build_report(LEDGER), self.logs.append)
        )
        self.assertTrue(delivered)
        self.assertEqual(len(api.received), 2)
        self.assertEqual(api.received[0]["bot_id"], LEDGER["meetingbaas_bot_id"])
        self.assertTrue(any("billed" in line for line in self.logs))

    def test_a_404_is_not_retried(self):
        api = _Api([404])
        delivered = self._serve(
            api, lambda url: post_report(url, build_report(LEDGER), self.logs.append)
        )
        self.assertFalse(delivered)
        self.assertEqual(len(api.received), 1)

    def test_gives_up_after_the_retries(self):
        api = _Api([500, 500, 500, 500, 500])
        delivered = self._serve(
            api, lambda url: post_report(url, build_report(LEDGER), self.logs.append)
        )
        self.assertFalse(delivered)
        self.assertEqual(len(api.received), 4)

    def test_an_unreachable_api_is_not_fatal(self):
        async def run():
            return await post_report(
                "http://127.0.0.1:9", build_report(LEDGER), self.logs.append
            )

        self.assertFalse(asyncio.run(run()))
        self.assertTrue(any("could not reach" in line for line in self.logs))


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.usage_dir = os.path.join(self.tmp.name, "usage")
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_leaves_without_an_endpoint(self):
        async def run():
            with mock.patch.dict(os.environ, {BOT_PROCESS_URL_ENV: ""}):
                return schedule_provider_usage_report(
                    LEDGER, usage_directory=self.usage_dir, log=self.logs.append
                )

        self.assertIsNone(asyncio.run(run()))
        self.assertTrue(any("stays local" in line for line in self.logs))

    def test_reports_once_per_bot_and_releases_the_claim_on_failure(self):
        bot_id = LEDGER["meetingbaas_bot_id"]

        async def run():
            posted = mock.AsyncMock(side_effect=[False, True])
            with (
                mock.patch.dict(os.environ, {BOT_PROCESS_URL_ENV: "http://api.test/"}),
                mock.patch.object(report_module, "post_report", posted),
            ):
                first = schedule_provider_usage_report(
                    LEDGER, usage_directory=self.usage_dir, log=self.logs.append
                )
                # A second end-of-call event while the first report is on its way
                second = schedule_provider_usage_report(
                    LEDGER, usage_directory=self.usage_dir, log=self.logs.append
                )
                self.assertIsNone(second)
                self.assertFalse(await first)
                # The failed report released the claim: the next event reports again
                third = schedule_provider_usage_report(
                    LEDGER, usage_directory=self.usage_dir, log=self.logs.append
                )
                self.assertTrue(await third)
                self.assertEqual(posted.await_args.args[0], "http://api.test")
                return posted.await_count

        self.assertEqual(asyncio.run(run()), 2)
        self.assertFalse(claim_report(self.usage_dir, bot_id))
        release_claim(self.usage_dir, bot_id)
        self.assertTrue(claim_report(self.usage_dir, bot_id))
