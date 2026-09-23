"""The VPN bot's operational surface, reached through the one gateway.

This module is the ``codebuddy_task`` pattern applied to a second external
service. It is **not** a second gateway: it holds no authority of its own, it is
never called by a Telegram handler, and it cannot be reached except through
``app/admin_service.execute`` — which is where the actor id becomes an authority
and where the audit row is written. Everything here is downstream of that.

What it does own is the three things ``admin_service`` should not have to know:

* **The operation catalogue.** Which of the six writes exist, which of them
  require the owner's explicit confirmation, and how each one is worded when the
  owner is asked to approve it.
* **The translation.** The VPN bot answers ``{"ok": false, "code": "..."}`` with
  a 200 and a machine code, because a refusal is a decision about a user rather
  than a transport failure. This module turns those codes, and the four
  ``VpnBotError`` codes that *are* transport failures, into this system's
  outcome vocabulary. Nothing is ever reported as done that was not.
* **The confirmation path.** The three operations that move money or bulk-reject
  orders are recorded and *not* executed. Only a second, separately authorised
  request releases one.

The last point is the security property worth stating plainly: a model cannot
execute a money operation. It can ask for one, which records a row and produces
a question; and it can ask for that row to be confirmed, which is a *reference*
and not an approval. Everything the execution actually needs is re-derived from
the stored row at confirmation time, so a model that confirms "the pending
operation" cannot smuggle a different amount, a different user or a different
transaction into the call.

The confirmation rules themselves are not reimplemented here.
:func:`app.agent_bridge.resolve_confirmation` already encodes them — only the
owner confirms, there must be something pending, a named reference must really
be waiting, and a bare confirmation resolves only when exactly one thing is —
and a second copy of that reasoning would be a second answer to "who may
approve". The two flows therefore cannot drift.
"""
from __future__ import annotations

import json
import logging
import time

from . import admin_service, agent_bridge, agent_data, config, db, rbac, vpnbot

log = logging.getLogger("guardbot.vpn")

# Every how many recorded operations the retention window is applied. The same
# shape as ``people.py`` and ``admin_service.py``, and for the same reason: this
# is called from the operation path, so it must not run a DELETE per call, and a
# rule that is never applied is not a retention rule.
PRUNE_EVERY = 50
_since_prune = 0


# ── The operation catalogue ───────────────────────────────────────────────
# The six writes, by the name they are audited under and the name the model is
# offered. Kept as a closed table for the same reason ``admin_service``'s is:
# an operation that is not a key here is refused before anything is recorded.
VPN_SERVICE_ENABLED = "vpn_service_enabled"
VPN_NOTIFICATIONS = "vpn_notifications"
VPN_PLAN_ACTIVE = "vpn_plan_active"
VPN_BALANCE = "vpn_balance"
VPN_ORDERS_SWEEP = "vpn_orders_sweep"
VPN_TRANSACTION_STATUS = "vpn_transaction_status"
# Not a write of its own: the second half of the three that need approving.
VPN_CONFIRM = "vpn_confirm"


class VpnOperation:
    """One operation, and the two things this module needs to know about it."""

    __slots__ = ("name", "label", "needs_confirmation")

    def __init__(self, name: str, label: str, *, needs_confirmation: bool = False):
        self.name = name
        self.label = label
        self.needs_confirmation = needs_confirmation


VPN_OPS: dict[str, VpnOperation] = {
    op.name: op
    for op in (
        VpnOperation(VPN_SERVICE_ENABLED, "فعال/غیرفعال کردن یک سرویس"),
        VpnOperation(VPN_NOTIFICATIONS, "روشن/خاموش کردن یادآوری‌های یک کاربر"),
        VpnOperation(VPN_PLAN_ACTIVE, "فعال/غیرفعال کردن یک پلن"),
        # ── The three that are recorded and not executed ──────────────────
        # A balance adjustment, a bulk rejection of orders and a transaction
        # status change are the operations that cannot be undone by asking
        # again. They are also the three a plausible-sounding sentence can
        # talk somebody into, which is exactly why the sentence is not enough
        # and a separate, explicit approval is required.
        VpnOperation(
            VPN_BALANCE, "تغییر موجودی کیف پول", needs_confirmation=True
        ),
        VpnOperation(
            VPN_ORDERS_SWEEP, "رد کردن سفارش‌های قدیمی", needs_confirmation=True
        ),
        VpnOperation(
            VPN_TRANSACTION_STATUS,
            "تغییر وضعیت یک تراکنش",
            needs_confirmation=True,
        ),
    )
}

