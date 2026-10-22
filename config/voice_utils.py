import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from openai import OpenAI

from config.persona_utils import PersonaManager

# Load environment variables
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPPORTED_LANGUAGES = [
    "English (en)",
    "French (fr)",
    "German (de)",
    "Spanish (es)",
    "Portuguese (pt)",
    "Chinese (zh)",
    "Japanese (ja)",
    "Hindi (hi)",
    "Italian (it)",
    "Korean (ko)",
    "Dutch (nl)",
    "Polish (pl)",
    "Russian (ru)",
    "Swedish (sv)",
    "Turkish (tr)",
]


class CartesiaVoiceManager:
    def __init__(self, api_key: Optional[str] = None):
        """Initialize CartesiaVoiceManager with optional API key"""
        self.api_key = api_key or os.getenv("CARTESIA_API_KEY")
        if not self.api_key:
            logger.warning("Cartesia API key not found in environment variables")

    async def list_voices(self) -> List[Dict]:
        """List all available Cartesia voices"""
        if not self.api_key:
            logger.warning("Cannot list voices: No API key provided")
            return []

        url = "https://api.cartesia.ai/voices/"
        headers = {"X-API-Key": self.api_key, "Cartesia-Version": "2024-06-10"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        voices = await response.json()
                        return voices
                    else:
                        error_msg = await response.text()
                        logger.error(f"Failed to fetch voices: {error_msg}")
                        return []
        except Exception as e:
            logger.error(f"Error connecting to Cartesia API: {e}")
            return []


# Create global instance
cartesia_voice_manager = CartesiaVoiceManager()


class VoiceUtils:
    def __init__(self):
        self.client = OpenAI(api_key=OPENAI_API_KEY)
        self.persona_manager = PersonaManager()

    async def save_voices_to_md(self) -> Optional[Path]:
        """Save all available Cartesia voices to a markdown file"""
        try:
            voices = await cartesia_voice_manager.list_voices()
            output_path = Path(__file__).parent / "available_voices.md"

            with open(output_path, "w", encoding="utf-8") as f:
                f.write("# Available Cartesia Voices\n\n")
                f.write(
                    f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                )

                for voice in voices:
                    f.write(f"## {voice['name']}\n\n")
                    f.write(f"- **ID**: `{voice['id']}`\n")
                    f.write(f"- **Language**: {voice['language']}\n")
                    if voice.get("description"):
                        f.write(f"- **Description**: {voice['description']}\n")
                    f.write(
                        f"- **Public**: {'Yes' if voice.get('is_public') else 'No'}\n"
                    )
                    f.write("\n---\n\n")

            logger.success(f"Saved voice information to {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Error saving voices to MD: {e}")
            return None

    async def match_voice_to_persona(
        self, persona_key: Optional[str] = None, language_code: str = "en", persona_details: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Use GPT-4 to match a persona with an appropriate voice"""
        try:
            persona = None
            if persona_details:
                # If persona_details are provided, use them directly (for temporary personas)
                persona = persona_details
                logger.info("Using provided persona details for voice matching.")
            elif persona_key:
                # Otherwise, try to get persona details from persona_manager
                persona = self.persona_manager.personas.get(persona_key)
                logger.info(f"Attempting to match voice for predefined persona: {persona_key}")

            if not persona:
                logger.error(f"Persona not found, neither by key '{persona_key}' nor provided details.")
                return None

            # Get available voices
            voices = await cartesia_voice_manager.list_voices()
            voices = [v for v in voices if v.get("language") == language_code]

            if not voices:
                logger.error(f"No voices available for language {language_code}")
                return None

            # Prepare prompt for GPT-4
            voices_text = "\n".join(
                [
                    f"Voice {i+1}: {v['name']} - {v.get('description', 'No description')}"
                    for i, v in enumerate(voices)
                ]
            )

            # Truncate the persona prompt to avoid context length errors
            # Voice matching only needs the core description, not additional content
            persona_prompt = persona.get('prompt', '')
            max_prompt_chars = 1500  # Keep it brief for voice matching
            if len(persona_prompt) > max_prompt_chars:
                persona_prompt = persona_prompt[:max_prompt_chars] + "..."
                logger.info(f"Truncated persona prompt for voice matching (was {len(persona['prompt'])} chars)")

            prompt = f"""Given this persona:
Name: {persona['name']}
Description: {persona_prompt}
Gender: {persona.get('gender', 'Unknown')}

And these available voices:
{voices_text}

Which voice number (1-{len(voices)}) would be the most appropriate match? 
Respond with ONLY the number."""

            # Get GPT-4o-mini's recommendation (128k context, faster and cheaper)
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
            )

            try:
                voice_index = int(response.choices[0].message.content.strip()) - 1
                selected_voice = voices[voice_index]
                logger.info(
                    f"Matched {persona['name']} with voice: {selected_voice['name']}"
                )
                return selected_voice["id"]
            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing GPT response: {e}")
                return None

        except Exception as e:
            logger.error(f"Error matching voice to persona: {e}")
            return None

    async def update_persona_voice(self, persona_key: str, voice_id: str) -> bool:
        """Update the voice ID in a persona's README file"""
        try:
            persona = self.persona_manager.personas.get(persona_key)
            if not persona:
                logger.error(f"Persona {persona_key} not found")
                return False

            persona["cartesia_voice_id"] = voice_id
            success = self.persona_manager.save_persona(persona_key, persona)

            if success:
                logger.success(f"Updated voice ID for {persona_key}")
            return success

        except Exception as e:
            logger.error(f"Error updating persona voice: {e}")
            return False


def get_language_input() -> str:
    """Interactive prompt for selecting language"""
    print("\n=== Language Selection ===")
    print("Available languages:")
    for i, lang in enumerate(SUPPORTED_LANGUAGES, 1):
        print(f"{i}. {lang}")

    while True:
        try:
            choice = input(
                "\nSelect language number (default: 1 for English): "
            ).strip()
            if not choice:
                return "en"

            index = int(choice) - 1
            if 0 <= index < len(SUPPORTED_LANGUAGES):
                return SUPPORTED_LANGUAGES[index].split("(")[1].strip(")")
            print("Invalid selection. Please try again.")
        except ValueError:
            print("Please enter a valid number.")


async def main():
    """Main function for testing"""
    voice_utils = VoiceUtils()
    await voice_utils.save_voices_to_md()

    # Example of matching voices to all personas
    for persona_key in voice_utils.persona_manager.personas:
        voice_id = await voice_utils.match_voice_to_persona(persona_key)
        if voice_id:
            await voice_utils.update_persona_voice(persona_key, voice_id)


if __name__ == "__main__":
    asyncio.run(main())
