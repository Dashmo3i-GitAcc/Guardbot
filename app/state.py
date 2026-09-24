"""Conversational state: what the current interaction is trying to accomplish.

The four context sources, and why this is the third of them rather than a second
copy of one of the others:

* **Conversation History** — what was recently said. The room window and the
  transcript; short-lived and chronological.
* **Awareness** — what is happening around Nexus now. The room-scoped reading.
* **State** — *this module*. What the current interaction is trying to do: the
  active topic, the active goal, the question left open.
* **Long-Term User Memory** — what is worth remembering about a person.

The boundary is the whole design, and it is easiest to see in one example. A
person's preference for Python is **Memory** (``app/memory.py``, durable, keyed
by slot, relevant to almost any question they ask). "Currently debugging the
Python authentication bug" is **State** — it is true *now*, it ends when the bug
is fixed, and it is about the interaction rather than about the person. Neither
is a transcript: State stores a topic, a goal and a question, never messages.

Four rules shape it, and each is a refusal.

**It is one active row.** State is keyed by ``(chat_id, user_id)`` and holds a
single current task — not a set, not a log, not a list of everything that was
ever discussed. That is what makes it unambiguous: "the active topic" has one
answer, and a new task *replaces* the old one rather than joining it. The row is
the bound; there is no "how many tasks" number to tune.

**It transitions explicitly.** A message either states a new task (activate or,
if one is already active, replace), continues the current one, completes it,
resets to something unrelated, or says nothing about state at all. A completion
or a reset **clears** the row rather than leaving a finished task looking active.
Ordinary conversation — greetings, acknowledgements, reactions, a transient
remark — matches nothing and changes nothing.

**It is deterministic and costs no request.** The transitions are read by rules
over the message the person typed. There is no model call, no provider workload
and no seam: increment X is scoped at "Gemini: 0 expected", the request
allowance is rationed, and the signals below cover the cases that matter. A
``state`` Gemini pool does not exist, so State cannot spend a request the chat
answer is waiting on — the isolation is by construction rather than by a budget.

**It grants nothing.** State is context the model reads. Nothing in
``app/rbac.py`` or ``app/admin_service.py`` imports this module, and no function
here returns an authority. A state that says "working on the admin panel" does
not make anybody an administrator: authority is resolved from the Telegram id
and from nothing anybody typed, exactly as it is for Memory.

Concurrency and idempotency are the store's job and are documented at
``app/db.state_put``: the write is a compare-and-swap on a version number, so an
older background worker can never overwrite a newer state, and the message id is
carried so a duplicate delivery is a no-op. This module never blocks a handler:
``app/main.py`` schedules ``observe`` as a background task, and the answer path
only ever *reads* the latest already-available row.
"""
from __future__ import annotations

import logging
import re
import time

from . import config, db, people

log = logging.getLogger("guardbot.state")

# ── The closed vocabularies ───────────────────────────────────────────────
# Status is closed so a corrupted or model-written value can never invent one.
STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUSES = (STATUS_ACTIVE, STATUS_BLOCKED)

# The transitions. Named rather than numbered so a reviewer can read a row and
# a test can assert the exact lifecycle step. Not every project needs these
# exact names; they are this repository's vocabulary for the states the brief
# lists (NEW/ACTIVATE/UPDATE/REFERENT_CHANGE/QUESTION_OPEN/QUESTION_RESOLVED/
# TASK_COMPLETED/TASK_REPLACED/EXPIRE/RESET).
TRANSITION_ACTIVATE = "activate"
TRANSITION_UPDATE = "update"
TRANSITION_REPLACE = "replace"
TRANSITION_COMPLETE = "complete"
TRANSITION_RESOLVE = "resolve"
TRANSITION_RESET = "reset"
TRANSITION_CONTINUE = "continue"
TRANSITION_EXPIRE = "expire"
TRANSITIONS = (
    TRANSITION_ACTIVATE,
    TRANSITION_UPDATE,
    TRANSITION_REPLACE,
    TRANSITION_COMPLETE,
    TRANSITION_RESOLVE,
    TRANSITION_RESET,
    TRANSITION_CONTINUE,
    TRANSITION_EXPIRE,
)

# Where a row came from. One value today, and a column rather than a constant so
# a later version can tell a rule-written state from anything it derives, the
# same shape ``app/memory.py`` uses.
SOURCE_AUTO = "auto"

# Every how many recordings the global backstop runs. The per-read TTL check is
# free, so it is not gated; only the whole-table prune is. Same shape and same
# reason as ``app/memory.PRUNE_EVERY``.
PRUNE_EVERY = 200
_since_prune = 0