# The names the write tool may name. ``vpn_confirm`` is here as well because it
# carries the same payload shape — a reference and nothing else — and one set is
# easier to keep honest than two.
VPN_OPERATION_NAMES = frozenset(VPN_OPS) | {VPN_CONFIRM}


# ── Result plumbing ───────────────────────────────────────────────────────
def _result(
    request,
    outcome: str,
    *,
    ok: bool = False,
    detail: str = "",
    message: str = "",
    reason: str = "",
    extra: dict | None = None,
) -> admin_service.AdminResult:
    """One result, built the way ``admin_service`` builds its own.

    ``message`` is overridden wherever this module has something more specific
    to say than the outcome's generic sentence — a refusal that names the code,
    a pending operation that names itself. ``reason`` is the machine key a
    denial carries, so the audit row says *why* rather than only *that*.
    """
    return admin_service.AdminResult(
        ok=ok,
        operation=str(getattr(request, "operation", "") or ""),
        outcome=outcome,
        detail=str(detail or "")[:200],
        reason=str(reason or ""),
        actor_id=int(getattr(request, "actor_id", 0) or 0),
        target_id=int(getattr(request, "target_id", 0) or 0),
        chat_id=int(getattr(request, "chat_id", 0) or 0),
        request_id=str(getattr(request, "request_id", "") or ""),
        message=message or admin_service.message_for(outcome),
        extra=dict(extra or {}),
    )


def _public_pending(row: dict) -> dict:
    """The parts of a pending operation that are safe to hand back.

    No payload: it holds a balance delta and a free-text reason, both of which
    are already in the conversation, and a tool result that repeats them is a
    second copy of the owner's words in a place he did not put them.
    """
    operation = VPN_OPS.get(str(row.get("operation") or ""))
    return {
        "pending_id": str(row.get("request_id") or ""),
        "operation": str(row.get("operation") or ""),
        "operation_label": operation.label if operation else "",
        "subject": str(row.get("subject") or "")[:200],
        "created_at": int(row.get("created_at") or 0),
        "expires_at": int(row.get("expires_at") or 0),
    }


# ── Validation ────────────────────────────────────────────────────────────
def _missing(request) -> str:
    """Which required field is absent, or "" when the request is complete.

    Per-operation rather than generic, because "what does this operation need"
    is the operation's own business: a balance change needs an amount, a sweep
    needs a day count, and a service toggle needs neither. Returning the *name*
    of the missing field rather than a sentence means the caller can log it
    precisely and the audit row says which field was absent.
    """
    operation = str(getattr(request, "operation", "") or "")
    if operation == VPN_SERVICE_ENABLED:
        if not int(getattr(request, "service_id", 0) or 0):
            return "service_id"
        if getattr(request, "enabled", None) is None:
            return "enabled"
    elif operation == VPN_NOTIFICATIONS:
        if not int(getattr(request, "target_id", 0) or 0):
            return "telegram_id"
        if getattr(request, "enabled", None) is None:
            return "enabled"
    elif operation == VPN_PLAN_ACTIVE:
        if not int(getattr(request, "plan_id", 0) or 0):
            return "plan_id"
        if getattr(request, "enabled", None) is None:
            return "enabled"
    elif operation == VPN_BALANCE:
        if not int(getattr(request, "target_id", 0) or 0):
            return "telegram_id"
        if not int(getattr(request, "amount", 0) or 0):
            return "amount"
        if not str(getattr(request, "reason", "") or "").strip():
            return "reason"
    elif operation == VPN_ORDERS_SWEEP:
        if int(getattr(request, "days", 0) or 0) <= 0:
            return "days"
        if not str(getattr(request, "reason", "") or "").strip():
            return "reason"
    elif operation == VPN_TRANSACTION_STATUS:
        if not int(getattr(request, "transaction_id", 0) or 0):
            return "transaction_id"
        if not str(getattr(request, "status", "") or "").strip():
            return "status"
        if not str(getattr(request, "reason", "") or "").strip():
            return "reason"
    else:  # pragma: no cover - VPN_OPS and this branch move together
        return "operation"
    return ""


