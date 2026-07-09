"""Server-side MCP presets for trusted local mcpproxy groups."""

import os
from typing import Any, Mapping

from pydantic import ValidationError

from app.models import MCPConfig


MCP_PROXY_PROFILE_URLS = {
    "professional": (
        "MCP_PROXY_PROFESSIONAL_URL",
        "http://127.0.0.1:8111/mcp",
    ),
    "personal": (
        "MCP_PROXY_PERSONAL_URL",
        "http://127.0.0.1:8110/mcp",
    ),
    "all": (
        "MCP_PROXY_ALL_URL",
        "http://127.0.0.1:8109/mcp",
    ),
}

MCP_PROXY_PROFILE_LABELS = {
    "professional": "professional work tools",
    "personal": "personal tools and documents",
    "all": "all registered local tools",
}

MCP_PROXY_READ_ONLY_TOOLS = [
    "retrieve_tools",
    "call_tool_read",
    "read_cache",
    "set_profile",
]
MCP_PROXY_READ_WRITE_TOOLS = [
    *MCP_PROXY_READ_ONLY_TOOLS,
    "call_tool_write",
]


def build_mcp_proxy_preset(profile: str, tool_access: str) -> MCPConfig:
    """Build a vetted MCP config for a local mcpproxy group."""
    if profile not in MCP_PROXY_PROFILE_URLS:
        raise ValueError(f"unsupported mcp_profile: {profile}")
    if tool_access not in {"read_only", "read_write"}:
        raise ValueError(f"unsupported mcp_profile_tool_access: {tool_access}")

    env_name, default_url = MCP_PROXY_PROFILE_URLS[profile]
    url = os.getenv(env_name, "").strip() or default_url
    tools = (
        MCP_PROXY_READ_WRITE_TOOLS
        if tool_access == "read_write"
        else MCP_PROXY_READ_ONLY_TOOLS
    )

    return MCPConfig.model_validate(
        {
            "instructions": _preset_instructions(profile, tool_access),
            "servers": [
                {
                    "name": f"mcpproxy-{profile}",
                    "enabled": True,
                    "transport": "streamable_http",
                    "url": url,
                    "tool_allowlist": tools,
                    "timeout_seconds": 45,
                    "instructions": (
                        "Use retrieve_tools to find the right upstream tool, then "
                        "call the recommended read/write proxy tool. Do not attempt "
                        "server management or destructive actions."
                    ),
                }
            ],
        }
    )


def resolve_mcp_config(
    profile: str | None,
    tool_access: str,
    explicit_config: MCPConfig | None,
) -> MCPConfig | None:
    """Merge an optional local mcpproxy preset with caller-supplied MCP config."""
    preset = build_mcp_proxy_preset(profile, tool_access) if profile else None
    if not preset:
        return explicit_config
    if not explicit_config:
        return preset

    try:
        return MCPConfig.model_validate(
            _merge_mcp_config(
                preset.model_dump(exclude_none=True),
                explicit_config.model_dump(exclude_none=True),
            )
        )
    except ValidationError as exc:
        raise ValueError(f"invalid merged MCP config: {exc}") from exc


def _preset_instructions(profile: str, tool_access: str) -> str:
    label = MCP_PROXY_PROFILE_LABELS[profile]
    write_clause = (
        "Read/write proxy calls are allowed when clearly useful for the meeting."
        if tool_access == "read_write"
        else "Use read-only proxy calls unless the request also supplies another explicit MCP server."
    )
    return (
        f"Local mcpproxy preset: {profile} ({label}). Use retrieve_tools first, "
        "then call the selected upstream tool through call_tool_read"
        f"{' or call_tool_write' if tool_access == 'read_write' else ''}. "
        f"{write_clause} Never use server-management, destructive, registry, "
        "quarantine, or code-execution tools."
    )


def _merge_mcp_config(
    preset: Mapping[str, Any],
    explicit: Mapping[str, Any],
) -> dict[str, Any]:
    preset_servers = list(preset.get("servers") or [])
    explicit_servers = list(explicit.get("servers") or [])

    preset_names = {
        str(server.get("name"))
        for server in preset_servers
        if isinstance(server, Mapping) and server.get("name")
    }
    for server in explicit_servers:
        if not isinstance(server, Mapping):
            continue
        name = str(server.get("name") or "")
        if name in preset_names:
            raise ValueError(
                f"mcp server name '{name}' is reserved by the mcp_profile preset"
            )

    instructions = preset.get("instructions")
    if explicit.get("instructions"):
        instructions = (
            f"{instructions}\n\nRequest MCP instructions:\n{explicit['instructions']}"
            if instructions
            else explicit["instructions"]
        )

    merged = dict(explicit)
    merged["servers"] = [*preset_servers, *explicit_servers]
    if instructions:
        merged["instructions"] = instructions
    return merged
