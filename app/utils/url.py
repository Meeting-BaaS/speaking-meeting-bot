"""URL manipulation utilities."""


def convert_http_to_ws_url(url: str) -> str:
    """
    Convert HTTP(S) URL to WS(S) URL.

    Args:
        url: HTTP or HTTPS URL to convert

    Returns:
        WebSocket URL (ws:// or wss://)
    """
    if url.startswith("http://"):
        return "ws://" + url[7:]
    elif url.startswith("https://"):
        return "wss://" + url[8:]
    return url  # Already a WS URL or other format
