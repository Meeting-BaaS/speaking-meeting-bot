import os
import unittest

from app.models import MCPConfig
from app.services.mcp_presets import (
    MCP_PROXY_READ_ONLY_TOOLS,
    MCP_PROXY_READ_WRITE_TOOLS,
    build_mcp_proxy_preset,
    resolve_mcp_config,
)


class MCPPresetsTest(unittest.TestCase):
    def test_professional_profile_builds_safe_read_only_mcpproxy_server(self) -> None:
        config = build_mcp_proxy_preset("professional", "read_only")
        data = config.model_dump(exclude_none=True)

        self.assertEqual(data["servers"][0]["name"], "mcpproxy-professional")
        self.assertEqual(data["servers"][0]["url"], "http://127.0.0.1:8111/mcp")
        self.assertEqual(
            data["servers"][0]["tool_allowlist"],
            MCP_PROXY_READ_ONLY_TOOLS,
        )
        self.assertNotIn("call_tool_write", data["servers"][0]["tool_allowlist"])
        self.assertNotIn("upstream_servers", data["servers"][0]["tool_allowlist"])
        self.assertNotIn("call_tool_destructive", data["servers"][0]["tool_allowlist"])
        self.assertNotIn("code_execution", data["servers"][0]["tool_allowlist"])

    def test_read_write_profile_only_adds_write_proxy_tool(self) -> None:
        config = build_mcp_proxy_preset("professional", "read_write")
        allowlist = config.model_dump(exclude_none=True)["servers"][0]["tool_allowlist"]

        self.assertEqual(allowlist, MCP_PROXY_READ_WRITE_TOOLS)
        self.assertIn("call_tool_write", allowlist)
        self.assertNotIn("call_tool_destructive", allowlist)

    def test_profile_url_can_be_overridden_by_environment(self) -> None:
        previous = os.environ.get("MCP_PROXY_PROFESSIONAL_URL")
        os.environ["MCP_PROXY_PROFESSIONAL_URL"] = "http://127.0.0.1:9000/mcp"
        try:
            config = build_mcp_proxy_preset("professional", "read_only")
        finally:
            if previous is None:
                os.environ.pop("MCP_PROXY_PROFESSIONAL_URL", None)
            else:
                os.environ["MCP_PROXY_PROFESSIONAL_URL"] = previous

        self.assertEqual(
            config.model_dump(exclude_none=True)["servers"][0]["url"],
            "http://127.0.0.1:9000/mcp",
        )

    def test_profile_merges_with_explicit_mcp_config(self) -> None:
        explicit = MCPConfig.model_validate(
            {
                "instructions": "Only use CRM when asked.",
                "servers": [
                    {
                        "name": "crm",
                        "transport": "streamable_http",
                        "url": "https://mcp.example.com/mcp",
                        "tool_allowlist": ["get_account"],
                    }
                ],
            }
        )

        merged = resolve_mcp_config("professional", "read_only", explicit)
        data = merged.model_dump(exclude_none=True)

        self.assertEqual(
            [server["name"] for server in data["servers"]],
            ["mcpproxy-professional", "crm"],
        )
        self.assertIn("Local mcpproxy preset: professional", data["instructions"])
        self.assertIn("Request MCP instructions", data["instructions"])

    def test_explicit_mcp_server_cannot_shadow_profile_server_name(self) -> None:
        explicit = MCPConfig.model_validate(
            {
                "servers": [
                    {
                        "name": "mcpproxy-professional",
                        "transport": "streamable_http",
                        "url": "https://mcp.example.com/mcp",
                    }
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "reserved by the mcp_profile preset"):
            resolve_mcp_config("professional", "read_only", explicit)


if __name__ == "__main__":
    unittest.main()
