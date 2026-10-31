"""What the bot has heard, whether or not it was addressed.

In ``respond_mode: mentioned`` the wake-phrase strategy discards every final
transcription that does not contain the bot's name, so the model only ever
saw the sentences that addressed it and could not answer "what did we agree
on". This memory keeps every final transcription (and the bot's own turns)
with a timestamp. When the bot is addressed, the turns heard since the last
time it spoke are prepended to the addressed sentence, so the model answers
with the meeting in view. The full log also goes into the transcript file
and the briefing, which previously held only the addressed turns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# What is handed to the model at wake time: enough for "what was just decided",
# small enough not to bloat every turn.
MAX_PENDING_CHARS = 4_000
MAX_PENDING_ENTRIES = 40
# The full log kept for the transcript and briefing.
MAX_LOG_ENTRIES = 4_000


@dataclass
class HeardEntry:
    at: float
    text: str
    kind: str = "human"  # or "bot"


@dataclass
class MeetingMemory:
    log: list[HeardEntry] = field(default_factory=list)
    _pending_from: int = 0

    def add(self, text: str, *, kind: str = "human", at: float | None = None) -> None:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        self.log.append(HeardEntry(at=at if at is not None else time.time(), text=cleaned, kind=kind))
        if len(self.log) > MAX_LOG_ENTRIES:
            overflow = len(self.log) - MAX_LOG_ENTRIES
            del self.log[:overflow]
            self._pending_from = max(0, self._pending_from - overflow)

    def pending(self) -> list[HeardEntry]:
        """Human turns heard since the bot last spoke or was last briefed."""
        entries = [e for e in self.log[self._pending_from :] if e.kind == "human"]
        entries = entries[-MAX_PENDING_ENTRIES:]
        total = 0
        kept: list[HeardEntry] = []
        for entry in reversed(entries):
            total += len(entry.text) + 1
            if total > MAX_PENDING_CHARS:
                break
            kept.append(entry)
        return list(reversed(kept))

    def mark_delivered(self) -> None:
        """The pending turns are now in the model's context."""
        self._pending_from = len(self.log)

    def wake_text(self, addressed: str) -> str:
        """The addressed sentence, preceded by what was said since the bot last spoke."""
        pending = self.pending()
        self.mark_delivered()
        if not pending:
            return addressed
        earlier = "\n".join(e.text for e in pending)
        return (
            "Said in the meeting since you last spoke (not addressed to you):\n"
            f"{earlier}\n\n"
            f"Now said to you:\n{addressed}"
        )

    def as_records(self) -> list[dict]:
        return [{"at": e.at, "kind": e.kind, "text": e.text} for e in self.log]
