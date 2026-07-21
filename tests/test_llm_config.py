import os
import unittest

from utils.llm_config import (
    missing_llm_provider_credential,
    resolve_llm_model,
    resolve_llm_provider,
    resolve_openai_api_surface,
)


class LLMConfigTest(unittest.TestCase):
    ENV_KEYS = [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_API_SURFACE",
        "OPENAI_MODEL",
        "ZAI_API_KEY",
        "ZAI_MODEL",
    ]

    def setUp(self) -> None:
        self.original_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_request_provider_overrides_env_provider(self) -> None:
        os.environ["LLM_PROVIDER"] = "anthropic"

        self.assertEqual(resolve_llm_provider({"llm_provider": "z.ai"}), "zai")

    def test_model_precedence_uses_request_then_provider_then_global(self) -> None:
        os.environ["OPENAI_MODEL"] = "gpt-provider"
        os.environ["LLM_MODEL"] = "gpt-global"

        self.assertEqual(
            resolve_llm_model("openai", {"llm_model": "gpt-request"}),
            "gpt-request",
        )
        self.assertEqual(resolve_llm_model("openai", {}), "gpt-provider")

    def test_missing_provider_credential_reports_required_env(self) -> None:
        self.assertEqual(missing_llm_provider_credential("anthropic"), "ANTHROPIC_API_KEY")

        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        self.assertIsNone(missing_llm_provider_credential("anthropic"))

    def test_openai_surface_defaults_to_responses(self) -> None:
        self.assertEqual(resolve_openai_api_surface(), "responses")


if __name__ == "__main__":
    unittest.main()