def _subject(request) -> str:
    """One line naming what is about to change, for the approval question.

    Built from ids and numbers only. The free-text reason is included because
    the owner wrote it and is being asked to approve it — but it is bounded, and
    it is the *owner's* text rather than anything a user could have sent.
    """
    operation = str(getattr(request, "operation", "") or "")
    reason = str(getattr(request, "reason", "") or "").strip()[:200]
    if operation == VPN_BALANCE:
        return (
            f"کاربر {int(request.target_id)}، تغییر موجودی "
            f"{int(request.amount):+d} — دلیل: {reason}"
        )
    if operation == VPN_ORDERS_SWEEP:
        return (
            f"همهٔ سفارش‌های قدیمی‌تر از {int(request.days)} روز رد می‌شوند "
            f"— دلیل: {reason}"
        )
    if operation == VPN_TRANSACTION_STATUS:
        return (
            f"تراکنش {int(request.transaction_id)} → {str(request.status)[:32]}"
            f"{'، با جبران' if getattr(request, 'compensate', False) else ''} "
            f"— دلیل: {reason}"
        )
    return operation


# ── The stored payload ────────────────────────────────────────────────────
# The fields that make up the operation, and nothing else. ``chat_id`` and
# ``actor_id`` are deliberately absent: they live in the pending row's own
# columns, and re-reading them from a JSON blob the model could in principle
# have influenced is a door that does not need to exist.
_PAYLOAD_FIELDS = (
    "target_id",
    "service_id",
    "plan_id",
    "transaction_id",
    "days",
    "amount",
    "enabled",
    "compensate",
    "reason",
    "status",
)


def _payload(request) -> dict:
    return {name: getattr(request, name, None) for name in _PAYLOAD_FIELDS}


def _request_from_row(row: dict, request):
    """Rebuild the operation from the row that was validated when it was asked.

    This is the whole reason the confirmation is a reference and not an
    approval. Everything the execution needs comes from here — from the record
    written the first time, by the gateway, after authorisation — and the
    confirming request contributes only its own identity and interface.
    """
    try:
        payload = json.loads(str(row.get("payload") or "{}"))
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return admin_service.AdminRequest(
        operation=str(row.get("operation") or ""),
        chat_id=int(row.get("chat_id") or 0),
        actor_id=int(getattr(request, "actor_id", 0) or 0),
        target_id=int(payload.get("target_id") or 0),
        service_id=int(payload.get("service_id") or 0),
        plan_id=int(payload.get("plan_id") or 0),
        transaction_id=int(payload.get("transaction_id") or 0),
        days=int(payload.get("days") or 0),
        amount=int(payload.get("amount") or 0),
        enabled=payload.get("enabled") if isinstance(payload.get("enabled"), bool) else None,
        compensate=bool(payload.get("compensate")),
        reason=str(payload.get("reason") or ""),
        status=str(payload.get("status") or ""),
        request_id=str(getattr(request, "request_id", "") or ""),
        interface=str(getattr(request, "interface", "") or admin_service.INTERFACE_PYTHON),
        at=int(getattr(request, "at", 0) or 0),
    ).normalized()


