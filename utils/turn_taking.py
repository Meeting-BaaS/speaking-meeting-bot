"""Turn-taking numbers for a bot: how fast it decides a speaker has finished."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Pipecat's own VAD defaults (0.2 s either way) are tuned for exactly this
# setup: a local end-of-turn model decides whether the speaker is done, so the
# VAD only has to notice the pause, not prove it. The previous 0.8 s stop made
# every reply wait 0.6 s longer than it had to.
DEFAULT_CONFIDENCE = 0.5
DEFAULT_START_SECS = 0.2
DEFAULT_STOP_SECS = 0.2
DEFAULT_MIN_VOLUME = 0.6
# When the end-of-turn model judges the speaker mid-thought, how much silence
# ends the turn anyway. Pipecat's 3 s reads as the bot ignoring you; people
# give each other about a second and a half.
DEFAULT_UNFINISHED_PAUSE_SECS = 1.5
# Words required before speech counts as an interruption while the bot is
# talking. In a multi-party meeting coughs, "uh-huh"s and cross-talk barge the
# bot constantly at 0; three words is the documented threshold for separating
# a real interjection from a backchannel.
DEFAULT_MIN_INTERRUPT_WORDS = 3


@dataclass(frozen=True)
class TurnTaking:
    confidence: float
    start_secs: float
    stop_secs: float
    min_volume: float
    unfinished_pause_secs: float
    min_interrupt_words: int


def resolve_turn_taking(
    turn_config: Mapping[str, object] | None, env: Mapping[str, str] | None = None
) -> TurnTaking:
    """Per-request turn_config > VAD_* / TURN_* environment > defaults.

    The unfinished pause can never be shorter than the VAD stop: the model only
    gets to judge the turn once the VAD has reported the pause.
    """
    env = os.environ if env is None else env
    turn_config = turn_config or {}

    def pick(key: str, env_key: str, default: float) -> float:
        value = turn_config.get(key)
        if value is None:
            value = env.get(env_key)
        if value is None or value == "":
            return float(default)
        return float(value)

    stop_secs = pick("stop_secs", "VAD_STOP_SECS", DEFAULT_STOP_SECS)
    unfinished = pick(
        "unfinished_pause_secs", "TURN_UNFINISHED_PAUSE_SECS", DEFAULT_UNFINISHED_PAUSE_SECS
    )
    return TurnTaking(
        confidence=pick("confidence", "VAD_CONFIDENCE", DEFAULT_CONFIDENCE),
        start_secs=pick("start_secs", "VAD_START_SECS", DEFAULT_START_SECS),
        stop_secs=stop_secs,
        min_volume=pick("min_volume", "VAD_MIN_VOLUME", DEFAULT_MIN_VOLUME),
        unfinished_pause_secs=max(unfinished, stop_secs),
        min_interrupt_words=max(
            0,
            int(
                pick(
                    "min_interrupt_words",
                    "TURN_MIN_INTERRUPT_WORDS",
                    DEFAULT_MIN_INTERRUPT_WORDS,
                )
            ),
        ),
    )
