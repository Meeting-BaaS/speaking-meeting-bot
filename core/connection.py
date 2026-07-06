"""Connection management for WebSocket clients and Pipecat processes."""

import json
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from fastapi import WebSocket

from meetingbaas_pipecat.utils.logger import logger
from utils.runtime import get_state_dir


class PersistentMeetingDetails(dict):
    """MEETING_DETAILS backed by one JSON file per bot in the state dir.

    The API process used to keep meeting details only in memory, so any
    restart orphaned every live bot: MeetingBaas' websocket reconnect found
    no entry and was closed with 1008. Entries are tuples (JSON lists on
    disk); the persona dict at index 5 is JSON-safe by construction.
    Best-effort persistence — a failed write never breaks the request path.
    """

    def __init__(self):
        super().__init__()
        self._dir = os.path.join(get_state_dir(), "meeting_details")
        os.makedirs(self._dir, exist_ok=True)
        for fname in os.listdir(self._dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path) as f:
                    super().__setitem__(fname[:-5], tuple(json.load(f)))
            except Exception as e:
                logger.warning(f"Dropping unreadable meeting details {fname}: {e}")
                try:
                    os.remove(path)
                except OSError:
                    pass
        if self:
            logger.info(f"Rehydrated meeting details for {len(self)} bot(s) from disk")

    def _path(self, client_id: str) -> str:
        return os.path.join(self._dir, f"{client_id}.json")

    def __setitem__(self, client_id, details):
        super().__setitem__(client_id, details)
        try:
            with open(self._path(client_id), "w") as f:
                json.dump(list(details), f)
        except Exception as e:
            logger.warning(f"Could not persist meeting details for {client_id}: {e}")

    def pop(self, client_id, *default):
        try:
            os.remove(self._path(client_id))
        except OSError:
            pass
        return super().pop(client_id, *default)


# Global dictionary to store meeting details for each client
MEETING_DETAILS: Dict[
    str, Tuple[str, str, Optional[str], bool, str]
] = PersistentMeetingDetails()  # client_id -> (meeting_url, persona_name, meetingbaas_bot_id, enable_tools, streaming_audio_frequency, persona_data)

# Global dictionary to store Pipecat processes
PIPECAT_PROCESSES: Dict[str, subprocess.Popen] = {}  # client_id -> process


class ConnectionRegistry:
    """Manages WebSocket connections for clients and Pipecat."""

    def __init__(self, logger=logger):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pipecat_connections: Dict[str, WebSocket] = {}
        self.logger = logger

    async def connect(
        self, websocket: WebSocket, client_id: str, is_pipecat: bool = False
    ):
        """Register a new connection."""
        await websocket.accept()
        if is_pipecat:
            self.pipecat_connections[client_id] = websocket
            self.logger.info(f"Pipecat client {client_id} connected")
        else:
            self.active_connections[client_id] = websocket
            self.logger.info(f"Client {client_id} connected")

    async def disconnect(self, client_id: str, is_pipecat: bool = False):
        """Remove a connection and close the websocket."""
        try:
            # First, remove the connection from our dictionaries before attempting to close it
            if is_pipecat:
                if client_id in self.pipecat_connections:
                    websocket = self.pipecat_connections.pop(client_id)
                    # Try to close it if possible
                    try:
                        await websocket.close(code=1000, reason="Bot disconnected")
                    except Exception as e:
                        # It's normal for this to fail if the connection is already closed
                        self.logger.debug(
                            f"Could not close Pipecat WebSocket for {client_id}: {e}"
                        )
                    self.logger.info(f"Pipecat client {client_id} disconnected")
            else:
                if client_id in self.active_connections:
                    websocket = self.active_connections.pop(client_id)
                    # Try to close it if possible
                    try:
                        await websocket.close(code=1000, reason="Bot disconnected")
                    except Exception as e:
                        # It's normal for this to fail if the connection is already closed
                        self.logger.debug(
                            f"Could not close client WebSocket for {client_id}: {e}"
                        )
                    self.logger.info(f"Client {client_id} disconnected")
        except Exception as e:
            # This should rarely happen now, but just in case
            self.logger.debug(f"Error during disconnect for {client_id}: {e}")

    def get_client(self, client_id: str) -> Optional[WebSocket]:
        """Get a client connection by ID."""
        return self.active_connections.get(client_id)

    def get_pipecat(self, client_id: str) -> Optional[WebSocket]:
        """Get a Pipecat connection by ID."""
        return self.pipecat_connections.get(client_id)


# Create a singleton instance
registry = ConnectionRegistry()
