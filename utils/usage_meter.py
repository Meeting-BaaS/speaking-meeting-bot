"""Per-bot provider usage, taken from Pipecat's own usage metrics.

Every service in the pipeline reports what it consumed as a ``MetricsFrame``:
Deepgram the seconds of audio it was sent, Cartesia the characters it spoke,
the LLM its token counts. ``UsageMeter`` sits at the end of the pipeline,
where every frame passes, and adds them up into a ``UsageLedger`` that is
written next to the transcript. The API reads it to invoice a bot that ran
on the server's provider keys and to report one that ran on the caller's.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    STTUsageMetricsData,
    TTSUsageMetricsData,
)
from pipecat.processors.frame_processor import FrameProcessor

USAGE_DIRNAME = "usage"

# Who paid for each provider: "platform" when the bot ran on the server's key,
# "caller" when the request brought its own.
KEY_SOURCE_PLATFORM = "platform"
KEY_SOURCE_CALLER = "caller"


def key_sources(credentials: dict[str, Any] | None) -> dict[str, str]:
    """Which key each provider ran on, from the credentials a request brought."""
    credentials = credentials or {}
    return {
        "llm": KEY_SOURCE_CALLER if credentials.get("llm_api_key") else KEY_SOURCE_PLATFORM,
        "stt": KEY_SOURCE_CALLER if credentials.get("stt_api_key") else KEY_SOURCE_PLATFORM,
        "tts": KEY_SOURCE_CALLER if credentials.get("tts_api_key") else KEY_SOURCE_PLATFORM,
    }


@dataclass
class UsageLedger:
    """Running totals for one bot, serialisable as the usage report."""

    bot_id: str
    meetingbaas_bot_id: str | None = None
    stt_provider: str = "deepgram"
    tts_provider: str = "cartesia"
    llm_provider: str | None = None
    llm_model: str | None = None
    key_sources: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    stt_audio_seconds: float = 0.0
    tts_characters: int = 0
    tts_requests: int = 0
    llm_turns: int = 0
    llm_prompt_tokens: int = 0
    llm_cached_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_total_tokens: int = 0
    models_seen: dict[str, str] = field(default_factory=dict)

    def record(self, data: Any) -> bool:
        """Fold one metrics entry in; True when it was a usage entry."""
        model = getattr(data, "model", None)
        if isinstance(data, STTUsageMetricsData):
            self.stt_audio_seconds += float(data.value.audio_seconds)
            if model:
                self.models_seen["stt"] = model
            return True
        if isinstance(data, TTSUsageMetricsData):
            self.tts_characters += int(data.value)
            self.tts_requests += 1
            if model:
                self.models_seen["tts"] = model
            return True
        if isinstance(data, LLMUsageMetricsData):
            usage = data.value
            self.llm_turns += 1
            self.llm_prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.llm_cached_prompt_tokens += int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            )
            self.llm_completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            self.llm_total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
            if model:
                self.models_seen["llm"] = model
                self.llm_model = self.llm_model or model
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        end = self.ended_at or time.time()
        data["duration_seconds"] = round(max(0.0, end - self.started_at), 3)
        data["stt_audio_seconds"] = round(self.stt_audio_seconds, 3)
        return data

    def save(self, directory: str) -> str | None:
        """Atomically write ``<directory>/<bot_id>.json``; None when it failed."""
        return save_usage(directory, self.bot_id, self.to_dict())


def usage_dir(state_dir: str) -> str:
    return os.path.join(state_dir, USAGE_DIRNAME)


def usage_path(state_dir: str, bot_id: str) -> str:
    return os.path.join(usage_dir(state_dir), f"{bot_id}.json")


def save_usage(directory: str, bot_id: str, report: dict[str, Any]) -> str | None:
    """Write a usage report atomically (the API may read it at any moment)."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{bot_id}.json")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f"{bot_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(report, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return None


def load_usage(directory: str, *bot_ids: str | None) -> dict[str, Any] | None:
    """The first readable usage report among the given ids, or None."""
    for bot_id in bot_ids:
        if not bot_id:
            continue
        path = os.path.join(directory, f"{bot_id}.json")
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            continue
    return None


class UsageMeter(FrameProcessor):
    """Adds every usage metric that passes into the ledger, then lets it go on.

    Placed last in the pipeline (just before the transport output) so it sees
    the metrics of every service upstream. Never raises: a bookkeeping error
    must not take the call down.
    """

    def __init__(self, ledger: UsageLedger, **kwargs):
        super().__init__(**kwargs)
        self.ledger = ledger

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for entry in frame.data or ():
                with contextlib.suppress(Exception):
                    self.ledger.record(entry)
        await self.push_frame(frame, direction)
