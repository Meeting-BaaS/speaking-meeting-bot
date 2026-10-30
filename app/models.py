"""Data models for the Speaking Meeting Bot API."""

from datetime import datetime
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def _validate_meeting_url(value: str) -> str:
    """Validate that a meeting URL uses http(s) and includes a real host.

    Parses structurally rather than string-sniffing: the old check let
    hostless URLs like ``https:///room`` through (``/room`` contains a ``/``),
    which then flowed to MeetingBaas as a malformed join.
    """
    if not value:
        raise ValueError("meeting_url is required")

    normalized = value.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("meeting_url must start with http:// or https://")
    host = parsed.hostname
    # hostname must exist, contain a dot (FQDN), and hold no whitespace —
    # urlparse happily returns "exa mple.com" as a hostname otherwise.
    if not host or any(c.isspace() for c in host) or "." not in host:
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
    """Per-bot turn-taking tuning.

    All fields optional; unset fields fall back to the VAD_* / TURN_* env
    vars, then the server defaults (0.2 s stop, 1.5 s unfinished pause). A
    local end-of-turn model decides whether the speaker has finished once the
    VAD reports a pause, so stop_secs only needs to catch the pause.
    Human-facing bots want the defaults; bot-vs-bot meetings want patience
    (higher start_secs and unfinished_pause_secs) so the bots stop barging in
    on each other.
    """

    model_config = ConfigDict(extra="forbid")

    confidence: float | None = Field(
        None, ge=0.0, le=1.0, description="VAD speech confidence threshold"
    )
    start_secs: float | None = Field(
        None,
        ge=0.05,
        le=5.0,
        description=(
            "Sustained speech (seconds) before a turn registers while the bot is "
            "silent; while it speaks, min_interrupt_words decides"
        ),
    )
    stop_secs: float | None = Field(
        None,
        ge=0.1,
        le=10.0,
        description=(
            "Silence (seconds) before the bot checks whether the speaker is done; "
            "it replies right away when they are"
        ),
    )
    min_volume: float | None = Field(
        None, ge=0.0, le=1.0, description="Minimum input volume for VAD"
    )
    unfinished_pause_secs: float | None = Field(
        None,
        ge=0.2,
        le=10.0,
        description=(
            "When the speaker sounds mid-thought, silence (seconds) after which "
            "the bot replies anyway"
        ),
    )
    min_interrupt_words: int | None = Field(
        None,
        ge=0,
        le=20,
        description=(
            "Words someone must say before they interrupt the bot mid-sentence; "
            "coughs and 'uh-huh's stay below it. 0 interrupts on any speech"
        ),
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


def _secret_field(description: str) -> Any:
    # writeOnly/password so generated docs and clients treat the value as a
    # secret; SecretStr keeps it out of reprs, logs and model dumps.
    return Field(
        None,
        min_length=1,
        description=description,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )


class BotCredentials(BaseModel):
    """Bring-your-own provider keys for one bot.

    Every field is optional; a key that is not supplied falls back to the
    server's own configuration. Keys are handed to this bot's pipeline process
    only, are never written to disk or logs, and are never returned by the API.
    """

    model_config = ConfigDict(extra="forbid")

    llm_api_key: SecretStr | None = _secret_field(
        "API key for the LLM provider selected by llm_provider (OpenAI, Anthropic or Z.ai)."
    )
    stt_api_key: SecretStr | None = _secret_field(
        "Deepgram API key for speech-to-text."
    )
    tts_api_key: SecretStr | None = _secret_field(
        "Cartesia API key for text-to-speech."
    )

    def reveal(self) -> dict[str, str]:
        """Return the supplied keys as plain strings, omitting unset ones."""
        return {
            name: value.get_secret_value()
            for name, value in (
                ("llm_api_key", self.llm_api_key),
                ("stt_api_key", self.stt_api_key),
                ("tts_api_key", self.tts_api_key),
            )
            if value is not None and value.get_secret_value().strip()
        }


MeetingBaasTranscriptionProvider = Literal[
    "gladia", "deepgram", "assemblyai", "speechmatics", "soniox"
]

# BCP-47 tag such as en, en-NG, fr-FR, or Deepgram's "multi" for code-switching
# between its multilingual set.
LANGUAGE_PATTERN = r"^(multi|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)$"
DEFAULT_BRIEFING_LANGUAGE = "en"


def _validate_callback_url(value: str | None) -> str | None:
    if value is None:
        return value
    normalized = value.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("callback_url must be an http(s) URL with a host")
    if parsed.scheme == "http" and not _is_private_or_local_http_url(normalized):
        raise ValueError("callback_url must use https:// unless it is private or local")
    return normalized


class AuthorityConfig(BaseModel):
    """What the bot may say on its owner's behalf, and what it must hand back.

    Turns the persona into a representative: it introduces itself as an AI
    attending for ``owner_name``, states only ``approved_positions``, raises an
    escalation (and tells the room it is checking) for anything in
    ``escalate_on``, and never does what ``forbidden`` lists. Spending,
    commitments and deadline changes are escalated even when not listed.
    """

    model_config = ConfigDict(extra="forbid")

    owner_name: str = Field(
        ..., min_length=1, max_length=120, description="Who the bot represents"
    )
    approved_positions: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Positions the bot may state, in the owner's words",
    )
    escalate_on: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Decisions that must go back to the owner before any answer",
    )
    forbidden: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Things the bot must never say or do, even if asked",
    )

    @field_validator("approved_positions", "escalate_on", "forbidden")
    @classmethod
    def validate_items(cls, items: list[str]) -> list[str]:
        cleaned = [item.strip() for item in items if item and item.strip()]
        if any(len(item) > 500 for item in cleaned):
            raise ValueError("each item must be at most 500 characters")
        return cleaned


