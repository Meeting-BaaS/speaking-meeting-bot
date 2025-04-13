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
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            def log_output(stream, prefix):
                for line in stream:
                    line = line.strip()
                    if line:
                        if "ERROR" in line:
                            logger.error(f"{prefix}: {line}")
                        elif "WARNING" in line:
                            logger.warning(f"{prefix}: {line}")
                        elif "SUCCESS" in line:
                            logger.success(f"{prefix}: {line}")
                        else:
                            logger.info(f"{prefix}: {line}")

            threading.Thread(
                target=log_output, args=(process.stdout, f"{name}"), daemon=True
            ).start()
            threading.Thread(
                target=log_output, args=(process.stderr, f"{name}"), daemon=True
            ).start()

            self.processes[name] = {"process": process, "command": command}
            return process
        except Exception as e:
            logger.error(f"Failed to start {name}: {e}")
            logger.error(
                "".join(traceback.format_exception(type(e), e, e.__traceback__))
            )
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
    ) -> None:
        """Main async function to run bots with direct parameters instead of command line args

        Args:
            count: Number of bot-proxy pairs to run
            meeting_url: Meeting URL to join
            websocket_url: WebSocket server URL
            personas: List of personas to use
            recorder_only: Whether to run only recorder bots
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

                logger.info(f"Starting bot {i} with command: {' '.join(bot_cmd)}")
                if not self.run_command(bot_cmd, bot_name):
                    logger.error(f"Failed to start {bot_name}")
                    await self.cleanup()
                    return

            # Monitor processes
            await self.monitor_processes()

        except KeyboardInterrupt:
            logger.warning("Keyboard interrupt detected")
            await self.cleanup()
        except Exception as e:
            logger.error(f"Error in main: {e}")
            logger.error(traceback.format_exc())
            await self.cleanup()

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
