"""Carrying the runner's work back to the owner, in order, once each.

The direction that is easy to get wrong
---------------------------------------
Asking for a coding task is one message and a database write. Everything after
that is a stream: the agent starts, says what it is doing, maybe stops to ask a
question, and finishes with an answer that may be far too long for a Telegram
message. This module is the only thing that reads that stream, and it has four
obligations that are worth stating before the code:

* **Nothing is delivered twice.** The stream is append-only and the database
  records how many of its lines have already been delivered. A restart resumes
  from that line rather than from the beginning, which is what stops the owner
  receiving a transcript he has already read — the failure the brief names
  explicitly.
* **Nothing is dropped.** A long answer is split into ordered chunks, or sent as
  a document, or both. There is deliberately no fourth branch, and the plan for
  which is used comes from :func:`app.agent_bridge.reply_plan`, so "we decided
  not to send it" is not a decision this module is able to make.
* **Order is preserved.** Lines are delivered in the order they were written and
  the offset only ever moves forward. A progress line cannot overtake the answer
  that followed it.
* **A task that stops is a task that stops.** A run that ends without a result —
  killed, timed out, or cancelled — is reported as such rather than left in
  ``running`` for ever, and the timeout is enforced here because this is the side
  that has a clock.

What it never does: it never runs anything, never writes to the repository, and
never decides whether a task *may* run. It reads files the runner wrote and
sends what they say.
"""
from __future__ import annotations

import io
import logging
import time

from telegram.constants import ChatAction

from . import agent_bridge, agent_service, agent_spool, config, db

log = logging.getLogger("guardbot.agent")

# Progress throttling, in memory. Deliberately not persisted: the counter is
# about how chatty this bot is being right now, and a restart is a perfectly
# good moment to allow one more line. The *delivery* offset is persisted, so
# nothing is lost or repeated by this being volatile.
_last_progress_at: dict[str, float] = {}
_progress_sent: dict[str, int] = {}

# The one message a running task narrates itself in, and the text currently on
# it. Both are in memory for the same reason as the throttle: they describe how
# this process is presenting a task, not what the task has done. A restart
# simply starts a fresh working message, and the offset guarantees nothing is
# delivered twice because of it.
_working_message: dict[str, int] = {}
_working_text: dict[str, str] = {}
_last_prune_at: float = 0.0

PRUNE_INTERVAL_SECONDS = 3600.0


def reset_state() -> None:
    """Drop the throttle counters. For the tests, and for a clean restart."""
    global _last_prune_at
    _last_progress_at.clear()
    _progress_sent.clear()
    _working_message.clear()
    _working_text.clear()
    _last_prune_at = 0.0


# ── The tick ──────────────────────────────────────────────────────────────
async def tick(ctx) -> None:
    """One pass. Called on a timer by ``app/main.py``.

    Never raises: a poller that dies takes the whole bridge with it, and every
    failure inside is about one task rather than about the tick.
    """
    if not config.AGENT_ENABLED:
        return
    try:
        agent_spool.ensure()
        now = time.time()
        for row in db.agent_task_active():
            try:
                await _service(ctx, row, now)
            except Exception:  # noqa: BLE001 - one task must not stop the rest
                log.exception(
                    "agent poll failed for %s", row.get("request_id", "")
                )
        _maybe_prune(now)
    except Exception:  # noqa: BLE001
        log.exception("agent poll tick failed")


async def _service(ctx, row: dict, now: float) -> None:
    """Bring one task up to date: deliver what is new, then check the clock."""
    request_id = str(row.get("request_id") or "")
    if not request_id:
        return

    # A dangerous task nobody has approved yet has never been published, so
    # there is no stream to read and no clock running. Skipping it is not an
    # optimisation: it is what keeps "waiting for the owner" from being reported
    # as "running and silent".
    if row.get("status") == "waiting_for_owner" and not row.get("started_at"):
        return

    records, next_offset = agent_spool.read_from(
        request_id, int(row.get("progress_offset") or 0)
    )
    if records:
        await _deliver(ctx, row, records, next_offset)
        row = db.agent_task_get(request_id) or row

    if row.get("status") in db.AGENT_ACTIVE_STATUSES:
        await _enforce_timeout(ctx, row, now)


