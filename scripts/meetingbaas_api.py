import json
import logging
import os
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import requests
from pydantic import BaseModel, Field, HttpUrl

logger = logging.getLogger("meetingbaas-api")

# Get API base URL from environment, default to production
api_base_url = os.environ.get("API_URL", "https://api.meetingbaas.com")


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


def stringify_values(obj: Any) -> Any:
    """
    Recursively convert any values that might cause JSON serialization issues to strings.

    Args:
        obj: Any Python object to stringify

    Returns:
        The object with all non-serializable values converted to strings
    """
    if isinstance(obj, dict):
        return {k: stringify_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [stringify_values(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        # For any other type, convert to string
        return str(obj)


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
    bot_image: Optional[str] = None  # Changed from HttpUrl to str
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
    bot_image: Optional[str] = None,
    entry_message: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    streaming_audio_frequency: str = "16khz",
):
    """
    Direct API call to MeetingBaas to create a bot

    Args:
        meeting_url: URL of the meeting to join
        websocket_url: Base WebSocket URL for audio streaming
        bot_id: Unique identifier for the bot
        persona_name: Name to display for the bot
        api_key: MeetingBaas API key
        bot_image: Optional URL for bot avatar
        entry_message: Optional message to send when joining
        extra: Optional additional metadata for the bot
        streaming_audio_frequency: Audio frequency for streaming (16khz or 24khz)

    Returns:
        str: The bot ID if successful, None otherwise
    """
    # Ensure all inputs are primitive types to avoid serialization issues
    if bot_image is not None:
        bot_image = str(bot_image)  # Ensure bot_image is a string

    # Create the WebSocket path for streaming
    websocket_with_path = f"{websocket_url}/ws/{bot_id}"

    # Create streaming config
    streaming = Streaming(
        input=websocket_with_path,
        output=websocket_with_path,
        audio_frequency=streaming_audio_frequency,
    )

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

    # Convert request to dict for the API call with custom handler for non-serializable types
    try:
        # First try the normal approach
        config = request.model_dump(exclude_none=True)
        # Make sure all values are serializable
        config = stringify_values(config)
    except Exception as e:
        logger.warning(f"Error in model_dump: {e}, trying manual conversion")
        # Fall back to manual conversion if that fails
        config = {
            "meeting_url": meeting_url,
            "bot_name": persona_name,
            "reserved": False,
            "deduplication_key": f"{persona_name}-BaaS-{bot_id}",
            "streaming": {
                "input": websocket_with_path,
                "output": websocket_with_path,
                "audio_frequency": streaming_audio_frequency,
            },
        }

        # Add optional fields
        if bot_image:
            config["bot_image"] = str(bot_image)
        if entry_message:
            config["entry_message"] = entry_message
        if extra:
            config["extra"] = extra

        # Ensure all values are serializable
        config = stringify_values(config)

    url = f"{api_base_url}/bots"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": api_key,
    }

    try:
        logger.info(f"Creating MeetingBaas bot for {meeting_url}")
        logger.debug(f"Request payload: {config}")

        # Try to serialize the payload to catch any JSON serialization issues
        try:
            json.dumps(config)
        except TypeError as e:
            logger.error(f"JSON serialization error: {e}")
            # Use our stringify_values function to convert all non-serializable values
            config = stringify_values(config)
            logger.info("Applied stringify_values to fix JSON serialization issues")

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


def leave_meeting_bot(bot_id: str, api_key: str) -> bool:
    """
    Call the MeetingBaas API to make a bot leave a meeting.

    Args:
        bot_id: The ID of the bot to remove
        api_key: MeetingBaas API key

    Returns:
        bool: True if successful, False otherwise
    """
    url = f"{api_base_url}/bots/{bot_id}"
    headers = {
        "x-meeting-baas-api-key": api_key,
    }

    try:
        logger.info(f"Removing bot with ID: {bot_id}")
        response = requests.delete(url, headers=headers)

        if response.status_code == 200:
            logger.info(f"Bot {bot_id} successfully left the meeting")
            return True
        else:
            logger.error(
                f"Failed to remove bot: {response.status_code} - {response.text}"
            )
            return False
    except Exception as e:
        logger.error(f"Error removing bot: {str(e)}")
        return False
