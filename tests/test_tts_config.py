import os
import unittest
from unittest import mock

from scripts.meetingbaas import (
    build_cartesia_tts_kwargs,
    resolve_tts_emotion,
    resolve_tts_volume,
)


class ResolveVolumeTest(unittest.TestCase):
    def test_absent_everywhere_means_provider_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(resolve_tts_volume({}))

    def test_request_wins_over_env_and_clamps(self):
        with mock.patch.dict(os.environ, {"CARTESIA_TTS_VOLUME": "1.0"}):
            self.assertEqual(resolve_tts_volume({"speech_volume": 3.0}), 2.0)
            self.assertEqual(resolve_tts_volume({"speech_volume": 0.1}), 0.5)
            self.assertEqual(resolve_tts_volume({}), 1.0)


class ResolveEmotionTest(unittest.TestCase):
    def test_absent_means_model_inferred(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(resolve_tts_emotion({}))

    def test_request_wins_and_blank_is_none(self):
        with mock.patch.dict(os.environ, {"CARTESIA_TTS_EMOTION": "calm"}):
            self.assertEqual(resolve_tts_emotion({"speech_emotion": "excited"}), "excited")
            self.assertEqual(resolve_tts_emotion({}), "calm")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(resolve_tts_emotion({"speech_emotion": "  "}))


class CartesiaKwargsTest(unittest.TestCase):
    def test_minimal_kwargs_leave_optional_generation_fields_unset(self):
        kwargs = build_cartesia_tts_kwargs(
            api_key="k", voice_id="v", sample_rate=16000, speed=1.2
        )
        settings = kwargs["settings"]
        self.assertEqual(settings.generation_config.speed, 1.2)
        # volume/emotion must not be sent at all when unset — sonic-3.5+ infers
        # tone better than a forced neutral value would.
        dumped = settings.generation_config.model_dump(exclude_unset=True)
        self.assertNotIn("volume", dumped)
        self.assertNotIn("emotion", dumped)

    def test_full_kwargs_carry_volume_emotion_and_dictionary(self):
        kwargs = build_cartesia_tts_kwargs(
            api_key="k",
            voice_id="v",
            sample_rate=16000,
            speed=1.0,
            volume=1.5,
            emotion="excited",
            pronunciation_dict_id="dict_123",
        )
        settings = kwargs["settings"]
        self.assertEqual(settings.generation_config.volume, 1.5)
        self.assertEqual(settings.generation_config.emotion, "excited")
        self.assertEqual(settings.pronunciation_dict_id, "dict_123")


if __name__ == "__main__":
    unittest.main()
