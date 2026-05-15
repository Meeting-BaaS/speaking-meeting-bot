# app/routes_sales.py
"""
Sales-agent specific API routes.

Endpoints:
  POST /run-bot             – launch bot & join meeting
  POST /bot/{client_id}/engage  – switch to active Q&A mode
  GET  /bot/{client_id}/report  – retrieve notes + extracted needs
  POST /meeting-baas/webhook    – MeetingBaaS event callbacks
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from app.utils.pipecat_logger import logger
from app.services.meeting_baas import (
    create_meeting_bot as baas_create_bot,
    stop_bot as baas_stop_bot,
)
from app.services.session_manager import session_manager
from app.services.report_generator import generate_report, send_email
from app.core.connection import MEETING_DETAILS, PIPECAT_PROCESSES
from app.core.process import start_pipecat_process

sales_router = APIRouter(tags=["sales-agent"])

# ─────────────────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────────────────


class RunBotRequest(BaseModel):
    meeting_url: str = Field(..., description="Google Meet / Zoom / Teams URL")
    marketing_person_email: str = Field(
        ..., description="Email of the sales rep attending the meeting"
    )
    client_name: str = Field(..., description="Display name of the prospect/client")
    bot_name: str = Field("Sales Assistant", description="Display name for the bot")
    bot_image: Optional[str] = Field(None, description="Avatar URL for the bot")
    entry_message: Optional[str] = Field(
        None, description="First message spoken when bot joins"
    )
    webhook_url: Optional[str] = Field(
        None, description="Override URL for MeetingBaaS callbacks"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "marketing_person_email": "alice@company.com",
                "client_name": "Acme Corp",
                "bot_name": "Sales Assistant",
            }
        }


class RunBotResponse(BaseModel):
    bot_id: str = Field(..., description="MeetingBaaS bot ID")
    client_id: str = Field(..., description="Internal session ID for follow-up calls")
    status: str = Field(..., description="'joined' or 'error'")


class EngageRequest(BaseModel):
    max_minutes: int = Field(3, ge=1, le=10, description="Active Q&A duration (1-10 min)")
    pain_point: Optional[str] = Field(
        None, description="Seed topic for first discovery question"
    )


class WebhookPayload(BaseModel):
    event: str
    bot_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _base_url() -> str:
    """Return the public-facing base URL (ngrok in local dev, BASE_URL or localhost otherwise)."""
    from app.utils.ngrok import LOCAL_DEV_MODE, load_ngrok_urls
    import app.utils.ngrok as ngrok_utils

    if LOCAL_DEV_MODE:
        if not ngrok_utils.NGROK_URLS:
            logger.info("[SalesAgent] Loading ngrok URLs for base URL...")
            ngrok_utils.NGROK_URLS = load_ngrok_urls()
        
        if ngrok_utils.NGROK_URLS:
            return ngrok_utils.NGROK_URLS[0].rstrip("/")
            
    # Fallback to BASE_URL from env or localhost
    url = os.getenv("BASE_URL", "http://localhost:8000")
    if url == "your_base_url_here":
        url = "http://localhost:8000"
    return url.rstrip("/")


async def _revert_to_passive_after(client_id: str, seconds: float) -> None:
    """Background task: switch bot back to passive after engage window expires."""
    await asyncio.sleep(seconds)
    session = session_manager.get_session(client_id)
    if session and session.mode == "active":
        session_manager.set_mode(client_id, "passive")
        session_manager.add_note(
            client_id,
            f"[System] Bot reverted to passive mode after {seconds:.0f}s engage window.",
        )
        logger.info(f"[SalesAgent] Bot {client_id} reverted to passive mode")


# ─────────────────────────────────────────────────────────────────────────────
# POST /run-bot
# ─────────────────────────────────────────────────────────────────────────────


@sales_router.post(
    "/run-bot",
    response_model=RunBotResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Launch sales bot and join the meeting",
)
async def run_bot(body: RunBotRequest, request: Request) -> RunBotResponse:
    """
    Create a MeetingBaaS bot that joins the meeting, then registers a session
    and starts the Pipecat/Gemini subprocess.
    The bot is active at all times and will participate in the meeting.
    """
    api_key: str = getattr(request.state, "api_key", "") or os.getenv(
        "MEETING_BAAS_API_KEY", ""
    )
    client_id: str = str(uuid.uuid4())
    base = _base_url()

    # MeetingBaaS will stream audio TO this WebSocket URL on our server
    websocket_url = f"{base}/ws/{client_id}"
    webhook_url = body.webhook_url or f"{base}/meeting-baas/webhook"

    bot_id = await baas_create_bot(
        meeting_url=body.meeting_url,
        websocket_url=websocket_url,
        bot_name=body.bot_name,
        bot_image=body.bot_image,
        entry_message=body.entry_message or "Hello, I'm joining to assist with notes today.",
        webhook_url=webhook_url,
        streaming_audio_frequency="16khz",
        extra={
            "client_name": body.client_name,
            "marketing_person_email": body.marketing_person_email,
        },
        api_key=api_key,
    )

    if not bot_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create bot via MeetingBaaS API.",
        )

    # ── CRITICAL: Register meeting details so the /ws/{client_id} handler
    # doesn't reject the connection when MeetingBaaS connects ──────────────
    MEETING_DETAILS[client_id] = (
        body.meeting_url,          # [0] meeting_url
        body.bot_name,             # [1] persona_name (bot display name)
        bot_id,                    # [2] meetingbaas_bot_id
        False,                     # [3] enable_tools
        "16khz",                   # [4] streaming_audio_frequency
    )
    logger.info(f"✅ Registered meeting details for client {client_id}")

    # ── Start the Gemini Live bot subprocess ──────────────────────────────
    # It connects as a WebSocket CLIENT to /pipecat/{client_id} on this server.
    server_port = int(os.getenv("PORT", "8000"))
    pipecat_ws_url = f"ws://localhost:{server_port}/pipecat/{client_id}"

    import sys, subprocess, threading

    bot_script = os.path.join(
        os.path.dirname(__file__), "..", "meetingbaas_pipecat", "bot", "bot.py"
    )

    def stream_output(pipe, prefix):
        for line in iter(pipe.readline, ""):
            print(f"{prefix} {line.strip()}")

    command = [
        sys.executable,
        bot_script,
        "--client-id", client_id,
        "--websocket-url", pipecat_ws_url,
        "--client-name", body.client_name,
        "--marketing-email", body.marketing_person_email,
    ]

    process = subprocess.Popen(
        command,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    threading.Thread(target=stream_output, args=(process.stdout, "[GeminiBot STDOUT]"), daemon=True).start()
    threading.Thread(target=stream_output, args=(process.stderr, "[GeminiBot STDERR]"), daemon=True).start()

    PIPECAT_PROCESSES[client_id] = process
    logger.info(f"[SalesAgent] Started Gemini bot subprocess PID={process.pid} for client {client_id}")

    # ── Register the sales session for report tracking ────────────────────
    session_manager.store_session(
        client_id=client_id,
        bot_id=bot_id,
        meeting_url=body.meeting_url,
        marketing_person_email=body.marketing_person_email,
        client_name=body.client_name,
    )

    logger.info(
        f"[SalesAgent] Bot {bot_id} created | session {client_id} | "
        f"client={body.client_name}"
    )
    return RunBotResponse(bot_id=bot_id, client_id=client_id, status="joined")


# ─────────────────────────────────────────────────────────────────────────────
# POST /bot/{client_id}/engage
# ─────────────────────────────────────────────────────────────────────────────


@sales_router.post(
    "/bot/{client_id}/engage",
    status_code=status.HTTP_200_OK,
    summary="Switch bot to active Q&A mode for 2-4 minutes",
)
async def engage_bot(
    client_id: str,
    body: EngageRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Trigger the bot to switch into active engagement mode.
    It will ask discovery questions for up to `max_minutes`, then revert to passive.
    """
    session = session_manager.get_session(client_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {client_id} not found.")

    if session.mode == "ended":
        raise HTTPException(status_code=409, detail="Meeting has already ended.")

    session_manager.set_mode(client_id, "active")
    session_manager.add_note(
        client_id,
        f"[System] Bot switched to ACTIVE mode at {session.engaged_at}.",
    )
    
    # Trigger the Pipecat process
    with open(f"/tmp/bot_{client_id}_engage", "w") as f:
        f.write("engage")

    # Cancel any previous revert task
    if session.engage_timer_task and not session.engage_timer_task.done():
        session.engage_timer_task.cancel()

    # Schedule revert
    engage_seconds = body.max_minutes * 60.0
    task = asyncio.create_task(
        _revert_to_passive_after(client_id, engage_seconds)
    )
    session.engage_timer_task = task

    logger.info(
        f"[SalesAgent] Session {client_id} → ACTIVE for {body.max_minutes} min"
    )
    return {
        "client_id": client_id,
        "mode": "active",
        "max_minutes": body.max_minutes,
        "engaged_at": session.engaged_at.isoformat() if session.engaged_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /bot/{client_id}/report
# ─────────────────────────────────────────────────────────────────────────────


@sales_router.get(
    "/bot/{client_id}/report",
    status_code=status.HTTP_200_OK,
    summary="Get meeting notes and extracted client needs",
)
async def get_report(
    client_id: str,
    send: bool = False,
) -> Dict[str, Any]:
    """
    Return the current session report.

    Query param `send=true` will also email the report to the marketing person.
    """
    session = session_manager.get_session(client_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {client_id} not found.")

    report = generate_report(session)

    if send:
        sent = await send_email(
            to_address=session.marketing_person_email,
            report=report,
        )
        report["email_sent"] = sent

    return report


# ─────────────────────────────────────────────────────────────────────────────
# POST /meeting-baas/webhook
# ─────────────────────────────────────────────────────────────────────────────


@sales_router.post(
    "/meeting-baas/webhook",
    status_code=status.HTTP_200_OK,
    summary="Receive MeetingBaaS event callbacks",
)
async def meetingbaas_webhook(request: Request) -> Dict[str, str]:
    """
    Handles inbound webhooks from MeetingBaaS (v2 API schema).
    """
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event: str = body.get("event", "unknown")
    data: Dict[str, Any] = body.get("data", {})
    bot_id: Optional[str] = data.get("bot_id") or body.get("bot_id")

    logger.info(f"[Webhook] event={event} bot_id={bot_id}")
    session = session_manager.get_session_by_bot_id(bot_id) if bot_id else None

    # ── bot.status_change ────────────────────────────────────────────────────
    if event == "bot.status_change":
        status_code: str = data.get("status", {}).get("code", "")
        logger.info(f"[Webhook] status_code={status_code}")

        if status_code in ("in_call_not_recording", "in_call_recording"):
            if session:
                session_manager.add_note(
                    session.client_id,
                    f"[System] Bot joined the call (status: {status_code}).",
                )

        elif status_code == "call_ended":
            if session:
                session_manager.set_mode(session.client_id, "ended")
                session_manager.add_note(
                    session.client_id, "[System] Call ended."
                )

        elif status_code == "recording_succeeded":
            if session:
                session_manager.add_note(
                    session.client_id, "[System] Recording succeeded."
                )

    # ── complete – full transcript delivered at end of meeting ────────────────
    elif event == "complete":
        bot_id = bot_id or body.get("bot_id")
        session = session_manager.get_session_by_bot_id(bot_id) if bot_id else session

        # FIX: MeetingBaaS sends transcript as array of objects with speaker/words
        transcript = body.get("transcript") or data.get("transcript", [])
        
        logger.info(f"[Webhook] complete event — {len(transcript) if isinstance(transcript, list) else 0} transcript segments")

        if session:
            # FIX: Properly handle MeetingBaaS transcript format
            if isinstance(transcript, list):
                for entry in transcript:
                    speaker = entry.get("speaker", "Unknown")
                    
                    # Handle both formats:
                    # Format 1: {"speaker": "X", "words": [{"word": "...", "start_time": ..., "end_time": ...}]}
                    # Format 2: {"speaker": "X", "text": "..."}
                    
                    words = entry.get("words", [])
                    if isinstance(words, list) and len(words) > 0:
                        # Extract text from words array
                        text = " ".join(
                            w.get("word", "") 
                            for w in words 
                            if isinstance(w, dict) and w.get("word")
                        )
                    else:
                        # Fall back to direct text field
                        text = entry.get("text", "")
                    
                    if text.strip():  # Only add non-empty transcriptions
                        session_manager.add_transcription(
                            session.client_id, 
                            speaker=speaker, 
                            text=text
                        )
                        logger.info(f"[Webhook] Added transcript: {speaker}: {text[:50]}...")
            
            # Get recording URLs if present
            mp4_url = body.get("mp4") or data.get("mp4", "")
            audio_url = body.get("audio") or data.get("audio", "")
            
            if mp4_url or audio_url:
                notes = []
                if mp4_url:
                    notes.append(f"Video: {mp4_url}")
                if audio_url:
                    notes.append(f"Audio: {audio_url}")
                session_manager.add_note(session.client_id, f"[System] Recording URLs: {', '.join(notes)}")
            
            session_manager.set_mode(session.client_id, "ended")

    else:
        logger.info(f"[Webhook] Unhandled event '{event}' for bot {bot_id}")

    return {"status": "ok"}
