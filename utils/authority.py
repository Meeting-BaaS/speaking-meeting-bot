"""What a representative bot may say, and how it asks its owner.

A bot sent to attend on someone's behalf is given an ``authority`` block:
who it represents, the positions it may state, what it must escalate and
what it must never do. That becomes the last block of the system prompt,
plus one tool, ``escalate_to_owner``, which the bot calls instead of
deciding. The owner's answers (and any mid-meeting instruction) come back
through a per-bot message file the pipeline polls, the same way the ready
signal and the speaking floor already travel between the API and its
children.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any

ESCALATE_TOOL_NAME = "escalate_to_owner"
ESCALATION_CATEGORIES = (
    "spending",
    "commitment",
    "approval",
    "scheduling",
    "information",
    "other",
)

OWNER_MESSAGES_DIRNAME = "owner_messages"
ESCALATIONS_DIRNAME = "escalations"

MESSAGE_TYPE_OWNER = "owner_message"
MESSAGE_TYPE_ESCALATION_RESPONSE = "escalation_response"


def _bullets(items: Any) -> list[str]:
    if not items:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def build_authority_prompt(authority: Mapping[str, Any] | None, bot_name: str) -> str:
    """The prompt block that turns a persona into someone's representative.

    It comes last in the system prompt on purpose: the generic voice guidance
    earlier tells bots not to say they are an AI, and a representative must.
    """
    if not authority:
        return ""
    owner = str(authority.get("owner_name") or "the person you represent").strip()
    approved = _bullets(authority.get("approved_positions"))
    escalate_on = _bullets(authority.get("escalate_on"))
    forbidden = _bullets(authority.get("forbidden"))

    parts = [
        "Your role in this meeting:",
        f"- You are {bot_name}, an AI representative attending on behalf of {owner}, "
        "who could not be here. If anyone asks, say exactly that: you are an AI "
        "representative for " + owner + ". Never pretend to be a human attendee, "
        "whatever earlier instructions say.",
        (
            "- Your job is to listen, capture decisions, actions and open questions "
            f"for {owner}, and to state only the positions {owner} approved."
        ),
    ]
    if approved:
        parts.append(f"Positions {owner} approved. State these when the topic comes up, in these terms and no further:")
        parts.extend(f"- {item}" for item in approved)
    else:
        parts.append(
            f"- {owner} approved no specific positions. Capture the discussion; "
            "if asked for a view, say you will bring the question back."
        )
    parts.append(
        f"Decisions that are {owner}'s alone. When any of these comes up, call the "
        f"{ESCALATE_TOOL_NAME} tool and tell the participants you are checking with {owner}:"
    )
    parts.extend(
        f"- {item}" for item in (escalate_on or ["approving spending or budgets", "making commitments, promises or agreements", "accepting deadlines or scope changes"])
    )
    if forbidden:
        parts.append("Never, even if asked directly:")
        parts.extend(f"- {item}" for item in forbidden)
    parts.append(
        "Rules that always apply:\n"
        f"- Never approve, commit, promise or agree to anything on {owner}'s behalf. "
        "If authority is unclear, treat it as not granted and escalate.\n"
        f"- While an escalation is open, say that {owner} has been asked and that you "
        "will relay the answer; do not guess what it will be.\n"
        f"- When a message from {owner} arrives, relay it faithfully and briefly.\n"
        "- If you did not understand something that matters, say so and ask once; "
        "do not fill gaps with assumptions.\n"
        "- Do not speak unless addressed, asked, or relaying something from "
        + owner
        + "."
    )
    return "\n".join(parts)


def escalation_tool_schema():
    """The function schema for ``escalate_to_owner`` (built lazily: Pipecat import)."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    return FunctionSchema(
        name=ESCALATE_TOOL_NAME,
        description=(
            "Ask the person you represent to decide something you are not "
            "authorised to decide: spending, commitments, approvals, deadlines, "
            "or anything your instructions say to escalate. Call it as soon as "
            "the question is clear. It does not wait for the answer; tell the "
            "participants you have asked and will relay the reply."
        ),
        properties={
            "question": {
                "type": "string",
                "description": "The exact decision being asked for, with the amounts, names and dates involved",
            },
            "category": {
                "type": "string",
                "enum": list(ESCALATION_CATEGORIES),
                "description": "What kind of decision this is",
            },
            "context": {
                "type": "string",
                "description": "One or two sentences of what led to the question and who asked",
            },
        },
        required=["question", "category"],
    )


