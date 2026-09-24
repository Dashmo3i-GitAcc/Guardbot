"""The room allowlist: which Telegram groups this deployment is authorized to serve.

This is the **room** boundary, and it is the only thing that decides whether a
group is served at all. A room is authorized because it has an enabled row in
``authorized_groups`` (``app/db.py``) and for no other reason: being added to a
group, being made an administrator in it, its title, a member's display name, or
anything anybody writes are Telegram facts and claims, and none of them is
application authorization.

Three properties matter, and each is a decision rather than an accident:

**The database is the source of truth.** ``GROUP_IDS`` is the bootstrap, not the
rule: on the first boot against an empty table it seeds the allowlist, so an
existing deployment keeps its rooms with no downtime. After that the table is
authoritative, which is what makes revocation possible — an environment variable
cannot be edited by an administrator at runtime.

**The seed runs once.** It is guarded by "the table has ever held a row", not by
"the table is currently empty". Revocation is a soft disable that keeps the row,
so a room the owner turned off can never be resurrected by a restart.

**It is fail-closed.** A read that cannot be answered is "no rooms", never "all
rooms". The boundary is enforced before any Chat/AI work, so an unauthorized
room produces no model call, no identity write and no awareness capture.

The room boundary and the *speaker* boundary are separate. This module answers
"is this room ours"; it says nothing about who in the room may be answered, and
an authorized room is open to every member.
"""
from __future__ import annotations

import logging

from . import config, db

log = logging.getLogger("guardbot.groups")

# The in-process cache. ``None`` means "not read yet" and is not the same fact as
# "no rooms" — ``load()`` turns one into the other, and it runs at startup before
# any handler can ask. A write through this module clears it, so a revocation
# takes effect on the very next message rather than on the next restart.
_ids: frozenset[int] | None = None


def reset_state() -> None:
    """Forget the cached allowlist, so the next read comes from the database."""
    global _ids
    _ids = None


def seed_if_empty() -> int:
    """Seed the allowlist from ``GROUP_IDS`` if it has never held a row.

    Returns how many rooms were seeded. The guard is "ever held a row", not
    "currently enabled": a soft-revoked room keeps its row, so this can never
    re-authorize a room the owner removed.
    """
    try:
        if db.authorized_group_any():
            return 0
    except Exception:  # noqa: BLE001 - a failed guard must not seed blindly
        log.exception("could not check whether the group allowlist is seeded")
        return 0
    seeded = 0
    for chat_id in config.GROUP_IDS:
        try:
            db.authorized_group_set(
                int(chat_id), enabled=True, added_by=0, note="seeded from GROUP_IDS"
            )
            seeded += 1
        except Exception:  # noqa: BLE001 - one bad id must not stop the rest
            log.exception("could not seed group %s from GROUP_IDS", chat_id)
    if seeded:
        log.info("group allowlist seeded from GROUP_IDS: %d room(s)", seeded)
    return seeded


def load() -> frozenset[int]:
    """Read the enabled rooms into the cache, seeding first if needed.

    An unreadable table is treated as **no rooms**, because the boundary is
    fail-closed: a database error must never widen who is served.
    """
    global _ids
    seed_if_empty()
    try:
        ids = frozenset(int(c) for c in db.authorized_group_ids())
    except Exception:  # noqa: BLE001 - a failed read is "no rooms", not "all"
        log.exception("could not read the group allowlist; serving no rooms")
        ids = frozenset()
    _ids = ids
    return _ids


def all_ids() -> tuple[int, ...]:
    """Every enabled room, sorted. For status, logs and the visibility report."""
    if _ids is None:
        load()
    return tuple(sorted(_ids or ()))


def count() -> int:
    return len(all_ids())


def is_authorized(chat_id: int) -> bool:
    """Whether this room is authorized. The room boundary, read on every message."""
    if _ids is None:
        load()
    try:
        return int(chat_id) in (_ids or frozenset())
    except (TypeError, ValueError):
        return False


def register(
    chat_id: int, *, actor_id: int = 0, interface: str = "", title: str = ""
) -> bool:
    """Authorize a room. Idempotent; a re-register of a live room changes nothing.

    Writes the allowlist row and clears the cache so the room is served on the
    next message. It performs **no** permission check: authority lives in exactly
    one place, ``app/admin_service.execute``, which re-resolves the actor from
    their Telegram id and asks ``app/rbac.py``. This function is the mechanism;
    the service is the boundary.
    """
    global _ids
    try:
        db.authorized_group_set(
            int(chat_id), enabled=True, added_by=int(actor_id), title=str(title)[:200]
        )
    except Exception:  # noqa: BLE001 - the caller reports the failure
        log.exception("could not register group %s", chat_id)
        return False
    _ids = None
    log.info(
        "group registered chat=%s by=%s via=%s", chat_id, actor_id or "-", interface or "-"
    )
    return True


def revoke(chat_id: int, *, actor_id: int = 0, interface: str = "") -> bool:
    """Soft-revoke a room. Returns whether a live row was actually disabled.

    Never a delete: the row is the room's tenant record and its audit metadata,
    and a vanished row would let the one-time seed resurrect the room. Like
    :func:`register` it performs no permission check — the service is the
    boundary.
    """
    global _ids
    try:
        changed = db.authorized_group_disable(int(chat_id), revoked_by=int(actor_id))
    except Exception:  # noqa: BLE001 - the caller reports the failure
        log.exception("could not revoke group %s", chat_id)
        return False
    _ids = None
    if changed:
        log.info(
            "group revoked chat=%s by=%s via=%s",
            chat_id,
            actor_id or "-",
            interface or "-",
        )
    return bool(changed)


def list_rows(*, enabled_only: bool = False) -> list[dict]:
    """Every registered room (or only the enabled ones), newest first."""
    try:
        return db.authorized_group_list(enabled_only=enabled_only)
    except Exception:  # noqa: BLE001 - a listing is never worth a crash
        log.exception("could not list the authorized groups")
        return []
