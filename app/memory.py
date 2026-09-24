"""Long-term user memory: what the server may remember about one person.

The brief's goal is that Nexus knows something durable about a member it has not
met this hour — a preference, a role they hold elsewhere, a fact they asked to be
remembered by. That is a different thing from everything already stored:

* the **room window** (``group_messages``) is a recent view of a conversation,
  kept for an hour and thrown away;
* **Awareness** is what the room is doing, summarised per chat;
* **Intent** is what one message is trying to do;
* **identity memory** (``app/people``) is a name and a message *count*.

None of them answers "what durable thing do I know about this person". So this
module owns one narrow thing: a bounded set of remembered clauses, keyed by
``(chat_id, user_id)``.

Four rules shape it, and each is a refusal.

**It is explicit-only.** A memory is written when, and only when, a person asks
to be remembered — «یادت باشه من ...». The clause they typed is stored, bounded
and verbatim. Nothing is inferred from ordinary conversation, because inferring a
durable fact from a passing sentence is exactly the guessing this codebase
refuses everywhere else, and because doing it well would need a model call the
evidence rule forbids on the ordinary path. A memory the server *guessed* is a
memory that can be wrong about a real person, and being wrong about a real person
is the failure this project is built to avoid.

**It grants nothing.** A memory is a sentence for the model to read, never a
permission, an authorisation or a gate. There is no function here that returns an
authority, and nothing in ``app/rbac.py`` or ``app/admin_service.py`` imports this
module. "یادت باشه من ادمینم" makes Nexus *say* the person said so; it does not
make the server believe it, because authority is resolved from the Telegram id and
from nothing a person typed.

**It is bounded, and by measurement.** ``NEXUS_MEMORY_MAX_PER_USER`` is 30, chosen
from the benchmark recorded in the roadmap rather than from the brief's "20–50":
storage is 208 bytes a row, so the ceiling that actually matters is not disk (30
items across 3000 members is 17.9 MB against a 200 MB budget) but how much can
ever be *read* — the retrieval block surfaces at most about four items — so 30
leaves a wide recall margin while keeping the table a fact set rather than a log.
Retention is applied on the observation path, because this process has no
scheduler.

**It never crosses a boundary.** The key is ``(chat_id, user_id)``, so a group
cannot inherit another group's memory and a private-chat memory can never render
in a group. Isolation is by construction — there is no code path that reads a
person's memory without naming the room — rather than by a check that could be
forgotten.

The write is a deterministic trigger match over the message the person typed, in
the same "evidence, never authority" shape as the other readers. The clause is
stored as the person wrote it: no normalisation is applied to the *value*, because
a remembered sentence is theirs and folding it would edit what they asked to keep.
"""
from __future__ import annotations

import hashlib
import logging
import re

from . import config, db, people

log = logging.getLogger("guardbot.memory")

# The one category this version writes. Named rather than implied so a later
# version can add categories without a migration: the column already exists.
CATEGORY_EXPLICIT = "explicit"

# Where a row came from. Kept as its own column so a reviewer can always tell a
# clause the person typed from anything a future version might derive, and so a
# future "only trust what they said" filter is a ``WHERE`` clause rather than a
# rewrite.
SOURCE_EXPLICIT = "explicit"

# Below this a clause is not a memory. «باشه» after a trigger is an acknowledgement,
# not a fact, and storing it would fill the table with noise.
MIN_CLAUSE_CHARS = 3

# Every how many recordings the whole-table retention bounds are applied. The
# per-person bound runs on every write — it is the indexed, cheap statement — but
# the age and global bounds are whole-table scans, so they run rarely. Same shape
# and same reason as ``app/people.PRUNE_EVERY``.
PRUNE_EVERY = 200
_since_prune = 0

# The explicit request to be remembered, as the person might type it. Written as
# patterns rather than plain strings because Persian is typed with two letters for
# the same sound («ی»/«ي») and the trigger must match either. Each alternative is
# anchored on a word boundary by the surrounding ``\b``-like behaviour of the
# Persian letter classes; English triggers carry ``\b`` explicitly.
_TRIGGER_PATTERNS = (
    r"[یي]ادت\s+باشه",
    r"[یي]ادت\s+بمونه",
    r"[یي]ادت\s+هست",
    r"به\s+[یي]اد\s+داشته?\s+باش",
    r"به\s+[یي]اد\s+بسپار",
    r"[یي]ادداشت\s+کن",
    r"\bremember\s+that\b",
    r"\bremember\s*:",
    r"\bnote\s+that\b",
    r"\bkeep\s+in\s+mind\b",
)
_TRIGGER_RE = re.compile(
    "|".join(f"(?:{pattern})" for pattern in _TRIGGER_PATTERNS),
    re.IGNORECASE | re.UNICODE,
)

# Punctuation and whitespace a clause may open with once the trigger is removed.
# «یادت باشه: من ...» and «یادت باشه که من ...» both leave the fact itself.
_LEADING = re.compile(r"^[\s:،,.;؛\-–—]+")
# Persian's «که» is a complementiser, not part of the fact: «یادت باشه که من ...».
# Stripped only when it is the first token, so a fact that legitimately starts
# with the word keeps it.
_LEADING_KEH = re.compile(r"^که\s+")


def reset_state() -> None:
    """Forget the prune counter. For tests."""
    global _since_prune
    _since_prune = 0


