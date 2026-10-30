"""Rollout drain state.

A pod told to drain takes no new bots: /ready answers 503 so the API Service
drops it, and POST /bots refuses. Its live bots keep running; the rollout
janitor in the chart deletes the pod once none are left.
"""

import time

from core.connection import MEETING_DETAILS
from utils.pod_routing import pod_name

_draining_since: float | None = None


def start_draining() -> None:
    global _draining_since
    if _draining_since is None:
        _draining_since = time.time()


def is_draining() -> bool:
    return _draining_since is not None


def reset() -> None:
    """Tests only."""
    global _draining_since
    _draining_since = None


def live_bot_count() -> int:
    return len(MEETING_DETAILS)


def status() -> dict:
    return {
        "pod": pod_name(),
        "draining": is_draining(),
        "draining_since": _draining_since,
        "live_bots": live_bot_count(),
    }
