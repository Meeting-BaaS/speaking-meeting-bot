import os
import subprocess
import sys
import unittest

# scripts.meetingbaas registers the audio protobufs on import, which collides
# with the API's own registration inside one interpreter, so these run in a
# subprocess like the other runner tests.


def run_snippet(code: str, **env_overrides: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARTESIA_")}
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", "from scripts import meetingbaas as m\n" + code],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    return result.stdout.strip().splitlines()[-1]


class TtsConfigTest(unittest.TestCase):
    def test_volume_absent_everywhere_means_provider_default(self):
        self.assertEqual(run_snippet("print(m.resolve_tts_volume({}))"), "None")

    def test_volume_request_wins_over_env_and_clamps(self):
        out = run_snippet(
            "print(m.resolve_tts_volume({'speech_volume': 3.0}), "
            "m.resolve_tts_volume({'speech_volume': 0.1}), m.resolve_tts_volume({}))",
            CARTESIA_TTS_VOLUME="1.0",
        )
        self.assertEqual(out, "2.0 0.5 1.0")

    def test_emotion_absent_means_model_inferred_and_blank_is_none(self):
        self.assertEqual(
            run_snippet("print(m.resolve_tts_emotion({}), m.resolve_tts_emotion({'speech_emotion': '  '}))"),
            "None None",
        )

    def test_emotion_request_wins_over_env(self):
        out = run_snippet(
            "print(m.resolve_tts_emotion({'speech_emotion': 'excited'}), m.resolve_tts_emotion({}))",
            CARTESIA_TTS_EMOTION="calm",
        )
        self.assertEqual(out, "excited calm")

    def test_minimal_kwargs_leave_optional_generation_fields_unset(self):
        out = run_snippet(
            "s = m.build_cartesia_tts_kwargs(api_key='k', voice_id='v', sample_rate=16000, speed=1.2)['settings']\n"
            "d = s.generation_config.model_dump(exclude_unset=True)\n"
            "print(s.generation_config.speed, sorted(d))"
        )
        # volume/emotion must not be sent at all when unset: sonic-3.5+ infers
        # tone better than a forced neutral value would.
        self.assertEqual(out, "1.2 ['speed']")

    def test_full_kwargs_carry_volume_emotion_and_dictionary(self):
        out = run_snippet(
            "s = m.build_cartesia_tts_kwargs(api_key='k', voice_id='v', sample_rate=16000, speed=1.0, "
            "volume=1.5, emotion='excited', pronunciation_dict_id='dict_123')['settings']\n"
            "print(s.generation_config.volume, s.generation_config.emotion, s.pronunciation_dict_id)"
        )
        self.assertEqual(out, "1.5 excited dict_123")
