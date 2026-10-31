# Cold-start instrumentation: everything imported after this line (pipecat ->
# torch -> silero, SDK clients, ...) is the expensive part of process startup.
# The stamp lives in its own tiny module so it is taken before them without a
# statement ahead of the imports; [STARTUP] reports real spawn-to-ready latency.
from utils.proc_start import PROC_T0 as _PROC_T0

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
from pipecat.frames.frames import (
    LLMMessagesAppendFrame,
    SystemFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.task import PipelineParams
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams

# from pipecat.services.gladia.stt import GladiaSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.client import (
    WebsocketClientParams,
    WebsocketClientTransport,
)
from pipecat.turns.user_start import (
    MinWordsUserTurnStartStrategy,
    WakePhraseUserTurnStartStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import (
    EagerUserTurnStrategies,
    UserTurnStrategies,
)

from config.persona_utils import PersonaManager
from meetingbaas_pipecat.utils.logger import configure_logger
from utils.authority import (
    ESCALATE_TOOL_NAME,
    OwnerMessageReader,
    build_authority_prompt,
    escalation_reply_for_llm,
    escalation_tool_schema,
    new_escalation,
    owner_message_for_llm,
    owner_messages_path,
    record_escalation,
)
from utils.customer_events import (
    EVENT_ESCALATION,
    EVENT_OWNER_MESSAGE_DELIVERED,
    EVENT_UTTERANCE,
    CustomerEventSink,
)
from utils.early_audio import hold_messages_until_started
from utils.floor import floor_blocked_by_sibling
from utils.idle_reengagement import IdleReengagementBudget, max_nudges_for
from utils.llm_config import (
    ANTHROPIC_THINKING_ON_MODES,
    DEFAULT_ZAI_BASE_URL,
    clean_string,
    resolve_anthropic_thinking,
    resolve_llm_model,
    resolve_llm_provider,
    resolve_openai_api_surface,
)
from utils.mcp_client import (
    HttpMcpClient,
    McpClientError,
    StdioMcpClient,
    apply_mcp_runtime_headers,
    build_mcp_tool_name,
)
from utils.runtime import get_state_dir
from utils.speakers import read_recent_speakers
from utils.speech_activity import SpeechActivityTracker
from utils.system_prompt import build_system_prompt
from utils.turn_taking import resolve_turn_taking
from utils.usage_meter import UsageLedger, UsageMeter, usage_dir
from utils.meeting_memory import MeetingMemory
from utils.wake_phrases import keyterms_for, matches_wake_phrase, wake_phrases_for

# Import every LLM service the runtime can select at MODULE level, not lazily
# inside build_llm_service(): the lazy `import anthropic` cost ~27s on a loaded
# box and landed AFTER the websocket transport existed but BEFORE the pipeline
# started consuming audio — stalling the audio bridge long enough for keepalive
# timeouts. Paying the import here keeps it inside the (warmed) startup window.
try:
    from pipecat.services.anthropic.llm import AnthropicLLMService
except ImportError:  # anthropic extra not installed; provider guarded at runtime
    AnthropicLLMService = None
try:
    from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
except ImportError:
    OpenAIResponsesLLMService = None

# Global transcript storage - will be saved to file for webhook to read
TRANSCRIPT_DIR = os.path.join(get_state_dir(), "transcripts")
# Directory for ready signals from webhook
READY_SIGNALS_DIR = os.path.join(get_state_dir(), "ready_signals")

load_dotenv(override=True)

logger = configure_logger()

# Ensure logs are flushed immediately and are human-readable
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('[%(asctime)s] %(levelname)s | %(name)s:%(funcName)s:%(lineno)d | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
handler.setLevel(logging.INFO)
logger.handlers = [handler]
logger.propagate = False

# Function to log and flush
def log_and_flush(level, msg):
    logger.log(level, msg)
    for h in logger.handlers:
        h.flush()

def speculative_turns_enabled() -> bool:
    """STT_SPECULATIVE_TURNS=1 switches to Deepgram Flux + eager end-of-turn.

    Experimental: the LLM generates against the predicted end of turn and the
    response is released only when the turn commits, hiding the turn-decision
    window. Off by default — it replaces the local smart-turn analyzer and
    changes the STT service.
    """
    return os.getenv("STT_SPECULATIVE_TURNS", "").strip() in ("1", "true", "yes")


def _voice_max_tokens() -> int:
    """Output cap for a voice turn (VOICE_LLM_MAX_TOKENS, default 1024).

    A spoken reply is a few sentences; an uncapped generation that rambles is
    pure time-to-last-word latency and floor-hogging. 1024 matches the
    Anthropic path's non-thinking cap. Tool-call JSON fits comfortably.
    """
    try:
        return int(os.getenv("VOICE_LLM_MAX_TOKENS", "1024"))
    except ValueError:
        return 1024


async def build_pipeline_runner(pipeline):
    """Construct and register the meeting worker before the runner starts."""
    # Per-turn latency accounting: every user-to-bot interval broken into named
    # parts that sum to the measured total (VAD silence, turn decision, LLM
    # TTFB, TTS…). One [LATENCY] block per turn — this is the ground truth any
    # latency tuning starts from. Per-service TTFB metrics still log separately.
    latency_observer = UserBotLatencyObserver()

    @latency_observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        for line in breakdown.turn_contribution_lines():
            log_and_flush(logging.INFO, f"[LATENCY] {line}")

    task = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        check_dangling_tasks=True,
        observers=[latency_observer],
    )
    # SIGTERM is what the API sends a child that outstayed its socket; handled,
    # it cancels the pipeline so the shutdown path below still runs (final
    # transcript, MCP sessions) instead of the process just vanishing.
    runner = WorkerRunner(handle_sigterm=True)
    await runner.add_workers(task)
    return task, runner


def build_llm_service(persona: dict | None, system_instruction: str | None = None):
    """Build a Pipecat LLM service for OpenAI, Anthropic, or OpenAI-compatible Z.ai.

    Pipecat 1.12: model configuration uses a Settings dataclass. Tool calls
    remain sequential via LLMService.run_in_parallel, and the system prompt uses
    system_instruction — a leading {"role": "system"} context message is
    deprecated since 1.9 and stops working in 2.0.
    """
    provider = resolve_llm_provider(persona)
    model = resolve_llm_model(provider, persona)
    api_surface = None

    if provider == "anthropic":
        api_key = clean_string(os.getenv("ANTHROPIC_API_KEY"))
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

        if AnthropicLLMService is None:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic but the anthropic pipecat extra is not installed"
            )

        # Voice turns must start within a second or two, so thinking is off
        # wherever the model allows it; the setting that achieves that differs
        # per generation and a rejected request leaves the bot mute, so it is
        # resolved from the model id. Models whose thinking is always on run
        # at the lowest effort instead, with room for the thinking tokens that
        # count against max_tokens. The persona prompt (often tens of KB of
        # persona docs) is identical every turn, so prompt caching cuts both
        # time-to-first-token and cost.
        thinking_mode = resolve_anthropic_thinking(model)
        thinking_on = thinking_mode in ANTHROPIC_THINKING_ON_MODES
        settings_kwargs: dict = dict(
            model=model,
            system_instruction=system_instruction,
            max_tokens=4096 if thinking_on else 1024,
            enable_prompt_caching=True,
        )
        if thinking_mode != "omit":
            settings_kwargs["thinking"] = AnthropicLLMService.ThinkingConfig(
                type=thinking_mode
            )
        if thinking_on:
            settings_kwargs["extra"] = {"output_config": {"effort": "low"}}
        llm = AnthropicLLMService(
            api_key=api_key,
            run_in_parallel=False,
            settings=AnthropicLLMService.Settings(**settings_kwargs),
        )
    elif provider == "zai":
        api_key = clean_string(os.getenv("ZAI_API_KEY"))
        if not api_key:
            raise RuntimeError("ZAI_API_KEY is required when LLM_PROVIDER=zai")

        base_url = clean_string(os.getenv("ZAI_BASE_URL")) or DEFAULT_ZAI_BASE_URL
        llm = OpenAILLMService(
            api_key=api_key,
            run_in_parallel=False,
            base_url=base_url,
            settings=OpenAILLMService.Settings(
                model=model,
                system_instruction=system_instruction,
                max_tokens=_voice_max_tokens(),
            ),
        )
    else:
        api_key = clean_string(os.getenv("OPENAI_API_KEY"))
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")

        api_surface = resolve_openai_api_surface()
        service_tier = clean_string(os.getenv("OPENAI_SERVICE_TIER"))
        if api_surface == "responses":
            if OpenAIResponsesLLMService is None:
                raise RuntimeError(
                    "OPENAI_API_SURFACE=responses but the responses pipecat extra is not installed"
                )

            llm = OpenAIResponsesLLMService(
                api_key=api_key,
                run_in_parallel=False,
                service_tier=service_tier,
                settings=OpenAIResponsesLLMService.Settings(
                    model=model,
                    system_instruction=system_instruction,
                    # The Responses API caps output with max_output_tokens, which
                    # pipecat fills from max_completion_tokens; max_tokens is the
                    # Chat Completions name and would be ignored here. Reasoning
                    # stays off by default (pipecat's choice for latency), so
                    # the cap is spent on the spoken reply alone.
                    max_completion_tokens=_voice_max_tokens(),
                ),
            )
        else:
            llm = OpenAILLMService(
                api_key=api_key,
                run_in_parallel=False,
                service_tier=service_tier,
                settings=OpenAILLMService.Settings(
                    model=model,
                    system_instruction=system_instruction,
                    max_tokens=_voice_max_tokens(),
                ),
            )

    surface_detail = f", api_surface={api_surface}" if api_surface else ""
    log_and_flush(
        logging.INFO, f"[LLM] Initialized provider={provider}, model={model}{surface_detail}"
    )
    return llm


