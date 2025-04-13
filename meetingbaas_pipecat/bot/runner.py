import argparse
import os

from loguru import logger

from config.persona_utils import persona_manager


async def configure(
    parser: argparse.ArgumentParser | None = None, system_prompt: str = None
):
    if not parser:
        parser = argparse.ArgumentParser(description="Pipecat SDK AI Bot")
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="Host to bind the server to"
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8766, help="Port to run the server on"
    )
    parser.add_argument(
        "--persona-name",
        type=str,
        required=False,
        help="Name of the persona to use",
    )
    parser.add_argument(
        "--speak-first",
        type=int,
        help="Index of the bot that should speak first (1-based)",
    )
    parser.add_argument(
        "--meeting-url",
        type=str,
        required=True,
        help="The meeting URL to join",
    )
    parser.add_argument(
        "--websocket-url",
        type=str,
        default="ws://localhost:8000",
        help="The WebSocket URL for MeetingBaas to connect to",
    )
    parser.add_argument(
        "--bot-id",
        type=str,
        default="default",
        help="Unique identifier for the bot",
    )
    parser.add_argument(
        "--recorder-only",
        action="store_true",
        help="Whether the bot is a recorder only",
    )
    parser.add_argument(
        "--meeting-baas-api-key",
        type=str,
        help="The API key for MeetingBaas (falls back to MEETING_BAAS_API_KEY env var if not provided)",
    )
    parser.add_argument(
        "--output-bot-id",
        action="store_true",
        help="Print the MeetingBaas bot ID in a machine-readable format",
    )

    args, unknown = parser.parse_known_args()

    logger.warning(f"**FOR ARGS: {args}**")
    # Get persona based on name or random if none provided
    persona = persona_manager.get_persona(args.persona_name)

    # Use persona's prompt as system prompt if no override provided
    system_prompt = system_prompt or persona["prompt"]

    # Pass on additional content from persona directory
    additional_content = persona.get("additional_content", "")
    if not additional_content:
        logger.warning(f"**NO ADDITIONAL CONTENT FOUND for {persona['name']}**")
        # raise Exception("No additional content found for persona")
    else:
        logger.warning(f"**ADDITIONAL CONTENT FOUND for {persona['name']}**")
        # logger.warning(f"Additional content: {additional_content}")

    if not system_prompt:
        raise Exception("No system prompt provided")

    # Get voice ID from persona data
    voice_id = persona.get("cartesia_voice_id")
    if not voice_id:
        # Fallback to environment variable only if not in persona
        voice_id = os.getenv("CARTESIA_VOICE_ID")
        if not voice_id:
            raise Exception(
                "No Cartesia voice ID found in persona config or environment variables"
            )

    logger.warning(
        f"returning {args.host, args.port, system_prompt, voice_id, persona['name'], args, additional_content}"
    )
    return (
        args.host,
        args.port,
        system_prompt,
        voice_id,
        persona["name"],
        args,
        additional_content,
    )
