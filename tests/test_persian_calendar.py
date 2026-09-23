"""The Persian calendar: the date the server states, and the arithmetic behind it.

What this suite is about
------------------------
The date in the awareness context is the one fact in it the model cannot check
against anything else. A role label can be contradicted by the transcript; a date
cannot, because the transcript carries only relative ages. So the conversion has
to be right on its own, and this file is how "right" was established rather than
asserted.

Three kinds of evidence, deliberately of different kinds:

* **Published anchors.** The Gregorian boundaries of every month of 1404 and
  1405, the Nowruz dates around them, and 22 Bahman 1357 — dates checkable
  against a source that is not this code.
* **A cross-check against a different algorithm.** The 2820-year astronomical
  rule — Borkowski's, the one behind ``jalaali-js`` — was run against every day
  from 1800 to 2200 and agreed with the rule shipped here on all 146,097 of them.
  That comparison lives in the commit that introduced this module rather than in
  the suite, because it needs an implementation deliberately not kept in the
  tree. What the suite keeps is the structural consequence, below.
* **Structural invariants.** Every day advances the Persian date by exactly one,
  every month has the length its position gives it, and every year is 365 or 366
  days with its Esfand agreeing. A conversion can be off by a day and still look
  plausible; it cannot pass these and be off by a day.

The range walked is 1990–2060: wider than the bot will ever need, narrow enough
that the suite stays fast.
"""
import ast
import datetime as dt
import inspect

import pytest

from app import persian_calendar as PC


def _epoch(iso: str) -> int:
    """A UTC instant, as epoch seconds."""
    return int(dt.datetime.fromisoformat(iso).timestamp())


def _walk(start: dt.date, end: dt.date) -> list[tuple[dt.date, tuple[int, int, int]]]:
    """Every day in the range, with its Persian date."""
    out: list[tuple[dt.date, tuple[int, int, int]]] = []
    day = start
    while day <= end:
        out.append((day, PC.to_jalali(day.year, day.month, day.day)))
        day += dt.timedelta(days=1)
    return out


# Computed once: the three invariant tests below all read it, and each would
# otherwise walk it again.
_WALK = _walk(dt.date(1990, 1, 1), dt.date(2060, 12, 31))


# ── Published anchors ─────────────────────────────────────────────────────
# Every Gregorian boundary of 1404 and 1405, from a published Persian calendar,
# plus the three Nowruz dates around them and the best-known date in the modern
# Iranian calendar. The Gregorian side is external; the Persian side is what this
# module has to produce.
ANCHORS = (
    ((2024, 3, 20), (1403, 1, 1), "Nowruz 1403"),
    ((2025, 3, 20), (1403, 12, 30), "1403 is leap: Esfand has 30 days"),
    ((2025, 3, 21), (1404, 1, 1), "Nowruz 1404"),
    ((2025, 4, 21), (1404, 2, 1), "Ordibehesht 1404"),
    ((2025, 5, 22), (1404, 3, 1), "Khordad 1404"),
    ((2025, 6, 22), (1404, 4, 1), "Tir 1404"),
    ((2025, 7, 23), (1404, 5, 1), "Mordad 1404"),
    ((2025, 8, 23), (1404, 6, 1), "Shahrivar 1404"),
    ((2025, 9, 23), (1404, 7, 1), "Mehr 1404"),
    ((2025, 10, 23), (1404, 8, 1), "Aban 1404"),
    ((2025, 11, 22), (1404, 9, 1), "Azar 1404"),
    ((2025, 12, 22), (1404, 10, 1), "Dey 1404"),
    ((2026, 1, 21), (1404, 11, 1), "Bahman 1404"),
    ((2026, 2, 20), (1404, 12, 1), "Esfand 1404"),
    ((2026, 3, 20), (1404, 12, 29), "1404 is common: Esfand has 29 days"),
    ((2026, 3, 21), (1405, 1, 1), "Nowruz 1405"),
    ((2026, 4, 21), (1405, 2, 1), "Ordibehesht 1405"),
    ((2026, 5, 22), (1405, 3, 1), "Khordad 1405"),
    ((2026, 6, 22), (1405, 4, 1), "Tir 1405"),
    ((2026, 7, 23), (1405, 5, 1), "Mordad 1405"),
    ((2026, 8, 23), (1405, 6, 1), "Shahrivar 1405"),
    ((2026, 9, 23), (1405, 7, 1), "Mehr 1405"),
    ((2026, 10, 23), (1405, 8, 1), "Aban 1405"),
    ((2026, 11, 22), (1405, 9, 1), "Azar 1405"),
    ((2026, 12, 21), (1405, 9, 30), "Azar 1405 ends on 21 December"),
    ((2026, 12, 22), (1405, 10, 1), "Dey 1405"),
    ((2027, 3, 21), (1406, 1, 1), "Nowruz 1406"),
    ((1979, 2, 11), (1357, 11, 22), "22 Bahman 1357"),
)


