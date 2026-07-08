"""Minimal MCP client primitives for stdio and streamable HTTP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse


LOGGER = logging.getLogger(__name__)
JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
SESSION_HEADER = "Mcp-Session-Id"
SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "session",
    "token",
)
aiohttp: Any | None = None


class McpClientError(Exception):
    """Raised when an MCP request cannot be completed."""


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Return an OpenAI/Pipecat-safe function name for an MCP tool."""
    raw = f"mcp_{server_name}_{tool_name}".lower()
    safe = re.sub(r"[^a-z0-9_]", "_", raw)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:64] or "mcp_tool"


def sanitize_mapping(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy with likely secret values redacted for safe logs/errors."""
    if not values:
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in values.items():
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in SECRET_KEY_PARTS):
            sanitized[key] = "[redacted]"
        else:
            sanitized[key] = value
    return sanitized


def _private_mcp_urls_allowed() -> bool:
    return os.getenv("MCP_ALLOW_PRIVATE_URLS", "").lower() in {"1", "true", "yes"}


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


def validate_mcp_http_url(url: str) -> None:
    """Block obvious SSRF targets unless explicitly allowed."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpClientError(f"Invalid MCP HTTP URL: {url}")

    if _private_mcp_urls_allowed():
        return

    host = parsed.hostname
    try:
        if _is_private_ip(host):
            raise McpClientError(f"MCP HTTP URL host is private or local: {host}")
        return
    except ValueError:
        pass

    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise McpClientError(f"Could not resolve MCP HTTP host '{host}': {e}") from e

    for address in addresses:
        if _is_private_ip(address[4][0]):
            raise McpClientError(
                f"MCP HTTP URL resolves to private or local address: {host}"
            )


def encode_stdio_message(message: Mapping[str, Any]) -> bytes:
    """Encode one JSON-RPC message using MCP Content-Length framing."""
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def read_stdio_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one Content-Length framed JSON message from a subprocess stream."""
    headers: dict[str, str] = {}

    while True:
        line = await reader.readline()
        if line == b"":
            raise McpClientError("MCP stdio server closed stdout")
        if line in {b"\r\n", b"\n"}:
            break
        try:
            name, value = line.decode("ascii").split(":", 1)
        except ValueError as e:
            raise McpClientError("Invalid MCP stdio header") from e
        headers[name.strip().lower()] = value.strip()

    try:
        content_length = int(headers["content-length"])
    except (KeyError, ValueError) as e:
        raise McpClientError("Missing or invalid MCP stdio Content-Length") from e

    body = await reader.readexactly(content_length)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise McpClientError("Invalid MCP stdio JSON response") from e

    if not isinstance(parsed, dict):
        raise McpClientError("MCP stdio response must be a JSON object")
    return parsed


def parse_sse_json(text: str) -> dict[str, Any]:
    """Parse a simple text/event-stream response and return the first JSON data."""
    events: list[str] = []
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if current:
                events.append("\n".join(current))
                current = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip())

    if current:
        events.append("\n".join(current))

    for event in events:
        if event == "[DONE]":
            continue
        try:
            parsed = json.loads(event)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise McpClientError("No JSON data found in MCP event stream")


def normalize_tools(tools_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert an MCP tools/list result into runner-friendly dictionaries."""
    tools = tools_payload.get("tools", [])
    if not isinstance(tools, list):
        return []

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        normalized.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
            }
        )
    return normalized


