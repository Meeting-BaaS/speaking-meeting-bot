import os
import subprocess
import sys
import unittest


class LLMProviderRoutingTest(unittest.TestCase):
    ENV_KEYS = [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_API_SURFACE",
        "OPENAI_MODEL",
        "OPENAI_SERVICE_TIER",
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "ZAI_MODEL",
    ]

    def run_meetingbaas_snippet(self, code: str, **env_overrides: str) -> str:
        env = os.environ.copy()
        for key in self.ENV_KEYS:
            env.pop(key, None)
        env.update(env_overrides)

        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )
        return result.stdout.strip().splitlines()[-1]

    def test_openai_defaults_to_responses_surface(self) -> None:
        class_name = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "llm = meetingbaas.build_llm_service({'llm_provider': 'openai'})\n"
            "print(type(llm).__name__)\n",
            OPENAI_API_KEY="test-key",
        )

        self.assertEqual(class_name, "OpenAIResponsesLLMService")

    def test_openai_chat_surface_is_available_for_compatibility(self) -> None:
        class_name = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "llm = meetingbaas.build_llm_service({'llm_provider': 'openai'})\n"
            "print(type(llm).__name__)\n",
            OPENAI_API_KEY="test-key",
            OPENAI_API_SURFACE="chat",
        )

        self.assertEqual(class_name, "OpenAILLMService")

    def test_openai_surface_aliases_are_normalized(self) -> None:
        surface = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "print(meetingbaas.resolve_openai_api_surface())\n",
            OPENAI_API_SURFACE="chat-completions",
        )

        self.assertEqual(surface, "chat")


if __name__ == "__main__":
    unittest.main()
