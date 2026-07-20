"""Data models for the Speaking Meeting Bot API."""

from datetime import datetime
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


def _is_private_or_local_http_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname:
        return False

    host = parsed.hostname
    if host == "localhost":
        return True
    try:
        parsed_ip = ip_address(host)
    except ValueError:
        return False
    return (
        parsed_ip.is_private
        or parsed_ip.is_loopback
        or parsed_ip.is_link_local
        or parsed_ip.is_unspecified
    )


def _validate_header_transport(url: str | None, headers: dict[str, str] | None) -> None:
    if not url or not headers or not url.startswith("http://"):
        return
    if _is_private_or_local_http_url(url):
        return
    raise ValueError("headers require https:// unless the URL is private or local")


class TurnConfig(BaseModel):
    """Per-bot voice-activity / turn-taking tuning.

    All fields optional; unset fields fall back to the VAD_* env vars, then
    pipecat defaults. Human-facing bots want snappy turn-taking (low
    stop_secs); bot-vs-bot meetings want patience (higher stop_secs and
    start_secs) so the bots stop barging in on each other.
    """

    model_config = ConfigDict(extra="forbid")

    confidence: float | None = Field(
        None, ge=0.0, le=1.0, description="VAD speech confidence threshold"
    )
    start_secs: float | None = Field(
        None,
        ge=0.05,
        le=5.0,
        description="Sustained speech (seconds) before a turn registers",
    )
    stop_secs: float | None = Field(
        None,
        ge=0.1,
        le=10.0,
        description="Silence (seconds) before the bot considers the speaker done and replies",
    )
    min_volume: float | None = Field(
        None, ge=0.0, le=1.0, description="Minimum input volume for VAD"
    )


class PromptDataSource(BaseModel):
    """External context to append to the bot prompt under a token budget."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        "external_context",
        min_length=1,
        max_length=120,
        description="Human-readable source name shown inside the prompt context block",
    )
    type: Literal["text", "url"] = Field(
        ...,
        description="Whether to load inline text or fetch an external HTTP(S) URL",
    )
    text: str | None = Field(
        None,
        description="Inline context. Required when type is text.",
    )
    url: str | None = Field(
        None,
        description="HTTP(S) URL to fetch. Required when type is url.",
    )
    headers: dict[str, str] | None = Field(
        None,
        description="Optional HTTP headers for URL sources. Avoid request-specific secrets unless needed.",
    )
    token_limit: int | None = Field(
        None,
        ge=1,
        le=50_000,
        description="Optional per-source token cap before the request-level cap is applied",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError(
                "prompt data source url must start with http:// or https://"
            )
        return normalized

    @model_validator(mode="after")
    def validate_source_payload(self):
        if self.type == "text" and not self.text:
            raise ValueError("text is required when prompt data source type is text")
        if self.type == "url" and not self.url:
            raise ValueError("url is required when prompt data source type is url")
        if self.type == "text" and self.url:
            raise ValueError("url is not allowed when prompt data source type is text")
        if self.type == "url" and self.text:
            raise ValueError("text is not allowed when prompt data source type is url")
        _validate_header_transport(self.url, self.headers)
        return self


MCPTransport = Literal["http", "streamable_http", "sse"]
LLMProvider = Literal["openai", "anthropic", "zai"]
MCPProxyProfile = Literal["professional", "personal", "all"]
MCPProxyToolAccess = Literal["read_only", "read_write"]


class MCPServerConfig(BaseModel):
    """MCP server metadata and optional live connection details."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    enabled: bool = Field(
        True,
        description="Whether this server may be used. Disabled servers are documented but not connected.",
    )
    url: str | None = Field(
        None,
        description="Remote MCP server URL. Required for http, streamable_http, and sse transports.",
    )
    headers: dict[str, str] | None = Field(
        None,
        description="Optional HTTP headers for remote MCP servers. Use only when a server requires them.",
    )
    transport: MCPTransport | None = Field(
        None,
        description="Remote MCP transport. Omit for metadata-only servers that cannot execute tools.",
    )
    tools: list[str] | None = Field(
        None,
        max_length=50,
        description="Known tool names exposed by this MCP server",
    )
    tool_allowlist: list[str] | None = Field(
        None,
        max_length=50,
        description="Optional allowlist of MCP tool names this bot may call from this server.",
    )
    timeout_seconds: float | None = Field(
        None,
        ge=0.1,
        le=300.0,
        description="Optional per-server connection/tool timeout in seconds.",
    )
    instructions: str | None = Field(
        None,
        max_length=4_000,
        description="Operator instructions or constraints for this MCP server",
    )

    @field_validator("url")
    @classmethod
    def validate_mcp_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("mcp server url must start with http:// or https://")
        return normalized

    @model_validator(mode="after")
    def validate_connection_details(self):
        if self.transport in {"http", "streamable_http", "sse"}:
            if not self.url:
                raise ValueError(
                    f"url is required when MCP transport is {self.transport}"
                )
        else:
            if self.url or self.headers:
                raise ValueError(
                    "transport is required when MCP connection details are supplied"
                )
        _validate_header_transport(self.url, self.headers)
        return self


