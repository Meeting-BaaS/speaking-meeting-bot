"""LLM provider routing configuration shared by the API and Pipecat runner."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_ZAI_MODEL = "glm-5.2"
DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/paas/v4/"
SUPPORTED_LLM_PROVIDERS = {"openai", "anthropic", "zai"}
SUPPORTED_OPENAI_API_SURFACES = {"responses", "chat"}

PROVIDER_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "zai": "ZAI_API_KEY",
}


def clean_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def resolve_llm_provider(persona: Mapping[str, Any] | None) -> str:
    """Resolve LLM provider with request data taking precedence over env."""
    persona = persona or {}
    provider = clean_string(persona.get("llm_provider")) or clean_string(
        os.getenv("LLM_PROVIDER")
    )
    provider = (provider or "openai").lower().replace("-", "_")

    aliases = {
        "z_ai": "zai",
        "z.ai": "zai",
        "zai": "zai",
        "glm": "zai",
        "claude": "anthropic",
        "anthropic": "anthropic",
        "openai": "openai",
    }
    provider = aliases.get(provider, provider)
    if provider not in SUPPORTED_LLM_PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. Expected one of: "
            f"{', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}"
        )
    return provider


def resolve_llm_model(provider: str, persona: Mapping[str, Any] | None) -> str:
    """Resolve provider model with request > provider env > generic env > default."""
    persona = persona or {}
    request_model = clean_string(persona.get("llm_model"))
    if request_model:
        return request_model

    provider_env = {
        "openai": "OPENAI_MODEL",
        "anthropic": "ANTHROPIC_MODEL",
        "zai": "ZAI_MODEL",
    }[provider]
    default_model = {
        "openai": DEFAULT_OPENAI_MODEL,
        "anthropic": DEFAULT_ANTHROPIC_MODEL,
        "zai": DEFAULT_ZAI_MODEL,
    }[provider]
    return (
        clean_string(os.getenv(provider_env))
        or clean_string(os.getenv("LLM_MODEL"))
        or default_model
    )


def resolve_openai_api_surface() -> str:
    """Resolve OpenAI API surface. Responses is preferred for newest models."""
    surface = (clean_string(os.getenv("OPENAI_API_SURFACE")) or "responses").lower()
    surface = surface.replace("-", "_")
    aliases = {
        "response": "responses",
        "responses": "responses",
        "chat": "chat",
        "chat_completion": "chat",
        "chat_completions": "chat",
    }
    surface = aliases.get(surface, surface)
    if surface not in SUPPORTED_OPENAI_API_SURFACES:
        raise ValueError(
            f"Unsupported OpenAI API surface {surface!r}. Expected one of: "
            f"{', '.join(sorted(SUPPORTED_OPENAI_API_SURFACES))}"
        )
    return surface


def missing_llm_provider_credential(provider: str) -> str | None:
    """Return the missing provider key env var, or None when configured."""
    key_env = PROVIDER_KEY_ENVS[provider]
    if clean_string(os.getenv(key_env)):
        return None
    return key_env
