"""The one place an administrative action is authorised, and the one place it runs.

Every administrative action in this bot — whether a person typed ``/ban`` or
asked the assistant *"این کاربر رو بن کن"* — arrives here as an
:class:`AdminRequest` and leaves as an :class:`AdminResult`. There is no second
path. That is the whole point of the module: the brief's requirement is that
Gemini and the Python commands "must NOT create two competing implementations of
Telegram actions", and the only way to make that true is to have exactly one
function that performs them.

Three separations make this work, and each one is a boundary rather than a
convention:

**The request carries identity, never authority.** A request says *who* is
acting (a Telegram user id), *what* they want, and *on whom*. It never says
"allowed". Authority is resolved here, from ``app/rbac.py``, on every call — so
a request that was built by a language model, or hand-written by an attacker, or
replayed from an hour ago, has exactly the same standing as one built by a
command handler: none, until :func:`authorize` says otherwise. There is no field
in :class:`AdminRequest` that can express "I am the owner", because a field that
can express it is a field a model can fill in.

**Telegram is reached through a gateway, not directly.** This module never
imports ``telegram``. It asks a :class:`Gateway` to do things, and the gateway
in ``app/main.py`` is the only object that holds a bot instance. Two things fall
out of that: the whole service is testable without a network, and the set of
Telegram operations this bot can ever perform is the gateway's method list —
which is short, explicit and reviewable, rather than "whatever ``ctx.bot``
happens to expose".

**Refusals are values, not exceptions.** Every failure — a missing permission, a
protected target, a bot without ``can_promote_members``, a Telegram error —
comes back as a result with a machine-readable ``outcome``. Callers map that to
a sentence. Nothing raises into a handler, and nothing reports success it did
not get.

What this module deliberately does not do: it does not decide what a role means
(``app/rbac.py`` does), it does not know how a request was phrased (the callers
do), and it does not read message text. It takes a typed request and returns a
typed result.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from . import config, db, rbac

log = logging.getLogger("guardbot.admin")

# ── Operations ────────────────────────────────────────────────────────────
# The closed set of things an administrator can ask for, and what each one
# needs. This table is the reason a model cannot invent an operation: an
# operation that is not a key here is refused before any other check runs.
#
# ``right`` is the Telegram administrator right the *bot* must hold, taken from
# the real ``ChatAdministratorRights`` field names. ``None`` means the operation
# needs no administrator right at all.
#
# ``kind`` splits the two shapes a target can take. Most operations act on a
# *user*; ``delete_message`` acts on a message. Keeping them apart is what stops
# "delete message 12345" from ever being read as "delete user 12345".
OP_USER = "user"
OP_MESSAGE = "message"


@dataclass(frozen=True)
class Operation:
    name: str
    permission: str
    right: str | None
    kind: str = OP_USER
    # True when the operation changes the *application* role table as well as
    # Telegram. Only promotions and demotions do.
    changes_role: bool = False
    # True when a missing Telegram right is reported as a note rather than
    # refusing the operation outright.
    #
    # This is not a hole, it is a statement about which layer owns the outcome.
    # For a ban, the ban *is* the Telegram call — if the bot cannot restrict
    # members there is nothing to do, so a missing right refuses. For a
    # promotion, the bot's own role table is the authoritative record and
    # Telegram's administrator flag is a second, separable thing: the brief says
    # so itself, that "a user may be an application Moderator but not currently
    # be a Telegram administrator". Refusing to record the role because Telegram
    # said no would make the two layers impossible to reconcile, which is the
    # opposite of what is wanted.
    soft_right: bool = False
    # The name this operation is written under in ``admin_audit``.
    #
    # Kept as an explicit field rather than derived from ``name``, because the
    # audit table is append-only and already contains years of rows under the
    # older vocabulary. Renaming an action here would not rename those rows — it
    # would leave an operator reading a history in two languages, where the same
    # event appears as ``moderation.ban`` before some date and something else
    # after. One vocabulary, stated once, is worth the extra column.
    audit_action: str = ""


def _op(
    name: str,
    permission: str,
    right: str | None,
    audit_action: str,
    **kwargs,
) -> Operation:
    return Operation(
        name, permission, right, audit_action=audit_action, **kwargs
    )


OPERATIONS: dict[str, Operation] = {
    "ban_member": _op(
        "ban_member", "moderation.ban", "can_restrict_members", "moderation.ban"
    ),
    "unban_member": _op(
        "unban_member", "moderation.ban", "can_restrict_members", "moderation.unban"
    ),
    "mute_member": _op(
        "mute_member", "moderation.mute", "can_restrict_members", "moderation.mute"
    ),
    "unmute_member": _op(
        "unmute_member", "moderation.mute", "can_restrict_members", "moderation.unmute"
    ),
    "warn_member": _op("warn_member", "moderation.warn", None, "moderation.warn"),
    "delete_message": _op(
        "delete_message",
        "moderation.delete",
        "can_delete_messages",
        "moderation.delete",
        kind=OP_MESSAGE,
    ),
    "promote_member": _op(
        "promote_member",
        "admins.manage",
        "can_promote_members",
        "admin.promote",
        changes_role=True,
        soft_right=True,
    ),
    "demote_member": _op(
        "demote_member",
        "admins.manage",
        "can_promote_members",
        "admin.demote",
        changes_role=True,
        soft_right=True,
    ),
}

# The role names an actor may ask for, mapped to the canonical role. Kept here
# rather than in ``app/main.py`` because both interfaces accept them now, and two
# alias tables would eventually disagree.
ROLE_ALIASES = {
    "helper": rbac.ROLE_HELPER,
    "moderator": rbac.ROLE_MODERATOR,
    "senior": rbac.ROLE_SENIOR_ADMIN,
    "senior_admin": rbac.ROLE_SENIOR_ADMIN,
}

# ── Outcomes ──────────────────────────────────────────────────────────────
# Machine keys. The callers own the Persian; this module owns the vocabulary, so
# a new outcome cannot leak an internal string into a group.
OUTCOME_OK = "ok"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_MALFORMED = "malformed"
OUTCOME_UNKNOWN_OPERATION = "unknown_operation"
OUTCOME_STALE = "stale"
OUTCOME_DENIED = "denied"
OUTCOME_BOT_LACKS_RIGHT = "bot_lacks_right"
OUTCOME_TELEGRAM_ERROR = "telegram_error"
OUTCOME_BAD_TARGET = "bad_target"
OUTCOME_TARGET_IS_BOT = "target_is_bot"
OUTCOME_UNKNOWN_ROLE = "unknown_role"
# A demotion of somebody who held no application role. Distinct from a refusal:
# nothing was forbidden, there was simply nothing to remove.
OUTCOME_NOT_AN_ADMIN = "not_an_admin"

# Which interfaces can raise a request. Recorded in the audit trail, because
# "was this a person typing or a model proposing?" is the first question after
# an unexpected action.
INTERFACE_AI = "ai"
INTERFACE_PYTHON = "python"


# ── The request ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AdminRequest:
    """One administrative intention, fully typed.

    Note what is *absent*: there is no ``is_owner``, no ``actor_role``, no
    ``allowed``. Those are derived in :func:`authorize` from ``actor_id`` and
    the database, so a request cannot assert its own authority. The brief calls
    this out explicitly — *"The model must never be allowed to declare
    'is_owner=true' by itself"* — and the way to guarantee it is to leave no
    field for it to declare it in.
    """

    operation: str
    chat_id: int
    actor_id: int
    target_id: int = 0
    message_id: int = 0
    role: str = ""
    permissions: tuple[str, ...] = ()
    reason: str = ""
    request_id: str = ""
    interface: str = INTERFACE_PYTHON
    # When the request was created, as a unix timestamp. Checked against the
    # replay window: a request that has been sitting around is not acted on.
    at: int = 0

    def normalized(self) -> "AdminRequest":
        """Coerce every field to the type the rest of the module assumes.

        The AI interface produces these from JSON, where a user id may arrive as
        ``"123"`` or ``123`` and a role as ``"Moderator"``. Normalising here
        rather than at each use site means there is one answer to "what type is
        target_id", and it is ``int``.
        """
        def _int(value, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        role = str(self.role or "").strip().lower()
        return replace(
            self,
            operation=str(self.operation or "").strip().lower(),
            chat_id=_int(self.chat_id),
            actor_id=_int(self.actor_id),
            target_id=_int(self.target_id),
            message_id=_int(self.message_id),
            role=ROLE_ALIASES.get(role, role),
            permissions=tuple(str(p) for p in (self.permissions or ()) if p),
            reason=str(self.reason or "")[:400],
            request_id=str(self.request_id or "")[:120],
            interface=self.interface if self.interface in (INTERFACE_AI, INTERFACE_PYTHON)
            else INTERFACE_PYTHON,
            at=_int(self.at),
        )


@dataclass(frozen=True)
class AdminResult:
    """What happened, in terms the caller can act on.

    ``outcome`` is the machine key; ``message`` is a Persian sentence that is
    safe to send to a group. ``duplicate`` is separate from ``ok`` so a caller
    can say "already done" rather than "done" — the second is a lie that makes
    an operator think their second attempt had an effect.
    """

    ok: bool
    operation: str
    outcome: str
    reason: str = ""
    detail: str = ""
    actor_id: int = 0
    target_id: int = 0
    chat_id: int = 0
    request_id: str = ""
    duplicate: bool = False
    message: str = ""
    # Filled by the callers that need it (the assistant wants the target's
    # name); never used for authorisation.
    extra: dict = field(default_factory=dict)


# ── The Telegram gateway ──────────────────────────────────────────────────
@runtime_checkable
class Gateway(Protocol):
    """Everything this bot is able to do to Telegram, and nothing else.

    The narrowness is the security property. There is no ``call`` method, no way
    to pass a raw method name, and no access to the underlying bot — so the
    complete set of Telegram side effects reachable from an administrative
    request is the ten methods below, and reviewing that set is reviewing the
    whole attack surface.
    """

    async def bot_right(self, chat_id: int, right: str) -> bool:
        """Whether the bot itself holds an administrator right in a chat."""

    async def promote(self, chat_id: int, user_id: int, rights: dict) -> None:
        """Grant exactly ``rights`` (``promoteChatMember``)."""

    async def demote(self, chat_id: int, user_id: int) -> None:
        """Strip every administrator right the bot could have granted."""

    async def mute(self, chat_id: int, user_id: int) -> None:
        """Restrict posting for the configured mute duration."""

    async def unmute(self, chat_id: int, user_id: int) -> None:
        """Restore the ordinary member permission set."""

    async def ban(self, chat_id: int, user_id: int) -> None: ...

    async def unban(self, chat_id: int, user_id: int) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...

    async def warn(self, chat_id: int, user_id: int, reason: str) -> None:
        """Say the warning in the group, addressed to the user."""

    async def member(self, chat_id: int, user_id: int) -> dict:
        """The target's live Telegram status, as a plain dict. Read-only."""


