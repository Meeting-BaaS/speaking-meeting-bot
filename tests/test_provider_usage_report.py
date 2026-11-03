import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from utils import provider_usage_report as report_module
from utils.provider_usage_report import (
    BOT_PROCESS_URL_ENV,
    build_report,
    claim_path,
    claim_report,
    post_report,
    release_claim,
    replay_unreported,
    schedule_provider_usage_report,
    sent_path,
    unreported_ledgers,
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
BOT_ID = LEDGER["meetingbaas_bot_id"]


class BuildReportTest(unittest.TestCase):
    def test_copies_the_ledger_field_for_field_and_nothing_else(self):
        report = build_report(LEDGER)
        self.assertEqual(report["bot_id"], BOT_ID)
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

    def _serve(self, api):
        async def run():
            app = web.Application()
            app.router.add_post("/provider-usage", api.handle)
            async with TestServer(app) as server:
                url = str(server.make_url("")).rstrip("/")
                return await post_report(url, build_report(LEDGER), self.logs.append)

        return asyncio.run(run())

    def test_posts_the_report_and_retries_server_errors(self):
        api = _Api([503, 200])
        self.assertEqual(self._serve(api), "delivered")
        self.assertEqual(len(api.received), 2)
        self.assertEqual(api.received[0]["bot_id"], BOT_ID)
        self.assertTrue(any("billed" in line for line in self.logs))

    def test_a_rate_limit_is_retried_like_a_server_error(self):
        api = _Api([429, 429, 200])
        self.assertEqual(self._serve(api), "delivered")
        self.assertEqual(len(api.received), 3)

    def test_a_404_is_refused_not_retried(self):
        api = _Api([404])
        self.assertEqual(self._serve(api), "refused")
        self.assertEqual(len(api.received), 1)

    def test_gives_up_after_the_retries(self):
        api = _Api([500, 429, 500, 500, 500])
        self.assertEqual(self._serve(api), "failed")
        self.assertEqual(len(api.received), 4)

    def test_an_unreachable_api_is_not_fatal(self):
        async def run():
            return await post_report(
                "http://127.0.0.1:9", build_report(LEDGER), self.logs.append
            )

        self.assertEqual(asyncio.run(run()), "failed")
        self.assertTrue(any("could not reach" in line for line in self.logs))


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.usage_dir = os.path.join(self.tmp.name, "usage")
        self.logs = []
        self.env = mock.patch.dict(
            os.environ, {BOT_PROCESS_URL_ENV: "http://api.test/"}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _schedule(self):
        return schedule_provider_usage_report(
            LEDGER, usage_directory=self.usage_dir, log=self.logs.append
        )

    def test_nothing_leaves_without_an_endpoint(self):
        async def run():
            with mock.patch.dict(os.environ, {BOT_PROCESS_URL_ENV: ""}):
                return self._schedule()

        self.assertIsNone(asyncio.run(run()))
        self.assertTrue(any("stays local" in line for line in self.logs))

    def test_delivers_once_and_retries_only_after_a_failure(self):
        async def run():
            posted = mock.AsyncMock(side_effect=["failed", "delivered"])
            with mock.patch.object(report_module, "post_report", posted):
                first = self._schedule()
                # A second end-of-call event while the first delivery runs
                self.assertIsNone(self._schedule())
                self.assertFalse(await first)
                # The failure left no marker: the next event delivers again
                self.assertFalse(os.path.exists(claim_path(self.usage_dir, BOT_ID)))
                third = self._schedule()
                self.assertTrue(await third)
                self.assertEqual(posted.await_args.args[0], "http://api.test")
                # Done for good: nothing else is scheduled for this bot
                self.assertIsNone(self._schedule())
                return posted.await_count

        self.assertEqual(asyncio.run(run()), 2)
        self.assertTrue(os.path.exists(sent_path(self.usage_dir, BOT_ID)))
        self.assertFalse(os.path.exists(claim_path(self.usage_dir, BOT_ID)))

    def test_a_refused_report_is_done_for_good(self):
        async def run():
            with mock.patch.object(
                report_module, "post_report", mock.AsyncMock(return_value="refused")
            ):
                self.assertFalse(await self._schedule())
                self.assertIsNone(self._schedule())

        asyncio.run(run())
        self.assertTrue(os.path.exists(sent_path(self.usage_dir, BOT_ID)))

    def test_a_cancelled_delivery_releases_its_claim(self):
        async def run():
            async def hang(*_args):
                await asyncio.sleep(60)

            with mock.patch.object(report_module, "post_report", hang):
                task = self._schedule()
                await asyncio.sleep(0)
                self.assertTrue(os.path.exists(claim_path(self.usage_dir, BOT_ID)))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        self.assertFalse(os.path.exists(claim_path(self.usage_dir, BOT_ID)))
        self.assertFalse(os.path.exists(sent_path(self.usage_dir, BOT_ID)))

    def test_a_stale_claim_from_a_dead_process_is_taken_over(self):
        self.assertTrue(claim_report(self.usage_dir, BOT_ID))
        self.assertFalse(claim_report(self.usage_dir, BOT_ID))
        later = time.time() + report_module.STALE_CLAIM_SECS + 1
        self.assertTrue(claim_report(self.usage_dir, BOT_ID, now=later))
        release_claim(self.usage_dir, BOT_ID)
        self.assertTrue(claim_report(self.usage_dir, BOT_ID))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.usage_dir = os.path.join(self.tmp.name, "usage")
        os.makedirs(self.usage_dir)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, usage: dict) -> None:
        with open(os.path.join(self.usage_dir, f"{name}.json"), "w") as handle:
            json.dump(usage, handle)

    def test_finds_finalised_ledgers_that_are_neither_done_nor_running(self):
        now = LEDGER["ended_at"] + 60
        self._write(BOT_ID, LEDGER)  # finalised, never delivered
        self._write(
            "client-1", {**LEDGER, "meetingbaas_bot_id": None, "ended_at": None}
        )
        self._write(
            "live-bot", {**LEDGER, "meetingbaas_bot_id": "live-bot", "ended_at": None}
        )
        self._write(
            "old-bot",
            {**LEDGER, "meetingbaas_bot_id": "old-bot", "ended_at": now - 10 * 86400},
        )
        self._write("done", {**LEDGER, "meetingbaas_bot_id": "done"})
        report_module.mark_sent(self.usage_dir, "done")
        self._write("running", {**LEDGER, "meetingbaas_bot_id": "running"})
        claim_report(self.usage_dir, "running")
        os.utime(claim_path(self.usage_dir, "running"), (now, now))
        # A claim left by a process that died mid-delivery
        self._write("stale", {**LEDGER, "meetingbaas_bot_id": "stale"})
        claim_report(self.usage_dir, "stale")
        stale_at = now - report_module.STALE_CLAIM_SECS - 1
        os.utime(claim_path(self.usage_dir, "stale"), (stale_at, stale_at))
        with open(os.path.join(self.usage_dir, "broken.json"), "w") as handle:
            handle.write("{not json")

        found = unreported_ledgers(self.usage_dir, now=now)
        self.assertEqual(
            sorted(u["meetingbaas_bot_id"] for u in found), sorted([BOT_ID, "stale"])
        )

    def test_replays_each_unreported_ledger_once(self):
        self._write(BOT_ID, LEDGER)
        self._write("second", {**LEDGER, "meetingbaas_bot_id": "second"})

        async def run():
            posted = mock.AsyncMock(return_value="delivered")
            with (
                mock.patch.dict(os.environ, {BOT_PROCESS_URL_ENV: "http://api.test"}),
                mock.patch.object(report_module, "post_report", posted),
                mock.patch.object(
                    report_module, "REPLAY_MAX_AGE_SECS", 10 * 365 * 86400.0
                ),
            ):
                tasks = await replay_unreported(self.usage_dir, log=self.logs.append)
                self.assertEqual(len(tasks), 2)
                await asyncio.gather(*tasks)
                # Delivered ledgers are marked done: a second pass sends nothing
                self.assertEqual(
                    await replay_unreported(self.usage_dir, log=self.logs.append), []
                )
                return posted.await_count

        self.assertEqual(asyncio.run(run()), 2)
        self.assertTrue(any("replaying 2" in line for line in self.logs))

    def test_replay_is_quiet_without_an_endpoint(self):
        self._write(BOT_ID, LEDGER)

        async def run():
            with mock.patch.dict(os.environ, {BOT_PROCESS_URL_ENV: ""}):
                return await replay_unreported(self.usage_dir, log=self.logs.append)

        self.assertEqual(asyncio.run(run()), [])
        self.assertEqual(self.logs, [])