def _coerce_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def resolve_tts_speed(persona: dict | None) -> float:
    """Resolve TTS speed with request data taking precedence over env defaults."""
    persona = persona or {}
    speech_config = persona.get("speech") or persona.get("tts") or {}
    persona_speed = None
    if isinstance(speech_config, dict):
        persona_speed = (
            speech_config.get("speed")
            or speech_config.get("tts_speed")
            or speech_config.get("speech_speed")
        )
    persona_speed = (
        persona_speed
        or persona.get("tts_speed")
        or persona.get("speech_speed")
        or persona.get("speed")
    )
    speed = _coerce_float(persona_speed)
    if speed is None:
        speed = _coerce_float(
            os.getenv("CARTESIA_TTS_SPEED")
            or os.getenv("TTS_SPEED")
            or os.getenv("SPEECH_SPEED"),
            1.2,
        )
    return min(max(speed, 0.6), 1.5)


def resolve_tts_volume(persona: dict | None) -> float | None:
    """Resolve TTS volume (Cartesia honors 0.5-2.0); None = provider default."""
    persona = persona or {}
    volume = _coerce_float(
        persona.get("speech_volume") or os.getenv("CARTESIA_TTS_VOLUME")
    )
    if volume is None:
        return None
    return min(max(volume, 0.5), 2.0)


def resolve_tts_emotion(persona: dict | None) -> str | None:
    """Resolve the TTS emotion tag; None = let sonic-3.x infer from the text.

    Cartesia calls emotion control experimental and recommends relying on the
    model's own expressiveness, so this is opt-in per request/persona only.
    """
    persona = persona or {}
    emotion = persona.get("speech_emotion") or os.getenv("CARTESIA_TTS_EMOTION")
    emotion = clean_string(emotion) if emotion else None
    return emotion or None


def build_cartesia_tts_kwargs(
    *,
    api_key: str | None,
    voice_id: str | None,
    sample_rate: int,
    speed: float,
    volume: float | None = None,
    emotion: str | None = None,
    pronunciation_dict_id: str | None = None,
) -> dict:
    """Build Cartesia kwargs for Pipecat 1.12+ (Settings replaces voice_id/params).

    volume/emotion ride sonic-3's generation_config (None = provider default);
    pronunciation_dict_id points at a Cartesia-hosted dictionary so names like
    "MeetingBaas" or client jargon are said right.
    """
    generation: dict = {"speed": speed}
    if volume is not None:
        generation["volume"] = volume
    if emotion is not None:
        generation["emotion"] = emotion
    settings_kwargs: dict = {
        "voice": voice_id,
        "generation_config": GenerationConfig(**generation),
    }
    if pronunciation_dict_id:
        settings_kwargs["pronunciation_dict_id"] = pronunciation_dict_id
    kwargs = {
        "api_key": api_key,
        "sample_rate": sample_rate,
        "settings": CartesiaTTSService.Settings(**settings_kwargs),
    }
    # How long Cartesia buffers audio before the first chunk plays. Lower =
    # first word sooner; too low risks audible underruns. Unset = provider
    # default.
    buffer_ms = _coerce_float(os.getenv("CARTESIA_MAX_BUFFER_DELAY_MS"))
    if buffer_ms is not None:
        kwargs["max_buffer_delay_ms"] = int(buffer_ms)
    return kwargs


def build_mcp_context_prompt(mcp_config) -> str:
    """Summarize MCP metadata for the LLM without implying tool execution works."""
    if not mcp_config:
        return ""

    if not isinstance(mcp_config, dict):
        return (
            "\n\nMCP context was provided for this session, but its metadata was "
            "not in a structured format. Do not claim you called MCP tools unless "
            "tool execution is explicitly implemented in this runner."
        )

    lines = [
        "\n\nExternal MCP context has been provided for this session.",
        "Live MCP tools may be available as callable functions when the server config is connectable. Only say you used a tool after receiving a tool result.",
    ]
    if mcp_config.get("instructions"):
        lines.append(f"MCP instructions: {str(mcp_config['instructions'])[:500]}")

    servers = mcp_config.get("servers") or mcp_config.get("server") or []
    if isinstance(servers, dict):
        servers = [
            {"name": name, **value} if isinstance(value, dict) else {"name": name}
            for name, value in servers.items()
        ]
    if isinstance(servers, list) and servers:
        lines.append("MCP servers:")
        for server in servers[:8]:
            if isinstance(server, dict):
                name = server.get("name") or server.get("id") or server.get("url")
                if name:
                    server_line = f"- {name}"
                    if server.get("transport"):
                        server_line += f" ({server['transport']})"
                    if server.get("url"):
                        server_line += f": {server['url']}"
                    lines.append(server_line)
                    tools = server.get("tools") or []
                    if tools:
                        lines.append(f"  Tools: {', '.join(str(tool) for tool in tools[:20])}")
                    if server.get("instructions"):
                        lines.append(f"  Instructions: {str(server['instructions'])[:300]}")
            elif server:
                lines.append(f"- {str(server)[:180]}")

    tools = mcp_config.get("tools") or mcp_config.get("tool") or []
    if isinstance(tools, dict):
        tools = [
            {"name": name, **value} if isinstance(value, dict) else {"name": name}
            for name, value in tools.items()
        ]
    if isinstance(tools, list) and tools:
        lines.append("MCP tools/data advertised:")
        for tool in tools[:12]:
            if isinstance(tool, dict):
                name = tool.get("name") or tool.get("id") or tool.get("title")
                description = tool.get("description") or tool.get("summary")
                if name and description:
                    lines.append(f"- {name}: {str(description)[:180]}")
                elif name:
                    lines.append(f"- {name}")
            elif tool:
                lines.append(f"- {str(tool)[:180]}")

    return "\n".join(lines)


def _schema_from_mcp_tool(tool: dict) -> tuple[dict, list[str]]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    if not isinstance(schema, dict):
        return {}, []
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return properties, required


def _tool_result_to_text(result) -> str:
    if result is None:
        return "MCP tool returned no result."
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    parts.append(str(item))
                    continue
                if item.get("type") == "text" and item.get("text") is not None:
                    parts.append(str(item["text"]))
                elif item.get("type") == "json" and item.get("json") is not None:
                    parts.append(json.dumps(item["json"], ensure_ascii=False))
                elif item.get("type") == "data" and item.get("data") is not None:
                    parts.append(json.dumps(item["data"], ensure_ascii=False))
                elif item.get("type") == "resource" and item.get("resource"):
                    parts.append(json.dumps(item["resource"], ensure_ascii=False))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            if parts:
                return "\n".join(parts)
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"], ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


@dataclass
class LiveMCPTool:
    function_name: str
    server_name: str
    tool_name: str
    client: object
    schema: dict