class BriefingConfig(BaseModel):
    """The post-meeting briefing, generated on the bot's own LLM key."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(True, description="Generate a briefing when the call ends")
    language: str | None = Field(
        None,
        pattern=LANGUAGE_PATTERN,
        description="Language the briefing is written in (BCP-47). Defaults to English.",
    )
    instructions: str | None = Field(
        None,
        max_length=2_000,
        description="What the owner wants the briefing to focus on",
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
                "language": "en",
                "callback_url": "https://app.example.com/hooks/speaking-bot",
                "authority": {
                    "owner_name": "Ada Okafor",
                    "approved_positions": ["Ada supports option B for the vendor choice"],
                    "escalate_on": ["any spending above 500 dollars"],
                    "forbidden": ["agreeing to new deadlines"],
                },
                "briefing": {"language": "en"},
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
            "TTS speaking speed multiplier between 0.6 and 1.5; values outside "
            "are rejected. Defaults to CARTESIA_TTS_SPEED, TTS_SPEED, "
            "SPEECH_SPEED, or the runner default."
        ),
    )
    speech_volume: float | None = Field(
        None,
        ge=0.5,
        le=2.0,
        description=(
            "TTS loudness multiplier. Honored range is 0.5-2.0 (Cartesia "
            "sonic-3). Omit for the provider default."
        ),
    )
    speech_emotion: str | None = Field(
        None,
        min_length=1,
        max_length=60,
        description=(
            "Emotion tag guiding the TTS tone (Cartesia sonic-3, experimental; "
            "60+ tags such as 'excited', 'calm'). Omit to let the model infer "
            "tone from the text, which Cartesia recommends."
        ),
    )
    respond_mode: Literal["always", "mentioned"] = Field(
        "always",
        description=(
            "When the bot replies. 'always': every completed turn gets an "
            "answer (good for 1:1 calls). 'mentioned': the bot only hears "
            "turns within a short window after someone says its name — the "
            "mode for multi-party meetings where it should stay quiet until "
            "addressed."
        ),
    )
    credentials: BotCredentials | None = Field(
        None,
        description=(
            "Bring your own provider keys for this bot (LLM, speech-to-text, "
            "text-to-speech). Any key left out falls back to the server's own "
            "configuration. Keys are used only by this bot's pipeline and are "
            "never stored or returned."
        ),
    )
    transcription_enabled: bool = Field(
        True,
        description=(
            "Have MeetingBaas transcribe the recording once the meeting ends, "
            "with the bot's own turns attributed to it by name. Set false for "
            "a recording without a transcript."
        ),
    )
    transcription_provider: MeetingBaasTranscriptionProvider = Field(
        "gladia",
        description="MeetingBaas speech-to-text provider for that transcript.",
    )
    language: str | None = Field(
        None,
        pattern=LANGUAGE_PATTERN,
        description=(
            "Language the bot listens for, as a BCP-47 tag (en, en-NG, fr), or "
            "'multi' to let Deepgram switch between its multilingual set during "
            "the call. Defaults to en-US."
        ),
    )
    callback_url: str | None = Field(
        None,
        description=(
            "Where this API posts the bot's own events: each utterance it speaks, "
            "each escalation to its owner, and the end of the call with the usage "
            "report and briefing. Signed with callback_secret when one is given."
        ),
    )
    callback_secret: SecretStr | None = _secret_field(
        "Shared secret used to sign callback_url deliveries "
        "(x-speaking-bot-signature: sha256=<HMAC of the body>)."
    )
    authority: AuthorityConfig | None = Field(
        None,
        description=(
            "Make the bot a representative: who it attends for, what it may say, "
            "what it must escalate. Adds the escalate_to_owner tool and switches "
            "off unprompted speech."
        ),
    )
    briefing: BriefingConfig | None = Field(
        None,
        description=(
            "Post-meeting briefing settings. Set (even empty) to get a briefing "
            "in the call-end event and at GET /bots/{bot_id}/briefing."
        ),
    )

    # NOTE: streaming_audio_frequency is intentionally excluded and handled internally

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, value: str | None) -> str | None:
        return _validate_callback_url(value)

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


class OwnerMessageRequest(BaseModel):
    """Something the owner wants the bot to relay to the meeting now."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2_000)


