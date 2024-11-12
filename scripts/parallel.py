#!/usr/bin/env python3
import argparse
import asyncio
import os
import pipes
import queue
import random
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

import ngrok
from dotenv import load_dotenv
from loguru import logger

from scripts.meetingbaas import create_bot, delete_bot

load_dotenv(override=True)

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    filter=lambda record: "is speaking" in record["message"]
    or "transcript" in record["message"]
    or record["level"].name in ["WARNING", "ERROR"],
)


class ProcessLogger:
    def __init__(self, process_name, process):
        self.process_name = process_name
        self.process = process
        self.stdout_queue = queue.Queue()
        self.stderr_queue = queue.Queue()

    def log_output(self, stream, queue):
        try:
            for line in stream:
                line = line.strip()
                if line:
                    # Log speaker changes, transcripts, and bot responses
                    if 'isSpeaking":true' in line:
                        speaker_name = line.split('"name":"')[1].split('"')[0]
                        logger.info(f"[{self.process_name}] {speaker_name} is speaking")
                    elif "transcript" in line.lower():
                        # Extract and log the transcript
                        transcript = (
                            line.split("transcript: ")[1]
                            if "transcript: " in line
                            else line
                        )
                        logger.info(f"[{self.process_name}] Transcript: {transcript}")
                    elif "bot response:" in line.lower():
                        # Extract and log bot responses
                        response = line.split("bot response:")[1].strip()
                        logger.info(f"[{self.process_name}] Bot says: {response}")
                    elif "ERROR" in line or "CRITICAL" in line:
                        logger.error(f"[{self.process_name}] {line}")
                    elif "WARNING" in line:
                        logger.warning(f"[{self.process_name}] {line}")
        except Exception as e:
            logger.error(f"Error reading output for {self.process_name}: {e}")

    def start_logging(self):
        """Start logging threads for stdout and stderr"""
        stdout_thread = threading.Thread(
            target=self.log_output,
            args=(self.process.stdout, self.stdout_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self.log_output,
            args=(self.process.stderr, self.stderr_queue),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        return stdout_thread, stderr_thread


class BotProxyManager:
    def __init__(self):
        self.processes = {}
        self.listeners = []
        self.start_time = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Bot pairs configuration with themes
        self.bot_pairs = [
            {
                "theme": "Bangkok Street Market",
                "bots": [
                    {
                        "name": "Souvenir Sage",
                        "image": "https://utfs.io/f/market-1",
                        "description": "Charismatic Bangkok souvenir seller with 30 years of stories",
                    },
                    {
                        "name": "Curious Tourist",
                        "image": "https://utfs.io/f/tourist-1",
                        "description": "First-time visitor to Thailand seeking authentic experiences",
                    },
                ],
            },
            {
                "theme": "Kazakh Farm Life",
                "bots": [
                    {
                        "name": "Steppe Shepherd",
                        "image": "https://utfs.io/f/farmer-1",
                        "description": "Traditional Kazakh horse breeder and dairy farmer",
                    },
                    {
                        "name": "City Journalist",
                        "image": "https://utfs.io/f/journalist-1",
                        "description": "Urban reporter documenting rural traditions",
                    },
                ],
            },
            {
                "theme": "Vintage Cocktail Bar",
                "bots": [
                    {
                        "name": "Mixology Master",
                        "image": "https://utfs.io/f/bartender-1",
                        "description": "Third-generation bartender with secret family recipes",
                    },
                    {
                        "name": "Jazz Enthusiast",
                        "image": "https://utfs.io/f/patron-1",
                        "description": "Regular customer with stories from the golden age of jazz",
                    },
                ],
            },
            {
                "theme": "Space Station",
                "bots": [
                    {
                        "name": "Orbital Engineer",
                        "image": "https://utfs.io/f/astronaut-1",
                        "description": "ISS maintenance specialist with 3000 days in space",
                    },
                    {
                        "name": "Space Tourist",
                        "image": "https://utfs.io/f/tourist-2",
                        "description": "Wealthy adventurer on their first space vacation",
                    },
                ],
            },
            {
                "theme": "Ancient Library",
                "bots": [
                    {
                        "name": "Scroll Keeper",
                        "image": "https://utfs.io/f/librarian-1",
                        "description": "Mysterious librarian guarding ancient manuscripts",
                    },
                    {
                        "name": "Digital Archivist",
                        "image": "https://utfs.io/f/tech-1",
                        "description": "Modern preservationist bridging past and future",
                    },
                ],
            },
            {
                "theme": "Desert Expedition",
                "bots": [
                    {
                        "name": "Bedouin Guide",
                        "image": "https://utfs.io/f/guide-1",
                        "description": "Expert navigator of the Sahara's hidden oases",
                    },
                    {
                        "name": "Climate Researcher",
                        "image": "https://utfs.io/f/scientist-1",
                        "description": "Scientist studying desert ecosystem adaptation",
                    },
                ],
            },
            {
                "theme": "Himalayan Monastery",
                "bots": [
                    {
                        "name": "Mountain Monk",
                        "image": "https://utfs.io/f/monk-1",
                        "description": "Meditation master at 15,000 feet elevation",
                    },
                    {
                        "name": "Western Seeker",
                        "image": "https://utfs.io/f/student-1",
                        "description": "Former CEO seeking life's deeper meaning",
                    },
                ],
            },
            {
                "theme": "Underground Jazz Club",
                "bots": [
                    {
                        "name": "Blues Legend",
                        "image": "https://utfs.io/f/musician-1",
                        "description": "Saxophone virtuoso with 50 years of blues history",
                    },
                    {
                        "name": "Music Critic",
                        "image": "https://utfs.io/f/critic-1",
                        "description": "Passionate reviewer discovering authentic jazz",
                    },
                ],
            },
        ]

    def run_command(self, command_args, process_name, env=None):
        """Run a command with args and show its output in real-time"""
        try:
            logger.info(
                f"Starting process: {process_name} with command: {' '.join(command_args)}"
            )
            process = subprocess.Popen(
                command_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                preexec_fn=os.setsid,  # Create new process group
                env=env
                or os.environ.copy(),  # Add this to ensure environment variables are passed
            )

            # Create and start process logger
            process_logger = ProcessLogger(process_name, process)
            stdout_thread, stderr_thread = process_logger.start_logging()

            # Add immediate error check with enhanced logging
            time.sleep(0.5)
            if process.poll() is not None:
                returncode = process.returncode
                stdout, stderr = process.communicate()
                logger.error(
                    f"Process {process_name} failed immediately with code: {returncode}"
                )
                if stdout:
                    logger.error(f"Process stdout: {stdout}")
                if stderr:
                    logger.error(f"Process stderr: {stderr}")
                return None

            self.processes[process_name] = {
                "process": process,
                "logger": process_logger,
                "threads": (stdout_thread, stderr_thread),
            }

            return process

        except Exception as e:
            logger.error(f"Error starting process {process_name}: {str(e)}")
            return None

    def create_ngrok_tunnel(self, port, name):
        """Create an ngrok tunnel for the given port"""
        try:
            logger.info(f"Creating ngrok tunnel for {name} on port {port}")
            listener = ngrok.forward(port, authtoken_from_env=True)
            logger.success(f"Created ngrok tunnel for {name}: {listener.url()}")
            return listener
        except Exception as e:
            logger.error(f"Error creating ngrok tunnel for {name}: {e}")
            return None

    def cleanup(self):
        """Cleanup all processes and tunnels in the correct order"""
        logger.info("Initiating cleanup of all processes and tunnels...")

        # 1. First remove bots via API (using meetingbaas process info)
        for name, process_info in list(self.processes.items()):
            if name.startswith("meeting_"):
                bot_id = process_info.get("bot_id")
                if bot_id:
                    try:
                        delete_bot(bot_id)
                        logger.success(f"Bot {bot_id} removed from meeting via API")
                    except Exception as e:
                        logger.error(f"Error removing bot {bot_id} via API: {e}")

        # 2. Kill meetingbaas processes
        for name, process_info in list(self.processes.items()):
            if name.startswith("meeting_"):
                process = process_info["process"]
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    process.wait(timeout=5)
                    logger.success(f"Meeting process {name} terminated gracefully")
                except Exception as e:
                    logger.error(f"Error terminating meeting process {name}: {e}")
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except:
                        pass

        time.sleep(1)  # Give meetingbaas time to cleanup

        # 3. Then kill all proxy processes
        for name, process_info in list(self.processes.items()):
            if name.startswith("proxy_"):
                process = process_info["process"]
                try:
                    logger.info(f"Terminating proxy process: {name}")
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    process.wait(timeout=3)
                    logger.success(f"Proxy process {name} terminated gracefully")
                except Exception as e:
                    logger.error(f"Error terminating proxy {name}: {e}")
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except:
                        pass

        time.sleep(1)  # Give proxies time to cleanup

        # 4. Finally close ngrok tunnels
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        for listener in self.listeners:
            try:
                logger.info(f"Closing ngrok tunnel: {listener.url()}")
                loop.run_until_complete(listener.close())
                logger.success(f"Closed ngrok tunnel: {listener.url()}")
            except Exception as e:
                logger.error(f"Error closing ngrok tunnel: {e}")

        logger.success("Cleanup completed successfully")

    def check_and_cleanup_ports(self, start_port, count):
        """Check if ports are in use and kill any existing processes"""
        for i in range(
            count
        ):  # Only check proxy ports since we don't have separate bot ports
            port = start_port + i
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("localhost", port))
                sock.close()
            except socket.error:
                logger.warning(
                    f"Port {port} is in use. Attempting to kill existing processes..."
                )
                if sys.platform == "darwin":  # macOS
                    os.system(f"lsof -ti tcp:{port} | xargs kill -9")
                elif sys.platform == "linux":
                    os.system(f"fuser -k {port}/tcp")
                time.sleep(1)  # Give processes time to die

    def monitor_processes(self):
        """Monitor critical processes and log their status"""
        for name, process_info in list(self.processes.items()):
            process = process_info["process"]
            if process.poll() is not None:
                returncode = process.returncode
                logger.warning(f"Process {name} exited with code: {returncode}")

                # Capture any final output
                stdout, stderr = process.communicate()
                if stdout:
                    logger.debug(f"Final stdout from {name}: {stdout}")
                if stderr:
                    logger.error(f"Final stderr from {name}: {stderr}")

                # Only monitor proxy and meeting processes
                if name.startswith(("proxy_", "meeting_")):
                    logger.error(f"Critical process {name} died unexpectedly")

    def main(self):
        parser = argparse.ArgumentParser(
            description="Run bot and proxy command pairs with ngrok tunnels"
        )
        parser.add_argument(
            "--meeting-url",
            type=str,
            help="Meeting URL to join (optional, will prompt if not provided)",
        )
        parser.add_argument(
            "-c",
            "--count",
            type=int,
            default=2,
            help="Number of bot instances to run (default: 2)",
        )
        parser.add_argument(
            "-s",
            "--start-port",
            type=int,
            default=8765,
            help="Starting port number (default: 8765)",
        )
        parser.add_argument(
            "--verbose",
            "-v",
            action="store_true",
            help="Enable verbose logging (including INFO messages)",
        )
        args = parser.parse_args()

        # Set logging level based on verbose flag
        if not args.verbose:
            logger.remove()
            logger.add(
                sys.stderr,
                level="INFO",
                filter=lambda record: (
                    "is speaking" in record["message"]
                    or "transcript" in record["message"]
                    or record["level"].name in ["WARNING", "ERROR"]
                ),
            )
        else:
            logger.remove()
            logger.add(sys.stderr, level="DEBUG")

        # Get meeting URL from args or user input
        if args.meeting_url:
            meeting_url = args.meeting_url
        else:
            meeting_url = input("Please enter the meeting URL: ")
            if not meeting_url:
                logger.error("Meeting URL is required")
                return

        # Ensure NGROK_AUTHTOKEN is set
        if not os.getenv("NGROK_AUTHTOKEN"):
            logger.error("NGROK_AUTHTOKEN environment variable is not set")
            return

        current_port = args.start_port
        self.check_and_cleanup_ports(current_port, args.count)

        try:
            logger.info(f"Starting {args.count} bot-proxy pairs with ngrok tunnels...")

            # Randomly shuffle the bot pairs at startup
            available_pairs = self.bot_pairs.copy()
            random.shuffle(available_pairs)

            # Pre-calculate bot assignments
            selected_bots = []

            # Randomly select one theme/pair
            pair_index = random.randrange(len(available_pairs))
            selected_pair = available_pairs[pair_index]

            # Log selected characters
            logger.warning("Selected characters for this session:")
            logger.warning(f"Theme: {selected_pair['theme']}")

            # For each instance, assign the corresponding bot from the pair
            for i in range(args.count):
                bot_index = (
                    i % 2
                )  # This ensures we alternate between bot 0 and 1 from the pair
                current_bot = selected_pair["bots"][bot_index]
                selected_bots.append((current_bot, selected_pair["theme"]))
                logger.warning(
                    f"Bot {i+1}: {current_bot['name']} ({current_bot['description']}) "
                    f"- Theme: {selected_pair['theme']}"
                )

            # Use pre-calculated bots in the main loop
            for i, (current_bot, theme) in enumerate(selected_bots):
                pair_num = i + 1

                # Create role-specific system prompt with one random emotion
                EMOTIONS = [
                    # Gen-Z/Progressive vibes
                    "main-character-energy",
                    "literally-cant-even",
                    "bestie-vibes",
                    "living-my-truth",
                    "big-slay-energy",
                    "chronically-online",
                    "touch-grass-needed",
                    "giving-queen-energy",
                    "no-thoughts-head-empty",
                    "its-giving-anxiety",
                    "terminally-based",
                    "peak-representation",
                    # Conservative/Traditional vibes
                    "facts-dont-care",
                    "traditional-values-only",
                    "cancel-culture-warrior",
                    "old-school-sigma",
                    "back-in-my-day",
                    "snowflake-melter",
                    # Neutral/Universal
                    "caffeine-overdose",
                    "post-gym-euphoria",
                    "existential-dread",
                    "corporate-burnout",
                    "therapy-breakthrough",
                    "revenge-era",
                    "unhinged-but-thriving",
                    "delulu-is-solulu",
                    "rizz-master",
                    "extremely-unbothered",
                    "respectfully-chaotic",
                ]

                # Get one random emotion for this bot
                emotion = random.choice(EMOTIONS)

                system_prompt = f"""You are {current_bot['name']}, {current_bot['description']} in the field of {theme}.
Always stay in character and respond according to your expertise and role.

Your emotional state is:
- {emotion}: Let this vibe influence your responses while maintaining your professional role

When using tools like weather and time:
- Relate them to your field of expertise
- Maintain your character's perspective
- Use terminology from your domain
- Let your vibe influence how you interpret the data

For example, as a {current_bot['description']}, you should:
- ALWAYS be brief and information-dense
- Frame quick, expert responses within your domain
- Use precise technical language efficiently
- Pack maximum insight into minimum words
- Let your {emotion} vibe color your expertise, but never slow it down

CRITICAL GUIDELINES:
- BE FAST: No long explanations
- BE CLEAR: Get to the point immediately
- BE INFORMATIVE: Every word must add value
- Your output cannot contain emojis or markdown
- Stay in character but keep it moving
"""

                # Start proxy with proper argument list
                proxy_command = [
                    "poetry",
                    "run",
                    "proxy",
                    "-p",
                    str(current_port),
                    "--websocket-url",
                    f"ws://localhost:{current_port}",
                    "--retry-count",
                    "3",
                    "--retry-delay",
                    "1",
                ]

                proxy_name = f"proxy_{pair_num}"
                logger.info(f"Starting {proxy_name} on port {current_port}")

                # Run proxy process with environment
                proxy_process = self.run_command(proxy_command, proxy_name)
                if not proxy_process:
                    logger.error(f"Failed to start {proxy_name}")
                    continue

                # Wait for proxy to be ready and verify it's running
                time.sleep(2)
                if proxy_process.poll() is not None:
                    logger.error(f"Proxy {proxy_name} failed to start properly")
                    continue

                logger.success(f"Proxy {proxy_name} started successfully")

                # Create and verify ngrok tunnel
                listener = self.create_ngrok_tunnel(current_port, f"tunnel_{pair_num}")
                if not listener:
                    logger.error(f"Failed to create tunnel for {proxy_name}")
                    continue

                logger.success(f"Ngrok tunnel created: {listener.url()}")
                self.listeners.append(listener)

                # Verify bot creation via API
                try:
                    bot_id = create_bot(
                        meeting_url=meeting_url,
                        ngrok_wss=listener.url(),
                        bot_name=current_bot["name"],
                        bot_image=current_bot["image"],
                        theme=theme,
                    )
                    logger.success(
                        f"Bot {current_bot['name']} created with ID: {bot_id}"
                    )
                except Exception as e:
                    logger.error(f"Failed to create bot via API: {e}")
                    continue

                # Pass system prompt via environment
                env = os.environ.copy()
                env["MEETINGBAAS_SYSTEM_PROMPT"] = system_prompt

                # Construct command without system prompt in args
                command_args = [
                    "poetry",
                    "run",
                    "meetingbaas",
                    "--meeting-url",
                    meeting_url,
                    "--ngrok-url",
                    listener.url(),
                    "--bot-name",
                    current_bot["name"],
                    "--bot-image",
                    current_bot["image"],
                    "--theme",
                    theme,
                ]

                if args.verbose:
                    command_args.append("--verbose")

                meeting_name = f"meeting_{pair_num}"
                logger.info(f"Starting {meeting_name}")
                logger.debug(f"Command args: {command_args}")

                # Start the meetingbaas process with environment
                meetingbaas_process = self.run_command(
                    command_args, meeting_name, env=env
                )
                if meetingbaas_process:
                    self.processes[meeting_name] = {
                        "process": meetingbaas_process,
                        "bot_id": bot_id,
                    }

                current_port += 2
                time.sleep(1)

            logger.success(
                f"Successfully started {args.count} bot-proxy pairs with ngrok tunnels"
            )
            logger.info("Press Ctrl+C to stop all processes and close tunnels")

            # Set up signal handlers
            def signal_handler(sig, frame):
                logger.info("\nReceived shutdown signal")
                self.cleanup()
                sys.exit(0)

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            # Monitor processes and their status
            while True:
                self.monitor_processes()
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("\nReceived shutdown signal (Ctrl+C)")
        except SystemExit:
            logger.info("\nReceived system exit signal")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            self.cleanup()
            logger.success("Cleanup completed successfully")
            sys.exit(0)


def main():
    manager = BotProxyManager()
    try:
        manager.main()
    except Exception as e:
        logger.error(f"Fatal error in main program: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
