"""The staged context an awareness pass reads: cheap always, deep only when asked.

The problem this module exists for
----------------------------------
An awareness pass is handed a transcript and a trusted block, and asked what the
room is doing. The transcript says *what was said*; it does not say who these
people are, what the room is, or what has been happening administratively. Those
are all facts the server already holds, and a pass that cannot see them
understands less than it could.

The obvious fix — put everything in the prompt — is the wrong one, and the
reason is cost. A group has dozens of members, so describing every one of them
on every pass would multiply the prompt for a batch that mentions two of them,
and the awareness allowance is rationed in **requests**: tokens spent on context
nobody asked for are paid on every pass, for ever. The owner's instruction was
explicit — the fullest useful picture, but **not preloaded without reason**.

So context is assembled from **sources**, and each source decides for itself
whether this batch needs it:

* **tier 0** — always rendered, and free. The date, from the server's own clock;
  the room's own name and type, from a cache the message handler fills; and the
  people the last pass recorded, which is a string already in the database that
  nothing used to read back.
* **tier 1** — rendered only when a deterministic predicate over the batch says
  the conversation calls for it: recent administrative actions when the batch
  involves authority, and one identity line per person the batch actually
  refers to when there is a reply edge to follow. Neither predicate consults a
  model, and neither fires on an ordinary member's ordinary message.

Adding a source later is adding a ``Source`` to ``SOURCES``; nothing in
``blocks`` and nothing in the caller changes. That is the extensibility the
owner asked for, and it is deliberately a data structure rather than a chain of
``if`` statements.

What this module may never do
-----------------------------
It reads. It makes no model call, no network call, and holds no path to a
permission: the roles come from ``awareness.roles_for`` (which reads
``app/rbac.py``) and the identities from ``identity``, both for a *label*, and
nothing here is imported by ``admin_service`` or ``rbac``. A role rendered here
is a sentence for the model to read, never a check — every tool call is
authorised again from the actor's id. The awareness boundary is unchanged; this
only adds context text to what the model is shown.

Every source is bounded twice — its own character cap, and the pass-wide
``NEXUS_AWARENESS_CONTEXT_CHARS`` ceiling — and a source that raises is logged
and skipped, because a context block is never worth failing a pass.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from . import (
    awareness,
    config,
    db,
    discourse,
    entities,
    identity,
    persian_calendar,
    referents,
    room_state,
    temporal,
)

log = logging.getLogger("guardbot.awareness.context")

# The two tiers. A tier-0 source is rendered on every pass; a tier-1 source is
# rendered only when its own predicate says this batch calls for it.
TIER_ALWAYS = 0
TIER_CONDITIONAL = 1

# A block with less room than this is not rendered at all: a fragment of a
# sentence costs tokens and tells the model *less* than nothing, because it looks
# like a complete thought that was cut off.
MIN_BLOCK_CHARS = 80


# ── The room, as Telegram last described it ───────────────────────────────
# Filled by the message handler, which already holds ``update.effective_chat``.
# A pass therefore needs no ``get_chat`` call — the cost of knowing the room is
# one dictionary write on a path that is already writing to the database, and
# the alternative is a network round trip per pass to learn a name that was in
# the update the pass was triggered by.
_rooms: dict[int, tuple[str, str]] = {}


def note_room(chat_id: int, title: str = "", chat_type: str = "") -> None:
    """Remember a room's name and type. Cheap, and never worth an exception."""
    try:
        _rooms[int(chat_id)] = (str(title or ""), str(chat_type or ""))
    except (TypeError, ValueError):  # pragma: no cover - ids are ints here
        return


def reset_rooms() -> None:
    """Forget the room cache. For tests, and for the same reason every other
    reset in this feature exists: a cache that cannot be cleared makes tests
    depend on the order they ran in."""
    _rooms.clear()