# ── Calling the VPN bot ───────────────────────────────────────────────────
async def _call(request) -> dict:
    """Make the one call this operation names.

    The ``operator_id`` on every write is this bot's assertion of *who asked*,
    and it is deliberately the actor id the gateway authorised rather than
    anything the model supplied. The VPN bot records it as ``guardbot:<id>`` in
    its own audit table. It is asserted and not proven — the HMAC proves which
    service asked — which is why the authorisation happened here, before this
    function was reached.
    """
    operator = int(getattr(request, "actor_id", 0) or 0)
    interface = str(getattr(request, "interface", "") or "")
    operation = str(getattr(request, "operation", "") or "")

    if operation == VPN_SERVICE_ENABLED:
        return await vpnbot.set_service_enabled(
            int(request.service_id),
            bool(request.enabled),
            operator_id=operator,
            interface=interface,
        )
    if operation == VPN_NOTIFICATIONS:
        return await vpnbot.set_notifications_enabled(
            int(request.target_id),
            bool(request.enabled),
            operator_id=operator,
            interface=interface,
        )
    if operation == VPN_PLAN_ACTIVE:
        return await vpnbot.set_plan_active(
            int(request.plan_id),
            bool(request.enabled),
            operator_id=operator,
            interface=interface,
        )
    if operation == VPN_BALANCE:
        return await vpnbot.adjust_balance(
            int(request.target_id),
            int(request.amount),
            str(request.reason),
            operator_id=operator,
            interface=interface,
        )
    if operation == VPN_ORDERS_SWEEP:
        return await vpnbot.reject_stale_orders(
            int(request.days),
            str(request.reason),
            operator_id=operator,
            interface=interface,
        )
    if operation == VPN_TRANSACTION_STATUS:
        return await vpnbot.set_transaction_status(
            int(request.transaction_id),
            str(request.status),
            str(request.reason),
            operator_id=operator,
            compensate=bool(getattr(request, "compensate", False)),
            interface=interface,
        )
    raise vpnbot.VpnBotError(vpnbot.ERR_BAD_RESPONSE, "unknown operation")


def _outcome_for_error(exc: vpnbot.VpnBotError) -> str:
    """Which of our outcomes a transport failure is.

    The distinction that matters: ``admin_disabled`` means the VPN bot's own
    write switch is off, so the next step is to look at *its* configuration —
    that is "unavailable", not "refused". A refused request whose request the
    other side could not even parse is our bug, so it is reported as an error
    rather than as a decision about a user.
    """
    if exc.code == vpnbot.ERR_REFUSED:
        if exc.detail == "admin_disabled":
            return admin_service.OUTCOME_VPN_UNAVAILABLE
        return admin_service.OUTCOME_VPN_REFUSED
    if exc.code in (vpnbot.ERR_NOT_CONFIGURED, vpnbot.ERR_UNREACHABLE):
        return admin_service.OUTCOME_VPN_UNAVAILABLE
    return admin_service.OUTCOME_VPN_ERROR


# Numbers and booleans the VPN bot returns, and nothing else. A string from the
# other side never reaches our audit row: ``reason`` is echoed back by the panel
# path and writing it down again would put a copy of operator copy in a table
# that is meant to hold ids.
_SAFE_DETAIL_KEYS = ("count", "before", "after", "delta", "changed", "days", "limit")


def _detail(answer: dict) -> str:
    parts = []
    for key in _SAFE_DETAIL_KEYS:
        value = answer.get(key)
        if isinstance(value, bool) or isinstance(value, int):
            parts.append(f"{key}={value}")
    ids = answer.get("ids")
    if isinstance(ids, list) and ids:
        parts.append(f"ids={len(ids)}")
    return " ".join(parts)[:200]


def _subject_id(request) -> str:
    """Which object this operation acted on, for the audit row.

    The ids live in different fields — a service, a plan, a transaction, a
    customer — and the audit table's ``target_id`` column holds a *Telegram*
    id, so they are named in the detail rather than put in a column where a
    later reader would take them for something they are not.
    """
    operation = str(getattr(request, "operation", "") or "")
    if operation == VPN_SERVICE_ENABLED:
        return f"service={int(getattr(request, 'service_id', 0) or 0)}"
    if operation == VPN_PLAN_ACTIVE:
        return f"plan={int(getattr(request, 'plan_id', 0) or 0)}"
    if operation == VPN_TRANSACTION_STATUS:
        return f"transaction={int(getattr(request, 'transaction_id', 0) or 0)}"
    if operation == VPN_ORDERS_SWEEP:
        return f"days={int(getattr(request, 'days', 0) or 0)}"
    if int(getattr(request, "target_id", 0) or 0):
        return f"telegram={int(request.target_id)}"
    return ""


