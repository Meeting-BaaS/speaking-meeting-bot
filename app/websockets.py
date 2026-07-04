"""WebSocket routes for the Speaking Meeting Bot API."""

import asyncio
import json
import os
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.connection import MEETING_DETAILS, PIPECAT_PROCESSES, registry
from core.process import start_pipecat_process, terminate_process_gracefully
from core.router import router as message_router
from meetingbaas_pipecat.utils.logger import logger
from utils.floor import floor_key, write_floor
from utils.ngrok import LOCAL_DEV_MODE, log_ngrok_status, release_ngrok_url
from utils.runtime import get_internal_pipecat_ws_url, get_state_dir

websocket_router = APIRouter()

# Last floor holder written per meeting key — avoids rewriting the floor file
# on every speaker-state update (MeetingBaas sends them continuously).
_last_floor_speaker: dict = {}

# Meeting keys whose bots already got their ready signal (see below).
_ready_signaled: set = set()


def _signal_ready_from_roster(meeting_url: str, key: str) -> None:
    """Write ready-signal files for every bot in this meeting, once.

    The bot-specific callback_config only delivers bot.completed/bot.failed —
    the in_call_recording status webhook exists only on account-level SVIX
    webhooks. So the reliable in-band readiness signal is the participant
    roster: MeetingBaas only streams it once the bot is actually admitted
    into the call (a lobbied bot can't see the roster). On the first roster
    message for a meeting, release every one of our bots' entry messages.
    """
    if key in _ready_signaled:
        return
    _ready_signaled.add(key)
    ready_dir = os.path.join(get_state_dir(), "ready_signals")
    os.makedirs(ready_dir, exist_ok=True)
    for client_id, details in MEETING_DETAILS.items():
        if len(details) > 0 and floor_key(details[0]) == key:
            try:
                with open(os.path.join(ready_dir, f"{client_id}.ready"), "w") as f:
                    f.write(datetime.now().isoformat())
                logger.info(f"Roster seen for meeting {key} — ready signal for {client_id}")
            except OSError as e:
                logger.warning(f"Could not write ready signal for {client_id}: {e}")


def _update_floor_from_speaker_state(meeting_url: str, text_data: str) -> None:
    """Track which of OUR bots is speaking in this meeting.

    MeetingBaas sends participant state as a JSON list of
    {"name", "id", "timestamp", "isSpeaking"}. We match speaking participants
    against the display names of our own bots in the same meeting (from
    MEETING_DETAILS) and record the result in the per-meeting floor file that
    the Pipecat children poll (see utils/floor.py).
    """
    try:
        payload = json.loads(text_data)
    except ValueError:
        return
    if not isinstance(payload, list):
        return

    key = floor_key(meeting_url)

    # A roster message means the bot is admitted and in the call — release
    # the entry messages for this meeting's bots.
    _signal_ready_from_roster(meeting_url, key)

    our_names = {
        details[1]
        for details in MEETING_DETAILS.values()
        if len(details) > 1 and floor_key(details[0]) == key
    }

    speaker = None
    for participant in payload:
        if not isinstance(participant, dict) or not participant.get("isSpeaking"):
            continue
        if participant.get("name") in our_names:
            speaker = participant["name"]
            break

    # Refresh the file whenever one of ours speaks (keeps the staleness
    # timestamp alive); write the release only on change.
    if speaker is not None or _last_floor_speaker.get(key) is not None:
        try:
            write_floor(meeting_url, speaker)
        except OSError as e:
            logger.warning(f"Could not write floor file for {key}: {e}")
        _last_floor_speaker[key] = speaker


def find_client_id_by_meetingbaas_bot_id(meetingbaas_bot_id: str) -> str | None:
    """Look up the internal client_id by MeetingBaas bot_id."""
    for internal_id, details in MEETING_DETAILS.items():
        if len(details) > 2 and details[2] == meetingbaas_bot_id:
            return internal_id
    return None


