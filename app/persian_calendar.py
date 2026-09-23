"""The Persian calendar, and the clock the awareness context reads the date from.

Why the server has to say what day it is
----------------------------------------
Nothing in a pass's context ever stated the date. The transcript carries only
*relative* ages — "+3m", "+2h" — which is the right rendering for reading how
recent something is and useless for answering «امروز چندمه؟». With no absolute
anchor anywhere, the answer came from the model's memory of when it was trained,
or from a date somebody in the chat happened to mention. Neither of those is the
date, and a bot that states one confidently is worse than one that admits it
does not know.

So the server states it, the same way it already states the room's name and
everyone's role: as a fact the chat cannot write. It is derived from the pass's
own clock reading (``Ctx.now``) and from nothing else — no message, no model, no
network call.

Both calendars, because both are asked for
------------------------------------------
Gregorian, because a date written in a message is almost always Gregorian; and
Solar Hijri (Jalali), because this is a Persian-language room and «امروز چندمه؟»
means the Persian date. Stating one and leaving the model to convert would be
asking it to do calendar arithmetic with no way to check itself.

Why Tehran and not UTC
----------------------
A date is a question about a timezone, and the room's is Tehran. At 20:30 UTC
the Gregorian day has not turned over in London and already has in Tehran, so a
date that rolls at midnight UTC is wrong for three and a half hours every night
— which is the busiest part of a Persian group's evening. ``Asia/Tehran`` is
read from the system's own tz database rather than written down as a constant:
Iran has observed no daylight saving since 2022, so the offset is +03:30 today,
but a constant cannot be corrected and a database can.

The conversion, and how far it was checked
------------------------------------------
The Solar Hijri year begins at the vernal equinox as observed at the meridian of
Tehran, so it is astronomical, and no arithmetic rule reproduces it for ever.
This module uses the 33-year cycle — Pournader and Toossi's rule, the one the
widely used ``jalali`` implementations are built on. It was checked two ways
before being written down:

* against the published Gregorian boundaries of every month of 1404 and 1405,
  and against 22 Bahman 1357 (11 February 1979);
* against the 2820-year astronomical algorithm — Borkowski's, the one behind
  ``jalaali-js`` — for **every day from 1800 to 2200**. The two agree on all
  146,097 of them.

That agreement is why this file can be twenty lines instead of sixty, and it is
also the accuracy bound, stated plainly: this is a clock for a chat room, not a
calendar authority, and it was verified over the range it is used over.

The other reason to prefer this form is that it uses only non-negative
arithmetic. The 2820-year algorithm has negative intermediate terms and depends
on a division that truncates toward zero, which Python's ``//`` does not do;
reproducing that subtly wrong would be a bug no test here would notice.

No dependency is added: ``zoneinfo`` and ``datetime`` are the standard library.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

# ── The room's clock ──────────────────────────────────────────────────────
_tehran: dt.tzinfo | None = None


def tehran() -> dt.tzinfo:
    """The room's timezone, resolved on first use and then remembered.

    Lazy rather than a module constant, and the reason is the import graph:
    ``awareness_context`` imports this module and ``main`` imports that, so a
    ``ZoneInfoNotFoundError`` raised here at import time would be a bot that
    does not start because it could not work out what day it was. Resolved on
    first use, a missing tz database costs one context block — the failure is
    caught and logged by the source that asked, exactly like every other
    context block that cannot be built — and the bot keeps moderating.
    """
    global _tehran
    if _tehran is None:
        _tehran = ZoneInfo("Asia/Tehran")
    return _tehran


# ── Names ─────────────────────────────────────────────────────────────────
# Farvardin first.
MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)

# Saturday first, because the Persian week starts on Saturday rather than on
# Monday. ``_WEEKDAY_SHIFT`` is what reconciles the two: ``date.weekday()``
# counts from Monday, so Saturday (5) has to land on index 0.
WEEKDAYS = (
    "شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه",
)
_WEEKDAY_SHIFT = 2

_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# Days elapsed before the first of each Gregorian month, in a common year.
_GREGORIAN_MONTH_STARTS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)

# The first six Persian months have 31 days each and the next five have 30; only
# Esfand, the twelfth, varies with the leap rule, and the conversion below
# already knows its length by the time it gets there.
_FIRST_HALF = 186  # 6 * 31


def _fa(number: int) -> str:
    """A number in Persian digits. The room reads them, so it may as well see
    them: a model asked for the date in Persian should not have to transliterate
    a numeral it was handed in Latin."""
    return str(number).translate(_DIGITS)


# ── The conversion ────────────────────────────────────────────────────────
def to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    """A Gregorian date as a Solar Hijri ``(year, month, day)``.

    The 33-year cycle, in the form Pournader and Toossi published: the day count
    of the Gregorian date is folded into a 12053-day cycle — 33 years, of which
    8 are leap — and then into the Persian year that contains it. Every term is
    non-negative for any date this bot can be running on, so the arithmetic is
    plain ``//`` throughout.
    """
    # January and February are the tail of the Persian year that began the
    # previous March, so the Gregorian leap-day correction for them is taken
    # from the following year rather than from this one.
    leap_year = year + 1 if month > 2 else year
    days = (
        355666
        + 365 * year
        + (leap_year + 3) // 4
        - (leap_year + 99) // 100
        + (leap_year + 399) // 400
        + day
        + _GREGORIAN_MONTH_STARTS[month - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < _FIRST_HALF:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - _FIRST_HALF) // 30, 1 + (days - _FIRST_HALF) % 30


# ── Rendering ─────────────────────────────────────────────────────────────
def tehran_moment(now: int) -> dt.datetime:
    """``now`` (epoch seconds) as a wall-clock moment in Tehran.

    The clock reading is the caller's, and it comes from the pass itself. This
    function is the only place a timezone is applied, so "the date is Tehran's"
    is a property of one line rather than of every caller.
    """
    return dt.datetime.fromtimestamp(int(now), tehran())


def weekday_name(moment: dt.datetime) -> str:
    """The Persian name of the day of the week the moment falls on."""
    return WEEKDAYS[(moment.weekday() + _WEEKDAY_SHIFT) % len(WEEKDAYS)]


def gregorian_text(moment: dt.datetime) -> str:
    """The Gregorian date, as ``2026-09-23``."""
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


def jalali_text(moment: dt.datetime) -> str:
    """The Persian date, as a Persian speaker would write it.

    The weekday leads because «امروز چندشنبه است؟» is a question about the same
    day, and answering it from the same line costs three words. Deliberately no
    clock time: an awareness pass runs on a debounce, so a minute rendered here
    can already be wrong by the time the model writes its sentence, and a date
    that is coarse is right where a time that is precise would be a small lie.
    """
    jy, jm, jd = to_jalali(moment.year, moment.month, moment.day)
    return f"{weekday_name(moment)} {_fa(jd)} {MONTHS[jm - 1]} {_fa(jy)}"
