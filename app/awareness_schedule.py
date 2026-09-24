"""Increment U — which room the next awareness pass should read.

The problem this module exists for
----------------------------------
The awareness allowance is rationed in **requests**: 200 a day for the whole
deployment, and it is not going up. Every pass therefore has an opportunity
cost, and the question this module answers is the one the scheduler could not:
*given several rooms with something unread, which one should the next pass
spend itself on?*

Until now the answer was "all of them, in whatever order they arrived". Each
room was read on its own debounce deadline, at its own share of the allowance,
and a room whose conversation never needed reading was read exactly as often as
one whose conversation did. On a deployment of many groups that is the waste:
the allowance is spread evenly over rooms that needed a reading and rooms that
did not.

This module is that decision, and nothing else. It is not a second semantic
reader, not a second model call and not a second classifier. It consumes the
one deterministic reading the project already produces for a message — whether
the message depends on the room — and turns it into a small, bounded,
content-free priority per room, which the pass loop uses to decide whether to
spend a request *now*.

The seam, and why ``awareness.due`` stays message-blind
-------------------------------------------------------
``awareness.due`` decides *whether* a room may be read, and its signature is
asserted by a test: it may see timestamps and nothing else, because a function
that could see the words would inevitably start deciding *whether the words
matter*. That is the boundary this module is built to respect rather than move.

So the content never reaches the decision. It is read **once**, at capture
time — where the text is already in hand and the server is already writing a
row — and reduced to a one-word class before it is stored::

    a message arrives
          |
          v
    awareness.capture(...)                 the existing write; unchanged
          |
          +--> awareness_schedule.note(chat_id, class)   <- this module
          |
          v
    awareness.due(row, now=...)            unchanged, still blind
          |
          v
    awareness_schedule.defer(chat_id, ...) <- this module: spend, or wait?
          |
          v
    the existing pass

``due`` never learns about the hint. It is asked about a room exactly as it was
before and it answers exactly as it did before. The deferral is applied *after*
it, on a room that ``due`` has already said is eligible, and the urgent path
(``_awareness_promptly``) does not consult it at all. The scheduler decides
only *whether this eligible room is worth a request yet*; it cannot make an
ineligible room eligible, cannot bypass a cooldown, a brake, a breaker or the
allowance, and cannot make a pass happen that the policy would have refused.

What the hint is
----------------
One word per room, from a closed vocabulary of three values, and it is the
**strongest class seen among the room's messages that no pass has read yet**:

* ``HIGH`` — the batch contains a message that depends on the room. This is the
  project's own reading of "this message needs the room" — an anaphor or a
  deictic («همونو بزن»), an instruction or a correction, an opinion request
  («نظرت چیه؟»), a bare interrogative with no subject of its own («چی؟»), a
  reply, an attachment, or a message that addressed the assistant. It is
  *reused* rather than re-implemented: see "The boundary this module must not
  cross" below.
* ``LOW`` — there is something unread, but nothing in it suggests it needs the
  room. Idle chatter, a greeting, a self-contained question with its own
  subject.
* ``""`` (none) — the room has no live hint: nothing has been noted, or the
  hint has expired, or a pass has read the batch it described.

There is deliberately no numeric score and no weight table. Two classes, and a
measurement rather than a taste decided that two are enough: see
``tools/eval_awareness_schedule.py``, which scores the candidate signals and
the candidate mechanisms over a labelled workload.

What the scheduler does with it, and what it deliberately does not
------------------------------------------------------------------
A room whose batch carries no evidence that it needs the room is **deferred**:
the pass loop skips it and spends the request elsewhere. The deferral is
bounded by ``_bound`` — the window's own retention — because past that point
the messages being deferred have been purged and there is nothing left to
read. A room is never deferred past that bound, and a room with *no* hint at
all is never deferred: the scheduler only holds back a room it has actually
read the evidence for.

One room is never deferred whatever its batch looks like: **a room the server
is waiting on.** A pending admin confirmation is consumed *by the pass*, and
the message that confirms it — «تأیید میکنم» — is self-contained, so the
classifier is right to say it does not need the room and the scheduler would be
wrong to postpone it. That distinction — *the message does not need the room*
versus *the pass does not need to run* — is the one real hazard in this
mechanism, and the server's own "I am waiting" flag is what closes it.

There is no reordering of the pending list. That was built, measured and
removed, and the reason is worth recording because it is a fact about the
architecture rather than about this module: **the scheduler is event-driven per
room, not batch-driven.** Each room is offered a pass on its own debounce
deadline and refused or allowed by its own share of the allowance, so at any
instant there is essentially one room to consider and nothing to sort. Measured
over eight seeds of the benchmark's workload, ordering the candidate list
changed the outcome by exactly zero passes; the spend decision changed it by
twenty-three percentage points. The honest implementation is therefore the one
that does the work, not the one that matches the sketch.

The other candidate mechanism — deferring a low-value room only while another
room is holding a message that needs the room — was also measured and rejected.
It is strictly safer (it can only ever postpone a pass while better work is
pending) and it is worth about one percentage point, because the rooms that
need the room are not holding work most of the time. It is recorded in the
benchmark rather than shipped.

Staleness
---------
A hint is evidence about a **batch of unread messages**, so it is discarded the
moment that batch is read (``forget``, called when a pass finishes) and it
expires on its own after a bounded time (``_bound``) whether or not a pass ever
happens. The TTL is the retention window, which is also the deferral bound, for
the same reason: past it, the messages the hint describes have been purged from
the window and the hint describes nothing. A missing or expired hint is not an
error and needs no fallback path: ``priority`` returns ``""``, ``defer``
returns ``False``, and the scheduler behaves exactly as it did before this
module existed. Stale evidence degrades to no evidence; it never becomes
authority.

The boundary this module must not cross
----------------------------------------
* **It makes no model call**, imports no model client, and adds no request to
  any workload. The hint is a regex-and-token pass over one message, run where
  the message is already being written to the database.
* **It is not the context selector.** Increment Y's ``context_plan`` decides
  the minimum *context* for a chat turn; this decides *whether an awareness
  pass is worth spending*. The only thing taken from Y is one boolean —
  whether the message's own shape says it depends on the room — and it is taken
  precisely so that the two layers cannot disagree about what "needs the room"
  means. Nothing here composes, selects, orders or bounds a context block, and
  the ``ContextPlan`` is never imported.
* **It grants nothing.** Nothing in ``app/rbac.py`` or ``app/admin_service.py``
  imports this module. A room being read first is not a permission, not a
  promotion and not an action; it is the order two reads happen in.
* **It holds no message content.** The store is ``chat_id -> (class, stamp)``.
  A class is one of three words and a stamp is a float; there is no field a
  sentence could be written into, and nothing here logs a message.
* **It is isolated by ``chat_id`` alone**, like every other per-room structure
  in the feature (``awareness._trigger_at``, ``main._awareness_last_pass``,
  ``awareness_context._rooms``). There is no read that does not name the room,
  so one group's hint cannot reach another's and a private chat — which never
  reaches the capture path at all — has no hint to leak.
* **It is bounded.** At most ``MAX_ROOMS`` entries, evicted oldest-first, and
  every entry expires by ``_bound``.
* **It cannot starve a room.** The deferral is bounded, the room stays
  eligible throughout, a room the server is waiting on is exempt, and the
  urgent path ignores the deferral entirely. The measured effect on starvation
  is an *improvement* rather than a regression: the requests it frees go to
  rooms that were previously going unread.
"""
from __future__ import annotations

