import asyncio
import random
import shutil
from pathlib import Path

from loguru import logger
from persona_utils import PersonaManager

from config.prompts import (
    DEFAULT_CHARACTERISTICS,
    DEFAULT_VOICE_CHARACTERISTICS,
)
from config.voice_utils import VoiceUtils


async def migrate_personas():
    """Migrate existing README.md files to add new voice and gender fields"""
    personas_dir = Path(__file__).parent / "personas"
    persona_mgr = PersonaManager()

    try:
        # Iterate through all persona directories
        for persona_dir in personas_dir.iterdir():
            if not persona_dir.is_dir():
                continue

            readme_path = persona_dir / "README.md"
            if not readme_path.exists():
                continue

            # Parse existing README
            persona_data = persona_mgr.parse_readme(readme_path.read_text())

            # Add new fields if not present
            if not persona_data.get("gender"):
                persona_data["gender"] = random.choice(["FEMALE", "NON-BINARY", "MALE"])
            if not persona_data.get("cartesia_voice_id"):
                persona_data["cartesia_voice_id"] = ""
            if not persona_data.get("relevant_links"):
                persona_data["relevant_links"] = []
            if not persona_data.get("language"):
                persona_data["language"] = (
                    "en"  # Default to English for existing personas
                )

            # Create backup of original README
            backup_path = readme_path.with_suffix(".md.bak")
            shutil.copy2(readme_path, backup_path)

            # Try to match voice if none exists
            if not persona_data.get("cartesia_voice_id"):
                voice_utils = VoiceUtils()
                voice_id = await voice_utils.match_voice_to_persona(
                    persona_dir.name, persona_data.get("language", "en")
                )
                if voice_id:
                    persona_data["cartesia_voice_id"] = voice_id

            # Write updated README with new fields
            readme_content = f"""# {persona_data['name']}

{persona_data['prompt']}

## Characteristics
{chr(10).join(f'- {char}' for char in DEFAULT_CHARACTERISTICS)}

## Voice
{persona_data['name']} speaks with:
{chr(10).join(f'- {char}' for char in DEFAULT_VOICE_CHARACTERISTICS)}

## Metadata
- image: {persona_data['image']}
- entry_message: {persona_data['entry_message']}
- cartesia_voice_id: {persona_data.get('cartesia_voice_id', '')}
- gender: {persona_data.get('gender', '')}
- relevant_links: {', '.join(persona_data.get('relevant_links', []))}
"""
            readme_path.write_text(readme_content)
            logger.info(f"Updated {persona_dir.name} README.md")

        logger.success("Successfully migrated all persona READMEs")

    except Exception as e:
        logger.error(f"Failed to migrate personas: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(migrate_personas())
