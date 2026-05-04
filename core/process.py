"""Process management for Pipecat processes."""

import os
import subprocess
import sys
import time
from typing import Any, Dict
import json
import threading

from meetingbaas_pipecat.utils.logger import logger

PIPECAT_PROCESSES: Dict[str, subprocess.Popen] = {}

def stream_output(pipe, prefix):
    for line in iter(pipe.readline, ''):
        print(f"{prefix} {line.strip()}")

def start_pipecat_process(
    client_id: str,
    websocket_url: str,
    meeting_url: str,
    persona_data: Dict[str, Any],
    streaming_audio_frequency: str,
    enable_tools: bool,
    api_key: str = "",
    meetingbaas_bot_id: str = "",
) -> subprocess.Popen:
    """
    Start a Pipecat process for a client.

    Args:
        client_id: Unique ID for the client
        websocket_url: WebSocket URL for communication
        meeting_url: Meeting URL to join
        persona_data: Data for the persona to use
        streaming_audio_frequency: Audio sampling frequency
        enable_tools: Whether to enable function calling tools
        api_key: API key for authentication
        meetingbaas_bot_id: ID of the meetingbaas bot

    Returns:
        The subprocess.Popen object for the started process
    """
    logger.info(f"Starting Pipecat process for client {client_id}")

    # Convert persona_data to JSON string
    persona_data_json = json.dumps(persona_data)

    # Construct the command to run the meetingbaas.py script
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "meetingbaas.py"
    )

    # Extract folder name from persona path, or derive from display name
    # persona_data["path"] is like "/path/to/personas/account_executive"
    persona_folder_name = None
    if persona_data.get("path"):
        persona_folder_name = os.path.basename(persona_data["path"])
    if not persona_folder_name:
        # Fallback: convert display name to folder format
        display_name = persona_data.get("name", "Unknown Bot")
        persona_folder_name = display_name.lower().replace(" ", "_")

    logger.info(f"Using persona folder name: {persona_folder_name}")

    # Build command with all parameters
    command = [
        sys.executable,
        script_path,
        "--client-id",
        client_id,
        "--websocket-url",
        websocket_url,
        "--meeting-url",
        meeting_url,
        "--persona-name",
        persona_folder_name,
        "--persona-data-json",
        persona_data_json,
        "--streaming-audio-frequency",
        streaming_audio_frequency,
    ]

    # Add optional flags
    if enable_tools:
        command.append("--enable-tools")

    if api_key:
        command.extend(["--api-key", api_key])

    if meetingbaas_bot_id:
        command.extend(["--meetingbaas-bot-id", meetingbaas_bot_id])

    # Start the process
    process = subprocess.Popen(
        command,
        env=os.environ.copy(),  # Copy the current environment
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,  # Capture output as text
    )

    # Start threads to print output
    threading.Thread(target=stream_output, args=(process.stdout, "[Pipecat STDOUT]"), daemon=True).start()
    threading.Thread(target=stream_output, args=(process.stderr, "[Pipecat STDERR]"), daemon=True).start()

    logger.info(f"Started Pipecat process with PID {process.pid}")
    return process


def terminate_process_gracefully(
    process: subprocess.Popen, timeout: float = 2.0
) -> bool:
    """
    Terminate a process gracefully by first sending SIGTERM, waiting for it to exit,
    and then forcefully killing it if needed.

    Args:
        process: The process to terminate
        timeout: How long to wait for graceful termination before force killing

    Returns:
        True if process was terminated gracefully, False if it had to be force-killed
    """
    if process.poll() is not None:
        # Process is already terminated
        return True

    # Send SIGTERM
    try:
        process.terminate()

        # Wait for process to exit
        for _ in range(int(timeout * 10)):  # Check 10 times per second
            if process.poll() is not None:
                return True
            time.sleep(0.1)

        # Process didn't exit gracefully, force kill it
        process.kill()
        process.wait(1.0)  # Wait up to 1 second for it to be killed
        return False
    except Exception as e:
        logger.error(f"Error terminating process: {e}")
        # Try one last time with kill
        try:
            process.kill()
        except:
            pass
        return False
