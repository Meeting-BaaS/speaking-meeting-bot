"""Routes messages between clients and Pipecat."""

from app.core.connection import registry
from app.core.converter import converter
from app.utils.pipecat_logger import logger
from app.services.session_manager import session_manager


class MessageRouter:
    """Routes messages between clients and Pipecat."""

    def __init__(self, registry, converter, logger=logger):
        self.registry = registry
        self.converter = converter
        self.logger = logger
        self.closing_clients = set()  # Track clients that are in the process of closing

    def mark_closing(self, client_id: str):
        """Mark a client as closing to prevent sending more data to it."""
        self.closing_clients.add(client_id)
        self.logger.debug(f"Marked client {client_id} as closing")

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
        if pipecat:
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
                audio_data, transcription = self.converter.protobuf_to_raw(message)
                
                if transcription and transcription.get("text"):
                    # Add live transcription to session manager
                    session_manager.add_transcription(
                        client_id=client_id,
                        speaker=transcription.get("user_id", "Speaker"),
                        text=transcription["text"]
                    )
                    self.logger.info(f"Recorded live transcription for {client_id}: {transcription['text']}")

                if audio_data:
                    await client.send_bytes(audio_data)
                    self.logger.info(
                        f"Forwarded audio ({len(audio_data)} bytes) from Pipecat to MeetingBaaS client {client_id}"
                    )
                elif not audio_data and not transcription:
                    self.logger.warning(
                        f"Extracted data was None, trying raw bytes fallback."
                    )
                    if len(message) > 100:
                        await client.send_bytes(message)
                        self.logger.info(f"Forwarded raw bytes ({len(message)}) as fallback")
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
