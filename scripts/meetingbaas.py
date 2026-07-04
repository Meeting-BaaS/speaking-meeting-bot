import asyncio
import os
import argparse
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
from pipecat.frames.frames import LLMMessagesAppendFrame, TextFrame
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

# from pipecat.services.gladia.stt import GladiaSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.client import (
    WebsocketClientParams,
    WebsocketClientTransport,
)

from config.persona_utils import PersonaManager
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


from pipecat.services.llm_service import FunctionCallParams

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

    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=voice_id,
        sample_rate=output_sample_rate,
    )
    log_and_flush(logging.INFO, f"[TTS] Cartesia TTS initialized with sample_rate={output_sample_rate}, voice_id={voice_id}")

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4.1",
        run_in_parallel=False,
    )
    log_and_flush(logging.INFO, "[LLM] OpenAI LLM initialized with model=gpt-4.1")

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

        # Create tools schema
        tools = ToolsSchema(standard_tools=[weather_function, time_function, save_call_summary_function])
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
    # Turn-taking knobs, env-tunable per deployment (no code edit needed):
    #   VAD_START_SECS — sustained speech required before a turn/interruption
    #     registers. Raise for bot-vs-bot meetings so TTS tails and breaths
    #     don't trigger mutual barge-in.
    #   VAD_STOP_SECS — silence required before the bot considers the speaker
    #     done and replies. 0.1 (the old hardcode) replied to half-sentences;
    #     pipecat's default is 0.8.
    aggregator_pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    confidence=float(os.getenv("VAD_CONFIDENCE", "0.5")),
                    start_secs=float(os.getenv("VAD_START_SECS", "0.25")),
                    stop_secs=float(os.getenv("VAD_STOP_SECS", "0.8")),
                    min_volume=float(os.getenv("VAD_MIN_VOLUME", "0.6")),
                ),
            ),
        ),
    )

    # Get the user and assistant aggregators from the pair
    user_aggregator = aggregator_pair.user()
    assistant_aggregator = aggregator_pair.assistant()

    pipeline = Pipeline([
        transport.input(),   # Add transport input to receive audio/data
        stt,
        user_aggregator,
        llm,
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
        # Wait for ready signal from webhook (in_call_recording event)
        ready_file = os.path.join(READY_SIGNALS_DIR, f"{bot_id}.ready")
        max_wait_seconds = 60  # Maximum time to wait for ready signal
        poll_interval = 0.5  # Check every 500ms
        waited = 0

        log_and_flush(logging.INFO, f"[BOT] Waiting for ready signal from webhook (in_call_recording)...")
        log_and_flush(logging.INFO, f"[BOT] Looking for ready file: {ready_file}")

        while waited < max_wait_seconds:
            if os.path.exists(ready_file):
                log_and_flush(logging.INFO, f"[BOT] Ready signal received! Bot is in call and recording.")
                # Clean up the ready file
                try:
                    os.remove(ready_file)
                except:
                    pass
                break
            await asyncio.sleep(poll_interval)
            waited += poll_interval

        if waited >= max_wait_seconds:
            log_and_flush(logging.WARNING, f"[BOT] Timeout waiting for ready signal, proceeding anyway...")

        # Small additional delay to ensure audio pipeline is stable
        await asyncio.sleep(1)

        # If someone already started talking (the ready signal arrived late or
        # timed out mid-conversation), skip the greeting entirely — a delayed
        # entry message reads as the bot randomly re-introducing itself.
        conversation_started = any(
            m.get("role") in ("user", "assistant") for m in context.messages
        )

        if conversation_started:
            log_and_flush(logging.INFO, "[BOT] Conversation already started — skipping entry message")
        elif persona_entry_message:
            log_and_flush(logging.INFO, f"[BOT] Prompting LLM to speak entry message")
            # Add a system instruction to say exactly the entry message
            speak_prompt = {
                "role": "user",
                "content": f"[SYSTEM: Say exactly the following to start the conversation, word for word, do not add anything else]: {persona_entry_message}"
            }
            await task.queue_frames([LLMMessagesAppendFrame(messages=[speak_prompt], run_llm=True)])
            log_and_flush(logging.INFO, "[BOT] LLM prompted to speak entry message")
        else:
            # No entry message - prompt LLM to introduce itself
            log_and_flush(logging.INFO, f"[BOT] No entry message, prompting LLM to introduce")
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
    parser.add_argument("--api-key", help="API key for authentication")
    parser.add_argument("--meetingbaas-bot-id", help="MeetingBaas bot ID")

    args = parser.parse_args()

    # Use the persona name passed via command line (should be the folder name like "account_executive")
    persona_name = args.persona_name
    print(f"[STARTUP] Using persona name from args: {persona_name}")

    # Parse the full persona payload from the parent process; main() uses it
    # directly when it carries a prompt (dynamic personas are never on disk).
    persona_data = None
    if args.persona_data_json:
        try:
            import json
            persona_data = json.loads(args.persona_data_json)
            # If persona_name is still the default, try to get folder name from path
            if persona_name == "Meeting Bot" and persona_data.get("path"):
                import os
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
