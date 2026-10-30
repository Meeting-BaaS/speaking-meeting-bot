import unittest
from unittest import mock

from app import websockets

MEETING = "https://meet.google.com/abc-defg-hij"


class RecordSpeakersTest(unittest.TestCase):
    def setUp(self):
        websockets._speakers_written.clear()
        self.clock = [100.0]
        self.writes = []
        self.patches = [
            mock.patch.object(websockets.time, "monotonic", side_effect=lambda: self.clock[0]),
            mock.patch.object(
                websockets, "write_speakers", side_effect=lambda url, names: self.writes.append(list(names))
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        websockets._speakers_written.clear()

    def record(self, names, advance=0.0):
        self.clock[0] += advance
        websockets._record_speakers(MEETING, "k", names)

    def test_change_then_steady_speech_then_silence(self):
        self.record(["Alice"])                 # change: written
        self.record(["Alice"], advance=0.3)    # same people, too soon: no write
        self.record(["Alice"], advance=0.8)    # refresh after a second
        self.record([], advance=0.2)           # speech ends: written once more
        self.record([], advance=0.5)           # more silence: nothing
        self.record([], advance=5.0)
        self.assertEqual(self.writes, [["Alice"], ["Alice"], ["Alice"]])

    def test_new_speaker_is_written_immediately(self):
        self.record(["Alice"])
        self.record(["Alice", "Bob"], advance=0.1)
        self.assertEqual(self.writes, [["Alice"], ["Alice", "Bob"]])

    def test_silence_before_any_speech_writes_nothing(self):
        self.record([])
        self.assertEqual(self.writes, [])