def _from_answer(request, answer: dict) -> admin_service.AdminResult:
    """Turn the VPN bot's answer into one of our results.

    An in-band ``ok: false`` is a *decision* and is reported as a refusal with
    the code intact, so the sentence the owner gets can be about the specific
    thing that was wrong rather than "the VPN bot said no".
    """
    code = str(answer.get("code") or "").strip()[:64]
    extra: dict = {"vpn": {"code": code}}
    service = agent_data.vpn_service_view(answer.get("service"))
    if service:
        extra["vpn"]["service"] = service

    if answer.get("ok"):
        return _result(
            request,
            admin_service.OUTCOME_OK,
            ok=True,
            detail=f"{code or 'ok'} {_subject_id(request)} {_detail(answer)}".strip(),
            extra=extra,
        )
    return _result(
        request,
        admin_service.OUTCOME_VPN_REFUSED,
        detail=f"{code or 'refused'} {_subject_id(request)}".strip(),
        extra=extra,
    )


async def _execute(request) -> admin_service.AdminResult:
    """Run one operation. Never raises; a failure is an outcome."""
    try:
        answer = await _call(request)
    except vpnbot.VpnBotError as exc:
        log.warning("vpn operation %s could not be made: %s", request.operation, exc)
        return _result(request, _outcome_for_error(exc), detail=_error_detail(exc))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        log.exception("vpn operation %s failed unexpectedly", request.operation)
        return _result(
            request, admin_service.OUTCOME_VPN_ERROR, detail=type(exc).__name__
        )
    if not isinstance(answer, dict):
        return _result(request, admin_service.OUTCOME_VPN_ERROR, detail="bad_response")
    return _from_answer(request, answer)


def _error_detail(exc: vpnbot.VpnBotError) -> str:
    """What to write in the audit row for a failed call.

    A refusal's detail is a machine token the other service chose
    (``admin_disabled``, ``bad_payload``), so it is worth keeping — it is what
    tells the operator *which* switch is off. A transport failure's detail is an
    exception string, which can carry a URL and is not worth writing down, so
    the code alone is recorded. Neither is ever a credential.
    """
    if exc.code == vpnbot.ERR_REFUSED:
        return str(exc.detail or "")[:64] or vpnbot.ERR_REFUSED
    return exc.code


# ── The entry point ───────────────────────────────────────────────────────
async def submit(request) -> admin_service.AdminResult:
    """Authorise nothing, execute one VPN operation, or record it for approval.

    Reached only from ``admin_service._apply``, which has already run the whole
    pipeline: shape, system state, replay, target, RBAC, and the audit write
    that follows either way. So this function's job is narrow — validate the
    operation's own arguments, then either execute it or record it and ask.
    """
    if request.operation == VPN_CONFIRM:
        return await _confirm(request)

    operation = VPN_OPS.get(str(request.operation or ""))
    if operation is None:
        return _result(
            request, admin_service.OUTCOME_VPN_ERROR, detail="unknown_operation"
        )

    missing = _missing(request)
    if missing:
        # Refused rather than repaired. A model that omitted the amount has not
        # asked for a balance change, and filling in a zero for it would be the
        # "silently guess" failure the brief names.
        return _result(
            request, admin_service.OUTCOME_MALFORMED, detail=missing
        )

    if not operation.needs_confirmation:
        return await _execute(request)

    return _record_pending(request, operation)


def _record_pending(request, operation: VpnOperation) -> admin_service.AdminResult:
    """Write the operation down and ask the owner. Executes nothing."""
    pending_id = str(getattr(request, "request_id", "") or "") or admin_service.new_request_id()
    now = int(time.time())
    expires_at = now + max(60, int(config.VPN_CONFIRMATION_TTL_SECONDS))
    subject = _subject(request)

    if not db.vpn_pending_add(
        pending_id,
        actor_id=int(request.actor_id),
        chat_id=int(request.chat_id),
        operation=operation.name,
        subject=subject,
        payload=json.dumps(_payload(request), separators=(",", ":"), default=str),
        expires_at=expires_at,
        now=now,
    ):
        # The id was taken. Reporting a question about an operation that was
        # never recorded would leave the owner approving nothing.
        return _result(
            request, admin_service.OUTCOME_VPN_ERROR, detail="could_not_record"
        )

    log.info(
        "vpn operation %s recorded as pending %s for actor %s",
        operation.name,
        pending_id,
        request.actor_id,
    )
    _maybe_prune()
    return _result(
        request,
        admin_service.OUTCOME_VPN_AWAITING_CONFIRMATION,
        detail=pending_id,
        message=config.VPN_CONFIRM_REQUIRED_TEXT.format(
            operation=operation.label, subject=subject
        ),
        extra={
            "pending": {
                "pending_id": pending_id,
                "operation": operation.name,
                "operation_label": operation.label,
                "subject": subject,
                "expires_at": expires_at,
            }
        },
    )


