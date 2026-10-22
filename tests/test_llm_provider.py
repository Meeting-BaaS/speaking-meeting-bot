import json
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

    PARSE_BY_MODEL = {
        "claude-3-opus-20240229": ["opus", [3, 0]],
        "claude-3-5-sonnet-20241022": ["sonnet", [3, 5]],
        "claude-sonnet-4-20250514": ["sonnet", [4, 0]],
        "claude-opus-4-1-20250805": ["opus", [4, 1]],
        "claude-sonnet-4-5-20250929": ["sonnet", [4, 5]],
        "claude-opus-4-5@20251101": ["opus", [4, 5]],
        "claude-opus-5": ["opus", [5, 0]],
        "anthropic.claude-opus-5-v1:0": ["opus", [5, 0]],
        "us.anthropic.claude-sonnet-5-5-v1:0": ["sonnet", [5, 5]],
        "claude-sonnet-5-1m": ["sonnet", [5, 0]],
        "Claude-Fable-5-1": ["fable", [5, 1]],
        "claude-next": None,
        "claude-mythos": None,
    }

    def test_anthropic_model_ids_parse_to_family_and_generation(self):
        result = self.run_meetingbaas_snippet(
            "import json\n"
            "from utils.llm_config import parse_anthropic_model as p\n"
            f"models = {list(self.PARSE_BY_MODEL)!r}\n"
            "print(json.dumps({m: p(m) for m in models}))\n"
        )
        self.assertEqual(json.loads(result), self.PARSE_BY_MODEL)

    THINKING_BY_MODEL = {
        "claude-3-opus-20240229": "omit",
        "claude-3-5-sonnet-20241022": "omit",
        "claude-3-7-sonnet-20250219": "disabled",
        "claude-haiku-4-5": "disabled",
        "claude-haiku-4-5-20251001": "disabled",
        "claude-opus-4-5@20251101": "disabled",
        "claude-sonnet-4-20250514": "disabled",
        "claude-opus-4-1-20250805": "disabled",
        "us.anthropic.claude-haiku-5-5-v1:0": "disabled",
        "claude-sonnet-4-6": "disabled",
        "claude-opus-4-8": "disabled",
        "claude-sonnet-5": "disabled",
        "claude-opus-5": "disabled",
        "claude-haiku-5-5": "disabled",
        "claude-sonnet-5-5": "between_tools",
        "anthropic.claude-sonnet-5-5-v1:0": "between_tools",
        "claude-opus-5-5": "adaptive",
        "claude-fable-5-1": "adaptive",
        "claude-mythos-5-1": "adaptive",
        "claude-opus-6": "adaptive",
        "claude-haiku-6-2": "adaptive",
        "claude-next": "adaptive",
        "claude-mythos": "adaptive",
    }

    def test_anthropic_thinking_mode_follows_the_model_generation(self):
        result = self.run_meetingbaas_snippet(
            "import json\n"
            "from utils.llm_config import resolve_anthropic_thinking as r\n"
            f"models = {list(self.THINKING_BY_MODEL)!r}\n"
            "print(json.dumps({m: r(m) for m in models}))\n"
        )
        self.assertEqual(json.loads(result), self.THINKING_BY_MODEL)

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
        # Sampling parameters are rejected on every current model.
        "assert not {'temperature', 'top_p', 'top_k'} & set(p), sorted(p)\n"
        "assert not {'temperature', 'top_p', 'top_k'} & set(p.get('extra_body', {}))\n"
        "print(p['model'], p.get('thinking'), p.get('output_config'), "
        "p['max_tokens'])\n"
    )

    def test_requests_carry_a_thinking_value_each_model_accepts(self):
        cases = {
            "claude-3-5-sonnet-20241022": "None None 1024",
            "claude-haiku-4-5": "{'type': 'disabled'} None 1024",
            "claude-opus-5": "{'type': 'disabled'} None 1024",
            "claude-sonnet-5-5": "{'type': 'between_tools'} None 1024",
            "claude-opus-5-5": "{'type': 'adaptive'} {'effort': 'low'} 4096",
            "claude-fable-5-1": "{'type': 'adaptive'} {'effort': 'low'} 4096",
            "claude-opus-6": "{'type': 'adaptive'} {'effort': 'low'} 4096",
        }
        for model, expected in cases.items():
            result = self.run_meetingbaas_snippet(
                self.REQUEST_SNIPPET,
                ANTHROPIC_API_KEY="test-key",
                ANTHROPIC_MODEL=model,
            )
            self.assertEqual(result, f"{model} {expected}", model)

    def test_per_bot_model_choice_is_resolved_the_same_way(self):
        result = self.run_meetingbaas_snippet(
            "from scripts import meetingbaas\n"
            "llm = meetingbaas.build_llm_service("
            "{'llm_provider': 'anthropic', 'llm_model': 'claude-sonnet-5-5'})\n"
            "s = llm._settings\n"
            "assert s.thinking.type == 'between_tools', s.thinking\n"
            "assert s.thinking.display is None and s.thinking.budget_tokens is None\n"
            "assert s.extra == {} and s.max_tokens == 1024, (s.extra, s.max_tokens)\n"
            "print(s.model)\n",
            ANTHROPIC_API_KEY="test-key",
            ANTHROPIC_MODEL="claude-haiku-4-5",
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
