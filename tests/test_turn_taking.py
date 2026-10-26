import unittest

from utils.turn_taking import TurnTaking, resolve_turn_taking


class TurnTakingTest(unittest.TestCase):
    def test_defaults_are_the_fast_ones(self):
        self.assertEqual(
            resolve_turn_taking(None, env={}),
            TurnTaking(
                confidence=0.5,
                start_secs=0.2,
                stop_secs=0.2,
                min_volume=0.6,
                unfinished_pause_secs=1.5,
                min_interrupt_words=3,
            ),
        )

    def test_min_interrupt_words_request_env_and_floor(self):
        self.assertEqual(
            resolve_turn_taking({"min_interrupt_words": 5}, env={}).min_interrupt_words, 5
        )
        self.assertEqual(
            resolve_turn_taking(None, env={"TURN_MIN_INTERRUPT_WORDS": "1"}).min_interrupt_words,
            1,
        )
        # 0 is a deliberate "interrupt on any speech"; negatives clamp to 0.
        self.assertEqual(
            resolve_turn_taking({"min_interrupt_words": 0}, env={}).min_interrupt_words, 0
        )
        self.assertEqual(
            resolve_turn_taking({"min_interrupt_words": -2}, env={}).min_interrupt_words, 0
        )

    def test_env_overrides_defaults_and_request_overrides_env(self):
        env = {"VAD_STOP_SECS": "0.5", "TURN_UNFINISHED_PAUSE_SECS": "3", "VAD_START_SECS": ""}
        turn = resolve_turn_taking({"stop_secs": 0.3, "confidence": "0.7"}, env=env)
        self.assertEqual(turn.stop_secs, 0.3)
        self.assertEqual(turn.unfinished_pause_secs, 3.0)
        self.assertEqual(turn.confidence, 0.7)
        self.assertEqual(turn.start_secs, 0.2)

    def test_unfinished_pause_never_shorter_than_the_vad_stop(self):
        turn = resolve_turn_taking({"stop_secs": 2.0, "unfinished_pause_secs": 0.5}, env={})
        self.assertEqual(turn.unfinished_pause_secs, 2.0)
