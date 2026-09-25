"""Jalali dates and Persian digits for the panel.

Persian speakers read Jalali dates, and a control centre full of ``2026-09-24``
would be the one place in this project that does not speak the room's language.

The conversion is not reimplemented here: it is ``app/persian_calendar.to_jalali``,
the same 33-year-cycle arithmetic the awareness layer uses to tell the room what
day it is. One conversion, one accuracy bound, one place to fix it — and the
panel and the bot can never disagree about the date.

Timestamps are stored by SQLite as UTC (``CURRENT_TIMESTAMP``). They are shown in
**Tehran**, because that is the room's clock and the clock the bot itself reads
(``persian_calendar.tehran()``); a dashboard that showed a different hour from
the bot would be worse than one that showed none.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.persian_calendar import MONTHS, tehran, to_jalali

_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_digits(value) -> str:
    """Latin digits in a string → Persian digits. Non-strings are stringified."""
    return str(value).translate(_DIGITS)


def fa_number(value) -> str:
    """``50000`` → ``۵۰,۰۰۰``. Thousands separators survive the translation."""
    try:
        return fa_digits(f"{int(value or 0):,}")
    except (TypeError, ValueError):
        return fa_digits(value)


def _parse(value) -> datetime | None:
    """A stored timestamp as a naive UTC ``datetime``, or ``None``.

    Two shapes reach the panel, and both are real: SQLite's ``CURRENT_TIMESTAMP``
    columns come back as ``YYYY-MM-DD HH:MM:SS`` strings, and the panel's own
    epoch columns — ``gemini_events.at``, ``seen_updates.at``,
    ``dashboard_audit.at`` — come back as integers. A number is taken as an epoch
    in UTC seconds, which is how every one of those columns is written
    (``int(time.time())``). Anything else is not a timestamp and renders as an em
    dash rather than as a wrong date.

    ``bool`` is rejected first because it *is* an ``int``: ``True`` would
    otherwise render as the first second of 1970.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).replace(
                tzinfo=None
            )
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _tehran(moment: datetime) -> datetime:
    """A naive (assumed UTC) or aware moment as Tehran wall-clock time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tehran())


def format_jalali(value, *, with_time: bool = True, with_month_name: bool = False) -> str:
    """``2026-09-24 12:30`` → ``۱۴۰۵/۰۷/۰۲ ۱۶:۰۰`` (or ``۲ مهر ۱۴۰۵``)."""
    moment = _parse(value)
    if moment is None:
        return "—"
    local = _tehran(moment)
    jy, jm, jd = to_jalali(local.year, local.month, local.day)
    if with_month_name:
        stamp = f"{fa_digits(jd)} {MONTHS[jm - 1]} {fa_digits(jy)}"
    else:
        stamp = f"{fa_digits(jy)}/{fa_digits(f'{jm:02d}')}/{fa_digits(f'{jd:02d}')}"
    if with_time:
        stamp += f" {fa_digits(f'{local.hour:02d}:{local.minute:02d}')}"
    return stamp


def format_relative(value) -> str:
    """``۳ دقیقه پیش`` / ``۲ روز پیش`` — for feeds and audit rows."""
    moment = _parse(value)
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())

    if seconds < 60:
        return "همین حالا"
    if seconds < 3600:
        return f"{fa_digits(seconds // 60)} دقیقه پیش"
    if seconds < 86400:
        return f"{fa_digits(seconds // 3600)} ساعت پیش"
    if seconds < 86400 * 30:
        return f"{fa_digits(seconds // 86400)} روز پیش"
    return format_jalali(value, with_time=False)


def format_duration(seconds) -> str:
    """A span as ``۱۲ روز و ۳ ساعت``."""
    try:
        seconds = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{fa_digits(days)} روز و {fa_digits(hours)} ساعت"
    if hours:
        return f"{fa_digits(hours)} ساعت و {fa_digits(minutes)} دقیقه"
    return f"{fa_digits(minutes)} دقیقه"


def now_text() -> str:
    """The current moment in Tehran, Jalali, for the shell's clock."""
    return format_jalali(datetime.now(timezone.utc))


__all__ = [
    "fa_digits",
    "fa_number",
    "format_duration",
    "format_jalali",
    "format_relative",
    "now_text",
]