def _expired(request) -> bool:
    """Whether the reference this request named is a row that timed out.

    Only a row that is *still pending* and past its deadline counts. A row whose
    status is ``confirmed`` or ``done`` is a different thing entirely — it ran —
    and calling that "expired" would tell the owner their operation was dropped
    when in fact it went through.
    """
    named = str(getattr(request, "pending_id", "") or "")
    if not named:
        return False
    row = db.vpn_pending_get(named)
    if not row or row.get("status") != "pending":
        return False
    return int(row.get("expires_at") or 0) <= int(time.time())


async def _confirm(request) -> admin_service.AdminResult:
    """Release one recorded operation the owner has approved.

    Four refusals before anything runs, and each one is a different next step
    for the owner: this is not yours to confirm, nothing is waiting, that
    reference is not waiting (the candidates come back so the next message can
    name one), or the reference is ambiguous because more than one thing is.

    The claim is a compare-and-swap on one row, so two confirmations arriving
    together cannot both execute. It is taken *before* the call and released
    again only when the failure was a transport one — a refusal from the VPN bot
    is a decision, and re-asking would produce the same answer.
    """
    is_owner = rbac.is_owner(int(getattr(request, "actor_id", 0) or 0))
    waiting = db.vpn_pending_waiting(chat_id=int(getattr(request, "chat_id", 0) or 0))
    decision = agent_bridge.resolve_confirmation(
        actor_id=int(getattr(request, "actor_id", 0) or 0),
        is_owner=is_owner,
        named_request_id=str(getattr(request, "pending_id", "") or ""),
        waiting=waiting,
    )
    candidates = ", ".join(decision.candidates)

    if decision.answer is agent_bridge.Confirm.NOT_OWNER:
        return _result(
            request,
            admin_service.OUTCOME_DENIED,
            detail=rbac.REASON_NOT_ADMIN,
            reason=rbac.REASON_NOT_ADMIN,
            message=config.VPN_CONFIRM_OWNER_ONLY_TEXT,
        )
    if decision.answer is agent_bridge.Confirm.NOTHING_PENDING:
        # Nothing live is waiting. That is *true* and it is also what an
        # operation whose deadline passed looks like from here — so when the
        # caller named a reference, the row is looked up to tell the two apart.
        # The lookup happens after the resolver, never before it: the owner rule
        # lives in ``resolve_confirmation`` and this must not become a second
        # place where it is written. Reaching this line means the owner check
        # already passed.
        if _expired(request):
            return _result(
                request,
                admin_service.OUTCOME_VPN_REFUSED,
                detail="expired",
                message=config.VPN_CONFIRM_EXPIRED_TEXT,
            )
        return _result(
            request,
            admin_service.OUTCOME_VPN_REFUSED,
            detail="nothing_pending",
            message=config.VPN_CONFIRM_NOTHING_TEXT,
        )
    if decision.answer is agent_bridge.Confirm.NOT_WAITING:
        # A reference that matched nothing live. Same distinction, same reason:
        # "the deadline passed, ask again" is a next step, while "that is not
        # waiting" reads as "you are mistaken about what you asked for".
        if _expired(request):
            return _result(
                request,
                admin_service.OUTCOME_VPN_REFUSED,
                detail="expired",
                message=config.VPN_CONFIRM_EXPIRED_TEXT,
            )
        return _result(
            request,
            admin_service.OUTCOME_VPN_REFUSED,
            detail=f"not_waiting {candidates}".strip(),
            message=config.VPN_CONFIRM_NOT_WAITING_TEXT
            + (f"\n{candidates}" if candidates else ""),
        )
    if decision.answer is agent_bridge.Confirm.AMBIGUOUS:
        # More than one operation is waiting, so a bare «اوکی» is a question
        # rather than an approval. The ids go back so the next message can name
        # one — and the model is told never to pick.
        return _result(
            request,
            admin_service.OUTCOME_VPN_REFUSED,
            detail="ambiguous",
            message=config.VPN_CONFIRM_AMBIGUOUS_TEXT + "\n" + candidates,
            extra={"candidates": list(decision.candidates)},
        )

    row = db.vpn_pending_get(decision.request_id)
    if not row:
        return _result(
            request,
            admin_service.OUTCOME_VPN_REFUSED,
            detail="unknown_pending",
            message=config.VPN_CONFIRM_NOT_WAITING_TEXT,
        )
    if not db.vpn_pending_claim(decision.request_id):
        # Lost the compare-and-swap: another confirmation took it between the
        # read above and this line, or the deadline passed in the same window.
        # Either way the answer is the same and so is the next step — nothing
        # ran, and the owner can look again.
        return _result(
            request,
            admin_service.OUTCOME_VPN_REFUSED,
            detail="already_claimed",
            message=config.VPN_CONFIRM_NOT_WAITING_TEXT,
        )

    result = await _execute(_request_from_row(row, request))
    if result.outcome == admin_service.OUTCOME_VPN_UNAVAILABLE:
        # Nothing was decided, so the operation is put back and can be confirmed
        # again once the integration is reachable.
        db.vpn_pending_release(decision.request_id)
    else:
        db.vpn_pending_finish(
            decision.request_id, outcome=result.outcome, detail=result.detail
        )
    log.info(
        "vpn pending %s (%s) confirmed by %s -> %s",
        decision.request_id,
        row.get("operation"),
        request.actor_id,
        result.outcome,
    )
    # The inner operation's name, not "vpn_confirm": the answer to "what
    # happened" is the operation that ran. The audit row is written by the
    # gateway under the confirming request, so both facts are on record.
    return admin_service.AdminResult(
        ok=result.ok,
        operation=result.operation,
        outcome=result.outcome,
        detail=result.detail,
        actor_id=result.actor_id,
        target_id=result.target_id,
        chat_id=result.chat_id,
        request_id=result.request_id,
        message=result.message,
        extra={
            **result.extra,
            "confirmed": {
                "pending_id": decision.request_id,
                "operation": str(row.get("operation") or ""),
            },
        },
    )


