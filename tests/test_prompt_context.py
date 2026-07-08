import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

PROMPT_CONTEXT_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "prompt_context.py"
)
spec = importlib.util.spec_from_file_location("prompt_context", PROMPT_CONTEXT_PATH)
prompt_context = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(prompt_context)

estimate_tokens = prompt_context.estimate_tokens
format_mcp_context = prompt_context.format_mcp_context
load_prompt_context = prompt_context.load_prompt_context
truncate_to_token_limit = prompt_context.truncate_to_token_limit
PromptContextError = prompt_context.PromptContextError
_validate_fetch_url = prompt_context._validate_fetch_url


class PromptContextTest(unittest.TestCase):
    def test_estimate_tokens_uses_four_chars_per_token(self) -> None:
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)

    def test_truncate_to_token_limit(self) -> None:
        text, truncated = truncate_to_token_limit("abcdefghij", 2)

        self.assertTrue(truncated)
        self.assertLessEqual(estimate_tokens(text), 12)
        self.assertIn("truncated", text)

    def test_load_prompt_context_from_inline_source(self) -> None:
        source = SimpleNamespace(
            name="CRM notes",
            type="text",
            text="Prospect uses MeetingBaas and wants MCP support.",
            url=None,
            headers=None,
            token_limit=None,
        )

        result = asyncio.run(load_prompt_context([source], total_token_limit=100))

        self.assertIn("CRM notes", result.block)
        self.assertIn("MCP support", result.block)
        self.assertEqual(result.sources[0]["name"], "CRM notes")
        self.assertNotIn("text", result.sources[0])

    def test_format_mcp_context(self) -> None:
        mcp = {
            "instructions": "Use CRM data when relevant.",
            "servers": [
                {
                    "name": "crm",
                    "url": "https://mcp.example.com",
                    "transport": "streamable_http",
                    "tools": ["get_account", "list_calls"],
                }
            ],
        }

        block = format_mcp_context(mcp)

        self.assertIn("Server: crm", block)
        self.assertIn("get_account", block)
        self.assertIn("Use CRM data", block)

    def test_private_prompt_urls_blocked_by_default(self) -> None:
        with self.assertRaises(PromptContextError):
            _validate_fetch_url("http://127.0.0.1:8000/notes.md")


if __name__ == "__main__":
    unittest.main()
