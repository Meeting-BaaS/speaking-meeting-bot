import asyncio
import os
import sys
from datetime import datetime

import aiohttp
import pytz
import requests
from dotenv import load_dotenv
from loguru import logger
from openai.types.chat import ChatCompletionToolParam
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMMessagesFrame, TextFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.cartesia import CartesiaTTSService

# from pipecat.services.gladia import GladiaSTTService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.network.websocket_server import (
    ProtobufFrameSerializer,
    WebsocketServerParams,
    WebsocketServerTransport,
)

from config.prompts import DEFAULT_SYSTEM_PROMPT, WAKE_WORD_INSTRUCTION
from meetingbaas_pipecat.utils.logger import configure_logger

from .runner import configure

load_dotenv(override=True)

logger = configure_logger()

# Add MeetingBaas API constants
API_KEY = None  # Will be set from args
API_URL = os.getenv("MEETING_BAAS_API_URL", "https://api.meetingbaas.com")

# Check for required API keys
# if not API_KEY:
#     logger.error("MEETING_BAAS_API_KEY not found in environment variables")
#     sys.exit(1)


# Add this function to create the bot via MeetingBaas API
def create_baas_bot(
    meeting_url,
    websocket_url,
    bot_id,
    persona_name,
    recorder_only=False,
    output_bot_id=False,
):
    """Create a bot using MeetingBaas API"""
    # Create bot configuration
    logger.info(
        f"Preparing MeetingBaas bot configuration for {persona_name} (ID: {bot_id})"
    )
    logger.info(f"Meeting URL: {meeting_url}")
    logger.info(f"WebSocket URL: {websocket_url}")
    logger.info(f"Recorder only: {recorder_only}")

    config = {
        "meeting_url": meeting_url,
        "bot_name": persona_name,
        "recording_mode": "speaker_view",
        "reserved": False,
        "automatic_leave": {"waiting_room_timeout": 600},
        "deduplication_key": f"{persona_name}-BaaS-{bot_id}",
        "streaming": {"input": websocket_url, "output": websocket_url},
    }

    if recorder_only:
        logger.info("Setting up recorder-only mode with Default STT provider")
        config["speech_to_text"] = {"provider": "Default"}

    # Make API call to create bot
    url = f"{API_URL}/bots"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": API_KEY,
    }

    # Log API key (partially redacted for security)
    api_key_prefix = API_KEY[:4] if API_KEY and len(API_KEY) > 8 else "****"
    api_key_suffix = API_KEY[-4:] if API_KEY and len(API_KEY) > 8 else "****"
    logger.info(f"Using MeetingBaas API key: {api_key_prefix}...{api_key_suffix}")

    logger.info(f"Sending request to MeetingBaas API: {url}")
    logger.debug(f"Request headers: {headers}")
    logger.debug(f"Request payload: {config}")

    try:
        logger.info("Making API call to create MeetingBaas bot...")
        response = requests.post(url, json=config, headers=headers)

        logger.info(f"API response status code: {response.status_code}")
        logger.debug(f"API response headers: {dict(response.headers)}")

        if response.status_code == 200:
            response_data = response.json()
            bot_id = response_data.get("bot_id")
            logger.success(f"Bot created successfully with ID: {bot_id}")
            logger.debug(f"Full API response: {response_data}")

            # Print the bot ID in a special format that can be captured by the parent process
            if output_bot_id:
                print(f"BOT_ID: {bot_id}")
                sys.stdout.flush()  # Ensure the output is sent immediately

            return bot_id
        else:
            try:
                error_data = response.json()
                logger.error(
                    f"Failed to create bot: {response.status_code} - {error_data}"
                )
                logger.error(
                    f"Error details: {error_data.get('error', {}).get('message', 'No error message')}"
                )
            except ValueError:
                logger.error(
                    f"Failed to create bot: {response.status_code} - {response.text}"
                )
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error during MeetingBaas API call: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating MeetingBaas bot: {str(e)}")
        return None


