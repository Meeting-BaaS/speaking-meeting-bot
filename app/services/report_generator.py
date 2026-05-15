# app/services/report_generator.py

import asyncio
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from app.utils.pipecat_logger import logger

from app.services.session_manager import BotSession


def generate_report(session: BotSession) -> Dict[str, Any]:
    """
    Assemble a structured sales report dict from a BotSession.

    This is intentionally kept as a pure dict builder; if you later
    want to call Gemini to summarize you can do so here.
    """
    duration_seconds: Optional[float] = None
    if session.ended_at and session.created_at:
        duration_seconds = (session.ended_at - session.created_at).total_seconds()

    engage_duration: Optional[float] = None
    if session.engaged_at and session.ended_at:
        engage_duration = (session.ended_at - session.engaged_at).total_seconds()

    return {
        "client_name": session.client_name,
        "marketing_person_email": session.marketing_person_email,
        "meeting_url": session.meeting_url,
        "bot_id": session.bot_id,
        "created_at": session.created_at.isoformat(),
        "engaged_at": session.engaged_at.isoformat() if session.engaged_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": duration_seconds,
        "engage_duration_seconds": engage_duration,
        "mode_at_report_time": session.mode,
        "notes": session.notes,
        "extracted_needs": session.extracted_needs,
        "transcription_count": len(session.transcriptions),
        "transcriptions": session.transcriptions,
    }


async def send_email(
    to_address: str,
    report: Dict[str, Any],
    smtp_host: Optional[str] = None,
    smtp_port: int = 587,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
) -> bool:
    """
    Send the report as a plain-text email.

    All SMTP credentials fall back to environment variables:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

    Returns True on success, False on any error (stub-safe: logs and returns False
    if credentials are missing rather than raising).
    """
    host = smtp_host or os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", str(smtp_port)))
    user = smtp_user or os.getenv("SMTP_USER")
    password = smtp_password or os.getenv("SMTP_PASSWORD")

    if not all([host, user, password]):
        logger.warning(
            "[ReportGenerator] SMTP credentials not configured – skipping email send."
        )
        return False

    subject = (
        f"Sales Meeting Report – {report.get('client_name', 'Unknown Client')}"
    )

    lines = [
        f"Client: {report.get('client_name')}",
        f"Meeting: {report.get('meeting_url')}",
        f"Duration: {report.get('duration_seconds', 0):.0f}s",
        "",
        "=== NOTES ===",
        *[f"- {n}" for n in report.get("notes", [])],
        "",
        "=== EXTRACTED CLIENT NEEDS ===",
        *[f"- {n}" for n in report.get("extracted_needs", [])],
        "",
        "=== FULL TRANSCRIPTION ===",
        *[
            f"[{t['timestamp']}] {t['speaker']}: {t['text']}"
            for t in report.get("transcriptions", [])
        ],
    ]
    body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["From"] = user  # type: ignore[assignment]
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        await asyncio.to_thread(
            _send_smtp, host, port, user, password, to_address, msg  # type: ignore[arg-type]
        )
        logger.info(f"[ReportGenerator] Email sent to {to_address}")
        return True
    except Exception as exc:
        logger.error(f"[ReportGenerator] Email send failed: {exc}")
        return False


def _send_smtp(
    host: str,
    port: int,
    user: str,
    password: str,
    to_address: str,
    msg: MIMEMultipart,
) -> None:
    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.sendmail(user, to_address, msg.as_string())