def normalize_tool_result(result_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an MCP tools/call result into simple content dictionaries."""
    content = result_payload.get("content", [])
    normalized_content: list[dict[str, Any]] = []

    if isinstance(content, list):
        for item in content:
            normalized_content.append(_normalize_content_item(item))
    elif content is not None:
        normalized_content.append(_normalize_content_item(content))

    return {
        "content": normalized_content,
        "is_error": bool(result_payload.get("isError", result_payload.get("is_error", False))),
    }


def _normalize_content_item(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text", ""))
            parsed_json = _parse_jsonish(text)
            normalized: dict[str, Any] = {"type": "text", "text": text}
            if parsed_json is not None:
                normalized["json"] = parsed_json
            return normalized
        if item_type == "json":
            return {"type": "json", "json": item.get("json", item.get("data"))}
        if "json" in item:
            return {"type": item_type or "json", "json": item["json"]}
        if "data" in item:
            return {"type": item_type or "data", "data": item["data"]}
        return dict(item)

    if isinstance(item, str):
        parsed_json = _parse_jsonish(item)
        normalized = {"type": "text", "text": item}
        if parsed_json is not None:
            normalized["json"] = parsed_json
        return normalized

    return {"type": "json", "json": item}


def _parse_jsonish(value: str) -> Any | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


@dataclass
class _JsonRpcState:
    next_id: int = 1

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        return message

    def notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            message["params"] = dict(params)
        return message


def _extract_result(response: Mapping[str, Any]) -> dict[str, Any]:
    if "error" in response:
        error = response["error"]
        if isinstance(error, Mapping):
            message = error.get("message") or error
        else:
            message = error
        raise McpClientError(f"MCP server returned error: {message}")

    result = response.get("result", {})
    if not isinstance(result, dict):
        raise McpClientError("MCP response result must be a JSON object")
    return result


@dataclass
class StdioMcpClient:
    """MCP client for subprocess servers using Content-Length stdio framing."""

    command: Sequence[str]
    env: Mapping[str, str] | None = None
    cwd: str | None = None
    client_name: str = "speaking-meeting-bot"
    client_version: str = "0.1.0"
    _state: _JsonRpcState = field(default_factory=_JsonRpcState, init=False)
    _process: asyncio.subprocess.Process | None = field(default=None, init=False)

    async def start(self) -> None:
        if self._process is not None:
            return
        if not self.command:
            raise McpClientError("Stdio MCP command cannot be empty")
        env = os.environ.copy()
        if self.env is not None:
            env.update({str(key): str(value) for key, value in self.env.items()})

        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=self.cwd,
        )

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )
        await self._notification("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        return normalize_tools(await self._request("tools/list"))

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        return normalize_tool_result(result)

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except BrokenPipeError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.start()
        assert self._process and self._process.stdin and self._process.stdout
        message = self._state.request(method, params)
        self._process.stdin.write(encode_stdio_message(message))
        await self._process.stdin.drain()
        response = await read_stdio_message(self._process.stdout)
        return _extract_result(response)

    async def _notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        await self.start()
        assert self._process and self._process.stdin
        self._process.stdin.write(encode_stdio_message(self._state.notification(method, params)))
        await self._process.stdin.drain()


@dataclass
class HttpMcpClient:
    """MCP client for streamable-http/http servers using JSON-RPC POST."""

    url: str
    headers: Mapping[str, str] | None = None
    client_name: str = "speaking-meeting-bot"
    client_version: str = "0.1.0"
    timeout_seconds: float = 12
    _state: _JsonRpcState = field(default_factory=_JsonRpcState, init=False)
    _session_id: str | None = field(default=None, init=False)

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )
        await self._notification("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        return normalize_tools(await self._request("tools/list"))

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        return normalize_tool_result(result)

    async def close(self) -> None:
        return None

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._post(self._state.request(method, params))
        return _extract_result(response)

    async def _notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        await self._post(self._state.notification(method, params))

    async def _post(self, message: Mapping[str, Any]) -> dict[str, Any]:
        aiohttp_module = _get_aiohttp()
        validate_mcp_http_url(self.url)
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **dict(self.headers or {}),
        }
        if self._session_id:
            request_headers[SESSION_HEADER] = self._session_id

        timeout = aiohttp_module.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp_module.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.url,
                    json=message,
                    headers=request_headers,
                ) as response:
                    if SESSION_HEADER in response.headers:
                        self._session_id = response.headers[SESSION_HEADER]
                    if response.status >= 400:
                        safe_headers = sanitize_mapping(request_headers)
                        LOGGER.warning(
                            "MCP HTTP request failed: status=%s headers=%s",
                            response.status,
                            safe_headers,
                        )
                        raise McpClientError(
                            f"MCP HTTP server returned HTTP {response.status}"
                        )
                    if response.status in {202, 204}:
                        return {}
                    return await _parse_http_response(response)
        except McpClientError:
            raise
        except Exception as e:
            raise McpClientError(f"MCP HTTP request failed: {e}") from e


def _get_aiohttp() -> Any:
    global aiohttp
    if aiohttp is None:
        import aiohttp as aiohttp_module

        aiohttp = aiohttp_module
    return aiohttp


async def _parse_http_response(response: Any) -> dict[str, Any]:
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        return parse_sse_json(await response.text())

    try:
        parsed = await response.json(content_type=None)
    except Exception as e:
        raise McpClientError("Invalid MCP HTTP JSON response") from e

    if not isinstance(parsed, dict):
        raise McpClientError("MCP HTTP response must be a JSON object")
    return parsed
