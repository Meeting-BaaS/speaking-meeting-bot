"""Runtime helpers for ports, public URLs, and CORS configuration."""

import os
from typing import TYPE_CHECKING, List

from utils.url import convert_http_to_ws_url, convert_ws_to_http_url

if TYPE_CHECKING:
    from fastapi import Request


DEFAULT_PORT = 7014


def get_state_dir() -> str:
    """Writable directory for runtime artifacts (transcripts, ready signals,
    call summaries, temp images).

    Defaults to the repo root, which is writable when running from a checkout.
    Set SPEAKING_BOT_STATE_DIR when the source lives in the read-only
    /nix/store (flake-pinned deployment) — every runtime write must land
    outside the store.
    """
    return os.getenv("SPEAKING_BOT_STATE_DIR") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )


def get_server_port(default: int = DEFAULT_PORT) -> int:
    """Return the configured server port, falling back to the default."""
    try:
        return int(os.getenv("PORT", str(default)))
    except ValueError:
        return default


def get_internal_pipecat_ws_url(client_id: str, default_port: int = DEFAULT_PORT) -> str:
    """Return the internal Pipecat websocket URL for a client."""
    return f"ws://localhost:{get_server_port(default_port)}/pipecat/{client_id}"


def build_public_base_url(
    request: "Request", configured_base_url: str | None = None
) -> str:
    """Resolve the public HTTP base URL for callbacks and docs."""
    if configured_base_url:
        return convert_ws_to_http_url(convert_http_to_ws_url(configured_base_url))

    host = request.headers.get("host", f"localhost:{get_server_port()}")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme or "http")
    if scheme in {"ws", "wss"}:
        return convert_ws_to_http_url(f"{scheme}://{host}")
    return f"{scheme}://{host}"


def parse_cors_origins(raw_origins: str | None) -> List[str]:
    """Parse a comma-separated origins string into a normalized list."""
    if not raw_origins:
        return ["*"]

    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]