class LiveMCPManager:
    """Owns live MCP clients and maps Pipecat function names to MCP tools."""

    def __init__(self, mcp_config: dict | None):
        self._mcp_config = mcp_config or {}
        self._clients = []
        self._tools: dict[str, LiveMCPTool] = {}

    async def connect(self) -> list[dict]:
        discovered = []
        for server in self._mcp_config.get("servers") or []:
            if not isinstance(server, dict) or server.get("enabled") is False:
                continue
            if not server.get("transport"):
                continue

            client = self._build_client(server)
            server_name = str(server.get("name") or "mcp")
            try:
                await client.initialize()
                self._clients.append(client)
                tool_allowlist = set(server.get("tool_allowlist") or [])
                for tool in await client.list_tools():
                    tool_name = str(tool.get("name") or "")
                    if not tool_name:
                        continue
                    if tool_allowlist and tool_name not in tool_allowlist:
                        continue
                    function_name = build_mcp_tool_name(server_name, tool_name)
                    tool_ref = LiveMCPTool(
                        function_name=function_name,
                        server_name=server_name,
                        tool_name=tool_name,
                        client=client,
                        schema=tool,
                    )
                    self._tools[function_name] = tool_ref
                    discovered.append(
                        {
                            **tool,
                            "server_name": server_name,
                            "function_name": function_name,
                        }
                    )
            except Exception as exc:
                await client.close()
                if server.get("required"):
                    await self.close()
                    raise
                log_and_flush(
                    logging.WARNING,
                    f"[MCP] Could not connect server {server_name}: {exc}",
                )
        return discovered

    def _build_client(self, server: dict):
        transport = str(server.get("transport") or "").lower()
        timeout = float(server.get("timeout_seconds") or 12)
        if transport == "stdio":
            if os.getenv("MCP_ALLOW_STDIO", "").lower() not in {"1", "true", "yes"}:
                raise McpClientError("stdio MCP transport is disabled")
            command = [server["command"], *(server.get("args") or [])]
            return StdioMcpClient(command=command, env=server.get("env"))
        if transport in {"http", "streamable_http", "streamable-http", "sse"}:
            return HttpMcpClient(
                url=server["url"],
                headers=server.get("headers"),
                timeout_seconds=timeout,
            )
        raise McpClientError(f"Unsupported MCP transport: {transport}")

    async def call_tool_by_function_name(self, function_name: str, arguments: dict):
        tool_ref = self._tools.get(function_name)
        if not tool_ref:
            raise McpClientError(f"Unknown MCP function: {function_name}")
        return await tool_ref.client.call_tool(tool_ref.tool_name, arguments or {})

    async def close(self):
        await asyncio.gather(
            *(client.close() for client in self._clients),
            return_exceptions=True,
        )


async def setup_mcp_tools(llm, tool_schemas: list, mcp_config: dict | None):
    """Connect configured MCP servers and register their tools with Pipecat."""
    if not mcp_config:
        return None

    manager = LiveMCPManager(mcp_config)
    discovered = await manager.connect()
    if not discovered:
        log_and_flush(logging.INFO, "[MCP] No live MCP tools discovered")
        return manager

    for tool_ref in discovered:
        function_name = tool_ref["function_name"]
        properties, required = _schema_from_mcp_tool(tool_ref)

        async def call_mcp_tool(params, name=function_name):
            try:
                result = await manager.call_tool_by_function_name(name, params.arguments)
                await params.result_callback(_tool_result_to_text(result))
            except Exception as exc:
                log_and_flush(logging.ERROR, f"[MCP] Tool {name} failed: {exc}")
                await params.result_callback(f"MCP tool {name} failed: {exc}")

        llm.register_function(function_name, call_mcp_tool)
        tool_schemas.append(
            FunctionSchema(
                name=function_name,
                description=tool_ref.get("description")
                or f"Call MCP tool {tool_ref['name']} on {tool_ref['server_name']}",
                properties=properties,
                required=required,
            )
        )

    log_and_flush(logging.INFO, f"[MCP] Registered {len(discovered)} live MCP tool(s)")
    return manager


def save_transcript(bot_id: str, persona_name: str, messages: list, heard: list | None = None):
    """Save the conversation transcript to a JSON file for webhook processing.

    ``messages`` is the model's context (what it was told and what it said);
    ``heard`` is everything said in the meeting in order, bot turns included,
    which in mentioned mode is far more than the model's context.
    """
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    transcript_file = os.path.join(TRANSCRIPT_DIR, f"{bot_id}.json")

    # Filter out system messages and just keep user/assistant conversation
    conversation = []
    for msg in messages:
        if msg.get("role") in ["user", "assistant"]:
            conversation.append({
                "role": msg["role"],
                "content": msg.get("content", "")
            })

    data = {
        "bot_id": bot_id,
        "persona_name": persona_name,
        "timestamp": datetime.now().isoformat(),
        "messages": conversation,
        "heard": heard or [],
    }

    # Atomic write: the webhook may read this file at any moment (call_ended
    # summary). A bare truncate+write let it observe half-written JSON and
    # ack the callback as "done" with a lost summary. Write a unique temp file
    # in the same dir, fsync, then rename over the target.
    tmp_file = f"{transcript_file}.{os.getpid()}.tmp"
    try:
        with open(tmp_file, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, transcript_file)
        # fsync the directory so the rename itself is durable — fsyncing only
        # the file does not persist the new directory entry across a reboot.
        dir_fd = os.open(TRANSCRIPT_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as e:
        # Never let a transcript write failure crash the pipeline; clean up.
        with contextlib.suppress(OSError):
            os.remove(tmp_file)
        log_and_flush(logging.WARNING, f"[TRANSCRIPT] Could not save transcript: {e}")
        return

    log_and_flush(logging.DEBUG, f"[TRANSCRIPT] Saved transcript to {transcript_file}")

# Function tool implementations
async def get_weather(params: FunctionCallParams):
    """Get the current weather for a location."""
    arguments = params.arguments
    location = arguments["location"]
    format = arguments["format"]  # Default to Celsius if not specified
    unit = (
        "m" if format == "celsius" else "u"
    )  # "m" for metric, "u" for imperial in wttr.in

    url = f"https://wttr.in/{location}?format=%t+%C&{unit}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                weather_data = await response.text()
                await params.result_callback(
                    f"The weather in {location} is currently {weather_data} ({format.capitalize()})."
                )
            else:
                await params.result_callback(
                    f"Failed to fetch the weather data for {location}."
                )


async def get_time(params: FunctionCallParams):
    """Get the current time for a location."""
    arguments = params.arguments
    location = arguments["location"]

    # Set timezone based on the provided location
    try:
        timezone = pytz.timezone(location)
        current_time = datetime.now(timezone)
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
        await params.result_callback(f"The current time in {location} is {formatted_time}.")
    except pytz.UnknownTimeZoneError:
        await params.result_callback(
            f"Invalid location specified. Could not determine time for {location}."
        )


async def save_call_summary(params: FunctionCallParams):
    """Save a summary of the discovery call to a file."""
    arguments = params.arguments
    prospect_name = arguments.get("prospect_name", "Unknown")
    company_name = arguments.get("company_name", "Unknown")
    summary = arguments.get("summary", "")
    next_steps = arguments.get("next_steps", "")
    qualified = arguments.get("qualified", "unknown")

    # Create call_summaries directory if it doesn't exist
    summaries_dir = os.path.join(get_state_dir(), "call_summaries")
    os.makedirs(summaries_dir, exist_ok=True)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() else "_" for c in prospect_name)
    filename = f"{timestamp}_{safe_name}.md"
    filepath = os.path.join(summaries_dir, filename)

    # Write the summary
    content = f"""# Discovery Call Summary

**Date:** {datetime.now().strftime("%Y-%m-%d %H:%M")}
**Prospect:** {prospect_name}
**Company:** {company_name}
**Qualified:** {qualified}

## Summary
{summary}

## Next Steps
{next_steps}
"""

    with open(filepath, "w") as f:
        f.write(content)

    log_and_flush(logging.INFO, f"[SUMMARY] Saved call summary to {filepath}")
    await params.result_callback(f"Great, I've saved the summary of our call. Thank you so much for your time today, {prospect_name}!")


