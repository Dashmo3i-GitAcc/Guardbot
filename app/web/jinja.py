"""Jinja environment and the formatting filters the templates use.

The point of this module is that a template never formats a number or a date
itself. It calls ``{{ value|fa_number }}`` or ``{{ value|date }}``, and those
delegate to ``app/web/jalali.py`` — the same Persian-digit and Jalali logic the
bot's own Persian output is built on. That is what keeps a number and a date
identical in Telegram and in the browser.

Copy is injected as a global (``copy.APP_TITLE``) so templates never contain
Persian literals, the same way ``app/chat.py`` holds the bot's wording.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.web import copy, labels
from app.web.jalali import (
    fa_digits,
    fa_number,
    format_duration,
    format_jalali,
    format_relative,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    env.filters.update(
        {
            "fa": fa_digits,
            "fa_number": fa_number,
            "date": format_jalali,
            "ago": format_relative,
            "duration": format_duration,
            # The status vocabulary. A template asks ``{{ kind|event }}`` for a
            # label and a colour rather than deciding either for itself, so a
            # status means the same thing on every page that shows it.
            "event": labels.event,
            "workload": labels.workload,
        }
    )

    env.globals.update({"copy": copy, "brand": copy.BRAND, "static_dir": STATIC_DIR})
    return env


templates = build_environment()

__all__ = ["TEMPLATES_DIR", "STATIC_DIR", "build_environment", "templates"]
