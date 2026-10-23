"""Tear a bot down completely: its sockets, its Pipecat child and its state.

Used when a caller removes the bot, when MeetingBaas reports the call over or
the bot failed, and when a bot never makes it into the call. Before this, only
DELETE /bots did all of it: a meeting that simply ended left the child running
with its speech sessions open, prompting the LLM into an empty room until the
next deploy.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

from core.connection import BOT_CREDENTIALS, MEETING_DETAILS, PIPECAT_PROCESSES, registry
from core.process import terminate_process_gracefully
from core.router import router as message_router
from meetingbaas_pipecat.utils.logger import logger
from utils.ngrok import LOCAL_DEV_MODE, log_ngrok_status, release_ngrok_url

# The child ends its own pipeline when its socket closes and saves the final
# transcript on the way out; give it that long before SIGTERM.
EXIT_GRACE_SECS = 5.0


async def shutdown_bot(
    client_id: str, *, reason: str, exit_grace_secs: float = EXIT_GRACE_SECS
) -> dict:
    """Close both sockets, let the child exit, kill it if it will not, forget the bot.

    The Pipecat socket goes first because closing it is the signal the child
    acts on. Returns what happened, for logs and tests.
    """
    outcome = {"client_id": client_id, "reason": reason, "process": "none", "sockets_closed": 0}

    message_router.mark_closing(client_id)
    for is_pipecat in (True, False):
        connections = registry.pipecat_connections if is_pipecat else registry.active_connections
        if client_id not in connections:
            continue
        try:
            await registry.disconnect(client_id, is_pipecat=is_pipecat)
            outcome["sockets_closed"] += 1
        except Exception as e:  # a socket that is already gone is fine
            logger.debug(f"Closing socket for {client_id} (pipecat={is_pipecat}): {e}")

    process = PIPECAT_PROCESSES.pop(client_id, None)
    if process is not None:
        if await _wait_for_exit(process, exit_grace_secs):
            outcome["process"] = "exited"
        elif await asyncio.to_thread(terminate_process_gracefully, process, 3.0):
            outcome["process"] = "terminated"
        else:
            outcome["process"] = "killed"

    message_router.drop_pending_audio(client_id)
    MEETING_DETAILS.pop(client_id, None)
    BOT_CREDENTIALS.pop(client_id, None)
    if LOCAL_DEV_MODE:
        release_ngrok_url(client_id)
        log_ngrok_status()

    logger.info(
        f"Bot {client_id} shut down ({reason}): process {outcome['process']}, "
        f"{outcome['sockets_closed']} socket(s) closed"
    )
    return outcome


async def _wait_for_exit(process: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    return process.poll() is not None