def _clip(text: str, cap: int) -> str:
    """Bound one clause to ``cap`` characters, on a word boundary where there is
    one. A memory is short by design; this is the guard that keeps a person from
    writing an essay into a context block."""
    text = " ".join(str(text or "").split())
    if cap <= 0 or len(text) <= cap:
        return text
    cut = text.rfind(" ", 0, max(1, cap - 1))
    if cut <= 0:
        cut = max(1, cap - 1)
    return text[:cut].rstrip()


def extract(text: str) -> dict | None:
    """The clause a person explicitly asked to be remembered, or ``None``.

    Deterministic and conservative: no trigger means no memory, and a trigger
    followed by nothing substantial means no memory. Returns a candidate dict
    rather than writing, so the decision and the storage can be tested apart.
    """
    if not text:
        return None
    match = _TRIGGER_RE.search(str(text))
    if not match:
        return None
    rest = str(text)[match.end():]
    rest = _LEADING.sub("", rest)
    rest = _LEADING_KEH.sub("", rest)
    value = _clip(rest, int(config.NEXUS_MEMORY_VALUE_CHARS))
    if len(value) < MIN_CLAUSE_CHARS:
        return None
    return {"category": CATEGORY_EXPLICIT, "value": value}


def key_for(category: str, value: str) -> str:
    """A stable key for a clause, so restating it updates one row.

    Derived from the normalised clause and not from a timestamp, which is what
    makes the write an upsert rather than a growing log: saying the same thing
    twice leaves one memory with a newer ``updated_at``. ``people.normalize`` is
    reused so «یادت باشه» folded one way and the other produce the same key.
    """
    material = f"{category}\x00{people.normalize(value)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _get(obj, key: str, default=""):
    """One field from a ``User`` object or a dict, without assuming which."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def remember(user, chat_id: int, text: str) -> bool:
    """Record a person's explicit request to be remembered. Never raises.

    Called from the observation path for every group message, beside
    ``people.remember``. It is a regex over text the handler already holds, so an
    ordinary message that asks for nothing costs one failed match and no write.
    A missing id, a bot, or the switch being off is a no-op rather than an error:
    a memory is never worth failing a handler over.
    """
    if not config.NEXUS_MEMORY_ENABLED:
        return False
    user_id = int(_get(user, "id", 0) or 0)
    if not user_id or not chat_id:
        return False
    if getattr(user, "is_bot", False):
        return False
    found = extract(text)
    if not found:
        return False
    try:
        db.memory_remember(
            chat_id,
            user_id,
            key_for(found["category"], found["value"]),
            category=found["category"],
            value=found["value"],
            source=SOURCE_EXPLICIT,
            confidence=1.0,
        )
        # The per-person bound, right where it is needed and nowhere else: this
        # is the indexed delete (0.02 ms measured), and pruning only the person
        # who just overflowed keeps it off the whole-table path.
        db.memory_prune_user(
            chat_id, user_id, keep=int(config.NEXUS_MEMORY_MAX_PER_USER)
        )
    except Exception:  # noqa: BLE001 - never the reason a handler fails
        log.exception("could not record a memory")
        return False
    _maybe_prune()
    return True


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
        return db.memory_prune(
            keep=max(0, int(config.NEXUS_MEMORY_MAX)),
            max_age=max(0, int(config.NEXUS_MEMORY_RETENTION)),
        )
    except Exception:  # noqa: BLE001
        log.exception("memory retention prune failed")
        return 0


def about(chat_id: int, user_id: int, *, limit: int = 0) -> list[dict]:
    """One person's memories in one room, most recently updated first.

    The retrieval reader, and it is a *read*: it returns rows for the caller to
    render, grants nothing, and takes both ids so no caller can ask across rooms.
    A failure returns an empty list — a context block is never worth failing a
    pass — and the switch being off returns empty rather than raising.
    """
    if not config.NEXUS_MEMORY_ENABLED:
        return []
    if not chat_id or not user_id:
        return []
    limit = int(limit or config.NEXUS_MEMORY_ITEMS)
    if limit <= 0:
        return []
    try:
        return db.memory_for(int(chat_id), int(user_id), limit=limit)
    except Exception:  # noqa: BLE001 - a block is never worth a pass
        log.exception("could not read a person's memory")
        return []


def render(rows: list[dict], *, budget: int = 0) -> str:
    """The memories as one bounded block, or ``""`` when there is nothing.

    Stated as what the person *asked to be remembered*, never as a fact the
    server asserts, so the model treats it as something a member said rather than
    as a statement from the server — the same distinction the room block makes.

    The budget covers the framing line as well as the memories, so the guarantee
    is ``len(block) <= budget``. A memory that does not fit the room left is
    clipped rather than dropped: losing the whole block because the header is
    large would turn a bounded block into a missing one.
    """
    cap = int(budget or config.NEXUS_MEMORY_CHARS)
    if cap <= 0:
        return ""
    header = "What this person asked to be remembered (their words, not the server's):\n"
    if len(header) >= cap:
        # A shorter frame beats no frame: a memory with no framing reads as the
        # server's own claim rather than as something a person said.
        header = "Remembered (their words):\n"
    if len(header) >= cap:
        return ""
    lines: list[str] = []
    used = len(header)
    for row in rows or []:
        value = " ".join(str(row.get("value") or "").split())
        if not value:
            continue
        line = f"- {value}\n"
        if used + len(line) > cap:
            room = cap - used
            if room > len("- ") + 8:
                text = _clip(value, room - len("- ") - 1)
                if text:
                    lines.append(f"- {text}\n")
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return header + "".join(lines)
