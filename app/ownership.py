"""Which API key a bot belongs to.

The auth middleware only checks that a key is present; MeetingBaas validates
it when the bot is created. The per-bot routes added for representatives
(messages, escalations, usage, briefing) must not be reachable with another
account's key and a guessed bot id, so the creating key's digest is stored
with the bot and compared on every such request. Only the digest is kept:
the key itself never lands in the persisted meeting details.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

OWNER_DIGEST_KEY = "owner_key_digest"


def key_digest(api_key: str | None) -> str:
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


def owned_by(record: dict[str, Any] | None, api_key: str | None) -> bool:
    """True when ``record`` carries the digest of ``api_key``.

    A record without a digest is never owned: it predates this check or was
    written by something else, and the safe answer is 404.
    """
    stored = (record or {}).get(OWNER_DIGEST_KEY)
    if not stored or not api_key:
        return False
    return hmac.compare_digest(str(stored), key_digest(api_key))
