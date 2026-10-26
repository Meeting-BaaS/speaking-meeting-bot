import os
import tempfile
import time
import unittest
from unittest import mock

from utils import speakers
from utils.speakers import read_recent_speakers, write_speakers

MEETING = "https://meet.google.com/abc-defg-hij"


class SpeakersFileTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ, {"SPEAKING_BOT_STATE_DIR": self._dir.name}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._dir.cleanup()

    def test_round_trip(self):
        write_speakers(MEETING, ["Cyril Mouas", "Lazare Rossillon"])
        self.assertEqual(
            read_recent_speakers(MEETING), ["Cyril Mouas", "Lazare Rossillon"]
        )

    def test_missing_file_is_empty(self):
        self.assertEqual(read_recent_speakers(MEETING), [])

    def test_stale_record_is_empty(self):
        write_speakers(MEETING, ["Cyril"])
        with mock.patch.object(
            speakers.time, "time", return_value=time.time() + 60
        ):
            self.assertEqual(read_recent_speakers(MEETING), [])

    def test_garbage_entries_are_dropped(self):
        write_speakers(MEETING, ["Cyril"])
        # Corrupt the file by hand: non-string entries must be ignored.
        path = speakers._speakers_path(MEETING)
        with open(path, "w") as f:
            f.write('{"speakers": ["Ok", 42, "  "], "ts": %f}' % time.time())
        self.assertEqual(read_recent_speakers(MEETING), ["Ok"])

    def test_corrupt_json_is_empty(self):
        path = speakers._speakers_path(MEETING)
        with open(path, "w") as f:
            f.write("{nope")
        self.assertEqual(read_recent_speakers(MEETING), [])


if __name__ == "__main__":
    unittest.main()
