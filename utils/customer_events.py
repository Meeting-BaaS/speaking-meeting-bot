"""Events a bot posts to the caller's ``callback_url`` while it works.

MeetingBaas' own webhooks say when the bot joined, left or failed. The
caller of this API also wants to know what the bot *said*, when it needed
its owner, and what it understood once the call was over. Those leave here,
one HTTP POST per event, signed with the caller's ``callback_secret`` when
one was given. Delivery is best effort with a few retries; the transcript
and usage files remain the record if an endpoint was down.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp

from utils.ssrf import build_pinned_connector, is_private_ip

SIGNATURE_HEADER = "x-speaking-bot-signature"
EVENT_HEADER = "x-speaking-bot-event"
RETRY_DELAYS_SECS = (1.0, 3.0, 9.0)
REQUEST_TIMEOUT_SECS = 10.0

EVENT_UTTERANCE = "speaking_bot.utterance"
EVENT_ESCALATION = "speaking_bot.escalation"
EVENT_OWNER_MESSAGE_DELIVERED = "speaking_bot.owner_message_delivered"
EVENT_ENDED = "speaking_bot.ended"


def private_urls_allowed() -> bool:
    return os.getenv("CALLBACK_ALLOW_PRIVATE_URLS", "").strip().lower() in {"1", "true", "yes"}


class CallbackRefused(ValueError):
    """The callback host is private, local or unresolvable."""


async def resolve_callback(url: str) -> list[str] | None:
    """Addresses the callback may be sent to, or None when any address is fine.

    Mirrors the prompt-data and MCP checks: a literal private IP is refused
    outright, a hostname is resolved here and every address checked, and the
    returned list pins the connection so the request goes to a checked
    address and nothing else.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        raise CallbackRefused(f"callback_url is not an http(s) URL: {url}")
    if private_urls_allowed():
        return None
    try:
        if is_private_ip(host):
            raise CallbackRefused(f"callback host is private or local: {host}")
        return [host]
    except ValueError as error:
        if isinstance(error, CallbackRefused):
            raise
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as error:
        raise CallbackRefused(f"could not resolve callback host {host}: {error}") from error
    resolved: list[str] = []
    for address in addresses:
        ip = address[4][0]
        if is_private_ip(ip):
            raise CallbackRefused(f"callback host {host} resolves to a private address")
        if ip not in resolved:
            resolved.append(ip)
    return resolved


async def open_session(url: str) -> aiohttp.ClientSession:
    """A session whose connections to ``url`` are pinned to checked addresses."""
    connector = build_pinned_connector(aiohttp, url, await resolve_callback(url))
    return aiohttp.ClientSession(connector=connector)


def sign_body(secret: str, body: bytes) -> str:
    """``sha256=<hex>`` HMAC of the exact bytes sent, so receivers can verify."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    if not header_value:
        return False
    return hmac.compare_digest(sign_body(secret, body), header_value)


def build_event(
    event: str,
    data: dict[str, Any],
    *,
    bot_id: str,
    meetingbaas_bot_id: str | None = None,
) -> dict[str, Any]:
    """The envelope every event is wrapped in."""
    return {
        "event": event,
        "event_id": str(uuid.uuid4()),
        "bot_id": meetingbaas_bot_id or bot_id,
        "client_id": bot_id,
        "created_at": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def post_event(
    url: str,
    envelope: dict[str, Any],
    *,
    secret: str | None = None,
    session: aiohttp.ClientSession | None = None,
    retry_delays: tuple[float, ...] = RETRY_DELAYS_SECS,
    log=None,
) -> bool:
    """POST one event, retrying on network errors and 5xx. True when accepted."""
    body = json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")
    headers = {
        "content-type": "application/json",
        EVENT_HEADER: str(envelope.get("event", "")),
    }
    if secret:
        headers[SIGNATURE_HEADER] = sign_body(secret, body)

    own_session = session is None
    if own_session:
        try:
            session = await open_session(url)
        except CallbackRefused as error:
            if log:
                log(f"[EVENTS] {envelope.get('event')} not sent: {error}")
            return False
    try:
        for attempt in range(len(retry_delays) + 1):
            try:
                async with session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS),
                    # A redirect would resend the signed body elsewhere; a 3xx
                    # is therefore a failed delivery, not a hop to follow.
                    allow_redirects=False,
                ) as response:
                    if response.status < 500:
                        accepted = 200 <= response.status < 300
                        if not accepted and log:
                            log(f"[EVENTS] {envelope.get('event')} not accepted ({response.status})")
                        return accepted
                    if log:
                        log(f"[EVENTS] {envelope.get('event')} got {response.status}, retrying")
            except (TimeoutError, aiohttp.ClientError) as error:
                if log:
                    log(f"[EVENTS] {envelope.get('event')} failed: {error}")
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
        return False
    finally:
        if own_session:
            await session.close()


class CustomerEventSink:
    """Fire-and-forget event delivery for one bot.

    ``emit`` returns at once; delivery runs in a background task so a slow
    endpoint never holds up speech. ``close`` waits for what is still in
    flight, bounded, so the final events of a call get out before the
    process does.
    """

    def __init__(
        self,
        url: str | None,
        secret: str | None = None,
        *,
        bot_id: str,
        meetingbaas_bot_id: str | None = None,
        log=None,
    ):
        self.url = (url or "").strip() or None
        self.secret = (secret or "").strip() or None
        self.bot_id = bot_id
        self.meetingbaas_bot_id = meetingbaas_bot_id
        self._log = log
        self._tasks: set[asyncio.Task] = set()
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def envelope(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        return build_event(
            event, data, bot_id=self.bot_id, meetingbaas_bot_id=self.meetingbaas_bot_id
        )

    async def emit(self, event: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Schedule delivery of one event; returns its envelope, or None if off."""
        if not self.enabled:
            return None
        envelope = self.envelope(event, data)
        if self._session is None or self._session.closed:
            try:
                self._session = await open_session(self.url)
            except CallbackRefused as error:
                if self._log:
                    self._log(f"[EVENTS] callback disabled: {error}")
                self.url = None
                return None
        task = asyncio.create_task(
            post_event(
                self.url, envelope, secret=self.secret, session=self._session, log=self._log
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return envelope

    async def send(self, event: str, data: dict[str, Any]) -> bool:
        """Deliver one event and wait for the outcome (end-of-call use)."""
        if not self.enabled:
            return False
        return await post_event(
            self.url, self.envelope(event, data), secret=self.secret, log=self._log
        )

    async def close(self, timeout: float = 15.0) -> None:
        """Wait for in-flight deliveries, then drop the HTTP session."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)
        if self._session is not None and not self._session.closed:
            await self._session.close()
