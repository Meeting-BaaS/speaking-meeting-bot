"""Utilities for handling ngrok URLs and tunnels."""

import os
from typing import List, Optional

import requests
from fastapi import HTTPException, Request

from meetingbaas_pipecat.utils.logger import logger
from utils.url import convert_http_to_ws_url

# Get the configured server port from environment variable, default to 8000
CONFIGURED_PORT = os.getenv("PORT", "8000")

# Global variables for ngrok URL tracking
NGROK_URLS = []
NGROK_URL_INDEX = 0
# Map client IDs to their assigned ngrok URL indexes
NGROK_CLIENT_MAP = {}

# Check for local dev mode marker file (created by the parent process)
LOCAL_DEV_MODE = False
if os.path.exists(".local_dev_mode"):
    with open(".local_dev_mode", "r") as f:
        if f.read().strip().lower() == "true":
            LOCAL_DEV_MODE = True
            logger.info(
                "🚀 Starting in LOCAL_DEV_MODE (detected from .local_dev_mode file)"
            )

# Get base URL from environment variable
BASE_URL = os.environ.get("BASE_URL", None)
if BASE_URL:
    logger.info(f"Using BASE_URL from environment: {BASE_URL}")
    # Convert http to ws or https to wss if needed
    WS_BASE_URL = convert_http_to_ws_url(BASE_URL)
else:
    logger.info(
        "No BASE_URL environment variable found. Will attempt auto-detection or use provided URLs."
    )
    WS_BASE_URL = None


def load_ngrok_urls() -> List[str]:
    """
    Load ngrok URLs using the ngrok API.
    Returns a list of available ngrok URLs with preference for those pointing to port 8766.
    """
    urls = []
    priority_urls = []  # For tunnels pointing to port 8766

    try:
        # Try to fetch active ngrok tunnels from the API
        # ngrok web interface is usually available at localhost:4040
        logger.info("📡 Attempting to fetch ngrok tunnels from API...")
        response = requests.get("http://localhost:4040/api/tunnels")

        if response.status_code == 200:
            data = response.json()
            tunnels = data.get("tunnels", [])

            if tunnels:
                logger.info(f"🔍 Found {len(tunnels)} active ngrok tunnels")

                # Extract public URLs from tunnels
                for tunnel in tunnels:
                    public_url = tunnel.get("public_url")
                    config = tunnel.get("config", {})
                    addr = config.get("addr", "")

                    # Log the tunnel details for debugging
                    logger.info(f"🔍 Tunnel: {public_url} -> {addr}")

                    if public_url and public_url.startswith("https://"):
                        # Check if this tunnel points to the configured port
                        if addr and CONFIGURED_PORT in addr:
                            logger.info(
                                f"✅ Found priority tunnel for port {CONFIGURED_PORT}: {public_url}"
                            )
                            priority_urls.append(public_url)
                        else:
                            urls.append(public_url)
                            logger.info(f"✅ Added regular ngrok tunnel: {public_url}")

                # Use priority URLs first, then regular ones
                if priority_urls:
                    logger.info(
                        f"✅ Using {len(priority_urls)} priority tunnels for port {CONFIGURED_PORT}"
                    )
                    urls = priority_urls + urls

                if not urls:
                    logger.warning("⚠️ Found tunnels but none with HTTPS protocol!")
            else:
                logger.warning(
                    "⚠️ No active ngrok tunnels found. Make sure ngrok is running with 'ngrok start --all'"
                )
        else:
            logger.warning(
                f"⚠️ Failed to get ngrok tunnels. Status code: {response.status_code}"
            )

    except Exception as e:
        logger.error(f"❌ Error accessing ngrok API: {e}")
        logger.info(
            "Make sure ngrok is running with 'ngrok start --all' before starting this server"
        )

    # Log the final URLs we're using
    if urls:
        logger.info(f"📡 Final ngrok URLs to be used: {urls}")
    else:
        logger.warning(
            "⚠️ No ngrok URLs found - websocket connections may not work properly!"
        )

    return urls


def _get_next_ngrok_url(urls: List[str], client_id: str) -> Optional[str]:
    """
    Get the next available ngrok URL.
    Uses a global counter variable to track which URLs have been used.

    Args:
        urls: List of available ngrok URLs
        client_id: Client ID to assign the URL to

    Returns:
        The next available ngrok URL, or None if all are in use
    """
    global NGROK_URL_INDEX, NGROK_CLIENT_MAP

    if not urls:
        return None

    # First check if there are any freed indexes we can reuse
    free_indexes = []
    used_indexes = set(NGROK_CLIENT_MAP.values())
    for i in range(len(urls)):
        if i not in used_indexes and i < NGROK_URL_INDEX:
            free_indexes.append(i)

    if free_indexes:
        # Reuse a freed index
        index = free_indexes[0]
        logger.info(f"Reusing freed ngrok URL index {index} for client {client_id}")
    else:
        # If we've used all URLs, return None
        if NGROK_URL_INDEX >= len(urls):
            logger.warning(f"⚠️ All {len(urls)} ngrok URLs have been assigned!")
            return None

        # Get a new index
        index = NGROK_URL_INDEX
        NGROK_URL_INDEX += 1

    # Assign this index to the client
    NGROK_CLIENT_MAP[client_id] = index

    # Get the URL for this index
    url = urls[index]

    # Convert http to ws for WebSocket
    url = convert_http_to_ws_url(url)

    logger.debug(
        f"Assigned ngrok WebSocket URL: {url} (URL #{index}) to client {client_id}"
    )
    return url


