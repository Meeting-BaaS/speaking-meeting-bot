"""Hand a bot to the pod that owns its meeting, or ask the other pods about a
bot this one does not host. Only active with several replicas; a request that
already carries the forwarded header is never forwarded again."""

import asyncio

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from meetingbaas_pipecat.utils.logger import logger
from utils.pod_routing import (
    FORWARDED_HEADER,
    owner_ordinal,
    peer_base_url,
    peer_ordinals,
    pod_ordinal,
    replica_count,
)

# A dead pod must show up as a connection failure within seconds, so the
# request can be hosted here; the total covers persona extraction and the
# MeetingBaas call on the owner.
FORWARD_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=5)


# Failures before anything was sent: the peer refused, or the sock_connect
# limit expired while connecting. Checked by name because the connect-timeout
# class is a subclass of ServerTimeoutError and must not be confused with a
# read timeout after delivery.
_PRE_DELIVERY_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectorError,
    ConnectionRefusedError,
    getattr(aiohttp, "ConnectionTimeoutError", aiohttp.ClientConnectorError),
)


def never_reached_peer(error: BaseException) -> bool:
    """True when the request could not even be delivered to the peer.

    Only then is hosting the bot here safe: after a read timeout or a dropped
    connection the owner may still finish creating the bot, and a second one
    on this pod would double the customer's bots and bill.
    """
    return isinstance(error, _PRE_DELIVERY_ERRORS)


def _peer_unsure_response(what: str) -> JSONResponse:
    """A peer that may hold the bot did not answer: not a 404, an error to retry."""
    return JSONResponse(
        content={
            "message": f"A pod that may host this bot did not answer; {what} could not be confirmed. Try again.",
            "status": "error",
        },
        status_code=502,
    )


async def _payload(resp: aiohttp.ClientResponse) -> tuple[dict | list, int]:
    """The peer's JSON body and the status to relay.

    An error the peer sent keeps its status. A success whose body cannot be
    read (truncated, HTML from a proxy) is relayed as a 502: a 201 without the
    bot id in it would leave the caller with a bot it cannot manage.
    """
    try:
        return await resp.json(content_type=None), resp.status
    except (ValueError, aiohttp.ClientPayloadError):
        status = resp.status if resp.status >= 400 else 502
        return (
            {"message": f"peer answered {resp.status} without a readable JSON body", "status": "error"},
            status,
        )


def is_forwarded(request: Request) -> bool:
    return request.headers.get(FORWARDED_HEADER) == "1"


def _enabled(request: Request) -> bool:
    return (
        not is_forwarded(request) and replica_count() > 1 and pod_ordinal() is not None
    )


def owner_for(meeting_url: str) -> int | None:
    """Ordinal of the pod that must host this meeting; None when it is this one."""
    owner = owner_ordinal(meeting_url)
    return None if owner == pod_ordinal() else owner


def _headers(request: Request) -> dict:
    return {
        FORWARDED_HEADER: "1",
        "Content-Type": "application/json",
        "x-meeting-baas-api-key": request.state.api_key,
    }


async def forward_bot_request(
    meeting_url: str, body: bytes, request: Request
) -> Response | None:
    """POST /bots on the owner pod. None means: host the bot here."""
    if not _enabled(request):
        return None
    owner = owner_for(meeting_url)
    if owner is None:
        return None
    base = peer_base_url(owner)
    if not base:
        return None
    try:
        async with aiohttp.ClientSession(timeout=FORWARD_TIMEOUT) as session:
            async with session.post(
                f"{base}/bots", data=body, headers=_headers(request)
            ) as resp:
                if resp.status == 503:
                    logger.warning(
                        f"Owner pod {owner} is draining; hosting the bot for {meeting_url} here"
                    )
                    return None
                payload, status = await _payload(resp)
                logger.info(f"Forwarded bot for {meeting_url} to pod {owner} ({resp.status})")
                return JSONResponse(content=payload, status_code=status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        if never_reached_peer(e):
            logger.warning(f"Owner pod {owner} unreachable ({e}); hosting the bot here")
            return None
        # The owner may still have created the bot after we stopped waiting,
        # so this is not "safe to retry": the caller must look before sending
        # another (MeetingBaas refuses a second bot on the same meeting URL
        # unless the request asks for one).
        logger.warning(f"Owner pod {owner} did not answer ({e}); outcome unknown")
        return JSONResponse(
            content={
                "message": (
                    "The pod that owns this meeting did not answer and may still have "
                    "created the bot. List your bots for this meeting URL before "
                    "sending another."
                ),
                "status": "error",
            },
            status_code=504 if isinstance(e, asyncio.TimeoutError) else 502,
        )


async def forward_leave_request(
    bot_id: str, body: bytes, request: Request
) -> Response | None:
    """DELETE /bots/{id} on every other pod; the one hosting the bot answers.

    None only when every peer said 404. A peer that failed to answer may be
    the one hosting the bot, so that becomes a 502 rather than a not-found.
    """
    return await forward_scoped_request("DELETE", f"/bots/{bot_id}", body, request)


async def forward_scoped_request(
    method: str, path: str, body: bytes | None, request: Request
) -> Response | None:
    """A per-bot request (messages, escalations, usage, briefing) on every other pod.

    The bot's state lives on the pod that hosts it; a pod that does not know
    the bot answers 404 and the next one is asked. None when every pod said
    404 (or forwarding is off). A peer that did not answer at all may be the
    one hosting the bot, and for a POST may even have acted before the
    connection failed, so an unanswered lookup is a 502, never a not-found.
    """
    if not _enabled(request):
        return None
    unsure: list[int] = []
    try:
        async with aiohttp.ClientSession(timeout=FORWARD_TIMEOUT) as session:
            for ordinal in peer_ordinals():
                base = peer_base_url(ordinal)
                if not base:
                    continue
                try:
                    async with session.request(
                        method, f"{base}{path}", data=body, headers=_headers(request)
                    ) as resp:
                        if resp.status == 404:
                            continue
                        payload, status = await _payload(resp)
                        logger.info(f"Pod {ordinal} answered {method} {path} ({resp.status})")
                        return JSONResponse(content=payload, status_code=status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"Pod {ordinal} did not answer {method} {path}: {e}")
                    unsure.append(ordinal)
    except aiohttp.ClientError as e:
        logger.warning(f"Peer lookup for {method} {path} failed: {e}")
        return _peer_unsure_response(f"{method} {path}")
    if unsure:
        return _peer_unsure_response(f"{method} {path}")
    return None
