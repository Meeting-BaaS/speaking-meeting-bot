import os
import unittest
from types import SimpleNamespace

from utils.runtime import (
    build_public_base_url,
    get_internal_pipecat_ws_url,
    parse_cors_origins,
)
from utils.url import convert_http_to_ws_url, convert_ws_to_http_url


class RuntimeHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_port = os.environ.get("PORT")

    def tearDown(self) -> None:
        if self.original_port is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = self.original_port

    def test_convert_http_and_ws_urls(self) -> None:
        self.assertEqual(
            convert_http_to_ws_url("https://bots.example.com"),
            "wss://bots.example.com",
        )
        self.assertEqual(
            convert_ws_to_http_url("wss://bots.example.com"),
            "https://bots.example.com",
        )

    def test_build_public_base_url_prefers_configured_base_url(self) -> None:
        request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http"))

        self.assertEqual(
            build_public_base_url(request, configured_base_url="wss://bots.example.com"),
            "https://bots.example.com",
        )

    def test_build_public_base_url_uses_forwarded_headers(self) -> None:
        request = SimpleNamespace(
            headers={"host": "api.example.com", "x-forwarded-proto": "https"},
            url=SimpleNamespace(scheme="http"),
        )

        self.assertEqual(build_public_base_url(request), "https://api.example.com")

    def test_internal_pipecat_url_uses_configured_port(self) -> None:
        os.environ["PORT"] = "8123"
        self.assertEqual(
            get_internal_pipecat_ws_url("client-123"),
            "ws://localhost:8123/pipecat/client-123",
        )

    def test_parse_cors_origins(self) -> None:
        self.assertEqual(parse_cors_origins(None), ["*"])
        self.assertEqual(
            parse_cors_origins("https://a.example.com, https://b.example.com"),
            ["https://a.example.com", "https://b.example.com"],
        )


if __name__ == "__main__":
    unittest.main()
