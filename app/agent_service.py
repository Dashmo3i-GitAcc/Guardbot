"""The container's half of the bridge: decide, record, publish, confirm.

The shape of a coding request, end to end::

    owner says «این باگ رو درست کن» in the group
      → Nexus calls the ``codebuddy_task`` tool
      → app/admin_service.py authorises it against the owner's real Telegram id
      → submit() below validates the envelope and writes a row
      → the row is published into the spool for the host runner
      → app/agent_poller.py delivers what comes back

Everything in that chain that is a *decision* is in
:mod:`app/agent_bridge` (pure, testable without Telegram) or in
:mod:`app/admin_service` (the one authority model). This module is the part that
has to talk to both, so it is deliberately thin and deliberately boring: it
resolves nothing about the request that it has not been handed, and it never
reads a field the model could have filled in.

The four things it is responsible for
-------------------------------------
* **Trusted context.** ``is_owner`` comes from :func:`app.rbac.is_owner` on the
  actor id the service authorised, never from the request. The repository comes
  from the allowlist by *name*. The operation comes from a closed table. There is
  no path through this module by which a string the model produced becomes an
  authorisation.
* **Recording before running.** The row is written before the request is
  published, so a restart between the two leaves a task that is *queued and not
  claimed* rather than one that is running and unknown.
* **Confirmation.** A dangerous task is recorded and *not* published. Only
  :func:`confirm`, called with the owner's real id, publishes it — and only when
  :func:`app.agent_bridge.resolve_confirmation` says the confirmation is
  unambiguous.
* **Isolation from the awareness allowance.** Nothing here imports
  ``app/awareness.py`` or ``app/gemini_pool.py``. A coding task costs the owner's
  CodeBuddy credential and not one unit of this deployment's Gemini budget.
"""
from __future__ import annotations

import logging
import time

from . import admin_service, agent_bridge, agent_spool, config, db, rbac

log = logging.getLogger("guardbot.agent")


# ── Result plumbing ───────────────────────────────────────────────────────
def _result(
    request,
    outcome: str,
    *,
    ok: bool = False,
    detail: str = "",
    message: str = "",
    extra: dict | None = None,
    actor_id: int = 0,
    chat_id: int = 0,
    reason: str = "",
) -> admin_service.AdminResult:
    """One result, built the same way ``admin_service`` builds its own.

    ``message`` is overridden rather than left to ``message_for`` whenever the
    bridge has something more specific to say than the outcome's generic
    sentence — a refused repository names the allowed ones, and a busy bridge
    says which bound was reached.

    ``request`` may be ``None``: :func:`confirm` and :func:`cancel` are reached
    from the assistant's tool loop rather than from an ``AdminRequest``, and the
    actor is passed in explicitly there.
    """
    return admin_service.AdminResult(
        ok=ok,
        operation=str(getattr(request, "operation", "") or "codebuddy_task"),
        outcome=outcome,
        detail=detail,
        reason=reason,
        actor_id=int(actor_id or getattr(request, "actor_id", 0) or 0),
        chat_id=int(chat_id or getattr(request, "chat_id", 0) or 0),
        request_id=str(getattr(request, "request_id", "") or ""),
        message=message or admin_service.message_for(outcome),
        extra=dict(extra or {}),
    )


def _public_row(row: dict) -> dict:
    """The parts of a task row that are safe to hand back to the model.

    No task text and no result: both are long, both are already in the
    conversation, and a tool result that repeats them is a second copy of the
    owner's words in a place he did not put them.
    """
    return {
        "request_id": row.get("request_id", ""),
        "repository": row.get("repository", ""),
        "operation": row.get("operation", ""),
        "status": row.get("status", ""),
        "status_label": agent_bridge.status_label(row.get("status", "")),
        "danger": row.get("danger", ""),
        "waiting_for_confirmation": row.get("status") == "waiting_for_owner",
    }