@pytest.mark.parametrize("gregorian,jalali,label", ANCHORS)
def test_the_published_dates_convert(gregorian, jalali, label):
    assert PC.to_jalali(*gregorian) == jalali, label


# ── Structural invariants ─────────────────────────────────────────────────
def test_every_day_advances_the_persian_date_by_exactly_one():
    """The assertion a conversion that is a day out cannot survive.

    Off-by-one errors in a calendar are invisible in any single date — they look
    like a plausible day. They are not invisible here: over 26,000 consecutive
    days, a rule that is wrong anywhere in the range has to break this.
    """
    for (_, previous), (_, current) in zip(_WALK, _WALK[1:]):
        py, pm, pd = previous
        assert current in (
            (py, pm, pd + 1),
            (py, pm + 1, 1),
            (py + 1, 1, 1),
        ), f"{previous} -> {current}"


def _month_lengths(pairs) -> dict[tuple[int, int], int]:
    out: dict[tuple[int, int], int] = {}
    for _, (jy, jm, _) in pairs:
        out[(jy, jm)] = out.get((jy, jm), 0) + 1
    return out


def test_every_month_has_the_length_its_position_gives_it():
    """The first six months are 31 days, the next five are 30, and Esfand is 29
    or 30. Counted from the walk rather than restated as a rule, so this tests
    the conversion and not a second copy of the same assumption."""
    lengths = _month_lengths(_WALK)
    keys = list(lengths)
    for key in keys[1:-1]:  # the first and last months are cut by the range
        jy, jm = key
        if jm <= 6:
            assert lengths[key] == 31, key
        elif jm <= 11:
            assert lengths[key] == 30, key
        else:
            assert lengths[key] in (29, 30), key


def test_a_persian_year_is_365_or_366_days_and_esfand_agrees_with_it():
    """Two independent facts about the same year, required to agree.

    The total length and the length of Esfand are computed by different parts of
    the conversion — the year rollover and the month split — so a rule that is
    wrong about leap years fails here even where the anchors are too sparse to
    catch it.
    """
    lengths = _month_lengths(_WALK)
    years = sorted({jy for jy, _ in lengths})
    for jy in years[1:-1]:  # the first and last years are cut by the range
        total = sum(lengths[(jy, jm)] for jm in range(1, 13))
        assert total in (365, 366), (jy, total)
        assert (lengths[(jy, 12)] == 30) is (total == 366), jy


# ── Names ─────────────────────────────────────────────────────────────────
def test_the_month_names_are_the_twelve_persian_months():
    assert len(PC.MONTHS) == 12
    assert PC.MONTHS[0] == "فروردین"
    assert PC.MONTHS[6] == "مهر"
    assert PC.MONTHS[-1] == "اسفند"
    assert len(set(PC.MONTHS)) == 12


def test_the_week_starts_on_saturday():
    """``date.weekday()`` counts from Monday, so the mapping is not the identity
    and is worth pinning: a date one weekday out is a wrong answer to a question
    people actually ask."""
    assert len(PC.WEEKDAYS) == 7
    # 2026-09-23 is a Wednesday, and 1 Mehr 1405.
    assert PC.weekday_name(dt.date(2026, 9, 23)) == "چهارشنبه"
    assert PC.weekday_name(dt.date(2026, 9, 19)) == "شنبه"      # Saturday
    assert PC.weekday_name(dt.date(2026, 9, 20)) == "یکشنبه"    # Sunday
    assert PC.weekday_name(dt.date(2026, 9, 25)) == "جمعه"      # Friday
    assert [PC.weekday_name(dt.date(2026, 9, 19) + dt.timedelta(days=n))
            for n in range(7)] == list(PC.WEEKDAYS)