# ── What a source may read ────────────────────────────────────────────────
@dataclass(frozen=True)
class Ctx:
    """Everything a source may read, resolved once for one pass.

    A frozen value rather than a live object with queries behind it, and that is
    the design: a source cannot read something the pass did not already read, so
    "cheap context always, deep context only when needed" is enforced by what is
    *in* here rather than by discipline. The window is the one read a pass makes
    (``main._awareness_read``), the anchor and the roles are derived from it, and
    the room's name comes from the handler's cache.
    """

    chat_id: int
    messages: tuple[dict, ...] = ()
    anchor: dict | None = None
    roles: dict[int, str] = field(default_factory=dict)
    now: int = 0
    chat_title: str = ""
    chat_type: str = ""

    def anchor_id(self) -> int:
        return int((self.anchor or {}).get("user_id") or 0)

    def role_of(self, user_id: int) -> str:
        """The role a speaker holds **now**, or ``""`` when it is not known.

        ``roles`` is built by ``awareness.roles_for`` from ``app/rbac.py``, which
        is the one authority model. This is a read of it, never a second copy.
        """
        return str(self.roles.get(int(user_id)) or "")

    def is_authority(self, user_id: int) -> bool:
        return self.role_of(user_id) in (
            awareness.ROLE_OWNER,
            awareness.ROLE_ADMIN,
        )

    def authority_involved(self) -> bool:
        """Whether this batch concerns somebody who can act.

        Two signals, and each is a fact the capture path already wrote: the
        anchor is an administrator, or some message in the window carries the
        ``actor`` hint or addressed the assistant. Either means the pass may be
        asked to *do* something, which is when the room's administrative history
        is worth the tokens. An ordinary member's ordinary message is neither,
        and that is the case this predicate exists to exclude.
        """
        if self.is_authority(self.anchor_id()):
            return True
        return any(
            message.get("actor") or message.get("directed")
            for message in self.messages
        )

    def has_reply_edge(self) -> bool:
        """Whether anybody in this window replied to anybody else."""
        return any(int(m.get("reply_user_id") or 0) for m in self.messages)

    def referenced_ids(self) -> list[int]:
        """The people this batch actually refers to, in order, deduped.

        The anchor first — the person the pass is *about* — then the targets of
        the window's reply edges, newest first. Deliberately **not** every
        participant: a window of forty messages from ten people must not become
        ten identity lookups, which is exactly the preload the owner asked to
        avoid. The reply edge is what makes a person "referred to" rather than
        merely present.
        """
        candidates = [self.anchor_id()]
        candidates.extend(
            int(m.get("reply_user_id") or 0) for m in reversed(self.messages)
        )
        seen: list[int] = []
        for user_id in candidates:
            if user_id and user_id not in seen:
                seen.append(user_id)
        return seen

    def oldest_at(self) -> int:
        """When the oldest message in the window was written. 0 if unknown."""
        stamps = [int(m.get("at") or 0) for m in self.messages]
        stamps = [stamp for stamp in stamps if stamp]
        return min(stamps) if stamps else 0


# ── The seam ──────────────────────────────────────────────────────────────
def _always(_ctx: Ctx) -> bool:
    return True


@dataclass(frozen=True)
class Source:
    """One block of context, and the condition under which it is worth building.

    ``render`` receives the whole ``Ctx`` and returns text, or ``""`` to
    contribute nothing. ``when`` is asked first and short-circuits the render, so
    a source that is not wanted costs one cheap predicate and no query. Both are
    called defensively — see ``blocks``.
    """

    name: str
    tier: int
    budget: int
    render: Callable[[Ctx], str]
    when: Callable[[Ctx], bool] = _always