# ── Delivery ──────────────────────────────────────────────────────────────
async def _deliver(
    ctx, row: dict, records: list[dict], next_offset: int
) -> None:
    """Deliver a run of new lines, in order, and advance the offset.

    The offset is written *after* the lines are sent, not before. That ordering
    means a crash mid-delivery repeats at most the messages in flight, while the
    opposite ordering would lose them — and repeating a progress line is a
    smaller fault than losing an answer.
    """
    request_id = str(row.get("request_id") or "")

    for record in records:
        kind = str(record.get("kind") or "")
        text = str(record.get("text") or "")
        # Re-read before every line, and the reason is a bug this code had: a
        # batch commonly contains ``started`` and then ``result``, and a handler
        # that read the status from the dict passed into the batch would see
        # ``queued`` while deciding whether ``succeeded`` was a legal move — and
        # refuse the result of a task that had, a moment earlier, legally
        # started. The transition table is the right check; the input to it has
        # to be current.
        current = db.agent_task_get(request_id) or row
        if kind == agent_spool.KIND_STARTED:
            await _on_started(ctx, current, record)
        elif kind == agent_spool.KIND_PROGRESS:
            await _on_progress(ctx, current, text)
        elif kind == agent_spool.KIND_QUESTION:
            await _on_question(ctx, current, text)
        elif kind == agent_spool.KIND_RESULT:
            await _on_result(ctx, current, text)
        elif kind == agent_spool.KIND_ERROR:
            await _on_error(ctx, current, text)
        elif kind == agent_spool.KIND_CANCELLED:
            await _on_cancelled(ctx, current, text)
        else:  # pragma: no cover - KINDS and this chain move together
            log.info("ignoring unknown agent line kind %r", kind)

    db.agent_task_update(request_id, progress_offset=int(next_offset))


async def _on_started(ctx, row: dict, record: dict) -> None:
    """The runner has claimed the task. Move it to ``running``.

    The transition is checked rather than assumed: a ``started`` line for a task
    the database already considers finished is a stale stream, and applying it
    would resurrect a task that had been cancelled — which is exactly the race
    the brief asks to be made impossible.
    """
    request_id = str(row.get("request_id") or "")
    current = str(row.get("status") or "")
    if not agent_bridge.transition_allowed(current, "running"):
        log.info(
            "ignoring a late start for %s (status=%s)", request_id, current
        )
        return
    session_id = str(record.get("session_id") or "")
    db.agent_task_update(
        request_id,
        status="running",
        started_at=int(record.get("at") or time.time()),
        session_id=session_id,
    )
    header = config.AGENT_PROGRESS_HEADER.format(
        request_id=request_id,
        repository=row.get("repository", ""),
        status=agent_bridge.status_label("running"),
    )
    if config.AGENT_WORKING_MESSAGE:
        # Open the one message this task will narrate itself in. Progress lines
        # rewrite it instead of arriving as a message each.
        sent = await _send(ctx, row, header)
        if sent:
            _working_message[request_id] = sent
            _working_text[request_id] = agent_bridge.redact(header)
        return
    await _say(ctx, row, header)


async def _on_progress(ctx, row: dict, text: str) -> None:
    """A line of narration. Throttled, because a chatty agent is not a feature."""
    request_id = str(row.get("request_id") or "")
    body = (text or "").strip()
    if not body:
        return
    now = time.time()
    sent = _progress_sent.get(request_id, 0)
    if sent >= max(0, int(config.AGENT_PROGRESS_MAX_MESSAGES)):
        return
    last = _last_progress_at.get(request_id, 0.0)
    if last and (now - last) < float(config.AGENT_PROGRESS_MIN_INTERVAL_SECONDS):
        # Dropped, not queued. A progress line that arrives late is noise, and
        # the answer at the end is what the owner is waiting for.
        return
    _last_progress_at[request_id] = now
    _progress_sent[request_id] = sent + 1
    cap = max(80, int(config.AGENT_PROGRESS_MAX_CHARS))
    if len(body) > cap:
        body = body[:cap] + "…"
    header = config.AGENT_PROGRESS_HEADER.format(
        request_id=request_id,
        repository=row.get("repository", ""),
        status=agent_bridge.status_label("running"),
    )
    if config.AGENT_WORKING_MESSAGE:
        # The header was sent once when the task started, so this is a rewrite
        # of that message: a count and how long the task has been going, then
        # the newest line. Earlier lines are overwritten rather than lost from
        # the record — the record is the database, and the answer is what the
        # owner is waiting for.
        line = (
            f"{header}\n"
            f"… پیشرفت {_progress_sent[request_id]} · "
            f"{_elapsed(row.get('started_at'), now)}\n"
            f"• {body}"
        )
        await _working_update(ctx, row, line)
        return
    await _say(ctx, row, header + "\n" + body)


