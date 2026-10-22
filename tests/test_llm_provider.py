import os
import subprocess
import sys
import unittest


class LLMProviderRoutingTest(unittest.TestCase):
    ENV_KEYS = [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_THINKING",
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

    def test_all_providers_preserve_sequential_tools_and_system_instruction(self):
        result = self.run_meetingbaas_snippet(
            "import os\n"
            "from scripts import meetingbaas\n"
            "for provider, surface in [('anthropic', 'chat'), ('openai', 'chat'), "
            "('openai', 'responses'), ('zai', 'chat')]:\n"
            "    os.environ['OPENAI_API_SURFACE'] = surface\n"
            "    llm = meetingbaas.build_llm_service({'llm_provider': provider}, "
            "system_instruction='Stay in character')\n"
            "    assert llm._run_in_parallel is False, (provider, surface)\n"
            "    assert llm._settings.system_instruction == 'Stay in character'\n"
            "print('verified')\n",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            ZAI_API_KEY="test-key",
        )
        self.assertEqual(result, "verified")

    def test_anthropic_runs_without_thinking_and_with_prompt_caching(self):
        result = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "llm = meetingbaas.build_llm_service({'llm_provider': 'anthropic'})\n"
            "s = llm._settings\n"
            "assert s.thinking.type == 'disabled', s.thinking\n"
            "assert s.enable_prompt_caching is True\n"
            "assert s.max_tokens == 1024\n"
            "print(s.model)\n",
            ANTHROPIC_API_KEY="test-key",
        )
        self.assertEqual(result, "claude-haiku-4-5")

    def test_anthropic_thinking_mode_follows_the_model(self):
        result = self.run_meetingbaas_snippet(
            "from utils.llm_config import resolve_anthropic_thinking as r\n"
            "modes = [r(m) for m in ('claude-haiku-4-5', 'claude-haiku-5-5', "
            "'claude-sonnet-4-6', 'claude-sonnet-5', 'claude-opus-5', "
            "'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-fable-5-1', "
            "'anthropic.claude-mythos-5-1')]\n"
            "print(' '.join(modes))\n"
        )
        self.assertEqual(
            result,
            "disabled disabled disabled disabled disabled "
            "between_tools omit omit omit",
        )

    def test_anthropic_thinking_override_wins_and_is_validated(self):
        result = self.run_meetingbaas_snippet(
            "from utils.llm_config import resolve_anthropic_thinking as r\n"
            "print(r('claude-haiku-4-5'))\n",
            ANTHROPIC_THINKING="Between-Tools",
        )
        self.assertEqual(result, "between_tools")
        for bad in ("sometimes", "omit"):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_meetingbaas_snippet(
                    "from utils.llm_config import resolve_anthropic_thinking as r\n"
                    "r('claude-sonnet-5-5')\n",
                    ANTHROPIC_THINKING=bad,
                )

    # What pipecat actually sends: it adds thinking=disabled to Sonnet 5+
    # requests that omit the parameter, so the settings alone do not prove
    # the request is one the model accepts.
    REQUEST_SNIPPET = (
        "import asyncio\n"
        "from types import SimpleNamespace\n"
        "from unittest.mock import AsyncMock\n"
        "from pipecat.processors.aggregators.llm_context import LLMContext\n"
        "from scripts import meetingbaas\n"
        "llm = meetingbaas.build_llm_service({'llm_provider': 'anthropic'})\n"
        "create = AsyncMock(return_value=SimpleNamespace(content=[]))\n"
        "llm._client = SimpleNamespace(beta=SimpleNamespace("
        "messages=SimpleNamespace(create=create)))\n"
        "ctx = LLMContext(messages=[{'role': 'user', 'content': 'hi'}])\n"
        "asyncio.run(llm.run_inference(ctx))\n"
        "p = create.call_args.kwargs\n"
        "print(p['model'], p.get('thinking'), p.get('output_config'))\n"
    )

    def test_requests_carry_a_thinking_value_each_model_accepts(self):
        cases = {
            "claude-haiku-4-5": "{'type': 'disabled'} None",
            "claude-sonnet-5-5": "{'type': 'between_tools'} None",
            "claude-fable-5-1": "None {'effort': 'low'}",
            "claude-opus-5-5": "None {'effort': 'low'}",
        }
        for model, expected in cases.items():
            result = self.run_meetingbaas_snippet(
                self.REQUEST_SNIPPET,
                ANTHROPIC_API_KEY="test-key",
                ANTHROPIC_MODEL=model,
            )
            self.assertEqual(result, f"{model} {expected}", model)

    def test_adaptive_override_reaches_the_request_untouched_on_sonnet(self):
        result = self.run_meetingbaas_snippet(
            self.REQUEST_SNIPPET,
            ANTHROPIC_API_KEY="test-key",
            ANTHROPIC_MODEL="claude-sonnet-5-5",
            ANTHROPIC_THINKING="adaptive",
        )
        self.assertEqual(
            result, "claude-sonnet-5-5 {'type': 'adaptive'} {'effort': 'low'}"
        )

    def test_always_on_thinking_models_get_no_thinking_param_and_low_effort(self):
        result = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "llm = meetingbaas.build_llm_service("
            "{'llm_provider': 'anthropic', 'llm_model': 'claude-fable-5-1'})\n"
            "s = llm._settings\n"
            "assert not isinstance(s.thinking, meetingbaas.AnthropicLLMService.ThinkingConfig), s.thinking\n"
            "assert s.extra == {'output_config': {'effort': 'low'}}, s.extra\n"
            "assert s.enable_prompt_caching is True\n"
            "print(s.model)\n",
            ANTHROPIC_API_KEY="test-key",
        )
        self.assertEqual(result, "claude-fable-5-1")

    def test_sonnet_5_5_turns_thinking_off_with_between_tools(self):
        result = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "llm = meetingbaas.build_llm_service({'llm_provider': 'anthropic'})\n"
            "s = llm._settings\n"
            "assert s.thinking.type == 'between_tools', s.thinking\n"
            "assert s.thinking.display is None and s.thinking.budget_tokens is None\n"
            "assert s.extra == {}, s.extra\n"
            "print(s.model)\n",
            ANTHROPIC_API_KEY="test-key",
            ANTHROPIC_MODEL="claude-sonnet-5-5",
        )
        self.assertEqual(result, "claude-sonnet-5-5")

    def test_default_models_are_the_fast_ones(self):
        result = self.run_meetingbaas_snippet(
            "from utils.llm_config import resolve_llm_model\n"
            "print(resolve_llm_model('openai', {}), resolve_llm_model('anthropic', {}))\n"
        )
        self.assertEqual(result, "gpt-5.4-mini claude-haiku-4-5")


if __name__ == "__main__":
    unittest.main()
