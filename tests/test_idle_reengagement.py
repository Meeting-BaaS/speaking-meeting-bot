import unittest

from utils.idle_reengagement import IdleReengagementBudget, max_nudges_for


class IdleReengagementTest(unittest.TestCase):
    def test_silent_call_stops_after_three_nudges(self):
        budget = IdleReengagementBudget(15, 3, 0)
        sent = [now for now in range(2, 1000, 2) if budget.take_nudge(now)]
        self.assertEqual(sent, [16, 32, 48])

    def test_new_speech_replenishes_exhausted_budget(self):
        budget = IdleReengagementBudget(15, 3, 0)
        for now in (15, 30, 45):
            self.assertTrue(budget.take_nudge(now))
        self.assertFalse(budget.take_nudge(100))
        budget.record_speech(101)
        self.assertFalse(budget.take_nudge(115))
        self.assertTrue(budget.take_nudge(116))
        self.assertEqual(budget.nudges, 1)

    def test_speech_postpones_pending_nudge(self):
        budget = IdleReengagementBudget(15, 3, 0)
        budget.record_speech(14)
        self.assertFalse(budget.take_nudge(15))
        self.assertTrue(budget.take_nudge(29))

    def test_zero_budget_disables_nudges(self):
        budget = IdleReengagementBudget(15, 0, 0)
        self.assertFalse(budget.take_nudge(100))
        budget.record_speech(101)
        self.assertFalse(budget.take_nudge(200))


class SpeechActivityTrackerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from utils.speech_activity import SpeechActivityTracker
        from unittest.mock import AsyncMock

        self.budget = IdleReengagementBudget(15, 3, 0, nudges=3)
        self.tracker = SpeechActivityTracker(self.budget)
        self.tracker.push_frame = AsyncMock()

    async def test_final_external_speech_resets_budget_and_forwards_frame(self):
        from pipecat.frames.frames import TranscriptionFrame
        from pipecat.processors.frame_processor import FrameDirection

        frame = TranscriptionFrame("Hello", "participant", "2026-10-20T12:00:00Z")
        await self.tracker.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.budget.nudges, 0)
        self.tracker.push_frame.assert_awaited_once_with(
            frame, FrameDirection.DOWNSTREAM
        )

    async def test_synthetic_prompts_and_bot_replies_do_not_reset_budget(self):
        from pipecat.frames.frames import LLMMessagesAppendFrame, TextFrame
        from pipecat.processors.frame_processor import FrameDirection

        for frame in (
            LLMMessagesAppendFrame(
                [{"role": "user", "content": "Keep going"}], run_llm=True
            ),
            TextFrame("I am still here"),
        ):
            await self.tracker.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.budget.nudges, 3)
        self.assertFalse(self.budget.take_nudge(1000))

    async def test_empty_and_interim_transcriptions_do_not_reset_budget(self):
        from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
        from pipecat.processors.frame_processor import FrameDirection

        for frame in (
            TranscriptionFrame("  ", "participant", "2026-10-20T12:00:00Z"),
            InterimTranscriptionFrame("Partial", "participant", "2026-10-20T12:00:00Z"),
        ):
            await self.tracker.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.budget.nudges, 3)


class MaxNudgesForTest(unittest.TestCase):
    def test_mentioned_mode_never_nudges(self):
        self.assertEqual(max_nudges_for("mentioned", 3), 0)
        self.assertEqual(max_nudges_for("Mentioned", 3), 0)

    def test_always_mode_keeps_the_configured_budget(self):
        self.assertEqual(max_nudges_for("always", 3), 3)
        self.assertEqual(max_nudges_for("", 2), 2)
        self.assertEqual(max_nudges_for("always", -1), 0)
