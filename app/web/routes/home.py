"""The landing page.

In M1 this is deliberately small: it proves the shell, the session and the
static assets work, and it states plainly that nothing is managed from the panel
yet. M2 adds the authorization gate — the page declares the permission it needs
and the middleware enforces it — and shows the role the panel resolved, so the
operator can see *which* authority the page is being served under.
"""
import time
from datetime import datetime, timezone

from aiohttp import web

from app.web import auth, authz, context, copy
from app.web.jalali import format_jalali
from app.web.render import render


def register(app: web.Application) -> None:
    app.router.add_get("/", home)


@authz.requires(authz.PANEL_PERMISSION)
async def home(request: web.Request) -> web.Response:
    session = request.get(context.SESSION) or {}
    actor = request.get(context.PRINCIPAL) or authz.principal(session)
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
        role_label=actor.label,
        permission_count=len(actor.permissions),
        secret_is_ephemeral=auth.secret_is_ephemeral(),
        configured=auth.is_configured(),
    )