# ── Publishing ────────────────────────────────────────────────────────────
def publish(row: dict) -> bool:
    """Hand one queued task to the host runner. Returns whether it went out.

    The envelope carries the *resolved path* rather than the logical name, and
    that is not a loosening of the allowlist: the path was produced by
    :func:`app.agent_bridge.repository_path` from the name, the runner checks it
    against its own copy of the same table, and a mismatch is refused there. Two
    independent checks, and the container's is the one with the authority.
    """
    if not row:
        return False
    request_id = str(row.get("request_id") or "")
    payload = {
        "request_id": request_id,
        "actor_id": int(row.get("actor_id") or 0),
        "chat_id": int(row.get("chat_id") or 0),
        "repository": str(row.get("repository") or ""),
        "repo_path": str(row.get("repo_path") or ""),
        "task": str(row.get("task") or ""),
        "operation": str(row.get("operation") or ""),
        "reply_mode": str(row.get("reply_mode") or "text"),
        "danger": str(row.get("danger") or ""),
        "created_at": int(row.get("created_at") or time.time()),
        # Bounds the container sets and the runner honours. Deliberately *not*
        # the executable or its arguments: which binary runs is the host's
        # business, and a container that could name one could name something
        # that is not a coding agent. See ``tools/agent_runner.py``.
        "timeout_seconds": int(config.AGENT_TIMEOUT_SECONDS),
        "max_turns": int(config.AGENT_MAX_TURNS),
        "progress_max_chars": int(config.AGENT_PROGRESS_MAX_CHARS),
        # The prompt is rendered here and not by the runner, so there is exactly
        # one implementation of it and the wording cannot drift between the two
        # halves. The runner is the process with the shell, so it could be
        # forgiven for trusting a prompt it was handed — but it does not have to
        # trust the *repository* or the *operation*, and those are what its own
        # checks are for.
        "prompt": agent_bridge.build_prompt(_envelope_for(row)),
    }
    ok = agent_spool.write_request(request_id, payload)
    if not ok:
        log.warning("could not publish agent task %s to the spool", request_id)
    return ok


def _envelope_for(row: dict) -> agent_bridge.AgentRequest:
    """Rebuild the envelope from a stored row, for rendering the prompt.

    The row is the record of what was validated, so rebuilding from it cannot
    introduce anything that was not checked. A row that is missing a field
    produces an envelope with that field empty, which renders as an empty
    sentence in the prompt rather than as a different task.
    """
    return agent_bridge.AgentRequest(
        request_id=str(row.get("request_id") or ""),
        actor_id=int(row.get("actor_id") or 0),
        chat_id=int(row.get("chat_id") or 0),
        repository=str(row.get("repository") or ""),
        repo_path=str(row.get("repo_path") or ""),
        task=str(row.get("task") or ""),
        operation=str(row.get("operation") or ""),
        reply_mode=str(row.get("reply_mode") or "text"),
        danger=str(row.get("danger") or ""),
        status=str(row.get("status") or "queued"),
    )


def publish_if_queued(request_id: str) -> bool:
    """Publish a task that is queued and has no request file yet.

    Called on startup for every queued row: a task recorded just before a
    restart has a row and no file, and without this it would sit in the queue
    for ever. Publishing is idempotent — the file is rewritten with the same
    content — so doing it twice costs nothing.
    """
    row = db.agent_task_get(request_id)
    if not row or row.get("status") != "queued":
        return False
    return publish(row)


# ── Submitting ────────────────────────────────────────────────────────────
async def submit(request) -> admin_service.AdminResult:
    """Validate and record one coding request. The only entry point.

    Ordered so that the cheapest and most authoritative checks come first, and
    so that nothing is written until the request is known to be acceptable:

    1. **Is the bridge switched on?** A deployment decision, and the first thing
       an operator turns off when they want this to stop.
    2. **Is the actor the owner?** Re-derived here from the id. The permission
       ``agent.request`` already refused anybody else in ``admin_service``, and
       this second check is not redundant — it is the check that holds if the
       permission table is ever edited wrongly, and it is one line.
    3. **Is the request well formed?** Repository on the allowlist, operation in
       the vocabulary, task non-empty. Raises ``Rejected`` with a sentence.
    4. **Does it fit the concurrency bounds?** The same request already active,
       too many tasks, or one already on that repository.
    5. **Record it.** Danger decides the initial state: a dangerous task is
       recorded as ``waiting_for_owner`` and is *not* published, so it cannot
       run until :func:`confirm` says so.
    """
    if not config.AGENT_ENABLED:
        return _result(request, admin_service.OUTCOME_AGENT_DISABLED)

    actor_id = int(getattr(request, "actor_id", 0) or 0)
    if not rbac.is_owner(actor_id):
        # Not "denied": this is the same refusal ``rbac`` would give, repeated
        # where the work would otherwise start. The audit row is written by
        # ``admin_service`` either way, so the attempt is still on record.
        return _result(
            request,
            admin_service.OUTCOME_DENIED,
            detail=rbac.REASON_NOT_ADMIN,
            reason=rbac.REASON_NOT_ADMIN,
        )

    try:
        envelope = agent_bridge.build_request(
            actor_id=actor_id,
            chat_id=int(getattr(request, "chat_id", 0) or 0),
            repository=str(getattr(request, "repository", "") or ""),
            task=str(getattr(request, "task", "") or ""),
            requested_operation=str(getattr(request, "agent_operation", "") or ""),
            reply_mode=str(getattr(request, "reply_mode", "") or ""),
        )
    except agent_bridge.Rejected as exc:
        log.info("agent request rejected: %s", exc.reason)
        return _result(
            request,
            admin_service.OUTCOME_AGENT_REJECTED,
            detail=exc.reason,
            message=exc.message,
        )

    scope = agent_bridge.scope_check(envelope, active=db.agent_task_active())
    if scope == "duplicate":
        existing = _find_duplicate(envelope.request_id)
        return _result(
            request,
            admin_service.OUTCOME_AGENT_DUPLICATE,
            detail=scope,
            extra={"task": _public_row(existing)} if existing else {},
        )
    if scope:
        return _result(request, admin_service.OUTCOME_AGENT_BUSY, detail=scope)

    row = db.agent_task_create(**envelope.as_row())
    if not row:
        # The write failed. Reporting a task that does not exist would be the
        # worst outcome here, so it is reported as a rejection.
        return _result(
            request,
            admin_service.OUTCOME_AGENT_REJECTED,
            detail="could_not_record",
        )

    if envelope.is_dangerous:
        log.info(
            "agent task %s recorded and waiting for the owner (%s)",
            envelope.request_id,
            envelope.danger,
        )
        return _result(
            request,
            admin_service.OUTCOME_AGENT_WAITING,
            detail=envelope.request_id,
            extra={"task": _public_row(row), "danger": envelope.danger},
        )

    publish(row)
    log.info(
        "agent task %s queued repository=%s operation=%s",
        envelope.request_id,
        envelope.repository,
        envelope.operation,
    )
    return _result(
        request,
        admin_service.OUTCOME_OK,
        ok=True,
        detail=envelope.request_id,
        extra={"task": _public_row(row)},
    )


