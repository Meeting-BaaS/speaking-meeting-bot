"""The briefing a representative bot sends its owner after the meeting.

Built from the bot's own conversation record (what it heard, labelled by
speaker where known, and what it said) plus the escalations it raised. One
LLM call on the bot's own provider and key, so a bot on the caller's key is
briefed on the caller's key. The output is structured so an app can show
decisions, actions and open items without parsing prose.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import Any

from utils.llm_config import (
    DEFAULT_ZAI_BASE_URL,
    resolve_llm_model,
    resolve_llm_provider,
)

BRIEFINGS_DIRNAME = "briefings"
DEFAULT_BRIEFING_LANGUAGE = "en"
MAX_TRANSCRIPT_CHARS = 60_000
LLM_TIMEOUT_SECS = 60.0

BRIEFING_KEYS = (
    "summary",
    "decisions",
    "actions",
    "unresolved",
    "what_the_representative_said",
    "escalations",
    "coverage_notes",
)


def bounded_transcript(lines: Sequence[str]) -> tuple[str, int]:
    """Join the lines; past the limit keep the opening and the end, and say so.

    Returns the text and how many characters of the middle were left out. The
    opening is kept because that is where a meeting's decisions tend to be
    framed; the end because that is what a question at the end is about.
    """
    joined = "\n".join(lines)
    if len(joined) <= MAX_TRANSCRIPT_CHARS:
        return joined, 0
    head_len = MAX_TRANSCRIPT_CHARS // 3
    tail_len = MAX_TRANSCRIPT_CHARS - head_len
    omitted = len(joined) - head_len - tail_len
    marker = f"\n[… {omitted} characters from the middle of the meeting are not in this record …]\n"
    return joined[:head_len] + marker + joined[-tail_len:], omitted


def transcript_lines(
    messages: Sequence[Mapping[str, Any]],
    bot_name: str,
    heard: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """One line per turn: ``Name: text`` for the room, ``<bot> (you): text`` for the bot.

    ``heard`` is the bot's full log of the meeting (every final transcription
    and its own turns, in order); when present it is the record, since in
    mentioned mode the model's context holds only the turns that named the bot.
    """
    lines: list[str] = []
    if heard:
        for entry in heard:
            text = " ".join(str(entry.get("text") or "").split())
            if not text:
                continue
            if entry.get("kind") == "bot":
                prefix = f"{bot_name}: "
                body = text[len(prefix) :] if text.startswith(prefix) else text
                lines.append(f"{bot_name} (you): {body}")
            elif _LABELLED_RE.match(text):
                lines.append(text)
            else:
                lines.append(f"Participant: {text}")
        return lines
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "").strip()
        if not content or role not in ("user", "assistant"):
            continue
        if role == "assistant":
            lines.append(f"{bot_name} (you): {content}")
        elif _LABELLED_RE.match(content):
            lines.append(content)
        else:
            lines.append(f"Participant: {content}")
    return lines


def transcript_text(
    messages: Sequence[Mapping[str, Any]],
    bot_name: str,
    heard: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """The bounded transcript text (see ``bounded_transcript``)."""
    text, _omitted = bounded_transcript(transcript_lines(messages, bot_name, heard))
    return text



def build_briefing_messages(
    *,
    bot_name: str,
    owner_name: str | None,
    language: str | None,
    instructions: str | None,
    transcript: str,
    escalations: Sequence[Mapping[str, Any]] = (),
    coverage: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """(system, user) messages for the briefing call."""
    owner = owner_name or "the person the bot represented"
    lang = language or DEFAULT_BRIEFING_LANGUAGE
    system = (
        "You write the post-meeting briefing an AI representative sends to the person "
        "it attended for. You only report what the transcript supports. Where the "
        "transcript is unclear or a segment could not be understood, say so instead "
        "of guessing. Never invent decisions, names, amounts or dates. Respond with "
        "one JSON object and nothing else."
    )
    escalation_lines = []
    for item in escalations:
        status = item.get("status", "pending")
        line = f"- [{status}] {item.get('question', '')}"
        if item.get("response"):
            line += f" -> answer: {item['response']}"
        escalation_lines.append(line)
    coverage_lines = []
    for key, value in (coverage or {}).items():
        coverage_lines.append(f"- {key}: {value}")

    user = (
        f"Write the briefing in this language: {lang}.\n"
        f"The representative was {bot_name}, attending for {owner}.\n"
        + (f"Owner's instructions for the briefing: {instructions.strip()}\n" if instructions else "")
        + "\nReturn JSON with exactly these keys:\n"
        '  "summary": 3 to 6 sentences on what the meeting covered and concluded,\n'
        '  "decisions": list of decisions actually taken (strings),\n'
        '  "actions": list of {"owner": who, "task": what, "due": when or null},\n'
        '  "unresolved": list of open questions and items that still need ' + owner + ",\n"
        '  "what_the_representative_said": list of the positions or statements the representative made,\n'
        '  "escalations": list of {"question", "status", "answer"} for what was escalated,\n'
        '  "coverage_notes": list of caveats: parts missed, unclear audio, late join, uncertain content.\n'
        "Use empty lists when there is nothing to report.\n\n"
        "Escalations raised during the meeting:\n"
        + ("\n".join(escalation_lines) if escalation_lines else "- none")
        + "\n\nCoverage facts:\n"
        + ("\n".join(coverage_lines) if coverage_lines else "- none recorded")
        + "\n\nTranscript:\n"
        + (transcript if transcript.strip() else "(no speech was captured)")
    )
    return system, user


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
# "Alice: ..." as written by the pipeline's speaker labeller.
_LABELLED_RE = re.compile(r"^[^:\n]{1,60}: ")


def parse_briefing(text: str) -> dict[str, Any]:
    """The model's JSON, with every expected key present; prose falls into summary."""
    candidate = text.strip()
    match = _FENCE_RE.search(candidate)
    if match:
        candidate = match.group(1).strip()
    data: Any
    try:
        data = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        try:
            data = json.loads(candidate[start : end + 1]) if start >= 0 < end else None
        except ValueError:
            data = None
    if not isinstance(data, dict):
        data = {"summary": text.strip()}
    briefing: dict[str, Any] = {}
    for key in BRIEFING_KEYS:
        value = data.get(key)
        if key == "summary":
            briefing[key] = str(value or "").strip()
        else:
            briefing[key] = list(value) if isinstance(value, list) else []
    return briefing


