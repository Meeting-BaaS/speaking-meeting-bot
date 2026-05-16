"""Handles conversion between raw audio and Protobuf frames."""

from typing import Optional, Tuple, Dict, Any

from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.frames.frames import (
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
)
from app.utils.pipecat_logger import logger


class ProtobufConverter:
    """Handles conversion between raw audio and Protobuf frames using Pipecat's native serializer."""

    def __init__(self, logger=logger, sample_rate: int = 24000, channels: int = 1):
        self.logger = logger
        self.sample_rate = sample_rate
        self.channels = channels
        self.serializer = ProtobufFrameSerializer()

    def set_sample_rate(self, sample_rate: int):
        """Update the sample rate."""
        self.sample_rate = sample_rate
        self.logger.info(f"Updated ProtobufConverter sample rate to {sample_rate}")

    async def raw_to_protobuf(self, raw_audio: bytes) -> bytes:
        """Convert raw audio data to a serialized Protobuf frame."""
        try:
            frame = OutputAudioRawFrame(
                audio=raw_audio, 
                sample_rate=self.sample_rate, 
                num_channels=self.channels
            )
            return await self.serializer.serialize(frame)
        except Exception as e:
            self.logger.error(f"Error converting raw audio to Protobuf: {str(e)}")
            raise

    async def protobuf_to_raw(self, proto_data: bytes) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
        """Extract raw audio and/or transcription from a serialized Protobuf frame."""
        try:
            frame = await self.serializer.deserialize(proto_data)

            if frame is None:
                return None, None
            
            if isinstance(frame, InputAudioRawFrame):
                return frame.audio, None
            elif isinstance(frame, TranscriptionFrame):
                transcription = {
                    "text": getattr(frame, "text", ""),
                    "user_id": getattr(frame, "user_id", "Speaker"),
                    "timestamp": getattr(frame, "timestamp", "")
                }
                return None, transcription
            
            return None, None
        except Exception as e:
            self.logger.error(f"Error extracting data from Protobuf: {str(e)}")
            return None, None


# Create a singleton instance
converter = ProtobufConverter()
