"""Observe incoming speech independently of generated conversation messages."""

import time

from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from utils.idle_reengagement import IdleReengagementBudget


class SpeechActivityTracker(FrameProcessor):
    """Reset idle nudges only for final speech received from STT."""

    def __init__(self, budget: IdleReengagementBudget):
        super().__init__()
        self.budget = budget

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TranscriptionFrame)
            and frame.text.strip()
        ):
            self.budget.record_speech(time.monotonic())
        await self.push_frame(frame, direction)
