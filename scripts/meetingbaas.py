import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.vad.silero import SileroVADAnalyzer

from config.persona_utils import PersonaManager
from meetingbaas_pipecat.utils.logger import configure_logger

load_dotenv(override=True)

logger = configure_logger()


async def main(
    meeting_url: str,
    persona_name: str,
    websocket_url: str,
    speak_first: bool = False,
    recorder_only: bool = False,
) -> None:
    """Run the MeetingBaas bot with the specified configuration.

    Args:
        meeting_url: The URL of the meeting to join
        persona_name: The name of the persona to use
        websocket_url: The WebSocket server URL to connect to
        speak_first: Whether the bot should speak first
        recorder_only: Whether the bot should only record and not speak
    """
    try:
        # Get persona configuration
        persona = PersonaManager().get_persona(persona_name)
        if not persona:
            logger.error(f"Persona '{persona_name}' not found")
            return

        # Initialize services
        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id="40104aff-a015-4da1-9912-af950fbec99e",
        )

        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="gpt-4-turbo-preview",
        )

        # Create Daily transport
        transport = DailyTransport(
            meeting_url,
            "Bot",
            token=os.getenv("DAILY_API_KEY"),
            params=DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                camera_out_enabled=True,
                vad_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
                transcription_enabled=True,
                websocket_url=websocket_url,
            ),
        )

        # Set up messages only once
        messages = [
            {
                "role": "system",
                "content": persona["prompt"],
            },
        ]

        # Create pipeline using the single instances
        pipeline = Pipeline(
            [
                transport.input(),
                OpenAILLMContext(messages),
                llm,
                tts,
                transport.output(),
            ]
        )

        # Create and run task
        task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
        runner = PipelineRunner()

        if speak_first:
            await transport.capture_user_audio("Hello, I'm ready to start the meeting.")

        await runner.run(task)

    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run MeetingBaas bot")
    parser.add_argument(
        "--meeting-url",
        required=True,
        help="The URL of the meeting to join",
    )
    parser.add_argument(
        "--persona-name",
        required=True,
        help="The name of the persona to use",
    )
    parser.add_argument(
        "--websocket-url",
        required=True,
        help="The WebSocket server URL to connect to",
    )
    parser.add_argument(
        "--speak-first",
        action="store_true",
        help="Whether the bot should speak first",
    )
    parser.add_argument(
        "--recorder-only",
        action="store_true",
        help="Whether the bot should only record and not speak",
    )

    args = parser.parse_args()

    try:
        asyncio.run(
            main(
                args.meeting_url,
                args.persona_name,
                args.websocket_url,
                args.speak_first,
                args.recorder_only,
            )
        )
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)
