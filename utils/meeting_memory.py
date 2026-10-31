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
MAX_PENDING_CHARS = 12_000
MAX_PENDING_ENTRIES = 200
# One final transcription is a sentence or two; anything longer is a runaway
# recogniser output and is cut here so no single turn can crowd the record.
MAX_ENTRY_CHARS = 2_000

# The full log kept for the transcript and briefing. Bounded only against a
# runaway process: a day of continuous speech is a few thousand turns, and the
# export must keep a meeting's opening, so nothing a real meeting produces is
# ever dropped.
MAX_LOG_ENTRIES = 50_000


@dataclass
class HeardEntry:
    """One thing said in the meeting.

    Attributes:
        at: Unix time in seconds (``time.time()``) when it was said; for a bot
            turn, when the bot started speaking.
        text: The words, with the speaker label the pipeline attached
            (``Name: text``) when it knew who spoke.
        kind: ``"human"`` for a transcription, ``"bot"`` for the bot's own turn.
    """

    at: float
    text: str
    kind: str = "human"


@dataclass
class MeetingMemory:
    """Everything heard in one meeting, and what the model has not seen yet.

    Attributes:
        log: Every entry in arrival order, bounded to ``MAX_LOG_ENTRIES``.
    """

    log: list[HeardEntry] = field(default_factory=list)
    _pending_from: int = 0

    def add(self, text: str, *, kind: str = "human", at: float | None = None) -> None:
        """Record one thing said.

        Args:
            text: The words, with the speaker label when known. Blank or
                whitespace-only text is ignored.
            kind: ``"human"`` for a transcription, ``"bot"`` for the bot's
                own turn.
            at: Unix time in seconds; defaults to now. A bot turn passes its
                speech start time.
        """
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        if len(cleaned) > MAX_ENTRY_CHARS:
            cleaned = cleaned[: MAX_ENTRY_CHARS - 1] + "…"
        self.log.append(HeardEntry(at=at if at is not None else time.time(), text=cleaned, kind=kind))
        if len(self.log) > MAX_LOG_ENTRIES:
            overflow = len(self.log) - MAX_LOG_ENTRIES
            del self.log[:overflow]
            self._pending_from = max(0, self._pending_from - overflow)

    def pending(self) -> tuple[list[HeardEntry], int]:
        """Human turns heard since the bot last spoke, and how many older ones were cut."""
        entries = [e for e in self.log[self._pending_from :] if e.kind == "human"]
        total = 0
        kept: list[HeardEntry] = []
        for entry in reversed(entries):
            total += len(entry.text) + 1
            if total > MAX_PENDING_CHARS or len(kept) >= MAX_PENDING_ENTRIES:
                break
            kept.append(entry)
        kept.reverse()
        return kept, len(entries) - len(kept)

    def mark_delivered(self) -> None:
        """Everything logged so far is now in the model's context.

        Called only when turns actually reached the model (a wake, or every
        turn in always mode), never when the bot merely spoke: speech heard
        during the bot's reply must stay pending for the next wake.
        """
        self._pending_from = len(self.log)

    def wake_text(self, addressed: str) -> str:
        """The addressed sentence, preceded by what was said since the model last heard."""
        pending, omitted = self.pending()
        self.mark_delivered()
        if not pending and not omitted:
            return addressed
        note = f" ({omitted} earlier turns omitted)" if omitted else ""
        earlier = "\n".join(e.text for e in pending) if pending else "(nothing that fits)"
        return (
            f"Said in the meeting since you last heard, not addressed to you{note}:\n"
            f"{earlier}\n\n"
            f"Now said to you:\n{addressed}"
        )

    def as_records(self) -> list[dict]:
        """The log in time order (a bot turn is stamped at its start, so it may
        have been appended after speech heard during it)."""
        ordered = sorted(enumerate(self.log), key=lambda pair: (pair[1].at, pair[0]))
        return [{"at": e.at, "kind": e.kind, "text": e.text} for _, e in ordered]
