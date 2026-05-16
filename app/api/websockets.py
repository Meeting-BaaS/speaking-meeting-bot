"""WebSocket routes for the Speaking Meeting Bot API.

Changes implemented:
  #1  - Separate /ws/input and /ws/output WebSocket routes
  #3  - READY handshake before streaming
  #6  - Heartbeat ping/pong handling
  #7  - Structured logging with session IDs
  #15 - Explicit cleanup on disconnect
  #16 - Business logic moved to orchestrator (no AI/process spawning here)
"""

import asyncio
import json

from dotenv import load_dotenv
load_dotenv()

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.connection import MEETING_DETAILS, registry
from app.core.router import router as message_router
from app.utils.pipecat_logger import logger
from app.utils.ngrok import LOCAL_DEV_MODE, log_ngrok_status, release_ngrok_url
from app.services.session_manager import SessionState, session_manager
from app.runtime import orchestrator

websocket_router = APIRouter()


def find_client_id_by_meetingbaas_bot_id(meetingbaas_bot_id: str) -> str | None:
    """Look up the internal client_id by MeetingBaas bot_id."""
    for internal_id, details in MEETING_DETAILS.items():
        if len(details) > 2 and details[2] == meetingbaas_bot_id:
            return internal_id
    return None


def _log(event: str, client_id: str, bot_id: str = "unknown",
         ws_state: str = "", session_state: str = ""):
    """Structured log helper (Change #7)."""
    logger.info(
        event,
        extra={
            "client_id": client_id,
            "bot_id": bot_id,
            "session_id": client_id,
            "websocket_state": ws_state,
            "session_state": session_state,
        }
    )


# ---------------------------------------------------------------------------
# Change #1: Separate Input and Output WebSocket Routes
# ---------------------------------------------------------------------------

@websocket_router.websocket("/ws/input/{client_id}")
async def websocket_input(websocket: WebSocket, client_id: str):
    """
    Receives audio FROM MeetingBaas and forwards to Pipecat.
    (Change #1 – dedicated INPUT route)
    """
    internal_client_id = client_id

    try:
        # Resolve internal client_id if MeetingBaas connects with its own bot_id
        if client_id not in MEETING_DETAILS:
            internal_client_id = find_client_id_by_meetingbaas_bot_id(client_id)
            if internal_client_id:
                _log("ws_input_id_resolved", internal_client_id, ws_state="input",
                     session_state=SessionState.WS_CONNECTED.value)
            else:
                logger.error(f"No meeting details found for client {client_id}")
                await websocket.close(code=1008, reason="Missing meeting details")
                return

        await registry.connect(websocket, internal_client_id, channel="input")

        # Update session state (Change #2/#3)
        session = session_manager.get_session(internal_client_id)
        bot_id = session.bot_id if session else "unknown"
        session_manager.mark_ws_connected(internal_client_id, client_id)
        _log("ws_input_connected", internal_client_id, bot_id=bot_id,
             ws_state="input", session_state=SessionState.WS_CONNECTED.value)

        meeting_details = MEETING_DETAILS[internal_client_id]
        meeting_url = meeting_details[0] if len(meeting_details) > 0 else None
        persona_name = meeting_details[1] if len(meeting_details) > 1 else None
        streaming_audio_frequency = meeting_details[4] if len(meeting_details) > 4 else "16khz"
        enable_tools = meeting_details[3] if len(meeting_details) > 3 else False
        meetingbaas_bot_id = meeting_details[2] if len(meeting_details) > 2 else ""

        # Resolve persona data
        persona_data = session_manager.get_session(internal_client_id)
        persona_dict = {"name": persona_name}
        if hasattr(persona_data, "system_prompt"):
            persona_dict["readme"] = persona_data.system_prompt

        # Start the Pipecat pipeline via orchestrator (Change #11, #5)
        import os
        port = os.environ.get("PORT", "8000")
        pipecat_ws_url = f"ws://127.0.0.1:{port}/pipecat/{internal_client_id}"
        await orchestrator.start_session(
            client_id=internal_client_id,
            websocket_url=pipecat_ws_url,
            meeting_url=meeting_url or "",
            persona_data=persona_dict,
            streaming_audio_frequency=streaming_audio_frequency,
            enable_tools=enable_tools,
            meetingbaas_bot_id=meetingbaas_bot_id,
        )

        # Send READY handshake once pipeline is starting (Change #3)
        await websocket.send_json({"type": "ready"})
        session_manager.mark_ready(internal_client_id)
        _log("ws_input_ready_sent", internal_client_id, bot_id=bot_id,
             ws_state="input", session_state=SessionState.READY.value)

        # Main audio receive loop
        audio_frame_count = 0
        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as e:
                if "disconnect" in str(e).lower():
                    break
                raise

            # Change #3: Reject frames before READY
            if not session_manager.is_ready(internal_client_id):
                logger.debug(f"Dropping frame for {internal_client_id} — session not READY")
                continue

            if "bytes" in message:
                audio_data = message["bytes"]
                audio_frame_count += 1
                if audio_frame_count <= 3 or audio_frame_count % 200 == 0:
                    logger.info(
                        f"[ws_input] Received audio frame #{audio_frame_count} for {internal_client_id} ({len(audio_data)} bytes)"
                    )
                session_manager.mark_audio_frame(internal_client_id)
                session_manager.mark_streaming(internal_client_id)
                # Change #6: touch on activity
                session_manager.touch(internal_client_id)

                # Change #9: Push through backpressure queue
                orchestrator.push_audio(internal_client_id, audio_data)

                # Forward to Pipecat
                await message_router.send_to_pipecat(audio_data, internal_client_id)

            elif "text" in message:
                text_data = message["text"]
                if text_data:
                    logger.info(
                        f"[ws_input] Text frame for {internal_client_id}: {text_data[:300]}"
                    )
                # Handle pong for heartbeat (Change #6)
                try:
                    payload = json.loads(text_data)
                    if payload.get("type") == "pong":
                        session_manager.touch(internal_client_id)
                        continue
                except Exception:
                    pass

                # MeetingBaas handshake: parse sample_rate
                try:
                    handshake = json.loads(text_data)
                    sr = handshake.get("sample_rate")
                    if sr and isinstance(sr, int):
                        from app.core.converter import converter as _conv
                        _conv.set_sample_rate(sr)
                        _log("ws_input_sample_rate_updated", internal_client_id,
                             bot_id=bot_id, ws_state=str(sr))
                    session_manager.mark_handshake(internal_client_id)
                except Exception:
                    pass

    except WebSocketDisconnect:
        _log("ws_input_disconnected", internal_client_id,
             session_state=SessionState.CLOSED.value)
    except Exception as e:
        logger.error(f"[ws_input] Error for {internal_client_id}: {e} (repr: {repr(e)})")
    finally:
        await _cleanup_input(internal_client_id, channel="input")


