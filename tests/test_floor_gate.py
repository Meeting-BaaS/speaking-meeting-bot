import os
import subprocess
import sys
import unittest

# scripts.meetingbaas registers the audio protobufs on import, which collides
# with the API's own registration inside one interpreter, so the gate is
# exercised in a subprocess like the other runner tests.
SNIPPET = """
from unittest import mock
from scripts import meetingbaas

gate = meetingbaas.FloorGate(meeting_url="https://meet.google.com/abc", my_name="Vibe")
clock = [100.0]
with mock.patch.object(meetingbaas.time, "monotonic", side_effect=lambda: clock[0]), \\
     mock.patch.object(meetingbaas, "floor_blocked_by_sibling", return_value=True) as read:
    assert gate._blocked() is True
    assert gate._blocked() is True
    assert read.call_count == 1, read.call_count
    clock[0] += meetingbaas.FloorGate.CHECK_INTERVAL_SECS + 0.01
    read.return_value = False
    assert gate._blocked() is False
    assert read.call_count == 2, read.call_count

with mock.patch.object(meetingbaas, "floor_blocked_by_sibling", side_effect=OSError("boom")):
    gate2 = meetingbaas.FloorGate(meeting_url="https://meet.google.com/abc", my_name="Vibe")
    assert gate2._blocked() is False
print("verified")
"""


class FloorGateCacheTest(unittest.TestCase):
    def test_floor_file_is_read_at_most_once_per_interval_and_errors_never_block(self):
        result = subprocess.run(
            [sys.executable, "-c", SNIPPET],
            check=True,
            capture_output=True,
            env=os.environ.copy(),
            text=True,
        )
        self.assertEqual(result.stdout.strip().splitlines()[-1], "verified")