def _find_duplicate(request_id: str) -> dict:
    row = db.agent_task_get(request_id)
    return row or {}


# ── Confirming ────────────────────────────────────────────────────────────
def confirm(
    *,
    actor_id: int,
    chat_id: int = 0,
    request_id: str = "",
    message_id: int = 0,
) -> admin_service.AdminResult:
    """Release a dangerous task the owner has approved. Server-side, entirely.

    The model may call this, and it may name a task, and neither of those is a
    decision: :func:`app.agent_bridge.resolve_confirmation` re-checks that the
    caller is the owner, that something is actually waiting, that a named task is
    one of them, and that a bare confirmation corresponds to exactly one. What
    the model supplies is a *reference*, and a reference is not an approval.
    """
    is_owner = rbac.is_owner(int(actor_id or 0))
    waiting = db.agent_task_waiting()
    decision = agent_bridge.resolve_confirmation(
        actor_id=int(actor_id or 0),
        is_owner=is_owner,
        named_request_id=request_id,
        waiting=waiting,
    )
    detail = ", ".join(decision.candidates)

    if decision.answer is agent_bridge.Confirm.NOT_OWNER:
        return _result(None, admin_service.OUTCOME_DENIED,
                       detail=rbac.REASON_NOT_ADMIN,
                       reason=rbac.REASON_NOT_ADMIN,
                       message=config.AGENT_CONFIRM_OWNER_ONLY_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if decision.answer is agent_bridge.Confirm.NOTHING_PENDING:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="nothing_pending",
                       message=config.AGENT_CONFIRM_NOTHING_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if decision.answer is agent_bridge.Confirm.NOT_WAITING:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail=f"not_waiting {detail}".strip(),
                       message=config.AGENT_CONFIRM_NOT_WAITING_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if decision.answer is agent_bridge.Confirm.AMBIGUOUS:
        # The brief's rule, in one branch: with more than one task waiting, a
        # bare «اوکی» is a question rather than an approval. The ids go back so
        # the next message can name one.
        return _result(
            None,
            admin_service.OUTCOME_AGENT_BUSY,
            detail="ambiguous",
            message=config.AGENT_CONFIRM_AMBIGUOUS_TEXT + "\n" + detail,
            extra={"candidates": list(decision.candidates)},
            actor_id=actor_id, chat_id=chat_id,
        )

    row = db.agent_task_get(decision.request_id)
    if not row:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="unknown_task",
                       message=config.AGENT_CONFIRM_NOT_WAITING_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if not agent_bridge.transition_allowed(row.get("status", ""), "queued"):
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="not_waiting",
                       message=config.AGENT_CONFIRM_NOT_WAITING_TEXT,
                       actor_id=actor_id, chat_id=chat_id)

    now = int(time.time())
    updated = db.agent_task_update(
        decision.request_id,
        status="queued",
        confirmed_by=int(actor_id or 0),
        confirmed_at=now,
    )
    publish(updated or row)
    log.info("agent task %s confirmed by %s", decision.request_id, actor_id)
    return _result(
        None,
        admin_service.OUTCOME_OK,
        ok=True,
        detail=decision.request_id,
        message=config.AGENT_CONFIRMED_TEXT,
        extra={"task": _public_row(updated or row)},
        actor_id=actor_id, chat_id=chat_id,
    )


