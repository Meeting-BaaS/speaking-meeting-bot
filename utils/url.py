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


def convert_ws_to_http_url(url: str) -> str:
    """
    Convert WS(S) URL to HTTP(S) URL.

    Args:
        url: WebSocket URL to convert

    Returns:
        HTTP URL (http:// or https://)
    """
    if url.startswith("ws://"):
        return "http://" + url[5:]
    elif url.startswith("wss://"):
        return "https://" + url[6:]
    return url
