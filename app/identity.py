"""Identity: the one place a name, a handle or a number becomes a Telegram id.

The brief asks for two things that this module exists to keep apart:

* an **internal UUID** — a stable, opaque handle for a person, so operational
  records and answers can refer to somebody without repeating their Telegram
  number everywhere; and
* **deterministic resolution** — turning "فلانی", "@milad", a reply target, a
  numeric id or an internal uuid into the one Telegram user id a privileged
  action will be authorised against.

What this module is not, and the distinction is the whole security story:

* It **grants nothing**. ``app/rbac.py`` is the authority model and it is keyed
  on Telegram user ids only. Nothing here returns a permission, and no caller
  may treat a resolved identity as an authorisation — the resolved id is a
  *target*, and the actor is still re-derived server-side by
  ``app/admin_service.py``.
* It **never guesses**. Resolution is an exact, normalised comparison. When two
  people could be meant the answer is ``ambiguous`` with the candidates
  attached, and the assistant is required to ask. Picking the most likely
  candidate is the single most dangerous thing this module could do, because
  the consequence of being wrong is an action on the wrong person.
* It **never invents a uuid from a Telegram id**. The handle is generated once
  and stored; a derived value would be reversible and would defeat the point of
  an opaque handle.

The internal uuid is deliberately *not* a secret and *not* a credential. It is
a name. Knowing somebody's uuid confers nothing, exactly as knowing their
Telegram id confers nothing — every privileged path still resolves the actor
from the update and asks ``app/rbac.py``.
"""
from __future__ import annotations

import logging
import re

from . import config, db, people, rbac

log = logging.getLogger("guardbot.identity")

# A generated handle is 32 lowercase hex characters — ``uuid4().hex``. The
# pattern is anchored so a numeric Telegram id (which is also a string of
# digits) can never be mistaken for one, and vice versa.
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
# A numeric Telegram user id. Negative ids exist (channels) but never for a
# person, so only positive ids are treated as one.
_ID_RE = re.compile(r"^[0-9]{4,15}$")

# How many aliases and audit rows a single identity view may carry. Bounded for
# the same reason every other read here is: this is context for an answer, not
# a dump of the database into a prompt.
MAX_ALIASES = 12
MAX_AUDIT = 8


def ensure(user_id: int) -> str:
    """Return this user's internal uuid, creating it on first sight. Never raises.

    Called on the observation path — every message the bot receives records a
    person, and this is where that person is given a handle — so it must not be
    able to fail a handler. A failure returns "" and the caller carries on with
    the Telegram id, which is always the authoritative key anyway.
    """
    try:
        row = db.identity_ensure(int(user_id))
    except Exception:  # noqa: BLE001 - an identity write is never worth a crash
        log.exception("could not record an identity")
        return ""
    return str(row.get("uuid") or "")


def uuid_for(user_id: int) -> str:
    """This user's handle, or "" if they have never been seen. Never creates."""
    try:
        row = db.identity_get(int(user_id))
    except Exception:  # noqa: BLE001 - a read must never be fatal
        log.exception("could not read an identity")
        return ""
    return str((row or {}).get("uuid") or "")


def _names_for(user_id: int) -> dict:
    """The name metadata recorded for this user across every room they spoke in.

    Read from ``people``, which stores names and nothing else. The most recent
    row wins for the primary name — Telegram lets a person rename themselves,
    and the latest is what somebody in the room would have seen.
    """
    try:
        rows = [r for r in db.people_rows(limit=0) if int(r.get("user_id") or 0) == int(user_id)]
    except Exception:  # noqa: BLE001
        log.exception("could not read the name metadata for an identity")
        rows = []
    if not rows:
        return {"name": "", "username": "", "aliases": [], "chats": [], "seen": 0}
    latest = rows[0]
    aliases: list[str] = []
    chats: list[int] = []
    for row in rows:
        for part in (row.get("first_name", ""), row.get("last_name", "")):
            cleaned = (part or "").strip()
            if cleaned and cleaned not in aliases:
                aliases.append(cleaned)
        full = " ".join(
            p for p in (row.get("first_name", ""), row.get("last_name", "")) if p
        ).strip()
        if full and full not in aliases:
            aliases.append(full)
        chat_id = int(row.get("chat_id") or 0)
        if chat_id and chat_id not in chats:
            chats.append(chat_id)
    name = " ".join(
        p for p in (latest.get("first_name", ""), latest.get("last_name", "")) if p
    ).strip()
    return {
        "name": name,
        "username": (latest.get("username") or "").strip(),
        "aliases": aliases[:MAX_ALIASES],
        "chats": chats,
        "seen": int(latest.get("message_count") or 0),
        "first_seen": int(latest.get("first_seen") or 0),
        "last_seen": int(latest.get("last_seen") or 0),
    }


