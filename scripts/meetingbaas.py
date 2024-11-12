import argparse
import os
import signal
import time
import uuid

import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
API_KEY = os.getenv("MEETING_BAAS_API_KEY")


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
                print(f"Invalid input: {e}")
        else:
            return user_input


def create_bot(
    meeting_url,
    ngrok_wss,
    bot_name,
    bot_image,
    theme,
    proxy_port,
    websocket_port,
):
    # Convert https:// to ws:// for local development
    if ngrok_wss.startswith("https://"):
        ngrok_wss = "ws://" + ngrok_wss[8:]

    url = "https://api.meetingbaas.com/bots"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": API_KEY,
    }

    # Create deduplication key using bot name and meeting URL
    deduplication_key = (
        f"{bot_name}-{str(proxy_port)}-{str(websocket_port)}-{meeting_url}"
    )

    # Add streaming configuration with required ports
    streaming_config = {
        "input": f"ws://localhost:{proxy_port}",
        "output": f"ws://localhost:{websocket_port}",
    }

    # Add extra field with bot metadata and port info
    extra = {
        "theme": theme,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
        "deduplication_key": deduplication_key,
        "ngrok_url": ngrok_wss,
        "ports": {"proxy": proxy_port, "websocket": websocket_port},
    }

    config = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_mode": "speaker_view",
        "bot_image": bot_image,
        "entry_message": f"Hi, I'm {bot_name}! Created with https://meetingbaas.com - I'm ready to chat about {theme}!",
        "reserved": True,
        "speech_to_text": {"provider": "Default"},
        "automatic_leave": {"waiting_room_timeout": 600},
        "deduplication_key": deduplication_key,
        "streaming": streaming_config,
        "extra": extra,
    }

    response = requests.post(url, json=config, headers=headers)
    if response.status_code == 200:
        return response.json().get("bot_id")
    else:
        raise Exception(f"Failed to create bot: {response.json()}")


def delete_bot(bot_id):
    delete_url = f"https://api.meetingbaas.com/bots/{bot_id}"
    headers = {
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": API_KEY,
    }
    response = requests.delete(delete_url, headers=headers)

    if response.status_code != 200:
        raise Exception(f"Failed to delete bot: {response.json()}")


class BotManager:
    def __init__(self, args):
        self.args = args
        self.current_bot_id = None

    def run(self):
        signal.signal(signal.SIGINT, self.signal_handler)

        try:
            self.get_or_update_urls()
            self.create_and_manage_bot()

            # Keep process alive without restarting
            while True:
                time.sleep(1)

        except Exception as e:
            if "AlreadyStarted" in str(e):
                print(f"Bot already exists in meeting, keeping process alive...")
                # Keep the process alive even if bot exists
                while True:
                    time.sleep(1)
            else:
                print(f"An error occurred: {e}")
                if self.current_bot_id:
                    self.delete_current_bot()

    def get_or_update_urls(self):
        if not self.args.meeting_url:
            self.args.meeting_url = get_user_input(
                "Enter the meeting URL (must start with https://): ", validate_url
            )
        if not self.args.ngrok_url:
            self.args.ngrok_url = get_user_input(
                "Enter the ngrok URL (must start with https://): ", validate_url
            )
        self.args.ngrok_wss = "wss://" + self.args.ngrok_url[8:]

    def create_and_manage_bot(self):
        # If we already have a bot_id from parallel.py, don't create a new one
        if not self.current_bot_id:
            self.current_bot_id = create_bot(
                self.args.meeting_url,
                self.args.ngrok_wss,
                self.args.bot_name,
                self.args.bot_image,
                self.args.theme,
                self.args.proxy_port,
                self.args.websocket_port,
            )
            print(f"Bot created successfully with bot_id: {self.current_bot_id}")

            # Only show interactive prompts when not running in parallel mode
            if not self.args.bot_id:  # bot_id presence indicates parallel mode
                print("\nPress Enter to respawn bot with same URLs")
                print("Enter 'n' to input new URLs")
                print("Press Ctrl+C to exit")
                user_choice = input().strip().lower()

                self.delete_current_bot()

                if user_choice == "n":
                    self.args.meeting_url = None
                    self.args.ngrok_url = None

    def delete_current_bot(self):
        if self.current_bot_id:
            try:
                delete_bot(self.current_bot_id)
                print(f"Bot with bot_id {self.current_bot_id} deleted successfully.")
            except Exception as e:
                print(f"Error deleting bot: {e}")
            finally:
                self.current_bot_id = None

    def signal_handler(self, signum, frame):
        print("\nCtrl+C detected. Cleaning up...")
        self.delete_current_bot()
        print("Bot cleaned up. Exiting...")
        exit(0)


def main():
    parser = argparse.ArgumentParser(description="Meeting BaaS Bot")
    parser.add_argument(
        "--meeting-url", help="The meeting URL (must start with https://)"
    )
    parser.add_argument("--ngrok-url", help="The ngrok URL (must start with https://)")
    parser.add_argument(
        "--bot-name",
        default="Speaking MeetingBaas Bot",
        help="The name of the bot which is going to join the meeting.",
    )
    parser.add_argument(
        "--bot-image",
        default="https://utfs.io/f/animal-1",
        help="The image of the bot which is going to join the meeting.",
    )
    parser.add_argument(
        "--system-prompt",
        help="System prompt for the bot's personality",
        default=os.getenv("MEETINGBAAS_SYSTEM_PROMPT", "You are a helpful assistant."),
    )
    parser.add_argument(
        "--theme",
        default="general conversation",
        help="The theme or topic the bot specializes in",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--bot-id", help="Existing bot ID to use")
    parser.add_argument("--proxy-port", help="Proxy port for incoming connections")
    parser.add_argument(
        "--websocket-port", help="Websocket port for outgoing connections"
    )

    args = parser.parse_args()
    bot_manager = BotManager(args)
    bot_manager.run()


if __name__ == "__main__":
    main()

# Example usage:
# python meeting_baas_bot.py
# or
# python meeting_baas_bot.py --meeting-url https://example.com/meeting --ngrok-url https://example.ngrok.io
