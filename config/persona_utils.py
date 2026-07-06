import asyncio
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

import aiohttp
import markdown
from dotenv import load_dotenv
from loguru import logger

from config.prompts import (
    DEFAULT_CHARACTERISTICS,
    DEFAULT_ENTRY_MESSAGE,
    DEFAULT_VOICE_CHARACTERISTICS,
    PERSONA_INTERACTION_INSTRUCTIONS,
)

# Load environment variables from .env file
load_dotenv()


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

        # Get prompt - include ALL content sections except Metadata, Characteristics, and Voice
        # These sections contain the personality, instructions, and knowledge
        prompt_parts = []

        # First section (after title) - the main description
        if "\n\n" in sections[0]:
            first_part = sections[0].split("\n\n", 1)[1].strip()
            if first_part:
                prompt_parts.append(first_part)

        # Process remaining sections - include everything except metadata-like sections
        skip_sections = ["metadata", "characteristics", "voice"]
        for section in sections[1:]:
            section_title = section.split("\n", 1)[0].strip().lower()
            if section_title not in skip_sections:
                # Include section with its header restored
                prompt_parts.append("## " + section)

        # Combine all parts into the full prompt
        prompt = "\n\n".join(prompt_parts)

        # Parse metadata section
        metadata = {
            "image": "",
            "entry_message": "",
            "cartesia_voice_id": "",
            "gender": "",
            "relevant_links": [],
        }  # Default values
        for section in sections:
            if section.startswith("Metadata"):
                for line in section.split("\n"):
                    if line.startswith("- "):
                        try:
                            key_value = line[2:].split(": ", 1)
                            if len(key_value) == 2:
                                key, value = key_value
                                if key == "relevant_links":
                                    # Split by spaces instead of commas for URLs
                                    metadata[key] = [
                                        url for url in value.strip().split() if url
                                    ]
                                else:
                                    metadata[key] = value.strip()
                        except ValueError:
                            continue
                break

        return {
            "name": name,
            "prompt": prompt,
            "image": metadata.get("image", ""),
            "entry_message": metadata.get("entry_message", ""),
            "cartesia_voice_id": metadata.get("cartesia_voice_id", ""),
            "gender": metadata.get("gender", ""),
            "relevant_links": metadata.get("relevant_links", []),
        }

    def load_additional_content(self, persona_dir: Path) -> str:
        """Load additional markdown content from persona directory"""
        additional_content = []

        # Skip these files
        skip_files = {"README.md", ".DS_Store"}

        try:
            for file_path in persona_dir.glob("*.md"):
                if file_path.name not in skip_files:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            additional_content.append(
                                f"# Content from {file_path.name}\n\n{content}"
                            )
        except Exception as e:
            logger.error(f"Error loading additional content from {persona_dir}: {e}")

        return "\n\n".join(additional_content)

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
                    persona_data = self.parse_readme(content)

                    # Load additional content
                    additional_content = self.load_additional_content(persona_dir)
                    if additional_content:
                        persona_data["additional_content"] = additional_content

                    personas[persona_dir.name] = persona_data

            return personas
        except Exception as e:
            logger.error(f"Failed to load personas: {e}")
            raise

    def save_persona(self, key: str, persona: Dict) -> bool:
        """Save a single persona's data"""
        try:
            persona_dir = self.personas_dir / key
            persona_dir.mkdir(exist_ok=True)

            # Read existing README if it exists to preserve metadata
            readme_file = persona_dir / "README.md"
            existing_metadata = {}
            if readme_file.exists():
                with open(readme_file, "r", encoding="utf-8") as f:
                    existing_persona = self.parse_readme(f.read())
                    # Preserve all existing metadata fields
                    existing_metadata = {
                        "image": existing_persona.get("image", ""),
                        "entry_message": existing_persona.get(
                            "entry_message", DEFAULT_ENTRY_MESSAGE
                        ),
                        "cartesia_voice_id": existing_persona.get(
                            "cartesia_voice_id", ""
                        ),
                        "gender": existing_persona.get("gender", ""),
                        "relevant_links": existing_persona.get("relevant_links", []),
                    }

            # Merge existing metadata with new data, preferring new data when available
            metadata = {
                "image": persona.get("image", existing_metadata.get("image", "")),
                "entry_message": persona.get(
                    "entry_message",
                    existing_metadata.get("entry_message", DEFAULT_ENTRY_MESSAGE),
                ),
                "cartesia_voice_id": persona.get(
                    "cartesia_voice_id", existing_metadata.get("cartesia_voice_id", "")
                ),
                "gender": persona.get(
                    "gender",
                    existing_metadata.get("gender", random.choice(["MALE", "FEMALE"])),
                ),
                "relevant_links": persona.get(
                    "relevant_links", existing_metadata.get("relevant_links", [])
                ),
            }

            # Format characteristics and voice characteristics
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
- image: {metadata['image']}
- entry_message: {metadata['entry_message']}
- cartesia_voice_id: {metadata['cartesia_voice_id']}
- gender: {metadata['gender']}
- relevant_links: {' '.join(metadata['relevant_links'])}
"""

            with open(readme_file, "w", encoding="utf-8") as f:
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
            # Convert to folder name format
            folder_name = name.lower().replace(" ", "_")

            # First try exact folder match
            if folder_name in self.personas:
                persona = self.personas[folder_name].copy()
                logger.info(f"Using specified persona folder: {folder_name}")
            else:
                # Try to find the closest match among folder names
                words = set(name.lower().split())
                closest_match = None
                max_overlap = 0

                for persona_key in self.personas.keys():
                    persona_words = set(persona_key.split("_"))
                    overlap = len(words & persona_words)
                    if overlap > max_overlap:
                        max_overlap = overlap
                        closest_match = persona_key

                if closest_match and max_overlap >= 1:  # At least 1 word matches
                    persona = self.personas[closest_match].copy()
                    logger.warning(
                        f"Using closest matching persona folder: {closest_match} (from: {name})"
                    )
                else:
                    raise KeyError(
                        f"Persona '{name}' not found. Valid options: {', '.join(self.personas.keys())}"
                    )
        else:
            persona = random.choice(list(self.personas.values())).copy()
            logger.info(f"Randomly selected persona: {persona['name']}")

        # Only set default image if needed for display purposes
        if not persona.get("image"):
            persona["image"] = ""  # Empty string instead of default URL

        persona["prompt"] = persona["prompt"] + PERSONA_INTERACTION_INSTRUCTIONS
        # Add the path to the persona's directory using the normalized name
        persona_key = (
            name.lower().replace(" ", "_")
            if name
            else persona["name"].lower().replace(" ", "_")
        )
        persona["path"] = os.path.join(self.personas_dir, persona_key)
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


# Create global instance for easy access
persona_manager = PersonaManager()
