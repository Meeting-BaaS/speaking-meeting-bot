# app/services/debug_recorder.py
# Change #17: Replayable Debug Recording per session
"""
Saves per-session debug artifacts for replaying realtime AI voice bugs:
    session_{id}/
    ├── input.raw        - raw PCM16 audio received from MeetingBaas
    ├── transcript.jsonl - live transcription lines (one JSON per line)
    ├── llm.jsonl        - LLM request/response pairs
    └── tts.raw          - TTS audio bytes returned to the meeting

Only active when the env var DEBUG_RECORDING=true is set.
Usage:
    from app.services.debug_recorder import recorder
    recorder.write_audio("input", client_id, audio_bytes)
    recorder.write_event("transcript", client_id, {"speaker": "User", "text": "..."})
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import logging
_log = logging.getLogger("debug_recorder")

_ENABLED = os.getenv("DEBUG_RECORDING", "false").lower() == "true"
_BASE_DIR = Path(os.getenv("DEBUG_RECORDING_DIR", "debug_recordings"))


class DebugRecorder:
    """Writes debug artifacts to disk per session when enabled."""

    def __init__(self) -> None:
        self.enabled = _ENABLED
        if self.enabled:
            _BASE_DIR.mkdir(parents=True, exist_ok=True)
            _log.info(f"[debug_recorder] Recording enabled → {_BASE_DIR.resolve()}")

    def _session_dir(self, client_id: str) -> Path:
        d = _BASE_DIR / f"session_{client_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_audio(self, track: str, client_id: str, audio_bytes: bytes) -> None:
        """Append raw PCM audio bytes to a per-session file (input or tts)."""
        if not self.enabled:
            return
        path = self._session_dir(client_id) / f"{track}.raw"
        try:
            with open(path, "ab") as f:
                f.write(audio_bytes)
        except Exception as e:
            _log.warning(f"[debug_recorder] Failed to write audio: {e}")

    def write_event(self, track: str, client_id: str, payload: Dict[str, Any]) -> None:
        """Append a JSON event line (transcript or llm) to a per-session .jsonl file."""
        if not self.enabled:
            return
        path = self._session_dir(client_id) / f"{track}.jsonl"
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            _log.warning(f"[debug_recorder] Failed to write event: {e}")

    def finalize(self, client_id: str) -> None:
        """Write a session summary manifest on cleanup."""
        if not self.enabled:
            return
        d = self._session_dir(client_id)
        manifest = {
            "client_id": client_id,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "files": [f.name for f in d.iterdir() if f.is_file()],
        }
        try:
            (d / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            _log.info(f"[debug_recorder] Finalized session {client_id} → {d}")
        except Exception as e:
            _log.warning(f"[debug_recorder] Failed to finalize: {e}")


# Singleton
recorder = DebugRecorder()