def _audit_for(user_id: int, *, chat_id: int = 0) -> list[dict]:
    """A bounded slice of the audit trail this user appears in, either side."""
    try:
        since = 0
        window = int(getattr(config, "ADMIN_CONTEXT_WINDOW", 0) or 0)
        if window > 0:
            import time

            since = int(time.time()) - window
        rows = db.audit_for_user(
            int(user_id),
            chat_id=int(chat_id) or None,
            since=since,
            limit=MAX_AUDIT,
        )
    except Exception:  # noqa: BLE001
        log.exception("could not read the audit trail for an identity")
        return []
    return [
        {
            "at": int(row.get("at") or 0),
            "action": row.get("action", ""),
            "actor_id": int(row.get("actor_id") or 0),
            "target_id": int(row.get("target_id") or 0),
            "outcome": row.get("outcome", ""),
            "interface": row.get("interface", ""),
            "role": row.get("role", ""),
        }
        for row in rows
    ]


def describe(user_id: int, *, chat_id: int = 0) -> dict:
    """One person, as the assistant and an operator may see them.

    Every field here is an *allowlisted* value assembled by the server. There is
    no row copied through, no ``SELECT *``, and therefore no column that can
    leak into an answer by being added to a table later: a field that is not
    named in the return value below does not exist as far as this function's
    callers are concerned.
    """
    user_id = int(user_id)
    if user_id <= 0:
        return {"error": "no user id supplied"}
    principal = rbac.resolve(user_id)
    names = _names_for(user_id)
    identity = db.identity_get(user_id) or {}
    strikes = 0
    if chat_id:
        try:
            strikes = int(db.get_strikes(int(chat_id), user_id) or 0)
        except Exception:  # noqa: BLE001
            strikes = 0
    return {
        "user_id": user_id,
        "uuid": str(identity.get("uuid") or ""),
        "name": names.get("name", ""),
        "username": names.get("username", ""),
        "aliases": names.get("aliases", []),
        "chats_seen": names.get("chats", []),
        "message_count": names.get("seen", 0),
        "first_seen": names.get("first_seen", 0),
        "last_seen": names.get("last_seen", 0),
        "role": principal.role,
        "role_label": principal.label,
        "level": principal.level,
        "is_owner": principal.is_owner,
        "is_admin": principal.is_admin,
        "permissions": sorted(principal.permissions),
        "role_source": principal.source,
        "strikes": strikes,
        "recent_audit": _audit_for(user_id, chat_id=chat_id),
    }


def resolve(query: str, *, chat_id: int = 0) -> dict:
    """Resolve anything a person might have written into one Telegram user id.

    Accepts, in order of how specific the key is:

    * a numeric Telegram user id;
    * an internal uuid;
    * an ``@username`` (or a bare username that the name memory knows);
    * a display name or alias, through :mod:`app.people`.

    Four answers, and the caller is expected to act on each differently:

    * ``ok`` — exactly one person matches. The id is safe to *propose* as a
      target; it is still authorised against the actor before anything happens.
    * ``ambiguous`` — several match. The candidates are returned and the
      assistant must ask. There is no field anywhere that lets it pick one.
    * ``unknown`` — nobody matches. Ask for a reply or an id.
    * ``invalid`` — the query is empty or too short to be anything.

    This is the only public entry point, and it is a thin counter around
    :func:`_resolve`. Counting here rather than at each of the eight returns
    below is what makes the tally complete by construction: a new branch cannot
    be added without being counted. The bump cannot fail the lookup — see
    ``db.identity_resolution_bump``.
    """
    answer = _resolve(query, chat_id=chat_id)
    # Guarded here as well as inside the bump: the lookup is the answer the
    # caller needs and the count is bookkeeping, so a counter that cannot be
    # written must not be able to turn a correct answer into an exception.
    try:
        db.identity_resolution_bump(str(answer.get("status") or "unknown"))
    except Exception:  # noqa: BLE001 - bookkeeping is never worth a failure
        log.exception("could not count an identity resolution")
    return answer