class SpeakerLabeler(FrameProcessor):
    """Prefix final transcriptions with the speaker's display name.

    MeetingBaas tells the API who is speaking (names, humans included); the
    API mirrors that into a per-meeting speakers file (utils/speakers.py).
    When a final transcription lands and exactly one recent speaker other than
    this bot matches, the text becomes "Name: text". The LLM can then tell a
    remark addressed to it from participants talking to each other — the whole
    basis of staying quiet in a multi-party meeting. Ambiguous (overlapping
    speakers, stale file): leave the text unlabeled rather than guess.
    """

    # The label is prepended before the wake-phrase check runs, so a speaker
    # whose own name contains a wake phrase would wake the bot just by being
    # labelled. Such speakers are left unlabelled.
    def __init__(
        self, meeting_url: str, my_name: str, wake_phrases: list[str] | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        self._meeting_url = meeting_url
        self._my_name = (my_name or "").strip().lower()
        self._wake_phrases = list(wake_phrases or [])
        self._unlabelled: set[str] = set()
        # Final transcriptions carry other people's speech; they reach the log
        # only when an operator asks for it (SPEAKING_LOG_HEARD=1), else at DEBUG.
        self._heard_level = (
            logging.INFO
            if os.getenv("SPEAKING_LOG_HEARD", "").strip().lower() in ("1", "true", "yes")
            else logging.DEBUG
        )

    def _labelled(self, name: str, text: str) -> str:
        if matches_wake_phrase(name, self._wake_phrases):
            if name not in self._unlabelled:
                self._unlabelled.add(name)
                log_and_flush(
                    logging.INFO,
                    f"[LABEL] Not labelling speaker {name!r}: the name contains a wake phrase",
                )
            return text
        return text if text.startswith(f"{name}:") else f"{name}: {text}"

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if type(frame).__name__ == "TranscriptionFrame" and getattr(frame, "text", None):
            candidates = [
                name
                for name in read_recent_speakers(self._meeting_url)
                if name.strip().lower() != self._my_name
            ]
            if len(candidates) == 1:
                frame.text = self._labelled(candidates[0], frame.text)
            # What the bot heard, as the wake-phrase and the model see it.
            log_and_flush(self._heard_level, f"[HEARD] {frame.text[:200]}")
        await self.push_frame(frame, direction)


class SpeechInjectionProbe(FrameProcessor):
    """Logs the exact on/off of THIS bot's outgoing TTS audio injection.

    Deterministic ground truth of when our bot is pushing audio into the
    meeting (as opposed to MeetingBaas' speaker *detection*): sits right after
    the TTS service and logs the bracketing start/stop frames. For correlating
    our speech injection against an external speaking indicator. Grep the
    journal for "[SPEECH-INJECT]". Matches frame class names so it survives
    Pipecat version churn.
    """

    _START = {"TTSStartedFrame", "BotStartedSpeakingFrame"}
    _STOP = {"TTSStoppedFrame", "BotStoppedSpeakingFrame"}

    def __init__(self, my_name: str, **kwargs):
        super().__init__(**kwargs)
        self._my_name = my_name

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        cls = type(frame).__name__
        if cls in self._START:
            log_and_flush(logging.INFO, f"[SPEECH-INJECT] {self._my_name} START ({cls})")
        elif cls in self._STOP:
            log_and_flush(logging.INFO, f"[SPEECH-INJECT] {self._my_name} STOP ({cls})")
        await self.push_frame(frame, direction)


class ConversationMemory(FrameProcessor):
    """Remembers every final transcription and briefs the model when it is addressed.

    Sits after the speaker labeller and before the user aggregator. In
    mentioned mode the wake-phrase strategy drops unaddressed speech, so this
    is where the bot keeps the meeting: unaddressed turns go into the memory,
    and a turn that names the bot is rewritten to carry what was said since
    the bot last spoke. In always mode everything reaches the model anyway;
    the memory still records the log for the transcript and briefing.
    """

    def __init__(self, memory: MeetingMemory, wake_phrases: list[str] | None, mentioned: bool, **kwargs):
        super().__init__(**kwargs)
        self._memory = memory
        self._wake_phrases = list(wake_phrases or [])
        self._mentioned = mentioned

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if type(frame).__name__ == "TranscriptionFrame" and getattr(frame, "text", None):
            text = frame.text
            if self._mentioned and matches_wake_phrase(text, self._wake_phrases):
                frame.text = self._memory.wake_text(text)
                self._memory.add(text, kind="human")
                self._memory.mark_delivered()
            else:
                self._memory.add(text, kind="human")
                if not self._mentioned:
                    self._memory.mark_delivered()
        await self.push_frame(frame, direction)


class UtteranceTap(FrameProcessor):
    """Reports what the bot says, one event per reply, to the caller's callback.

    Sits right after the TTS service. The TTS emits its text sentence by
    sentence between started/stopped frames; the sentences of one reply are
    coalesced (a reply's sentences follow each other within a second) so the
    caller gets "what the bot said" at turn granularity, as a person would
    quote it. The transcript file stays the complete record.
    """

    COALESCE_SECS = 1.0

    def __init__(
        self,
        sink: CustomerEventSink,
        memory: MeetingMemory | None = None,
        bot_name: str = "Bot",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._sink = sink
        self._memory = memory
        self._bot_name = bot_name
        self._parts: list[str] = []
        self._started_at: float | None = None
        self._flush_task: asyncio.Task | None = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self._sink.enabled or self._memory is not None:
            if isinstance(frame, TTSStartedFrame):
                self._cancel_flush()
                if self._started_at is None:
                    self._started_at = time.time()
            elif isinstance(frame, TTSTextFrame):
                text = (getattr(frame, "text", "") or "").strip()
                if text:
                    self._parts.append(text)
            elif isinstance(frame, TTSStoppedFrame):
                self._cancel_flush()
                self._flush_task = asyncio.create_task(self._flush_later())
        await self.push_frame(frame, direction)

    def _cancel_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.COALESCE_SECS)
        await self.flush()

    async def flush(self) -> None:
        """Send what was collected so far as one utterance event."""
        text = " ".join(self._parts).strip()
        started_at = self._started_at
        self._parts = []
        self._started_at = None
        if not text:
            return
        if self._memory is not None:
            # Recorded, not "delivered": speech heard while the bot talked is
            # still owed to the model at the next wake.
            self._memory.add(f"{self._bot_name}: {text}", kind="bot", at=started_at)
        await self._sink.emit(
            EVENT_UTTERANCE,
            {"text": text, "started_at": started_at, "ended_at": time.time()},
        )


class FloorGate(FrameProcessor):
    """Holds LLM→TTS frames while a sibling bot is speaking in the meeting.

    Sits between the LLM and TTS in the pipeline. Content frames are buffered
    while the per-meeting floor file (written by the API process from
    MeetingBaas speaker-state events) names another one of OUR bots as the
    current speaker. System frames always pass straight through. A hold
    timeout guarantees frames are never stuck (e.g. floor writer died), and a
    small random jitter before release de-synchronizes two bots grabbing a
    freed floor in the same instant.
    """

    POLL_SECS = 0.2
    MAX_HOLD_SECS = 20.0
    # The floor file is read at most this often. Every TTS audio chunk passes
    # through here (50+ frames/s), and a JSON read per frame on the event loop
    # was measurable jitter for nothing: the floor cannot change faster than
    # the API process writes it.
    CHECK_INTERVAL_SECS = 0.1

    def __init__(self, meeting_url: str, my_name: str, **kwargs):
        super().__init__(**kwargs)
        self._meeting_url = meeting_url
        self._my_name = my_name
        self._buffer = []
        self._flush_task = None
        self._blocked_cached = False
        self._blocked_checked_at = 0.0

    def _blocked(self) -> bool:
        now = time.monotonic()
        if now - self._blocked_checked_at < self.CHECK_INTERVAL_SECS:
            return self._blocked_cached
        self._blocked_checked_at = now
        try:
            self._blocked_cached = floor_blocked_by_sibling(
                self._meeting_url, self._my_name
            )
        except Exception:
            self._blocked_cached = False  # never let floor bookkeeping kill the pipeline
        return self._blocked_cached

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM or isinstance(frame, SystemFrame):
            await self.push_frame(frame, direction)
            return

        # Preserve ordering: once anything is buffered, everything queues
        # behind it until the flush task drains.
        if self._buffer or self._blocked():
            self._buffer.append((frame, direction))
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_when_free())
            return

        await self.push_frame(frame, direction)

    async def _flush_when_free(self):
        import random

        waited = 0.0
        while self._blocked() and waited < self.MAX_HOLD_SECS:
            await asyncio.sleep(self.POLL_SECS)
            waited += self.POLL_SECS
        if waited > 0:
            # Collision-avoidance jitter when the floor just freed up
            await asyncio.sleep(random.uniform(0.05, 0.5))
        if waited >= self.MAX_HOLD_SECS:
            log_and_flush(logging.WARNING, "[FLOOR] Hold timeout — releasing buffered frames")
        while self._buffer:
            frame, direction = self._buffer.pop(0)
            await self.push_frame(frame, direction)


