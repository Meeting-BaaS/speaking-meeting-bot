import unittest

from config.prompts import DEFAULT_SYSTEM_PROMPT, VOICE_STYLE_PROMPT
from utils.system_prompt import build_system_prompt


class SystemPromptTest(unittest.TestCase):
    def test_persona_then_voice_guidance(self):
        prompt = build_system_prompt({"prompt": "You are Vibe."}, "Vibe")
        self.assertTrue(prompt.startswith("You are Vibe."))
        self.assertIn(VOICE_STYLE_PROMPT.strip(), prompt)
        self.assertNotIn(DEFAULT_SYSTEM_PROMPT.strip(), prompt)
        self.assertNotIn("PUNCTUATION", prompt)

    def test_extra_knowledge_brings_the_default_instructions(self):
        prompt = build_system_prompt(
            {"prompt": "You are Vibe.", "additional_content": "# Content from pricing.md\nFree tier."},
            "Vibe",
            mcp_context="\n\nMCP servers: none",
        )
        self.assertIn("You are Vibe\n\n" + DEFAULT_SYSTEM_PROMPT.strip(), prompt)
        self.assertIn("Free tier.", prompt)
        self.assertLess(prompt.index("Free tier."), prompt.index("How to talk:"))
        self.assertTrue(prompt.endswith("MCP servers: none"))

    def test_voice_guidance_asks_for_sentence_punctuation(self):
        self.assertIn("punctuation", VOICE_STYLE_PROMPT)
        self.assertIn("short first sentence", VOICE_STYLE_PROMPT)

    def test_voice_guidance_only_answers_what_is_directed_at_the_bot(self):
        self.assertIn("directed at you", VOICE_STYLE_PROMPT)
        self.assertIn("talking to each other, do not reply", VOICE_STYLE_PROMPT)
