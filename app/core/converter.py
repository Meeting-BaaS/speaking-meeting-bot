"""Handles conversion between raw audio and Protobuf frames."""

from typing import Optional, Tuple, Dict, Any

import protobufs.frames_pb2 as frames_pb2
from app.utils.pipecat_logger import logger


class ProtobufConverter:
    """Handles conversion between raw audio and Protobuf frames."""

    def __init__(self, logger=logger, sample_rate: int = 24000, channels: int = 1):
        self.logger = logger
        self.sample_rate = sample_rate
        self.channels = channels

    def set_sample_rate(self, sample_rate: int):
        """Update the sample rate."""
        self.sample_rate = sample_rate
        self.logger.info(f"Updated ProtobufConverter sample rate to {sample_rate}")

    def raw_to_protobuf(self, raw_audio: bytes) -> bytes:
        """Convert raw audio data to a serialized Protobuf frame."""
        try:
            frame = frames_pb2.Frame()
            frame.audio.audio = raw_audio
            frame.audio.sample_rate = self.sample_rate
            frame.audio.num_channels = self.channels

            return frame.SerializeToString()
        except Exception as e:
            self.logger.error(f"Error converting raw audio to Protobuf: {str(e)}")
            raise

    def protobuf_to_raw(self, proto_data: bytes) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
        """Extract raw audio and/or transcription from a serialized Protobuf frame."""
        try:
            frame = frames_pb2.Frame()
            frame.ParseFromString(proto_data)

            audio_bytes = bytes(frame.audio.audio) if frame.HasField("audio") else None
            
            transcription = None
            if frame.HasField("transcription"):
                transcription = {
                    "text": frame.transcription.text,
                    "user_id": frame.transcription.user_id,
                    "timestamp": frame.transcription.timestamp
                }

            return audio_bytes, transcription
        except Exception as e:
            self.logger.error(f"Error extracting data from Protobuf: {str(e)}")
            return None, None


# Create a singleton instance
converter = ProtobufConverter()
