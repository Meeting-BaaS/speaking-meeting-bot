# app/bots/gemini_live_bot.py

import argparse
import asyncio
import os
import logging
import sys
from typing import Optional
from dotenv import load_dotenv

from pipecat.frames.frames import LLMMessagesAppendFrame, AudioRawFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.logger import FrameLogger
from pipecat.processors.aggregators.sentence import SentenceAggregator
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
import json

# Services
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from pipecat.transports.websocket.client import (
    WebsocketClientParams,
    WebsocketClientTransport,
)

from app.utils.pipecat_logger import configure_logger, logger

load_dotenv()

# Configure logger
configure_logger()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s | %(name)s:%(funcName)s:%(lineno)d | %(message)s'
)
logger = logging.getLogger(__name__)

class AudioFrameVerificationLogger(FrameProcessor):
    """Custom processor to verify AudioFrame generation in logs."""
    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            logger.info(f"🔊 [TTS VERIFICATION] AudioFrame generated: {len(frame.audio)} bytes")

def get_tts_service():
    """
    Initialize and return a TTS service based on available environment variables.
    Priority: Deepgram (Works with API Key) > ElevenLabs > Google Cloud (Requires Service Account)
    """
    deepgram_key = os.getenv("DEEPGRAM_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
    google_key = os.getenv("GOOGLE_API_KEY")

    if deepgram_key:
        logger.info("✅ Using Deepgram Text-to-Speech")
        return DeepgramTTSService(
            api_key=deepgram_key,
            voice="aura-asteria-en"
        )
    elif elevenlabs_key:
        logger.info("✅ Using ElevenLabs Text-to-Speech")
        return ElevenLabsTTSService(
            api_key=elevenlabs_key,
            voice_id="pNInz6obpgDQGcFmaJgB",
        )
    elif google_key:
        # Note: Standard GoogleTTSService often requires a service account JSON.
        # If using an API key, it might fail depending on the specific Pipecat version/provider logic.
        logger.info("✅ Using Google Cloud Text-to-Speech")
        return GoogleTTSService(
            api_key=google_key,
            settings=GoogleTTSService.Settings(
                voice="en-US-Neural2-F"
            )
        )
    else:
        logger.error("❌ No TTS API credentials found (DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, or GOOGLE_API_KEY)")
        raise ValueError("No valid credentials provided")

async def run_sales_bot(
    client_id: str,
    websocket_url: str,
    meeting_url: str = "",
    persona_data: Optional[dict] = None,
    streaming_audio_frequency: str = "24khz",
    enable_tools: bool = False,
) -> None:
    """
    Connect via WebSocket to FastAPI server and run Pipecat pipeline with TTS.
    
    Pipeline: Input -> STT -> LLM -> SentenceAggregator -> TTS -> Output
    """
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")

    if not gemini_api_key:
        logger.error("❌ No GEMINI_API_KEY or GOOGLE_API_KEY in environment!")
        sys.exit(1)
    
    if not deepgram_api_key:
        logger.error("❌ No DEEPGRAM_API_KEY in environment (required for STT)!")
        sys.exit(1)

    logger.info(f"🚀 Starting bot {client_id}")

    # ── Transport: WebSocket client ──
    transport = WebsocketClientTransport(
        uri=websocket_url,
        params=WebsocketClientParams(
            audio_out_sample_rate=24000,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_enabled=True,
            serializer=ProtobufFrameSerializer(),
        ),
    )

    # ── STT Service ──
    stt = DeepgramSTTService(api_key=deepgram_api_key)

    # ── LLM Service (Gemini) ──
    # Extract persona details
    name = persona_data.get("name", "Meeting Bot") if persona_data else "Meeting Bot"
    system_instruction = persona_data.get("readme", "You are a helpful meeting assistant.") if persona_data else "You are a helpful meeting assistant."
    
    llm = GoogleLLMService(
        api_key=gemini_api_key,
        settings=GoogleLLMService.Settings(
            model="gemini-2.5-flash",
            system_instruction=system_instruction,
        )
    )

    # Set up context aggregator
    context = LLMContext([{"role": "system", "content": system_instruction}])
    context_aggregator = LLMContextAggregatorPair(context=context)

    # ── Sentence Aggregator (Ensures full sentences are sent to TTS) ──
    aggregator = SentenceAggregator()

    # ── TTS Service ──
    try:
        tts = get_tts_service()
    except ValueError as e:
        logger.error(f"❌ TTS Initialization failed: {e}")
        sys.exit(1)

    # ── Logging & Verification ──
    audio_logger = AudioFrameVerificationLogger()
    transcript_logger = FrameLogger("Transcription", ignored_frame_types=(AudioRawFrame,))

    # ── Pipeline Order: Input -> STT -> Context(User) -> LLM -> Aggregator -> TTS -> Output -> Context(Assistant) ──
    pipeline = Pipeline([
        transport.input(),    # 1. Receive audio from meeting
        stt,                  # 2. Audio -> Text
        context_aggregator.user(),
        llm,                  # 3. Text -> Response Text
        aggregator,           # 4. Group into sentences
        tts,                  # 5. Text -> Audio
        transport.output(),   # 6. Send audio back
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_connected")
    async def on_connected(transport, client):
        logger.info("🔗 Transport connected to FastAPI server!")
        await asyncio.sleep(1)
        greeting = persona_data.get("entry_message", f"Hi everyone, I am {name}, your meeting assistant.") if persona_data else "Hello!"
        await task.queue_frames([
            LLMMessagesAppendFrame([
                {"role": "user", "content": greeting}
            ]),
            TextFrame(greeting)
        ])
        logger.info(f"📢 Queued greeting: {greeting}")

    @transport.event_handler("on_connection_error")
    async def on_error(error, *args, **kwargs):
        logger.error(f"❌ Connection error: {error}")

    runner = PipelineRunner()
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"❌ Pipeline error: {e}")
        import traceback
        traceback.print_exc()

def start() -> None:
    parser = argparse.ArgumentParser(description="Gemini Live Meeting Bot")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--websocket-url", required=True)
    parser.add_argument("--meeting-url", default="")
    parser.add_argument("--persona-data-json", default="{}")
    parser.add_argument("--streaming-audio-frequency", default="24khz")
    parser.add_argument("--enable-tools", action="store_true")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--meetingbaas-bot-id", default="")
    
    args = parser.parse_args()

    persona_data = json.loads(args.persona_data_json)

    asyncio.run(
        run_sales_bot(
            client_id=args.client_id,
            websocket_url=args.websocket_url,
            meeting_url=args.meeting_url,
            persona_data=persona_data,
            streaming_audio_frequency=args.streaming_audio_frequency,
            enable_tools=args.enable_tools,
        )
    )

if __name__ == "__main__":
    start()