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


def never_reached_peer(error: BaseException) -> bool:
    """True when the request could not even be delivered to the peer.

    Only then is hosting the bot here safe: after a timeout or a dropped
    connection the owner may still finish creating the bot, and a second one
    on this pod would double the customer's bots and bill.
    """
    return isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError))


async def _payload(resp: aiohttp.ClientResponse) -> dict | list:
    """The peer's JSON body, or a generic error body when it sent none."""
    try:
        return await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientPayloadError):
        return {"message": f"peer answered {resp.status} without a JSON body", "status": "error"}


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
                payload = await _payload(resp)
                logger.info(f"Forwarded bot for {meeting_url} to pod {owner} ({resp.status})")
                return JSONResponse(content=payload, status_code=resp.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        if never_reached_peer(e):
            logger.warning(f"Owner pod {owner} unreachable ({e}); hosting the bot here")
            return None
        logger.warning(f"Owner pod {owner} did not answer ({e}); asking the caller to retry")
        return JSONResponse(
            content={
                "message": "The pod that owns this meeting did not answer; retry the request",
                "status": "error",
            },
            status_code=504 if isinstance(e, asyncio.TimeoutError) else 502,
        )


async def forward_leave_request(
    bot_id: str, body: bytes, request: Request
) -> Response | None:
    """DELETE /bots/{id} on every other pod; the one hosting the bot answers."""
    if not _enabled(request):
        return None
    try:
        async with aiohttp.ClientSession(timeout=FORWARD_TIMEOUT) as session:
            for ordinal in peer_ordinals():
                base = peer_base_url(ordinal)
                if not base:
                    continue
                try:
                    async with session.delete(
                        f"{base}/bots/{bot_id}", data=body, headers=_headers(request)
                    ) as resp:
                        if resp.status == 404:
                            continue
                        payload = await _payload(resp)
                        logger.info(f"Pod {ordinal} removed bot {bot_id} ({resp.status})")
                        return JSONResponse(content=payload, status_code=resp.status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"Pod {ordinal} unreachable while removing {bot_id}: {e}")
    except aiohttp.ClientError as e:
        logger.warning(f"Peer lookup for {bot_id} failed: {e}")
    return None
