"""The Admin Control Center's front door.

Three jobs, in this order:

1. **An identity.** One password, checked against the panel's own store first
   (``app/web/credentials.py``, written by ``ops/dashboard_passwd.py``), then
   ``DASHBOARD_PASSWORD_HASH``, then ``DASHBOARD_PASSWORD``. If none of the three
   is set the panel refuses *every* login — an unconfigured panel is locked, not
   open.

   This identity is deliberately **separate from Telegram membership**: being an
   administrator of a Telegram group does not make anybody a dashboard
   administrator. The panel never trusts a role, a group scope or an owner claim
   supplied by the client; authorization is decided server-side from the
   authenticated identity (M2 onward, through ``app/rbac.py``).

2. **A signed session cookie.** Stateless: the cookie is
   ``base64(payload).base64(hmac)`` and the payload carries the operator
   identity, the audience it was minted for, an issue and expiry time, a CSRF
   token and the password epoch. Nothing is stored server-side, so a restart does
   not log anyone out as long as ``DASHBOARD_SECRET`` is set — and a password
   change logs everyone out anyway, because the epoch no longer matches.

3. **A gate.** Every route except the login page, ``/healthz`` and the static
   files needs a valid session, and every state-changing request needs the
   session's CSRF token. Both are middlewares, so a new route cannot forget them.

Password hashing uses ``hashlib.scrypt`` from the standard library. There is no
``passlib``/``bcrypt`` in this project and adding a compiled dependency to a
one-operator control panel is not worth it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import deque

from aiohttp import web

from app import config
from app.web import context, credentials

# The audience a session was minted for. The payload carries it and every read
# checks it, so a token can never be replayed across surfaces if a second one is
# ever added (for example a read-only status page). The value is a constant, not
# a client input: a caller cannot ask to be treated as another audience.
AUDIENCE_ADMIN = "admin"

COOKIE_NAME = "gb_admin"

# Reasons attached to HTTP errors so the error page can say something useful
# instead of "403 Forbidden".
REASON_AUTH_REQUIRED = "authentication required"
REASON_STALE_FORM = "stale form"

# Paths that never require a session. ``/healthz`` is a liveness probe for the
# compose health check and the reverse proxy, and it says nothing about the data;
# the login page is where a session is *created*, so it cannot demand one first.
PUBLIC_PATHS = frozenset({"/login", "/healthz", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)

# scrypt cost. n=2**14 with r=8 needs ~16 MB and a few milliseconds, which is a
# reasonable trade for an interactive login.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

# The bootstrap credentials, read from the environment once at import.
DASHBOARD_USERNAME = config.DASHBOARD_USERNAME
DASHBOARD_PASSWORD = config.DASHBOARD_PASSWORD
DASHBOARD_PASSWORD_HASH = config.DASHBOARD_PASSWORD_HASH
DASHBOARD_SECURE_COOKIES = config.DASHBOARD_SECURE_COOKIES

_SECRET_IS_EPHEMERAL = False


def _load_secret() -> bytes:
    """The cookie-signing key.

    A configured ``DASHBOARD_SECRET`` is used as-is. Without one we generate a
    random key per process, which is safe but logs everyone out on restart —
    reported as a fixable warning rather than hidden.
    """
    global _SECRET_IS_EPHEMERAL
    if config.DASHBOARD_SECRET:
        return config.DASHBOARD_SECRET.encode("utf-8")
    _SECRET_IS_EPHEMERAL = True
    print(
        "[dashboard] DASHBOARD_SECRET is not set; generating a random session "
        "key. Everyone will be logged out on restart.",
        flush=True,
    )
    return secrets.token_bytes(32)


_SECRET = _load_secret()


def secret_is_ephemeral() -> bool:
    return _SECRET_IS_EPHEMERAL


# ── Passwords ─────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash`` — the format ``ops/dashboard_passwd.py`` writes."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            salt.hex(),
            digest.hex(),
        ]
    )


def _verify_hash(password: str, stored: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def is_configured() -> bool:
    """Whether a password login could succeed at all.

    Three sources, in precedence order: a password set from the panel
    (``app/web/credentials.py``), the ``.env`` hash, the ``.env`` plaintext. The
    panel's own store wins because it is the more recent decision.
    """
    return bool(
        credentials.current_hash() or DASHBOARD_PASSWORD_HASH or DASHBOARD_PASSWORD
    )


def operator_id() -> int:
    """The Telegram identity the panel is bound to. Read from config every time.

    This is the *whole* of the panel's authority model: the dashboard authorizes
    this id and nobody else, and it comes from configuration — never from the
    ``admins`` table, never from ``CONFIG_ADMINS``, never from a request. A
    Telegram group administrator is therefore not a dashboard administrator.
    See AgentMD §53.13.
    """
    return int(config.DASHBOARD_OPERATOR_ID or 0)


def operator_configured() -> bool:
    """Whether the panel has an operator to authorize at all.

    Without one every page would be refused by ``rbac`` as ``no_owner``, which is
    the right answer but a confusing one to arrive at *after* a successful login.
    The login page checks this first so the operator is told what is missing.
    """
    return operator_id() != 0


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time check of both fields.

    The password is always hashed, even when the username is wrong or no
    password is configured, so the response time does not reveal whether a guess
    was closer.
    """
    username_ok = hmac.compare_digest(
        (username or "").strip().encode("utf-8"),
        DASHBOARD_USERNAME.encode("utf-8"),
    )

    # A password changed from the panel replaces the `.env` one rather than
    # being checked alongside it: two live credentials would mean changing the
    # password does not revoke the old one, which is the whole point of the
    # change.
    stored_hash = credentials.current_hash() or DASHBOARD_PASSWORD_HASH
    if stored_hash:
        password_ok = _verify_hash(password or "", stored_hash)
    elif DASHBOARD_PASSWORD:
        password_ok = hmac.compare_digest(
            (password or "").encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8")
        )
    else:
        _verify_hash(password or "", hash_password("no-password-configured"))
        password_ok = False

    return bool(username_ok and password_ok)