import logging
import time

from . import config, context_plan

log = logging.getLogger("guardbot.awareness.schedule")

# ── The closed priority vocabulary ────────────────────────────────────────
# Three values, ordered. ``P_NONE`` is not "low" — it is *no evidence*, which
# is why it is the empty string, why ``note`` refuses to store it, and why
# ``defer`` never holds back a room that carries it: an unknown room and a
# quiet room must be treated the same, and the safe direction is to read.
P_NONE = ""
P_LOW = "low"
P_HIGH = "high"

# The order, as a rank. Used for the maximum in ``note``; the only numbers in
# the module, and they are ordinals rather than weights.
RANK = {P_NONE: 0, P_LOW: 1, P_HIGH: 2}

# How many rooms may hold a hint at once. A deployment has a bounded number of
# groups, but "bounded by how many groups exist" is not a bound this module can
# see, and an unbounded dict on the capture path is exactly the shape the
# architecture forbids. Past the cap the oldest stamp is evicted, which is the
# entry whose evidence is closest to expiring anyway.
MAX_ROOMS = 512


def read(
    text: str,
    *,
    kind: str = "",
    reply: bool = False,
    media: bool = False,
    directed: bool = False,
) -> str:
    """The scheduling class of one message. Pure, cheap, and never raising.

    ``P_HIGH`` when the message's own shape says it depends on the room, and
    ``P_LOW`` otherwise. The reading is ``context_plan.read`` — the project's
    one deterministic answer to "does this message need the room" — asked for
    the single boolean ``wants_awareness``. The ``Reading`` itself is not kept,
    not returned and not imported by any caller: this function's whole output
    is one of two words.

    ``reply``, ``media`` and ``kind`` are the structural facts the capture path
    already holds and already stores; they are passed through because Y's
    reading uses them and a scheduler that dropped them would disagree with the
    chat path about the same message. ``directed`` — the message called Nexus —
    is carried for the same reason and *is* part of the class: a room in which
    somebody is addressing the assistant is a room the assistant is in the
    middle of, and that is worth the same as any other dependency. It is
    evidence about the *room*, not a claim about the message's meaning, and it
    grants nothing: the addressed message is still answered (or not) by the
    addressed path, and every action is still authorised from the actor's id.

    A reader that raises contributes nothing: the class falls back to ``P_NONE``
    — *no evidence* — which ``note`` refuses to store, so the room is read
    exactly as it was before this module existed. That is the only safe
    fallback: ``P_LOW`` would mean a broken reader silently *delays* readings
    across the whole deployment, which is the failure this module exists to
    avoid rather than to introduce.
    """
    try:
        reading = context_plan.read(text, kind=kind, reply=reply, media=media)
    except Exception:  # noqa: BLE001 - a reader is never worth a capture
        log.exception("the context reader failed while scheduling awareness")
        return P_NONE
    if directed or reading.wants_awareness:
        return P_HIGH
    return P_LOW


