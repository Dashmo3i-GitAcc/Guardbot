"""Detecting "I need a VPN" in group chatter.

The vocabulary lives in ``intent_rules.json`` next to this file, so the words
can be extended — new spellings, new slang, new tools — without touching code.
This module is only the engine.

Two ideas keep it from firing on ordinary conversation:

**A topic is not an intent.** Someone mentioning a VPN is not someone asking
for one. A message only counts when it is *about* circumvention **and** carries
a supporting signal — they want something, or something is broken. "vpn" on its
own matches nothing; "vpn وصل نمیشه" does.

**Text is unified before matching.** Persian in the wild mixes Arabic and
Persian letter forms, sprinkles ZWNJ, and writes digits both ways. All of that
is folded away first, and the same folding is applied to the patterns at load
time, so rules can be written in plain Persian. Spacing is left alone except
for collapsing runs, and patterns use ``\\s*`` between words so one rule covers
``فیلترشکن``, ``فیلتر شکن`` and ``فیلتر  شکن``.

Everything here is pure: no Telegram, no database, no network. That is what
makes it testable against a realistic corpus.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "intent_rules.json"

# ── Normalisation ─────────────────────────────────────────────────────────
# Letter forms: Arabic yeh/kaf and friends become their Persian equivalents so
# "ميخوام" and "میخوام" are the same word.
_CHAR_MAP = {
    "\u064a": "\u06cc",  # ARABIC YEH
    "\u0649": "\u06cc",  # ALEF MAKSURA
    "\u06d2": "\u06cc",  # YEH BARREE
    "\u06d0": "\u06cc",
    "\u0643": "\u06a9",  # ARABIC KAF
    "\u06aa": "\u06a9",
    "\u0629": "\u0647",  # TEH MARBUTA
    "\u06c0": "\u0647",
    "\u06c1": "\u0647",
    "\u0623": "\u0627",  # ALEF WITH HAMZA
    "\u0625": "\u0627",
    "\u0622": "\u0627",  # ALEF WITH MADDA
    "\u0621": "",        # standalone HAMZA
    "\u0654": "",
    "\u0655": "",
    "\u0640": "",        # TATWEEL
}

# Persian and Arabic-Indic digits both become ASCII, so a rule only needs one
# spelling of a number.
_DIGIT_MAP = str.maketrans(
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
    "01234567890123456789",
)

# Arabic punctuation becomes its ASCII counterpart.
_PUNCT_MAP = str.maketrans(
    {"\u061f": "?", "\u060c": ",", "\u061b": ";", "\u066a": "%", "\u066b": ".", "\u066c": ","}
)

# Zero-width and bidi-control characters: invisible, and they would otherwise
# split a word in half.
_ZERO_WIDTH = dict.fromkeys(
    [
        0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF,
    ]
)

# Harakat and superscript alef: vocalisation that changes spelling, not meaning.
_DIACRITICS = dict.fromkeys(range(0x064B, 0x0653))
_DIACRITICS[0x0670] = None

_WHITESPACE = re.compile(r"\s+")

_CHAR_TRANSLATE = {ord(k): v for k, v in _CHAR_MAP.items()}


def normalise(text: str | None, *, collapse: bool = True) -> str:
    """Fold the ways Persian is actually typed into one canonical spelling.

    ``collapse`` is off when normalising a rule pattern: collapsing is safe for
    text but a pattern may legitimately contain literal spacing that the author
    wants kept.
    """
    if not text:
        return ""
    out = text.casefold()
    out = out.translate(_DIGIT_MAP).translate(_PUNCT_MAP).translate(_CHAR_TRANSLATE)
    out = out.translate(_ZERO_WIDTH).translate(_DIACRITICS)
    if collapse:
        out = _WHITESPACE.sub(" ", out).strip()
    return out


# ── Rules ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuleSet:
    topic: tuple
    support: dict
    standalone: tuple
    ignore: tuple


@dataclass(frozen=True)
class IntentMatch:
    """The verdict, plus enough detail to explain it in a log or a test."""

    matched: bool
    score: int
    reasons: tuple
    normalised: str

    def __bool__(self) -> bool:
        return self.matched


def rules_path() -> str:
    """Where the rules come from. ``INTENT_RULES_PATH`` overrides the default."""
    from . import config

    override = (config.INTENT_RULES_PATH or "").strip()
    return override or str(_DEFAULT_RULES_PATH)


def _compile(patterns) -> tuple:
    compiled = []
    for pattern in patterns or ():
        # The pattern is normalised exactly like the text it will meet, so a
        # rule can be written in ordinary Persian.
        try:
            compiled.append(re.compile(normalise(pattern, collapse=False)))
        except re.error as exc:
            raise ValueError(f"invalid intent pattern {pattern!r}: {exc}") from exc
    return tuple(compiled)


@lru_cache(maxsize=4)
def _load(path: str) -> RuleSet:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    groups = raw.get("groups") or {}
    topic_groups = raw.get("topic_groups") or ["topic"]
    support_groups = raw.get("support_groups") or ["request"]

    def patterns_of(name: str):
        return (groups.get(name) or {}).get("patterns") or []

    topic = tuple(
        pattern
        for name in topic_groups
        for pattern in _compile(patterns_of(name))
    )
    support = {
        name: _compile(patterns_of(name))
        for name in support_groups
        if patterns_of(name)
    }
    standalone = _compile((raw.get("standalone") or {}).get("patterns"))
    ignore = _compile((raw.get("ignore") or {}).get("patterns"))

    if not topic:
        raise ValueError(f"{path}: no topic patterns configured")
    return RuleSet(topic, support, standalone, ignore)


def load_rules(path: str | None = None) -> RuleSet:
    return _load(path or rules_path())


def reload_rules() -> None:
    """Forget the parsed rules. For tests and for a config change at runtime."""
    _load.cache_clear()


def detect(
    text: str | None,
    *,
    rules: RuleSet | None = None,
    require_topic: bool | None = None,
    min_length: int | None = None,
) -> IntentMatch:
    """Decide whether ``text`` is someone asking about getting a VPN.

    ``require_topic`` and ``min_length`` default to the configured values; they
    are parameters so the tests can pin both behaviours.
    """
    from . import config

    normalised = normalise(text)
    if min_length is None:
        min_length = config.INTENT_MIN_LENGTH
    if require_topic is None:
        require_topic = config.INTENT_REQUIRE_TOPIC

    if len(normalised) < max(1, int(min_length)):
        return IntentMatch(False, 0, (), normalised)

    rules = rules or load_rules()

    # Hard vetoes first: a competing seller is not a customer.
    for pattern in rules.ignore:
        if pattern.search(normalised):
            return IntentMatch(False, 0, ("ignore",), normalised)

    topic_hits = [p.pattern for p in rules.topic if p.search(normalised)]
    support_hits = {
        name: [p.pattern for p in patterns if p.search(normalised)]
        for name, patterns in rules.support.items()
    }
    support_hits = {name: hits for name, hits in support_hits.items() if hits}
    standalone_hits = [p.pattern for p in rules.standalone if p.search(normalised)]

    reasons: list[str] = []
    score = 0
    if topic_hits:
        reasons.append("topic")
        score += 2
    for name in support_hits:
        reasons.append(name)
        score += 1
    if standalone_hits:
        reasons.append("standalone")
        score += 3

    if standalone_hits:
        matched = True
    elif topic_hits and support_hits:
        matched = True
    else:
        # Without a topic this only fires when the operator has deliberately
        # relaxed the rule. The default is to stay quiet.
        matched = bool(support_hits) and not require_topic

    return IntentMatch(matched, score, tuple(reasons), normalised)