async def _on_question(ctx, row: dict, text: str) -> None:
    """The agent stopped to ask the owner something.

    The task goes to ``waiting_for_owner`` with a ``started_at``, which is what
    distinguishes it from a dangerous task awaiting approval — see
    ``db.agent_task_waiting``. Confirming this one would re-run work that had
    already begun, so it is deliberately not in that list.
    """
    request_id = str(row.get("request_id") or "")
    body = (text or "").strip() or agent_bridge.question_in(
        agent_spool.result_text(request_id)
    )
    if not body:
        return
    current = str(row.get("status") or "")
    if agent_bridge.transition_allowed(current, "waiting_for_owner"):
        db.agent_task_update(request_id, status="waiting_for_owner")
    await _say(
        ctx,
        row,
        config.AGENT_QUESTION_HEADER.format(
            request_id=request_id, repository=row.get("repository", "")
        )
        + "\n"
        + body,
    )


async def _on_result(ctx, row: dict, text: str) -> None:
    """The agent finished. Deliver the answer, however long it is."""
    request_id = str(row.get("request_id") or "")
    current = str(row.get("status") or "")
    if not agent_bridge.transition_allowed(current, "succeeded"):
        # Already cancelled, or already reported. A second result line is the
        # runner's, not the owner's, and delivering it would double an answer.
        log.info("ignoring a result for %s (status=%s)", request_id, current)
        return

    body = agent_bridge.redact(text or "")
    plan = agent_bridge.reply_plan(body, row.get("reply_mode") or "text")
    _forget_working(request_id)

    if plan["document"]:
        await _send_document(ctx, row, body)
    total = len(plan["chunks"])
    for index, chunk in enumerate(plan["chunks"], start=1):
        await _say(
            ctx,
            row,
            _chunk_header(row, index, total) + "\n" + agent_bridge.redact(chunk),
        )
    if not plan["chunks"] and not plan["document"]:
        # An empty answer is still an answer, and saying so is better than
        # silence: the owner asked for work and is owed a report.
        await _say(
            ctx,
            row,
            _chunk_header(row, 1, 1) + "\n" + "عامل چیزی برای گزارش برنگرداند.",
        )

    db.agent_task_update(
        request_id,
        status="succeeded",
        result=body[: int(db.AGENT_RESULT_MAX_CHARS)],
        finished_at=int(time.time()),
    )
    agent_spool.release(request_id)
    log.info("agent task %s succeeded (%d chars)", request_id, len(body))


async def _on_error(ctx, row: dict, text: str) -> None:
    request_id = str(row.get("request_id") or "")
    current = str(row.get("status") or "")
    if not agent_bridge.transition_allowed(current, "failed"):
        log.info("ignoring an error for %s (status=%s)", request_id, current)
        return
    body = agent_bridge.redact((text or "").strip())[: int(config.AGENT_PROGRESS_MAX_CHARS)]
    db.agent_task_update(
        request_id,
        status="failed",
        error=body[: int(db.AGENT_ERROR_MAX_CHARS)],
        finished_at=int(time.time()),
    )
    agent_spool.release(request_id)
    _forget_working(request_id)
    await _say(
        ctx,
        row,
        config.AGENT_FAILED_HEADER.format(
            request_id=request_id, repository=row.get("repository", "")
        )
        + ("\n" + body if body else ""),
    )
    log.info("agent task %s failed", request_id)


async def _on_cancelled(ctx, row: dict, text: str) -> None:
    request_id = str(row.get("request_id") or "")
    current = str(row.get("status") or "")
    if not agent_bridge.transition_allowed(current, "cancelled"):
        return
    db.agent_task_update(
        request_id, status="cancelled", finished_at=int(time.time())
    )
    agent_spool.release(request_id)
    _forget_working(request_id)
    await _say(
        ctx,
        row,
        config.AGENT_CANCELLED_TEXT
        + f"\n{request_id} — {row.get('repository', '')}"
        + (("\n" + agent_bridge.redact(text)) if (text or "").strip() else ""),
    )