# ── Time ──────────────────────────────────────────────────────────────────
def _now() -> int:
    return int(time.time())


def new_request_id() -> str:
    """A fresh id for one request. The replay key."""
    return uuid.uuid4().hex


def is_stale(request: AdminRequest, *, now: int | None = None) -> bool:
    """Whether a request is outside the replay window.

    A request with no timestamp is *not* treated as stale: the Python commands
    build requests in-process and stamp them at the moment of use, so a missing
    stamp means "just made", not "from the past". The AI path always stamps, so
    this only ever relaxes the in-process path, which cannot be replayed anyway
    because it has no external representation.
    """
    if not request.at:
        return False
    window = max(0, int(config.ADMIN_REQUEST_REPLAY_WINDOW))
    if window == 0:
        return False
    return (now if now is not None else _now()) - request.at > window


# ── Idempotency ───────────────────────────────────────────────────────────
def _seen(request: AdminRequest) -> dict | None:
    """The stored result for this request id, if it was already carried out."""
    if not request.request_id:
        return None
    return db.admin_request_get(request.request_id)


def _remember(request: AdminRequest, result: AdminResult) -> None:
    """Record that this request id was handled, and how it ended.

    Written for refusals as well as successes. A replayed *refusal* is also a
    replay: answering it twice from a stale row would let a denial be re-run
    after the actor's permissions had changed in either direction.
    """
    if not request.request_id:
        return
    try:
        db.admin_request_put(
            request.request_id,
            actor_id=request.actor_id,
            chat_id=request.chat_id,
            operation=request.operation,
            target_id=request.target_id,
            outcome=result.outcome,
            at=request.at or _now(),
        )
    except Exception:  # noqa: BLE001 - never the reason an action fails
        log.exception("could not record admin request id")