@websocket_router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections from clients."""
    await registry.connect(websocket, client_id)
    logger.info(f"Client {client_id} connected")

    try:
        # Get meeting details from our in-memory storage
        # First try direct lookup, then try to find by MeetingBaas bot_id
        internal_client_id = client_id
        if client_id not in MEETING_DETAILS:
            # MeetingBaas might be connecting with its own bot_id instead of our internal client_id
            internal_client_id = find_client_id_by_meetingbaas_bot_id(client_id)
            if internal_client_id:
                logger.info(f"Found internal client_id {internal_client_id} for MeetingBaas bot_id {client_id}")
            else:
                logger.error(f"No meeting details found for client {client_id}")
                await websocket.close(code=1008, reason="Missing meeting details")
                return

        # Get stored meeting details with fallbacks to ensure compatibility
        meeting_details = MEETING_DETAILS[internal_client_id]
        meeting_url = meeting_details[0] if len(meeting_details) > 0 else None
        persona_name = meeting_details[1] if len(meeting_details) > 1 else None
        meetingbaas_bot_id = meeting_details[2] if len(meeting_details) > 2 else None
        enable_tools = meeting_details[3] if len(meeting_details) > 3 else False

        # Default to 16khz if not specified
        streaming_audio_frequency = (
            meeting_details[4] if len(meeting_details) > 4 else "16khz"
        )

        # Full resolved persona dict from routes.py (index 5). Required for
        # dynamic prompt-derived personas, which exist only in memory; fall
        # back to a name-only dict for entries stored before this field.
        persona_data = (
            meeting_details[5]
            if len(meeting_details) > 5 and isinstance(meeting_details[5], dict)
            else {"name": persona_name}
        )

        logger.info(
            f"Retrieved meeting details for {internal_client_id}: {meeting_url}, {persona_name}, {meetingbaas_bot_id}, {enable_tools}, {streaming_audio_frequency}"
        )

        # Check if a Pipecat process is already running for this client
        if (
            internal_client_id in PIPECAT_PROCESSES
            and PIPECAT_PROCESSES[internal_client_id].poll() is None
        ):
            logger.info(f"Pipecat process already running for client {internal_client_id}")
        else:
            # Start Pipecat process if not already running
            pipecat_websocket_url = get_internal_pipecat_ws_url(internal_client_id)
            process = start_pipecat_process(
                client_id=internal_client_id,
                websocket_url=pipecat_websocket_url,
                meeting_url=meeting_url,
                persona_data=persona_data,
                streaming_audio_frequency=streaming_audio_frequency,
                enable_tools=enable_tools,
                api_key="",
                meetingbaas_bot_id=meetingbaas_bot_id or "",
            )

            # Store the process for cleanup
            PIPECAT_PROCESSES[internal_client_id] = process

        # Process messages - route to Pipecat using internal_client_id
        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as e:
                if "Cannot call \"receive\" once a disconnect message has been received" in str(e):
                    logger.info(f"WebSocket for client {client_id} closed by client.")
                    break
                raise

            # logger.info(f"Received message type: {type(message)}, keys: {list(message.keys())}")
            if "bytes" in message:
                audio_data = message["bytes"]
                # Audio logging disabled - we confirmed audio is being received
                # Route to Pipecat using internal_client_id
                await message_router.send_to_pipecat(audio_data, internal_client_id)
            elif "text" in message:
                text_data = message["text"]
                logger.debug(
                    f"Received text message from client {client_id}: {text_data[:100]}..."
                )
                # Speaker-state updates drive the bot-vs-bot floor control
                _update_floor_from_speaker_state(meeting_url, text_data)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for client {client_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e} (repr: {repr(e)})")
    finally:
        # Clean up using internal_client_id
        if internal_client_id in PIPECAT_PROCESSES:
            process = PIPECAT_PROCESSES[internal_client_id]
            if process and process.poll() is None:  # If process is still running
                try:
                    if terminate_process_gracefully(process, timeout=3.0):
                        logger.info(
                            f"Gracefully terminated Pipecat process for client {internal_client_id}"
                        )
                    else:
                        logger.warning(
                            f"Had to forcefully kill Pipecat process for client {internal_client_id}"
                        )
                except Exception as e:
                    logger.error(f"Error terminating process: {e}")
            # Remove from our storage
            PIPECAT_PROCESSES.pop(internal_client_id, None)

        if internal_client_id in MEETING_DETAILS:
            MEETING_DETAILS.pop(internal_client_id, None)

        # Mark client as closing to prevent further message sending
        message_router.mark_closing(internal_client_id)

        # Gracefully disconnect - wrapping in try/except to handle already closed connections
        try:
            await registry.disconnect(client_id)
            logger.info(f"Client {client_id} disconnected")
        except Exception as e:
            # Only log at debug level since this is expected during abrupt disconnections
            logger.debug(f"Error disconnecting client {client_id}: {e}")

        # Release ngrok URL
        if LOCAL_DEV_MODE:
            release_ngrok_url(client_id)
            log_ngrok_status()


@websocket_router.websocket("/pipecat/{client_id}")
async def pipecat_websocket(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections from Pipecat."""
    await registry.connect(websocket, client_id, is_pipecat=True)
    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                data = message["bytes"]
                logger.debug(
                    f"Received binary data ({len(data)} bytes) from Pipecat client {client_id}"
                )
                # Forward Pipecat messages to client with conversion
                await message_router.send_from_pipecat(data, client_id)
            elif "text" in message:
                data = message["text"]
                logger.info(
                    f"Received text message from Pipecat client {client_id}: {data[:100]}..."
                )
    except WebSocketDisconnect:
        logger.info(f"Pipecat WebSocket disconnected for client {client_id}")
    except Exception as e:
        logger.error(
            f"Error in Pipecat WebSocket handler for client {client_id}: {str(e)}"
        )
    finally:
        # Mark client as closing before disconnecting
        message_router.mark_closing(client_id)

        try:
            await registry.disconnect(client_id, is_pipecat=True)
            logger.info(f"Pipecat client {client_id} disconnected")
        except Exception as e:
            # Log at debug level since this can happen during normal shutdown
            logger.debug(f"Error disconnecting Pipecat client {client_id}: {e}")

        # Release ngrok URL
        if LOCAL_DEV_MODE:
            release_ngrok_url(client_id)
            log_ngrok_status()