def resolution_line() -> str:
    """One line of how identity lookups have been ending. Counts, never content.

    The signal is the shape of the distribution. A run of ``ambiguous`` means
    names in the group collide and the assistant is being made to ask, which is
    correct but worth seeing; ``unknown`` climbing means people are naming
    somebody the bot has never observed. Neither is derivable from anything
    else stored, which is why the counter exists at all.
    """
    try:
        counts = db.identity_resolution_counts()
    except Exception:  # noqa: BLE001 - a status line is never worth a crash
        log.exception("could not read the identity resolution counts")
        return "identity lookups: unavailable"
    if not counts:
        return "identity lookups: none yet"
    total = sum(counts.values())
    parts = " ".join(f"{k}={counts[k]}" for k in sorted(counts))
    return f"identity lookups: total={total} {parts}"


def _resolve(query: str, *, chat_id: int = 0) -> dict:
    """The resolution itself. See :func:`resolve` for the contract."""
    raw = (query or "").strip()
    if not raw:
        return {"status": "invalid", "query": raw, "reason": "empty"}

    # A uuid first: it is the only key that is unambiguously a handle.
    if _UUID_RE.match(raw.lower()):
        row = db.identity_by_uuid(raw.lower())
        if not row:
            return {"status": "unknown", "query": raw, "match": "uuid"}
        return {
            "status": "ok",
            "query": raw,
            "match": "uuid",
            "identity": describe(int(row["user_id"]), chat_id=chat_id),
        }

    if _ID_RE.match(raw):
        user_id = int(raw)
        known = db.identity_get(user_id) is not None or bool(_names_for(user_id)["chats"])
        if not known:
            # A well-formed id nobody has ever seen. It is still a usable
            # target — Telegram will accept it — but the caller is told it is
            # unseen rather than being handed a confident identity.
            return {
                "status": "ok",
                "query": raw,
                "match": "telegram_id",
                "seen_before": False,
                "identity": describe(user_id, chat_id=chat_id),
            }
        return {
            "status": "ok",
            "query": raw,
            "match": "telegram_id",
            "seen_before": True,
            "identity": describe(user_id, chat_id=chat_id),
        }

    # A username, with or without the @. ``people.resolve`` already treats the
    # username as one of a person's keys, so this is the same exact comparison
    # the name path uses rather than a second, weaker one.
    if raw.startswith("@"):
        handle = raw[1:].strip()
        if not handle:
            return {"status": "invalid", "query": raw, "reason": "empty username"}
        found = people.resolve(handle, chat_id=chat_id)
        if found.get("status") == "ok":
            found = dict(found)
            found["match"] = "username"
            found["identity"] = describe(int(found["user_id"]), chat_id=chat_id)
        return found

    found = people.resolve(raw, chat_id=chat_id)
    if found.get("status") == "ok":
        found = dict(found)
        found["match"] = "name"
        found["identity"] = describe(int(found["user_id"]), chat_id=chat_id)
    return found


def describe_actor(user_id: int) -> dict:
    """The actor's own identity, for the trusted-context block.

    A narrower view than :func:`describe` — the actor does not need their own
    audit trail read back to them, and the block this feeds is rebuilt on every
    turn.
    """
    view = describe(user_id)
    view.pop("recent_audit", None)
    view.pop("chats_seen", None)
    view.pop("aliases", None)
    return view
