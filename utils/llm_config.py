"""LLM provider routing configuration shared by the API and Pipecat runner."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

# Voice defaults favour time-to-first-token over depth: a bot that answers in
# two sentences gains nothing from a frontier model and loses seconds to it.
# Operators pick a bigger model with OPENAI_MODEL / ANTHROPIC_MODEL, callers
# per bot with llm_model.
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_ZAI_MODEL = "glm-5.2"
DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/paas/v4/"
SUPPORTED_LLM_PROVIDERS = {"openai", "anthropic", "zai"}
SUPPORTED_OPENAI_API_SURFACES = {"responses", "chat"}
# Thinking settings under which the model reasons before answering. The
# request then needs room for the thinking tokens and the lowest effort to keep
# a voice turn fast.
ANTHROPIC_THINKING_ON_MODES = {"adaptive"}

# Family and generation of a Claude model id, wherever the id came from:
# the current form (claude-sonnet-4-5, claude-opus-5-5, claude-fable-5-1), ids
# without a minor (claude-opus-5), the pre-4 form with the generation first
# (claude-3-7-sonnet-20250219, claude-3-opus-20240229), dated or Vertex
# suffixes (claude-haiku-4-5-20251001, claude-opus-4-5@20251101) and Bedrock
# prefixes (anthropic.claude-sonnet-5-5-v1:0). A date suffix never reads as a
# generation because a generation is at most two digits.
_ANTHROPIC_MODEL_RE = re.compile(
    r"claude-(?:(?P<old_major>\d)(?:-(?P<old_minor>\d))?-)?"
    r"(?P<family>opus|sonnet|haiku|fable|mythos)"
    r"(?:-(?P<major>\d{1,2})(?!\d)(?:[-.](?P<minor>\d{1,2})(?!\d))?)?"
)

PROVIDER_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "zai": "ZAI_API_KEY",
}


def validate_llm_provider(provider: str) -> None:
    """Raise ValueError when provider is not supported."""
    if provider not in SUPPORTED_LLM_PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. Expected one of: "
            f"{', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}"
        )


def clean_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def resolve_llm_provider(persona: Mapping[str, Any] | None) -> str:
    """Resolve LLM provider with request data taking precedence over env.

    Raises:
        ValueError: If the resolved provider is not supported.
    """
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
    validate_llm_provider(provider)
    return provider


def resolve_llm_model(provider: str, persona: Mapping[str, Any] | None) -> str:
    """Resolve provider model with request > provider env > generic env > default.

    Raises:
        ValueError: If provider is not supported.
    """
    validate_llm_provider(provider)
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


def parse_anthropic_model(model: str) -> tuple[str, tuple[int, int]] | None:
    """Family and (major, minor) generation of a Claude model id, or None.

    None means the id names no generation this parser knows (an alias, or a
    naming scheme newer than it), not that the model does not exist.
    """
    match = _ANTHROPIC_MODEL_RE.search(model.lower())
    if not match:
        return None
    family = match.group("family")
    if match.group("old_major"):
        return family, (int(match.group("old_major")), int(match.group("old_minor") or 0))
    if match.group("major"):
        return family, (int(match.group("major")), int(match.group("minor") or 0))
    return None


def resolve_anthropic_thinking(model: str) -> str:
    """The fastest thinking setting the Anthropic model accepts for a voice turn.

    Returns ``disabled``, ``between_tools``, ``adaptive`` or ``omit`` (send no
    thinking parameter at all). Voice turns want thinking off, but every
    generation draws that line differently and a request the model rejects
    leaves the bot mute, so the choice follows the model id alone, with no
    operator override to combine wrongly with a per-bot ``llm_model``.
    Rules as of 2026-10:

    - Fable and Mythos: thinking is always on; ``disabled`` is rejected.
    - Before 3.7: no thinking support; any thinking parameter is rejected.
    - 3.7 up to 4.5: ``disabled`` is accepted; ``adaptive`` does not exist yet.
    - 4.6 up to 5.0, and Haiku 5.5: ``disabled`` is accepted (Opus 5 and
      Haiku 5.5 only at effort ``high`` or below, which is what a request
      without an effort setting gets).
    - Sonnet 5.5: ``disabled`` is rejected; ``between_tools`` is its off
      switch and takes no other field.
    - Opus 5.5: thinking is always on.
    - Anything newer, and any id the parser does not recognise: ``adaptive``,
      the one setting every model since 4.6 accepts.

    Explicit settings also keep pipecat from adding ``disabled`` itself on
    Sonnet 5+ requests that omit the parameter. ``omit`` is only returned for
    pre-3.7 ids, which pipecat does not touch.
    """
    parsed = parse_anthropic_model(model)
    if parsed is None:
        return "adaptive"
    family, version = parsed
    if family in ("fable", "mythos"):
        return "adaptive"
    if version < (3, 7):
        return "omit"
    if version < (5, 5):
        return "disabled"
    if version == (5, 5):
        return {"haiku": "disabled", "sonnet": "between_tools", "opus": "adaptive"}[family]
    return "adaptive"


def resolve_openai_api_surface() -> str:
    """Resolve OpenAI API surface. Responses is preferred for newest models.

    Raises:
        ValueError: If OPENAI_API_SURFACE is not supported.
    """
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


def missing_llm_provider_credential(
    provider: str, request_key: str | None = None
) -> str | None:
    """Return the missing provider key env var, or None when configured.

    A key supplied with the request (bring your own key) satisfies the check
    without the server-wide env var being set.

    Raises:
        ValueError: If provider is not supported.
    """
    validate_llm_provider(provider)
    if clean_string(request_key):
        return None
    key_env = PROVIDER_KEY_ENVS[provider]
    if clean_string(os.getenv(key_env)):
        return None
    return key_env
