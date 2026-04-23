# app/services/session_manager.py

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


BotMode = Literal["passive", "active", "ended"]


class BotSession:
    """Holds all runtime state for a single bot session."""

    def __init__(
        self,
        client_id: str,
        bot_id: str,
        meeting_url: str,
        marketing_person_email: str,
        client_name: str,
    ) -> None:
        self.client_id: str = client_id
        self.bot_id: str = bot_id
        self.meeting_url: str = meeting_url
        self.marketing_person_email: str = marketing_person_email
        self.client_name: str = client_name
        self.mode: BotMode = "passive"
        self.notes: List[str] = []
        self.extracted_needs: List[str] = []
        self.transcriptions: List[Dict[str, str]] = []  # [{speaker, text, ts}]
        self.created_at: datetime = datetime.utcnow()
        self.engaged_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        self.engage_timer_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "bot_id": self.bot_id,
            "meeting_url": self.meeting_url,
            "marketing_person_email": self.marketing_person_email,
            "client_name": self.client_name,
            "mode": self.mode,
            "notes": self.notes,
            "extracted_needs": self.extracted_needs,
            "transcriptions": self.transcriptions,
            "created_at": self.created_at.isoformat(),
            "engaged_at": self.engaged_at.isoformat() if self.engaged_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }


class SessionManager:
    """Thread-safe in-memory store for all active bot sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, BotSession] = {}  # keyed by client_id
        self._bot_id_index: Dict[str, str] = {}      # bot_id -> client_id

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def store_session(
        self,
        client_id: str,
        bot_id: str,
        meeting_url: str,
        marketing_person_email: str,
        client_name: str,
    ) -> BotSession:
        session = BotSession(
            client_id=client_id,
            bot_id=bot_id,
            meeting_url=meeting_url,
            marketing_person_email=marketing_person_email,
            client_name=client_name,
        )
        self._sessions[client_id] = session
        self._bot_id_index[bot_id] = client_id
        return session

    def get_session(self, client_id: str) -> Optional[BotSession]:
        return self._sessions.get(client_id)

    def get_session_by_bot_id(self, bot_id: str) -> Optional[BotSession]:
        client_id = self._bot_id_index.get(bot_id)
        if client_id:
            return self._sessions.get(client_id)
        return None

    def remove_session(self, client_id: str) -> None:
        session = self._sessions.pop(client_id, None)
        if session:
            self._bot_id_index.pop(session.bot_id, None)

    # ------------------------------------------------------------------ #
    # Note-taking helpers
    # ------------------------------------------------------------------ #

    def add_note(self, client_id: str, note: str) -> bool:
        session = self._sessions.get(client_id)
        if not session:
            return False
        session.notes.append(note)
        return True

    def add_extracted_need(self, client_id: str, need: str) -> bool:
        session = self._sessions.get(client_id)
        if not session:
            return False
        session.extracted_needs.append(need)
        return True

    def add_transcription(
        self, client_id: str, speaker: str, text: str
    ) -> bool:
        session = self._sessions.get(client_id)
        if not session:
            return False
        session.transcriptions.append(
            {
                "speaker": speaker,
                "text": text,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        return True

    # ------------------------------------------------------------------ #
    # Mode management
    # ------------------------------------------------------------------ #

    def set_mode(self, client_id: str, mode: BotMode) -> bool:
        session = self._sessions.get(client_id)
        if not session:
            return False
        session.mode = mode
        if mode == "active" and session.engaged_at is None:
            session.engaged_at = datetime.utcnow()
        if mode == "ended":
            session.ended_at = datetime.utcnow()
        return True

    def all_sessions(self) -> List[BotSession]:
        return list(self._sessions.values())


# Singleton
session_manager = SessionManager()