EscalationDecision = Literal["approved", "declined", "deferred", "info"]


class EscalationResponseRequest(BaseModel):
    """The owner's answer to an escalation the bot raised."""

    model_config = ConfigDict(extra="forbid")

    response: str = Field(..., min_length=1, max_length=2_000)
    decision: EscalationDecision | None = Field(
        None, description="How the owner decided, when it was a yes/no question"
    )


class AcceptedResponse(BaseModel):
    """The message was queued for the bot."""

    ok: bool = True
    message_id: str


class UsageReport(BaseModel):
    """What one bot consumed on each provider, from Pipecat's usage metrics."""

    model_config = ConfigDict(extra="allow")

    bot_id: str
    meetingbaas_bot_id: str | None = None
    stt_provider: str
    tts_provider: str
    llm_provider: str | None = None
    llm_model: str | None = None
    key_sources: dict[str, str] = Field(
        default_factory=dict,
        description="Per provider: 'platform' (ran on the server's key) or 'caller'",
    )
    started_at: float
    ended_at: float | None = None
    duration_seconds: float
    stt_audio_seconds: float
    tts_characters: int
    tts_requests: int
    llm_turns: int
    llm_prompt_tokens: int
    llm_cached_prompt_tokens: int
    llm_completion_tokens: int
    llm_total_tokens: int


class BriefingAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    owner: str | None = None
    task: str | None = None
    due: str | None = None


class BriefingEscalation(BaseModel):
    model_config = ConfigDict(extra="allow")

    question: str | None = None
    status: str | None = None
    answer: str | None = None


class BriefingReport(BaseModel):
    """The post-meeting briefing for the bot's owner."""

    model_config = ConfigDict(extra="allow")

    language: str = DEFAULT_BRIEFING_LANGUAGE
    summary: str = ""
    decisions: list[str] = Field(default_factory=list)
    actions: list[BriefingAction] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    what_the_representative_said: list[str] = Field(default_factory=list)
    escalations: list[BriefingEscalation] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    generated_at: float | None = None
    llm_provider: str | None = None
    llm_model: str | None = None


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