async def get_weather(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
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
                await result_callback(
                    f"The weather in {location} is currently {weather_data} ({format.capitalize()})."
                )
            else:
                await result_callback(
                    f"Failed to fetch the weather data for {location}."
                )


async def get_time(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    location = arguments["location"]

    # Set timezone based on the provided location
    try:
        timezone = pytz.timezone(location)
        current_time = datetime.now(timezone)
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
        await result_callback(f"The current time in {location} is {formatted_time}.")
    except pytz.UnknownTimeZoneError:
        await result_callback(
            f"Invalid location specified. Could not determine time for {location}."
        )


async def log_transcript(frame):
    if isinstance(frame, TranscriptionFrame):
        logger.info(f"Transcript received: {frame.text}")
    return frame


async def log_speech(frame):
    if isinstance(frame, TextFrame):
        logger.info(f"Speaking out: {frame.text}")
    return frame


async def main():
    # Get arguments from runner.configure
    (
        host,
        port,
        system_prompt,
        voice_id,
        persona_name,
        args,
        additional_content,
    ) = await configure()

    # Extract meeting URL and bot ID from args
    meeting_url = getattr(args, "meeting_url", None)
    bot_id = getattr(args, "bot_id", "default")
    recorder_only = getattr(args, "recorder_only", False)
    websocket_url = getattr(args, "websocket_url", None)
    output_bot_id = getattr(args, "output_bot_id", False)

    # Set API key from args
    global API_KEY
    API_KEY = getattr(args, "meeting_baas_api_key", os.getenv("MEETING_BAAS_API_KEY"))

    if not API_KEY:
        logger.error("MeetingBaas API key not provided and not found in environment")
        return

    # Use fully qualified websocket URL with port
    if websocket_url:
        if not websocket_url.startswith("wss://") and not websocket_url.startswith(
            "ws://"
        ):
            websocket_url = f"ws://{websocket_url}"

        # Extract base URL from websocket_url for MeetingBaas
        if websocket_url.startswith("wss://"):
            meeting_baas_url = websocket_url
        else:
            # If websocket URL is local, we need to use a specific port
            meeting_baas_url = f"{websocket_url}:{port}"

        # Add the path /ws/{bot_id} to the WebSocket URL for streaming
        websocket_with_path = f"{meeting_baas_url}/ws/{bot_id}"

        # Create bot via MeetingBaas API
        if meeting_url:
            bot_baas_id = create_baas_bot(
                meeting_url,
                websocket_with_path,
                bot_id,
                persona_name,
                recorder_only,
                output_bot_id,
            )

            if not bot_baas_id:
                logger.error(
                    "Failed to create MeetingBaas bot. Check API key and parameters."
                )
                return

    logger.warning(f"**CARTESIA VOICE ID: {voice_id}**")
    logger.warning(f"**BOT NAME: {persona_name}**")
    logger.warning(f"**SYSTEM PROMPT**")
    logger.warning(f"System prompt: {system_prompt}")

    transport = WebsocketServerTransport(
        host=host,
        port=port,
        params=WebsocketServerParams(
            audio_out_sample_rate=24000,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            # binary_mode=True,
            # should be ProtobufFrameSerializer?
            # serializer=ProtobufSerializer(),
        ),
    )

    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o-mini")
    llm.register_function("get_weather", get_weather)
    llm.register_function("get_time", get_time)

    tools = [
        ChatCompletionToolParam(
            type="function",
            function={
                "name": "get_weather",
                "description": "Get the current weather",
                "parameters": {
                    "type": "object",
                    "properties": {
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
                    "required": ["location", "format"],
                },
            },
        ),
        ChatCompletionToolParam(
            type="function",
            function={
                "name": "get_time",
                "description": "Get the current time for a specific location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The location for which to retrieve the current time (e.g., 'Asia/Kolkata', 'America/New_York')",
                        },
                    },
                    "required": ["location"],
                },
            },
        ),
    ]

    # use Gladia as our default STT service ;)
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"), encoding="linear24", sample_rate=24000
    )
    # stt = GladiaSTTService(
    #     api_key=os.getenv("GLADIA_API_KEY"), encoding="linear24", sample_rate=24000
    # )

    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=voice_id,
        sample_rate=24000,
    )

    logger.warning(f"**BOT NAME: {persona_name}**")
    logger.warning(f"**SYSTEM PROMPT**")
    logger.warning(f"System prompt: {system_prompt}")
    logger.warning(f"**SYSTEM PROMPT END**")
    logger.warning(f"**ADDITIONAL CONTENT FOUND for {persona_name}**")
    # logger.warning(f"Additional context: {additional_content}")
    logger.warning(f"**FOR BOT NAME: {persona_name}**")

    messages = [
        {
            "role": "system",
            "content": (
                system_prompt
                + "\n\n"
                + f"You are {persona_name}"
                + "\n\n"
                + DEFAULT_SYSTEM_PROMPT
                + "\n\n"
                + "\n\n"
                + "You have the following additional context. USE IT TO INFORM YOUR RESPONSES:"
                + "\n\n"
                + "\n\n"
                + additional_content
            ),
        },
    ]

    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        if args.speak_first and args.speak_first > 0:
            # Send an initial greeting when the bot joins
            initial_message = {
                "role": "user",
                "content": "Please introduce yourself and start the conversation.",
            }
            await task.queue_frames([LLMMessagesFrame([initial_message])])

    runner = PipelineRunner()
    await runner.run(task)


def start():
    asyncio.run(main())


if __name__ == "__main__":
    start()