def release_ngrok_url(client_id: str) -> None:
    """
    Release an ngrok URL when a client disconnects.

    Args:
        client_id: The client ID that was using the URL
    """
    global NGROK_CLIENT_MAP

    if client_id in NGROK_CLIENT_MAP:
        index = NGROK_CLIENT_MAP.pop(client_id)
        logger.debug(f"Released ngrok URL index {index} from client {client_id}")


def update_ngrok_client_id(temp_client_id: str, real_client_id: str) -> None:
    """
    Update the ngrok client mapping when a temporary ID is replaced with a real client ID.

    Args:
        temp_client_id: The temporary client ID used during URL assignment
        real_client_id: The actual client ID to use going forward
    """
    global NGROK_CLIENT_MAP

    if temp_client_id in NGROK_CLIENT_MAP:
        # Get the URL index assigned to the temporary ID
        index = NGROK_CLIENT_MAP.pop(temp_client_id)
        # Assign it to the real client ID
        NGROK_CLIENT_MAP[real_client_id] = index
        logger.debug(
            f"Updated ngrok URL mapping from temp ID {temp_client_id} to real client ID {real_client_id}"
        )


def determine_websocket_url(
    request_websocket_url: Optional[str], client_request: Request
) -> tuple[str, Optional[str]]:
    """
    Determine the appropriate WebSocket URL based on the environment and request.
    Uses a cached list of ngrok URLs when in local dev mode.

    Args:
        request_websocket_url: Optional explicit WebSocket URL provided by the client
        client_request: The FastAPI request object

    Returns:
        A tuple containing (websocket_url, temp_client_id)
        temp_client_id will be None if not using ngrok URLs

    Raises:
        HTTPException: If in local dev mode and no ngrok URLs are available
    """
    global NGROK_URLS

    temp_client_id = None

    # 1. If user explicitly provided a URL, use it (highest priority)
    if request_websocket_url:
        logger.info(f"Using user-provided WebSocket URL: {request_websocket_url}")
        return request_websocket_url, temp_client_id

    # 2. If BASE_URL is set in environment, use it
    if WS_BASE_URL:
        logger.info(f"Using WebSocket URL from BASE_URL env: {WS_BASE_URL}")
        return WS_BASE_URL, temp_client_id

    # 3. In local dev mode, try to use ngrok URL
    if LOCAL_DEV_MODE:
        logger.info(
            f"🔍 LOCAL_DEV_MODE is {LOCAL_DEV_MODE}, attempting to get ngrok URL"
        )

        # Use cached ngrok URLs instead of loading them every time
        if not NGROK_URLS:
            logger.info("Loading ngrok URLs (first request)")
            NGROK_URLS = load_ngrok_urls()

        if NGROK_URLS:
            logger.info(f"🔍 Found {len(NGROK_URLS)} ngrok URLs")

            # Generate a temporary client ID from the request info if we don't have a real one yet
            temp_client_id = (
                f"temp-{client_request.client.host}-{client_request.client.port}"
            )

            # Get the next available URL
            ngrok_url = _get_next_ngrok_url(NGROK_URLS, temp_client_id)
            if ngrok_url:
                logger.info(f"Using ngrok WebSocket URL: {ngrok_url}")
                return ngrok_url, temp_client_id
            else:
                # If we're here, we've used all available ngrok URLs
                logger.warning("⚠️ All ngrok URLs have been used")
                # In local dev mode, we should require ngrok URLs
                raise HTTPException(
                    status_code=400,
                    detail="No more ngrok URLs available. Limited to 2 bots in local dev mode.",
                )
        else:
            logger.warning("⚠️ No ngrok URLs found despite being in LOCAL_DEV_MODE")

    # 4. Auto-detect from request (fallback, only for non-local environments)
    host = client_request.headers.get("host", f"localhost:{CONFIGURED_PORT}")
    scheme = client_request.headers.get("x-forwarded-proto", "http")
    websocket_scheme = "wss" if scheme == "https" else "ws"
    auto_url = f"{websocket_scheme}://{host}"
    logger.warning(
        f"⚠️ Using auto-detected WebSocket URL: {auto_url} - This may not work in production. "
        "Consider setting the BASE_URL environment variable."
    )
    return auto_url, temp_client_id


def log_ngrok_status() -> None:
    """
    Log the current status of ngrok URL assignments.
    This is useful for debugging to see which clients are using which URLs.
    """
    global NGROK_URLS, NGROK_CLIENT_MAP, NGROK_URL_INDEX

    if not NGROK_URLS:
        logger.info("No ngrok URLs loaded")
        return

    logger.info(
        f"Ngrok URL Status: {len(NGROK_URLS)} total URLs, next index: {NGROK_URL_INDEX}"
    )
    logger.info(f"Currently assigned: {len(NGROK_CLIENT_MAP)} URLs to clients")

    for client_id, index in NGROK_CLIENT_MAP.items():
        try:
            url = NGROK_URLS[index]
            ws_url = convert_http_to_ws_url(url)
            logger.info(f"Client {client_id}: using URL index {index} -> {ws_url}")
        except IndexError:
            logger.error(f"Client {client_id} mapped to invalid index {index}")

    # Show available indexes
    used_indexes = set(NGROK_CLIENT_MAP.values())
    available = [i for i in range(len(NGROK_URLS)) if i not in used_indexes]

    if available:
        logger.info(f"Available indexes: {available}")
    else:
        logger.info("No URLs available for assignment")