# ── Authorisation ─────────────────────────────────────────────────────────
def _denied(request: AdminRequest, decision: rbac.Decision, outcome: str) -> AdminResult:
    """A refusal, carrying the same sentence every other outcome carries.

    ``message`` is filled here rather than left empty because the field means
    "the Persian sentence for this outcome" wherever it appears. The command
    path happens to re-derive a more specific sentence from ``reason``, but the
    AI path hands this straight to the model, and a model told ``message: ""``
    has been given a sentence-shaped hole where the wording should be.
    """
    return AdminResult(
        ok=False,
        operation=request.operation,
        outcome=outcome,
        reason=decision.reason,
        detail=decision.detail,
        actor_id=request.actor_id,
        target_id=request.target_id,
        chat_id=request.chat_id,
        request_id=request.request_id,
        message=message_for(outcome),
    )


def authorize(
    request: AdminRequest, *, actor: rbac.Principal | None = None
) -> rbac.Decision:
    """Whether this request may proceed, judged only on its own contents.

    The actor is *always* re-resolved from ``actor_id`` unless a caller passes
    one in — and the only caller that does is the Python command path, which has
    already resolved the same id from the same Telegram update. Nothing that
    arrives over the wire, and nothing a model produces, can supply a principal.

    Promotions and demotions take a different road because they change the role
    table: they must satisfy ``authorize_grant``, which bounds *what* may be
    handed out, not merely *who* may act.
    """
    actor = actor if actor is not None else rbac.resolve(request.actor_id)
    target = rbac.resolve(request.target_id) if request.target_id else None

    operation = OPERATIONS.get(request.operation)
    if operation is None:
        return rbac.Decision(False, rbac.REASON_MISSING_PERMISSION, "unknown operation")

    if operation.changes_role:
        role = request.role or rbac.ROLE_MODERATOR
        if role not in rbac.ROLE_PERMISSIONS:
            return rbac.Decision(False, rbac.REASON_UNKNOWN_ROLE, role)
        # An explicit permission set is honoured when one is supplied, and
        # ``authorize_grant`` still bounds it twice over: nothing beyond the
        # role's own bundle, and nothing beyond what this actor may hand out.
        #
        # Only the Python confirmation UI ever supplies one — the operator
        # toggles individual permissions there. The AI path cannot, because
        # ``promote_member`` has no such parameter, which is how "the model may
        # not specify arbitrary Telegram rights" is enforced structurally
        # rather than by a check somebody has to remember.
        permissions = request.permissions or rbac.ROLE_PERMISSIONS[role]
        return rbac.authorize_grant(actor, role, permissions, target=target)

    return rbac.authorize(actor, operation.permission, target=target)


