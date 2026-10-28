"""Per-pod addressing when the API runs as several StatefulSet replicas.

A bot lives on one pod: its Pipecat child, its websockets and its floor file
are there and nowhere else. Each pod therefore puts its own public prefix
(/p<ordinal>) in the websocket and webhook URLs it hands to MeetingBaas, and
the ingress routes that prefix to that pod alone. Bots sent to the same meeting
share a floor file, so they are placed on one pod chosen from the meeting URL;
a pod that receives a request for a meeting it does not own forwards it there
(app/peer_forwarding.py).

With POD_NAME unset (one process, local dev) every helper is a no-op.
"""

import os
import re

from utils.floor import floor_key

FORWARDED_HEADER = "x-speaking-forwarded"
_ORDINAL = re.compile(r"-(\d+)$")


def pod_name() -> str | None:
    return os.getenv("POD_NAME") or None


def pod_ordinal() -> int | None:
    name = pod_name()
    if not name:
        return None
    match = _ORDINAL.search(name)
    return int(match.group(1)) if match else None


def pod_route_prefix() -> str:
    ordinal = pod_ordinal()
    return "" if ordinal is None else f"/p{ordinal}"


def with_pod_prefix(base_url: str) -> str:
    return base_url.rstrip("/") + pod_route_prefix()


def replica_count() -> int:
    try:
        return max(1, int(os.getenv("SPEAKING_REPLICAS", "1")))
    except ValueError:
        return 1


def owner_ordinal(meeting_url: str, replicas: int | None = None) -> int:
    """The pod that hosts every bot of this meeting."""
    count = replicas if replicas is not None else replica_count()
    return int(floor_key(meeting_url), 16) % count


def peer_base_url(ordinal: int) -> str | None:
    template = os.getenv("SPEAKING_PEER_URL_TEMPLATE")
    if not template:
        return None
    return template.format(ordinal=ordinal).rstrip("/")


def peer_ordinals() -> list[int]:
    """Every other pod, for fan-out lookups."""
    me = pod_ordinal()
    if me is None:
        return []
    return [n for n in range(replica_count()) if n != me]