@websocket_router.websocket("/ws/output/{client_id}")
async def websocket_output(websocket: WebSocket, client_id: str):
    """
    Sends audio FROM Pipecat/TTS BACK to MeetingBaas.
    (Change #1 – dedicated OUTPUT route)
    This is a receive-only socket from MeetingBaas's perspective — it just receives
    the bot's audio stream.
    """
    internal_client_id = client_id
    if client_id not in MEETING_DETAILS:
        resolved_client_id = find_client_id_by_meetingbaas_bot_id(client_id)
        if resolved_client_id:
            internal_client_id = resolved_client_id
        else:
            await websocket.close(code=1008, reason="Missing meeting details")
            return

    await registry.connect(websocket, internal_client_id, channel="output")
    _log("ws_output_connected", internal_client_id, ws_state="output")

    try:
        await websocket.send_json({"type": "ready"})
        logger.info(f"[ws_output] READY sent for {internal_client_id}")
        # Keep alive — MeetingBaas will receive audio pushed from pipecat_websocket
        while True:
            try:
                message = await websocket.receive()
                # Handle pong (Change #6)
                if "bytes" in message:
                    logger.warning(
                        f"[ws_output] Unexpected binary frame for {internal_client_id} ({len(message['bytes'])} bytes)"
                    )
                elif "text" in message:
                    logger.info(
                        f"[ws_output] Text frame for {internal_client_id}: {message['text'][:300]}"
                    )
                    try:
                        payload = json.loads(message["text"])
                        if payload.get("type") == "pong":
                            session_manager.touch(internal_client_id)
                    except Exception:
                        pass
            except RuntimeError:
                break
    except WebSocketDisconnect:
        _log("ws_output_disconnected", internal_client_id, session_state=SessionState.CLOSED.value)
    except Exception as e:
        logger.error(f"[ws_output] Error for {internal_client_id}: {e}")
    finally:
        try:
            await registry.disconnect(internal_client_id, channel="output")
        except Exception:
            pass
        if LOCAL_DEV_MODE:
            release_ngrok_url(internal_client_id)


# ---------------------------------------------------------------------------
# Legacy single WebSocket route (kept for backward compat with old MeetingBaas)
# ---------------------------------------------------------------------------