# ── Execution ─────────────────────────────────────────────────────────────
async def execute(
    request: AdminRequest,
    gateway: Gateway,
    *,
    actor: rbac.Principal | None = None,
    bot_id: int = 0,
) -> AdminResult:
    """Authorise one request, then carry it out. The whole pipeline, in order.

    The order below is the brief's list and it is not negotiable: shape, then
    replay, then actor, then permission, then target, then Telegram's own
    rights, then the call. Every step that can refuse does so *before* anything
    with a side effect has run, so a refusal never leaves a half-finished
    action behind.

    ``bot_id`` is passed in rather than read from the gateway because the
    gateway's protocol is deliberately about chat operations; the caller knows
    its own bot id.
    """
    request = request.normalized()

    # 1. Shape. An unknown operation, or a request with no chat or no actor, is
    #    refused before anything else — including before the replay lookup, so a
    #    malformed request cannot be used to probe the idempotency table.
    operation = OPERATIONS.get(request.operation)
    if operation is None:
        return _result(
            request, OUTCOME_UNKNOWN_OPERATION, detail=request.operation
        )
    if not request.chat_id or not request.actor_id:
        return _result(request, OUTCOME_MALFORMED, detail="chat_id/actor_id")

    # 2. Replay window, then idempotency.
    if is_stale(request):
        return _result(request, OUTCOME_STALE)
    seen = _seen(request)
    if seen is not None:
        return _result(
            request,
            OUTCOME_DUPLICATE,
            duplicate=True,
            ok=seen.get("outcome") == OUTCOME_OK,
            detail=seen.get("outcome", ""),
        )

    # 3. The target must be real, and must not be the bot itself. Promoting the
    #    bot is a no-op that looks like a success, and banning it is worse.
    if operation.kind == OP_USER:
        if not request.target_id:
            return _result(request, OUTCOME_BAD_TARGET)
        if bot_id and request.target_id == bot_id:
            return _result(request, OUTCOME_TARGET_IS_BOT)
    else:
        if not request.message_id:
            return _result(request, OUTCOME_BAD_TARGET, detail="message_id")

    # 4. Authorisation, resolved here, from the id. Nothing the caller said
    #    about itself is trusted, because nothing the caller said about itself
    #    is read.
    decision = authorize(request, actor=actor)
    if not decision:
        result = _denied(request, decision, OUTCOME_DENIED)
        _record(request, result, decision=decision)
        return result

    # 5. Telegram's own permission for this action, checked live. Configuration
    #    saying the bot should have a right is not evidence that it has one.
    #    Skipped for the operations whose outcome the application owns — see
    #    ``Operation.soft_right``.
    if (
        operation.right
        and not operation.soft_right
        and not await gateway.bot_right(request.chat_id, operation.right)
    ):
        result = _result(request, OUTCOME_BOT_LACKS_RIGHT, detail=operation.right or "")
        _record(request, result)
        return result

    # 6. The call itself. A Telegram failure is reported as a failure, never
    #    smoothed over — the brief is explicit that the caller must not fabricate
    #    success.
    try:
        note = await _apply(request, operation, gateway)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        log.warning("admin action %s failed: %s", request.operation, exc)
        result = _result(
            request, OUTCOME_TELEGRAM_ERROR, detail=str(exc)[:160]
        )
        _record(request, result)
        return result

    if note == OUTCOME_NOT_AN_ADMIN:
        result = _result(request, OUTCOME_NOT_AN_ADMIN)
        _record(request, result)
        return result

    result = _result(request, OUTCOME_OK, ok=True)
    if operation.changes_role:
        # What was granted, in the audit row. The old command wrote exactly this
        # string and the trail is read by an operator looking for it, so the
        # detail is reproduced rather than reinvented.
        role = request.role or rbac.ROLE_MODERATOR
        permissions = request.permissions or rbac.ROLE_PERMISSIONS[role]
        result = replace(
            result,
            detail=f"role={role} perms={','.join(sorted(permissions))}",
        )
    if note:
        # The application change went through and Telegram did not. The caller
        # shows the note beside the success rather than instead of it, which is
        # the honest description: two layers, one of which refused.
        result = replace(result, extra={"telegram_note": note})
    _record(request, result)
    log.info(
        "admin action=%s interface=%s actor=%s target=%s chat=%s",
        request.operation,
        request.interface,
        request.actor_id,
        request.target_id or request.message_id,
        request.chat_id,
    )
    return result


