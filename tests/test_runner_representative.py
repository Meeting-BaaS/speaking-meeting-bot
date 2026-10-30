"""The runner's representative pieces, exercised in a subprocess like the other runner tests."""

import os
import subprocess
import sys
import unittest

SNIPPET = """
import asyncio
from unittest import mock
from pipecat.frames.frames import TTSStartedFrame, TTSStoppedFrame, TTSTextFrame
from scripts import meetingbaas as m

class Sink:
    enabled = True
    def __init__(self):
        self.events = []
    async def emit(self, event, data):
        self.events.append((event, data))

async def check():
    sink = Sink()
    tap = m.UtteranceTap(sink)
    tap.push_frame = mock.AsyncMock()
    tap.COALESCE_SECS = 0.05
    for frame in (TTSStartedFrame(), TTSTextFrame(text="Hello there.", aggregated_by="sentence"), TTSStoppedFrame(),
                  TTSStartedFrame(), TTSTextFrame(text="I am Ada's representative.", aggregated_by="sentence"), TTSStoppedFrame()):
        await tap.process_frame(frame, m.FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.01)
    assert sink.events == [], sink.events
    await asyncio.sleep(0.15)
    assert len(sink.events) == 1, sink.events
    assert sink.events[0][1]["text"] == "Hello there. I am Ada's representative.", sink.events
    assert tap.push_frame.await_count == 6
    await tap.flush()
    assert len(sink.events) == 1

    meter = m.UsageMeter(m.UsageLedger(bot_id="b"))
    meter.push_frame = mock.AsyncMock()
    from pipecat.frames.frames import MetricsFrame
    from pipecat.metrics.metrics import TTSUsageMetricsData
    await meter.process_frame(MetricsFrame(data=[TTSUsageMetricsData(processor="tts", value=7)]), m.FrameDirection.DOWNSTREAM)
    assert meter.ledger.tts_characters == 7
    assert meter.push_frame.await_count == 1

    assert m.max_nudges_for("always", 3, represents_owner=True) == 0
    assert m.max_nudges_for("always", 3) == 3
    print("verified")

asyncio.run(check())
"""


class RunnerRepresentativeTest(unittest.TestCase):
    def test_utterance_tap_coalesces_a_reply_and_usage_meter_counts(self):
        result = subprocess.run(
            [sys.executable, "-c", SNIPPET],
            capture_output=True,
            env=os.environ.copy(),
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertEqual(result.stdout.strip().splitlines()[-1], "verified")
