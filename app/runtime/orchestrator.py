# app/runtime/orchestrator.py
# Change #11: Dedicated Runtime Orchestrator
# Change #5: Async Tasks instead of Subprocesses
# Change #9: Backpressure Handling (audio queue)
# Change #6: Heartbeat monitoring

"""
Runtime Orchestrator — Single source of truth for a bot session's lifecycle.

Routes and other modules should only call:
    orchestrator.start_session(...)
    orchestrator.stop_session(...)

The orchestrator manages:
- session creation & state transitions
- Pipecat pipeline as an asyncio.Task (not subprocess)
- heartbeat ping loop
- explicit ordered cleanup on shutdown
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

from app.services.session_manager import SessionState, session_manager
from app.utils.pipecat_logger import logger

_log = logger

# Active asyncio tasks keyed by client_id (Change #5)
_active_tasks: Dict[str, asyncio.Task] = {}

# Backpressure audio queues keyed by client_id (Change #9)
_audio_queues: Dict[str, asyncio.Queue] = {}

# Heartbeat tasks keyed by client_id (Change #6)
_heartbeat_tasks: Dict[str, asyncio.Task] = {}

AUDIO_QUEUE_SIZE = 50  # Drop old frames when full (Change #9)
HEARTBEAT_INTERVAL = 10  # seconds (Change #6)
STALE_SESSION_TIMEOUT = 60  # seconds (Change #6)


# ---------------------------------------------------------------------------
# Public API — called only from routes
# ---------------------------------------------------------------------------

async def start_session(
    client_id: str,
    websocket_url: str,
    meeting_url: str,
    persona_data: Dict[str, Any],
    streaming_audio_frequency: str = "16khz",
    enable_tools: bool = False,
    meetingbaas_bot_id: str = "",
) -> None:
    """
    Start a Pipecat pipeline session as an asyncio Task. (Change #5 & #11)
    """
    _log.info(
        "orchestrator_start_session",
        extra={
            "client_id": client_id,
            "bot_id": meetingbaas_bot_id,
            "session_id": client_id,
            "websocket_state": websocket_url,
            "session_state": SessionState.CREATED.value,
        }
    )

    # Create backpressure queue for this session (Change #9)
    _audio_queues[client_id] = asyncio.Queue(maxsize=AUDIO_QUEUE_SIZE)

    # Launch the Pipecat pipeline as a coroutine Task (Change #5)
    task = asyncio.create_task(
        _run_pipecat_pipeline(
            client_id=client_id,
            websocket_url=websocket_url,
            meeting_url=meeting_url,
            persona_data=persona_data,
            streaming_audio_frequency=streaming_audio_frequency,
            enable_tools=enable_tools,
        ),
        name=f"pipecat-{client_id}"
    )
    _active_tasks[client_id] = task
    task.add_done_callback(lambda t: _on_task_done(client_id, t))

    # Start heartbeat monitor (Change #6)
    hb_task = asyncio.create_task(
        _heartbeat_loop(client_id),
        name=f"heartbeat-{client_id}"
    )
    _heartbeat_tasks[client_id] = hb_task


async def stop_session(client_id: str) -> None:
    """
    Ordered teardown: cancel tasks → release queues → clear registry → remove session.
    (Change #15 — Explicit Cleanup Pipeline)
    """
    _log.info(
        "orchestrator_stop_session",
        extra={
            "client_id": client_id,
            "bot_id": _get_bot_id(client_id),
            "session_id": client_id,
            "websocket_state": None,
            "session_state": SessionState.CLOSED.value,
        }
    )

    # 1. Cancel heartbeat task
    hb_task = _heartbeat_tasks.pop(client_id, None)
    if hb_task and not hb_task.done():
        hb_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(hb_task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # 2. Cancel pipecat task
    task = _active_tasks.pop(client_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # 3. Drain and release audio queue
    queue = _audio_queues.pop(client_id, None)
    if queue:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # 4. Clear registry. Keep the session object around so late webhook
    # transcripts and summary reads still have state to attach to.
    from app.core.router import router as message_router
    message_router.mark_closing(client_id)
    session_manager.set_mode(client_id, "ended")


def push_audio(client_id: str, audio_bytes: bytes) -> None:
    """
    Push incoming audio into the per-session backpressure queue. (Change #9)
    Drop oldest frame when the queue is full (lag spike protection).
    """
    queue = _audio_queues.get(client_id)
    if queue is None:
        return

    if queue.full():
        try:
            queue.get_nowait()  # Drop the oldest frame
            _log.warning(
                "audio_queue_full_dropped_frame",
                extra={
                    "client_id": client_id,
                    "bot_id": _get_bot_id(client_id),
                    "session_id": client_id,
                    "websocket_state": None,
                    "session_state": "streaming",
                }
            )
        except asyncio.QueueEmpty:
            pass

    try:
        queue.put_nowait(audio_bytes)
    except asyncio.QueueFull:
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_bot_id(client_id: str) -> str:
    session = session_manager.get_session(client_id)
    return session.bot_id if session else "unknown"


def _on_task_done(client_id: str, task: asyncio.Task) -> None:
    """Called when a Pipecat task finishes (completed, cancelled, or errored)."""
    _active_tasks.pop(client_id, None)
    if task.cancelled():
        _log.info(f"[orchestrator] Pipecat task cancelled for {client_id}")
    elif task.exception():
        _log.error(f"[orchestrator] Pipecat task error for {client_id}: {task.exception()}")
    else:
        _log.info(f"[orchestrator] Pipecat task completed for {client_id}")


async def _heartbeat_loop(client_id: str) -> None:
    """
    Send a ping every HEARTBEAT_INTERVAL seconds and disconnect stale sessions.
    (Change #6)
    """
    from app.core.router import router as message_router
    import json as _json

    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            session = session_manager.get_session(client_id)
            if not session:
                break

            # Send ping to client WebSocket
            try:
                await message_router.send_text(
                    _json.dumps({"type": "ping"}), client_id
                )
                session_manager.touch(client_id)
            except Exception as e:
                _log.warning(f"[heartbeat] Ping failed for {client_id}: {e}")

            # Check for stale session
            if session.is_stale(STALE_SESSION_TIMEOUT):
                _log.warning(
                    "stale_session_detected",
                    extra={
                        "client_id": client_id,
                        "bot_id": session.bot_id,
                        "session_id": client_id,
                        "websocket_state": session.websocket_id,
                        "session_state": session.state.value,
                    }
                )
                await stop_session(client_id)
                break

        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"[heartbeat] Unexpected error for {client_id}: {e}")


async def _run_pipecat_pipeline(
    client_id: str,
    websocket_url: str,
    meeting_url: str,
    persona_data: Dict[str, Any],
    streaming_audio_frequency: str,
    enable_tools: bool,
) -> None:
    """
    Run the Pipecat bot pipeline as an async coroutine. (Change #5)
    Replaces the old subprocess-based start_pipecat_process().
    """
    # Import here to avoid circular imports
    from app.bots.gemini_live_bot import run_sales_bot

    _log.info(
        "pipecat_task_started",
        extra={
            "client_id": client_id,
            "bot_id": _get_bot_id(client_id),
            "session_id": client_id,
            "websocket_state": websocket_url,
            "session_state": SessionState.PIPECAT_CONNECTED.value,
        }
    )

    try:
        await run_sales_bot(
            client_id=client_id,
            websocket_url=websocket_url,
            meeting_url=meeting_url,
            persona_data=persona_data,
            streaming_audio_frequency=streaming_audio_frequency,
            enable_tools=enable_tools,
        )
    except asyncio.CancelledError:
        _log.info(f"[orchestrator] Pipecat pipeline cancelled cleanly for {client_id}")
        raise
    except Exception as e:
        _log.error(
            "pipecat_pipeline_error",
            extra={
                "client_id": client_id,
                "bot_id": _get_bot_id(client_id),
                "session_id": client_id,
                "websocket_state": websocket_url,
                "session_state": "error",
                "error": str(e),
            }
        )
