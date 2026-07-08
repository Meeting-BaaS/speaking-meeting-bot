import unittest
import importlib.util
import sys
from pathlib import Path

try:
    from pydantic import ValidationError
except ModuleNotFoundError:
    ValidationError = None

if ValidationError is not None:
    MODELS_PATH = Path(__file__).resolve().parents[1] / "app" / "models.py"
    spec = importlib.util.spec_from_file_location("models", MODELS_PATH)
    models = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["models"] = models
    spec.loader.exec_module(models)
    MCPServerConfig = models.MCPServerConfig
else:
    MCPServerConfig = None


class MCPServerConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        if MCPServerConfig is None or ValidationError is None:
            self.skipTest("pydantic is not installed")

    def test_stdio_server_accepts_live_connection_details(self) -> None:
        config = MCPServerConfig(
            name="google-drive",
            enabled=True,
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-gdrive"],
            env={"GDRIVE_CREDENTIALS_PATH": "/run/secrets/gdrive.json"},
            tool_allowlist=["search", "read_file"],
            timeout_seconds=20,
        )

        self.assertEqual(config.command, "npx")
        self.assertEqual(config.args[0], "-y")

    def test_remote_server_accepts_url_headers_and_allowlist(self) -> None:
        config = MCPServerConfig(
            name="crm",
            transport="streamable_http",
            url="https://mcp.example.com",
            headers={"Authorization": "Bearer token"},
            tool_allowlist=["get_account"],
        )

        self.assertEqual(config.url, "https://mcp.example.com")
        self.assertEqual(config.headers["Authorization"], "Bearer token")

    def test_metadata_only_server_is_allowed_but_not_connectable(self) -> None:
        config = MCPServerConfig(
            name="crm",
            tools=["get_account"],
            instructions="Available when configured with a live transport.",
        )

        self.assertIsNone(config.transport)
        self.assertTrue(config.enabled)

    def test_stdio_requires_command(self) -> None:
        with self.assertRaises(ValidationError):
            MCPServerConfig(name="google-drive", transport="stdio")

    def test_remote_transport_requires_url(self) -> None:
        with self.assertRaises(ValidationError):
            MCPServerConfig(name="crm", transport="streamable_http")

    def test_connection_details_require_transport(self) -> None:
        with self.assertRaises(ValidationError):
            MCPServerConfig(name="crm", url="https://mcp.example.com")

    def test_rejects_incompatible_transport_fields(self) -> None:
        with self.assertRaises(ValidationError):
            MCPServerConfig(
                name="crm",
                transport="streamable_http",
                url="https://mcp.example.com",
                command="npx",
            )


if __name__ == "__main__":
    unittest.main()
