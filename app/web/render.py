"""Shared rendering helpers for the panel's route modules.

Every page gets the same context — the nav, the session, the CSRF token, the
current flash, the shell's clock — so no route has to remember to pass them and
no template has to defend against them being missing.

Filter and pagination link builders are deliberately absent: no page in M1
filters or pages, and a helper with no caller is dead code. They arrive with the
first page that needs them.
"""
from aiohttp import web

from app.web import auth, context, copy
from app.web.jalali import now_text
from app.web.jinja import templates

# (key, label, href). Order matters: it is the order they appear in the nav.
# M1 shipped the shell with a single entry; each later stage appends its own line
# here rather than growing a second navigation.
NAV = (
    ("overview", copy.NAV_OVERVIEW, "/"),
)

# Emoji, not an icon font: they are the same glyphs the bot uses in its
# keyboards, so the two surfaces name a thing the same way. One per item, and
# only where it helps you find the row.
NAV_ICONS = {
    "overview": "📊",
}


def _flash(request: web.Request) -> dict:
    key = request.query.get("flash")
    text = copy.flash_text(key)
    if not text:
        return {"text": None, "is_error": False}
    return {"text": text, "is_error": copy.flash_is_error(key)}


def base_context(request: web.Request, **extra) -> dict:
    session = request.get(context.SESSION) or {}
    ctx = {
        "nav": NAV,
        "icons": NAV_ICONS,
        "current": request.path,
        "nav_key": "",
        "csrf": session.get("csrf", ""),
        "operator": auth.session_label(session),
        "flash": _flash(request),
        "server_time": now_text(),
    }
    ctx.update(extra)
    return ctx


def render(
    request: web.Request, template: str, *, http_status: int = 200, **extra
) -> web.Response:
    """Render a template with the shared context merged in.

    The HTTP status is ``http_status`` rather than ``status`` because several
    pages legitimately carry a ``status`` value of their own and shadowing it
    would be a silent bug.
    """
    ctx = base_context(request, **extra)
    body = templates.get_template(template).render(**ctx)
    return web.Response(
        text=body, content_type="text/html", charset="utf-8", status=http_status
    )


def redirect(path: str, flash: str | None = None) -> web.HTTPException:
    """A 303 to ``path``, optionally carrying a flash key.

    303 rather than 302 so a POST never leaves a re-submittable page behind in
    the browser. Callers use it as ``raise redirect("/", "saved")``.
    """
    target = f"{path}?flash={flash}" if flash else path
    return web.HTTPSeeOther(target)


def safe_next(raw: str | None, fallback: str = "/") -> str:
    """Only ever redirect to a path on this site.

    An open redirect is a real phishing primitive, and ``next`` is
    attacker-controlled, so anything that is not a plain local path is
    discarded.
    """
    if not raw:
        return fallback
    value = raw.strip()
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    if "\\" in value or "\n" in value or "\r" in value:
        return fallback
    return value


__all__ = [
    "NAV",
    "base_context",
    "redirect",
    "render",
    "safe_next",
]