# ── Answering a question ──────────────────────────────────────────────────
def resume(
    *, actor_id: int, request_id: str, text: str, chat_id: int = 0
) -> admin_service.AdminResult:
    """Give a stopped agent the answer it asked for, and let it carry on.

    A question the owner cannot answer is not a question, so this is what makes
    ``waiting_for_owner`` mean something for a task that *ran* and stopped. It
    is deliberately not the same call as :func:`confirm`, and the difference is
    enforced rather than documented:

    * a task that has never started is refused here, because its
      ``waiting_for_owner`` means "dangerous and unapproved" and an answer to a
      question it never asked is not an approval of it;
    * the answer is appended to the task and the *danger is recomputed*. An
      answer that turns an ordinary task into "yes, deploy it" is therefore
      caught: the task goes back to waiting for an explicit confirmation rather
      than inheriting one from a sentence about something else.
    """
    row = db.agent_task_get(request_id)
    if not row:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="unknown_task", message=config.AGENT_REJECTED_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if row.get("actor_id") != int(actor_id or 0) and not rbac.is_owner(int(actor_id or 0)):
        return _result(None, admin_service.OUTCOME_DENIED,
                       detail=rbac.REASON_NOT_ADMIN,
                       reason=rbac.REASON_NOT_ADMIN,
                       message=config.AGENT_NOT_YOURS_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if row.get("status") != "waiting_for_owner":
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="not_waiting_for_an_answer",
                       message=config.AGENT_CONFIRM_NOT_WAITING_TEXT,
                       actor_id=actor_id, chat_id=chat_id)
    if not int(row.get("started_at") or 0):
        # An unapproved dangerous task. Answering it is not approving it.
        return _result(None, admin_service.OUTCOME_AGENT_WAITING,
                       detail=request_id,
                       message=config.AGENT_WAITING_TEXT,
                       actor_id=actor_id, chat_id=chat_id,
                       extra={"task": _public_row(row)})

    body = (text or "").strip()
    if not body:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="empty_answer", message=config.AGENT_REJECTED_TEXT,
                       actor_id=actor_id, chat_id=chat_id)

    enriched = (str(row.get("task") or "") + "\n\n" + body)[
        : int(db.AGENT_TASK_MAX_CHARS)
    ]
    danger = agent_bridge.danger_for(str(row.get("operation") or ""), enriched)
    if danger and not int(row.get("confirmed_by") or 0):
        # The answer made it dangerous. Recorded, and waiting — not started.
        db.agent_task_update(request_id, task=enriched, danger=danger,
                             status="waiting_for_owner")
        return _result(None, admin_service.OUTCOME_AGENT_WAITING,
                       detail=request_id,
                       message=config.AGENT_WAITING_TEXT,
                       actor_id=actor_id, chat_id=chat_id,
                       extra={"task": _public_row(db.agent_task_get(request_id) or row),
                              "danger": danger})

    updated = db.agent_task_update(
        request_id, task=enriched, status="queued", danger=danger
    )
    publish(updated or row)
    log.info("agent task %s resumed with an answer from %s", request_id, actor_id)
    return _result(None, admin_service.OUTCOME_OK, ok=True, detail=request_id,
                   message=config.AGENT_RESUMED_TEXT,
                   actor_id=actor_id, chat_id=chat_id,
                   extra={"task": _public_row(updated or row)})


