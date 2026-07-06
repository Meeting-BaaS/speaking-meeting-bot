"""Data models for the Speaking Meeting Bot API."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_meeting_url(value: str) -> str:
    """Validate that a meeting URL uses http(s) and includes a host."""
    if not value:
        raise ValueError("meeting_url is required")

    normalized = value.strip()
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("meeting_url must start with http:// or https://")

    without_scheme = normalized.split("://", 1)[1]
    if "/" not in without_scheme and "." not in without_scheme:
        raise ValueError("meeting_url must include a valid host")

    return normalized


class TurnConfig(BaseModel):
    """Per-bot voice-activity / turn-taking tuning.

    All fields optional; unset fields fall back to the VAD_* env vars, then
    pipecat defaults. Human-facing bots want snappy turn-taking (low
    stop_secs); bot-vs-bot meetings want patience (higher stop_secs and
    start_secs) so the bots stop barging in on each other.
    """

    model_config = ConfigDict(extra="forbid")

    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="VAD speech confidence threshold"
    )
    start_secs: Optional[float] = Field(
        None, ge=0.05, le=5.0,
        description="Sustained speech (seconds) before a turn registers",
    )
    stop_secs: Optional[float] = Field(
        None, ge=0.1, le=10.0,
        description="Silence (seconds) before the bot considers the speaker done and replies",
    )
    min_volume: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Minimum input volume for VAD"
    )


class BotRequest(BaseModel):
    """Request model for creating a speaking bot in a meeting."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Meeting Assistant",
                "personas": ["helpful_assistant", "meeting_facilitator"],
                "bot_image": "https://example.com/bot-avatar.png",
                "entry_message": "Hello! I'm here to assist with the meeting.",
                "enable_tools": True,
                "extra": {"company": "ACME Corp", "meeting_purpose": "Weekly sync"},
                "websocket_url": "wss://bots.example.com",
                "prompt": "You are Meeting Assistant, a concise and professional \
                AI bot that helps summarize key points and keep the meeting on track. Speak clearly and stay on topic.",
            }
        },
    )

    # Define ONLY the fields we want in our API
    meeting_url: str = Field(
        ...,
        description="URL of the Google Meet, Zoom or Microsoft Teams meeting to join",
    )
    bot_name: str = Field("", description="Name to display for the bot in the meeting")
    personas: Optional[List[str]] = Field(
        None,
        description="List of persona names to use. The first available will be selected.",
    )
    bot_image: Optional[str] = None
    entry_message: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    enable_tools: bool = True
    prompt: Optional[str] = None
    websocket_url: Optional[str] = Field(
        None,
        description="Optional public WebSocket base URL override, e.g. wss://bot.example.com",
    )
    turn_config: Optional[TurnConfig] = Field(
        None,
        description="Per-bot turn-taking tuning (VAD confidence/start_secs/stop_secs/min_volume)",
    )

    # NOTE: streaming_audio_frequency is intentionally excluded and handled internally

    @field_validator("meeting_url")
    @classmethod
    def validate_meeting_url(cls, value: str) -> str:
        return _validate_meeting_url(value)

    @field_validator("websocket_url")
    @classmethod
    def validate_websocket_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value

        normalized = value.strip()
        if not normalized.startswith(("ws://", "wss://")):
            raise ValueError("websocket_url must start with ws:// or wss://")
        return normalized

class JoinResponse(BaseModel):
    """Response model for a bot joining a meeting"""

    bot_id: str = Field(
        ...,
        description="The MeetingBaas bot ID used for API operations with MeetingBaas",
    )


class LeaveResponse(BaseModel):
    """Response model for a bot leaving a meeting"""

    ok: bool


class LeaveBotRequest(BaseModel):
    """Request model for making a bot leave a meeting"""

    model_config = ConfigDict(extra="forbid")

    bot_id: Optional[str] = Field(
        None,
        description="The MeetingBaas bot ID to remove from the meeting. This will also close the WebSocket connection made through Pipecat by this bot.",
    )


class PersonaImageRequest(BaseModel):
    """Request model for generating persona images."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Name of the persona")
    description: str = Field(None, description="Description of the persona")
    gender: Optional[str] = Field(None, description="Gender of the persona")
    characteristics: Optional[List[str]] = Field(None, description="List of characteristics like blue eyes, etc.")

class PersonaImageResponse(BaseModel):
    """Response model for generated persona images."""
    name: str = Field(..., description="Name of the persona")
    image_url: str = Field(..., description="URL of the generated image")
    generated_at: datetime = Field(..., description="Timestamp of generation")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
