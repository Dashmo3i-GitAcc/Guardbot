"""Login, logout and the health probe."""
from aiohttp import web

from app.web import auth, context, copy
from app.web.render import redirect, render, safe_next


def register(app: web.Application) -> None:
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_post("/logout", logout)
    app.router.add_get("/healthz", healthz)


async def login_page(request: web.Request) -> web.Response:
    if request.get(context.SESSION):
        raise redirect(safe_next(request.query.get("next")))
    return render(
        request,
        "login.html",
        title=copy.LOGIN_TITLE,
        next_path=safe_next(request.query.get("next")),
        configured=auth.is_configured(),
        # A flash is a key in the query string, never text, so this is the same
        # mechanism every other page uses. It matters here more than elsewhere:
        # this is where a signed-out operator lands after a logout or an expired
        # session, and both need to say what just happened.
        flash_text=copy.flash_text(request.query.get("flash")),
        flash_is_error=copy.flash_is_error(request.query.get("flash")),
    )


async def login_submit(request: web.Request) -> web.Response:
    form = await request.post()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    next_path = safe_next(str(form.get("next") or "/"))
    ip = auth.client_ip(request)

    retry_after = auth.throttle.retry_after(ip)
    if retry_after:
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=429,
            next_path=next_path,
            configured=auth.is_configured(),
            error=copy.LOGIN_RATE_LIMITED,
        )

    if not auth.is_configured():
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=503,
            next_path=next_path,
            configured=False,
            error=copy.LOGIN_NOT_CONFIGURED,
        )

    if not auth.verify_credentials(username, password):
        auth.throttle.record_failure(ip)
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=401,
            next_path=next_path,
            configured=True,
            error=copy.LOGIN_FAILED,
        )

    auth.throttle.reset(ip)
    # A brand-new token, never a re-used one: this is the session-fixation
    # defence, and it is why `create_session` always mints rather than refreshing.
    token, _ = auth.create_session()

    response = redirect(next_path)
    auth.set_session_cookie(response, token)
    raise response


async def logout(request: web.Request) -> web.Response:
    response = redirect("/login", "logged_out")
    auth.clear_session_cookie(response)
    raise response


async def healthz(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe. Says nothing about the data."""
    return web.json_response({"status": "ok"})
