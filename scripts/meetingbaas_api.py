import json
import logging
import os
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import requests
from pydantic import BaseModel, Field, HttpUrl

# Base URL for the MeetingBaas API. Override to target a self-hosted
# deployment (e.g. https://api.gmeetrecorder.com).
MEETING_BAAS_API_URL = os.getenv("MEETING_BAAS_API_URL", "https://api.meetingbaas.com")

logger = logging.getLogger("meetingbaas-api")


class MeetingBaasError(Exception):
    """MeetingBaas API rejected a request; carries the upstream status + message."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"MeetingBaas API error {status_code}: {message}")


class RecordingMode(str, Enum):
    """Available recording modes for the MeetingBaas API"""

    SPEAKER_VIEW = "speaker_view"
    GALLERY_VIEW = "gallery_view"
    AUDIO_ONLY = "audio_only"


class TimeoutConfig(BaseModel):
    """Settings for automatic leaving of meetings.

    Note: The v2 API enforces a minimum of 120 for timeout values,
    with a default of 600.
    """

    waiting_room_timeout: int = 600
    no_one_joined_timeout: int = 600
    silence_timeout: Optional[int] = None


class TranscriptionConfig(BaseModel):
    """Transcription configuration"""

    provider: str = "gladia"
    api_key: Optional[str] = None
    custom_params: Optional[Dict[str, Any]] = None


class StreamingConfig(BaseModel):
    """WebSocket streaming configuration"""

    input_url: str
    output_url: str
    audio_frequency: int = 16000


class CallbackConfig(BaseModel):
    """Callback/webhook configuration"""

    url: str


def _parse_audio_frequency(freq: str) -> int:
    """Convert a string audio frequency like '16khz' to an integer in Hz.

    Args:
        freq: Audio frequency string (e.g. '16khz', '24khz')

    Returns:
        int: Frequency in Hz (e.g. 16000, 24000)
    """
    freq = freq.lower().strip()
    if freq.endswith("khz"):
        return int(freq[:-3]) * 1000
    # If it's already a numeric string, return as int
    return int(freq)


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


class CreateBotRequest(BaseModel):
    """
    Complete model for MeetingBaas v2 API request
    Reference: https://docs.meetingbaas.com/api-reference/bots/join
    """

    # Required fields
    meeting_url: str
    bot_name: str

    # Streaming
    streaming_enabled: bool = True
    streaming_config: StreamingConfig

    # Timeout (omitted by default to let the API use its own defaults)
    timeout_config: Optional[TimeoutConfig] = None

    # Recording
    recording_mode: RecordingMode = RecordingMode.SPEAKER_VIEW

    # Optional fields
    bot_image: Optional[str] = None
    entry_message: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

    # Callback/webhook
    callback_enabled: bool = False
    callback_config: Optional[CallbackConfig] = None

    # Transcription
    transcription_enabled: bool = False
    transcription_config: Optional[TranscriptionConfig] = None

    # Deduplication (True to match v1 behavior where each bot had a unique dedup key)
    allow_multiple_bots: bool = True


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
    webhook_url: Optional[str] = None,
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
        webhook_url: Optional webhook URL for callbacks

    Returns:
        str: The bot ID if successful, None otherwise
    """
    # Ensure all inputs are primitive types to avoid serialization issues
    if bot_image is not None:
        bot_image = str(bot_image)  # Ensure bot_image is a string

    # Create the WebSocket path for streaming
    websocket_with_path = f"{websocket_url}/ws/{bot_id}"

    # Convert string frequency to int Hz
    audio_freq_hz = _parse_audio_frequency(streaming_audio_frequency)

    # Create streaming config
    streaming_config = StreamingConfig(
        input_url=websocket_with_path,
        output_url=websocket_with_path,
        audio_frequency=audio_freq_hz,
    )

    # Build callback config if webhook_url is provided
    callback_enabled = webhook_url is not None
    callback_config = CallbackConfig(url=webhook_url) if webhook_url else None

    # Create request model
    request = CreateBotRequest(
        meeting_url=meeting_url,
        bot_name=persona_name,
        streaming_enabled=True,
        streaming_config=streaming_config,
        bot_image=bot_image,
        entry_message=entry_message,
        extra=extra,
        callback_enabled=callback_enabled,
        callback_config=callback_config,
        allow_multiple_bots=True,
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
            "streaming_enabled": True,
            "streaming_config": {
                "input_url": websocket_with_path,
                "output_url": websocket_with_path,
                "audio_frequency": audio_freq_hz,
            },
            "allow_multiple_bots": True,
        }

        # Add optional fields
        if bot_image:
            config["bot_image"] = str(bot_image)
        if entry_message:
            config["entry_message"] = entry_message
        if extra:
            config["extra"] = extra
        if callback_enabled:
            config["callback_enabled"] = True
            config["callback_config"] = {"url": webhook_url}

        # Ensure all values are serializable
        config = stringify_values(config)

    url = f"{MEETING_BAAS_API_URL}/v2/bots"
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

        response = requests.post(url, json=config, headers=headers, timeout=(5, 30))

        if response.status_code == 201:
            data = response.json()
            # v2 response: {"success": true, "data": {"bot_id": "..."}}
            bot_id = data.get("data", {}).get("bot_id")
            if not bot_id:
                logger.error(f"201 response missing bot_id: {data}")
                raise MeetingBaasError(502, "MeetingBaas returned 201 without a bot_id")
            logger.info(f"Bot created with ID: {bot_id}")
            return bot_id

        # Surface the upstream rejection verbatim (e.g. 409 bot-already-exists,
        # 401 bad key) instead of collapsing everything into a generic failure.
        logger.error(f"Failed to create bot: {response.status_code} - {response.text}")
        try:
            body = response.json()
            message = body.get("message") or body.get("error") or response.text
        except ValueError:
            message = response.text
        raise MeetingBaasError(response.status_code, message)
    except MeetingBaasError:
        raise
    except requests.RequestException as e:
        logger.error(f"Error creating bot: {str(e)}")
        raise MeetingBaasError(502, f"Could not reach the MeetingBaas API: {e}")


def leave_meeting_bot(bot_id: str, api_key: str) -> bool:
    """
    Call the MeetingBaas API to make a bot leave a meeting.

    Args:
        bot_id: The ID of the bot to remove
        api_key: MeetingBaas API key

    Returns:
        bool: True if successful, False otherwise
    """
    url = f"{MEETING_BAAS_API_URL}/v2/bots/{bot_id}/leave"
    headers = {
        "x-meeting-baas-api-key": api_key,
    }

    try:
        logger.info(f"Removing bot with ID: {bot_id}")
        response = requests.post(url, headers=headers, timeout=(5, 30))

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
