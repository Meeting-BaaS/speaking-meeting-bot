"""The events a bot POSTs to its ``callback_url``, declared for the OpenAPI document.

Nothing here is served. FastAPI publishes these under ``webhooks`` in the
schema (OpenAPI 3.1) so the reference and generated clients know the four
payloads a caller must be ready to receive; delivery itself lives in
``utils.customer_events``.
"""

from fastapi import APIRouter

from app.models import EndedEvent, EscalationEvent, OwnerMessageDeliveredEvent, UtteranceEvent
from utils.customer_events import (
    EVENT_ENDED,
    EVENT_ESCALATION,
    EVENT_OWNER_MESSAGE_DELIVERED,
    EVENT_UTTERANCE,
)

webhooks = APIRouter()

DELIVERY_CONTRACT = (
    "Delivered as an HTTP POST to the bot's `callback_url`, with the event name in the "
    "`x-speaking-bot-event` header and, when the bot was created with a `callback_secret`, "
    "`x-speaking-bot-signature: sha256=<hex>`: the HMAC-SHA256 of the exact request body "
    "with that secret. Answer with any 2xx. Network errors and 5xx are retried three times "
    "(after 1, 3 and 9 seconds); a redirect counts as a failed delivery and is not followed."
)

_RESPONSES = {200: {"description": "Received. Any 2xx counts as delivered."}}


@webhooks.post(
    EVENT_UTTERANCE,
    operation_id="speaking_bot_utterance",
    tags=["events"],
    summary="Bot utterance",
    responses=_RESPONSES,
)
def utterance(body: UtteranceEvent) -> None:
    """The bot finished saying something.

    One event per spoken turn, including the entry message and anything
    relayed from the owner. `data.text` is what went to text-to-speech.
    """


@webhooks.post(
    EVENT_ESCALATION,
    operation_id="speaking_bot_escalation",
    tags=["events"],
    summary="Escalation raised",
    responses=_RESPONSES,
)
def escalation(body: EscalationEvent) -> None:
    """A representative bot handed a decision back to its owner.

    Fires the moment the bot calls its escalate tool, before anyone has
    answered. Reply with `POST /bots/{bot_id}/escalations/{escalation_id}/response`
    and the bot relays the answer to the meeting.
    """


@webhooks.post(
    EVENT_OWNER_MESSAGE_DELIVERED,
    operation_id="speaking_bot_owner_message_delivered",
    tags=["events"],
    summary="Owner message delivered",
    responses=_RESPONSES,
)
def owner_message_delivered(body: OwnerMessageDeliveredEvent) -> None:
    """The bot relayed an owner message or an escalation answer to the meeting.

    `data.message_id` matches the `message_id` returned when it was queued.
    """


@webhooks.post(
    EVENT_ENDED,
    operation_id="speaking_bot_ended",
    tags=["events"],
    summary="Bot ended",
    responses=_RESPONSES,
)
def ended(body: EndedEvent) -> None:
    """The bot has left the meeting; nothing follows this event.

    Carries the final usage report, the briefing when one was requested and
    every escalation of the call with its answer. Unlike the others this one
    is awaited before the bot's process exits, so it is delivered even when
    the call ends abruptly.
    """


for _route in webhooks.routes:
    _route.description = f"{_route.description}\n\n{DELIVERY_CONTRACT}"