# ── Password policy ───────────────────────────────────────────────────────
# One rule, in one place: `ops/dashboard_passwd.py` enforces it and so does the
# panel's own change form. Two different minimums would mean the CLI could set a
# password the panel would refuse to set again.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

# Values that are long enough to pass the length rule and still useless. Kept
# short and obvious on purpose: this is a guard against the laziest choice, not
# a password-strength oracle. A real blocklist belongs in a library, and adding
# one would not make a 12-character minimum any stronger.
_WEAK_PASSWORDS = frozenset(
    {
        "password",
        "password1234",
        "administrator",
        "qwertyuiop12",
        "123456789012",
        "000000000000",
        "aaaaaaaaaaaa",
        "letmein12345",
        "changeme1234",
        "guardbot1234",
    }
)


def password_problems(
    new_password: str, confirm: str, *, current_password: str | None = None
) -> list[str]:
    """Every reason this password is refused, as machine-readable keys.

    Returns all of them rather than the first, so the form can tell the owner
    everything that is wrong at once instead of one item per attempt. Nothing
    user-facing is built here, and the password itself is never echoed back,
    logged, or put in a message.
    """
    problems: list[str] = []
    candidate = new_password or ""

    if not candidate:
        problems.append("empty")
    if candidate and len(candidate) < MIN_PASSWORD_LENGTH:
        problems.append("too_short")
    if len(candidate) > MAX_PASSWORD_LENGTH:
        # Not a policy anyone should hit by hand; it is a bound on what reaches
        # scrypt, so a megabyte of "password" cannot be used to burn CPU.
        problems.append("too_long")
    if candidate and candidate != candidate.strip():
        # Leading or trailing whitespace is almost always a paste accident, and
        # it is invisible in a password field — so it is refused rather than
        # silently trimmed into a password the owner cannot type again.
        problems.append("whitespace")
    if candidate and candidate.lower() in _WEAK_PASSWORDS:
        problems.append("too_common")
    if candidate and len(set(candidate)) < 4:
        problems.append("too_repetitive")
    if candidate != (confirm or ""):
        problems.append("mismatch")
    if current_password is not None and candidate and hmac.compare_digest(
        candidate.encode("utf-8"), (current_password or "").encode("utf-8")
    ):
        problems.append("unchanged")

    return problems