async def _apply(request: AdminRequest, operation: Operation, gateway: Gateway) -> str:
    """Make the one Telegram call this operation names.

    Returns a note to show beside the success, or an empty string. Only the two
    role-changing operations produce one, and only when the application layer
    succeeded and Telegram did not.
    """
    chat_id = request.chat_id
    if request.operation == "ban_member":
        await gateway.ban(chat_id, request.target_id)
    elif request.operation == "unban_member":
        await gateway.unban(chat_id, request.target_id)
    elif request.operation == "mute_member":
        await gateway.mute(chat_id, request.target_id)
    elif request.operation == "unmute_member":
        await gateway.unmute(chat_id, request.target_id)
    elif request.operation == "warn_member":
        await gateway.warn(chat_id, request.target_id, request.reason)
    elif request.operation == "delete_message":
        await gateway.delete(chat_id, request.message_id)
    elif request.operation == "promote_member":
        return await _promote(request, gateway)
    elif request.operation == "demote_member":
        return await _demote(request, gateway)
    else:  # pragma: no cover - OPERATIONS and this branch move together
        raise ValueError(f"unhandled operation {request.operation}")
    return ""


async def _promote(request: AdminRequest, gateway: Gateway) -> str:
    """Record the application role, then try to mirror it in Telegram.

    The order and the tolerance are both deliberate, and both are inherited from
    the command this replaces. The bot's own role table is the authoritative
    record of who may use this bot's commands; Telegram's administrator flag is
    a separate capability that the bot may or may not be able to grant, and the
    brief's own §35 describes the state where the two disagree. Writing the role
    first means a Telegram refusal cannot lose the operator's decision, and
    returning the refusal as a *note* means it is reported rather than hidden.

    The role's permissions decide the Telegram flags — never the caller. The
    brief forbids the model specifying ``can_delete_messages`` and friends, and
    the way that is enforced is that :class:`AdminRequest` has no field for
    them: the only thing a caller can name is a role, and ``rbac`` maps it.
    """
    role = request.role or rbac.ROLE_MODERATOR
    permissions = request.permissions or rbac.ROLE_PERMISSIONS[role]
    db.admin_set(
        request.target_id,
        role,
        permissions,
        granted_by=request.actor_id,
        note=f"via {request.interface}",
    )
    return await _mirror_in_telegram(
        gateway, request.chat_id, request.target_id, permissions
    )


