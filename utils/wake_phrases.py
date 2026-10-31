"""Every way the bot's name may come out of the speech-to-text.

``respond_mode: mentioned`` starts a turn only when a final transcription
contains the bot's name, matched as a literal word. Names that are not
dictionary words rarely survive transcription as written: a bot called
"Baas" was heard as "Bass" and "Bess" and never answered. Two defences:
the name (and any aliases the caller gives) is sent to Deepgram as a key
term so it is recognised as written, and the wake phrases include the
obvious variants, such as a compound name split into words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_WAKE_PHRASES = 12

_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")
_NON_WORD = re.compile(r"[^0-9A-Za-z ]+")


def _clean(value: str) -> str:
    return " ".join(_NON_WORD.sub(" ", value).split()).strip().lower()


def name_variants(name: str) -> list[str]:
    """The name as written, split on case changes and digits, and joined."""
    variants: list[str] = []
    base = _clean(name)
    if base:
        variants.append(base)
    split = _clean(_CAMEL_SPLIT.sub(" ", name))
    if split and split not in variants:
        variants.append(split)
    joined = base.replace(" ", "")
    if joined and joined not in variants and " " in base:
        variants.append(joined)
    return variants


def wake_phrases_for(display_name: str, aliases: Iterable[str] | None = None) -> list[str]:
    """Deduplicated, lower-cased phrases that should wake the bot."""
    phrases: list[str] = []
    for candidate in [display_name, *(aliases or [])]:
        for variant in name_variants(str(candidate or "")):
            if variant and variant not in phrases:
                phrases.append(variant)
    return phrases[:MAX_WAKE_PHRASES]


def keyterms_for(display_name: str, aliases: Iterable[str] | None = None) -> list[str]:
    """Terms to boost in the recogniser: the name as written and the aliases."""
    terms: list[str] = []
    for candidate in [display_name, *(aliases or [])]:
        term = " ".join(str(candidate or "").split())
        if term and term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    return terms[:MAX_WAKE_PHRASES]