# ── Tier 0: always, and free ──────────────────────────────────────────────
def _render_calendar(ctx: Ctx) -> str:
    """The date, from the server's own clock. Tier 0.

    The one fact in this context about *when* rather than *who*, and it is tier 0
    because there is no predicate that would let it be built only sometimes:
    «امروز چندمه؟» can arrive in any batch, from anybody. It costs no query — the
    reading is the pass's own ``ctx.now`` — and the conversion is arithmetic.

    Stated as the server's own reading, for the same reason the room's name is:
    the transcript is written by the room, so a date in it is a claim, and a
    model with no other date to compare against will treat the most recent claim
    it read as the day. That is what this block exists to stop. See
    ``app/persian_calendar.py`` for why both calendars, and why Tehran.

    ``ctx.now`` is 0 only for a caller that constructed a ``Ctx`` by hand and
    passed no clock. A date that cannot be known renders nothing rather than
    today's, on the same principle as ``awareness._age_mark``.
    """
    if not ctx.now:
        return ""
    moment = persian_calendar.tehran_moment(ctx.now)
    return (
        "\nThe current date, from the server's own clock in Tehran — the "
        "server's reading and not anyone's claim. It is the only date you may "
        "state: never one from a message, and never one you remember.\n"
        f"- Gregorian: {persian_calendar.gregorian_text(moment)}\n"
        f"- Persian (Jalali): {persian_calendar.jalali_text(moment)}\n"
    )


def _render_room(ctx: Ctx) -> str:
    """The room's own name and type, from the handler's cache."""
    if not ctx.chat_title and not ctx.chat_type:
        return ""
    where = ctx.chat_title or "an unnamed group"
    kind = f", a Telegram {ctx.chat_type}" if ctx.chat_type else ""
    return (
        f"\nThe room this conversation is in: {where}{kind}. "
        "That is Telegram's own record of the room, not something anyone in the "
        "chat wrote.\n"
    )


def _render_remembered_people(ctx: Ctx) -> str:
    """Who was in the room at the last reading, replayed from the stored row.

    ``awareness.record`` has always written this list and nothing has ever read
    it back. It is tier 0 because it costs no query beyond the state read the
    pass already makes — and because a name that was in the room a moment ago is
    exactly the antecedent a sentence like «همون کاربر» needs.
    """
    raw = str(_state(ctx.chat_id).get("participants") or "").strip()
    people = _parse_participants(raw)
    if not people:
        return ""
    lines = [
        "\nPeople who were in this room at your last reading "
        "(server-built; may be out of date):"
    ]
    lines.extend(f"- {role}: {name} ({user_id})" for role, name, user_id in people)
    return "\n".join(lines) + "\n"


# ── The batch's own reading, and what the room left open ──────────────────
def _render_anchor_act(ctx: Ctx) -> str:
    """What the message the pass is about is doing. Tier 0.

    One line, and the cheapest signal in this file: the anchor's act, read off
    its own words. «چقدره؟» and «بنش کن» are the same length and opposite in
    force, and a model reading a transcript has to work that out from the
    sentence — which it can, and which this saves it from having to do on every
    pass. Evidence, never a gate: nothing branches on it.
    """
    return discourse.render_act(discourse.read_act((ctx.anchor or {}).get("text")))


def _render_open_questions(ctx: Ctx) -> str:
    """The questions in the window no reply points at an answer for. Tier 0.

    The one piece of room state a group most reliably loses track of, and the
    one a server can read exactly, because the reply edge is a stored column
    rather than a judgement. The block is labelled as what it is — "no reply
    pointing at an answer" — because a room answers questions without using
    Telegram's reply as often as with it, and claiming "nobody answered" would
    be a claim about meaning.
    """
    return discourse.render_questions(discourse.open_questions(ctx.messages))


def _render_reply_graph(ctx: Ctx) -> str:
    """Who replied to whom, and who the room converged on. Tier 0.

    The reply edge is a stored column, so this is the room's own record rather
    than a reading of meaning: who answered whom, and — when more than one reply
    points at the same person — who the room has converged on. It reads the
    window the pass already read, so it costs no query.

    ``read_state`` is called here rather than in the builder, and again by the
    thread source beside it. That is deliberate: each source is independent, so
    one failing costs its own block and no other, and the scan it repeats is a
    pass over rows already in memory.
    """
    return room_state.render_graph(room_state.read_state(ctx.messages, ctx.anchor))