@websocket_router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """
    Legacy combined route. Forwards incoming audio to Pipecat.
    New deployments should use /ws/input/{id} and /ws/output/{id}.
    """
    internal_client_id = client_id

    try:
        if client_id not in MEETING_DETAILS:
            internal_client_id = find_client_id_by_meetingbaas_bot_id(client_id)
            if internal_client_id:
                logger.info(f"[ws] Resolved {client_id} -> {internal_client_id}")
            else:
                logger.error(f"[ws] No meeting details for {client_id}")
                await websocket.close(code=1008, reason="Missing meeting details")
                return

        await registry.connect(websocket, internal_client_id, channel="legacy")

        meeting_details = MEETING_DETAILS[internal_client_id]
        meeting_url = meeting_details[0] if len(meeting_details) > 0 else None
        persona_name = meeting_details[1] if len(meeting_details) > 1 else None
        streaming_audio_frequency = meeting_details[4] if len(meeting_details) > 4 else "16khz"
        enable_tools = meeting_details[3] if len(meeting_details) > 3 else False
        meetingbaas_bot_id = meeting_details[2] if len(meeting_details) > 2 else ""

        session_manager.mark_ws_connected(internal_client_id, client_id)

        import os
        port = os.environ.get("PORT", "8000")
        pipecat_ws_url = f"ws://127.0.0.1:{port}/pipecat/{internal_client_id}"
        await orchestrator.start_session(
            client_id=internal_client_id,
            websocket_url=pipecat_ws_url,
            meeting_url=meeting_url or "",
            persona_data={"name": persona_name},
            streaming_audio_frequency=streaming_audio_frequency,
            enable_tools=enable_tools,
            meetingbaas_bot_id=meetingbaas_bot_id,
        )

        # Send READY handshake (Change #3)
        await websocket.send_json({"type": "ready"})
        session_manager.mark_ready(internal_client_id)
        logger.info(f"[ws] READY sent for {internal_client_id}")

        audio_frame_count = 0

        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as e:
                if "disconnect" in str(e).lower():
                    break
                raise

            # Change #3: Gate on READY
            if not session_manager.is_ready(internal_client_id):
                continue

            if "bytes" in message:
                audio_frame_count += 1
                if audio_frame_count <= 3 or audio_frame_count % 200 == 0:
                    logger.info(
                        f"[ws] Received audio frame #{audio_frame_count} for {internal_client_id} ({len(message['bytes'])} bytes)"
                    )
                session_manager.mark_audio_frame(internal_client_id)
                session_manager.mark_streaming(internal_client_id)
                session_manager.touch(internal_client_id)
                orchestrator.push_audio(internal_client_id, message["bytes"])
                await message_router.send_to_pipecat(message["bytes"], internal_client_id)

            elif "text" in message:
                logger.info(f"[ws] Text frame for {internal_client_id}: {message['text'][:300]}")
                try:
                    payload = json.loads(message["text"])
                    if payload.get("type") == "pong":
                        session_manager.touch(internal_client_id)
                        continue
                    sr = payload.get("sample_rate")
                    if sr and isinstance(sr, int):
                        from app.core.converter import converter as _conv
                        _conv.set_sample_rate(sr)
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.info(f"[ws] Disconnected: {client_id}")
    except Exception as e:
        logger.error(f"[ws] Error for {client_id}: {e} (repr: {repr(e)})")
    finally:
        await _cleanup_input(internal_client_id, channel="legacy")


# ---------------------------------------------------------------------------
# Pipecat internal WebSocket (unchanged API, enhanced logging)
# ---------------------------------------------------------------------------

@websocket_router.websocket("/pipecat/{client_id}")
async def pipecat_websocket(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections FROM Pipecat. (Change #7 logging)"""
    await registry.connect(websocket, client_id, is_pipecat=True)
    session_manager.mark_pipecat_connected(client_id)
    session = session_manager.get_session(client_id)
    bot_id = session.bot_id if session else "unknown"

    _log("pipecat_connected", client_id, bot_id=bot_id,
         ws_state="pipecat", session_state=SessionState.PIPECAT_CONNECTED.value)

    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                data = message["bytes"]
                logger.debug(f"[pipecat] {len(data)} bytes from {client_id}")
                await message_router.send_from_pipecat(data, client_id)
            elif "text" in message:
                data = message["text"]
                logger.info(f"[pipecat] Text from {client_id}: {data[:100]}...")
    except WebSocketDisconnect:
        _log("pipecat_disconnected", client_id, bot_id=bot_id,
             ws_state="pipecat", session_state=SessionState.CLOSED.value)
    except Exception as e:
        logger.error(f"[pipecat] Error for {client_id}: {e}")
    finally:
        message_router.mark_closing(client_id)
        try:
            await registry.disconnect(client_id, is_pipecat=True)
        except Exception as e:
            logger.debug(f"[pipecat] Disconnect error for {client_id}: {e}")
        if LOCAL_DEV_MODE:
            release_ngrok_url(client_id)


# ---------------------------------------------------------------------------
# Shared cleanup helper (Change #15)
# ---------------------------------------------------------------------------

async def _cleanup_input(internal_client_id: str, channel: str = "input") -> None:
    """
    Ordered cleanup: tasks → queues → registry → session.
    (Change #15 — Explicit Cleanup Pipeline)
    """
    _log("cleanup_started", internal_client_id, ws_state="cleanup",
         session_state=SessionState.CLOSED.value)

    # 1. Cancel tasks + queues via orchestrator
    await orchestrator.stop_session(internal_client_id)

    # 2. Clear connection registry
    message_router.mark_closing(internal_client_id)
    try:
        await registry.disconnect(internal_client_id, channel=channel)
    except Exception as e:
        logger.debug(f"[cleanup] Disconnect error for {internal_client_id}: {e}")

    # 3. Remove meeting details
    from app.core.connection import MEETING_DETAILS
    MEETING_DETAILS.pop(internal_client_id, None)

    # 4. Release ngrok if local dev
    if LOCAL_DEV_MODE:
        release_ngrok_url(internal_client_id)
        log_ngrok_status()

    _log("cleanup_done", internal_client_id, session_state=SessionState.CLOSED.value)
