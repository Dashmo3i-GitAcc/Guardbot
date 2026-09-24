"""The panel's aiohttp application and its standalone entry point.

Run it as::

    python -m app.web

or through the ``dashboard`` compose service. It is a **separate process** from
the bot on purpose: a problem in the web layer must not be able to disturb
Telegram polling, and the two can be deployed independently. They share
``app/config.py`` and the database, which is what keeps them in agreement.

**The panel never migrates the schema.** The bot owns the tables and applies the
migrations at boot; a second process running them concurrently would race it. The
dashboard reads what the bot has created.

Errors are handled here rather than in each route: an unauthenticated request
goes to the login page, a stale CSRF token gets its own explanation, and an
unexpected exception gets a page that says what to do instead of a traceback.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path

from aiohttp import web

from app import config
from app.web import auth, context, copy
from app.web.jinja import templates
from app.web.render import base_context
from app.web.routes import register

_STATIC_DIR = Path(__file__).resolve().parent / "static"

_ERROR_FLASH = {
    403: "forbidden",
    404: "not_found",
}

# The headline of the error page. A 404 is not "something went wrong" — it is
# "that page does not exist", and saying so saves the operator a support round
# trip.
_ERROR_TITLES = {
    403: copy.FORBIDDEN_TITLE,
    404: copy.NOT_FOUND_TITLE,
}


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        return await _error_response(request, exc)
    except Exception:
        print("[dashboard] unhandled error:", flush=True)
        traceback.print_exc()
        return await _error_response(request, web.HTTPInternalServerError())


async def _error_response(request: web.Request, exc: web.HTTPException):
    audience = request.get(context.AUDIENCE) or auth.audience_for_path(request.path)

    # A POST whose session expired should land on the way in, not on a
    # "forbidden" wall it cannot get past.
    if exc.status == 403 and not request.get(context.SESSION):
        return web.Response(
            status=303,
            headers={
                "Location": f"{auth.login_path_for(audience)}?flash=session_expired"
            },
        )

    key = (
        "csrf"
        if exc.reason == auth.REASON_STALE_FORM
        else _ERROR_FLASH.get(exc.status, "error")
    )
    # HTML unless the caller explicitly asked for JSON: this is a browser-only
    # panel, so a page is the right default and a JSON body the exception.
    if "application/json" in request.headers.get("Accept", ""):
        return web.json_response({"error": key, "status": exc.status}, status=exc.status)

    body = templates.get_template("error.html").render(
        **base_context(
            request,
            title=_ERROR_TITLES.get(exc.status, copy.ERROR_TITLE),
            status=exc.status,
            message=copy.flash_text(key) or copy.ERROR_BODY,
            nav_key="",
        )
    )
    return web.Response(
        text=body, content_type="text/html", charset="utf-8", status=exc.status
    )


async def favicon(request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "/static/logo.svg"})


def create_app(*, started_at: float | None = None) -> web.Application:
    """Build the application.

    ``started_at`` is injectable so a test can assert on uptime without sleeping.
    """
    app = web.Application(
        middlewares=[
            error_middleware,
            auth.session_middleware,
            auth.csrf_middleware,
        ],
        client_max_size=64 * 1024,
    )
    app[context.STARTED_AT] = started_at if started_at is not None else time.monotonic()

    register(app)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_static("/static/", _STATIC_DIR, name="static")
    return app


async def main() -> None:
    app = create_app()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    await site.start()

    print(
        f"[dashboard] listening on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}",
        flush=True,
    )
    if not auth.is_configured():
        print(
            "[dashboard] no DASHBOARD_PASSWORD / DASHBOARD_PASSWORD_HASH set — "
            "every login will be refused until one is.",
            flush=True,
        )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