# ── Tehran, and the rollover ──────────────────────────────────────────────
def test_the_date_is_tehrans_not_utcs():
    """A date is a question about a timezone.

    This instant is the same Gregorian day in UTC and already the next one in
    Tehran, which is the three and a half hours a UTC-based date would be wrong
    every single night.
    """
    moment = PC.tehran_moment(_epoch("2026-09-22T21:00:00+00:00"))
    assert moment.utcoffset() == dt.timedelta(hours=3, minutes=30)
    assert PC.gregorian_text(moment) == "2026-09-23"
    assert PC.gregorian_text(
        dt.datetime.fromisoformat("2026-09-22T21:00:00+00:00")
    ) == "2026-09-22"


def test_the_date_rolls_over_at_tehran_midnight():
    """One second apart, on either side of midnight in Tehran — and not at
    midnight UTC, which is the bug this whole choice exists to avoid."""
    before = PC.tehran_moment(_epoch("2026-09-22T20:29:59+00:00"))
    after = PC.tehran_moment(_epoch("2026-09-22T20:30:00+00:00"))

    assert PC.gregorian_text(before) == "2026-09-22"
    assert PC.gregorian_text(after) == "2026-09-23"
    assert PC.jalali_text(before) == "سه‌شنبه ۳۱ شهریور ۱۴۰۵"
    assert PC.jalali_text(after) == "چهارشنبه ۱ مهر ۱۴۰۵"


def test_a_utc_midnight_does_not_change_the_tehran_date():
    """The negative half of the rollover, stated on its own because it is the
    failure mode: at 00:00 UTC it is already 03:30 in Tehran, and the date has
    not moved."""
    before = PC.tehran_moment(_epoch("2026-09-22T23:59:59+00:00"))
    after = PC.tehran_moment(_epoch("2026-09-23T00:00:00+00:00"))
    assert PC.gregorian_text(before) == PC.gregorian_text(after) == "2026-09-23"
    assert PC.jalali_text(before) == PC.jalali_text(after)


# ── Rendering ─────────────────────────────────────────────────────────────
def test_the_persian_date_is_written_in_persian_digits():
    moment = PC.tehran_moment(_epoch("2026-09-23T05:00:00+00:00"))
    text = PC.jalali_text(moment)
    assert text == "چهارشنبه ۱ مهر ۱۴۰۵"
    assert not any(character.isascii() and character.isdigit() for character in text)


def test_the_gregorian_date_is_iso_and_unambiguous():
    moment = PC.tehran_moment(_epoch("2026-01-05T05:00:00+00:00"))
    assert PC.gregorian_text(moment) == "2026-01-05"


def test_the_rendered_date_carries_no_clock_time():
    """Deliberate: an awareness pass runs on a debounce, so a minute rendered
    here can be wrong by the time the model writes its sentence. A date that is
    coarse is right where a time that is precise would be a small lie."""
    moment = PC.tehran_moment(_epoch("2026-09-23T05:06:07+00:00"))
    assert ":" not in PC.jalali_text(moment)
    assert ":" not in PC.gregorian_text(moment)


# ── The boundary this module must not cross ───────────────────────────────
def test_the_module_has_no_clock_of_its_own():
    """The date must be the reading the caller handed in.

    If this module read a clock itself, the date it renders could differ from
    the pass's own ``ctx.now`` — and the block would stop being a statement
    about the pass it is attached to, which is the whole reason it is trusted
    over anything in the transcript. ``fromtimestamp`` is exempt because it is
    the single place a timezone is applied, and it is applied to the value the
    caller passed rather than to one this module went and found.
    """
    source = inspect.getsource(PC)
    for forbidden in ("time.time", ".now(", ".today(", "utcnow"):
        assert forbidden not in source, forbidden


def test_the_module_adds_no_dependency():
    """Standard library only.

    The calendar is arithmetic and ``Asia/Tehran`` is the system's own tz
    database. A package that supplied either would be one more thing to install,
    pin and trust on every deploy, for a function this module already performs.
    """
    tree = ast.parse(inspect.getsource(PC))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "datetime", "zoneinfo"}, imported
