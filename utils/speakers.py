"""Who is speaking in the meeting right now — humans included.

MeetingBaas streams per-participant speaker state (name + isSpeaking) to the
API process. utils/floor.py distills that into "one of OUR bots holds the
floor"; this module records the rest of it: the names of EVERY participant
currently speaking, in a small per-meeting JSON file the Pipecat children can
poll.

The children use it to label transcriptions with the speaker's name before
the LLM sees them ("Cyril: can you summarize?"), which is what lets the model
tell "they are talking to each other" from "that was addressed to me" in a
multi-party meeting.

Same file-based pattern as floor.py on purpose: separate processes, shared
state dir, seconds-scale freshness is fine.
"""

import contextlib
import json
import os
import tempfile
import time

from utils.floor import floor_key
from utils.runtime import get_state_dir

# A speakers record older than this says nothing about who is talking now.
# Speaker-state updates arrive on every change, so staleness means silence
# (or a dropped socket) — either way, don't attribute.
SPEAKERS_STALE_SECS = 3.0


def _speakers_path(meeting_url: str) -> str:
    speakers_dir = os.path.join(get_state_dir(), "speakers")
    os.makedirs(speakers_dir, exist_ok=True)
    return os.path.join(speakers_dir, f"{floor_key(meeting_url)}.json")


def write_speakers(meeting_url: str, names: list[str]) -> None:
    """Record who is speaking right now (atomic, per-writer temp file)."""
    path = _speakers_path(meeting_url)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"speakers": names, "ts": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def read_recent_speakers(
    meeting_url: str, max_age_secs: float = SPEAKERS_STALE_SECS
) -> list[str]:
    """Names seen speaking within max_age_secs; [] when stale, missing, or bad."""
    try:
        with open(_speakers_path(meeting_url)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    # Valid JSON is not the same as a valid record: a list, or a null or
    # string timestamp, must read as "nothing known" rather than raise.
    if not isinstance(data, dict):
        return []
    ts = data.get("ts")
    if not isinstance(ts, (int, float)) or time.time() - ts > max_age_secs:
        return []
    speakers = data.get("speakers")
    if not isinstance(speakers, list):
        return []
    return [s for s in speakers if isinstance(s, str) and s.strip()]
