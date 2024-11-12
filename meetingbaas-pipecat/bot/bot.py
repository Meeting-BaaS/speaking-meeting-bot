import asyncio
import os
import sys
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv
from loguru import logger
from openai.types.chat import ChatCompletionToolParam
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMMessagesFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.network.websocket_server import (
    WebsocketServerParams,
    WebsocketServerTransport,
)

from .runner import configure

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stdout, level="INFO")
logger.add(sys.stdout, level="DEBUG")
logger.add(sys.stderr, level="WARNING")


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


async def log_transcript(text, source="HUMAN"):
    logger.info(f"TRANSCRIPT [{source}]: {text}")


async def main():
    (host, port, system_prompt, voice_id, args) = await configure()

    transport = WebsocketServerTransport(
        host=host,
        port=port,
        params=WebsocketServerParams(
            audio_out_sample_rate=16000,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
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

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"), encoding="linear16", sample_rate=16000
    )

    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=voice_id,
        sample_rate=16000,
    )
    logger.debug(f"Initialized CartesiaTTSService with voice_id: {voice_id}")

    async def tts_debug_callback(event_type, data):
        logger.debug(f"TTS Event: {event_type}")
        if event_type == "audio_chunk":
            logger.debug(f"Audio chunk size: {len(data)} bytes")
        elif event_type == "error":
            logger.error(f"TTS Error: {data}")

    tts.add_event_callback(tts_debug_callback)

    async def test_tts():
        try:
            logger.info("Testing TTS generation...")
            test_audio = await tts.synthesize("This is a test message.")
            logger.info(
                f"Successfully generated test TTS audio: {len(test_audio)} bytes"
            )
        except Exception as e:
            logger.error(f"Failed to generate test TTS: {e}")
            logger.exception(e)

    await test_tts()

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
    ]

    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # Verify critical environment variables
    critical_vars = {
        "CARTESIA_API_KEY": os.getenv("CARTESIA_API_KEY"),
        "CARTESIA_VOICE_ID": os.getenv("CARTESIA_VOICE_ID"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "DEEPGRAM_API_KEY": os.getenv("DEEPGRAM_API_KEY"),
    }

    for var_name, value in critical_vars.items():
        if not value:
            logger.error(f"Missing critical environment variable: {var_name}")
        else:
            logger.debug(f"Found {var_name}: {value[:4]}...{value[-4:]}")

    async def pipeline_debug(event_type, data):
        logger.debug(f"Pipeline Event: {event_type}")
        if event_type == "frame_processed":
            logger.debug(f"Frame processed: {type(data)}")
        elif event_type == "error":
            logger.error(f"Pipeline Error: {data}")

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ],
        debug_callback=pipeline_debug,
    )

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        messages.append(
            {"role": "system", "content": "Please introduce yourself to the user."}
        )
        await task.queue_frames([LLMMessagesFrame(messages)])

    runner = PipelineRunner()
    await runner.run(task)


def start():
    asyncio.run(main())


if __name__ == "__main__":
    start()
