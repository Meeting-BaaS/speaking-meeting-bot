import argparse
import os


async def configure(
    parser: argparse.ArgumentParser | None = None,
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
        "--system-prompt", type=str, required=False, help="Prompt of the AI Bot"
    )
    parser.add_argument(
        "--voice-id",
        type=str,
        required=False,
        help="Cartesia voice ID for text-to-speech conversion",
    )
    parser.add_argument(
        "--theme",
        type=str,
        default="general conversation, while being quirky, to the point and hating small talk and bureaucracy. Straight to the point.",
        required=False,
        help="The theme or topic the bot specializes in",
    )

    args, unknown = parser.parse_known_args()
    theme = args.theme or os.getenv("MEETINGBAAS_THEME", "general conversation")
    system_prompt = (
        args.system_prompt
        or f"""\
You are a helpful assistant who specializes in {theme}. Respond concisely to questions.

You are currently in a meeting as a meeting bot, and your response will be turned into speech so use only simple words and punctuation.

You have access to two tools: get_weather and get_time.

You can respond to questions about the weather using the get_weather tool.
"""
    )
    voice_id = args.voice_id or os.getenv("CARTESIA_VOICE_ID")

    if not voice_id:
        raise Exception(
            "No Cartesia voice ID. use the -v/--voice-id option from the command line, or set CARTESIA_API_KEY in your environment to specify a Cartesia voice ID."
        )

    return (args.host, args.port, system_prompt, voice_id, args)