def reset_state() -> None:
    """Forget the prune counter. For tests."""
    global _since_prune
    _since_prune = 0


def _clip(text: str, cap: int) -> str:
    """Bound one field to ``cap`` characters, on a word boundary where there is
    one. A state field is a phrase, not a paragraph."""
    text = " ".join(str(text or "").split())
    if cap <= 0 or len(text) <= cap:
        return text
    cut = text.rfind(" ", 0, max(1, cap - 1))
    if cut <= 0:
        cut = max(1, cap - 1)
    return text[:cut].rstrip()


def _fold(text: str) -> str:
    """The text the rules match against: ZWNJ replaced by a space.

    Persian is written with and without the zero-width non-joiner — «میکنیم»
    and «می کنیم» are the same word — so folding to a space before matching is
    what makes one rule cover both. Same fold ``app/memory.py`` uses.
    """
    return str(text or "").replace("\u200c", " ")


def _get(obj, key: str, default=""):
    """One field from a ``User`` object or a dict, without assuming which."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _tokens(text: str) -> set[str]:
    return {t for t in people.normalize(text).split() if len(t) >= 3}


# ── The rules ─────────────────────────────────────────────────────────────
#
# A transition is read from the message's own words, and the reading is
# deliberately conservative: a rule must match, or the message changes nothing.
# The rules are grouped by the transition they produce, and the order in
# ``read`` decides the precedence.

# A completion, or a reset. Both clear the active state.
_COMPLETE_RE = re.compile(
    r"(?:حل|درست|تموم|تمام|فیکس)\s*(?:شد|ش|شده|کردیم|کردم)"
    r"|راه\s*افتاد"
    r"|\b(?:fixed|solved|resolved|done|finished|it\s+works\s+now)\b",
    re.IGNORECASE | re.UNICODE,
)

# An explicit change of subject: a new, unrelated context, not a new task. The
# phrasings are the ones that mean "drop what we were doing", and they are kept
# narrow on purpose — «بریم سراغ X» is a *new task* (activate) and must not be
# read as a reset.
_RESET_RE = re.compile(
    r"(?:بحث|موضوع|حرف)\s*(?:رو|را)?\s*عوض"
    r"|(?:یه|یک)\s+چیز\s+دیگه"
    r"|دیگه\s+(?:اون|آن)\s+\S+\s+نیست"
    r"|از\s+(?:اون|آن)\s+بگذر"
    r"|\b(?:change\s+the\s+subject|different\s+topic|never\s+mind\s+that"
    r"|forget\s+(?:that|it))\b",
    re.IGNORECASE | re.UNICODE,
)

# A task/topic begins. The "let's do X" family: an inclusive imperative with a
# task verb, a bare "let's go to X", or the English equivalent. The captured
# group is the topic; a validator is not used because the *verb* is what makes
# it a task, and the verb is part of the pattern.
_TASK_PATTERNS = (
    # «بیا مشکل لاگین رو درست کنیم» / «بریم X رو حل کنیم» / «باید X رو بررسی کنیم»
    r"(?:بیا|بیایید|بریم|بزن\s+بریم|میخوام|می\s?خوام|میخوایم|می\s?خوایم|باید)\s+"
    r"(?:(?:روی|سراغ)\s+)?(?P<v>[\w\u0600-\u06ff][\w\u0600-\u06ff ]{1,60}?)\s+"
    r"(?:رو\s+|را\s+)?(?:درست|حل|اصلاح|بررسی|ادامه|تموم|انجام|راه\s?اندازی|کار)\s*"
    r"(?:کنیم|بدیم|بکنیم|کنم|بدم|کنید|بکنید)",
    # «بریم سراغ X» — a topic with no verb of its own
    r"(?:بریم|بزن\s+بریم|برو)\s+سراغ\s+(?P<v2>[\w\u0600-\u06ff][\w\u0600-\u06ff ]{1,60})",
    # "let's fix X"
    r"\blet'?s\s+(?:fix|debug|solve|work\s+on|continue|handle|build|do|start)\s+"
    r"(?P<v3>[\w ']{2,60})",
)
_TASK_RE = re.compile(
    "|".join(f"(?:{p})" for p in _TASK_PATTERNS), re.IGNORECASE | re.UNICODE
)

# A continuation marker: the message asks to carry on with what is already
# active. It changes nothing but is a real event (it re-stamps the row), and it
# is what makes a "قدم بعدی چیه؟" legible as a continuation rather than a new
# question.
_CONTINUE_RE = re.compile(
    r"ادامه\s*(?:بده|بدیم|داریم)"
    r"|قدم\s*بعدی"
    r"|بعدش\s*چی"
    r"|کجا\s*بودیم"
    r"|برگردیم\s+به"
    r"|همون\s+(?:مشکل|موضوع|بحث|کار)"
    r"|\b(?:continue|next\s+step|where\s+were\s+we|go\s+on|keep\s+going)\b",
    re.IGNORECASE | re.UNICODE,
)

# A question, for the unresolved-question field. Only a question *about the
# active task* sets it (see ``observe``), so an unrelated question does not
# overwrite the open question of a task it has nothing to do with.
_QUESTION_TAIL = re.compile(r"[?؟]\s*$")

# The demonstratives and articles a captured topic may open with, stripped so
# «اون مشکل لاگین بات» is stored as «مشکل لاگین بات».
_LEADING_TOPIC = re.compile(
    r"^(?:اون|آن|این|همون|همان|یه|یک|the|a|an)\s+", re.IGNORECASE | re.UNICODE
)

# The speaker's own marker, stripped from the front of a captured topic.
_LEADING_SELF = re.compile(
    r"^(?:من|منم|منو|خودم|ما|I|I'?m|I'?ve|my|myself|we|our)\s+",
    re.IGNORECASE | re.UNICODE,
)

# A topic that carries a link or a handle is not a task description.
_NOT_A_TOPIC_RE = re.compile(r"(?:https?://|www\.|@\w|t\.me/|[\x00-\x1f])", re.IGNORECASE)


def _norm_topic(text: str) -> str:
    """One captured topic, folded to the form it is stored in."""
    value = " ".join(str(text or "").split()).strip(" .,،:;-–—")
    value = _LEADING_SELF.sub("", value).strip(" .,،:;-–—")
    value = _LEADING_TOPIC.sub("", value).strip(" .,،:;-–—")
    return _clip(value, int(config.NEXUS_STATE_VALUE_CHARS))


def _rule_value(match: re.Match) -> str:
    """The captured topic from whichever alternative matched."""
    for name in ("v", "v2", "v3"):
        try:
            got = match.group(name)
        except IndexError:  # this alternative did not capture; try the next
            got = None
        if got:
            return got
    return ""


def read(text: str) -> dict | None:
    """The state transition one message states, or ``None``.

    Deterministic and cheap: a handful of regexes over text the handler already
    holds. Precedence is the requirement, not an accident — a reset is read
    before an activation (an explicit "drop this" beats a new topic), and a
    question is handled by the caller because it needs the current state.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    folded = _fold(raw)
    if _RESET_RE.search(folded):
        return {"transition": TRANSITION_RESET}
    match = _TASK_RE.search(folded)
    if match:
        topic = _norm_topic(_rule_value(match))
        if len(topic) >= 2 and not _NOT_A_TOPIC_RE.search(topic):
            return {
                "transition": TRANSITION_ACTIVATE,
                "topic": topic,
                "goal": topic,
            }
    if _COMPLETE_RE.search(folded):
        return {"transition": TRANSITION_COMPLETE}
    if _CONTINUE_RE.search(folded):
        return {"transition": TRANSITION_CONTINUE}
    return None