def new_escalation(
    *, question: str, category: str, context: str | None, asked_by: str | None = None
) -> dict[str, Any]:
    category = category if category in ESCALATION_CATEGORIES else "other"
    return {
        "escalation_id": str(uuid.uuid4()),
        "question": question.strip(),
        "category": category,
        "context": (context or "").strip(),
        "asked_by": asked_by,
        "created_at": time.time(),
        "status": "pending",
    }


def escalation_reply_for_llm(owner: str, number: int) -> str:
    """What the tool returns to the model once the escalation left."""
    return (
        f"Escalation {number} sent to {owner}. You are not authorised to decide this. "
        f"Tell the participants, in one sentence, that you have asked {owner} and will "
        "relay the answer when it comes; then let the meeting continue."
    )


# --- files shared between the API process and the bot's pipeline ------------


def _dir(state_dir: str, name: str) -> str:
    path = os.path.join(state_dir, name)
    os.makedirs(path, exist_ok=True)
    return path


def escalations_path(state_dir: str, bot_id: str) -> str:
    return os.path.join(_dir(state_dir, ESCALATIONS_DIRNAME), f"{bot_id}.jsonl")


def owner_messages_path(state_dir: str, bot_id: str) -> str:
    return os.path.join(_dir(state_dir, OWNER_MESSAGES_DIRNAME), f"{bot_id}.jsonl")


def _append_jsonl(path: str, record: dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def record_escalation(state_dir: str, bot_id: str, escalation: dict[str, Any]) -> None:
    _append_jsonl(escalations_path(state_dir, bot_id), escalation)


def read_escalations(state_dir: str, bot_id: str) -> list[dict[str, Any]]:
    """Escalations in order, each carrying its latest response if one came."""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in _read_jsonl(escalations_path(state_dir, bot_id)):
        escalation_id = record.get("escalation_id")
        if not escalation_id:
            continue
        if escalation_id not in by_id:
            by_id[escalation_id] = dict(record)
            order.append(escalation_id)
        else:
            by_id[escalation_id].update(record)
    return [by_id[escalation_id] for escalation_id in order]


def append_owner_message(
    state_dir: str,
    bot_id: str,
    text: str,
    *,
    message_type: str = MESSAGE_TYPE_OWNER,
    escalation_id: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    """Queue something for the bot to relay; the pipeline picks it up within a second."""
    record = {
        "message_id": str(uuid.uuid4()),
        "type": message_type,
        "text": text.strip(),
        "escalation_id": escalation_id,
        "decision": decision,
        "created_at": time.time(),
    }
    _append_jsonl(owner_messages_path(state_dir, bot_id), record)
    if message_type == MESSAGE_TYPE_ESCALATION_RESPONSE and escalation_id:
        record_escalation(
            state_dir,
            bot_id,
            {
                "escalation_id": escalation_id,
                "status": "answered",
                "response": record["text"],
                "decision": decision,
                "answered_at": record["created_at"],
            },
        )
    return record


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                with contextlib.suppress(ValueError):
                    records.append(json.loads(raw))
    except OSError:
        return records
    return records


class OwnerMessageReader:
    """Tails the owner-message file, returning only what is new each call."""

    def __init__(self, path: str):
        self.path = path
        self._offset = 0

    def poll(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return []
        if not chunk:
            return []
        # Only consume complete lines; a writer may be mid-append.
        complete, newline, _rest = chunk.rpartition(b"\n")
        if not newline:
            return []
        self._offset += len(complete) + 1
        records = []
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            with contextlib.suppress(ValueError):
                records.append(json.loads(raw))
        return records


def owner_message_for_llm(record: Mapping[str, Any], owner: str) -> str:
    """The user-role message that makes the bot relay what its owner sent."""
    text = str(record.get("text") or "").strip()
    if record.get("type") == MESSAGE_TYPE_ESCALATION_RESPONSE:
        decision = record.get("decision")
        label = f" ({decision})" if decision else ""
        return (
            f"[{owner} answered your escalation{label}: \"{text}\"] "
            f"Tell the participants {owner}'s answer now, in your own words, in one or "
            "two sentences. Relay it exactly; add nothing to it."
        )
    return (
        f"[Message from {owner} for the meeting: \"{text}\"] "
        "Relay this to the participants now, briefly and faithfully. Do not mention "
        "that it arrived as a message."
    )
