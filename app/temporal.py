"""When does «الان» mean? The server's clock, never the model's sense of time.

The problem this module exists for
----------------------------------
Persian puts time into a sentence with a word rather than a date: «الان»،
«همین الان»، «قبلاً»، «چند دقیقه پیش»، «دیروز»، «فردا»، «هفته پیش»، «بعداً»،
«دوباره»، «هنوز». A model reading a transcript has to place all of those
relative to *now*, and it has no clock — so it supplies one, and the brief names
the failure exactly: **do not extract dates from model guessing; use the server
clock and real timestamps.**

This module reads the words and reports the direction and the granularity, with
the server's clock as the only reference point. It does not produce a date: a
date would be a claim, and «چند دقیقه پیش» does not contain one. What it
produces is what the words do say — this points backwards, at a scale of
minutes; this points forwards, at a scale of days — plus a bounded offset for
the expressions that carry one («دیروز» is a day).

What it is not
--------------
* **Not a date parser.** It never invents a calendar date from a relative word.
  ``seconds`` is the *offset the words state*, and it is 0 when they state none.
* **Not authority.** It is evidence for the prompt. Nothing branches on it.
* **Not a second clock.** ``now`` is passed in by the caller — the same value
  the pass already read — so there is exactly one notion of "now" in a pass.

The vocabulary
--------------
``past`` | ``now`` | ``future`` | ``repeat``. ``repeat`` is «دوباره»/«بازم»/
«مجدداً»: it says *this has happened before*, which is a fact about the room
rather than a location in time, and it is the one aspectual word worth carrying
because a room that is being asked the same thing twice is worth noticing.

«هنوز» reads as ``now`` — it is about the state at the present moment ("still
has not"), and the present is what it points at.

A demonstrative and a time noun are also a time: «این هفته» is the present week,
«اون موقع» and «اون روز» point back at one already established. The near/far
split is the one ``app/referents.py`` reads for people, applied to time — near
is the present, far is the past — and it is the reason both readers consult the
same ``TEMPORAL_NOUNS`` list rather than each keeping one.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

WHEN_PAST = "past"
WHEN_NOW = "now"
WHEN_FUTURE = "future"
WHEN_REPEAT = "repeat"

WHENS = (WHEN_PAST, WHEN_NOW, WHEN_FUTURE, WHEN_REPEAT)

UNIT_MINUTE = "minute"
UNIT_HOUR = "hour"
UNIT_DAY = "day"
UNIT_WEEK = "week"
UNIT_MONTH = "month"
UNIT_YEAR = "year"

UNITS = (UNIT_MINUTE, UNIT_HOUR, UNIT_DAY, UNIT_WEEK, UNIT_MONTH, UNIT_YEAR)

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
# Persian months and years are calendar lengths and vary; these are the
# *approximate* spans the words denote, and they are labelled as approximations
# wherever they are rendered. The module never turns them into a date, so the
# approximation cannot become a wrong date — only a wrong "about a month ago".
MONTH = 30 * DAY
YEAR = 365 * DAY


# ── Folding ───────────────────────────────────────────────────────────────
# The shared fold is ``people.normalize``, reused rather than copied for the
# reason every other reader in this codebase reuses it. It maps the ZWNJ to a
# space in «همینالان» but leaves it in «هماکنون», so this module strips any
# remaining joiner itself rather than depending on which side of that the fold
# lands: a temporal phrase is a phrase, and the joiner is not a letter.
def _fold(text: str | None) -> str:
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a read fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    folded = folded.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    return " ".join(folded.split())


# ── The phrases ───────────────────────────────────────────────────────────
# (phrase, kind, unit, seconds), matched against the folded text as a padded
# string so a phrase only matches on token boundaries. **Longest first**: the
# table is scanned in order and the first hit wins, so «چند دقیقه پیش» must be
# tried before «دقیقه پیش», and «نیم ساعت پیش» before «ساعت پیش» — the two pairs
# differ only in the offset they state, which a kind-only assertion would not
# catch. That ordering is the whole correctness argument for this table and is
# asserted by a test. The words that *state* a direction come before the ones
# that inherit it from the room — see ``_DEICTIC_PHRASES`` below.
_EXPLICIT_PHRASES: tuple[tuple[str, str, str, int], ...] = (
    # — now —
    ("همین الان", WHEN_NOW, "", 0),
    ("همین حالا", WHEN_NOW, "", 0),
    ("همین لحظه", WHEN_NOW, "", 0),
    ("در حال حاضر", WHEN_NOW, "", 0),
    ("هماکنون", WHEN_NOW, "", 0),
    ("هما کنون", WHEN_NOW, "", 0),
    ("الان", WHEN_NOW, "", 0),
    ("حالا", WHEN_NOW, "", 0),
    ("فعلا", WHEN_NOW, "", 0),
    ("امروز", WHEN_NOW, UNIT_DAY, 0),
    ("امسال", WHEN_NOW, UNIT_YEAR, 0),
    ("هنوز", WHEN_NOW, "", 0),
    ("هنوزم", WHEN_NOW, "", 0),
    # — past, with a stated span —
    ("چند دقیقه پیش", WHEN_PAST, UNIT_MINUTE, 0),
    ("چند لحظه پیش", WHEN_PAST, UNIT_MINUTE, 0),
    ("چند ساعت پیش", WHEN_PAST, UNIT_HOUR, 0),
    ("چند روز پیش", WHEN_PAST, UNIT_DAY, 0),
    # «چند وقت پیش» states no unit at all — it is "a while ago" and nothing more
    # precise, so it carries the direction and no scale, exactly as «قبلاً» does.
    ("چند وقت پیش", WHEN_PAST, "", 0),
    ("یه ربع پیش", WHEN_PAST, UNIT_MINUTE, 0),
    ("یک ربع پیش", WHEN_PAST, UNIT_MINUTE, 0),
    ("نیم ساعت پیش", WHEN_PAST, UNIT_HOUR, HOUR // 2),
    ("یه دقیقه پیش", WHEN_PAST, UNIT_MINUTE, MINUTE),
    ("یک دقیقه پیش", WHEN_PAST, UNIT_MINUTE, MINUTE),
    ("یه ساعت پیش", WHEN_PAST, UNIT_HOUR, HOUR),
    ("یک ساعت پیش", WHEN_PAST, UNIT_HOUR, HOUR),
    ("دقیقه پیش", WHEN_PAST, UNIT_MINUTE, 0),
    ("لحظه پیش", WHEN_PAST, UNIT_MINUTE, 0),
    ("ساعت پیش", WHEN_PAST, UNIT_HOUR, 0),
    ("هفته پیش", WHEN_PAST, UNIT_WEEK, WEEK),
    ("هفته قبل", WHEN_PAST, UNIT_WEEK, WEEK),
    ("ماه پیش", WHEN_PAST, UNIT_MONTH, MONTH),
    ("ماه قبل", WHEN_PAST, UNIT_MONTH, MONTH),
    ("سال پیش", WHEN_PAST, UNIT_YEAR, YEAR),
    ("سال قبل", WHEN_PAST, UNIT_YEAR, YEAR),
    ("دیروز", WHEN_PAST, UNIT_DAY, DAY),
    ("دیشب", WHEN_PAST, UNIT_DAY, DAY),
    ("پریروز", WHEN_PAST, UNIT_DAY, 2 * DAY),
    ("پارسال", WHEN_PAST, UNIT_YEAR, YEAR),
    ("پیشتر", WHEN_PAST, "", 0),
    ("سابقا", WHEN_PAST, "", 0),
    ("قبلا", WHEN_PAST, "", 0),
    ("قبل", WHEN_PAST, "", 0),
    ("گذشته", WHEN_PAST, "", 0),
    # — future —
    ("پس فردا", WHEN_FUTURE, UNIT_DAY, 2 * DAY),
    ("هفته بعد", WHEN_FUTURE, UNIT_WEEK, WEEK),
    ("هفته اینده", WHEN_FUTURE, UNIT_WEEK, WEEK),
    ("ماه بعد", WHEN_FUTURE, UNIT_MONTH, MONTH),
    ("ماه اینده", WHEN_FUTURE, UNIT_MONTH, MONTH),
    ("سال بعد", WHEN_FUTURE, UNIT_YEAR, YEAR),
    ("سال اینده", WHEN_FUTURE, UNIT_YEAR, YEAR),
    ("فردا", WHEN_FUTURE, UNIT_DAY, DAY),
    ("بعدا", WHEN_FUTURE, "", 0),
    ("بعدها", WHEN_FUTURE, "", 0),
    ("در اینده", WHEN_FUTURE, "", 0),
    ("اینده", WHEN_FUTURE, "", 0),
    # — repeat —
    ("دوباره", WHEN_REPEAT, "", 0),
    ("بازم", WHEN_REPEAT, "", 0),
    ("باز هم", WHEN_REPEAT, "", 0),
    # «مجدد» and its two written forms: the bare word, the colloquial «مجدا»,
    # and «مجددا» — which is what «مجدداً» folds to, because the fold drops the
    # tanwin but keeps the alef it sits on.
    ("مجدد", WHEN_REPEAT, "", 0),
    ("مجددا", WHEN_REPEAT, "", 0),
    ("مجدا", WHEN_REPEAT, "", 0),
    ("از نو", WHEN_REPEAT, "", 0),
)

# A demonstrative directly before one of these is not pointing at a person:
# «همین الان» and «این هفته» are times. Exported because two other readers
# consult it — ``referents`` will not read a demonstrative before a time noun as
# a person, and ``discourse`` will not read a question word before one as a
# question. The fact is shared rather than copied three times.
TEMPORAL_NOUNS = frozenset(
    {
        "الان", "حالا", "لحظه", "موقع", "وقت", "بار", "دفعه", "مرتبه",
        "هفته", "ماه", "سال", "روز", "شب", "صبح", "ظهر", "عصر", "دقیقه",
        "ساعت", "دیروز", "فردا", "امروز", "مدت",
    }
)

# «این هفته»، «همین موقع»، «اون روز»: a demonstrative and a time noun, which is
# how Persian says "this week" and "that time". Near points at the present, far
# points back — the same near/far split ``referents`` reads for people, applied
# to time. Generated rather than written out because it is the cross product of
# two short lists, and a table that is *built* cannot fall out of step with the
# nouns in it.
_DEICTIC_NEAR = ("این", "همین")
_DEICTIC_FAR = ("اون", "همون")
_DEICTIC_UNITS = (
    ("موقع", ""),
    ("وقت", ""),
    ("مدت", ""),
    ("هفته", UNIT_WEEK),
    ("ماه", UNIT_MONTH),
    ("سال", UNIT_YEAR),
    ("روز", UNIT_DAY),
    ("شب", UNIT_DAY),
)
_DEICTIC_PHRASES: tuple[tuple[str, str, str, int], ...] = tuple(
    (f"{demonstrative} {noun}", WHEN_NOW, unit, 0)
    for demonstrative in _DEICTIC_NEAR
    for noun, unit in _DEICTIC_UNITS
) + tuple(
    (f"{demonstrative} {noun}", WHEN_PAST, unit, 0)
    for demonstrative in _DEICTIC_FAR
    for noun, unit in _DEICTIC_UNITS
)

# The whole table, and the order is the reading. The explicit words are scanned
# first, so a message that carries both («فردا اون موقع») reads by the word that
# states a direction rather than the one that inherits it from the room — the
# same "the stronger evidence decides" rule ``discourse`` uses for acts.
_PHRASES: tuple[tuple[str, str, str, int], ...] = _EXPLICIT_PHRASES + _DEICTIC_PHRASES

_OBJECT_MARKERS = frozenset({"رو", "را"})


@dataclass(frozen=True)
class When:
    """What the words say about time, and how much they say."""

    kind: str = ""
    unit: str = ""
    seconds: int = 0
    surface: str = ""
    why: str = ""

    def __bool__(self) -> bool:
        return bool(self.kind)


def read_when(text: str | None) -> When:
    """The temporal expression in ``text``, or an empty reading.

    Longest phrase first, and the first hit wins. The surface is taken from the
    *original* text so a log reads the way the message did.
    """
    folded = _fold(text)
    if not folded:
        return When()

    padded = f" {folded} "
    for needle, kind, unit, seconds, phrase, folded_phrase in _needles():
        if needle in padded:
            return When(
                kind=kind,
                unit=unit,
                seconds=seconds,
                surface=_surface(text, phrase, folded_phrase),
                why=f"the time word «{phrase}»",
            )
    return When()


# The folded table, built once on first use. The fold is the shared one and it
# is not free; folding 88 phrases on every call would spend most of the reader's
# time re-deriving a constant. Built lazily rather than at import so it is
# folded under the same environment the reads happen in — an import-time build
# could fold before the shared fold is importable and cache the wrong spelling
# for the life of the process.
_NEEDLES: tuple[tuple[str, str, str, int, str, str], ...] | None = None


def _needles() -> tuple[tuple[str, str, str, int, str, str], ...]:
    global _NEEDLES
    if _NEEDLES is None:
        _NEEDLES = tuple(
            (f" {_fold(phrase)} ", kind, unit, seconds, phrase, _fold(phrase))
            for phrase, kind, unit, seconds in _PHRASES
        )
    return _NEEDLES


def _surface(text: str | None, phrase: str, folded_phrase: str = "") -> str:
    """The words the message actually used, as close to them as we can get.

    The fold is lossy — it removes diacritics and normalises the letters — so
    this walks the original tokens looking for the one whose folded form starts
    the phrase, and returns the original span. Falling back to the folded phrase
    is honest: it is what was matched.
    """
    tokens = re.split(r"[^\w\u0600-\u06ff]+", str(text or ""))
    folded = folded_phrase or _fold(phrase)
    parts = folded.split()
    if not parts:
        return folded
    first, length = parts[0], len(parts)
    for index, token in enumerate(tokens):
        if _fold(token) == first:
            span = [t for t in tokens[index : index + length] if t]
            if span:
                return " ".join(span)
    return folded


# ── Rendering ─────────────────────────────────────────────────────────────
_DIRECTION = {
    WHEN_PAST: "backwards, before now",
    WHEN_NOW: "at the present moment",
    WHEN_FUTURE: "forwards, after now",
    WHEN_REPEAT: "at something that has happened before",
}

_UNIT_SPAN = {
    UNIT_MINUTE: "at a scale of minutes",
    UNIT_HOUR: "at a scale of hours",
    UNIT_DAY: "at a scale of days",
    UNIT_WEEK: "at a scale of weeks",
    UNIT_MONTH: "at a scale of months (about)",
    UNIT_YEAR: "at a scale of years (about)",
}


def _ago(seconds: int) -> str:
    """A span in words, for the offsets that state one."""
    if seconds <= 0:
        return ""
    if seconds < HOUR:
        return f"about {max(1, seconds // MINUTE)} minute(s) ago"
    if seconds < DAY:
        return f"about {max(1, seconds // HOUR)} hour(s) ago"
    if seconds < WEEK:
        return f"about {max(1, seconds // DAY)} day(s) ago"
    if seconds < MONTH:
        return f"about {max(1, seconds // WEEK)} week(s) ago"
    if seconds < YEAR:
        return f"about {max(1, seconds // MONTH)} month(s) ago"
    return f"about {max(1, seconds // YEAR)} year(s) ago"


def render(when: When, *, now: int = 0, window_start: int = 0) -> str:
    """One line for the prompt, or nothing when there is no time word.

    ``now`` and ``window_start`` are the server's own numbers, passed in rather
    than read, because a second clock is a second answer. The line tells the
    model what the words point at *and* how old the window it is reading is,
    which is the pair it needs to place «قبلاً» without inventing a date.
    """
    if not when:
        return ""
    line = f"\nThe message says «{when.surface}», which points {_DIRECTION[when.kind]}"
    if when.unit:
        line += f" {_UNIT_SPAN[when.unit]}"
    span = _ago(when.seconds)
    if span:
        line += f" — {span}"
    line += ". That is the server's clock, not a reading of the words.\n"
    if window_start and now and window_start < now:
        line += (
            f"The window this pass is reading starts {_ago(now - window_start)}; "
            "work from the server's times, not from your own sense of when this "
            "is.\n"
        )
    return line
