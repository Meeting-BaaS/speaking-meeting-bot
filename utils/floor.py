"""Speaking-floor coordination between our own bots in the same meeting.

When the API puts several bots into one meeting, each bot's VAD hears the
others as "a user" and they barge in on each other. MeetingBaas streams
speaker-state updates (who is speaking right now) to the API process, which
records "one of OUR bots holds the floor" in a small per-meeting JSON file.
Each Pipecat child polls that file and holds its TTS output while a sibling
bot is speaking.

File-based on purpose: the children are separate processes that already share
the state dir (ready_signals/ uses the same pattern), and floor changes are
seconds-scale — poll latency is fine.
"""

import hashlib
import json
import os
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from utils.runtime import get_state_dir

# A floor claim older than this is treated as free: the writer (the API
# process) refreshes it on every speaker-state update while someone speaks.
FLOOR_STALE_SECS = 10.0


def floor_key(meeting_url: str) -> str:
    """Stable key for a meeting, ignoring query params.

    Two bots are often sent with URL variants of the same room (e.g. with and
    without ?authuser=0) to satisfy MeetingBaas' per-URL dedup — they must
    still share one floor.
    """
    parsed = urlparse(meeting_url or "")
    return hashlib.sha1(f"{parsed.netloc}{parsed.path}".lower().encode()).hexdigest()[:12]


def floor_file(meeting_url: str) -> str:
    floor_dir = os.path.join(get_state_dir(), "floor")
    os.makedirs(floor_dir, exist_ok=True)
    return os.path.join(floor_dir, f"{floor_key(meeting_url)}.json")


def write_floor(meeting_url: str, speaker: Optional[str]) -> None:
    """Record which of our bots (by display name) holds the floor, or None."""
    path = floor_file(meeting_url)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"speaker": speaker, "ts": time.time()}, f)
    os.replace(tmp, path)


def read_floor(meeting_url: str) -> Tuple[Optional[str], float]:
    """Return (speaker, age_seconds); (None, inf) when unset or stale."""
    try:
        with open(floor_file(meeting_url)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, float("inf")
    age = time.time() - float(data.get("ts", 0))
    if age > FLOOR_STALE_SECS:
        return None, age
    return data.get("speaker"), age


def floor_blocked_by_sibling(meeting_url: str, my_name: str) -> bool:
    """True while another one of our bots holds the floor in this meeting."""
    speaker, _ = read_floor(meeting_url)
    return bool(speaker) and speaker != my_name
