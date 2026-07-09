import asyncio
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

MCP_CLIENT_PATH = (
    Path(__file__).resolve().parents[1] / "utils" / "mcp_client.py"
)
spec = importlib.util.spec_from_file_location("mcp_client", MCP_CLIENT_PATH)
mcp_client = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["mcp_client"] = mcp_client
spec.loader.exec_module(mcp_client)

HttpMcpClient = mcp_client.HttpMcpClient
StdioMcpClient = mcp_client.StdioMcpClient
encode_stdio_message = mcp_client.encode_stdio_message
build_mcp_tool_name = mcp_client.build_mcp_tool_name
apply_mcp_runtime_headers = mcp_client.apply_mcp_runtime_headers
normalize_tool_result = mcp_client.normalize_tool_result
normalize_tools = mcp_client.normalize_tools
parse_sse_json = mcp_client.parse_sse_json
sanitize_mapping = mcp_client.sanitize_mapping
split_mcp_runtime_headers = mcp_client.split_mcp_runtime_headers
validate_mcp_http_url = mcp_client.validate_mcp_http_url
validate_mcp_response_peer = mcp_client.validate_mcp_response_peer
McpClientError = mcp_client.McpClientError


class McpClientHelpersTest(unittest.TestCase):
    def test_encode_stdio_message_uses_content_length_frame(self) -> None:
        frame = encode_stdio_message({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        header, body = frame.split(b"\r\n\r\n", 1)

        self.assertEqual(header, f"Content-Length: {len(body)}".encode("ascii"))
        self.assertEqual(json.loads(body.decode("utf-8"))["method"], "ping")

    def test_build_mcp_tool_name_sanitizes_names(self) -> None:
        self.assertEqual(
            build_mcp_tool_name("Google Drive", "read-file"),
            "mcp_google_drive_read_file",
        )

    def test_parse_sse_json_reads_data_lines(self) -> None:
        parsed = parse_sse_json(
            ': keepalive\n'
            'event: message\n'
            'data: {"jsonrpc":"2.0","result":{"ok":true}}\n\n'
            "data: [DONE]\n\n"
        )

        self.assertEqual(parsed["result"], {"ok": True})

    def test_normalize_tools_converts_schema_key(self) -> None:
        tools = normalize_tools(
            {
                "tools": [
                    {
                        "name": "search",
                        "description": "Search CRM",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        )

        self.assertEqual(
            tools,
            [
                {
                    "name": "search",
                    "description": "Search CRM",
                    "input_schema": {"type": "object"},
                }
            ],
        )

    def test_normalize_tool_result_extracts_text_and_jsonish_content(self) -> None:
        result = normalize_tool_result(
            {
                "content": [
                    {"type": "text", "text": '{"account":"acme"}'},
                    {"type": "json", "json": {"score": 42}},
                    "plain text",
                ]
            }
        )

        self.assertFalse(result["is_error"])
        self.assertEqual(result["content"][0]["json"], {"account": "acme"})
        self.assertEqual(result["content"][1]["json"], {"score": 42})
        self.assertEqual(result["content"][2]["text"], "plain text")

    def test_sanitize_mapping_redacts_secret_like_keys(self) -> None:
        sanitized = sanitize_mapping(
            {
                "Authorization": "Bearer secret",
                "Mcp-Session-Id": "session-secret",
                "X-Trace-Id": "trace-123",
            }
        )

        self.assertEqual(sanitized["Authorization"], "[redacted]")
        self.assertEqual(sanitized["Mcp-Session-Id"], "[redacted]")
        self.assertEqual(sanitized["X-Trace-Id"], "trace-123")

    def test_split_mcp_runtime_headers_removes_persisted_headers(self) -> None:
        payload, runtime_headers = split_mcp_runtime_headers(
            {
                "servers": [
                    {
                        "name": "drive",
                        "transport": "streamable_http",
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"Authorization": "Bearer secret"},
                    }
                ]
            }
        )

        self.assertNotIn("headers", payload["servers"][0])
        self.assertEqual(runtime_headers[0]["Authorization"], "Bearer secret")

        apply_mcp_runtime_headers(payload, runtime_headers)

        self.assertEqual(payload["servers"][0]["headers"]["Authorization"], "Bearer secret")

    def test_validate_mcp_http_url_blocks_localhost(self) -> None:
        with self.assertRaises(McpClientError):
            validate_mcp_http_url("http://127.0.0.1:3000/mcp")

    def test_validate_mcp_response_peer_blocks_private_ip(self) -> None:
        class FakeTransport:
            def get_extra_info(self, name):
                if name == "peername":
                    return ("127.0.0.1", 443)
                return None

        response = SimpleNamespace(
            _protocol=SimpleNamespace(transport=FakeTransport()),
        )

        with self.assertRaises(McpClientError):
            validate_mcp_response_peer("https://mcp.example.com/mcp", response)

    def test_validate_mcp_http_url_allows_exact_private_url(self) -> None:
        original_allowed = os.environ.get("MCP_ALLOWED_PRIVATE_URLS")
        os.environ["MCP_ALLOWED_PRIVATE_URLS"] = "http://127.0.0.1:8123/mcp"
        try:
            validate_mcp_http_url("http://127.0.0.1:8123/mcp")
            with self.assertRaises(McpClientError):
                validate_mcp_http_url("http://127.0.0.1:8124/mcp")
        finally:
            if original_allowed is None:
                os.environ.pop("MCP_ALLOWED_PRIVATE_URLS", None)
            else:
                os.environ["MCP_ALLOWED_PRIVATE_URLS"] = original_allowed


class McpClientTransportTest(unittest.TestCase):
    def test_http_client_preserves_session_id_without_network(self) -> None:
        calls: list[dict[str, str]] = []

        class FakeResponse:
            def __init__(self, headers: dict[str, str]) -> None:
                self.status = 200
                self.headers = headers

            async def __aenter__(self) -> "FakeResponse":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def json(self, content_type: object = None) -> dict[str, object]:
                return {"jsonrpc": "2.0", "result": {"ok": True}}

        class FakeSession:
            def __init__(self, timeout: object) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            def post(
                self,
                url: str,
                json: dict[str, object],
                headers: dict[str, str],
                allow_redirects: bool,
            ) -> FakeResponse:
                self.allow_redirects = allow_redirects
                calls.append(dict(headers))
                response_headers = {"Mcp-Session-Id": "session-123"} if len(calls) == 1 else {}
                return FakeResponse(response_headers)

        original_aiohttp = mcp_client.aiohttp
        original_allowed = os.environ.get("MCP_ALLOWED_PRIVATE_URLS")
        os.environ["MCP_ALLOWED_PRIVATE_URLS"] = "http://127.0.0.1:8123/mcp"
        mcp_client.aiohttp = SimpleNamespace(
            ClientSession=FakeSession,
            ClientTimeout=lambda total: {"total": total},
        )
        try:
            client = HttpMcpClient(
                "http://127.0.0.1:8123/mcp",
                headers={"Authorization": "Bearer not-logged"},
            )
            asyncio.run(client._post({"jsonrpc": "2.0", "id": 1, "method": "one"}))
            asyncio.run(client._post({"jsonrpc": "2.0", "id": 2, "method": "two"}))
        finally:
            mcp_client.aiohttp = original_aiohttp
            if original_allowed is None:
                os.environ.pop("MCP_ALLOWED_PRIVATE_URLS", None)
            else:
                os.environ["MCP_ALLOWED_PRIVATE_URLS"] = original_allowed

        self.assertNotIn("Mcp-Session-Id", calls[0])
        self.assertEqual(calls[1]["Mcp-Session-Id"], "session-123")

    def test_stdio_client_with_tiny_python_server(self) -> None:
        server_code = r"""
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, value = line.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(body.decode("utf-8"))

def send(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "{\"echo\": true}"}]}
    else:
        result = {}
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""

        async def run_client() -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
            client = StdioMcpClient([sys.executable, "-c", server_code])
            try:
                initialized = await client.initialize()
                tools = await client.list_tools()
                result = await client.call_tool("echo", {"value": "hi"})
                return initialized, tools, result
            finally:
                await client.close()

        initialized, tools, result = asyncio.run(run_client())

        self.assertEqual(initialized["protocolVersion"], "2024-11-05")
        self.assertEqual(tools[0]["name"], "echo")
        self.assertEqual(result["content"][0]["json"], {"echo": True})


if __name__ == "__main__":
    unittest.main()