# ── Cancelling ────────────────────────────────────────────────────────────
def cancel(*, actor_id: int, request_id: str) -> admin_service.AdminResult:
    """Stop a task. The owner may stop any of them; an actor may stop their own.

    The cancel file is what stops a run that is already in flight — the runner
    checks it between turns — and the row is what stops it being picked up. Both
    are needed: the row alone would leave a running process going until it
    finished, and the file alone would let a queued task start anyway.
    """
    row = db.agent_task_get(request_id)
    if not row:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="unknown_task",
                       message=config.AGENT_REJECTED_TEXT,
                       actor_id=actor_id)
    if row.get("actor_id") != int(actor_id or 0) and not rbac.is_owner(int(actor_id or 0)):
        return _result(None, admin_service.OUTCOME_DENIED,
                       detail=rbac.REASON_NOT_ADMIN,
                       reason=rbac.REASON_NOT_ADMIN,
                       message=config.AGENT_NOT_YOURS_TEXT,
                       actor_id=actor_id)
    if row.get("status") not in db.AGENT_ACTIVE_STATUSES:
        return _result(None, admin_service.OUTCOME_AGENT_REJECTED,
                       detail="already_finished",
                       message=config.AGENT_REJECTED_TEXT,
                       actor_id=actor_id)

    agent_spool.request_cancel(request_id)
    db.agent_task_update(
        request_id, status="cancelled", finished_at=int(time.time())
    )
    log.info("agent task %s cancelled by %s", request_id, actor_id)
    return _result(None, admin_service.OUTCOME_OK, ok=True,
                   detail=request_id, message=config.AGENT_CANCELLED_TEXT,
                   actor_id=actor_id)


# ── Status ────────────────────────────────────────────────────────────────
def status_text(*, actor_id: int, limit: int = 8) -> str:
    """The owner's view of the bridge. Owner-only, and never a task body.

    ``/agent`` is the typed fallback for exactly the situation the bridge is
    for: the owner wants to know whether his request went anywhere, and asking
    the assistant "did you do it" is the question this answers without a model
    in the loop.
    """
    if not rbac.is_owner(int(actor_id or 0)):
        return config.AGENT_CONFIRM_OWNER_ONLY_TEXT

    lines = [
        "وضعیت پل عامل برنامه‌نویسی:",
        f"- فعال: {'بله' if config.AGENT_ENABLED else 'خیر'}",
        f"- مخزن‌های مجاز: {', '.join(agent_bridge.repository_names()) or '—'}",
        f"- سقف هم‌زمان: {int(config.AGENT_MAX_ACTIVE)} "
        f"(روی هر مخزن {int(config.AGENT_MAX_PER_REPOSITORY)})",
        f"- عامل: {config.AGENT_CLI or '—'}",
    ]
    if not config.AGENT_ENABLED:
        lines.append("")
        lines.append("پل خاموشه، پس هیچ درخواستی اجرا نمی‌شه.")
        return "\n".join(lines)

    lines.append("")
    lines.extend(agent_bridge.status_lines(actor_id=0, limit=limit))
    waiting = db.agent_task_waiting()
    if waiting:
        lines.append("")
        lines.append("منتظر تأیید تو:")
        for row in waiting[:limit]:
            lines.append(
                f"- {row['request_id']} | {row['repository']} | {row['danger']}"
            )
        lines.append("برای تأیید: «تأییدش کن» یا /agent confirm <id>")
    return "\n".join(lines)


def queue_lines(*, limit: int = 8) -> list[str]:
    """Active tasks, for the startup log. Ids and states only."""
    return [
        f"{row['request_id']} {row['repository']} {row['status']}"
        for row in db.agent_task_active()[:limit]
    ]


def recover() -> int:
    """Publish everything queued that has no request file. Returns how many.

    Called once at startup. The case it exists for is a restart between the
    database write and the spool write, which would otherwise strand a task in
    ``queued`` for ever; the case it must not create is a *second* run of
    something already running, which is why it only ever touches rows whose
    status is exactly ``queued``.
    """
    published = 0
    for row in db.agent_task_active():
        if row.get("status") != "queued":
            continue
        request_id = str(row.get("request_id") or "")
        if agent_spool.read_request(request_id):
            continue
        if publish(row):
            published += 1
    if published:
        log.info("republished %d queued agent task(s) after startup", published)
    return published


def prune() -> int:
    """Apply the retention window to finished tasks, files and locks.

    Called from the poller rather than from a timer, for the same reason the
    audit prune is called from the administrative path: a retention rule that
    only runs when somebody remembers is not a retention rule.
    """
    removed = 0
    try:
        rows = db.agent_task_recent(limit=200)
        cutoff = int(time.time()) - max(0, int(config.AGENT_RETENTION_SECONDS))
        for row in rows:
            if row.get("status") in db.AGENT_ACTIVE_STATUSES:
                continue
            if int(row.get("finished_at") or row.get("updated_at") or 0) > cutoff:
                continue
            agent_spool.forget(str(row.get("request_id") or ""))
            removed += 1
        db.agent_task_prune(int(config.AGENT_RETENTION_SECONDS))
    except Exception:  # noqa: BLE001 - retention is never worth a crash
        log.exception("agent retention prune failed")
    return removed


def reset_state() -> None:
    """No module-level mutable state; kept for the test-reset convention."""
    return None
