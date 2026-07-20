"""Shared SSRF-hardening primitives for outbound HTTP.

Centralizes the DNS-pinning resolver, the private/local IP check, and the
pinned aiohttp connector builder so the two call sites — ``utils/mcp_client.py``
(live MCP tool execution) and ``app/services/prompt_context.py`` (external
prompt data sources) — cannot drift apart on security-critical logic.

Each caller keeps its own URL-validation wrapper: the exception type, HTTP
status code, and allow-private-URL bypass policy differ by feature, so those
stay next to the code that owns them. Only the caller-agnostic mechanics live
here.
"""

from __future__ import annotations

import socket
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse


class PinnedResolver:
    """aiohttp resolver that only returns pre-validated addresses for one host."""

    def __init__(self, addresses_by_host: dict[str, list[str]]):
        self._addresses_by_host = addresses_by_host

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        addresses = self._addresses_by_host.get(host)
        if not addresses:
            raise OSError(f"Host {host} was not pre-validated")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in addresses
        ]

    async def close(self) -> None:
        return None


def is_private_ip(value: str) -> bool:
    """True if the literal IP is private, loopback, link-local, etc.

    Raises ValueError (from ip_address) if value is not a literal IP — callers
    that pass hostnames must handle that.
    """
    parsed = ip_address(value)
    return (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def build_pinned_connector(
    aiohttp_module: Any,
    url: str,
    resolved_ips: list[str] | None,
) -> Any | None:
    """Build an aiohttp TCPConnector pinned to pre-validated IPs, or None."""
    if not resolved_ips or not hasattr(aiohttp_module, "TCPConnector"):
        return None
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    return aiohttp_module.TCPConnector(
        resolver=PinnedResolver({parsed.hostname: resolved_ips}),
        ttl_dns_cache=0,
    )