# ── Reporting ─────────────────────────────────────────────────────────────
def pending_lines(*, chat_id: int = 0, limit: int = 5) -> list[str]:
    """Recorded operations still awaiting the owner. Ids and labels only.

    Read by ``get_vpn_status``, so the assistant can answer "what is waiting for
    me?" from the server's record rather than from the conversation. No payload:
    it holds a balance delta and the owner's own free-text reason, both of which
    are already in the conversation.
    """
    rows = db.vpn_pending_waiting(chat_id=int(chat_id or 0))[: max(1, int(limit))]
    return [
        f"{row['request_id']} {row['operation']} {row.get('subject', '')}"
        for row in rows
    ]


# ── Retention ─────────────────────────────────────────────────────────────
def _maybe_prune() -> None:
    global _since_prune
    _since_prune += 1
    if _since_prune < PRUNE_EVERY:
        return
    _since_prune = 0
    prune()


def prune() -> int:
    """Apply the retention window to recorded operations. Best effort.

    Called from the operation path rather than from a timer, for the same reason
    ``admin_service.prune`` is: this process has no scheduler, and a retention
    rule that only runs when somebody remembers is not a retention rule.
    """
    try:
        return db.vpn_pending_prune(
            max(1, int(config.VPN_PENDING_RETENTION_SECONDS))
        )
    except Exception:  # noqa: BLE001 - retention is never worth a crash
        log.exception("vpn pending retention prune failed")
        return 0


def prune_reset() -> None:
    """Forget the prune counter. For tests."""
    global _since_prune
    _since_prune = 0


def reset_state() -> None:
    """Reset the module's own state, for the test-reset convention."""
    prune_reset()
    return None