def change_password(
    current_password: str, new_password: str, confirm: str
) -> tuple[bool, list[str], int]:
    """Verify the old password and store the new one. Returns ``(ok, problems,
    epoch)``.

    The current password is required even though the caller already holds a
    session. A session is a cookie, and a cookie can be a stolen laptop; making
    the change require the password again means an attacker with a session but
    not the password cannot lock the owner out of their own panel. That is the
    one failure mode of this feature worth designing against, so it is not
    negotiable.
    """
    if not verify_credentials(DASHBOARD_USERNAME, current_password):
        return False, ["current_wrong"], credentials.current_epoch()

    problems = password_problems(
        new_password, confirm, current_password=current_password
    )
    if problems:
        return False, problems, credentials.current_epoch()

    epoch = credentials.set_password_hash(hash_password(new_password))
    return True, [], epoch


# ── Sessions ──────────────────────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(_SECRET, payload, hashlib.sha256).digest())


def session_seconds() -> int:
    return max(1, int(config.DASHBOARD_SESSION_SECONDS))


def rotate_after_seconds() -> int:
    """A session is re-minted once it is this old, on a safe request.

    Half the session's life: long enough that rotation is rare, short enough
    that a cookie captured from a browser is unlikely to outlive the session it
    was captured from.
    """
    return max(1, session_seconds() // 2)


def create_session(
    *,
    identity: str | None = None,
    audience: str = AUDIENCE_ADMIN,
    display_name: str | None = None,
) -> tuple[str, dict]:
    """Mint a session token. Returns ``(token, session_dict)``.

    The token is always fresh: login never reuses a token that was already in
    the caller's browser, which is the session-fixation defence.
    """
    now = int(time.time())
    session = {
        "aud": audience,
        "u": identity or DASHBOARD_USERNAME,
        # The Telegram identity this panel session acts as. Stamped at mint time
        # and re-checked on every read, so re-pointing `DASHBOARD_OPERATOR_ID` at
        # a different id retires every session that existed before it — the same
        # shape as the password epoch below, for the same reason.
        "pid": operator_id(),
        "iat": now,
        "exp": now + session_seconds(),
        "csrf": secrets.token_urlsafe(24),
        # The password epoch this session was minted under. ``read_session``
        # refuses a session whose epoch is stale, so changing the password
        # retires every session — including this one — without the panel having
        # to keep a list of live sessions.
        "pe": credentials.current_epoch(),
    }
    if display_name:
        session["n"] = display_name

    payload = _b64(
        json.dumps(session, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload}.{_sign(payload.encode('ascii'))}", session


def _identity_is_valid(session: dict, audience: str) -> bool:
    """Is the identity in this payload still entitled to be here?

    Two things must both hold, and they are different questions:

    * the **username** must still be the configured operator, so renaming the
      operator (and restarting) revokes the sessions that named the old one;
    * the **bound Telegram id** must still be the configured one, so pointing the
      panel at a different identity revokes the sessions minted under the old
      one. Without this, an operator id change would leave live sessions acting
      as the previous identity — which is the one way a cookie edit could
      re-point the panel's authority.
    """
    if audience != AUDIENCE_ADMIN:
        return False
    if (session.get("u") or "") != DASHBOARD_USERNAME:
        return False
    try:
        return int(session.get("pid") or 0) == operator_id()
    except (TypeError, ValueError):
        return False


def read_session(token: str | None, *, audience: str = AUDIENCE_ADMIN) -> dict | None:
    """Verify a cookie and return its session, or ``None`` if it is not valid."""
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload.encode("ascii")), signature):
        return None
    try:
        session = json.loads(_unb64(payload).decode("utf-8"))
    except (ValueError, TypeError):
        return None
    if not isinstance(session, dict):
        return None
    if int(session.get("exp") or 0) < int(time.time()):
        return None
    # A session minted for another surface is not a session here, even though it
    # carries a valid signature.
    if session.get("aud") != audience:
        return None
    if not _identity_is_valid(session, audience):
        return None
    # A password change retires every session, so a cookie captured before the
    # change stops working the moment the password moves.
    if int(session.get("pe") or 0) != credentials.current_epoch():
        return None
    return session


