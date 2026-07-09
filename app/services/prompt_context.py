"""Load and format external prompt context under a token budget."""

import asyncio
import math
import os
import socket
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


CHARS_PER_TOKEN = 4
DEFAULT_SOURCE_MAX_BYTES = 1_000_000


class _PinnedResolver:
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


class PromptContextError(Exception):
    """Raised when external prompt context cannot be loaded."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class PromptContextResult:
    block: str
    sources: list[dict[str, Any]]
    estimated_tokens: int


def estimate_tokens(text: str) -> int:
    """Cheap token estimate good enough for bounding prompt context."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def truncate_to_token_limit(text: str, token_limit: int) -> tuple[str, bool]:
    """Truncate text to an approximate token limit."""
    if token_limit <= 0:
        return "", bool(text)

    max_chars = token_limit * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text, False

    suffix = "\n\n[truncated to prompt_data_token_limit]"
    allowed = max(0, max_chars - len(suffix))
    return f"{text[:allowed].rstrip()}{suffix}", True


def _section_header(index: int, name: str, source_type: str, url: str | None) -> str:
    heading = f"Source {index}: {name} ({source_type})"
    if url:
        heading += f"\nURL: {url}"
    return heading


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _dump_source(source: Any) -> dict[str, Any]:
    if hasattr(source, "model_dump"):
        return source.model_dump(exclude_none=True)
    if isinstance(source, Mapping):
        return {k: v for k, v in source.items() if v is not None}
    return {
        key: getattr(source, key)
        for key in ("name", "type", "text", "url", "headers", "token_limit")
        if getattr(source, key, None) is not None
    }


async def _fetch_url_source(source: Any) -> str:
    import aiohttp

    url = _get(source, "url")
    resolved_ips = await _validate_fetch_url(url)
    headers = _get(source, "headers") or {}
    max_bytes = int(os.getenv("PROMPT_DATA_SOURCE_MAX_BYTES", DEFAULT_SOURCE_MAX_BYTES))

    timeout = aiohttp.ClientTimeout(total=12)
    connector = _build_pinned_connector(aiohttp, url, resolved_ips)
    session_kwargs: dict[str, Any] = {"timeout": timeout}
    if connector is not None:
        session_kwargs["connector"] = connector
    try:
        async with aiohttp.ClientSession(**session_kwargs) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                if 300 <= resp.status < 400:
                    raise PromptContextError(
                        f"Prompt data source '{url}' returned a redirect; redirects are not followed",
                        status_code=400,
                    )
                if resp.status >= 400:
                    raise PromptContextError(
                        f"Prompt data source '{url}' returned HTTP {resp.status}",
                        status_code=502,
                    )
                body = await resp.content.read(max_bytes + 1)
    except PromptContextError:
        raise
    except Exception as e:
        raise PromptContextError(
            f"Could not fetch prompt data source '{url}': {e}",
            status_code=502,
        ) from e

    if len(body) > max_bytes:
        body = body[:max_bytes]

    return body.decode("utf-8", errors="replace")


