"""Who may use the panel — decided by ``rbac``, from the session, server-side.

This is M2 of the Admin Control Center (AgentMD §54.26). It exists to make three
statements true, each of which is the answer to a way this could be got wrong.

**The panel's authority is ``rbac``'s authority.** A session resolves to an
:class:`rbac.Principal` through ``rbac.resolve``, and a route is allowed only if
``rbac.authorize`` allows it. There is no second permission vocabulary and no
second role table: the panel cannot drift from the bot about who may do what,
because it asks the same module.

**A Telegram administrator is not a dashboard administrator.** The panel
authorizes exactly one identity — ``config.DASHBOARD_OPERATOR_ID`` — and that id
comes from configuration alone. It is never read from the ``admins`` table, from
``CONFIG_ADMINS``, or from anything a client sends. Being promoted in a chat
therefore grants nothing here; the panel is a separate door with a separate key.
The session carries the bound id (``pid``) and ``auth.read_session`` refuses a
session whose id is not the configured one, so even a stolen-and-edited cookie
cannot re-point the panel at another identity.

**A client cannot name its own authority.** Nothing below reads a query
parameter, a header, a form field or a body. The actor is derived from the
session, and the session is derived from a signed cookie. This is the property
that makes the panel's IDOR surface empty: there is no request-supplied subject
for a route to trust, and the first route that takes an object id will take it as
a *subject to be checked*, never as an *identity to act as*.

The permission a page needs is declared on the handler (``@requires``) and
enforced by a middleware, so a new page cannot forget it — and one that does
forget is refused rather than opened, with a test enumerating the routes so the
mistake is caught before it ships.
"""
from __future__ import annotations

import logging

from aiohttp import web
from aiohttp.web_urldispatcher import SystemRoute

from app import rbac
from app.web import audit, auth, context

log = logging.getLogger("guardbot.dashboard.authz")

# The permission the panel's own pages require.
#
# `config.manage` is "see and change runtime configuration (the bot's own
# settings surface)", which is exactly what the control centre is. It is
# deliberately **not** a new permission added to `rbac.PERMISSIONS`: that tuple
# is the wire format of the bot's promotion dialog bitmask, so appending to it
# would add a tick box to the bot's own UI — a change to the bot's behaviour,
# which this stage must not make. The panel expresses its gates with the
# vocabulary that already exists.
PANEL_PERMISSION = "config.manage"

# The attribute ``@requires`` sets on a handler, and the middleware reads back.
PERMISSION_ATTR = "__dashboard_permission__"

# A sentinel permission meaning "any signed-in operator, no rbac permission
# needed". It exists because one route genuinely has no authority requirement:
# signing *out* must always work, including for an operator whose principal
# holds nothing — otherwise a misconfigured operator id would sign you in and
# then refuse to let you sign out again. Declaring this is still a declaration,
# so the fail-closed rule below is not weakened.
AUTHENTICATED = "authenticated"

# Reasons attached to the 403 so the error page can tell "you are not signed in"
# apart from "you are signed in and may not do this".
REASON_FORBIDDEN = "insufficient permission"
REASON_NO_DECLARATION = "route declares no permission"


def principal(session: dict | None) -> rbac.Principal:
    """The rbac principal a panel session acts as.

    The id comes from the session's ``pid``, which ``auth.read_session`` has
    already checked against the configured operator. A missing or unreadable
    ``pid`` is a guest — which holds no permissions, so it is refused by the
    ordinary authorization path rather than by a branch of its own.

    A resolution that *fails* is also a guest, for the reason
    ``rbac.resolve_many`` gives: a missing overlay is a guest, not a crash. The
    panel opens the database but never creates the bot's tables (AgentMD §53.13
    — the panel is additive), so on a host where the bot has not yet run its
    migrations, resolving a non-owner operator reaches for a table that is not
    there. That must be a refusal, not a 500 on the login page: the panel is
    fail-closed, and an operator whose authority cannot be read has none.
    """
    if not session:
        return rbac.guest()
    try:
        pid = int(session.get("pid") or 0)
    except (TypeError, ValueError):
        return rbac.guest()
    try:
        return rbac.resolve(pid)
    except Exception:  # noqa: BLE001 - an unreadable authority is no authority
        log.exception("could not resolve the panel operator %s", pid)
        return rbac.guest(pid)


