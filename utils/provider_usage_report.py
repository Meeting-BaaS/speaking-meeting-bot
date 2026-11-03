"""Report a bot's provider usage to the Meeting BaaS API once the call ends.

The usage ledger (see ``utils.usage_meter``) says what a bot consumed on
speech-to-text, text-to-speech and the language model, and whose key each
ran on. The API bills the part that ran on Meeting BaaS keys as tokens on the
bot, so it needs the final ledger: one POST to its in-cluster bot-process
endpoint when the call is over. The endpoint is idempotent (a bot is billed
once, later reports of a billed bot are ignored), so retrying is safe.

Delivery is tried a few times when the call ends. A ledger the API did not
take (it was down, or unreachable) stays on disk without its claim file, and
the replay loop sends it again later, long after the bot itself is forgotten.
A report the API refused (an unknown bot, a malformed body) keeps its claim:
retrying cannot change that answer.

The report only leaves when ``MEETING_BAAS_BOT_PROCESS_URL`` is set, which it
is in the cluster deployments; a local run keeps its usage files and sends
nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from typing import Any, Literal

import aiohttp

BOT_PROCESS_URL_ENV = "MEETING_BAAS_BOT_PROCESS_URL"
REPORT_PATH = "/provider-usage"
RETRY_DELAYS_SECS = (1.0, 3.0, 9.0)
REQUEST_TIMEOUT_SECS = 10.0
CLAIM_SUFFIX = ".reported"
# How often finalised ledgers the API never took are sent again, and how old
# a ledger may be before it is left alone.
REPLAY_INTERVAL_SECS = 300.0
REPLAY_MAX_AGE_SECS = 7 * 24 * 3600.0

Delivery = Literal["delivered", "refused", "failed"]

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
) -> Delivery:
    """POST the report, retrying server errors.

    Returns "delivered" once the API took it, "refused" when the API answered
    with a client error (404: the API does not know the bot; anything else is
    a bug on one side), which retrying cannot change, and "failed" when the
    API was unreachable or kept answering 5xx: the replay loop tries again.
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
                        return "delivered"
                    if response.status < 500:
                        text = (await response.text())[:200]
                        log(
                            f"[USAGE] API refused the usage report for bot {bot_id} "
                            f"({response.status}): {text}"
                        )
                        return "refused"
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
    return "failed"


async def _json_or_none(response: aiohttp.ClientResponse) -> dict[str, Any] | None:
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def claim_path(usage_directory: str, bot_id: str) -> str:
    """Path of the claim file that marks a bot's report as sent or in flight.

    Args:
        usage_directory: The ``usage/`` directory under the state dir.
        bot_id: The MeetingBaas bot id the ledger is filed under.
    """
    return os.path.join(usage_directory, f"{bot_id}{CLAIM_SUFFIX}")


def claim_report(usage_directory: str, bot_id: str) -> bool:
    """Atomically claim the report for a bot; False when one is already on its way.

    Every end-of-call event (bot left, call ended, failed) finalises the
    ledger again; the first claim reports, the rest are no-ops. The claim is
    released when the API could not be reached so the ledger is sent again.
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
    """Drop a bot's claim so the next end-of-call event or replay sends again.

    Args:
        usage_directory: The ``usage/`` directory under the state dir.
        bot_id: The MeetingBaas bot id the ledger is filed under.
    """
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
            outcome = await post_report(url, report, log)
        except Exception as error:  # a bookkeeping error must not go unlogged
            log(f"[USAGE] usage report for bot {bot_id} crashed: {error}")
            outcome = "failed"
        if outcome == "failed":
            release_claim(usage_directory, bot_id)
        return outcome == "delivered"

    return asyncio.get_running_loop().create_task(_run())


def unreported_ledgers(
    usage_directory: str, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Finalised ledgers in the directory that no delivery has claimed.

    A finalised ledger is filed under its MeetingBaas bot id with ``ended_at``
    set (see ``_finalize_usage`` in the routes). Ledgers older than
    ``REPLAY_MAX_AGE_SECS`` are left alone.
    """
    now = time.time() if now is None else now
    found: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(usage_directory))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        bot_id = name[: -len(".json")]
        if os.path.exists(claim_path(usage_directory, bot_id)):
            continue
        try:
            with open(os.path.join(usage_directory, name)) as handle:
                usage = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(usage, dict) or usage.get("meetingbaas_bot_id") != bot_id:
            continue
        ended_at = _timestamp(usage.get("ended_at"))
        if ended_at is None or now - ended_at > REPLAY_MAX_AGE_SECS:
            continue
        found.append(usage)
    return found


def replay_unreported(
    usage_directory: str, *, log: Callable[[str], None]
) -> list[asyncio.Task[bool]]:
    """Send every finalised ledger the API never took; the tasks started."""
    if bot_process_url() is None:
        return []
    tasks = []
    for usage in unreported_ledgers(usage_directory):
        task = schedule_provider_usage_report(
            usage, usage_directory=usage_directory, log=log
        )
        if task is not None:
            tasks.append(task)
    if tasks:
        log(f"[USAGE] replaying {len(tasks)} unreported usage ledger(s)")
    return tasks


async def replay_loop(
    usage_directory: str,
    *,
    log: Callable[[str], None],
    interval_secs: float = REPLAY_INTERVAL_SECS,
) -> None:
    """Run ``replay_unreported`` every ``interval_secs`` until cancelled."""
    while True:
        await asyncio.sleep(interval_secs)
        try:
            replay_unreported(usage_directory, log=log)
        except Exception as error:  # the loop must outlive one bad directory read
            log(f"[USAGE] replay failed: {error}")
