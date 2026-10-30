"""Keep a bot's input transport alive when audio arrives before it has started."""

from __future__ import annotations

from collections import deque

# The API replays at most 30 s of audio buffered during the child's startup, in
# chunks of a few hundred bytes; this comfortably holds all of it.
MAX_HELD_MESSAGES = 4000


def hold_messages_until_started(input_transport) -> None:
    """Patch ``input_transport.on_message`` to hold frames until the transport starts.

    Pipecat's WebsocketClientInputTransport only creates its audio queue in
    ``start()``, but its socket receives from the moment it connects, and the
    API replays the audio it buffered during the child's startup the instant
    the child connects. A message in that gap raised AttributeError inside the
    receive loop, which then exited for good: the bot kept speaking but never
    heard anything again (preprod, 2026-10-26). Held messages are delivered in
    order when the first message arrives after start, so the words said during
    startup still reach STT.
    """
    original = input_transport.on_message
    held: deque = deque(maxlen=MAX_HELD_MESSAGES)

    async def on_message(websocket, message):
        if not hasattr(input_transport, "_audio_in_queue"):
            held.append(message)
            return
        while held:
            await original(websocket, held.popleft())
        await original(websocket, message)

    input_transport.on_message = on_message