def _render_thread(ctx: Ctx) -> str:
    """Whether the anchor continues the thread, with the words that decided it. Tier 0.

    The one reading in this pair rather than a record, and it says so: the shared
    words are rendered as its reason, because the model is the one that should
    weigh a heuristic. An abstention renders nothing.
    """
    return room_state.render_thread(room_state.read_state(ctx.messages, ctx.anchor))


def _render_entities(ctx: Ctx) -> str:
    """The things the anchor may point at, when they are not people. Tier 0.

    The correction this block exists to make: ``referent_candidates`` offers the
    room's *members* for a demonstrative, and when the demonstrative means the
    photograph somebody just posted, that is a wrong lead. The server knows the
    things exactly — the media kind is a stored column, a link is a regular
    expression away — so this says so, and says that they are things rather than
    people, and lets the model decide.
    """
    return entities.render(entities.read_entities(ctx.messages, ctx.anchor))


def _render_anchor_when(ctx: Ctx) -> str:
    """Where the message's own time words point, from the server's clock. Tier 0.

    A Persian sentence places itself in time with a word rather than a date —
    «الان»، «قبلاً»، «چند دقیقه پیش»، «دیروز»، «فردا» — and a model reading a
    transcript has no clock, so it supplies one and reads «قبلاً» as whenever it
    imagines the conversation to be. This block is the server's clock doing that
    arithmetic instead: the direction and the granularity the words state, plus
    how old the window the pass is reading is, so «قبلاً» has something to be
    earlier *than*.

    It renders nothing when the message carries no time word — the block is
    about what the words say, and a message that says nothing about time has
    nothing here. It states the server's reading and never a date, because
    «چند دقیقه پیش» does not contain one; see ``app/temporal.py``.
    """
    when = temporal.read_when((ctx.anchor or {}).get("text"))
    if not when:
        return ""
    return temporal.render(when, now=ctx.now, window_start=ctx.oldest_at())


def _state(chat_id: int) -> dict:
    try:
        return db.awareness_get(chat_id) or {}
    except Exception:  # noqa: BLE001 - context, never worth a crash
        log.exception("could not read the awareness state for the context")
        return {}


