"""Bound autonomous prompts until another participant speaks."""

from dataclasses import dataclass


def max_nudges_for(
    respond_mode: str, configured: int, *, represents_owner: bool = False
) -> int:
    """How many unprompted nudges a bot gets; none when it only answers when named.

    A bot in ``mentioned`` mode exists to stay quiet until addressed, and an
    idle nudge is exactly the unprompted speech that mode rules out. The same
    goes for a bot attending on someone's behalf: it never fills a silence.
    """
    if represents_owner or (respond_mode or "").lower() == "mentioned":
        return 0
    return max(0, configured)


@dataclass
class IdleReengagementBudget:
    idle_secs: float
    max_nudges: int
    last_activity: float
    nudges: int = 0

    def record_speech(self, now: float) -> None:
        """Only external speech (or initial admission) replenishes the budget."""
        self.last_activity = now
        self.nudges = 0

    def take_nudge(self, now: float) -> bool:
        if self.nudges >= self.max_nudges:
            return False
        if now - self.last_activity < self.idle_secs:
            return False
        self.nudges += 1
        self.last_activity = now
        return True
