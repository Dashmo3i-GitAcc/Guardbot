"""The landing page.

In M1 this is deliberately small: it proves the shell, the session and the
static assets work, and it states plainly that nothing is managed from the panel
yet. Every later stage adds a real section next to it.
"""
import time
from datetime import datetime, timezone

from aiohttp import web

from app.web import auth, context, copy
from app.web.jalali import format_jalali
from app.web.render import render


def register(app: web.Application) -> None:
    app.router.add_get("/", home)


async def home(request: web.Request) -> web.Response:
    session = request.get(context.SESSION) or {}
    started_at = request.app.get(context.STARTED_AT)

    expires = ""
    try:
        expires = format_jalali(datetime.fromtimestamp(int(session["exp"]), timezone.utc))
    except (KeyError, TypeError, ValueError, OSError):
        expires = ""

    return render(
        request,
        "home.html",
        title=copy.HOME_TITLE,
        nav_key="home",
        uptime_seconds=(time.monotonic() - started_at) if started_at else 0,
        session_expires=expires,
        secret_is_ephemeral=auth.secret_is_ephemeral(),
        configured=auth.is_configured(),
    )