async def main(
    meeting_url: str = "",
    persona_name: str = "Meeting Bot",
    entry_message: str = "",
    bot_image: str = "",
    streaming_audio_frequency: str = "24khz",
    websocket_url: str = "",
    enable_tools: bool = True,
    persona_data: dict = None,
    meetingbaas_bot_id: str = "",
):
    """
    Run the MeetingBaas bot with specified configurations

    Args:
        meeting_url: URL to join the meeting
        persona_name: Name to display for the bot
        entry_message: Message to send when joining
        bot_image: URL for bot avatar
        streaming_audio_frequency: Audio frequency for streaming (16khz or 24khz)
        websocket_url: Full WebSocket URL to connect to, including any path
        enable_tools: Whether to enable function tools like weather and time
        persona_data: Full resolved persona dict from the parent process. When it
            carries a prompt (always the case for dynamic prompt-derived
            personas, which never exist on disk), it is used directly instead of
            re-resolving the persona from config/personas.
    """
    # Set TaskManager event loop FIRST, before any other pipecat operations
    from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams
    TaskManager().setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    
    log_and_flush(
        logging.INFO,
        f"[STARTUP] MeetingBaas bot launching with persona: {persona_name} "
        f"(imports+spawn took {time.monotonic() - _PROC_T0:.1f}s)",
    )
    load_dotenv()

    if not websocket_url:
        log_and_flush(logging.ERROR, "[ERROR] WebSocket URL not provided")
        return
    log_and_flush(logging.INFO, f"[CONFIG] Using WebSocket URL: {websocket_url}")
    # Extract bot_id from the websocket_url if possible
    # Format is usually: ws://localhost:{PORT}/pipecat/{client_id} or the ngrok URL
    parts = websocket_url.split("/")
    # Dynamically determine the expected port for localhost URLs
    expected_local_port = os.getenv("PORT", "7014")
    if "localhost" in websocket_url and f":{expected_local_port}/pipecat/" in websocket_url:
        bot_id = parts[-1] if len(parts) > 3 else "unknown"
    elif "ngrok.io" in websocket_url:
        # Assume ngrok URL will have the client_id as the last part after /pipecat/
        bot_id = parts[-1] if len(parts) > 3 and parts[-2] == "pipecat" else "unknown"
    else:
        # Fallback for other URL formats or if client_id is not easily extractable
        bot_id = parts[-1] if len(parts) > 3 else "unknown"
    logger.info(f"Using bot ID: {bot_id}")


    output_sample_rate = 24000 if streaming_audio_frequency == "24khz" else 16000
    vad_sample_rate = 16000
    log_and_flush(logging.INFO, f"[CONFIG] Audio frequency: {streaming_audio_frequency} (output: {output_sample_rate}, VAD: {vad_sample_rate})")

    print("Event loop set for Pipecat:", asyncio.get_running_loop())

    transport = WebsocketClientTransport(
        uri=websocket_url,
        params=WebsocketClientParams(
            audio_out_sample_rate=output_sample_rate,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_enabled=True,
            audio_in_passthrough=True,
            serializer=ProtobufFrameSerializer(),
        ),
    )
    # Audio can reach this socket before the pipeline has started the input
    # side (the API replays startup audio on connect); hold it until then
    # instead of letting it kill the receive loop. See utils/early_audio.py.
    audio_input = transport.input()
    hold_messages_until_started(audio_input)
    log_and_flush(logging.INFO, "[TRANSPORT] WebSocket transport initialized")
    log_and_flush(logging.INFO, f"[TRANSPORT] URI: {websocket_url}")
    log_and_flush(logging.INFO, f"[TRANSPORT] Audio out enabled: True, sample_rate: {output_sample_rate}")
    log_and_flush(logging.INFO, "[TRANSPORT] Audio in enabled: True, VAD sample_rate: 16000")

    # Add WebSocket connection event handlers for debugging
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        log_and_flush(logging.INFO, "[WEBSOCKET] Client connected to WebSocket server")
        
    @transport.event_handler("on_client_disconnected") 
    async def on_client_disconnected(transport, client):
        log_and_flush(logging.INFO, "[WEBSOCKET] Client disconnected from WebSocket server")

    @transport.event_handler("on_connection_established")
    async def on_connection_established(transport):
        log_and_flush(logging.INFO, "[WEBSOCKET] WebSocket connection established successfully")
        
    @transport.event_handler("on_connection_error")
    async def on_connection_error(transport, error):
        log_and_flush(logging.ERROR, f"[WEBSOCKET] Connection error: {error}")

    # Prefer the persona handed over by the parent process: dynamic
    # prompt-derived personas live only in the API process's memory, so a
    # disk lookup would KeyError and leave the bot mute in the meeting.
    if persona_data and persona_data.get("prompt"):
        persona = persona_data
        log_and_flush(logging.INFO, f"[PERSONA] Using persona data passed from parent process: '{persona.get('name', persona_name)}'")
    else:
        persona_manager = PersonaManager()
        log_and_flush(logging.INFO, f"[PERSONA] Available personas: {list(persona_manager.personas.keys())}")
        log_and_flush(logging.INFO, f"[PERSONA] Looking for persona: '{persona_name}'")
        persona = persona_manager.get_persona(persona_name)
        if not persona:
            log_and_flush(logging.ERROR, f"[ERROR] Persona '{persona_name}' not found")
            return
    log_and_flush(logging.INFO, f"[PERSONA] Loaded persona: {persona_name}")
    log_and_flush(logging.INFO, f"[PERSONA] Entry message: {persona.get('entry_message', 'NONE')[:100]}...")

    additional_content = persona.get("additional_content", "")
    if additional_content:
        log_and_flush(logging.INFO, "[PERSONA] Found additional content for persona")
    else:
        log_and_flush(logging.INFO, "[PERSONA] No additional content found for persona")

    # Use the voice ID from the persona data, falling back to env var if not set
    voice_id = persona.get("cartesia_voice_id") or os.getenv("CARTESIA_VOICE_ID")
    log_and_flush(logging.INFO, f"[PERSONA] Using voice ID: {voice_id}")

    tts_speed = resolve_tts_speed(persona)
    tts_volume = resolve_tts_volume(persona)
    tts_emotion = resolve_tts_emotion(persona)
    tts_kwargs = build_cartesia_tts_kwargs(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=voice_id,
        sample_rate=output_sample_rate,
        speed=tts_speed,
        volume=tts_volume,
        emotion=tts_emotion,
        pronunciation_dict_id=clean_string(os.getenv("CARTESIA_PRONUNCIATION_DICT_ID")),
    )
    tts = CartesiaTTSService(**tts_kwargs)
    log_and_flush(
        logging.INFO,
        f"[TTS] Cartesia TTS initialized with sample_rate={output_sample_rate}, "
        f"voice_id={voice_id}, speed={tts_speed}, volume={tts_volume}, emotion={tts_emotion}",
    )

    # Assemble the system prompt BEFORE building the LLM service: since
    # pipecat 1.9 the system prompt belongs on the service (system_instruction),
    # not as a leading {"role": "system"} message in LLMContext. The voice
    # guidance it appends is part of the latency budget: TTS speaks sentence
    # by sentence, and the old instruction to drop punctuation meant the whole
    # reply had to finish generating before a word was heard.
    mcp_context = build_mcp_context_prompt(persona.get("mcp"))
    if mcp_context:
        log_and_flush(logging.INFO, "[MCP] Added MCP metadata to system context")
    # The name the meeting shows; the persona's own name is only a fallback.
    bot_display_name = (
        persona.get("display_name") or persona.get("name") or persona_name
    )
    authority = persona.get("authority") if isinstance(persona.get("authority"), dict) else None
    owner_name = str((authority or {}).get("owner_name") or "the owner")
    authority_context = build_authority_prompt(authority, bot_display_name)
    if authority_context:
        log_and_flush(logging.INFO, f"[AUTHORITY] Representing {owner_name}")
    system_content = build_system_prompt(
        persona, persona_name, mcp_context, authority_context
    )

    llm = build_llm_service(persona, system_instruction=system_content)

    # Where the bot's own events go (what it said, what it escalated), and the
    # ledger of what it consumed on each provider.
    sink = CustomerEventSink(
        persona.get("callback_url"),
        os.getenv("CUSTOMER_CALLBACK_SECRET"),
        bot_id=bot_id,
        meetingbaas_bot_id=meetingbaas_bot_id or None,
        log=lambda message: log_and_flush(logging.WARNING, message),
    )
    if sink.enabled:
        log_and_flush(logging.INFO, "[EVENTS] Posting bot events to the caller's callback_url")
    llm_provider = resolve_llm_provider(persona)
    ledger = UsageLedger(
        bot_id=bot_id,
        meetingbaas_bot_id=meetingbaas_bot_id or None,
        llm_provider=llm_provider,
        llm_model=resolve_llm_model(llm_provider, persona),
        key_sources=dict(persona.get("key_sources") or {}),
    )

    mcp_manager = None
    if enable_tools:
        log_and_flush(logging.INFO, "[TOOLS] Registering function tools")
        llm.register_function("get_weather", get_weather)
        llm.register_function("get_time", get_time)
        llm.register_function("save_call_summary", save_call_summary)

        # Define function schemas
        weather_function = FunctionSchema(
            name="get_weather",
            description="Get the current weather",
            properties={
                "location": {
                    "type": "string",
                    "description": "The city and state, e.g. San Francisco, CA",
                },
                "format": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "The temperature unit to use. Infer this from the users location.",
                },
            },
            required=["location", "format"],
        )

        time_function = FunctionSchema(
            name="get_time",
            description="Get the current time for a specific location",
            properties={
                "location": {
                    "type": "string",
                    "description": "The location for which to retrieve the current time (e.g., 'Asia/Kolkata', 'America/New_York')",
                },
            },
            required=["location"],
        )

        save_call_summary_function = FunctionSchema(
            name="save_call_summary",
            description="Save a summary of the discovery call. Use this at the end of a sales or discovery call to record the key information gathered.",
            properties={
                "prospect_name": {
                    "type": "string",
                    "description": "The name of the prospect/person you spoke with",
                },
                "company_name": {
                    "type": "string",
                    "description": "The name of the prospect's company",
                },
                "summary": {
                    "type": "string",
                    "description": "A summary of the conversation including: their current situation, pain points, goals, and any relevant details about their needs",
                },
                "next_steps": {
                    "type": "string",
                    "description": "The agreed upon next steps, such as scheduling a demo or follow-up call",
                },
                "qualified": {
                    "type": "string",
                    "enum": ["yes", "no", "maybe"],
                    "description": "Whether the prospect seems qualified and a good fit for the product",
                },
            },
            required=["prospect_name", "company_name", "summary", "next_steps", "qualified"],
        )

        tool_schemas = [weather_function, time_function, save_call_summary_function]
        mcp_manager = await setup_mcp_tools(llm, tool_schemas, persona.get("mcp"))
    else:
        log_and_flush(logging.INFO, "[TOOLS] Function tools are disabled")
        tool_schemas = []

    if authority:
        # A representative always has its one tool, whatever enable_tools says:
        # the prompt tells it to escalate instead of deciding, so it must be able to.
        escalation_count = {"n": 0}

        async def escalate_to_owner(params: FunctionCallParams):
            arguments = params.arguments or {}
            escalation_count["n"] += 1
            number = escalation_count["n"]
            escalation = new_escalation(
                question=str(arguments.get("question") or ""),
                category=str(arguments.get("category") or "other"),
                context=arguments.get("context"),
            )
            escalation["number"] = number
            try:
                record_escalation(get_state_dir(), bot_id, escalation)
            except OSError as e:
                log_and_flush(logging.WARNING, f"[ESCALATION] Could not record escalation: {e}")
            log_and_flush(
                logging.INFO,
                f"[ESCALATION] #{number} ({escalation['category']}): {escalation['question'][:160]}",
            )
            await sink.emit(EVENT_ESCALATION, escalation)
            await params.result_callback(escalation_reply_for_llm(owner_name, number))

        llm.register_function(ESCALATE_TOOL_NAME, escalate_to_owner)
        tool_schemas.append(escalation_tool_schema())
        log_and_flush(logging.INFO, f"[TOOLS] Registered {ESCALATE_TOOL_NAME}")

    tools = ToolsSchema(standard_tools=tool_schemas) if tool_schemas else None

    # Per-request language (BCP-47 or Deepgram's "multi"), then the persona's
    # own, then US English as before.
    language = (
        clean_string(persona.get("language"))
        or clean_string(persona.get("language_code"))
        or "en-US"
    )
    log_and_flush(logging.INFO, f"[PERSONA] Using language: {language}")

    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
    log_and_flush(logging.INFO, f"[STT] Deepgram API key present: {bool(deepgram_api_key)}")
    log_and_flush(logging.INFO, f"[STT] Deepgram config: encoding=linear16, sample_rate={output_sample_rate}, language={language}")

    # The bot's name is rarely a dictionary word; boost it so a mention is
    # transcribed as written and the wake phrase can match it. Both Deepgram
    # services take the same keyterm setting.
    keyterms = keyterms_for(bot_display_name, persona.get("wake_phrases"))
    if keyterms:
        log_and_flush(logging.INFO, f"[STT] Boosting key terms: {keyterms}")
    wake_phrases = wake_phrases_for(bot_display_name, persona.get("wake_phrases"))
    if speculative_turns_enabled():
        # Flux is the Deepgram service that emits eager end-of-turn
        # predictions; the Eager turn strategies below depend on them.
        flux_kwargs: dict = {}
        if keyterms:
            flux_kwargs["settings"] = DeepgramFluxSTTService.Settings(keyterm=keyterms)
        stt = DeepgramFluxSTTService(
            api_key=deepgram_api_key,
            # Flux takes 16-bit PCM at any supported rate; the rate rides in
            # sample_rate, there is no "linear24" encoding.
            sample_rate=output_sample_rate,
            flux_encoding="linear16",
            # Without this Flux emits no eager end-of-turn predictions and the
            # Eager strategies below never speculate.
            enable_eager_end_of_turn=True,
            **flux_kwargs,
        )
    else:
        stt_settings_kwargs: dict = {"language": language}
        if keyterms:
            stt_settings_kwargs["keyterm"] = keyterms
        # How many ms of silence Deepgram waits before finalizing a transcript.
        # Lower = faster final transcripts = faster replies; too low splits
        # sentences. Unset = Deepgram's default.
        dg_endpointing = _coerce_float(os.getenv("DEEPGRAM_ENDPOINTING_MS"))
        if dg_endpointing is not None:
            stt_settings_kwargs["endpointing"] = int(dg_endpointing)
        stt = DeepgramSTTService(
            api_key=deepgram_api_key,
            encoding="linear16" if streaming_audio_frequency == "16khz" else "linear24",
            sample_rate=output_sample_rate,
            settings=DeepgramSTTService.Settings(**stt_settings_kwargs),
        )

    # stt = GladiaSTTService(
    #     api_key=os.getenv("GLADIA_API_KEY"),
    #     encoding="linear16" if streaming_audio_frequency == "16khz" else "linear24",
    #     sample_rate=output_sample_rate,
    #     language=language,  # Use language from persona
    # )

    bot_name = persona_name or "Bot"
    log_and_flush(logging.INFO, f"[BOT] Using bot name: {bot_name}")

    # The system prompt was set on the LLM service above (system_instruction);
    # the context starts with no messages. A leading "system" message here is
    # deprecated since pipecat 1.9 and stops working in 2.0.
    messages = []

    # Create the context object - with or without tools. enable_tools already
    # decided what went into the schema list; a representative's escalation
    # tool is in it regardless, and the model must see it.
    if tools:
        context = LLMContext(messages, tools)
    else:
        context = LLMContext(messages)

    # Turn-taking. Precedence: per-request turn_config (rides in the persona
    # dict from the API) > VAD_* / TURN_* env vars > utils.turn_taking defaults.
    # Two stages decide that a speaker has finished: the VAD notices a pause
    # (stop_secs, 0.2 s), then the local smart-turn model judges from the
    # audio whether the thought is complete. Complete: the reply starts as
    # soon as the final transcript lands. Mid-thought: the bot waits up to
    # unfinished_pause_secs of silence before answering anyway. Pipecat ran
    # this model by default already, but with its 3 s fallback on top of a
    # 0.8 s VAD stop; making it explicit puts the numbers where they can be
    # read and tuned.
    turn = resolve_turn_taking(persona.get("turn_config") if persona else None)
    log_and_flush(logging.INFO, f"[TURN] {turn}")
    vad_params = VADParams(
        confidence=turn.confidence,
        start_secs=turn.start_secs,
        stop_secs=turn.stop_secs,
        min_volume=turn.min_volume,
    )
    # Interruption hygiene for multi-party meetings: a cough, an "uh-huh"
    # or cross-talk should not cut the bot off mid-sentence. The word
    # threshold applies only while the bot is speaking; when it is silent,
    # a turn starts normally. min_interrupt_words=0 restores barge-in on
    # any speech.
    start_strategies = []
    # respond_mode="mentioned": the bot's name must be heard in a final
    # transcription before a turn starts, and again before the next one
    # (single_activation). Pipecat's other mode keeps the window open while
    # anyone keeps talking, which in a lively meeting means one mention and
    # the bot listens for the rest of the call. RESPOND_MENTION_WINDOW_SECS is
    # how long the turn that follows a mention may take to complete. Placed
    # FIRST so it gates the other start strategies.
    respond_mode = str((persona or {}).get("respond_mode") or "always").lower()
    if respond_mode == "mentioned":
        # The name participants say is the one the meeting shows (bot_display_name,
        # resolved above), not the persona's.
        wake_window = _coerce_float(os.getenv("RESPOND_MENTION_WINDOW_SECS"), 10.0)
        start_strategies.append(
            WakePhraseUserTurnStartStrategy(
                phrases=wake_phrases,
                timeout=wake_window,
                single_activation=True,
            )
        )
        log_and_flush(
            logging.INFO,
            f"[RESPOND] mentioned-only mode: wake phrases {wake_phrases}, "
            f"window {wake_window}s",
        )
    if turn.min_interrupt_words > 0:
        start_strategies.append(
            MinWordsUserTurnStartStrategy(min_words=turn.min_interrupt_words)
        )
    start_strategies = start_strategies or None
    if speculative_turns_enabled():
        # EXPERIMENTAL (STT_SPECULATIVE_TURNS=1): Deepgram Flux predicts the
        # end of a turn eagerly and the LLM generates against the prediction;
        # the response is held and released only when the turn commits — the
        # turn-decision window is spent generating instead of waiting. The
        # service owns turn detection in this mode, so Eager REPLACES the
        # local smart-turn analyzer (running both would double-answer turns).
        turn_strategies = EagerUserTurnStrategies()
        if start_strategies:
            turn_strategies.start = start_strategies
        log_and_flush(
            logging.WARNING,
            "[TURN] Speculative turns enabled (Deepgram Flux + eager end-of-turn)"
            " — experimental",
        )
    else:
        turn_strategies = UserTurnStrategies(
            start=start_strategies,
            stop=[
                TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=LocalSmartTurnAnalyzerV3(
                        params=SmartTurnParams(stop_secs=turn.unfinished_pause_secs)
                    )
                )
            ],
        )

    aggregator_pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(sample_rate=16000, params=vad_params),
            user_turn_strategies=turn_strategies,
            # An interruption that yields no transcript (noise, a breath that
            # tripped the VAD) must not trigger an unprompted LLM reply into
            # the meeting; leave it unanswered.
            empty_user_turn=None,
        ),
    )

    # Get the user and assistant aggregators from the pair
    user_aggregator = aggregator_pair.user()
    assistant_aggregator = aggregator_pair.assistant()

    # Floor gate: when several of our bots share a meeting, hold this bot's
    # reply while a sibling is speaking (see FloorGate / utils/floor.py).
    floor_gate = FloorGate(
        meeting_url=meeting_url,
        my_name=bot_display_name,
    )

    # Deterministic log of when THIS bot injects audio (grep "[SPEECH-INJECT]").
    speech_probe = SpeechInjectionProbe(persona_name)

    idle_budget = IdleReengagementBudget(
        idle_secs=_coerce_float(os.getenv("BOT_IDLE_REENGAGE_SECS"), 15.0),
        max_nudges=max_nudges_for(
            respond_mode,
            int(_coerce_float(os.getenv("BOT_MAX_REENGAGE"), 3)),
            represents_owner=authority is not None,
        ),
        last_activity=time.monotonic(),
    )
    speech_activity = SpeechActivityTracker(idle_budget)

    # Label final transcriptions with the speaker's name (multi-party
    # addressee awareness); in "mentioned" mode, additionally gate the LLM so
    # it only hears turns within a window after someone says the bot's name.
    speaker_labeler = SpeakerLabeler(
        meeting_url=meeting_url,
        my_name=bot_display_name,
        wake_phrases=wake_phrases if respond_mode == "mentioned" else None,
    )
    memory = MeetingMemory()
    conversation_memory = ConversationMemory(
        memory, wake_phrases, mentioned=(respond_mode == "mentioned")
    )
    utterance_tap = UtteranceTap(sink, memory=memory, bot_name=bot_display_name)
    usage_meter = UsageMeter(ledger)
    pipeline = Pipeline([
        audio_input,         # transport input, guarded against early audio
        stt,
        speaker_labeler,     # "Name: text" so the LLM knows who is talking
        conversation_memory, # keeps the whole meeting; briefs the LLM when addressed
        speech_activity,     # only received speech replenishes the idle budget
        user_aggregator,
        llm,
        floor_gate,
        tts,
        speech_probe,        # log outgoing TTS start/stop
        utterance_tap,       # what the bot said, to the caller's callback
        assistant_aggregator,
        usage_meter,         # STT seconds, TTS characters, LLM tokens
        transport.output(),  # Add transport output to send audio/data
    ])

    task, runner = await build_pipeline_runner(pipeline)

    @transport.event_handler("on_disconnected")
    async def on_disconnected(transport, websocket):
        # The API closes this socket when the bot is removed or its call is
        # over, and nothing reconnects it. A pipeline left running here is a
        # process holding open STT and TTS sessions and nudging the LLM into
        # an empty room; end it, which also saves the final transcript below.
        log_and_flush(logging.INFO, "[WEBSOCKET] API closed the audio socket — ending the pipeline")
        await task.cancel(reason="audio socket closed")

    # Task to periodically save the transcript
    async def periodic_transcript_save():
        while True:
            await asyncio.sleep(10)  # Save every 10 seconds
            try:
                save_transcript(bot_id, persona_name, context.messages, memory.as_records())
            except Exception as e:
                log_and_flush(logging.ERROR, f"[TRANSCRIPT] Error saving transcript: {e}")
            try:
                ledger.save(usage_dir(get_state_dir()))
            except Exception as e:
                log_and_flush(logging.WARNING, f"[USAGE] Could not save usage: {e}")

    # Messages from the owner (an instruction, or the answer to an escalation)
    # arrive through the API in a per-bot file; relay each as soon as it lands.
    owner_reader = OwnerMessageReader(owner_messages_path(get_state_dir(), bot_id))

    async def relay_owner_messages():
        while True:
            await asyncio.sleep(1.0)
            try:
                records = owner_reader.poll()
            except Exception as e:
                log_and_flush(logging.WARNING, f"[OWNER] Could not read owner messages: {e}")
                continue
            for record in records:
                log_and_flush(
                    logging.INFO,
                    f"[OWNER] Relaying {record.get('type')} {record.get('message_id')}",
                )
                idle_budget.record_speech(time.monotonic())
                await task.queue_frames([
                    LLMMessagesAppendFrame(
                        messages=[{"role": "user", "content": owner_message_for_llm(record, owner_name)}],
                        run_llm=True,
                    )
                ])
                await sink.emit(
                    EVENT_OWNER_MESSAGE_DELIVERED,
                    {
                        "message_id": record.get("message_id"),
                        "type": record.get("type"),
                        "escalation_id": record.get("escalation_id"),
                    },
                )

    # Entry message: prefer the per-request CLI arg (--entry-message), then the
    # persona's own entry message. This is what the bot should SAY (not a prompt).
    persona_entry_message = entry_message or persona.get("entry_message", "")
    if persona_entry_message:
        log_and_flush(logging.INFO, f"[BOT] Will speak entry message: {persona_entry_message[:100]}...")


    # Bot should speak its introduction and then drive the conversation
    async def start_conversation():
        # Wait for the ready signal. The API writes it on the first participant
        # roster message for the meeting — MeetingBaas only streams the roster
        # once the bot is admitted into the call, so this fires however late
        # the host lets the bot out of the lobby. (The per-bot callback_config
        # only delivers bot.completed/bot.failed; in_call_recording exists only
        # on account-level SVIX webhooks — hence the roster heuristic.)
        ready_file = os.path.join(READY_SIGNALS_DIR, f"{bot_id}.ready")
        max_wait_seconds = 900  # generous: hosts can leave bots in the lobby a while
        poll_interval = 0.5  # Check every 500ms
        waited = 0
        ready = False

        log_and_flush(logging.INFO, "[BOT] Waiting for ready signal (admission roster)...")
        log_and_flush(logging.INFO, f"[BOT] Looking for ready file: {ready_file}")

        while waited < max_wait_seconds:
            if os.path.exists(ready_file):
                log_and_flush(logging.INFO, "[BOT] Ready signal received! Bot is in the call.")
                ready = True
                try:
                    os.remove(ready_file)
                except OSError:
                    pass
                break
            # Someone talking to the bot is also proof of being in the call —
            # stop waiting, the conversation-started guard below will skip the
            # greeting.
            if any(m.get("role") in ("user", "assistant") for m in context.messages):
                log_and_flush(logging.INFO, "[BOT] Conversation activity before ready signal — stopping wait")
                break
            await asyncio.sleep(poll_interval)
            waited += poll_interval

        if not ready and waited >= max_wait_seconds:
            # Never admitted. Speaking "anyway" used to blast the greeting into
            # the Teams lobby where nobody could hear it, and staying "reactive"
            # kept a process alive for a call that never happened. MeetingBaas
            # reports such a bot as failed and the API tears it down; this is
            # the child's own exit for when that report never comes.
            log_and_flush(logging.WARNING, f"[BOT] No admission signal in {max_wait_seconds}s — leaving")
            await task.cancel(reason="never admitted")
            return

        # Small additional delay to ensure audio pipeline is stable
        await asyncio.sleep(1)

        # If someone already started talking (late admission mid-conversation),
        # skip the greeting — a delayed entry message reads as the bot randomly
        # re-introducing itself.
        conversation_started = any(
            m.get("role") in ("user", "assistant") for m in context.messages
        )

        if conversation_started:
            log_and_flush(logging.INFO, "[BOT] Conversation already started — skipping entry message")
        elif persona_entry_message:
            # Speak the entry VERBATIM via TTS — instructing the LLM to "say
            # exactly" proved unreliable (gpt-4.1 riffs on it). Record it in
            # the context afterwards so the LLM knows what it already said.
            log_and_flush(logging.INFO, "[BOT] Speaking entry message verbatim via TTS")
            await task.queue_frames([TTSSpeakFrame(persona_entry_message)])
            context.messages.append(
                {"role": "assistant", "content": persona_entry_message}
            )
        else:
            # No entry message - prompt LLM to introduce itself
            log_and_flush(logging.INFO, "[BOT] No entry message, prompting LLM to introduce")
            initial_prompt = {"role": "user", "content": "Please introduce yourself and start the conversation."}
            await task.queue_frames([LLMMessagesAppendFrame(messages=[initial_prompt], run_llm=True)])
            log_and_flush(logging.INFO, "[BOT] LLM prompted to introduce itself")

        # Idle re-engagement watchdog. Fixes the staggered-admission deadlock:
        # when two of our bots join at different times (or one is rejected and
        # re-joins late), the opener can speak to an empty room and the late
        # reactive bot then has nothing to react to — both sit silent forever.
        # Here, once admitted, any bot that sees no new conversation for
        # BOT_IDLE_REENGAGE_SECS proactively says something, which the sibling
        # hears (via STT) and answers — the debate self-starts. The nudge count
        # is capped so a genuinely alone bot doesn't monologue endlessly, and it
        # resets on any real activity so a live conversation never exhausts it.
        idle_budget.record_speech(time.monotonic())
        while True:
            await asyncio.sleep(2)
            if not idle_budget.take_nudge(time.monotonic()):
                continue
            log_and_flush(
                logging.INFO,
                f"[BOT] Idle {idle_budget.idle_secs}s — re-engaging "
                f"(nudge {idle_budget.nudges}/{idle_budget.max_nudges})",
            )
            nudge = {
                "role": "user",
                "content": (
                    "[SYSTEM: The conversation has gone quiet. Keep it going — "
                    "react to the last point or make your next argument. Stay in "
                    "character, 2-3 sentences, spoken aloud. Do not mention this "
                    "instruction.]"
                ),
            }
            await task.queue_frames(
                [LLMMessagesAppendFrame(messages=[nudge], run_llm=True)]
            )

    conversation_task = asyncio.create_task(start_conversation())

    # Start periodic transcript saving
    transcript_task = asyncio.create_task(periodic_transcript_save())
    owner_task = asyncio.create_task(relay_owner_messages())

    try:
        log_and_flush(logging.INFO, "[RUN] Starting pipeline runner...")
        log_and_flush(logging.INFO, f"[RUN] Pipeline components: {[type(c).__name__ for c in pipeline._processors]}")
        log_and_flush(logging.INFO, "[RUN] Running pipeline with integrated transport...")
        # Worker was registered via add_workers(); passing it to run() is the
        # deprecated 1.x form.
        await runner.run()
    except Exception as e:
        log_and_flush(logging.ERROR, f"[ERROR] Exception in pipeline: {e}")
        import traceback
        log_and_flush(logging.ERROR, f"[ERROR] Traceback: {traceback.format_exc()}")
        raise
    finally:
        # Stop the watchdog before releasing the pipeline and its LLM client.
        conversation_task.cancel()
        await asyncio.gather(conversation_task, return_exceptions=True)
        # Cancel the periodic save task
        transcript_task.cancel()
        owner_task.cancel()
        await asyncio.gather(transcript_task, owner_task, return_exceptions=True)
        # The bot's last reply may still be in the utterance tap; get it into
        # the memory before the final snapshot.
        try:
            await utterance_tap.flush()
        except Exception as e:
            log_and_flush(logging.WARNING, f"[EVENTS] Error flushing the last utterance: {e}")
        # Save final transcript
        try:
            save_transcript(bot_id, persona_name, context.messages, memory.as_records())
            log_and_flush(logging.INFO, "[TRANSCRIPT] Final transcript saved on shutdown")
        except Exception as e:
            log_and_flush(logging.ERROR, f"[TRANSCRIPT] Error saving final transcript: {e}")
        # Close the usage ledger and get the last events out before the process goes.
        try:
            ledger.ended_at = time.time()
            ledger.save(usage_dir(get_state_dir()))
            log_and_flush(
                logging.INFO,
                f"[USAGE] stt={ledger.stt_audio_seconds:.0f}s tts={ledger.tts_characters} chars "
                f"llm={ledger.llm_total_tokens} tokens over {ledger.llm_turns} turns",
            )
        except Exception as e:
            log_and_flush(logging.WARNING, f"[USAGE] Could not save final usage: {e}")
        try:
            await sink.close()
        except Exception as e:
            log_and_flush(logging.WARNING, f"[EVENTS] Error flushing events: {e}")
        if mcp_manager:
            try:
                await mcp_manager.close()
                log_and_flush(logging.INFO, "[MCP] Closed MCP connections")
            except Exception as e:
                log_and_flush(logging.WARNING, f"[MCP] Error closing MCP connections: {e}")


