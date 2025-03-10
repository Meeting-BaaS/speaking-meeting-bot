import argparse
import os
import signal
import sys
import time
import uuid
import traceback

import requests
from dotenv import load_dotenv
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")

from config.persona_utils import persona_manager
from meetingbaas_pipecat.utils.logger import configure_logger

logger = configure_logger()

# Load environment variables from .env file
load_dotenv()
API_KEY = os.getenv("MEETING_BAAS_API_KEY")
API_URL = os.getenv("MEETING_BAAS_API_URL", "https://api.meetingbaas.com")

if not API_KEY:
    logger.error("MEETING_BAAS_API_KEY not found in environment variables")
    exit(1)

if not API_URL:
    logger.warning("MEETING_BAAS_API_URL not found, using default: https://api.meetingbaas.com")


def validate_url(url):
    """Validates the URL format, ensuring it starts with https://"""
    if not url.startswith("https://"):
        raise ValueError("URL must start with https://")
    return url


def get_user_input(prompt, validator=None):
    while True:
        user_input = input(prompt).strip()
        if validator:
            try:
                return validator(user_input)
            except ValueError as e:
                logger.warning(f"Invalid input received: {e}")
        else:
            return user_input


def get_persona_selection():
    """Prompts user to select a persona from available options"""
    available_personas = persona_manager.list_personas()
    logger.info("\nAvailable personas:")
    for persona_key in available_personas:
        persona = persona_manager.get_persona(persona_key)
        logger.info(f"{persona_key}: {persona['name']}")

    logger.info("\nPress Enter for random selection or type persona name:")

    while True:
        try:
            choice = (
                input("\nSelect a persona (enter name or press Enter for random): ")
                .strip()
                .lower()
            )
            if not choice:  # Empty input
                return None
            if choice in available_personas:
                return choice
            logger.warning("Invalid selection. Please try again.")
        except ValueError:
            logger.warning("Please enter a valid persona name.")


def get_baas_bot_dedup_key(character_name: str, is_recorder_only: bool, suffix="") -> str:
    """Generate a unique deduplication key for a bot.
    
    Args:
        character_name: The persona name to use
        is_recorder_only: Whether this is a recorder-only bot
        suffix: Optional suffix to make the key unique (e.g., for multiple bots in same meeting)
        
    Returns:
        A unique deduplication key string
    """
    if is_recorder_only:
        # Generate a random UUID for recorder bots
        return f"BaaS-Recorder-{uuid.uuid4().hex[:8]}"
    
    # For speaking bots, include the suffix if provided
    if suffix:
        return f"{character_name}-BaaS-{suffix}"
    return f"{character_name}-BaaS"


def create_baas_bot(meeting_url, ngrok_url, persona_name=None, recorder_only=False, dedup_suffix=""):
    if recorder_only:
        config = {
            "meeting_url": meeting_url,
            "bot_name": "BaaS Meeting Recorder",
            "recording_mode": "speaker_view",
            "bot_image": "https://i0.wp.com/fishingbooker-prod-blog-backup.s3.amazonaws.com/blog/media/2019/06/14152536/Largemouth-Bass-1024x683.jpg",
            "entry_message": "I will only record this meeting to check the quality of the data recorded by MeetingBaas API through this meeting bot. To learn more about Meeting Baas, visit meetingbaas.com. Data recorded in this meeting will not be used for any other purpose than this quality check, in accordance with MeetingBaas's privacy policy, https://meetingbaas.com/privacy.",
            "reserved": False,
            "speech_to_text": {"provider": "Default"},
            "automatic_leave": {"waiting_room_timeout": 600},
            "deduplication_key": get_baas_bot_dedup_key(persona_name, recorder_only, dedup_suffix),
            "extra": {
                "deduplication_key": get_baas_bot_dedup_key(persona_name, recorder_only, dedup_suffix)
            },
            # "webhook_url": "",
        }
    else:
        # Get persona details for the bot
        if persona_name:
            try:
                persona = persona_manager.get_persona(persona_name)
                bot_name = persona["name"]
                entry_message = persona.get("entry_message", "")
                bot_image = persona.get("image", "")
            except Exception as e:
                logger.error(f"Error loading persona data: {e}")
                bot_name = persona_name
                entry_message = ""
                bot_image = ""
        else:
            bot_name = "MeetingBaas Bot"
            entry_message = ""
            bot_image = ""

        config = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_mode": "speaker_view",
            "reserved": False,
            "automatic_leave": {"waiting_room_timeout": 600},
            "deduplication_key": get_baas_bot_dedup_key(persona_name, recorder_only, dedup_suffix),
            "streaming": {
                "input": ngrok_url,
                "output": ngrok_url,
            },
            "extra": {
                "deduplication_key": get_baas_bot_dedup_key(persona_name, recorder_only, dedup_suffix)
            },
            # "webhook_url": "https://webhook-test.com/ce63096bd2c0f2793363fd3fb32bc066",
        }

        if bot_image:
            config["bot_image"] = bot_image
        if entry_message:
            config["entry_message"] = entry_message

    url = f"{API_URL}/bots"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": API_KEY,
    }

    logger.warning(f"Sending bot config to MeetingBaas API: {config}")

    response = requests.post(url, json=config, headers=headers)
    if response.status_code == 200:
        bot_id = response.json().get("bot_id")
        logger.success(f"Bot created successfully with ID: {bot_id}")
        return bot_id
    else:
        error_msg = f"Failed to create bot: {response.json()}"
        logger.error(error_msg)
        raise Exception(error_msg)


