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

FORWARD_TIMEOUT = aiohttp.ClientTimeout(total=30)


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
                payload = await resp.json(content_type=None)
                logger.info(f"Forwarded bot for {meeting_url} to pod {owner} ({resp.status})")
                return JSONResponse(content=payload, status_code=resp.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Owner pod {owner} unreachable ({e}); hosting the bot here")
        return None


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
                        payload = await resp.json(content_type=None)
                        logger.info(f"Pod {ordinal} removed bot {bot_id} ({resp.status})")
                        return JSONResponse(content=payload, status_code=resp.status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"Pod {ordinal} unreachable while removing {bot_id}: {e}")
    except aiohttp.ClientError as e:
        logger.warning(f"Peer lookup for {bot_id} failed: {e}")
    return None
