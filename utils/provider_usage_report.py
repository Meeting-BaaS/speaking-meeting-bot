"""Report a bot's provider usage to the Meeting BaaS API once the call ends.

The usage ledger (see ``utils.usage_meter``) says what a bot consumed on
speech-to-text, text-to-speech and the language model, and whose key each
ran on. The API bills the part that ran on Meeting BaaS keys as tokens on the
bot, so it needs the final ledger: one POST to its in-cluster bot-process
endpoint when the call is over. The endpoint is idempotent (a bot is billed
once, later reports of a billed bot are ignored), so retrying is safe.

The report only leaves when ``MEETING_BAAS_BOT_PROCESS_URL`` is set, which it
is in the cluster deployments; a local run keeps its usage files and sends
nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from typing import Any

import aiohttp

BOT_PROCESS_URL_ENV = "MEETING_BAAS_BOT_PROCESS_URL"
REPORT_PATH = "/provider-usage"
RETRY_DELAYS_SECS = (1.0, 3.0, 9.0)
REQUEST_TIMEOUT_SECS = 10.0
CLAIM_SUFFIX = ".reported"

# Ledger fields the API takes, name for name.
_COUNT_FIELDS = (
    "tts_characters",
    "tts_requests",
    "llm_turns",
    "llm_prompt_tokens",
    "llm_cached_prompt_tokens",
    "llm_completion_tokens",
    "llm_total_tokens",
)
_NAME_FIELDS = ("stt_provider", "tts_provider", "llm_provider", "llm_model")
_KEY_SOURCES = ("stt", "tts", "llm")


def bot_process_url() -> str | None:
    """Where the API takes bot-process reports, or None when not configured."""
    url = os.getenv(BOT_PROCESS_URL_ENV, "").strip().rstrip("/")
    return url or None


def build_report(usage: dict[str, Any]) -> dict[str, Any] | None:
    """The API's request body for a finalised ledger; None without a bot id.

    Only known fields are copied: the ledger also carries the owner digest and
    anything a future meter adds, none of which the API should see.
    """
    bot_id = usage.get("meetingbaas_bot_id")
    if not bot_id:
        return None
    key_sources = usage.get("key_sources") or {}
    report: dict[str, Any] = {
        "bot_id": bot_id,
        "source": "speaking_bot",
        "key_sources": {
            provider: (
                "caller" if key_sources.get(provider) == "caller" else "platform"
            )
            for provider in _KEY_SOURCES
        },
        "duration_seconds": max(0.0, float(usage.get("duration_seconds") or 0.0)),
        "started_at": _timestamp(usage.get("started_at")),
        "ended_at": _timestamp(usage.get("ended_at")),
        "stt_audio_seconds": max(0.0, float(usage.get("stt_audio_seconds") or 0.0)),
    }
    for field in _NAME_FIELDS:
        value = usage.get(field)
        report[field] = str(value)[:128] if value else None
    for field in _COUNT_FIELDS:
        report[field] = max(0, int(usage.get(field) or 0))
    return report


def _timestamp(value: Any) -> float | None:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp >= 0 else None


async def post_report(
    url: str, report: dict[str, Any], log: Callable[[str], None]
) -> bool:
    """POST the report, retrying server errors; True once the API took it.

    A 404 means the API does not know the bot (nothing to retry); any other
    client error is a bug on one side and is logged, not retried.
    """
    endpoint = f"{url}{REPORT_PATH}"
    bot_id = report["bot_id"]
    attempts = len(RETRY_DELAYS_SECS) + 1
    for attempt in range(attempts):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=report,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS),
                ) as response:
                    if response.status < 300:
                        body = await _json_or_none(response)
                        billing = (body or {}).get("data", {}).get("billing")
                        log(f"[USAGE] reported bot {bot_id} to the API: {billing}")
                        return True
                    if response.status < 500:
                        text = (await response.text())[:200]
                        log(
                            f"[USAGE] API refused the usage report for bot {bot_id} "
                            f"({response.status}): {text}"
                        )
                        return False
                    log(
                        f"[USAGE] API error {response.status} for bot {bot_id}'s "
                        f"usage report (attempt {attempt + 1}/{attempts})"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            log(
                f"[USAGE] could not reach the API for bot {bot_id}'s usage report "
                f"(attempt {attempt + 1}/{attempts}): {error}"
            )
        if attempt < len(RETRY_DELAYS_SECS):
            await asyncio.sleep(RETRY_DELAYS_SECS[attempt])
    return False


async def _json_or_none(response: aiohttp.ClientResponse) -> dict[str, Any] | None:
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def claim_path(usage_directory: str, bot_id: str) -> str:
    return os.path.join(usage_directory, f"{bot_id}{CLAIM_SUFFIX}")


def claim_report(usage_directory: str, bot_id: str) -> bool:
    """Atomically claim the report for a bot; False when one is already on its way.

    Every end-of-call event (bot left, call ended, failed) finalises the
    ledger again; the first claim reports, the rest are no-ops. The claim is
    released when the report fails so the next event tries again.
    """
    os.makedirs(usage_directory, exist_ok=True)
    try:
        fd = os.open(
            claim_path(usage_directory, bot_id),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    os.close(fd)
    return True


def release_claim(usage_directory: str, bot_id: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(claim_path(usage_directory, bot_id))


def schedule_provider_usage_report(
    usage: dict[str, Any],
    *,
    usage_directory: str,
    log: Callable[[str], None],
) -> asyncio.Task[bool] | None:
    """Send a finalised ledger to the API in the background, at most once per bot.

    Returns the task, or None when nothing was scheduled: no endpoint
    configured, no MeetingBaas bot id, or a report already claimed.
    """
    url = bot_process_url()
    if url is None:
        log("[USAGE] no MEETING_BAAS_BOT_PROCESS_URL: usage stays local")
        return None
    report = build_report(usage)
    if report is None:
        return None
    bot_id = report["bot_id"]
    if not claim_report(usage_directory, bot_id):
        return None

    async def _run() -> bool:
        try:
            delivered = await post_report(url, report, log)
        except Exception as error:  # a bookkeeping error must not go unlogged
            log(f"[USAGE] usage report for bot {bot_id} crashed: {error}")
            delivered = False
        if not delivered:
            release_claim(usage_directory, bot_id)
        return delivered

    return asyncio.get_running_loop().create_task(_run())
