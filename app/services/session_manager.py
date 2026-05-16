# app/services/session_manager.py
# Changes: #2 (Session State Machine), #6 (Heartbeat), #7 (Structured Logging),
#           #15 (Explicit Cleanup), #18 (Dedicated Internal IDs)

import asyncio
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

log = logging.getLogger("session_manager")

BotMode = Literal["passive", "active", "ended"]


class SessionState(Enum):
    """Explicit state machine for a bot session lifecycle. (Change #2)"""
    CREATED = "created"
    WS_CONNECTED = "ws_connected"
    PIPECAT_CONNECTED = "pipecat_connected"
    READY = "ready"
    STREAMING = "streaming"
    CLOSED = "closed"


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
        # Dedicated Internal IDs (Change #18)
        self.session_id: str = client_id           # Canonical session key
        self.client_id: str = client_id            # Internal WebSocket ID
        self.bot_id: str = bot_id                  # MeetingBaas bot ID
        self.websocket_id: Optional[str] = None    # Active WS connection ID

        self.meeting_url: str = meeting_url
        self.marketing_person_email: str = marketing_person_email
        self.client_name: str = client_name

        # Session State Machine (Change #2)
        self.state: SessionState = SessionState.CREATED

        self.mode: BotMode = "passive"
        self.notes: List[str] = []
        self.extracted_needs: List[str] = []
        self.transcriptions: List[Dict[str, str]] = []  # [{speaker, text, ts}]
        self.audio_frames_received: int = 0
        self.last_audio_frame_at: Optional[datetime] = None
        self.last_handshake_at: Optional[datetime] = None

        self.created_at: datetime = datetime.utcnow()
        self.engaged_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        self.engage_timer_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

        # Heartbeat tracking (Change #6)
        self.last_seen: datetime = datetime.utcnow()

    def touch(self) -> None:
        """Update last_seen timestamp (called on every ping/pong)."""
        self.last_seen = datetime.utcnow()

    def is_stale(self, timeout_seconds: int = 60) -> bool:
        """Return True if no activity seen within timeout_seconds."""
        return (datetime.utcnow() - self.last_seen).total_seconds() > timeout_seconds

    def transition(self, new_state: SessionState) -> None:
        """Explicit state transition with logging. (Change #2, #7)"""
        old_state = self.state
        self.state = new_state
        log.info(
            "session_state_transition",
            extra={
                "client_id": self.client_id,
                "bot_id": self.bot_id,
                "session_id": self.session_id,
                "websocket_state": self.websocket_id,
                "from_state": old_state.value,
                "to_state": new_state.value,
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "bot_id": self.bot_id,
            "websocket_id": self.websocket_id,
            "meeting_url": self.meeting_url,
            "marketing_person_email": self.marketing_person_email,
            "client_name": self.client_name,
            "state": self.state.value,
            "mode": self.mode,
            "notes": self.notes,
            "extracted_needs": self.extracted_needs,
            "transcriptions": self.transcriptions,
            "audio_frames_received": self.audio_frames_received,
            "last_audio_frame_at": self.last_audio_frame_at.isoformat() if self.last_audio_frame_at else None,
            "last_handshake_at": self.last_handshake_at.isoformat() if self.last_handshake_at else None,
            "created_at": self.created_at.isoformat(),
            "engaged_at": self.engaged_at.isoformat() if self.engaged_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "last_seen": self.last_seen.isoformat(),
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
        log.info(
            "session_created",
            extra={
                "client_id": client_id,
                "bot_id": bot_id,
                "session_id": client_id,
                "websocket_state": None,
                "session_state": SessionState.CREATED.value,
            }
        )
        return session

    def get_session(self, client_id: str) -> Optional[BotSession]:
        return self._sessions.get(client_id)

    def get_session_by_bot_id(self, bot_id: str) -> Optional[BotSession]:
        client_id = self._bot_id_index.get(bot_id)
        if client_id:
            return self._sessions.get(client_id)
        return None

    def remove_session(self, client_id: str) -> None:
        """Explicit full cleanup of a session. (Change #15)"""
        session = self._sessions.pop(client_id, None)
        if session:
            self._bot_id_index.pop(session.bot_id, None)
            # Cancel any pending engage timer
            if session.engage_timer_task and not session.engage_timer_task.done():
                session.engage_timer_task.cancel()
            session.transition(SessionState.CLOSED)
            log.info(
                "session_removed",
                extra={
                    "client_id": client_id,
                    "bot_id": session.bot_id,
                    "session_id": client_id,
                    "websocket_state": session.websocket_id,
                    "session_state": SessionState.CLOSED.value,
                }
            )

    # ------------------------------------------------------------------ #
    # State transitions (Change #2)
    # ------------------------------------------------------------------ #

    def mark_ws_connected(self, client_id: str, websocket_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.websocket_id = websocket_id
            session.transition(SessionState.WS_CONNECTED)

    def mark_pipecat_connected(self, client_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.transition(SessionState.PIPECAT_CONNECTED)

    def mark_ready(self, client_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.transition(SessionState.READY)

    def mark_streaming(self, client_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.transition(SessionState.STREAMING)

    def mark_handshake(self, client_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.last_handshake_at = datetime.utcnow()

    def mark_audio_frame(self, client_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.audio_frames_received += 1
            session.last_audio_frame_at = datetime.utcnow()

    def is_ready(self, client_id: str) -> bool:
        """Return True only when the session is READY or STREAMING. (Change #3)"""
        session = self._sessions.get(client_id)
        if not session:
            return False
        return session.state in (SessionState.READY, SessionState.STREAMING)

    # ------------------------------------------------------------------ #
    # Heartbeat (Change #6)
    # ------------------------------------------------------------------ #

    def touch(self, client_id: str) -> None:
        session = self._sessions.get(client_id)
        if session:
            session.touch()

    def get_stale_sessions(self, timeout_seconds: int = 60) -> List[str]:
        """Return client_ids of sessions with no recent activity."""
        return [
            cid for cid, s in self._sessions.items()
            if s.is_stale(timeout_seconds)
        ]

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
