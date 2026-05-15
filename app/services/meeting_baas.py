# app/services/meeting_baas.py

import os
from typing import Any, Dict, Optional

import aiohttp
from dotenv import load_dotenv

from app.utils.pipecat_logger import logger

load_dotenv()

_BAAS_BASE = "https://api.meetingbaas.com"


def _headers(api_key: Optional[str] = None) -> Dict[str, str]:
    key = api_key or os.getenv("MEETING_BAAS_API_KEY", "")
    return {
        "x-meeting-baas-api-key": key,
        "Content-Type": "application/json",
    }


async def create_meeting_bot(
    *,
    meeting_url: str,
    websocket_url: str,
    bot_name: str,
    bot_image: Optional[str] = None,
    entry_message: Optional[str] = None,
    webhook_url: Optional[str] = None,
    streaming_audio_frequency: str = "16khz",
    extra: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """
    Call the MeetingBaas /bots endpoint to create a new bot.

    Returns the MeetingBaas bot_id string on success, None on failure.
    """
    import uuid
    payload: Dict[str, Any] = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "deduplication_key": str(uuid.uuid4()),
        "streaming": {
            "input": websocket_url,
            "output": websocket_url,
            "audio_frequency": streaming_audio_frequency,
        }
    }
    if bot_image:
        payload["bot_image"] = bot_image
    if entry_message:
        payload["entry_message"] = entry_message
    if webhook_url:
        payload["webhook_url"] = webhook_url
    if extra:
        payload["extra"] = extra

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_BAAS_BASE}/bots",
                json=payload,
                headers=_headers(api_key),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    data: Dict[str, Any] = await resp.json()
                    bot_id: Optional[str] = data.get("bot_id") or data.get("id")
                    logger.info(f"[MeetingBaaS] Created bot {bot_id} for {meeting_url}")
                    return bot_id
                body = await resp.text()
                logger.error(
                    f"[MeetingBaaS] create_meeting_bot failed {resp.status}: {body}"
                )
                return None
    except Exception as exc:
        logger.error(f"[MeetingBaaS] create_meeting_bot exception: {exc}")
        return None


async def stop_bot(bot_id: str, api_key: Optional[str] = None) -> bool:
    """
    Ask MeetingBaas to remove the bot from the meeting.

    Returns True on success.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{_BAAS_BASE}/bots/{bot_id}",
                headers=_headers(api_key),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                ok = resp.status in (200, 204)
                if ok:
                    logger.info(f"[MeetingBaaS] Stopped bot {bot_id}")
                else:
                    body = await resp.text()
                    logger.error(
                        f"[MeetingBaaS] stop_bot {bot_id} failed {resp.status}: {body}"
                    )
                return ok
    except Exception as exc:
        logger.error(f"[MeetingBaaS] stop_bot exception: {exc}")
        return False


async def list_bots(api_key: Optional[str] = None) -> list[Dict[str, Any]]:
    """
    Fetch a list of all current bots from MeetingBaas.

    Returns an empty list on failure.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_BAAS_BASE}/bots",
                headers=_headers(api_key),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data if isinstance(data, list) else data.get("bots", [])
                body = await resp.text()
                logger.error(
                    f"[MeetingBaaS] list_bots failed {resp.status}: {body}"
                )
                return []
    except Exception as exc:
        logger.error(f"[MeetingBaaS] list_bots exception: {exc}")
        return []
