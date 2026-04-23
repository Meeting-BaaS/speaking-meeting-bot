# meetingbaas_pipecat/bot/bot.py
"""
Sales meeting agent powered by Gemini Live API.

Architecture:
  MeetingBaaS → /ws/{client_id} on FastAPI server
      → message_router bridges to /pipecat/{client_id}
      → THIS subprocess connects (as WebSocket client) to /pipecat/{client_id}
      → GeminiLiveLLMService (native audio: STT + LLM + TTS in one)
      → audio response back through the WebSocket to MeetingBaaS
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger


from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.websocket.client import (
    WebsocketClientParams,
    WebsocketClientTransport,
)

load_dotenv()


async def run_sales_bot(
    client_id: str,
    websocket_url: str,
    client_name: str = "Client",
    marketing_person_email: str = "",
    mode: str = "passive",
    max_engage_minutes: int = 3,
) -> None:
    """
    Connect to the FastAPI /pipecat/{client_id} WebSocket endpoint
    and run the Gemini Live pipeline.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("No GEMINI_API_KEY or GOOGLE_API_KEY found in environment!")
        sys.exit(1)

    logger.info(f"[Bot:{client_id}] Connecting to {websocket_url}")

    # ── Transport: connect as client to the FastAPI /pipecat/{client_id} endpoint ──
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

    # ── Gemini Live: handles STT + reasoning + TTS natively ──────────────────────
    system_instruction = (
        f"You are a smart AI sales assistant in a meeting between "
        f"{marketing_person_email or 'a sales representative'} and {client_name}. "
        f"In PASSIVE mode: listen carefully. Stay quiet — only speak if someone directly "
        f"addresses you by name ('hey bot', 'assistant', etc). "
        f"In ACTIVE mode: introduce yourself warmly and ask one discovery question at a time: "
        f"1) What is the biggest challenge your team faces today? "
        f"2) What would success look like for you in the next 6 months? "
        f"3) Who else is involved in the buying decision? "
        f"Keep answers brief, professional, and empathetic. "
        f"When finished, say: 'Thank you — our team will follow up shortly.'"
    )

    gemini = GeminiLiveLLMService(
        api_key=api_key,
        settings=GeminiLiveLLMService.Settings(
            model="models/gemini-2.5-flash-native-audio-preview-12-2025",
            system_instruction=system_instruction,
            voice="Puck",
            temperature=0.7,
            modalities=["audio"],
        ),
    )

    # ── Pipeline ──────────────────────────────────────────────────────────────────
    pipeline = Pipeline([
        transport.input(),
        gemini,
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    from pipecat.frames.frames import LLMMessagesAppendFrame

    async def mode_watcher():
        flag_file = f"/tmp/bot_{client_id}_engage"
        while True:
            await asyncio.sleep(2)
            if os.path.exists(flag_file):
                os.remove(flag_file)
                logger.info(f"[Bot:{client_id}] Received engage trigger! Switching mode...")
                await task.queue_frame(LLMMessagesAppendFrame([
                    {
                        "role": "user",
                        "content": "The user has switched you to ACTIVE mode. Introduce yourself warmly and ask your first discovery question now."
                    }
                ]))

    asyncio.create_task(mode_watcher())

    @transport.event_handler("on_connected")
    async def on_connected(*args, **kwargs):
        logger.info(f"[Bot:{client_id}] Connected to Pipecat WebSocket — sending greeting")
        # Send a short greeting so we can verify the bot can speak
        await task.queue_frame(LLMMessagesAppendFrame([
            {
                "role": "user",
                "content": "You just joined the meeting. Say a brief, friendly greeting to let participants know you're here to help take notes. Keep it under 2 sentences."
            }
        ]))

    runner = PipelineRunner()
    await runner.run(task)


def start() -> None:
    parser = argparse.ArgumentParser(description="Sales Meeting Bot (Gemini Live API)")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--websocket-url", required=True)
    parser.add_argument("--client-name", default="Client")
    parser.add_argument("--marketing-email", default="")
    parser.add_argument("--mode", default="passive", choices=["passive", "active"])
    parser.add_argument("--max-engage-minutes", type=int, default=3)
    args = parser.parse_args()

    asyncio.run(
        run_sales_bot(
            client_id=args.client_id,
            websocket_url=args.websocket_url,
            client_name=args.client_name,
            marketing_person_email=args.marketing_email,
            mode=args.mode,
            max_engage_minutes=args.max_engage_minutes,
        )
    )


if __name__ == "__main__":
    start()