async def _mirror_in_telegram(
    gateway: Gateway, chat_id: int, user_id: int, permissions
) -> str:
    """Grant the role's Telegram rights. Returns "" or a sentence to append.

    Four outcomes rather than two, and the distinction is the point: "the API
    refused" and "we chose not to ask" are different facts, and an operator
    acting on the first would chase a problem that does not exist. A role whose
    permissions map to no Telegram right at all is a legitimate state — the
    helper role is application-only — and it is not a failure.
    """
    rights = rbac.telegram_rights_for(permissions)
    if not rights:
        return config.ADMIN_PROMOTE_NO_TELEGRAM_TEXT
    if not await gateway.bot_right(chat_id, "can_promote_members"):
        log.warning("cannot promote in %s: the bot lacks can_promote_members", chat_id)
        return config.ADMIN_BOT_LACKS_RIGHT_TEXT
    try:
        await gateway.promote(chat_id, user_id, rights)
    except Exception as exc:  # noqa: BLE001 - reported as a note, never raised
        log.warning("promote_chat_member failed: %s", exc)
        return config.ADMIN_TELEGRAM_FAILED_TEXT
    return config.ADMIN_PROMOTE_TELEGRAM_TEXT


async def _demote(request: AdminRequest, gateway: Gateway) -> str:
    """Remove the application role, then strip the Telegram rights.

    Returns ``OUTCOME_NOT_AN_ADMIN`` when there was no application role to
    remove — the caller turns that into "there was nothing to do" rather than a
    success, because a demotion that removed nothing has not happened.
    """
    removed = db.admin_remove(request.target_id)
    if not removed:
        return OUTCOME_NOT_AN_ADMIN

    if not await gateway.bot_right(request.chat_id, "can_promote_members"):
        return config.ADMIN_PROMOTE_NO_TELEGRAM_TEXT
    try:
        await gateway.demote(request.chat_id, request.target_id)
    except Exception as exc:  # noqa: BLE001 - reported as a note, never raised
        log.warning("demotion in Telegram failed: %s", exc)
        return config.ADMIN_TELEGRAM_FAILED_TEXT
    return ""


# ── Result plumbing ───────────────────────────────────────────────────────
def _result(
    request: AdminRequest,
    outcome: str,
    *,
    ok: bool = False,
    detail: str = "",
    duplicate: bool = False,
) -> AdminResult:
    return AdminResult(
        ok=ok,
        operation=request.operation,
        outcome=outcome,
        detail=detail,
        actor_id=request.actor_id,
        target_id=request.target_id or request.message_id,
        chat_id=request.chat_id,
        request_id=request.request_id,
        duplicate=duplicate,
        message=message_for(outcome),
    )


def _record(
    request: AdminRequest,
    result: AdminResult,
    *,
    decision: rbac.Decision | None = None,
) -> None:
    """Write the audit row and remember the request id.

    The audit row is written for refusals too. "Who tried" is the question asked
    after an incident, and a trail that only records successes cannot answer it.
    """
    operation = OPERATIONS.get(request.operation)
    try:
        db.audit_write(
            request.actor_id,
            operation.audit_action if operation else request.operation,
            outcome=result.outcome,
            target_id=request.target_id or request.message_id or None,
            chat_id=request.chat_id or None,
            detail=(
                (decision.detail or decision.reason)
                if decision is not None and not decision.allowed
                else result.detail
            ),
            interface=request.interface,
        )
    except Exception:  # noqa: BLE001
        log.exception("audit write failed action=%s", request.operation)
    _remember(request, result)


