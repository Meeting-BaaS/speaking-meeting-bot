"""Routes messages between clients and Pipecat."""

from collections import deque
from typing import Deque, Dict

from core.connection import registry
from core.converter import converter
from meetingbaas_pipecat.utils.logger import logger

# Meeting audio arriving before the Pipecat child has connected is buffered and
# replayed on connect, so words spoken during child startup still reach STT.
# 16kHz * 2 bytes * 30s; beyond that the oldest audio is dropped.
PENDING_AUDIO_MAX_BYTES = 16_000 * 2 * 30


class MessageRouter:
    """Routes messages between clients and Pipecat."""

    def __init__(self, registry, converter, logger=logger):
        self.registry = registry
        self.converter = converter
        self.logger = logger
        self.closing_clients = set()  # Track clients that are in the process of closing
        self._pending_audio: Dict[str, Deque[bytes]] = {}
        self._pending_audio_bytes: Dict[str, int] = {}

    def mark_closing(self, client_id: str):
        """Mark a client as closing to prevent sending more data to it."""
        self.closing_clients.add(client_id)
        self.logger.debug(f"Marked client {client_id} as closing")

    def _buffer_pending_audio(self, client_id: str, message: bytes):
        """Hold meeting audio for a client whose Pipecat side isn't connected yet."""
        queue = self._pending_audio.setdefault(client_id, deque())
        queue.append(message)
        total = self._pending_audio_bytes.get(client_id, 0) + len(message)
        while total > PENDING_AUDIO_MAX_BYTES and queue:
            total -= len(queue.popleft())
        self._pending_audio_bytes[client_id] = total

    async def on_pipecat_connected(self, client_id: str):
        """A (possibly replacement) Pipecat child connected: unblock and catch up.

        Clearing closing_clients matters as much as the replay — mark_closing
        was permanent, so a child that reconnected after a socket error never
        received another frame, leaving the bot deaf and mute for good.
        """
        self.closing_clients.discard(client_id)
        queue = self._pending_audio.pop(client_id, None)
        total = self._pending_audio_bytes.pop(client_id, 0)
        if not queue:
            return
        self.logger.info(
            f"Replaying {total} buffered audio bytes to Pipecat for client {client_id}"
        )
        for chunk in queue:
            await self.send_to_pipecat(chunk, client_id)

    def drop_pending_audio(self, client_id: str):
        """Discard any audio buffered for a client (bot removed / call over)."""
        self._pending_audio.pop(client_id, None)
        self._pending_audio_bytes.pop(client_id, None)

    async def send_binary(self, message: bytes, client_id: str):
        """Send binary data to a client."""
        if client_id in self.closing_clients:
            self.logger.debug(f"Skipping send to closing client {client_id}")
            return

        client = self.registry.get_client(client_id)
        if client:
            try:
                await client.send_bytes(message)
                self.logger.debug(f"Sent {len(message)} bytes to client {client_id}")
            except Exception as e:
                self.logger.debug(f"Error sending binary to client {client_id}: {e}")

    async def send_text(self, message: str, client_id: str):
        """Send text message to a specific client."""
        if client_id in self.closing_clients:
            self.logger.debug(f"Skipping send_text to closing client {client_id}")
            return

        client = self.registry.get_client(client_id)
        if client:
            try:
                await client.send_text(message)
                self.logger.debug(
                    f"Sent text message to client {client_id}: {message[:100]}..."
                )
            except Exception as e:
                self.logger.debug(f"Error sending text to client {client_id}: {e}")

    async def broadcast(self, message: str):
        """Broadcast text message to all clients."""
        for client_id, connection in self.registry.active_connections.items():
            if client_id not in self.closing_clients:
                try:
                    await connection.send_text(message)
                    self.logger.debug(f"Broadcast text message to client {client_id}")
                except Exception as e:
                    self.logger.debug(f"Error broadcasting to client {client_id}: {e}")

    async def send_to_pipecat(self, message: bytes, client_id: str):
        """Convert raw audio to Protobuf frame and send to Pipecat."""
        if client_id in self.closing_clients:
            self.logger.debug(
                f"Skipping send to Pipecat for closing client {client_id}"
            )
            return

        pipecat = self.registry.get_pipecat(client_id)
        if not pipecat:
            # Child still starting (or between reconnects): keep the audio so
            # words spoken during warmup reach STT once it connects.
            self._buffer_pending_audio(client_id, message)
            return
        try:
            serialized_frame = self.converter.raw_to_protobuf(message)
            await pipecat.send_bytes(serialized_frame)
            self.logger.debug(
                f"Forwarded audio frame ({len(message)} bytes) to Pipecat for client {client_id}"
            )
        except Exception as e:
            # Check for connection closed errors specifically
            if "close" in str(e).lower() or "closed" in str(e).lower():
                self.logger.debug(
                    f"Connection closed when sending to Pipecat for client {client_id}: {e}"
                )
                self.mark_closing(client_id)
            else:
                self.logger.error(f"Error sending to Pipecat: {str(e)}")

    async def send_from_pipecat(self, message: bytes, client_id: str):
        """Extract audio from Protobuf frame and send to client."""
        if client_id in self.closing_clients:
            self.logger.debug(
                f"Skipping send from Pipecat for closing client {client_id}"
            )
            return

        client = self.registry.get_client(client_id)
        if client:
            try:
                audio_data = self.converter.protobuf_to_raw(message)
                if audio_data:
                    await client.send_bytes(audio_data)
                    self.logger.debug(
                        f"Forwarded audio ({len(audio_data)} bytes) from Pipecat to client {client_id}"
                    )
            except Exception as e:
                # Check for connection closed errors specifically
                if "close" in str(e).lower() or "closed" in str(e).lower():
                    self.logger.debug(
                        f"Connection closed when sending to client {client_id}: {e}"
                    )
                    self.mark_closing(client_id)
                else:
                    self.logger.error(f"Error processing Pipecat message: {str(e)}")


# Create a singleton instance
router = MessageRouter(registry, converter)