# ── The store ─────────────────────────────────────────────────────────────
# ``chat_id -> (class, monotonic stamp)``. Monotonic because this is a
# duration, and a wall clock that steps must not be able to make a hint look
# older than it is (which would expire it early) or younger (which would let it
# outlive its batch). The row timestamps the caller compares are wall-clock and
# stay in the caller; the two clocks are kept apart deliberately.
_hints: dict[int, tuple[str, float]] = {}


def _bound() -> float:
    """How long a hint may live, and how long a room may be deferred.

    One number for both, and it is derived rather than configured:
    ``NEXUS_AWARENESS_RETENTION_SECONDS``. A hint describes a batch of unread
    messages, and a deferral postpones reading that batch; past the retention
    the rows have been purged from the window, so the hint would describe
    nothing and the deferral would lose the messages it was holding back.
    Deriving it means there is no second number to drift out of step with the
    retention it is about, and no knob an operator has to reason about.
    """
    return max(1.0, float(config.NEXUS_AWARENESS_RETENTION_SECONDS))


def note(chat_id: int, priority: str, *, now: float | None = None) -> None:
    """Raise this room's hint to ``priority``, stamped at ``now``.

    The maximum, never the last: a room that has produced one message needing
    the room and nine that do not is still a room that needs the room, and
    letting the newest trivial message overwrite that would make the hint
    depend on which message happened to arrive last. A weaker class still
    **refreshes the stamp**, because the stronger message it refers to is still
    unread — the hint is not stale merely because the room kept talking.

    ``P_NONE`` is refused rather than stored: it means "no evidence", and a room
    with no evidence must be indistinguishable from a room never seen.
    """
    if priority not in RANK or priority == P_NONE:
        return
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):  # pragma: no cover - ids are ints here
        return
    moment = time.monotonic() if now is None else float(now)
    current, _ = _hints.get(chat_id, (P_NONE, 0.0))
    if RANK.get(current, 0) > RANK.get(priority, 0):
        priority = current
    _hints[chat_id] = (priority, moment)
    if len(_hints) > MAX_ROOMS:
        _evict()