# ── Sentences ─────────────────────────────────────────────────────────────
# One key, one sentence, in the same place — so the AI and the command paths
# cannot describe the same outcome two different ways.
def message_for(outcome: str) -> str:
    return {
        OUTCOME_OK: config.ADMIN_DONE_TEXT,
        OUTCOME_DUPLICATE: config.ADMIN_DUPLICATE_TEXT,
        OUTCOME_MALFORMED: config.ADMIN_DENIED_TEXT,
        OUTCOME_UNKNOWN_OPERATION: config.ADMIN_DENIED_TEXT,
        OUTCOME_STALE: config.ADMIN_STALE_TEXT,
        OUTCOME_DENIED: config.ADMIN_DENIED_TEXT,
        OUTCOME_BOT_LACKS_RIGHT: config.ADMIN_BOT_LACKS_RIGHT_TEXT,
        OUTCOME_TELEGRAM_ERROR: config.MOD_COMMAND_FAILED_TEXT,
        OUTCOME_BAD_TARGET: config.MOD_TARGET_REQUIRED_TEXT,
        OUTCOME_TARGET_IS_BOT: config.ADMIN_TARGET_IS_BOT_TEXT,
        OUTCOME_UNKNOWN_ROLE: config.ADMIN_DENIED_TEXT,
        OUTCOME_NOT_AN_ADMIN: config.ADMIN_DEMOTE_NOTHING_TEXT,
    }.get(outcome, config.ADMIN_DENIED_TEXT)


# English glosses for the machine keys. They exist for exactly one audience: the
# model, which has to explain a refusal to a person in Persian and cannot do
# that from the token ``higher_rank``. They are never shown to a user directly —
# the Persian sentence is — so this is not a second copy of the copy.
REASON_GLOSS = {
    rbac.REASON_NO_OWNER: "no owner is configured for this bot, so no administrative action is authorised at all",
    rbac.REASON_NOT_ADMIN: "the person asking is not an administrator of this bot",
    rbac.REASON_MISSING_PERMISSION: "the person asking does not hold the permission this action requires",
    rbac.REASON_OWNER_PROTECTED: "the target is the bot's owner, who can never be the target of an administrative action",
    rbac.REASON_HIGHER_RANK: "the target is at or above the asker's own level in the hierarchy",
    rbac.REASON_CANNOT_GRANT_ROLE: "the asker is not allowed to hand out that role",
    rbac.REASON_CANNOT_GRANT_PERMISSION: "the asker is not allowed to hand out those permissions",
    rbac.REASON_UNKNOWN_ROLE: "that role does not exist",
    rbac.REASON_SELF_TARGET: "the target is the person asking",
    rbac.REASON_BAD_TARGET: "the target is not usable",
}

OUTCOME_GLOSS = {
    OUTCOME_DUPLICATE: "this exact request had already been carried out, so it was not repeated",
    OUTCOME_MALFORMED: "the request was incomplete and was not executed",
    OUTCOME_UNKNOWN_OPERATION: "the requested operation does not exist",
    OUTCOME_STALE: "the request was too old to act on and was not executed",
    OUTCOME_BOT_LACKS_RIGHT: "the bot itself does not have the required Telegram permission in this group",
    OUTCOME_TELEGRAM_ERROR: "Telegram refused the operation",
    OUTCOME_BAD_TARGET: "no usable target was identified",
    OUTCOME_TARGET_IS_BOT: "the target was this bot itself",
    OUTCOME_UNKNOWN_ROLE: "that role does not exist",
    OUTCOME_NOT_AN_ADMIN: "that person holds no administrative role here",
}


def explain(result: AdminResult) -> str:
    """One English sentence for the model to build a Persian answer on.

    Deliberately a gloss and not a sentence to repeat: the brief asks for
    natural conversation, and a model that echoes a canned string has stopped
    being conversational. This tells it *why*, and leaves the wording to it.
    """
    if result.outcome == OUTCOME_DENIED and result.reason:
        return REASON_GLOSS.get(result.reason, "the action was refused")
    return OUTCOME_GLOSS.get(result.outcome, "the action did not go through")


def describe(result: AdminResult) -> dict:
    """A log-safe summary. Ids and keys only — never a credential, never text."""
    return {
        "operation": result.operation,
        "outcome": result.outcome,
        "ok": result.ok,
        "duplicate": result.duplicate,
        "actor_id": result.actor_id,
        "target_id": result.target_id,
        "chat_id": result.chat_id,
        "reason": result.reason,
    }