def _is_question(text: str) -> bool:
    return bool(_QUESTION_TAIL.search(str(text or "").strip()))


def _overlaps(row: dict, text: str) -> bool:
    """Whether a message shares a meaningful word with the active task."""
    hay = _tokens(f"{row.get('topic', '')} {row.get('goal', '')}")
    if not hay:
        return False
    return bool(hay & _tokens(text))


# ── The read path ─────────────────────────────────────────────────────────
def _stale(row: dict, *, now: int = 0) -> bool:
    """Whether a state is older than its TTL. A stale task is not "current"."""
    ttl = int(config.NEXUS_STATE_TTL)
    if ttl <= 0:
        return False
    stamp = int(now or time.time())
    return stamp - int(row.get("updated_at") or 0) > ttl


def relevant(row: dict, text: str) -> bool:
    """Whether the active state still bears on this message.

    The relevance test is *not* lexical, and that is deliberate: continuation
    is pronominal — «خب الان قدم بعدی چیه؟», «این قسمت رو چطور درست کنیم؟» share
    no word with the topic — so a word-overlap filter would drop exactly the
    continuations State exists to serve. What it does drop is a message that
    explicitly *supersedes* the state: a new task, a completion, or a reset.
    Fresh explicit input wins, so the old state is withheld rather than shown
    beside it. Freshness is the other bound, and it lives in ``current``.
    """
    if not row:
        return False
    change = read(text)
    if not change:
        return True
    transition = change.get("transition")
    if transition in (TRANSITION_RESET, TRANSITION_COMPLETE):
        return False
    if transition == TRANSITION_ACTIVATE:
        # A new task supersedes the shown one unless it is the same task
        # restated, which is a continuation in all but name.
        return people.normalize(change.get("topic", "")) == people.normalize(
            row.get("topic", "")
        )
    return True