def _parse_participants(raw: str) -> list[tuple[str, str, int]]:
    """Read back the ``role:name:user_id`` list ``participants_of`` writes.

    Parsed defensively, because the name is whatever a person set as their
    display name and may itself contain a colon. The id is taken from the right
    and the role from the left, which leaves every colon in between in the name
    where it belongs. An entry that does not parse is skipped rather than shown
    as a fragment.
    """
    out: list[tuple[str, str, int]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        head, _, raw_id = entry.rpartition(":")
        role, _, name = head.partition(":")
        role, name = role.strip(), name.strip()
        try:
            user_id = int(raw_id)
        except ValueError:
            continue
        if role and name and user_id:
            out.append((role, name, user_id))
    return out


# ── Tier 1: only when the batch calls for it ──────────────────────────────
def _render_admin_activity(ctx: Ctx) -> str:
    """What administrators have done here recently. Tier 1.

    Rendered only when the batch involves authority (``Ctx.authority_involved``)
    because this is the block that answers "what has already been tried here" —
    a question worth the tokens when somebody is about to act, and noise when
    nobody is.

    The window is the batch's own: actions since the oldest message the pass is
    reading. That is the honest bound and it needs no second configuration
    number to drift out of step with the window it is supposed to match.
    """
    limit = max(1, int(config.NEXUS_AWARENESS_ADMIN_ACTIONS))
    since = ctx.oldest_at() or (ctx.now - 3600)
    try:
        rows = db.audit_since(chat_id=ctx.chat_id, since=since, limit=limit)
    except Exception:  # noqa: BLE001 - context, never worth a crash
        log.exception("could not read the room's administrative history")
        return ""
    if not rows:
        return ""
    lines = [
        "\nRecent administrative actions in this room "
        "(the server's own record, newest first):"
    ]
    lines.extend("- " + _audit_line(row, ctx.now) for row in rows)
    return "\n".join(lines) + "\n"


def _audit_line(row: dict, now: int) -> str:
    """One audit row, as the few facts worth the tokens."""
    bits = [str(row.get("action") or "?")]
    actor_id = int(row.get("actor_id") or 0)
    if actor_id:
        bits.append(f"by {actor_id}")
    target_id = row.get("target_id")
    if target_id:
        bits.append(f"on {int(target_id)}")
    outcome = str(row.get("outcome") or "")
    if outcome:
        bits.append(f"-> {outcome}")
    age = _ago(int(row.get("at") or 0), now)
    if age:
        bits.append(f"({age} ago)")
    return " ".join(bits)


def _render_referenced_people(ctx: Ctx) -> str:
    """One bounded identity line per person this batch refers to. Tier 1.

    The anchor and the reply targets — the people the conversation is actually
    *about* — and never the whole room. Each line is assembled from an
    allowlisted view (``identity.describe``) and only the few fields worth the
    tokens: who they are, how they stand, and how they have behaved here.
    """
    cap = max(1, int(config.NEXUS_AWARENESS_REFERENCED_PEOPLE))
    lines = [
        line
        for user_id in ctx.referenced_ids()[:cap]
        if (line := _identity_line(user_id, ctx.chat_id, ctx.now))
    ]
    if not lines:
        return ""
    return (
        "\nPeople this batch refers to (server-built from ids, not from what "
        "anyone in the chat claimed):\n" + "\n".join(lines) + "\n"
    )


def _identity_line(user_id: int, chat_id: int, now: int) -> str:
    try:
        view = identity.describe(user_id, chat_id=chat_id)
    except Exception:  # noqa: BLE001 - one person's line is not worth a pass
        log.exception("could not describe a referenced person")
        return ""
    if not view or view.get("error"):
        return ""
    name = str(view.get("name") or view.get("username") or "?")
    bits = [f"{name} ({user_id})", f"role {str(view.get('role') or 'member')}"]
    username = str(view.get("username") or "")
    if username:
        bits.append(f"@{username}")
    level = int(view.get("level") or 0)
    if level:
        bits.append(f"level {level}")
    strikes = int(view.get("strikes") or 0)
    if strikes:
        bits.append(f"strikes {strikes}")
    count = int(view.get("message_count") or 0)
    if count:
        bits.append(f"{count} messages seen")
    age = _ago(int(view.get("last_seen") or 0), now)
    if age:
        bits.append(f"last seen {age} ago")
    return "- " + ", ".join(bits)


def _wants_referents(ctx: Ctx) -> bool:
    """Whether this batch is one where a pronoun needs resolving.

    Two clauses, and each excludes a case where the block would be waste:

    * **a reply edge settles it already.** When the instruction was sent as a
      reply, ``awareness.instruction_block`` states that id as fact, and a ranked
      candidate list beside it would spend tokens re-deciding something the
      server already knows. The resolver's value is exactly the cases the reply
      edge cannot reach.
    * **only somebody who can act.** The block exists to resolve a person for a
      possible action, and only an authority can act. A member's «این چیه» has no
      action behind it, so the block would put a ranked list of the room's people
      in front of the model for an ordinary message — tokens with no use, and the
      kind of preload the owner asked to avoid. A *directed* message is the
      second case, because Nexus has been asked something and the referent is
      what it is being asked about.
    """
    if int((ctx.anchor or {}).get("reply_user_id") or 0):
        return False
    if ctx.is_authority(ctx.anchor_id()):
        return True
    # The capture-time ``actor`` hint is the second reading, and it is a stored
    # server fact rather than a second authority model: it was written by
    # ``nexus.is_actor``, which is itself a read of ``app/rbac.py``. It covers
    # the case the role lookup cannot — an anchor whose row is not in the window
    # the roles were resolved from — without ever granting anything.
    if bool((ctx.anchor or {}).get("actor")):
        return True
    return bool((ctx.anchor or {}).get("directed"))


def _render_referent_candidates(ctx: Ctx) -> str:
    """Who a deictic instruction may mean, ranked. Tier 1.

    The resolver is ``app/referents.py`` and the rendering is its own, because
    the two are one idea: the candidates and the honest reading of how close
    they are. This function only decides that the batch calls for it and bounds
    the list — the arithmetic and the wording both live beside the tests that
    pin them.

    It reads the window the pass already read (``ctx.messages``) and the roles
    the pass already resolved (``ctx.roles``), so it costs no query. A resolver
    that raises contributes nothing, like every other source.
    """
    try:
        resolution = referents.resolve(
            ctx.anchor,
            messages=list(ctx.messages),
            roles=dict(ctx.roles or {}),
            limit=max(1, int(config.NEXUS_AWARENESS_REFERENTS)),
        )
    except Exception:  # noqa: BLE001 - context, never worth a crash
        log.exception("could not resolve the referent candidates")
        return ""
    return referents.render(resolution)


def _ago(then: int, now: int) -> str:
    """A short, honest age. ``""`` when it cannot be known."""
    if not then or not now or then > now:
        return ""
    seconds = now - then
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# The registry, and the extensible seam: a new source is one more entry here.
# Order is tier order and then declaration order, so the always-cheap context
# comes first and the conditional blocks fill whatever budget is left.
#
# The date is first among the tier-0 sources because it is the only block whose
# absence is *worse* than a wrong answer would be: a pass that loses the room's
# name still knows the room from the transcript, while a pass that loses the date
# has nothing to compare a claim against and will supply one from memory. It is
# short, it costs no query, and the ceiling below is a hard one that stops
# sources — so the one that must not be stopped goes first.
SOURCES: tuple[Source, ...] = (
    Source("calendar", TIER_ALWAYS, 400, _render_calendar),
    Source("room", TIER_ALWAYS, 200, _render_room),
    Source("remembered_people", TIER_ALWAYS, 400, _render_remembered_people),
    # What the batch is *doing*, and what the room has left unanswered. Both are
    # tier 0 because both are cheap — a scan of the window the pass already
    # read, no query — and because both are room state the model should not have
    # to re-derive from a transcript on every pass. The act is one line and
    # renders nothing when the words carry no reading; the question block is
    # empty unless there is a question no reply points at.
    Source("anchor_act", TIER_ALWAYS, 200, _render_anchor_act),
    Source("open_questions", TIER_ALWAYS, 500, _render_open_questions),
    # Who is talking to whom, and whether this message is still the same thread.
    # Tier 0: both are a pass over the window the pass already read. The graph is
    # the room's own record (a stored reply column); the thread is a heuristic
    # with its evidence attached, and it renders nothing when the anchor is too
    # short to judge.
    Source("reply_graph", TIER_ALWAYS, 600, _render_reply_graph),
    Source("thread", TIER_ALWAYS, 500, _render_thread),
    # What the anchor may point at when it is not a person — the photo, the link,
    # the message it replies to. Tier 0: a scan of the window the pass already
    # read. It goes after the reply graph because the two are read together, and
    # before ``anchor_when`` so the shortest block stays the cheapest to lose.
    Source("entities", TIER_ALWAYS, 600, _render_entities),
    # Where the anchor's own time words point, from the server's clock. Last of
    # the tier-0 sources on purpose: it is the shortest block and the one that
    # renders least often (only when the message carries a time word), so if the
    # pass-wide ceiling ever bites it is the cheapest thing to lose. It is a
    # *reading of the anchor*, like ``anchor_act``, but it goes after the
    # window-wide question block so that block — the larger piece of room state
    # — is never the one dropped.
    Source("anchor_when", TIER_ALWAYS, 300, _render_anchor_when),
    Source(
        "admin_activity",
        TIER_CONDITIONAL,
        500,
        _render_admin_activity,
        lambda ctx: ctx.authority_involved(),
    ),
    # Before ``referenced_people`` and after ``admin_activity``: it is the block
    # that answers "who does this instruction mean", so it goes ahead of the
    # directory of who these people are, and behind the record of what has
    # already been tried here. The two predicates are disjoint in practice —
    # this one declines whenever the anchor is a reply, and ``referenced_people``
    # requires a reply edge somewhere in the window — so on the batch that
    # matters most (an authority's un-replied «اینو بن کن») this is the only
    # conditional block with something to say.
    Source(
        "referent_candidates",
        TIER_CONDITIONAL,
        700,
        _render_referent_candidates,
        _wants_referents,
    ),
    Source(
        "referenced_people",
        TIER_CONDITIONAL,
        500,
        _render_referenced_people,
        lambda ctx: ctx.has_reply_edge(),
    ),
)


# ── Assembly ──────────────────────────────────────────────────────────────
def build_ctx(
    chat_id: int,
    *,
    messages: list[dict] | None = None,
    anchor: dict | None = None,
    roles: dict[int, str] | None = None,
    now: int = 0,
) -> Ctx:
    """Resolve everything a source may read, in one place.

    The window, the anchor and the roles are passed in when the caller already
    holds them: ``main._awareness_read`` reads the room once and derives all
    three, and a builder that read them again would be a second query per pass
    for an answer it was handed. Only the room's name comes from elsewhere, and
    that is a dictionary lookup — the handler cached it.
    """
    rows = list(messages if messages is not None else awareness.window(chat_id))
    if anchor is None:
        anchor = awareness.anchor(chat_id, messages=rows)
    if roles is None:
        roles = awareness.roles_for(rows)
    title, chat_type = _rooms.get(int(chat_id), ("", ""))
    return Ctx(
        chat_id=int(chat_id),
        messages=tuple(rows),
        anchor=anchor,
        roles=dict(roles or {}),
        now=int(now or time.time()),
        chat_title=title,
        chat_type=chat_type,
    )


def blocks(ctx: Ctx) -> str:
    """Render every source this batch calls for, within the pass-wide ceiling.

    Tier order, then declaration order, and the first tier to be exhausted stops
    the rest: the total budget is a hard ceiling, not a target. A source whose
    predicate is false is not rendered at all, and a source that raises is
    logged and skipped — a pass that loses a context block still understands the
    room, while a pass that dies loses the room entirely.
    """
    total = max(0, int(config.NEXUS_AWARENESS_CONTEXT_CHARS))
    deep = bool(config.NEXUS_AWARENESS_CONTEXT_DEEP)
    out: list[str] = []
    used = 0
    for source in SOURCES:
        if source.tier != TIER_ALWAYS and not deep:
            continue
        if not _wanted(source, ctx):
            continue
        remaining = total - used
        if remaining < MIN_BLOCK_CHARS:
            break
        text = _rendered(source, ctx, min(source.budget, remaining))
        if not text:
            continue
        out.append(text)
        used += len(text)
        if used >= total:
            break
    return "".join(out)


def _wanted(source: Source, ctx: Ctx) -> bool:
    try:
        return bool(source.when(ctx))
    except Exception:  # noqa: BLE001 - a predicate is never worth a pass
        log.exception(
            "awareness context source %r failed its predicate", source.name
        )
        return False


def _rendered(source: Source, ctx: Ctx, cap: int) -> str:
    try:
        text = source.render(ctx)
    except Exception:  # noqa: BLE001 - a block is never worth a pass
        log.exception("awareness context source %r failed to render", source.name)
        return ""
    return _clip(str(text or ""), cap)


def _clip(text: str, cap: int) -> str:
    """Bound one block, on a line boundary where there is one.

    Cutting mid-sentence is avoided where the block has lines to cut on, because
    a truncated clause reads as a finished thought. A block with no newline in
    its first ``cap`` characters is cut where it must be. The newline the result
    ends with is counted against the cap, so the guarantee is ``len <= cap`` and
    not ``len <= cap + 1``.
    """
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    room = max(1, cap - 1)
    cut = text.rfind("\n", 0, room)
    if cut <= 0:
        cut = room
    return text[:cut].rstrip() + "\n"
