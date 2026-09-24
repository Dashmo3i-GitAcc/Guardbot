"""Login, logout and the health probe."""
from aiohttp import web

from app.web import audit, auth, authz, context, copy
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
        # Two different misconfigurations, two different sentences. Without the
        # second check a panel with a password but no operator would accept the
        # login and then refuse every page, which reads as a broken panel rather
        # than as a missing setting.
        operator_configured=auth.operator_configured(),
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
        # One row per blocked window per address, not one per knock: the brake is
        # on the address, so a caller can keep knocking, and an audit row per
        # request would make a brute-force attempt a way to grow the table.
        if auth.throttle.should_audit_block(ip):
            audit.record(
                audit.ACTION_LOGIN_THROTTLED,
                outcome=audit.OUTCOME_REFUSED,
                actor=username or "-",
                detail=f"retry_after={retry_after}",
                client_ip=ip,
            )
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=429,
            next_path=next_path,
            configured=auth.is_configured(),
            operator_configured=auth.operator_configured(),
            error=copy.LOGIN_RATE_LIMITED,
        )

    if not auth.is_configured():
        # Not audited on purpose: the panel is not accepting logins at all, so
        # there is no event to investigate and every request would be a row.
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=503,
            next_path=next_path,
            configured=False,
            operator_configured=auth.operator_configured(),
            error=copy.LOGIN_NOT_CONFIGURED,
        )

    if not auth.operator_configured():
        # Same reasoning, and the same refusal: an unconfigured panel is locked,
        # not open.
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=503,
            next_path=next_path,
            configured=True,
            operator_configured=False,
            error=copy.LOGIN_NO_OPERATOR,
        )

    if not auth.verify_credentials(username, password):
        auth.throttle.record_failure(ip)
        audit.record(
            audit.ACTION_LOGIN_FAILED,
            outcome=audit.OUTCOME_REFUSED,
            actor=username or "-",
            detail="bad credentials",
            client_ip=ip,
        )
        return render(
            request,
            "login.html",
            title=copy.LOGIN_TITLE,
            http_status=401,
            next_path=next_path,
            configured=True,
            operator_configured=True,
            error=copy.LOGIN_FAILED,
        )

    auth.throttle.reset(ip)
    # A brand-new token, never a re-used one: this is the session-fixation
    # defence, and it is why `create_session` always mints rather than refreshing.
    token, session = auth.create_session()
    audit.record(
        audit.ACTION_LOGIN,
        outcome=audit.OUTCOME_OK,
        actor=username,
        actor_id=int(session.get("pid") or 0),
        role=authz.principal(session).role,
        client_ip=ip,
    )

    response = redirect(next_path)
    auth.set_session_cookie(response, token)
    raise response


# Signing out needs no authority — only a session — so it declares the sentinel
# rather than a permission. An operator whose principal holds nothing must still
# be able to leave.
@authz.requires(authz.AUTHENTICATED)
async def logout(request: web.Request) -> web.Response:
    session = request.get(context.SESSION) or {}
    audit.record(
        audit.ACTION_LOGOUT,
        outcome=audit.OUTCOME_OK,
        actor=str(session.get("u") or ""),
        actor_id=int(session.get("pid") or 0),
        role=authz.principal(session).role,
        client_ip=auth.client_ip(request),
    )
    response = redirect("/login", "logged_out")
    auth.clear_session_cookie(response)
    raise response


async def healthz(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe. Says nothing about the data."""
    return web.json_response({"status": "ok"})