def current(
    chat_id: int, user_id: int, *, text: str = "", now: int = 0
) -> dict | None:
    """The active state that should be shown for this message, or ``None``.

    The retrieval reader, and it is a *read*: it returns a row for the caller to
    render, grants nothing, and takes both ids so no caller can ask across
    rooms. A failure returns ``None`` — a context block is never worth failing a
    pass — and the switch being off returns ``None`` rather than raising.
    """
    if not config.NEXUS_STATE_ENABLED:
        return None
    if not chat_id or not user_id:
        return None
    try:
        row = db.state_get(int(chat_id), int(user_id))
    except Exception:  # noqa: BLE001 - a block is never worth a pass
        log.exception("could not read the conversation state")
        return None
    if not row:
        return None
    if _stale(row, now=now):
        return None
    if not relevant(row, text):
        return None
    return row


def render(row: dict | None, *, budget: int = 0) -> str:
    """The active state as one bounded block, or ``""`` when there is nothing.

    Stated as the server's reading of *the interaction*, never as a fact about
    the person, so the model treats it as what this conversation is doing rather
    than as something it knows about a member — the same distinction the memory
    and room blocks make. The budget covers the framing line, so the guarantee
    is ``len(block) <= budget``.
    """
    cap = int(budget or config.NEXUS_STATE_CHARS)
    if cap <= 0 or not row:
        return ""
    header = (
        "The current task in this conversation — the server's reading of what "
        "this interaction is trying to accomplish, not a fact about the person:\n"
    )
    if len(header) >= cap:
        # A shorter frame beats no frame: state with no framing reads as the
        # server asserting something about the person.
        header = "Current conversation state:\n"
    if len(header) >= cap:
        return ""
    topic = " ".join(str(row.get("topic") or "").split())
    goal = " ".join(str(row.get("goal") or "").split())
    question = " ".join(str(row.get("question") or "").split())
    items: list[str] = []
    if topic:
        items.append(f"active topic: {topic}")
    if goal and goal != topic:
        items.append(f"active goal: {goal}")
    if question:
        items.append(f"unresolved question: {question}")
    if str(row.get("status") or "") == STATUS_BLOCKED:
        items.append("status: blocked")
    lines: list[str] = []
    used = len(header)
    for item in items:
        line = f"- {item}\n"
        if used + len(line) > cap:
            room = cap - used
            if room > len("- ") + 8:
                clipped = _clip(item, room - len("- ") - 1)
                if clipped:
                    lines.append(f"- {clipped}\n")
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return header + "".join(lines)


# ── The write path: one transition at a time ──────────────────────────────
def _write(
    chat_id: int,
    user_id: int,
    row: dict | None,
    *,
    topic: str,
    goal: str,
    question: str,
    status: str,
    transition: str,
    message_id: int = 0,
) -> dict | None:
    """Apply one transition under optimistic concurrency.

    The version read with the row is named in the write, so a background worker
    whose read is already stale has its update refused rather than clobbering a
    newer state. Returns the stored row, or ``None`` when the write was refused
    (lost the race, or a duplicate of the same message) — neither of which is an
    error, because both mean a newer or identical state already holds.
    """
    expect = int(row.get("version") or 0) if row else 0
    try:
        return db.state_put(
            int(chat_id),
            int(user_id),
            topic=topic,
            goal=goal,
            question=question,
            status=status,
            transition=transition,
            message_id=int(message_id or 0),
            expect_version=expect,
        )
    except Exception:  # noqa: BLE001 - a state write is never worth a handler
        log.exception("could not write the conversation state")
        return None