# ── The clock ─────────────────────────────────────────────────────────────
async def _enforce_timeout(ctx, row: dict, now: float) -> None:
    """Stop a task that has outlived its bound.

    Two bounds, and they are about different waits. A task that was *published*
    and never claimed is waiting on the host — the runner may be down, and the
    owner should hear that rather than watch a task sit in ``queued`` for ever.
    A task that was claimed and never finished is waiting on the agent, and is
    stopped with a cancel request so the host is not left running it.
    """
    request_id = str(row.get("request_id") or "")
    limit = max(60, int(config.AGENT_TIMEOUT_SECONDS))
    started = int(row.get("started_at") or 0)
    created = int(row.get("created_at") or 0)
    reference = started or created
    if not reference or (now - reference) <= limit:
        return

    if started:
        # Ask the runner to stop, then report. The status is set here rather
        # than waited for, because the runner may be wedged — which is the case
        # this branch exists for — and a task that cannot be reported as timed
        # out is a task that can never be retried.
        agent_spool.request_cancel(request_id)
        body = "از زمان مجاز بیشتر طول کشید و متوقف شد."
    else:
        body = (
            "عامل روی این میزبان این کار را برنداشت. "
            "ممکن است اجراکننده بالا نباشد."
        )

    db.agent_task_update(
        request_id,
        status="timed_out",
        error=body,
        finished_at=int(time.time()),
    )
    agent_spool.release(request_id)
    _forget_working(request_id)
    await _say(
        ctx,
        row,
        config.AGENT_TIMEOUT_HEADER.format(
            request_id=request_id, repository=row.get("repository", "")
        )
        + "\n"
        + body,
    )
    log.warning("agent task %s timed out after %.0fs", request_id, now - reference)


def _maybe_prune(now: float) -> None:
    """Run the retention prune at most hourly."""
    global _last_prune_at
    if _last_prune_at and (now - _last_prune_at) < PRUNE_INTERVAL_SECONDS:
        return
    _last_prune_at = now
    agent_service.prune()


# ── Sending ───────────────────────────────────────────────────────────────
def _chunk_header(row: dict, index: int, total: int) -> str:
    """The header for one part of an answer.

    The first part carries the result header, because that is the sentence that
    says the work finished. Later parts carry the continuation header with their
    part number, so a three-part answer reads as one answer rather than three
    identical "done" messages. A single-part answer is not numbered — there is
    nothing to distinguish it from.
    """
    request_id = str(row.get("request_id") or "")
    repository = row.get("repository", "")
    if index <= 1:
        head = config.AGENT_RESULT_HEADER.format(
            request_id=request_id, repository=repository
        )
        return head + (f" ({index}/{total})" if total > 1 else "")
    return config.AGENT_CONTINUATION_HEADER.format(
        request_id=request_id, repository=repository, part=index, total=total
    )


def _elapsed(started_at, now: float) -> str:
    """How long a task has been running, in the shortest honest form."""
    try:
        seconds = int(now - float(started_at or 0))
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _forget_working(request_id: str) -> None:
    """Stop treating a task's message as editable. Called when it ends."""
    _working_message.pop(request_id, None)
    _working_text.pop(request_id, None)


async def _typing(ctx, chat_id: int) -> None:
    """Show the "typing" indicator. Best effort; never worth an exception."""
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as exc:  # noqa: BLE001 - an indicator is not a delivery
        log.debug("could not send the agent typing action: %s", exc)


async def _working_update(ctx, row: dict, text: str) -> None:
    """Rewrite the task's single working message, or open one if there is none.

    The edit is the point: a task that runs for minutes should be one calm
    message that changes, not twenty that arrive. Two failures are expected and
    handled rather than raised — there may be no message to edit (a restart
    dropped the id, or the working message is switched off), and Telegram may
    refuse the edit (the message was deleted, or it is older than the edit
    window). Either one falls back to sending a new message, because silence
    would be worse than a second message.

    Identical text is skipped: Telegram rejects an edit that changes nothing,
    and that is a refusal with nothing behind it to report.
    """
    request_id = str(row.get("request_id") or "")
    chat_id = int(row.get("chat_id") or 0)
    if not chat_id or ctx is None:
        return
    body = agent_bridge.redact(text or "")
    if not body.strip():
        return
    await _typing(ctx, chat_id)
    if _working_text.get(request_id) == body:
        return
    message_id = _working_message.get(request_id, 0)
    if message_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=body[:4000]
            )
            _working_text[request_id] = body
            return
        except Exception as exc:  # noqa: BLE001 - fall back to a new message
            log.info(
                "could not edit the working message for %s (%s); sending a new one",
                request_id,
                exc,
            )
    sent = await _send(ctx, row, body)
    if sent:
        _working_message[request_id] = sent
        _working_text[request_id] = body


