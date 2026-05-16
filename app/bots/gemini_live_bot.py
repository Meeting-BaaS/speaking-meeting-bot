# app/bots/gemini_live_bot.py

import argparse
import asyncio
import os
import sys
from typing import Optional
from dotenv import load_dotenv

from pipecat.frames.frames import LLMMessagesAppendFrame, AudioRawFrame, TextFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.logger import FrameLogger
from pipecat.processors.aggregators.sentence import SentenceAggregator
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
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
from app.services.session_manager import session_manager

load_dotenv()

# Configure logger
configure_logger()

# Setup logging

class AudioFrameVerificationLogger(FrameProcessor):
    """Custom processor to verify AudioFrame generation in logs."""
    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            logger.info(f"🔊 [TTS VERIFICATION] AudioFrame generated: {len(frame.audio)} bytes")
        await self.push_frame(frame, direction)

class TranscriptionHook(FrameProcessor):
    """
    In-process hook to directly extract transcriptions and save them to the session manager.
    Since Pipecat now runs in the same process as FastAPI, we can bypass the websocket overhead.
    """
    def __init__(self, client_id: str):
        super().__init__()
        self.client_id = client_id

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        
        # Intercept transcription frames and write directly to global session state
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if text:
                speaker = frame.user_id if getattr(frame, "user_id", None) else "User"
                session_manager.add_transcription(self.client_id, speaker, text)
                logger.info(f"📝 Transcribed ({speaker}): {text}")
        await self.push_frame(frame, direction)

class TTSReEntryFilter(FrameProcessor):
    """
    Prevents TTS audio from being fed back into the STT engine.
    This acts as a one-way valve in the pipeline.
    """
    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Allow everything EXCEPT audio frames coming from TTS
        if isinstance(frame, AudioRawFrame) and getattr(frame, "source", None) == "tts":
            return

        await self.push_frame(frame, direction)

def get_tts_service(sample_rate: int):
    """
    Initialize and return a TTS service based on available environment variables.
    Priority: Deepgram (Works with API Key) > ElevenLabs > Google Cloud (Requires Service Account)
    """
    deepgram_key = os.getenv("DEEPGRAM_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
    google_key = os.getenv("GOOGLE_API_KEY")

    if deepgram_key:
        logger.info(f"✅ Using Deepgram Text-to-Speech at {sample_rate} Hz")
        return DeepgramTTSService(
            api_key=deepgram_key,
            voice="aura-asteria-en",
            sample_rate=sample_rate,
        )
    elif elevenlabs_key:
        logger.info(f"✅ Using ElevenLabs Text-to-Speech at {sample_rate} Hz")
        return ElevenLabsTTSService(
            api_key=elevenlabs_key,
            voice_id="pNInz6obpgDQGcFmaJgB",
            sample_rate=sample_rate,
        )
    elif google_key:
        # Note: Standard GoogleTTSService often requires a service account JSON.
        # If using an API key, it might fail depending on the specific Pipecat version/provider logic.
        logger.info(f"✅ Using Google Cloud Text-to-Speech at {sample_rate} Hz")
        return GoogleTTSService(
            api_key=google_key,
            sample_rate=sample_rate,
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
        return
    
    if not deepgram_api_key:
        logger.error("❌ No DEEPGRAM_API_KEY in environment (required for STT)!")
        return

    logger.info(f"🚀 Starting bot {client_id}")
    output_sample_rate = 16000 if streaming_audio_frequency == "16khz" else 24000
    logger.info(
        f"🔉 Configured Pipecat audio rates: output={output_sample_rate}Hz, meeting={streaming_audio_frequency}"
    )

    # ── Transport: WebSocket client ──
    transport = WebsocketClientTransport(
        uri=websocket_url,
        params=WebsocketClientParams(
            audio_out_sample_rate=output_sample_rate,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    threshold=0.5,
                    min_speech_duration_ms=250,
                    min_silence_duration_ms=100,
                    min_volume=0.6,
                ),
            ),
            audio_in_passthrough=True,
            serializer=ProtobufFrameSerializer(),
        ),
    )

    # ── STT Service ──
    stt = DeepgramSTTService(
        api_key=deepgram_api_key,
        sample_rate=16000,
        encoding="linear16",
    )

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
        tts = get_tts_service(output_sample_rate)
    except ValueError as e:
        logger.error(f"❌ TTS Initialization failed: {e}")
        return
        
    # Change #13: TTS re-entry prevention
    tts_filter = TTSReEntryFilter()
    
    # Transcription native hook (Change #18: Bypass websocket for STT)
    transcription_hook = TranscriptionHook(client_id)

    # ── Logging & Verification ──
    audio_logger = AudioFrameVerificationLogger()
    # transcript_logger is no longer needed since TranscriptionHook logs directly

    # Change #8: VAD is handled by Pipecat's SmartTurn (built-in) via PipelineParams
    # Change #13: Insert TTSReEntryFilter before STT to block self-feedback
    # Pipeline: Input → TTS-filter → STT → TranscriptionHook → ContextUser → LLM → Aggregator → TTS → Output → ContextAssistant
    pipeline = Pipeline([
        transport.input(),           # 1. Receive audio from meeting
        tts_filter,                  # 2. Drop TTS self-feedback (Change #13)
        stt,                         # 3. Audio → Text (with VAD via smart_turn)
        transcription_hook,          # 4. Native intercept of transcript to session
        context_aggregator.user(),   # 5. Buffer until end of utterance
        llm,                         # 6. Text → Response Text (Gemini)
        aggregator,                  # 7. Group into sentences
        tts,                         # 8. Text → Audio
        audio_logger,                # 8.5 Debug: Verify AudioRawFrame generation
        transport.output(),          # 9. Send audio back to meeting
        context_aggregator.assistant(),  # 10. Save response to history
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
        
        from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame
        await task.queue_frames([
            LLMMessagesAppendFrame([
                {"role": "assistant", "content": greeting}
            ]),
            LLMFullResponseStartFrame(),
            TextFrame(greeting),
            LLMFullResponseEndFrame(),
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