def _observe_deterministic(
    chat_id: int, user_id: int, text: str, *, message_id: int = 0
) -> dict | None:
    """Read one message into a transition, then apply it. Never raises."""
    try:
        row = db.state_get(int(chat_id), int(user_id))
    except Exception:  # noqa: BLE001 - a read is never worth a handler
        log.exception("could not read the conversation state before a write")
        return None
    change = read(text)
    # A duplicate delivery — the same message processed twice — is a no-op. The
    # store guards this too, but reporting it here is what lets a caller (and a
    # test) tell "already applied" from "applied just now" without inspecting the
    # version.
    if (
        message_id
        and row
        and int(row.get("message_id") or 0) == int(message_id)
    ):
        return {
            "transition": (change or {}).get("transition") or TRANSITION_UPDATE,
            "applied": False,
            "duplicate": True,
        }
    if change is None:
        # No transition, but a question *about the active task* is the one other
        # event that changes state: it opens (or replaces) the unresolved
        # question. A question with no active task, or about something else,
        # changes nothing.
        if row and _is_question(text) and _overlaps(row, text):
            stored = _write(
                chat_id,
                user_id,
                row,
                topic=str(row.get("topic") or ""),
                goal=str(row.get("goal") or ""),
                question=_clip(text, int(config.NEXUS_STATE_VALUE_CHARS)),
                status=str(row.get("status") or STATUS_ACTIVE),
                transition=TRANSITION_UPDATE,
                message_id=message_id,
            )
            _maybe_prune()
            return _result(TRANSITION_UPDATE, row, stored)
        return None
    transition = change["transition"]
    if transition in (TRANSITION_COMPLETE, TRANSITION_RESET):
        # A finished or abandoned task leaves no active state. The row is
        # cleared rather than marked done, because "what is this conversation
        # trying to accomplish" has no answer once the answer is "nothing".
        try:
            removed = db.state_clear(
                int(chat_id),
                int(user_id),
                expect_version=int(row.get("version") or 0) if row else 0,
            )
        except Exception:  # noqa: BLE001
            log.exception("could not clear the conversation state")
            return None
        if removed:
            _maybe_prune()
            return {"transition": transition, "applied": True, "cleared": True}
        return {"transition": transition, "applied": False, "cleared": False}
    if transition == TRANSITION_CONTINUE:
        if not row:
            return None
        stored = _write(
            chat_id,
            user_id,
            row,
            topic=str(row.get("topic") or ""),
            goal=str(row.get("goal") or ""),
            question=str(row.get("question") or ""),
            status=str(row.get("status") or STATUS_ACTIVE),
            transition=TRANSITION_CONTINUE,
            message_id=message_id,
        )
        _maybe_prune()
        return _result(TRANSITION_CONTINUE, row, stored)
    if transition == TRANSITION_ACTIVATE:
        topic = str(change.get("topic") or "")
        goal = str(change.get("goal") or topic)
        if row and people.normalize(row.get("topic", "")) == people.normalize(topic):
            step = TRANSITION_UPDATE
        elif row:
            step = TRANSITION_REPLACE
        else:
            step = TRANSITION_ACTIVATE
        stored = _write(
            chat_id,
            user_id,
            row,
            topic=topic,
            goal=goal,
            # A new or continued task starts with no open question; the old
            # question belonged to the old task.
            question="",
            status=STATUS_ACTIVE,
            transition=step,
            message_id=message_id,
        )
        _maybe_prune()
        return _result(step, row, stored)
    return None


def _result(transition: str, row: dict | None, stored: dict | None) -> dict:
    """What one applied transition reports, for tests and the benchmark."""
    if stored is not None:
        return {"transition": transition, "applied": True, "state": stored}
    # Refused: either the same message was already applied (idempotent) or a
    # newer write won the race. Both mean a valid state already holds.
    return {"transition": transition, "applied": False}


async def observe(user, chat_id: int, text: str, *, message_id: int = 0) -> dict | None:
    """Learn the state one message states about the interaction. Never raises.

    The single entry point, and it is deliberately **not awaited on the answer
    path**: ``app/main.py`` schedules it as a background task, so a locked
    database or a broken rule can never delay the reply somebody is waiting for.
    It is a coroutine only so it can be scheduled the same way Memory is; it
    awaits nothing, because there is no provider on this path at all.

    Every stage is wrapped, so the worst case is that a state is not learned —
    never that the handler fails.
    """
    if not config.NEXUS_STATE_ENABLED or not config.NEXUS_STATE_AUTO_ENABLED:
        return None
    user_id = int(_get(user, "id", 0) or 0)
    if not user_id or not chat_id or _get(user, "is_bot", False):
        return None
    try:
        return _observe_deterministic(
            int(chat_id), user_id, str(text or ""), message_id=int(message_id or 0)
        )
    except Exception:  # noqa: BLE001 - a state is never worth a handler
        log.exception("state observation failed")
        return None


def _maybe_prune() -> None:
    global _since_prune
    _since_prune += 1
    if _since_prune < PRUNE_EVERY:
        return
    _since_prune = 0
    prune()


def prune() -> int:
    """Apply the age and global bounds. Best effort; never raises."""
    try:
        return db.state_prune(
            keep=max(0, int(config.NEXUS_STATE_MAX)),
            max_age=max(0, int(config.NEXUS_STATE_TTL)),
        )
    except Exception:  # noqa: BLE001
        log.exception("state retention prune failed")
        return 0
