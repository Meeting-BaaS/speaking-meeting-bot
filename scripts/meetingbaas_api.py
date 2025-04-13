import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import requests
from pydantic import BaseModel, Field, HttpUrl

logger = logging.getLogger("meetingbaas-api")


class RecordingMode(str, Enum):
    """Available recording modes for the MeetingBaas API"""

    SPEAKER_VIEW = "speaker_view"
    GALLERY_VIEW = "gallery_view"
    SCREEN_SHARE = "screen_share"


class AutomaticLeave(BaseModel):
    """Settings for automatic leaving of meetings"""

    waiting_room_timeout: int = 600
    noone_joined_timeout: int = 0


class SpeechToText(BaseModel):
    """Speech to text settings"""

    provider: str = "Gladia"
    api_key: Optional[str] = None


class Streaming(BaseModel):
    """WebSocket streaming configuration"""

    input: str
    output: str
    audio_frequency: str = "16khz"


class MeetingBaasRequest(BaseModel):
    """
    Complete model for MeetingBaas API request
    Reference: https://docs.meetingbaas.com/api-reference/bots/join
    """

    # Required fields
    meeting_url: str
    bot_name: str
    reserved: bool = False
    streaming: Streaming  # Now a required field

    # Optional fields with defaults
    automatic_leave: AutomaticLeave = Field(default_factory=AutomaticLeave)
    recording_mode: RecordingMode = RecordingMode.SPEAKER_VIEW

    # Optional fields
    bot_image: Optional[HttpUrl] = None
    deduplication_key: Optional[str] = None
    entry_message: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    speech_to_text: Optional[SpeechToText] = None
    start_time: Optional[int] = None
    webhook_url: Optional[str] = None


def create_meeting_bot(
    meeting_url: str,
    websocket_url: str,
    bot_id: str,
    persona_name: str,
    api_key: str,
    recorder_only: bool = False,
    bot_image: Optional[str] = None,
    entry_message: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
):
    """
    Direct API call to MeetingBaas to create a bot

    Args:
        meeting_url: URL of the meeting to join
        websocket_url: Base WebSocket URL for audio streaming
        bot_id: Unique identifier for the bot
        persona_name: Name to display for the bot
        api_key: MeetingBaas API key
        recorder_only: Whether the bot should only record (no STT processing)
        bot_image: Optional URL for bot avatar
        entry_message: Optional message to send when joining
        extra: Optional additional metadata for the bot

    Returns:
        str: The bot ID if successful, None otherwise
    """
    # Create the WebSocket path for streaming
    websocket_with_path = f"{websocket_url}/ws/{bot_id}"

    # Create streaming config
    streaming = Streaming(input=websocket_with_path, output=websocket_with_path)

    # Create request model
    request = MeetingBaasRequest(
        meeting_url=meeting_url,
        bot_name=persona_name,
        reserved=False,
        deduplication_key=f"{persona_name}-BaaS-{bot_id}",
        streaming=streaming,
        bot_image=bot_image,
        entry_message=entry_message,
        extra=extra,
    )

    # Add speech-to-text configuration if recorder-only mode
    if recorder_only:
        request.speech_to_text = SpeechToText(provider="Default")

    # Convert request to dict for the API call
    config = request.model_dump(exclude_none=True)

    url = "https://api.meetingbaas.com/bots"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": api_key,
    }

    try:
        logger.info(f"Creating MeetingBaas bot for {meeting_url}")
        logger.debug(f"Request payload: {config}")

        response = requests.post(url, json=config, headers=headers)

        if response.status_code == 200:
            data = response.json()
            bot_id = data.get("bot_id")
            logger.info(f"Bot created with ID: {bot_id}")
            return bot_id
        else:
            logger.error(
                f"Failed to create bot: {response.status_code} - {response.text}"
            )
            return None
    except Exception as e:
        logger.error(f"Error creating bot: {str(e)}")
        return None
