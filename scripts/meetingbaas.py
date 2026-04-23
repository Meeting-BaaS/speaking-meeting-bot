import argparse
import asyncio
import os
import os
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
from config.prompts import DEFAULT_SYSTEM_PROMPT
from meetingbaas_pipecat.utils.logger import configure_logger
import sys
import logging


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


async def main(
    meeting_url: str = "",
    persona_name: str = "Meeting Bot",
    entry_message: str = "Hello, I am the meeting bot",
    bot_image: str = "",
    streaming_audio_frequency: str = "24khz",
    websocket_url: str = "",
    enable_tools: bool = True,
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

    persona_manager = PersonaManager()
    persona = persona_manager.get_persona(persona_name)
    if not persona:
        log_and_flush(logging.ERROR, f"[ERROR] Persona '{persona_name}' not found")
        return
    log_and_flush(logging.INFO, f"[PERSONA] Loaded persona: {persona_name}")

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

        # Create tools schema
        tools = ToolsSchema(standard_tools=[weather_function, time_function])
    else:
        log_and_flush(logging.INFO, "[TOOLS] Function tools are disabled")
        tools = None

    language = persona.get("language_code", "en-US")
    log_and_flush(logging.INFO, f"[PERSONA] Using language: {language}")

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
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
    aggregator_pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    confidence=0.5,
                    start_secs=0.25,
                    stop_secs=0.1,
                    min_volume=0.6,
                ),
            ),
        ),
    )

    # Get the user and assistant aggregators from the pair
    user_aggregator = aggregator_pair.user()
    assistant_aggregator = aggregator_pair.assistant()

    # Log pipeline step data
    def log_pipeline_step(step_name, data):
        log_and_flush(logging.INFO, f"[PIPELINE] Step: {step_name}, Type: {type(data)}, Data: {str(data)[:120]}")

    # Remove the LoggingStep wrapper - it doesn't properly proxy all methods
    # Instead, we'll log in the pipeline components themselves if needed
    
    pipeline = Pipeline([
        transport.input(),   # Add transport input to receive audio/data
        stt,
        user_aggregator,
        llm,
        tts,
        assistant_aggregator,
        transport.output(),  # Add transport output to send audio/data
    ])

    task = PipelineTask(pipeline, params=PipelineParams(), check_dangling_tasks=True)
    runner = PipelineRunner()

    # Add a simple test to verify TTS is working
    async def test_tts_output():
        log_and_flush(logging.INFO, "[TEST] Testing TTS output directly")
        try:
            # Try to generate some test audio
            test_text = "Testing TTS output"
            log_and_flush(logging.INFO, f"[TEST] Generating TTS for: {test_text}")
            # We'll let the pipeline handle this rather than calling TTS directly
            await task.queue_frames([TextFrame(test_text)])
            log_and_flush(logging.INFO, "[TEST] Test TTS frame queued")
        except Exception as e:
            log_and_flush(logging.ERROR, f"[TEST] TTS test failed: {e}")

    if entry_message:
        log_and_flush(logging.INFO, "[BOT] Bot will speak first with an introduction")
        initial_message = {"role": "user", "content": entry_message}
        async def queue_initial_message():
            log_and_flush(logging.INFO, "[BOT] Waiting 2 seconds before sending initial message")
            await asyncio.sleep(2)
            log_and_flush(logging.INFO, f"[BOT] Queuing initial message: {initial_message}")
            # Append to the existing context so we don't overwrite any messages
            # (system prompt, or user speech) that arrived during the 2-second delay.
            await task.queue_frames([LLMMessagesAppendFrame(messages=[initial_message], run_llm=True)])
            log_and_flush(logging.INFO, "[BOT] Initial greeting message queued successfully")
            
            # Also queue a simple TTS test
            await asyncio.sleep(1)
            await test_tts_output()
        asyncio.create_task(queue_initial_message())
    else:
        log_and_flush(logging.INFO, "[BOT] No entry message configured")

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


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Run a MeetingBaas bot")
    parser.add_argument("--meeting-url", help="URL of the meeting to join")
    parser.add_argument(
        "--persona-name", default="Meeting Bot", help="Name to display for the bot"
    )
    parser.add_argument(
        "--entry-message",
        default="Hello, I am the meeting bot",
        help="Message to send when joining",
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

    # Determine persona name from persona data JSON if provided
    persona_name = args.persona_name
    if args.persona_data_json:
        try:
            import json
            persona_data = json.loads(args.persona_data_json)
            # Use the folder name key if available, otherwise fall back to looking up by display name
            # The persona_data should contain a key that matches the folder name
            # For now, we'll extract it from the persona data or use a mapping
            # The persona folder names are like "interviewer", not "Technical Interviewer Bot"
            # We need to find the folder name that corresponds to this persona
            from config.persona_utils import PersonaManager
            pm = PersonaManager()
            # Try to find persona by matching the display name
            for folder_name, data in pm.personas.items():
                if data.get("name") == persona_data.get("name"):
                    persona_name = folder_name
                    break
        except Exception as e:
            print(f"Error parsing persona data JSON: {e}")

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
        )
    )
