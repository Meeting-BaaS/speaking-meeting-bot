"""Assemble the system prompt a bot runs on."""

from __future__ import annotations

from collections.abc import Mapping

from config.prompts import DEFAULT_SYSTEM_PROMPT, VOICE_STYLE_PROMPT


def build_system_prompt(
    persona: Mapping[str, object],
    persona_name: str,
    mcp_context: str = "",
    authority_context: str = "",
) -> str:
    """Persona prompt, extra knowledge, how to speak, MCP notes, then authority.

    The voice guidance is appended for every bot: it is what lets the pipeline
    speak the first sentence while the rest is still being generated. The
    authority block comes last so that what a representative may say wins over
    anything the persona or the voice guidance said before it.
    """
    parts = [str(persona["prompt"])]
    additional = str(persona.get("additional_content") or "")
    if additional:
        parts.append(f"You are {persona_name}\n\n{DEFAULT_SYSTEM_PROMPT.strip()}")
        parts.append(
            "You have the following additional context. Use it to inform your "
            f"responses:\n\n{additional}"
        )
    parts.append(VOICE_STYLE_PROMPT)
    if mcp_context:
        parts.append(mcp_context)
    if authority_context:
        parts.append(authority_context)
    return "\n\n".join(part.strip() for part in parts if part and part.strip())
