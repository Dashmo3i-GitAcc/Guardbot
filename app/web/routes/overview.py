"""The Overview: the panel's first *read* page.

It answers "what is the bot doing right now" from the shared database, using
only readers that already existed (plus ``db.seen_updates_latest``, the one
additive reader this stage needed). Nothing here writes, and nothing here
decides what a number means — that is ``app/web/queries.py`` for the data and
``app/web/copy.py``/``labels.py`` for the words.

M1 shipped a placeholder landing page here whose whole content was "the panel
is up". That page is gone: it is replaced by the real overview, and the panel's
own state — uptime, the resolved role, the session's expiry — moved into a
section of its own at the bottom, because an operator who cannot tell "the panel
is misconfigured" from "the bot is broken" will debug the wrong process.
"""
import time
from datetime import datetime, timezone

from aiohttp import web

from app.web import auth, authz, context, copy, queries
from app.web.jalali import format_jalali
from app.web.render import render


def register(app: web.Application) -> None:
    app.router.add_get("/", overview)


@authz.requires(authz.PANEL_PERMISSION)
async def overview(request: web.Request) -> web.Response:
    session = request.get(context.SESSION) or {}
    actor = request.get(context.PRINCIPAL) or authz.principal(session)
    started_at = request.app.get(context.STARTED_AT)

    expires = ""
    try:
        expires = format_jalali(datetime.fromtimestamp(int(session["exp"]), timezone.utc))
    except (KeyError, TypeError, ValueError, OSError):
        expires = ""

    # ``overview()`` never raises: every source is guarded and a failure is
    # reported in ``failed_sources`` rather than thrown. So this route has no
    # error path of its own to write, which is the point of putting the guard
    # in the data layer rather than here.
    data = queries.overview()

    return render(
        request,
        "overview.html",
        title=copy.OVERVIEW_TITLE,
        nav_key="overview",
        panel_uptime=(time.monotonic() - started_at) if started_at else 0,
        session_expires=expires,
        role_label=actor.label,
        permission_count=len(actor.permissions),
        secret_is_ephemeral=auth.secret_is_ephemeral(),
        configured=auth.is_configured(),
        **data,
    )