def priority(chat_id: int, *, now: float | None = None) -> str:
    """This room's live hint, or ``P_NONE`` when there is none. Never raises.

    An expired entry is dropped on the read rather than swept on a timer: the
    only reader is the scheduler, the store is at most ``MAX_ROOMS`` entries,
    and a room nobody asks about costs nothing while it sits there.
    """
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):  # pragma: no cover
        return P_NONE
    entry = _hints.get(chat_id)
    if entry is None:
        return P_NONE
    cls, stamp = entry
    moment = time.monotonic() if now is None else float(now)
    if moment - stamp > _bound():
        _hints.pop(chat_id, None)
        return P_NONE
    return cls


def forget(chat_id: int) -> None:
    """Drop this room's hint. Called when a pass has read the batch it described."""
    try:
        _hints.pop(int(chat_id), None)
    except (TypeError, ValueError):  # pragma: no cover
        return


def reset() -> None:
    """Forget every hint. For tests, and for shutdown."""
    _hints.clear()


def size() -> int:
    """How many rooms currently hold a hint. Counts only, for a test or a log."""
    return len(_hints)


def _evict() -> None:
    """Drop the oldest entries until the store is back under its cap."""
    overflow = len(_hints) - MAX_ROOMS
    if overflow <= 0:
        return
    for chat_id, _ in sorted(_hints.items(), key=lambda item: item[1][1])[:overflow]:
        _hints.pop(chat_id, None)


# ── The spend decision ────────────────────────────────────────────────────
def defer(
    chat_id: int,
    *,
    waited: float,
    waiting: bool = False,
    now: float | None = None,
) -> bool:
    """Whether this room's pass should be postponed. The whole of increment U.

    True only when the server has actually read the evidence and it says the
    room does not need reading: the room's hint is ``P_LOW`` — a batch with
    something unread in it and no dependency signal — and the oldest unread
    message has not yet waited past ``_bound``.

    Four refusals are what make this safe rather than merely clever:

    * **No hint is not low.** A room the server has never classified, or whose
      hint has expired, or which the tests captured through ``awareness.capture``
      directly, returns ``False`` here and is read exactly as before. The
      deferral is applied only where the evidence exists.
    * **The bound is real.** A room is never postponed past the retention
      window, so a room that is only ever idle chatter is still read, and the
      messages being held back are still in the window when it is.
    * **A room the server is waiting on is never deferred.** A pending admin
      confirmation is consumed *by the pass*, and its message — «تأیید میکنم» —
      is self-contained: it does not need the room's context, so the class is
      ``P_LOW`` and the naive rule would postpone the owner's already-approved
      action by up to the retention window. The server knows when it is waiting
      on a room (``db.admin_pending_waiting``), so ``waiting`` is part of the
      decision rather than left to chance. This is the one place the scheduler
      can be *wrong* rather than merely slow, and it is refused here.
    * **The caller can always override.** ``main._awareness_run_room`` consults
      this only on the ordinary path; the urgent path (an administrator's
      actionable-looking message, ``nexus.looks_actionable``) never asks, so
      the hint can delay a *routine* reading and cannot delay a *prompted* one.

    ``waited`` is how long the oldest unread message has been waiting, in
    seconds — computed by the caller, which already holds the row. ``waiting``
    is whether the server is holding something for this room that only a pass
    can act on. ``now`` is the monotonic clock for the hint's expiry and exists
    so a test can age a hint without sleeping.
    """
    if waiting:
        return False
    if priority(chat_id, now=now) != P_LOW:
        return False
    return max(0.0, float(waited)) < _bound()


__all__ = [
    "MAX_ROOMS",
    "P_HIGH",
    "P_LOW",
    "P_NONE",
    "RANK",
    "defer",
    "forget",
    "note",
    "priority",
    "read",
    "reset",
    "size",
]