def _complete(provider: str, model: str, api_key: str, system: str, user: str) -> str:
    """One blocking completion on the chosen provider; returns the text."""
    if provider == "anthropic":
        import anthropic

        # Bounded: the call-end webhook waits for this before it answers.
        client = anthropic.Anthropic(api_key=api_key, timeout=LLM_TIMEOUT_SECS, max_retries=1)
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            getattr(block, "text", "") for block in response.content if getattr(block, "text", None)
        )

    import openai

    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": LLM_TIMEOUT_SECS, "max_retries": 1}
    if provider == "zai":
        kwargs["base_url"] = os.getenv("ZAI_BASE_URL") or DEFAULT_ZAI_BASE_URL
    client = openai.OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def generate_briefing(
    *,
    persona: Mapping[str, Any] | None,
    api_key: str,
    bot_name: str,
    messages: Sequence[Mapping[str, Any]],
    escalations: Sequence[Mapping[str, Any]] = (),
    coverage: Mapping[str, Any] | None = None,
    heard: Sequence[Mapping[str, Any]] | None = None,
    complete=_complete,
) -> dict[str, Any]:
    """Run the briefing call for one bot and return the structured result."""
    persona = persona or {}
    briefing_config = persona.get("briefing") or {}
    authority = persona.get("authority") or {}
    provider = resolve_llm_provider(persona)
    model = resolve_llm_model(provider, persona)
    language = briefing_config.get("language") or DEFAULT_BRIEFING_LANGUAGE
    transcript, omitted = bounded_transcript(transcript_lines(messages, bot_name, heard))
    if omitted:
        coverage = {
            **(coverage or {}),
            "transcript_truncated": (
                f"{omitted} characters from the middle of the meeting are not in the record; "
                "say so in coverage_notes and do not guess at that part"
            ),
        }
    system, user = build_briefing_messages(
        bot_name=bot_name,
        owner_name=authority.get("owner_name"),
        language=language,
        instructions=briefing_config.get("instructions"),
        transcript=transcript,
        escalations=escalations,
        coverage=coverage,
    )
    text = complete(provider, model, api_key, system, user)
    briefing = parse_briefing(text)
    briefing.update(
        {
            "language": language,
            "generated_at": time.time(),
            "llm_provider": provider,
            "llm_model": model,
        }
    )
    return briefing


def briefings_dir(state_dir: str) -> str:
    return os.path.join(state_dir, BRIEFINGS_DIRNAME)


def save_briefing(state_dir: str, bot_id: str, briefing: Mapping[str, Any]) -> str | None:
    directory = briefings_dir(state_dir)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{bot_id}.json")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f"{bot_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(dict(briefing), handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return None


def load_briefing(state_dir: str, *bot_ids: str | None) -> dict[str, Any] | None:
    for bot_id in bot_ids:
        if not bot_id:
            continue
        try:
            with open(os.path.join(briefings_dir(state_dir), f"{bot_id}.json")) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            continue
    return None
