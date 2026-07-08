import asyncio
import os
import argparse
import inspect
import json as jsonlib
from dataclasses import dataclass
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams

# from pipecat.services.gladia.stt import GladiaSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.client import (
    WebsocketClientParams,
    WebsocketClientTransport,
)

from pipecat.frames.frames import SystemFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from config.persona_utils import PersonaManager
from utils.floor import floor_blocked_by_sibling
from utils.mcp_client import (
    HttpMcpClient,
    McpClientError,
    StdioMcpClient,
    build_mcp_tool_name,
)
from utils.runtime import get_state_dir
from config.prompts import DEFAULT_SYSTEM_PROMPT
from meetingbaas_pipecat.utils.logger import configure_logger
import sys
import logging
import json

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


def build_cartesia_tts_kwargs(
    *,
    api_key: str | None,
    voice_id: str | None,
    sample_rate: int,
    speed: float,
) -> dict:
    """Build Cartesia kwargs across Pipecat versions without breaking startup."""
    kwargs = {
        "api_key": api_key,
        "voice_id": voice_id,
        "sample_rate": sample_rate,
    }
    try:
        signature = inspect.signature(CartesiaTTSService.__init__)
    except (TypeError, ValueError):
        signature = None

    params = signature.parameters if signature else {}
    if "speed" in params:
        kwargs["speed"] = speed
        return kwargs

    if "params" not in params or not hasattr(CartesiaTTSService, "InputParams"):
        return kwargs

    try:
        kwargs["params"] = CartesiaTTSService.InputParams(
            generation_config={"speed": speed}
        )
    except Exception as exc:
        log_and_flush(
            logging.WARNING,
            f"[TTS] Cartesia speed config not supported by this Pipecat version: {exc}",
        )
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
                    parts.append(jsonlib.dumps(item["json"], ensure_ascii=False))
                elif item.get("type") == "data" and item.get("data") is not None:
                    parts.append(jsonlib.dumps(item["data"], ensure_ascii=False))
                elif item.get("type") == "resource" and item.get("resource"):
                    parts.append(jsonlib.dumps(item["resource"], ensure_ascii=False))
                else:
                    parts.append(jsonlib.dumps(item, ensure_ascii=False))
            if parts:
                return "\n".join(parts)
        if "structuredContent" in result:
            return jsonlib.dumps(result["structuredContent"], ensure_ascii=False)
    return jsonlib.dumps(result, ensure_ascii=False, default=str)


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