class MCPConfig(BaseModel):
    """Bot-level MCP configuration: the set of servers to expose plus global instructions."""

    model_config = ConfigDict(extra="forbid")

    servers: list[MCPServerConfig] = Field(
        default_factory=list,
        max_length=10,
        description="MCP servers to document and optionally connect for tool calls",
    )
    instructions: str | None = Field(
        None,
        max_length=4_000,
        description="Global MCP usage instructions for the bot",
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
                "prompt_data_token_limit": 3000,
                "llm_provider": "anthropic",
                "llm_model": "claude-opus-4-8",
                "mcp_profile": "professional",
                "mcp_profile_tool_access": "read_only",
                "prompt_data_sources": [
                    {
                        "name": "CRM account notes",
                        "type": "url",
                        "url": "https://example.com/account-notes.md",
                    }
                ],
                "speech_speed": 1.15,
                "mcp": {
                    "servers": [
                        {
                            "name": "crm",
                            "url": "https://mcp.example.com",
                            "transport": "streamable_http",
                            "tools": ["get_account", "list_recent_calls"],
                            "tool_allowlist": ["get_account", "list_recent_calls"],
                        }
                    ]
                },
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
    personas: list[str] | None = Field(
        None,
        max_length=10,
        description="List of persona names to use. The first available will be selected.",
    )
    bot_image: str | None = None
    entry_message: str | None = None
    extra: dict[str, Any] | None = None
    enable_tools: bool = True
    prompt: str | None = None
    websocket_url: str | None = Field(
        None,
        description="Optional public WebSocket base URL override, e.g. wss://bot.example.com",
    )
    turn_config: TurnConfig | None = Field(
        None,
        description="Per-bot turn-taking tuning (VAD confidence/start_secs/stop_secs/min_volume)",
    )
    prompt_data_sources: list[PromptDataSource] | None = Field(
        None,
        max_length=10,
        description="External text or URL data sources to append to the bot prompt",
    )
    prompt_data_token_limit: int = Field(
        4_000,
        ge=0,
        le=50_000,
        description="Approximate total token cap for loaded prompt_data_sources. 0 disables loading.",
    )
    mcp: MCPConfig | None = Field(
        None,
        description="MCP server/tool metadata and optional live connection details",
    )
    mcp_profile: MCPProxyProfile | None = Field(
        None,
        description=(
            "Optional trusted local mcpproxy group preset. "
            "'professional' connects to MCP_PROXY_PROFESSIONAL_URL or "
            "http://127.0.0.1:8111/mcp; 'personal' connects to "
            "MCP_PROXY_PERSONAL_URL or http://127.0.0.1:8110/mcp; 'all' "
            "connects to MCP_PROXY_ALL_URL or http://127.0.0.1:8109/mcp. "
            "The preset is merged with explicit mcp servers when both are supplied."
        ),
    )
    mcp_profile_tool_access: MCPProxyToolAccess = Field(
        "read_only",
        description=(
            "Tool exposure for mcp_profile presets. 'read_only' exposes "
            "retrieve_tools, call_tool_read, read_cache, and set_profile. "
            "'read_write' also exposes call_tool_write. Presets never expose "
            "upstream_servers, call_tool_destructive, code_execution, registry, "
            "or quarantine tools."
        ),
    )
    llm_provider: LLMProvider | None = Field(
        None,
        description="LLM provider for this bot. Defaults to LLM_PROVIDER, then openai.",
    )
    llm_model: str | None = Field(
        None,
        min_length=1,
        max_length=120,
        description="Provider model for this bot. Defaults to provider-specific env vars.",
    )
    speech_speed: float | None = Field(
        None,
        ge=0.6,
        le=1.5,
        description=(
            "TTS speaking speed multiplier. Honored range is 0.6-1.5 (the "
            "runner clamps to it); values outside are rejected. Defaults to "
            "CARTESIA_TTS_SPEED, TTS_SPEED, SPEECH_SPEED, or the runner default."
        ),
    )

    # NOTE: streaming_audio_frequency is intentionally excluded and handled internally

    @field_validator("meeting_url")
    @classmethod
    def validate_meeting_url(cls, value: str) -> str:
        return _validate_meeting_url(value)

    @model_validator(mode="after")
    def validate_mcp_profile_tool_access(self):
        if self.mcp_profile is None and self.mcp_profile_tool_access != "read_only":
            raise ValueError("mcp_profile_tool_access requires mcp_profile")
        return self

    @field_validator("websocket_url")
    @classmethod
    def validate_websocket_url(cls, value: str | None) -> str | None:
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

    bot_id: str | None = Field(
        None,
        description="The MeetingBaas bot ID to remove from the meeting. This will also close the WebSocket connection made through Pipecat by this bot.",
    )


class PersonaImageRequest(BaseModel):
    """Request model for generating persona images."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Name of the persona")
    description: str | None = Field(None, description="Description of the persona")
    gender: str | None = Field(None, description="Gender of the persona")
    characteristics: list[str] | None = Field(
        None, description="List of characteristics like blue eyes, etc."
    )


class PersonaImageResponse(BaseModel):
    """Response model for generated persona images."""

    name: str = Field(..., description="Name of the persona")
    image_url: str = Field(..., description="URL of the generated image")
    generated_at: datetime = Field(..., description="Timestamp of generation")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
