import argparse
import asyncio
import os
import random
import subprocess
from typing import Dict, Optional

from dotenv import load_dotenv

from config.persona_utils import (
    PersonaManager,
)
from config.prompts import DEFAULT_CHARACTERISTICS as PROMPTS_CHARACTERISTICS
from config.prompts import (
    DEFAULT_ENTRY_MESSAGE,
    DEFAULT_SYSTEM_PROMPT,
    IS_ANIMAL,
    SKIN_TONES,
)
from config.prompts import (
    DEFAULT_VOICE_CHARACTERISTICS as PROMPTS_VOICE_CHARACTERISTICS,
)
from config.voice_utils import VoiceUtils, get_language_input
from meetingbaas_pipecat.utils.logger import configure_logger

# Load environment variables
load_dotenv()
REPLICATE_KEY = os.getenv("REPLICATE_KEY")
UTFS_KEY = os.getenv("UTFS_KEY")
APP_ID = os.getenv("APP_ID")

logger = configure_logger()


def create_persona_structure(
    key: str,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    entry_message: Optional[str] = None,
    characteristics: Optional[list] = None,
    tone_of_voice: Optional[list] = None,
    skin_tone: Optional[str] = None,
    gender: Optional[str] = None,
    relevant_links: Optional[list] = None,
) -> Dict:
    """Create a persona dictionary with provided or default values"""
    # If no skin tone provided, randomly select one (unless it's an animal)
    if not skin_tone and not IS_ANIMAL:
        skin_tone = random.choice(SKIN_TONES)
        logger.info(f"Randomly selected skin tone: {skin_tone}")

    if not gender:
        gender = random.choice(["MALE", "FEMALE", "NON-BINARY"])
        logger.info(f"Randomly selected gender: {gender}")

    return {
        "name": name or key.replace("_", " ").title(),
        "prompt": prompt or DEFAULT_SYSTEM_PROMPT,
        "entry_message": entry_message or DEFAULT_ENTRY_MESSAGE,
        "characteristics": characteristics or PROMPTS_CHARACTERISTICS,
        "tone_of_voice": tone_of_voice or PROMPTS_VOICE_CHARACTERISTICS,
        "skin_tone": skin_tone,
        "gender": gender,
        "relevant_links": relevant_links or [],
        "image": "",  # Will be populated by image generation
    }


def generate_persona_image(
    persona_key: str, replicate_key: str, utfs_key: str, app_id: str
):
    """Generate and upload image for the persona"""
    try:
        cmd = [
            "python",
            "config/generate_images.py",
            "--replicate-key",
            replicate_key,
            "--utfs-key",
            utfs_key,
            "--app-id",
            app_id,
        ]

        subprocess.run(cmd, check=True)
        logger.success(f"Generated and uploaded image for {persona_key}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate image: {e}")
        raise