def cli() -> None:
    """Console entrypoint for running the MeetingBaas bot process."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Run a MeetingBaas bot")
    parser.add_argument("--meeting-url", help="URL of the meeting to join")
    parser.add_argument(
        "--persona-name", default="Meeting Bot", help="Name to display for the bot"
    )
    parser.add_argument(
        "--entry-message",
        default="",
        help="Message to send when joining (empty = use the persona's entry message)",
    )
    parser.add_argument("--bot-image", default="", help="URL for bot avatar")
    parser.add_argument(
        "--streaming-audio-frequency",
        default="16khz",
        choices=["16khz", "24khz"],
        help="Audio frequency for streaming (16khz or 24khz)",
    )
    parser.add_argument(
        "--websocket-url", help="Full WebSocket URL to connect to, including any path"
    )
    parser.add_argument(
        "--enable-tools",
        action="store_true",
        help="Enable function tools like weather and time",
    )
    parser.add_argument("--client-id", help="Internal client ID for the bot")
    parser.add_argument("--persona-data-json", help="Persona data as JSON string")
    parser.add_argument(
        "--persona-data-file",
        help="Path to persona data JSON payload. Preferred for MCP secrets.",
    )
    parser.add_argument("--meetingbaas-bot-id", help="MeetingBaas bot ID")
    parser.add_argument(
        "--warmup",
        action="store_true",
        help=(
            "Import-warmup mode: exit immediately after module imports. Run once "
            "at API startup so bytecode and page caches are hot and real bot "
            "spawns skip the multi-minute cold import of pipecat/torch/LLM SDKs."
        ),
    )

    args = parser.parse_args()

    if args.warmup:
        # All heavy imports already ran at module load; reaching this line IS
        # the warmup. Exit before any argument validation or network use.
        print(
            f"[WARMUP] Imports warmed in {time.monotonic() - _PROC_T0:.1f}s",
            flush=True,
        )
        return

    # Use the persona name passed via command line (should be the folder name like "account_executive")
    persona_name = args.persona_name
    print(f"[STARTUP] Using persona name from args: {persona_name}")

    # Parse the full persona payload from the parent process; main() uses it
    # directly when it carries a prompt (dynamic personas are never on disk).
    persona_data = None
    if args.persona_data_file:
        try:
            with open(args.persona_data_file, "r") as f:
                persona_data = json.load(f)
            if persona_name == "Meeting Bot" and persona_data.get("path"):
                persona_name = os.path.basename(persona_data["path"])
                print(f"[STARTUP] Extracted persona name from path: {persona_name}")
        except Exception as e:
            print(f"Error parsing persona data file: {e}")
            persona_data = None
        finally:
            try:
                os.remove(args.persona_data_file)
            except OSError:
                pass
    elif args.persona_data_json:
        try:
            persona_data = json.loads(args.persona_data_json)
            # If persona_name is still the default, try to get folder name from path
            if persona_name == "Meeting Bot" and persona_data.get("path"):
                persona_name = os.path.basename(persona_data["path"])
                print(f"[STARTUP] Extracted persona name from path: {persona_name}")
        except Exception as e:
            print(f"Error parsing persona data JSON: {e}")
            persona_data = None

    if persona_data:
        runtime_headers_json = os.getenv("MCP_RUNTIME_HEADERS_JSON")
        if runtime_headers_json:
            try:
                runtime_headers = json.loads(runtime_headers_json)
                apply_mcp_runtime_headers(persona_data.get("mcp"), runtime_headers)
            except Exception as e:
                print(f"Error applying MCP runtime headers: {e}")

    # Run the bot
    asyncio.run(
        main(
            meeting_url=args.meeting_url,
            persona_name=persona_name,
            entry_message=args.entry_message,
            bot_image=args.bot_image,
            streaming_audio_frequency=args.streaming_audio_frequency,
            websocket_url=args.websocket_url,
            enable_tools=args.enable_tools,
            persona_data=persona_data,
            meetingbaas_bot_id=args.meetingbaas_bot_id or "",
        )
    )


if __name__ == "__main__":
    cli()