def needs_rotation(session: dict) -> bool:
    """Whether this session is old enough to be re-minted on a safe request.

    A session with no readable issue time is *not* rotated: we always stamp one
    when minting, so a payload without one is not ours, and treating it as
    ancient would re-mint a cookie on every single request.
    """
    raw = session.get("iat")
    if raw is None:
        return False
    try:
        issued = int(raw)
    except (TypeError, ValueError):
        return False
    return (int(time.time()) - issued) >= rotate_after_seconds()


def rotate_session(session: dict) -> tuple[str, dict]:
    """Re-mint a live session, keeping its identity and audience."""
    return create_session(
        identity=session.get("u"),
        audience=session.get("aud") or AUDIENCE_ADMIN,
        display_name=session.get("n"),
    )


def session_label(session: dict | None) -> str:
    """What to call the operator on screen."""
    if not session:
        return ""
    return session.get("n") or session.get("u") or ""


def cookie_name(audience: str = AUDIENCE_ADMIN) -> str:
    """Which cookie carries this audience's session."""
    # One audience today; the function exists so call sites already read the
    # cookie by audience rather than by a hard-coded name.
    return COOKIE_NAME


def set_session_cookie(
    response: web.StreamResponse,
    token: str,
    *,
    audience: str = AUDIENCE_ADMIN,
) -> None:
    response.set_cookie(
        cookie_name(audience),
        token,
        max_age=session_seconds(),
        httponly=True,
        samesite="Lax",
        secure=DASHBOARD_SECURE_COOKIES,
        path="/",
    )


def clear_session_cookie(
    response: web.StreamResponse, *, audience: str = AUDIENCE_ADMIN
) -> None:
    response.del_cookie(cookie_name(audience), path="/")


# ── Login throttling ──────────────────────────────────────────────────────
class LoginThrottle:
    """In-memory failure counter, keyed by client address.

    Memory-only on purpose: the dashboard is a single process with a single
    operator, and losing the counters on restart costs nothing.
    """

    def __init__(
        self,
        *,
        max_failures: int | None = None,
        window_seconds: int | None = None,
    ):
        self.max_failures = (
            config.DASHBOARD_LOGIN_MAX_FAILURES
            if max_failures is None
            else max_failures
        )
        self.window = (
            config.DASHBOARD_LOGIN_WINDOW_SECONDS
            if window_seconds is None
            else window_seconds
        )
        self._failures: dict[str, deque] = {}
        # Addresses already reported as blocked in the current window. See
        # `should_audit_block`.
        self._blocked_notified: set[str] = set()

    def _prune(self, key: str) -> deque:
        now = time.time()
        bucket = self._failures.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if not bucket:
            # The window has emptied, so the next block is a new event worth
            # reporting rather than a continuation of the one already reported.
            self._blocked_notified.discard(key)
        return bucket

    def retry_after(self, key: str) -> int:
        """Seconds until this address may try again; ``0`` means it may now."""
        bucket = self._prune(key)
        if len(bucket) < self.max_failures:
            return 0
        return max(1, int(self.window - (time.time() - bucket[0])) + 1)

    def should_audit_block(self, key: str) -> bool:
        """True the first time this address is seen blocked in the current window.

        The brake is on the *address*, not on the guess, so a blocked caller can
        keep knocking. Auditing every knock would turn a brute-force attempt into
        a way to grow the audit table from outside, so only the first blocked
        request in a window is recorded — bounded by construction at
        ``max_failures + 1`` rows per address per window.
        """
        self._prune(key)
        if key in self._blocked_notified:
            return False
        self._blocked_notified.add(key)
        return True

    def record_failure(self, key: str) -> None:
        self._prune(key).append(time.time())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
        self._blocked_notified.discard(key)

    def clear(self) -> None:
        self._failures.clear()
        self._blocked_notified.clear()


