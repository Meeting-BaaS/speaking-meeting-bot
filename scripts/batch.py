#!/usr/bin/env python3
import argparse
import asyncio
import os
import queue
import random
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from config.persona_utils import PersonaManager
from meetingbaas_pipecat.utils.logger import configure_logger

load_dotenv(override=True)

logger = configure_logger()


def validate_url(url):
    """Validates the URL format, ensuring it starts with https:// or ws://"""
    if not (url.startswith("https://") or url.startswith("ws://")):
        raise ValueError("URL must start with https:// or ws://")
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


def get_consecutive_personas(persona_options):
    if len(persona_options) < 2:
        raise ValueError("Need at least two personas to pick consecutive items.")

    # Ensure we're working with folder names
    folder_names = [name.lower().replace(" ", "_") for name in persona_options]

    # Choose a random start index that allows for two consecutive items
    start_index = random.randint(0, len(folder_names) - 2)
    return folder_names[start_index : start_index + 2]


class ProcessLogger:
    def __init__(self, process_name: str, process: subprocess.Popen):
        self.process_name = process_name
        self.process = process
        self.stdout_queue: queue.Queue = queue.Queue()
        self.stderr_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self.logger = configure_logger()

    def log_output(self, pipe, queue: queue.Queue, is_error: bool = False) -> None:
        """Log output from a pipe to a queue and logger"""
        try:
            for line in iter(pipe.readline, ""):
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if line:
                    queue.put(line)
                    log_msg = f"[{self.process_name}] {line}"
                    if is_error:
                        self.logger.error(log_msg)
                    else:
                        self.logger.info(log_msg)
        finally:
            pipe.close()

    def start_logging(self) -> Tuple[threading.Thread, threading.Thread]:
        """Start logging threads for stdout and stderr"""
        stdout_thread = threading.Thread(
            target=self.log_output,
            args=(self.process.stdout, self.stdout_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self.log_output,
            args=(self.process.stderr, self.stderr_queue, True),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        return stdout_thread, stderr_thread

    def stop(self) -> None:
        """Stop the logging threads gracefully"""
        self._stop_event.set()


class BotProxyManager:
    def __init__(self):
        self.processes: Dict = {}
        self.start_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.shutdown_event = asyncio.Event()
        self.initial_args = None
        self.selected_persona_names = []

    def run_command(self, command: List[str], name: str) -> Optional[subprocess.Popen]:
        """Run a command and store the process"""
        try:
            logger.info(f"Running command: {' '.join(command)}")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            self.processes[name] = {"process": process, "start_time": time.time()}

            def log_output(stream, prefix):
                """Log the output from the stream with the given prefix"""
                for line in stream:
                    line = line.strip()
                    if line:
                        # Check for bot ID in the output
                        if "BOT_ID:" in line:
                            try:
                                bot_id = line.split("BOT_ID:")[1].strip()
                                logger.info(f"Captured bot ID for {name}: {bot_id}")
                                self.processes[name]["bot_id"] = bot_id
                            except Exception as e:
                                logger.error(f"Error parsing bot ID: {e}")

                        if "ERROR" in line:
                            logger.error(f"{prefix}: {line}")
                        elif "WARNING" in line:
                            logger.warning(f"{prefix}: {line}")
                        elif "SUCCESS" in line or "INFO" in line:
                            logger.info(f"{prefix}: {line}")
                        else:
                            logger.info(f"{prefix}: {line}")

            threading.Thread(
                target=log_output, args=(process.stdout, f"{name}"), daemon=True
            ).start()
            threading.Thread(
                target=log_output, args=(process.stderr, f"{name}_err"), daemon=True
            ).start()

            logger.success(f"Successfully started process: {name}")
            return process
        except Exception as e:
            logger.error(f"Error running command {' '.join(command)}: {e}")
            return None

    async def cleanup(self):
        """Cleanup all processes"""
        try:
            process_names = list(self.processes.keys())
            process_names.reverse()

            for name in process_names:
                process_info = self.processes[name]
                logger.info(f"Terminating process: {name}")
                process = process_info["process"]
                try:
                    process.terminate()
                    await asyncio.sleep(1)
                    if process.poll() is None:
                        process.kill()
                    logger.success(f"Process {name} terminated successfully")
                except Exception as e:
                    logger.error(f"Error terminating process {name}: {e}")

            self.processes.clear()
            logger.success("Cleanup completed successfully")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def signal_handler(self, signum, frame):
        logger.warning("Ctrl+C detected, initiating cleanup...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.cleanup())
        finally:
            loop.close()
        logger.success("Cleanup completed")
        logger.info("Exiting...")
        sys.exit(0)

    async def monitor_processes(self) -> None:
        """Monitor running processes and handle failures"""
        while not self.shutdown_event.is_set():
            try:
                for name, process_info in list(self.processes.items()):
                    process = process_info["process"]
                    if process.poll() is not None:
                        logger.warning(
                            f"Process {name} exited with code: {process.returncode}"
                        )
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error monitoring processes: {e}")

    async def async_main(
        self,
        count: int = None,
        meeting_url: str = None,
        websocket_url: str = "ws://localhost:8000",
        personas: List[str] = None,
        recorder_only: bool = False,
        meeting_baas_api_key: str = None,
        return_bot_id: bool = False,
    ) -> Optional[str]:
        """Main async function to run bots with direct parameters instead of command line args

        Args:
            count: Number of bot-proxy pairs to run
            meeting_url: Meeting URL to join
            websocket_url: WebSocket server URL
            personas: List of personas to use
            recorder_only: Whether to run only recorder bots
            meeting_baas_api_key: API key for MeetingBaas
            return_bot_id: Whether to return the bot ID (for API integration)

        Returns:
            The created bot ID if return_bot_id is True, otherwise None
        """
        # For backward compatibility with command line usage
        if count is None or meeting_url is None:
            parser = argparse.ArgumentParser(description="Run multiple bot-proxy pairs")
            parser.add_argument(
                "-c", "--count", type=int, help="Number of bot-proxy pairs to run"
            )
            parser.add_argument("--meeting-url", type=str, help="Meeting URL")
            parser.add_argument(
                "--websocket-url",
                type=str,
                default="ws://localhost:8000",
                help="WebSocket server URL",
            )
            parser.add_argument("--personas", nargs="+", help="List of personas to use")
            parser.add_argument(
                "--recorder-only", action="store_true", help="Run only recorder bots"
            )

            args = parser.parse_args()
            self.initial_args = args

            count = count or args.count
            meeting_url = meeting_url or args.meeting_url
            websocket_url = websocket_url or args.websocket_url
            personas = personas or args.personas
            recorder_only = recorder_only or args.recorder_only

        if not count:
            count = int(get_user_input("Enter number of bot-proxy pairs to run: "))

        if not meeting_url:
            meeting_url = get_user_input(
                "Enter meeting URL (must start with https://): ", validate_url
            )

        # Initialize persona manager
        persona_manager = PersonaManager()
        persona_options = persona_manager.list_personas()

        if not personas:
            if len(persona_options) >= 2:
                self.selected_persona_names = get_consecutive_personas(persona_options)
            else:
                logger.warning("Not enough personas available, using default behavior")
                self.selected_persona_names = []
        else:
            self.selected_persona_names = personas

        # Variable to store bot ID for return
        created_bot_id = None

        try:
            # Start bot processes
            for i in range(count):
                bot_name = f"bot_{i}"
                bot_cmd = [
                    "python3",
                    "-m",
                    "meetingbaas_pipecat.bot.bot",
                    "--meeting-url",
                    meeting_url,
                    "--websocket-url",
                    websocket_url,
                    "--bot-id",
                    str(i),
                ]

                if recorder_only:
                    bot_cmd.append("--recorder-only")

                if self.selected_persona_names:
                    persona = self.selected_persona_names[
                        i % len(self.selected_persona_names)
                    ]
                    bot_cmd.extend(["--persona", persona])

                if meeting_baas_api_key:
                    bot_cmd.extend(["--meeting-baas-api-key", meeting_baas_api_key])

                # Add flag to output the bot ID to stdout
                if return_bot_id:
                    bot_cmd.extend(["--output-bot-id"])

                logger.info(f"Starting bot {i} with command: {' '.join(bot_cmd)}")
                process = self.run_command(bot_cmd, bot_name)

                if not process:
                    logger.error(f"Failed to start {bot_name}")
                    if return_bot_id:
                        return None
                    await self.cleanup()
                    return None

                # If we want to return the bot ID, wait for it to appear in stdout
                if return_bot_id:
                    # Create a future that will be completed when the bot ID is found
                    bot_id_future = asyncio.Future()

                    # Store the future in the process info so log_output can complete it
                    self.processes[bot_name]["bot_id_future"] = bot_id_future

                    try:
                        # Wait for the future to be completed with a timeout
                        created_bot_id = await asyncio.wait_for(bot_id_future, 10.0)
                        logger.info(f"Received bot ID: {created_bot_id}")

                        if return_bot_id:
                            return created_bot_id
                    except asyncio.TimeoutError:
                        logger.warning(f"Timed out waiting for bot ID from {bot_name}")

                    # If we're still here and there's no bot ID, try to get it from the process info
                    if created_bot_id is None and "bot_id" in self.processes[bot_name]:
                        created_bot_id = self.processes[bot_name]["bot_id"]
                        if created_bot_id and return_bot_id:
                            return created_bot_id

            # If not waiting for a bot ID or if we already found it, start monitoring
            if not return_bot_id:
                # Monitor processes
                await self.monitor_processes()

        except KeyboardInterrupt:
            logger.warning("Keyboard interrupt detected")
            await self.cleanup()
        except Exception as e:
            logger.error(f"Error in main: {e}")
            logger.error(traceback.format_exc())
            await self.cleanup()

        # Return the bot ID if requested and found
        if return_bot_id:
            return created_bot_id
        return None

    def main(
        self,
        count: int = None,
        meeting_url: str = None,
        websocket_url: str = "ws://localhost:8000",
        personas: List[str] = None,
        recorder_only: bool = False,
    ) -> None:
        """Synchronous entry point with direct parameters"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:

            def signal_handler():
                self.signal_handler(None, None)

            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

            loop.run_until_complete(
                self.async_main(
                    count=count,
                    meeting_url=meeting_url,
                    websocket_url=websocket_url,
                    personas=personas,
                    recorder_only=recorder_only,
                )
            )
        finally:
            loop.close()


if __name__ == "__main__":
    manager = BotProxyManager()
    # Call main without parameters to use command-line argument parsing
    manager.main()
