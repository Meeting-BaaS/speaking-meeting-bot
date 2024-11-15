import random
from pathlib import Path
from typing import Dict, List, Optional, Union

import markdown
from loguru import logger

from config.prompts import (
    DEFAULT_CHARACTERISTICS,
    DEFAULT_ENTRY_MESSAGE,
    DEFAULT_VOICE_CHARACTERISTICS,
    PERSONA_INTERACTION_INSTRUCTIONS,
)


class PersonaManager:
    def __init__(self, personas_dir: Optional[Path] = None):
        """Initialize PersonaManager with optional custom personas directory"""
        self.personas_dir = personas_dir or Path(__file__).parent / "personas"
        self.md = markdown.Markdown(extensions=["meta"])
        self.personas = self.load_personas()

    def parse_readme(self, content: str) -> Dict:
        """Parse README.md content to extract persona information"""
        # Reset markdown instance for new content
        self.md.reset()
        html = self.md.convert(content)

        # Split content by sections
        sections = content.split("\n## ")

        # Get name from first line (# Title)
        name = sections[0].split("\n", 1)[0].replace("# ", "").strip()

        # Get prompt (first paragraph after title)
        prompt = sections[0].split("\n\n", 1)[1].strip()

        # Parse metadata section
        metadata = {"image": "", "entry_message": ""}  # Default values
        for section in sections:
            if section.startswith("Metadata"):
                for line in section.split("\n"):
                    if line.startswith("- "):
                        try:
                            key_value = line[2:].split(": ", 1)
                            if len(key_value) == 2:
                                key, value = key_value
                                metadata[key] = value.strip()
                        except ValueError:
                            # Skip malformed lines
                            continue
                break

        return {
            "name": name,
            "prompt": prompt,
            "image": metadata.get("image", ""),
            "entry_message": metadata.get("entry_message", ""),
        }

    def load_personas(self) -> Dict:
        """Load personas from directory structure"""
        personas = {}
        try:
            for persona_dir in self.personas_dir.iterdir():
                if not persona_dir.is_dir():
                    continue

                readme_file = persona_dir / "README.md"
                if not readme_file.exists():
                    logger.warning(
                        f"Skipping persona without README: {persona_dir.name}"
                    )
                    continue

                with open(readme_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    personas[persona_dir.name] = self.parse_readme(content)

            return personas
        except Exception as e:
            logger.error(f"Failed to load personas: {e}")
            raise

    def save_persona(self, key: str, persona: Dict) -> bool:
        """Save a single persona's data"""
        try:
            persona_dir = self.personas_dir / key
            persona_dir.mkdir(exist_ok=True)

            # Format characteristics and voice characteristics from prompts
            characteristics = "\n".join(f"- {char}" for char in DEFAULT_CHARACTERISTICS)
            voice_chars = "\n".join(
                f"- {char}" for char in DEFAULT_VOICE_CHARACTERISTICS
            )

            readme_content = f"""# {persona['name']}

{persona['prompt']}

## Characteristics
{characteristics}

## Voice
{persona['name']} speaks with:
{voice_chars}

## Metadata
- image: {persona['image']}
- entry_message: {persona.get('entry_message', DEFAULT_ENTRY_MESSAGE)}
"""

            with open(persona_dir / "README.md", "w", encoding="utf-8") as f:
                f.write(readme_content)

            return True
        except Exception as e:
            logger.error(f"Failed to save persona {key}: {e}")
            return False

    def save_personas(self) -> bool:
        """Save all personas to their respective README files"""
        success = True
        for key, persona in self.personas.items():
            if not self.save_persona(key, persona):
                success = False
                logger.error(f"Failed to save persona {key}")
        return success

    def list_personas(self) -> List[str]:
        """Returns a sorted list of available persona names"""
        return sorted(self.personas.keys())

    def get_persona(self, name: Optional[str] = None) -> Dict:
        """Get a persona by name or return a random one"""
        if name:
            if name not in self.personas:
                raise KeyError(
                    f"Persona '{name}' not found. Valid options: {', '.join(self.personas.keys())}"
                )
            persona = self.personas[name].copy()
            logger.info(f"Using specified persona: {name}")
        else:
            persona = random.choice(list(self.personas.values())).copy()
            logger.info(f"Randomly selected persona: {persona['name']}")

        # Only set default image if needed for display purposes
        if not persona.get("image"):
            persona["image"] = ""  # Empty string instead of default URL

        persona["prompt"] = persona["prompt"] + PERSONA_INTERACTION_INSTRUCTIONS
        return persona

    def get_persona_by_name(self, name: str) -> Dict:
        """Get a specific persona by display name"""
        for persona in self.personas.values():
            if persona["name"] == name:
                return persona.copy()
        raise KeyError(
            f"Persona '{name}' not found. Valid options: {', '.join(p['name'] for p in self.personas.values())}"
        )

    def update_persona_image(self, key: str, image_path: Union[str, Path]) -> bool:
        """Update image path/URL for a specific persona"""
        if key in self.personas:
            self.personas[key]["image"] = str(image_path)
            return self.save_persona(key, self.personas[key])
        logger.error(f"Persona key '{key}' not found")
        return False

    def get_image_urls(self) -> Dict[str, str]:
        """Get mapping of persona keys to their image URLs"""
        return {key: persona.get("image", "") for key, persona in self.personas.items()}

    def needs_image_upload(self, key: str, domain: str = "uploadthing.com") -> bool:
        """Check if a persona needs image upload"""
        if key not in self.personas:
            return False
        current_url = self.personas[key].get("image", "")
        return not (current_url and domain in current_url)


# Global instance for easy access
persona_manager = PersonaManager()