# ── Mode status ───────────────────────────────────────────────────────────
def ai_available() -> bool:
    """Whether AI-mediated administration can run right now.

    Two conditions, and both must hold: the operator must have switched it on,
    and there must be a conversational credential to talk to. A switched-on
    feature with no key is not available, and reporting it as available would be
    the "silently pretend Gemini succeeded" failure the brief names.
    """
    if not config.ADMIN_AI_ENABLED:
        return False
    from . import chat  # imported late: chat imports this module's config peers

    return chat.is_enabled()


def python_fallback_available() -> bool:
    """Whether the direct commands still work. They always do, unless disabled."""
    return bool(config.ADMIN_PYTHON_ENABLED)


def mode_status() -> dict:
    """The operational line: which mode is live, and what is behind it.

    Three states rather than two, because "AI is off" and "AI is broken" are
    different facts and an operator needs to tell them apart:

    * ``ai``       — AI administration is up.
    * ``degraded`` — AI is configured but unavailable; the commands are the way.
    * ``python``   — AI administration is switched off by configuration.
    """
    ai = ai_available()
    fallback = python_fallback_available()
    if ai:
        mode = "ai"
    elif config.ADMIN_AI_ENABLED and fallback:
        mode = "degraded"
    else:
        mode = "python"
    return {
        "mode": mode,
        "ai_available": ai,
        "python_fallback": fallback,
        "ai_enabled": bool(config.ADMIN_AI_ENABLED),
    }


def mode_line() -> str:
    """The one-line status for the startup log and the owner's report."""
    state = mode_status()
    if state["mode"] == "ai":
        return "AI ADMIN MODE: AVAILABLE"
    if state["mode"] == "degraded":
        return "AI ADMIN MODE: DEGRADED — PYTHON FALLBACK ACTIVE"
    return "AI ADMIN MODE: OFF — PYTHON COMMANDS ONLY"


# Outcomes that mean "nothing happened, and here is why". A duplicate is
# deliberately absent: the desired state does hold, it was simply reached
# earlier, and counting it as a failure would make an operator chase a problem
# that does not exist.
_REFUSAL_OUTCOMES = frozenset({
    OUTCOME_DENIED,
    OUTCOME_BOT_LACKS_RIGHT,
    OUTCOME_TELEGRAM_ERROR,
    OUTCOME_MALFORMED,
    OUTCOME_STALE,
    OUTCOME_BAD_TARGET,
    OUTCOME_TARGET_IS_BOT,
    OUTCOME_UNKNOWN_ROLE,
    OUTCOME_UNKNOWN_OPERATION,
})


def recent_refusals(limit: int = 5) -> list[dict]:
    """Recent administrative requests that did not happen, newest first.

    Read from the audit table rather than kept in memory, because the question
    this answers — "why has nothing been working?" — is usually asked after a
    restart, and a counter that resets with the process would answer it with a
    confident zero.

    The scan is bounded: the audit table is ordered by id, so a window of recent
    rows is enough, and it is the refusals inside that window that matter.
    """
    window = max(1, int(limit))
    rows = db.audit_recent(window * 6)
    return [r for r in rows if r.get("outcome") in _REFUSAL_OUTCOMES][:window]


def status_report() -> str:
    """The operator's view of AI administration: the mode, and what was refused.

    Both halves are needed to tell the two failure shapes apart. "The assistant
    never answers" and "the assistant answers and is refused every time" look
    identical from inside a group and are completely different problems; the
    first is this mode line, the second is the refusal list.
    """
    state = mode_status()
    if state["mode"] == "ai":
        headline = "Conversational administration is answering."
    elif state["mode"] == "degraded":
        headline = (
            "Gemini is configured but not answering, so the commands are the "
            "way in. They work; nothing here is broken."
        )
    else:
        headline = "AI administration is switched off. Use the commands."

    lines = [mode_line(), headline]

    refusals = recent_refusals()
    lines.append("")
    if not refusals:
        lines.append("No administrative refusals on record.")
        return "\n".join(lines)

    lines.append(f"Recent refusals ({len(refusals)}):")
    for row in refusals:
        via = row.get("interface") or "-"
        lines.append(
            f"  • {row['action']} actor={row['actor_id']} "
            f"outcome={row['outcome']} via={via}"
        )
    return "\n".join(lines)


def reset_state() -> None:
    """No module-level mutable state; kept for the test-reset convention."""
    return None