async def _say(ctx, row: dict, text: str) -> None:
    """Send one message to the chat that asked. Never raises.

    A send failure is logged and swallowed: the task's state is already in the
    database, so an owner who did not receive a progress line can still ask
    ``/agent`` and get the truth. Raising here would instead abort the delivery
    loop and lose the rest of the transcript.
    """
    await _send(ctx, row, text)


async def _send(ctx, row: dict, text: str) -> int:
    """Send one message and return its id, or 0 when it could not be sent."""
    chat_id = int(row.get("chat_id") or 0)
    if not chat_id or ctx is None:
        return 0
    body = agent_bridge.redact(text or "")
    if not body.strip():
        return 0
    try:
        message = await ctx.bot.send_message(chat_id=chat_id, text=body[:4000])
    except Exception as exc:  # noqa: BLE001 - delivery is best effort
        log.warning("could not send agent message to %s: %s", chat_id, exc)
        return 0
    return int(getattr(message, "message_id", 0) or 0)


async def _send_document(ctx, row: dict, text: str) -> None:
    """Send the whole answer as a file, in addition to or instead of chunks.

    The brief's rule for a long response is "chunk it in order, or send it as a
    document, or both — never silently discard it". This is the document half;
    it is called for the ``document`` and ``both`` reply modes, and a failure
    here falls back to chunks rather than losing the text.
    """
    chat_id = int(row.get("chat_id") or 0)
    if not chat_id or ctx is None:
        return
    request_id = str(row.get("request_id") or "")
    name = config.AGENT_DOCUMENT_NAME.format(request_id=request_id)
    try:
        await ctx.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(agent_bridge.redact(text).encode("utf-8")),
            filename=name,
            caption=config.AGENT_RESULT_HEADER.format(
                request_id=request_id, repository=row.get("repository", "")
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001 - fall back to chat, never drop
        log.warning("could not send the agent document to %s: %s", chat_id, exc)

    chunks = agent_bridge.chunk_text(agent_bridge.redact(text))
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        await _say(ctx, row, _chunk_header(row, index, total) + "\n" + chunk)


# ── Startup ───────────────────────────────────────────────────────────────
def recover() -> int:
    """Reconcile the spool with the database once, at startup.

    Three cases, and each is one the brief names:

    * a task recorded as ``queued`` with no request file — the process died
      between the two writes — is republished, so it is not stranded;
    * a task whose stream already ended is brought to its terminal state, so a
      result that arrived while the bot was down is not lost;
    * a task whose runner is gone (a lock with no stream, or a stream with no
      lock and no end) is left alone, because the poller's timeout is what
      decides it and deciding here would race a runner that is starting up.

    Returns how many queued tasks were republished.
    """
    published = agent_service.recover()
    for row in db.agent_task_active():
        request_id = str(row.get("request_id") or "")
        if not request_id:
            continue
        kind = agent_spool.last_kind(request_id)
        if kind not in agent_spool.TERMINAL_KINDS:
            continue
        # The stream ended while the bot was down. Apply the ending without
        # sending anything: the messages were never delivered, but the state is
        # the part that must not be lost, and the owner will see it in /agent.
        status = {
            agent_spool.KIND_RESULT: "succeeded",
            agent_spool.KIND_ERROR: "failed",
            agent_spool.KIND_CANCELLED: "cancelled",
        }.get(kind, "")
        if not status:
            continue
        # The path, not the destination: a stream that ends with a result while
        # the row still says ``queued`` means the bot was down when the
        # ``started`` line arrived, and the table rightly refuses a queued task
        # that "succeeded". Walking the path applies what the stream is evidence
        # for — that it ran — before applying how it ended.
        path = agent_bridge.path_to(row.get("status", ""), status)
        if not path:
            continue
        for step in path[:-1]:
            db.agent_task_update(request_id, status=step, started_at=int(time.time()))
        db.agent_task_update(
            request_id,
            status=status,
            result=agent_spool.result_text(request_id)[
                : int(db.AGENT_RESULT_MAX_CHARS)
            ],
            finished_at=int(time.time()),
        )
        agent_spool.release(request_id)
        log.info("agent task %s reconciled to %s at startup", request_id, status)
    # A lock for a task that is no longer active is a runner that was killed
    # outright. Clearing it is what lets the task be retried.
    for row in db.agent_task_recent(limit=50):
        if row.get("status") in db.AGENT_ACTIVE_STATUSES:
            continue
        if agent_spool.locked(str(row.get("request_id") or "")):
            agent_spool.release(str(row.get("request_id") or ""))
    return published
