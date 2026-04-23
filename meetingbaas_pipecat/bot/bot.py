# scripts/meetingbaas.py - UPDATED for Gemini Live API

import argparse
import asyncio
import os
import logging
import sys
from dotenv import load_dotenv

from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.logger import FrameLogger
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService, GeminiModalities
from pipecat.transports.websocket.client import (
    WebsocketClientParams,
    WebsocketClientTransport,
)

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s | %(name)s:%(funcName)s:%(lineno)d | %(message)s'
)
logger = logging.getLogger(__name__)


async def run_sales_bot(
    client_id: str,
    websocket_url: str,
    client_name: str = "Client",
    marketing_person_email: str = "",
) -> None:
    """
    Connect via WebSocket to FastAPI server and run Gemini Live pipeline.
    
    Gemini Live handles STT + LLM + TTS natively (speech-to-speech).
    """
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not gemini_api_key:
        logger.error("❌ No GEMINI_API_KEY or GOOGLE_API_KEY in environment!")
        sys.exit(1)

    logger.info(f"✅ Starting bot {client_id}")
    logger.info(f"   WebSocket: {websocket_url}")
    logger.info(f"   Client: {client_name}")
    logger.info(f"   Email: {marketing_person_email}")

    # ── Transport: WebSocket client connecting to FastAPI /pipecat/{client_id} ──
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
    logger.info("✅ WebSocket transport configured")

    # ── Gemini Live LLM (CRITICAL: handles audio natively) ──
    system_instruction = (
        f"You are an AI sales assistant in a meeting between "
        f"{marketing_person_email or 'a sales representative'} and {client_name}. "
        f"\n\nBehavior Instructions:"
        f"\n- Actively participate and ask discovery questions:"
        f"\n  1) What is the biggest challenge your team faces today?"
        f"\n  2) What would success look like in the next 6 months?"
        f"\n  3) Who else is involved in the buying decision?"
        f"\n- Keep responses brief, professional, and empathetic."
        f"\n- Speak clearly and naturally."
        f"\n- When finished: 'Thank you — our team will follow up shortly.'"
    )

    gemini = GeminiLiveLLMService(
        api_key=gemini_api_key,
        settings=GeminiLiveLLMService.Settings(
            model="models/gemini-2.5-flash-native-audio-preview-12-2025",
            system_instruction=system_instruction,
            voice="Puck",  # or "Charon", "Sage"
            temperature=0.7,
            max_tokens=2048,
            language="en-US",
            modalities=GeminiModalities.AUDIO,
        ),
        inference_on_context_initialization=False,  # Don't speak on startup
    )
    logger.info("✅ Gemini Live LLM configured with native audio")

    transcript_logger = FrameLogger("Transcription")

    # ── Pipeline: Audio in → Gemini (speech-to-speech) → Audio out ──
    pipeline = Pipeline([
        transport.input(),    # Receive audio from meeting
        gemini,              # Gemini handles STT + LLM + TTS
        transcript_logger,   # Log Gemini's output (text/audio frames)
        transport.output(),  # Send audio back to meeting
    ])
    logger.info("✅ Pipeline created: Transport.Input → Gemini → Transport.Output")

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    # ── Event handlers ──
    @transport.event_handler("on_connected")
    async def on_connected(transport, client):
        logger.info("🔗 Transport connected to FastAPI server!")
        # Give Gemini a second to connect to Google
        await asyncio.sleep(1)
        logger.info("✅ Sending greeting...")
        greeting = f"Hi everyone, I'm an AI assistant here to help take notes for this meeting. Nice to meet you {client_name}!"
        await task.queue_frames([
            LLMMessagesAppendFrame([
                {"role": "user", "content": greeting}
            ])
        ])
        logger.info(f"📢 Queued greeting: {greeting}")

    @transport.event_handler("on_connection_error")
    async def on_error(error, *args, **kwargs):
        logger.error(f"❌ Connection error: {error}")

    # ── Run pipeline ──
    runner = PipelineRunner()
    try:
        logger.info("🚀 Starting pipeline...")
        await runner.run(task)
    except Exception as e:
        logger.error(f"❌ Pipeline error: {e}")
        import traceback
        traceback.print_exc()


def start() -> None:
    parser = argparse.ArgumentParser(description="Sales Meeting Bot (Gemini Live)")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--websocket-url", required=True)
    parser.add_argument("--client-name", default="Client")
    parser.add_argument("--marketing-email", default="")
    
    args = parser.parse_args()

    asyncio.run(
        run_sales_bot(
            client_id=args.client_id,
            websocket_url=args.websocket_url,
            client_name=args.client_name,
            marketing_person_email=args.marketing_email,
        )
    )


if __name__ == "__main__":
    start()