def delete_bot(bot_id):
    delete_url = f"{API_URL}/bots/{bot_id}"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": API_KEY,
    }

    logger.info(f"Attempting to delete bot with ID: {bot_id}")
    response = requests.delete(delete_url, headers=headers)

    if response.status_code != 200:
        error_msg = f"Failed to delete bot: {response.json()}"
        logger.error(error_msg)
        raise Exception(error_msg)
    else:
        logger.success(f"Bot {bot_id} deleted successfully")


class BotManager:
    def __init__(self, args):
        """Initialize the BotManager with the provided arguments.
        
        Args:
            args: The command-line arguments.
        """
        self.args = args
        self.current_bot_id = None
        logger.info(f"BotManager initialized with args: {args}")
        
    def run(self):
        """Main method to run the bot creation and management process."""
        logger.info("Starting BotManager")
        try:
            self.create_and_manage_bot()
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, cleaning up...")
        except Exception as e:
            logger.error(f"An error occurred during bot management: {e}")
            logger.error(traceback.format_exc())
        
    def create_and_manage_bot(self):
        """Create a bot and manage its lifecycle."""
        try:
            # Get config parameters from CLI args
            meeting_url = self.args.meeting_url
            ngrok_url = self.args.ngrok_url
            persona_name = self.args.persona_name
            recorder_only = self.args.recorder_only
            
            # Get custom config if provided
            custom_config = self.args.config
            dedup_suffix = ""
            
            # Check if we have a custom config with a deduplication suffix
            if hasattr(self.args, 'dedup_suffix') and self.args.dedup_suffix:
                dedup_suffix = self.args.dedup_suffix
            
            # Create bot
            self.current_bot_id = create_baas_bot(
                meeting_url,
                ngrok_url,
                persona_name,
                recorder_only,
                dedup_suffix
            )

            logger.warning(f"Bot name: {persona_name}")

            logger.info("\nOptions:")
            logger.info("- Press Enter to respawn bot with same URLs")
            logger.info("- Enter 'n' to input new URLs")
            logger.info("- Enter 'p' to select a new persona")
            logger.info("- Press Ctrl+C to exit")

            user_choice = input().strip().lower()
            logger.debug(f"User selected option: {user_choice}")

            self.delete_current_bot()

            if user_choice == "n":
                logger.warning("User requested new URLs")
                self.args.meeting_url = None
                self.args.ngrok_url = None
            elif user_choice == "p":
                logger.warning("User requested new persona")
                self.args.persona_name = None
        except Exception as e:
            logger.exception(f"An error occurred during bot creation: {e}")
            logger.error(traceback.format_exc())

    def delete_current_bot(self):
        if self.current_bot_id:
            try:
                delete_bot(self.current_bot_id)
            except Exception as e:
                logger.exception(f"Error deleting bot: {e}")
            finally:
                self.current_bot_id = None


def main():
    parser = argparse.ArgumentParser(description="Meeting BaaS Bot")
    parser.add_argument(
        "--meeting-url", help="The meeting URL (must start with https://)"
    )
    parser.add_argument("--ngrok-url", help="The ngrok URL (must start with https://)")
    parser.add_argument(
        "--persona-name",
        help="The name of the persona to use (e.g., 'interviewer', 'pair_programmer')",
    )
    parser.add_argument(
        "--recorder-only",
        action="store_true",
        help="Run as recording-only bot",
    )
    parser.add_argument(
        "--config", type=str, help="JSON configuration for recorder bot"
    )
    parser.add_argument(
        "--dedup-suffix", type=str, help="Deduplication suffix for the bot"
    )

    args = parser.parse_args()
    logger.info("Starting application with arguments: {}", args)

    bot_manager = BotManager(args)
    bot_manager.run()


if __name__ == "__main__":
    main()