def save_transcript(bot_id: str, persona_name: str, messages: list):
    """Save the conversation transcript to a JSON file for webhook processing."""
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
        "messages": conversation
    }

    with open(transcript_file, "w") as f:
        json.dump(data, f, indent=2)

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

    def __init__(self, meeting_url: str, my_name: str, **kwargs):
        super().__init__(**kwargs)
        self._meeting_url = meeting_url
        self._my_name = my_name
        self._buffer = []
        self._flush_task = None

    def _blocked(self) -> bool:
        try:
            return floor_blocked_by_sibling(self._meeting_url, self._my_name)
        except Exception:
            return False  # never let floor bookkeeping kill the pipeline

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
    
    log_and_flush(logging.INFO, f"[STARTUP] MeetingBaas bot launching with persona: {persona_name}")
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
    tts_kwargs = build_cartesia_tts_kwargs(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=voice_id,
        sample_rate=output_sample_rate,
        speed=tts_speed,
    )
    tts = CartesiaTTSService(**tts_kwargs)
    speed_applied = "speed" in tts_kwargs or "params" in tts_kwargs
    log_and_flush(
        logging.INFO,
        f"[TTS] Cartesia TTS initialized with sample_rate={output_sample_rate}, voice_id={voice_id}, speed={tts_speed}, speed_applied={speed_applied}",
    )

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4.1",
        run_in_parallel=False,
    )
    log_and_flush(logging.INFO, "[LLM] OpenAI LLM initialized with model=gpt-4.1")

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

        # Create tools schema
        tools = ToolsSchema(standard_tools=tool_schemas)
    else:
        log_and_flush(logging.INFO, "[TOOLS] Function tools are disabled")
        tools = None

    language = persona.get("language_code", "en-US")
    log_and_flush(logging.INFO, f"[PERSONA] Using language: {language}")

    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
    log_and_flush(logging.INFO, f"[STT] Deepgram API key present: {bool(deepgram_api_key)}")
    log_and_flush(logging.INFO, f"[STT] Deepgram config: encoding=linear16, sample_rate={output_sample_rate}, language={language}")

    stt = DeepgramSTTService(
        api_key=deepgram_api_key,
        encoding="linear16" if streaming_audio_frequency == "16khz" else "linear24",
        sample_rate=output_sample_rate,
        settings=DeepgramSTTService.Settings(language=language),
    )

    # stt = GladiaSTTService(
    #     api_key=os.getenv("GLADIA_API_KEY"),
    #     encoding="linear16" if streaming_audio_frequency == "16khz" else "linear24",
    #     sample_rate=output_sample_rate,
    #     language=language,  # Use language from persona
    # )

    bot_name = persona_name or "Bot"
    log_and_flush(logging.INFO, f"[BOT] Using bot name: {bot_name}")

    # Create a more comprehensive system prompt
    system_content = persona["prompt"]

    # Add additional context if available
    if additional_content:
        system_content += f"\n\nYou are {persona_name}\n\n{DEFAULT_SYSTEM_PROMPT}\n\n"
        system_content += "You have the following additional context. USE IT TO INFORM YOUR RESPONSES:\n\n"
        system_content += additional_content
        system_content += "You are a meeting bot. You are in a meeting with a group of people. You are here to help the group. You are not the host of the meeting. You are not the organizer of the meeting. You are not the participant in the meeting. You are the meeting bot."
        system_content += "YOU ARE HELP TO HELP. KEEP IT SHORT. EVERYTHING YOU SAY WILL BE REPEATED BACK TO THE GROUP OUT LOUD so DO NOT add PUNCTUATION OR CAPS. JUST SAY WHAT YOU NEED TO SAY IN A CONCISE MANNER."

    mcp_context = build_mcp_context_prompt(persona.get("mcp"))
    if mcp_context:
        system_content += mcp_context
        log_and_flush(logging.INFO, "[MCP] Added MCP metadata to system context")

    # Set up messages
    messages = [
        {
            "role": "system",
            "content": system_content,
        },
    ]

    # Create the context object - with or without tools
    if enable_tools and tools:
        context = LLMContext(messages, tools)
    else:
        context = LLMContext(messages)

    # Create the context aggregator pair with VAD on the user aggregator
    # Turn-taking knobs. Precedence: per-request turn_config (rides in the
    # persona dict from the API) > VAD_* env vars > defaults.
    #   start_secs — sustained speech required before a turn/interruption
    #     registers. Raise for bot-vs-bot meetings so TTS tails and breaths
    #     don't trigger mutual barge-in.
    #   stop_secs — silence required before the bot considers the speaker
    #     done and replies. 0.1 (the old hardcode) replied to half-sentences;
    #     pipecat's default is 0.8.
    turn_config = (persona.get("turn_config") or {}) if persona else {}

    def _vad_param(key: str, env: str, default: str) -> float:
        value = turn_config.get(key)
        return float(value) if value is not None else float(os.getenv(env, default))

    vad_params = VADParams(
        confidence=_vad_param("confidence", "VAD_CONFIDENCE", "0.5"),
        start_secs=_vad_param("start_secs", "VAD_START_SECS", "0.25"),
        stop_secs=_vad_param("stop_secs", "VAD_STOP_SECS", "0.8"),
        min_volume=_vad_param("min_volume", "VAD_MIN_VOLUME", "0.6"),
    )
    log_and_flush(logging.INFO, f"[VAD] Params: {vad_params}")

    aggregator_pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=vad_params,
            ),
        ),
    )

    # Get the user and assistant aggregators from the pair
    user_aggregator = aggregator_pair.user()
    assistant_aggregator = aggregator_pair.assistant()

    # Floor gate: when several of our bots share a meeting, hold this bot's
    # reply while a sibling is speaking (see FloorGate / utils/floor.py).
    floor_gate = FloorGate(
        meeting_url=meeting_url,
        my_name=(persona.get("name") if persona else None) or persona_name,
    )

    pipeline = Pipeline([
        transport.input(),   # Add transport input to receive audio/data
        stt,
        user_aggregator,
        llm,
        floor_gate,
        tts,
        assistant_aggregator,
        transport.output(),  # Add transport output to send audio/data
    ])

    # Metrics log per-service TTFB (STT/LLM/TTS) — grep journal for "TTFB".
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        check_dangling_tasks=True,
    )
    runner = PipelineRunner()

    # Task to periodically save the transcript
    async def periodic_transcript_save():
        while True:
            await asyncio.sleep(10)  # Save every 10 seconds
            try:
                save_transcript(bot_id, persona_name, context.messages)
            except Exception as e:
                log_and_flush(logging.ERROR, f"[TRANSCRIPT] Error saving transcript: {e}")

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
            # Never admitted: stay silent. Speaking "anyway" used to blast the
            # greeting into the Teams lobby where nobody could hear it.
            log_and_flush(logging.WARNING, "[BOT] No admission signal — skipping entry message, staying reactive")
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

    asyncio.create_task(start_conversation())

    # Start periodic transcript saving
    transcript_task = asyncio.create_task(periodic_transcript_save())

    try:
        log_and_flush(logging.INFO, "[RUN] Starting pipeline runner...")
        log_and_flush(logging.INFO, f"[RUN] Pipeline components: {[type(c).__name__ for c in pipeline._processors]}")
        log_and_flush(logging.INFO, "[RUN] Running pipeline with integrated transport...")
        await runner.run(task)
    except Exception as e:
        log_and_flush(logging.ERROR, f"[ERROR] Exception in pipeline: {e}")
        import traceback
        log_and_flush(logging.ERROR, f"[ERROR] Traceback: {traceback.format_exc()}")
        raise
    finally:
        # Cancel the periodic save task
        transcript_task.cancel()
        # Save final transcript
        try:
            save_transcript(bot_id, persona_name, context.messages)
            log_and_flush(logging.INFO, "[TRANSCRIPT] Final transcript saved on shutdown")
        except Exception as e:
            log_and_flush(logging.ERROR, f"[TRANSCRIPT] Error saving final transcript: {e}")
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
    parser.add_argument("--api-key", help="API key for authentication")
    parser.add_argument("--meetingbaas-bot-id", help="MeetingBaas bot ID")

    args = parser.parse_args()

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
            try:
                os.remove(args.persona_data_file)
            except OSError:
                pass
            if persona_name == "Meeting Bot" and persona_data.get("path"):
                persona_name = os.path.basename(persona_data["path"])
                print(f"[STARTUP] Extracted persona name from path: {persona_name}")
        except Exception as e:
            print(f"Error parsing persona data file: {e}")
            persona_data = None
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
        )
    )


if __name__ == "__main__":
    cli()
