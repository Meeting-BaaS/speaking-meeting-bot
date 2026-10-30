import os
import tempfile
import unittest

from pipecat.metrics.metrics import (
    LLMTokenUsage,
    LLMUsageMetricsData,
    STTUsage,
    STTUsageMetricsData,
    TTSUsageMetricsData,
)

from utils.usage_meter import (
    KEY_SOURCE_CALLER,
    KEY_SOURCE_PLATFORM,
    UsageLedger,
    key_sources,
    load_usage,
    save_usage,
)


class UsageLedgerTest(unittest.TestCase):
    def test_folds_every_provider_metric_and_ignores_the_rest(self):
        ledger = UsageLedger(bot_id="b1")
        self.assertTrue(
            ledger.record(STTUsageMetricsData(processor="stt", model="nova-3", value=STTUsage(audio_seconds=12.5)))
        )
        self.assertTrue(ledger.record(STTUsageMetricsData(processor="stt", value=STTUsage(audio_seconds=2.5))))
        self.assertTrue(ledger.record(TTSUsageMetricsData(processor="tts", model="sonic-3", value=40)))
        self.assertTrue(
            ledger.record(
                LLMUsageMetricsData(
                    processor="llm",
                    model="gpt-5.4-mini",
                    value=LLMTokenUsage(
                        prompt_tokens=1000,
                        completion_tokens=50,
                        total_tokens=1050,
                        cache_read_input_tokens=800,
                    ),
                )
            )
        )
        self.assertFalse(ledger.record(object()))

        report = ledger.to_dict()
        self.assertEqual(report["stt_audio_seconds"], 15.0)
        self.assertEqual(report["tts_characters"], 40)
        self.assertEqual(report["tts_requests"], 1)
        self.assertEqual(report["llm_turns"], 1)
        self.assertEqual(report["llm_prompt_tokens"], 1000)
        self.assertEqual(report["llm_cached_prompt_tokens"], 800)
        self.assertEqual(report["llm_completion_tokens"], 50)
        self.assertEqual(report["llm_total_tokens"], 1050)
        self.assertEqual(report["models_seen"], {"stt": "nova-3", "tts": "sonic-3", "llm": "gpt-5.4-mini"})
        self.assertEqual(report["llm_model"], "gpt-5.4-mini")
        self.assertGreaterEqual(report["duration_seconds"], 0)

    def test_save_and_load_round_trip_under_either_id(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = UsageLedger(bot_id="client-1", key_sources=key_sources({"llm_api_key": "k"}))
            path = ledger.save(directory)
            self.assertTrue(path and os.path.exists(path))
            loaded = load_usage(directory, "missing", "client-1")
            self.assertEqual(loaded["bot_id"], "client-1")
            self.assertEqual(loaded["key_sources"]["llm"], KEY_SOURCE_CALLER)
            self.assertEqual(loaded["key_sources"]["stt"], KEY_SOURCE_PLATFORM)
            self.assertIsNone(load_usage(directory, "missing", None))
            save_usage(directory, "mb-1", {**loaded, "meetingbaas_bot_id": "mb-1"})
            self.assertEqual(load_usage(directory, "mb-1")["meetingbaas_bot_id"], "mb-1")

    def test_key_sources_default_to_platform(self):
        self.assertEqual(
            key_sources(None),
            {"llm": KEY_SOURCE_PLATFORM, "stt": KEY_SOURCE_PLATFORM, "tts": KEY_SOURCE_PLATFORM},
        )
        self.assertEqual(key_sources({"tts_api_key": "x", "callback_secret": "s"})["tts"], KEY_SOURCE_CALLER)
