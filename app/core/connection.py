"""Connection management for WebSocket clients and Pipecat processes."""

import subprocess
from typing import Dict, List, Optional, Tuple

from fastapi import WebSocket

from app.utils.pipecat_logger import logger

# Global dictionary to store meeting details for each client
MEETING_DETAILS: Dict[
    str, Tuple[str, str, Optional[str], bool, str]
] = {}  # client_id -> (meeting_url, persona_name, meetingbaas_bot_id, enable_tools, streaming_audio_frequency)

# Global dictionary to store Pipecat processes
PIPECAT_PROCESSES: Dict[str, subprocess.Popen] = {}  # client_id -> process


class ConnectionRegistry:
    """Manages WebSocket connections for clients and Pipecat."""

    def __init__(self, logger=logger):
        self.input_connections: Dict[str, WebSocket] = {}
        self.output_connections: Dict[str, WebSocket] = {}
        self.legacy_connections: Dict[str, WebSocket] = {}
        self.pipecat_connections: Dict[str, WebSocket] = {}
        self.logger = logger

    async def connect(
        self,
        websocket: WebSocket,
        client_id: str,
        is_pipecat: bool = False,
        channel: str = "legacy",
    ):
        """Register a new connection."""
        await websocket.accept()
        if is_pipecat:
            self.pipecat_connections[client_id] = websocket
            self.logger.info(f"Pipecat client {client_id} connected")
        else:
            self._get_client_bucket(channel)[client_id] = websocket
            self.logger.info(f"Client {client_id} connected")

    async def disconnect(
        self,
        client_id: str,
        is_pipecat: bool = False,
        channel: Optional[str] = None,
    ):
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
                channels = [channel] if channel else ["input", "output", "legacy"]
                for current_channel in channels:
                    bucket = self._get_client_bucket(current_channel)
                    if client_id in bucket:
                        websocket = bucket.pop(client_id)
                        try:
                            await websocket.close(code=1000, reason="Bot disconnected")
                        except Exception as e:
                            self.logger.debug(
                                f"Could not close {current_channel} WebSocket for {client_id}: {e}"
                            )
                        self.logger.info(f"Client {client_id} disconnected")
        except Exception as e:
            # This should rarely happen now, but just in case
            self.logger.debug(f"Error during disconnect for {client_id}: {e}")

    def get_client(self, client_id: str) -> Optional[WebSocket]:
        """Get the preferred outward-facing client connection by ID."""
        return self.get_output_client(client_id)

    def get_input_client(self, client_id: str) -> Optional[WebSocket]:
        """Get the input or legacy client connection by ID."""
        return self.input_connections.get(client_id) or self.legacy_connections.get(client_id)

    def get_output_client(self, client_id: str) -> Optional[WebSocket]:
        """Get the output or legacy client connection by ID."""
        return self.output_connections.get(client_id) or self.legacy_connections.get(client_id)

    def get_clients(self, client_id: str) -> List[WebSocket]:
        """Get all unique client sockets for a session."""
        clients = []
        for websocket in (
            self.input_connections.get(client_id),
            self.output_connections.get(client_id),
            self.legacy_connections.get(client_id),
        ):
            if websocket and websocket not in clients:
                clients.append(websocket)
        return clients

    def iter_unique_clients(self) -> List[Tuple[str, WebSocket]]:
        """Iterate over unique client sockets keyed by client ID."""
        seen: set[int] = set()
        clients: List[Tuple[str, WebSocket]] = []
        for bucket in (self.input_connections, self.output_connections, self.legacy_connections):
            for client_id, websocket in bucket.items():
                marker = id(websocket)
                if marker in seen:
                    continue
                seen.add(marker)
                clients.append((client_id, websocket))
        return clients

    def get_pipecat(self, client_id: str) -> Optional[WebSocket]:
        """Get a Pipecat connection by ID."""
        return self.pipecat_connections.get(client_id)

    def _get_client_bucket(self, channel: str) -> Dict[str, WebSocket]:
        if channel == "input":
            return self.input_connections
        if channel == "output":
            return self.output_connections
        return self.legacy_connections


# Create a singleton instance
registry = ConnectionRegistry()
