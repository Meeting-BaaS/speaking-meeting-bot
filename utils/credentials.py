"""Per-bot provider credentials (bring your own key).

The API accepts optional provider keys on POST /bots. They reach the bot's
Pipecat child only through its environment, overriding the server-wide
variables the runner already reads, so the runner itself needs no knowledge
of where a key came from. Nothing here writes a key to disk or logs.
"""

from __future__ import annotations

from collections.abc import Mapping

from utils.llm_config import PROVIDER_KEY_ENVS, clean_string, validate_llm_provider

STT_KEY_ENV = "DEEPGRAM_API_KEY"
TTS_KEY_ENV = "CARTESIA_API_KEY"

LLM_CREDENTIAL = "llm_api_key"
STT_CREDENTIAL = "stt_api_key"
TTS_CREDENTIAL = "tts_api_key"


def credential_env_overrides(
    provider: str, credentials: Mapping[str, str] | None
) -> dict[str, str]:
    """Map supplied credentials to the env vars the Pipecat runner reads.

    Blank values are ignored so a caller cannot blank out a server key by
    sending an empty string.

    Raises:
        ValueError: If provider is not supported.
    """
    validate_llm_provider(provider)
    if not credentials:
        return {}

    mapping = {
        LLM_CREDENTIAL: PROVIDER_KEY_ENVS[provider],
        STT_CREDENTIAL: STT_KEY_ENV,
        TTS_CREDENTIAL: TTS_KEY_ENV,
    }
    overrides: dict[str, str] = {}
    for name, env_var in mapping.items():
        value = clean_string(credentials.get(name))
        if value:
            overrides[env_var] = value
    return overrides