async def create_persona_cli():
    parser = argparse.ArgumentParser(
        description="""Interactive persona creation tool for the meeting bot.
        
The persona key should be in snake_case format (e.g., tech_expert, friendly_interviewer).
If not provided via command line, you will be prompted to enter it.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Make key optional
    parser.add_argument(
        "key",
        nargs="?",  # Makes the positional argument optional
        help="Unique identifier for the persona (e.g., tech_expert, friendly_interviewer)",
    )
    parser.add_argument("--name", help="Display name for the persona")
    parser.add_argument("--prompt", help="Main prompt/description")
    parser.add_argument("--entry-message", help="Entry message when joining")
    parser.add_argument(
        "--blank", action="store_true", help="Create with minimal values"
    )
    parser.add_argument("--replicate-key", help="Override Replicate API key from .env")
    parser.add_argument("--utfs-key", help="Override UTFS API key from .env")
    parser.add_argument("--app-id", help="Override UTFS App ID from .env")
    parser.add_argument(
        "--non-interactive", action="store_true", help="Skip interactive prompts"
    )

    args = parser.parse_args()

    # Initialize PersonaManager
    persona_manager = PersonaManager()

    try:
        # If no key provided, prompt for it
        if not args.key:
            print("\n=== Persona Key Creation ===")
            print("Format: use_underscores_like_this")
            print("Examples:")
            print("  • tech_expert")
            print("  • friendly_interviewer")
            print("  • sales_specialist")

            while True:
                key = input("\n🔑 Persona key: ").strip().lower()
                if key:
                    if " " in key:
                        print("Please use underscores instead of spaces")
                        continue
                    if key in persona_manager.personas:
                        print(f"Warning: Persona '{key}' already exists.")
                        choice = input(
                            "Press Enter to overwrite or any key to choose another name > "
                        )
                        if choice.strip():
                            continue
                    args.key = key
                    break
                print("Key cannot be empty")

        if args.blank or args.non_interactive:
            persona_data = create_persona_structure(args.key)
        else:
            # System prompt
            print("\n=== System Prompt ===")
            print("This is the core of your persona's behavior.")
            print("Press Enter twice to finish.")
            print("\nCurrent default:")
            print("-" * 50)
            print(f"{DEFAULT_SYSTEM_PROMPT}")
            print("-" * 50)

            prompt_lines = []
            while True:
                line = input()
                if not line and prompt_lines and not prompt_lines[-1]:
                    break
                prompt_lines.append(line)
            prompt = (
                args.prompt or "\n".join(prompt_lines[:-1]) or DEFAULT_SYSTEM_PROMPT
            )

            # Name
            print("\n=== Display Name ===")
            default_name = args.key.replace("_", " ").title()
            name = (
                args.name
                or input(f"💭 Enter display name (default: {default_name}): ").strip()
            )
            if not name:
                name = default_name

            # Entry message
            print("\n=== Entry Message ===")
            print(f"Default: {DEFAULT_ENTRY_MESSAGE}")
            entry_message = (
                args.entry_message
                or input("💬 Enter message: ").strip()
                or DEFAULT_ENTRY_MESSAGE
            )

            # Characteristics
            print("\n=== Characteristics ===")
            print("Current defaults:")
            for char in PROMPTS_CHARACTERISTICS:
                print(f"  • {char}")
            print("\nEnter new characteristics (empty line to finish):")

            characteristics = []
            while True:
                char = input("✨ > ").strip()
                if not char:
                    break
                characteristics.append(char)
            if not characteristics:
                characteristics = PROMPTS_CHARACTERISTICS

            # Tone of voice
            print("\n=== Tone of Voice ===")
            print("Current defaults:")
            for tone in PROMPTS_VOICE_CHARACTERISTICS:
                print(f"  • {tone}")
            print("\nEnter new voice characteristics (empty line to finish):")

            tone_of_voice = []
            while True:
                tone = input("🗣️ > ").strip()
                if not tone:
                    break
                tone_of_voice.append(tone)
            if not tone_of_voice:
                tone_of_voice = PROMPTS_VOICE_CHARACTERISTICS

            # Skin tone
            print("\n=== Skin Tone ===")
            print("Current defaults:")
            for skin_tone in SKIN_TONES:
                print(f"  • {skin_tone}")
            print("\nEnter skin tone (empty for random):")

            skin_tone = input("👩‍🦰 > ").strip()

            # Gender selection
            print("\n=== Gender ===")
            print("Options: MALE, FEMALE, NON-BINARY")
            print("Press Enter for random selection")
            gender = input("🧑 > ").strip().upper()
            if gender and gender not in ["MALE", "FEMALE", "NON-BINARY"]:
                print("Invalid gender, using random selection")
                gender = None

            # Relevant links
            print("\n=== Relevant Links ===")
            print("Enter links one per line (empty line to finish)")
            relevant_links = []
            while True:
                link = input("🔗 > ").strip()
                if not link:
                    break
                relevant_links.append(link)

            # Language selection
            language_code = get_language_input()
            persona_data["language"] = language_code

            persona_data = create_persona_structure(
                args.key,
                name=name,
                prompt=prompt,
                entry_message=entry_message,
                characteristics=characteristics,
                tone_of_voice=tone_of_voice,
                skin_tone=skin_tone,
                gender=gender,
                relevant_links=relevant_links,
            )

        # Save persona
        persona_manager.personas[args.key] = persona_data
        success = persona_manager.save_persona(args.key, persona_data)

        if not success:
            logger.error("Failed to save persona")
            return 1

        logger.success(f"Created persona: {args.key}")

        # Image generation
        replicate_key = args.replicate_key or REPLICATE_KEY
        utfs_key = args.utfs_key or UTFS_KEY
        app_id = args.app_id or APP_ID

        if replicate_key and utfs_key and app_id:
            logger.info("Generating persona image...")
            generate_persona_image(args.key, replicate_key, utfs_key, app_id)
        else:
            logger.warning("Missing API keys - skipping image generation")

        # If we have API keys, try to match a voice
        if REPLICATE_KEY and UTFS_KEY and APP_ID:
            voice_utils = VoiceUtils()
            voice_id = await voice_utils.match_voice_to_persona(args.key, language_code)
            if voice_id:
                persona_data["cartesia_voice_id"] = voice_id
                # Save the updated persona with the voice ID
                success = persona_manager.save_persona(args.key, persona_data)
                if not success:
                    logger.error("Failed to save persona with voice ID")

        return 0

    except Exception as e:
        logger.error(f"Error creating persona: {e}")
        return 1


if __name__ == "__main__":
    exit(asyncio.run(create_persona_cli()))