throttle = LoginThrottle()


def client_ip(request: web.Request) -> str:
    """Best-effort client address, honouring a reverse proxy's header."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


# ── Middlewares ───────────────────────────────────────────────────────────
def is_public(path: str) -> bool:
    """Whether a path may be used without a session.

    The prefix comparison also accepts the prefix *without* its trailing slash,
    so ``/static`` is public for the same reason ``/static/app.css`` is. It is
    not a hole: the static resource itself answers it — with a redirect or a 404
    — and treating it as protected instead would answer a public URL with a
    login redirect.
    """
    if path in PUBLIC_PATHS:
        return True
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in PUBLIC_PREFIXES
    )


def audience_for_path(path: str) -> str:
    """Which surface a request belongs to, decided by the path alone.

    Never by a header, cookie or query parameter: a caller must not be able to
    ask to be treated as another audience.
    """
    return AUDIENCE_ADMIN


def login_path_for(audience: str) -> str:
    """Where an unauthenticated caller of this audience should be sent."""
    return "/login"


@web.middleware
async def session_middleware(request: web.Request, handler):
    """Attach the session to the request, gate everything else, and rotate.

    Rotation happens *before* the handler runs, so the CSRF token the page is
    rendered with is the token the freshly-set cookie carries. Rotating after
    the handler would hand the browser a new session whose CSRF token no form on
    the just-rendered page knows, and every POST from that page would fail.
    """
    audience = audience_for_path(request.path)
    request[context.AUDIENCE] = audience
    session = read_session(
        request.cookies.get(cookie_name(audience)), audience=audience
    )
    request[context.SESSION] = session

    # Public paths are never rotated: there is nothing to protect on them, and
    # returning early keeps the cookie unchanged for the login page.
    if is_public(request.path):
        return await handler(request)

    if session is None:
        # A browser navigating to a page gets sent to the login page; anything
        # else gets a plain 403, which the error middleware turns into the same
        # redirect. Deciding on Accept: would be guesswork.
        if request.method in ("GET", "HEAD"):
            raise web.HTTPFound(f"{login_path_for(audience)}?next={request.path_qs}")
        raise web.HTTPForbidden(reason=REASON_AUTH_REQUIRED)

    rotated_token: str | None = None
    if request.method in ("GET", "HEAD") and needs_rotation(session):
        rotated_token, session = rotate_session(session)
        request[context.SESSION] = session

    response = await handler(request)
    if rotated_token:
        set_session_cookie(response, rotated_token, audience=audience)
    return response


@web.middleware
async def csrf_middleware(request: web.Request, handler):
    """Every state-changing request must carry the session's CSRF token."""
    if request.method in ("GET", "HEAD", "OPTIONS") or is_public(request.path):
        return await handler(request)

    session = request.get(context.SESSION)
    if not session:
        raise web.HTTPForbidden(reason=REASON_AUTH_REQUIRED)

    expected = session.get("csrf") or ""
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied and request.content_type in (
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ):
        try:
            post = await request.post()
            supplied = str(post.get("csrf") or "")
        except Exception:
            supplied = ""

    if not expected or not hmac.compare_digest(supplied, expected):
        raise web.HTTPForbidden(reason=REASON_STALE_FORM)
    return await handler(request)


__all__ = [
    "AUDIENCE_ADMIN",
    "COOKIE_NAME",
    "LoginThrottle",
    "REASON_AUTH_REQUIRED",
    "REASON_STALE_FORM",
    "change_password",
    "client_ip",
    "cookie_name",
    "create_session",
    "csrf_middleware",
    "hash_password",
    "is_configured",
    "is_public",
    "needs_rotation",
    "operator_configured",
    "operator_id",
    "password_problems",
    "read_session",
    "rotate_session",
    "secret_is_ephemeral",
    "session_label",
    "session_middleware",
    "session_seconds",
    "set_session_cookie",
    "clear_session_cookie",
    "throttle",
    "verify_credentials",
]
