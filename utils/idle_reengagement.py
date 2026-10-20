"""Bound autonomous prompts until another participant speaks."""

from dataclasses import dataclass


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
