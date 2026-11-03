"""Report a bot's provider usage to the Meeting BaaS API once the call ends.

The usage ledger (see ``utils.usage_meter``) says what a bot consumed on
speech-to-text, text-to-speech and the language model, and whose key each
ran on. The API bills the part that ran on Meeting BaaS keys as tokens on the
bot, so it needs the final ledger: one POST to its in-cluster bot-process
endpoint when the call is over. The endpoint is idempotent (a bot is billed
once, later reports of a billed bot are ignored), so sending twice is safe.

Two marker files next to a ledger say where its report stands. ``<bot>.sent``
is permanent: the API took the report, or refused it with an answer that will
not change (an unknown bot, a malformed body). ``<bot>.inflight`` exists only
while a delivery runs, is removed when it ends however it ends, and is ignored
once stale (a process that died mid-delivery). A finalised ledger with neither
is replayed by the loop started at app startup, long after the bot itself is
forgotten.

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
CLAIM_SUFFIX = ".inflight"
SENT_SUFFIX = ".sent"
# A delivery takes under a minute (four attempts of ten seconds plus the
# delays); an in-flight claim older than this belongs to a process that died.
STALE_CLAIM_SECS = 15 * 60.0
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
    """POST the report, retrying server errors and rate limits.

    Returns "delivered" once the API took it, "refused" when the API answered
    with a client error that retrying cannot change (404: the API does not
    know the bot; anything else is a bug on one side), and "failed" when the
    API was unreachable, rate limited us, or kept answering 5xx: the replay
    loop tries again later.
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
                    if response.status < 500 and response.status != 429:
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
    """Path of the marker that says a bot's report is in flight right now.

    Args:
        usage_directory: The ``usage/`` directory under the state dir.
        bot_id: The MeetingBaas bot id the ledger is filed under.
    """
    return os.path.join(usage_directory, f"{bot_id}{CLAIM_SUFFIX}")


def sent_path(usage_directory: str, bot_id: str) -> str:
    """Path of the permanent marker that says a bot's report is done.

    Args:
        usage_directory: The ``usage/`` directory under the state dir.
        bot_id: The MeetingBaas bot id the ledger is filed under.
    """
    return os.path.join(usage_directory, f"{bot_id}{SENT_SUFFIX}")


def _is_stale(path: str, now: float) -> bool:
    try:
        return now - os.path.getmtime(path) > STALE_CLAIM_SECS
    except OSError:
        return False


def claim_report(
    usage_directory: str, bot_id: str, *, now: float | None = None
) -> bool:
    """Atomically claim the delivery of a bot's report; False when it is done or running.

    Every end-of-call event (bot left, call ended, failed) finalises the
    ledger again; the first claim delivers, the rest are no-ops. A claim left
    by a process that died mid-delivery is stale after ``STALE_CLAIM_SECS``
    and is taken over: the API is idempotent, so a second delivery is safe.
    """
    if os.path.exists(sent_path(usage_directory, bot_id)):
        return False
    os.makedirs(usage_directory, exist_ok=True)
    path = claim_path(usage_directory, bot_id)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not _is_stale(path, time.time() if now is None else now):
                return False
            release_claim(usage_directory, bot_id)
            continue
        os.close(fd)
        return True
    return False


def release_claim(usage_directory: str, bot_id: str) -> None:
    """Drop a bot's in-flight claim so a later event or the replay loop may deliver again.

    Args:
        usage_directory: The ``usage/`` directory under the state dir.
        bot_id: The MeetingBaas bot id the ledger is filed under.
    """
    with contextlib.suppress(OSError):
        os.remove(claim_path(usage_directory, bot_id))


def mark_sent(usage_directory: str, bot_id: str) -> None:
    """Record that a bot's report is done: delivered, or refused for good.

    Args:
        usage_directory: The ``usage/`` directory under the state dir.
        bot_id: The MeetingBaas bot id the ledger is filed under.
    """
    with contextlib.suppress(OSError):
        with open(sent_path(usage_directory, bot_id), "w") as handle:
            handle.write(str(time.time()))


def schedule_provider_usage_report(
    usage: dict[str, Any],
    *,
    usage_directory: str,
    log: Callable[[str], None],
) -> asyncio.Task[bool] | None:
    """Send a finalised ledger to the API in the background, at most once per bot.

    Returns the task, or None when nothing was scheduled: no endpoint
    configured, no MeetingBaas bot id, a report already done, or one in
    flight. The in-flight claim is released however the delivery ends,
    cancellation included; a delivered or refused report is marked done.
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
        outcome: Delivery = "failed"
        try:
            outcome = await post_report(url, report, log)
        except Exception as error:  # a bookkeeping error must not go unlogged
            log(f"[USAGE] usage report for bot {bot_id} crashed: {error}")
        finally:
            if outcome != "failed":
                mark_sent(usage_directory, bot_id)
            release_claim(usage_directory, bot_id)
        return outcome == "delivered"

    return asyncio.get_running_loop().create_task(_run())


def unreported_ledgers(
    usage_directory: str, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Finalised ledgers in the directory whose report is neither done nor running.

    A finalised ledger is filed under its MeetingBaas bot id with ``ended_at``
    set (see ``_finalize_usage`` in the routes). Ledgers older than
    ``REPLAY_MAX_AGE_SECS`` are left alone. Blocking: call it off the event
    loop.
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
        if os.path.exists(sent_path(usage_directory, bot_id)):
            continue
        claim = claim_path(usage_directory, bot_id)
        if os.path.exists(claim) and not _is_stale(claim, now):
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


async def replay_unreported(
    usage_directory: str, *, log: Callable[[str], None]
) -> list[asyncio.Task[bool]]:
    """Send every finalised ledger the API never took; the tasks started.

    The directory scan runs in a worker thread: it grows with the number of
    bots the pod has run and must not stall the event loop.
    """
    if bot_process_url() is None:
        return []
    ledgers = await asyncio.to_thread(unreported_ledgers, usage_directory)
    tasks = []
    for usage in ledgers:
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
            await replay_unreported(usage_directory, log=log)
        except Exception as error:  # the loop must outlive one bad directory read
            log(f"[USAGE] replay failed: {error}")