def _private_urls_allowed() -> bool:
    return os.getenv("PROMPT_DATA_ALLOW_PRIVATE_URLS", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _build_pinned_connector(
    aiohttp_module: Any,
    url: str,
    resolved_ips: list[str] | None,
) -> Any | None:
    if not resolved_ips or not hasattr(aiohttp_module, "TCPConnector"):
        return None
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    return aiohttp_module.TCPConnector(
        resolver=_PinnedResolver({parsed.hostname: resolved_ips}),
        ttl_dns_cache=0,
    )


def _is_private_ip(value: str) -> bool:
    parsed = ip_address(value)
    return (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


async def _validate_fetch_url(url: str) -> list[str] | None:
    """Block SSRF targets and return DNS answers pinned into aiohttp."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PromptContextError(
            f"Invalid prompt data source URL: {url}",
            status_code=400,
        )

    if _private_urls_allowed():
        return None

    host = parsed.hostname
    try:
        if _is_private_ip(host):
            raise PromptContextError(
                f"Prompt data source URL host is private or local: {host}",
                status_code=400,
            )
        return [host]
    except ValueError:
        pass

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise PromptContextError(
            f"Could not resolve prompt data source host '{host}': {e}",
            status_code=400,
        ) from e

    resolved_ips = []
    for address in addresses:
        resolved_ip = address[4][0]
        if _is_private_ip(resolved_ip):
            raise PromptContextError(
                f"Prompt data source URL resolves to private or local address: {host}",
                status_code=400,
            )
        if resolved_ip not in resolved_ips:
            resolved_ips.append(resolved_ip)
    return resolved_ips


async def _load_source_text(source: Any) -> str:
    source_type = _get(source, "type")
    if source_type == "text":
        return _get(source, "text") or ""
    if source_type == "url":
        return await _fetch_url_source(source)
    raise PromptContextError(f"Unsupported prompt data source type: {source_type}")


async def load_prompt_context(
    sources: Sequence[Any] | None,
    total_token_limit: int,
) -> PromptContextResult:
    """Load sources, truncate to budget, and return prompt-ready context."""
    if not sources or total_token_limit <= 0:
        return PromptContextResult(block="", sources=[], estimated_tokens=0)

    remaining_tokens = total_token_limit
    loaded_sources: list[dict[str, Any]] = []
    sections: list[str] = []

    for index, source in enumerate(sources, start=1):
        if remaining_tokens <= 0:
            break

        name = _get(source, "name") or f"source_{index}"
        source_type = _get(source, "type")
        raw_text = (await _load_source_text(source)).strip()
        source_limit = _get(source, "token_limit")
        effective_limit = min(remaining_tokens, int(source_limit or remaining_tokens))
        header = _section_header(index, name, source_type, _get(source, "url"))
        header_tokens = estimate_tokens(f"{header}\n")
        content_limit = max(0, effective_limit - header_tokens)
        text, truncated = truncate_to_token_limit(raw_text, content_limit)
        tokens = header_tokens + estimate_tokens(text)
        remaining_tokens = max(0, remaining_tokens - tokens)

        source_record = _dump_source(source)
        source_record.pop("headers", None)
        source_record.pop("text", None)
        source_record.update(
            {
                "loaded": True,
                "estimated_tokens": tokens,
                "truncated": truncated,
            }
        )
        loaded_sources.append(source_record)

        if text:
            sections.append(f"{header}\n{text}")

    if not sections:
        return PromptContextResult(block="", sources=loaded_sources, estimated_tokens=0)

    block = (
        "External prompt context supplied by the API. Use it as background "
        "knowledge for this meeting. Do not mention source mechanics unless asked.\n\n"
        + "\n\n---\n\n".join(sections)
    )
    return PromptContextResult(
        block=block,
        sources=loaded_sources,
        estimated_tokens=estimate_tokens(block),
    )


def format_mcp_context(mcp: Any | None) -> str:
    """Format MCP metadata as prompt context. Does not execute MCP tools."""
    if not mcp:
        return ""

    data = mcp.model_dump(exclude_none=True) if hasattr(mcp, "model_dump") else mcp
    if not isinstance(data, Mapping):
        return ""

    lines = [
        "MCP context supplied by the API.",
        "Use this as integration metadata. Do not claim to call MCP tools unless a tool result appears in the conversation context.",
    ]

    instructions = data.get("instructions")
    if instructions:
        lines.append(f"Global instructions: {instructions}")

    servers = data.get("servers") or []
    for server in servers:
        if not isinstance(server, Mapping):
            continue
        lines.append(f"- Server: {server.get('name', 'unnamed')}")
        if server.get("url"):
            lines.append(f"  URL: {server['url']}")
        if server.get("transport"):
            lines.append(f"  Transport: {server['transport']}")
        tools = server.get("tools") or []
        if tools:
            lines.append(f"  Tools: {', '.join(tools)}")
        if server.get("instructions"):
            lines.append(f"  Instructions: {server['instructions']}")

    return "\n".join(lines)


def merge_context_blocks(blocks: Iterable[str]) -> str:
    """Merge non-empty context blocks."""
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())