def requires(permission: str):
    """Declare the permission a route needs. Read back by the middleware.

    A decorator rather than a parameter to ``add_get`` because aiohttp does not
    carry arbitrary metadata through route registration, and an attribute on the
    handler is the one thing the middleware can always see.
    """

    def decorate(handler):
        setattr(handler, PERMISSION_ATTR, permission)
        return handler

    return decorate


def declared_permission(request: web.Request) -> str | None:
    """The permission the matched route declared, or ``None`` if it declared none."""
    return getattr(request.match_info.handler, PERMISSION_ATTR, None)


def authorize(actor: rbac.Principal, permission: str) -> rbac.Decision:
    """Whether ``actor`` may exercise ``permission``. ``rbac``'s answer, unaltered."""
    return rbac.authorize(actor, permission)


def allowed(session: dict | None, permission: str) -> bool:
    """Would this session be allowed this permission? For templates and tests."""
    return bool(authorize(principal(session), permission))


def _refuse(
    request: web.Request,
    session: dict,
    actor: rbac.Principal,
    permission: str,
    reason: str,
) -> None:
    """Record a refusal. The audit row is written for refusals, not only successes."""
    audit.record(
        audit.ACTION_AUTHZ_REFUSED,
        outcome=audit.OUTCOME_REFUSED,
        actor=str(session.get("u") or ""),
        actor_id=int(session.get("pid") or 0),
        permission=permission,
        role=actor.role,
        detail=reason,
        client_ip=auth.client_ip(request),
    )


@web.middleware
async def authorization_middleware(request: web.Request, handler):
    """Decide, server-side, whether this session may use this route.

    Runs *after* the session middleware, so a request without a valid session has
    already been answered and never reaches the decision below.

    Ordering inside: the public check first (there is nothing to decide), then the
    system-route check (a 404 or 405 is not an authorization failure and must keep
    its own answer), then the fail-closed declaration check, then ``rbac``.
    """
    if auth.is_public(request.path):
        return await handler(request)

    session = request.get(context.SESSION)
    if session is None:
        # The session middleware owns the unauthenticated answer; it has either
        # redirected or is about to. Passing through keeps the two layers from
        # disagreeing about which of them refuses.
        return await handler(request)

    actor = principal(session)
    request[context.PRINCIPAL] = actor

    # A 404 or a 405 matched no handler of ours. Authorizing it would turn "that
    # page does not exist" into "you may not see that page", which is a worse
    # answer and a confusing one.
    if isinstance(request.match_info.route, SystemRoute):
        return await handler(request)

    permission = declared_permission(request)
    if permission is None:
        # Fail closed. A route that declares no permission is a bug, and a bug
        # must not be an open door — so it is refused, and the route-inventory
        # test fails before it can be deployed.
        _refuse(request, session, actor, "", REASON_NO_DECLARATION)
        raise web.HTTPForbidden(reason=REASON_FORBIDDEN)

    if permission == AUTHENTICATED:
        return await handler(request)

    decision = authorize(actor, permission)
    if not decision:
        _refuse(request, session, actor, permission, decision.reason)
        raise web.HTTPForbidden(reason=REASON_FORBIDDEN)

    return await handler(request)


__all__ = [
    "AUTHENTICATED",
    "PANEL_PERMISSION",
    "REASON_FORBIDDEN",
    "REASON_NO_DECLARATION",
    "allowed",
    "authorization_middleware",
    "authorize",
    "declared_permission",
    "principal",
    "requires",
]
