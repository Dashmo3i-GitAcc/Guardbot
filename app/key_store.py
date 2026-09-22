"""The credentials the owner adds at runtime, and nothing else.

Why this module exists
----------------------
Every credential this deployment uses used to come from the environment. That is
the right default — a secret in a file the process reads at boot is the easiest
thing in the world to audit — but it has one consequence that matters when the
owner is not sitting at the host: giving a workload a new key means editing
``.env`` and restarting the container. The owner asked for the ability to do it
from Telegram instead.

This module is the smallest thing that makes that possible, and it is
deliberately not a second pool. It knows which credentials exist and where they
came from; it knows nothing about health, cooldown, allowance or failover, and
it has no way to make a provider call. ``app/gemini_pool.py`` stays the single
source of truth for state, and it reads this module when it builds a pool.

Why a file, and not a table
---------------------------
The store is a JSON file with mode ``0600`` inside the container's data volume,
next to the SQLite database. Two reasons, and the second is the important one:

* the database is the *operational* record — counters, states, events — and it
  is copied, backed up and inspected freely. A credential must not travel with
  it. ``data/`` is gitignored either way, but a database backup taken by an
  operator for support reasons should not be a keyring.
* SQLite has no way to hold a value the process cannot read back. "Encrypted at
  rest with a key that is also in the environment" is a lock with the key taped
  to it, and inventing a scheme like that is worse than admitting the file is
  the boundary. The boundary here is the filesystem mode plus the container.

So the credential is plaintext on disk, readable by root only, and the database
holds only the ``fingerprint`` and ``masked`` tail the pool already persists.
That is stated plainly rather than dressed up.

What is never written down
--------------------------
The key does not appear in the database, in ``admin_audit``, in a log line, in a
Telegram message or in the dashboard. ``Entry`` hides it from ``repr`` so that a
future ``log.info("%s", entry)`` cannot leak it, and the pool redacts it out of
any provider error text before that text is stored or shown.

Fail-closed
-----------
Every failure mode here refuses rather than guesses. A store that cannot be
parsed is *not* treated as empty: writing over it would silently destroy every
other credential in it, so a read error is raised and the caller keeps the
environment's keys and nothing else. An unmanaged workload cannot be written to
at all, whatever the callback payload says.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("guardbot.keys")

STORE_VERSION = 1
# Runtime slots are prefixed so they can never collide with the environment's
# numeric slots ("1".."20") or its "sharedN" slots.
SLOT_PREFIX = "k"

# The shape a credential may have. Deliberately permissive about the alphabet:
# the deployment's own keys are ``AIza...`` and the short-lived tokens it has
# used are ``AQ.Ab8...``, so a stricter rule would refuse a credential that
# works. What it does insist on is that the text is a single token with no
# whitespace — a pasted line of prose, or a block of two keys, is never mistaken
# for one.
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.]{20,200}$")

_lock = threading.RLock()


class StoreError(Exception):
    """The store could not be read, or could not be written safely."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Entry:
    """One credential the owner added at runtime.

    ``key`` is excluded from ``repr`` on purpose. This object will be logged by
    accident one day, and a dataclass that renders its own secret is a leak
    waiting for a stack trace. ``masked`` and ``fingerprint`` are what the rest
    of the application is allowed to see.
    """

    workload: str
    slot: str
    label: str
    fingerprint: str
    masked: str
    added_at: int
    added_by: int
    key: str = field(default="", repr=False)

    @property
    def source(self) -> str:
        return "runtime"

    def describe(self) -> dict:
        """Everything safe to render. No credential, ever."""
        return {
            "workload": self.workload,
            "slot": self.slot,
            "label": self.label,
            "fingerprint": self.fingerprint,
            "masked": self.masked,
            "source": self.source,
            "added_at": self.added_at,
            "added_by": self.added_by,
        }


# ── Identity ──────────────────────────────────────────────────────────────
def _identify(key: str) -> tuple[str, str]:
    """``(fingerprint, masked)`` for one credential.

    Imported lazily so this module can be imported by the pool without a cycle,
    and so the two definitions of "what identifies a credential" stay in exactly
    one place.
    """
    from . import gemini_pool

    return gemini_pool.fingerprint(key), gemini_pool.mask(key)


def slot_for(key: str) -> str:
    """The stable slot a credential gets.

    Derived from the fingerprint rather than from the order it was added, so
    removing a key and adding it back resumes the state that slot already has
    instead of starting its counters over — and so two entries can never claim
    the same slot by racing.
    """
    fingerprint, _masked = _identify(key)
    return SLOT_PREFIX + fingerprint[:8]


# ── Shape ─────────────────────────────────────────────────────────────────
def normalise(key: str) -> str:
    """Trim a pasted credential. Internal whitespace is left alone on purpose.

    A trailing newline is what a copy-paste actually looks like, so it is
    removed. A space *inside* the token is not trimmed, because a value with a
    space in it is not this credential and silently joining the halves would
    store something the operator never pasted.
    """
    return (key or "").strip()


def looks_like_key(text: str) -> bool:
    """Whether this text could be a credential. Never validates it.

    Used to decide whether a private message is a key being handed over, so it
    must be cheap and must not be clever: the only question is "is this one
    token, of a plausible length, and not a sentence".
    """
    return bool(_KEY_RE.match(normalise(text)))


def is_managed(workload: str) -> bool:
    """Whether this workload's credentials may be changed from Telegram.

    Not a security boundary on its own — the caller still checks the owner — but
    it is the reason a crafted callback cannot name a workload the owner never
    intended to expose here.
    """
    return str(workload) in config.GEMINI_KEY_MANAGED_WORKLOADS


# ── Reading ───────────────────────────────────────────────────────────────
def path() -> str:
    return config.GEMINI_KEY_STORE_PATH


def _read_raw() -> dict:
    target = path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {"version": STORE_VERSION, "keys": []}
    except (OSError, ValueError) as exc:
        # Not "empty". An unreadable store is a store whose contents are
        # unknown, and treating unknown as empty would make the next write
        # destroy every credential in it.
        raise StoreError("corrupt", f"{type(exc).__name__}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("keys"), list):
        raise StoreError("corrupt", "unexpected document shape")
    return raw


def _entries_from(raw: dict) -> list[Entry]:
    out: list[Entry] = []
    for row in raw.get("keys") or []:
        if not isinstance(row, dict):
            log.warning("[keys] skipping a malformed store row")
            continue
        key = normalise(str(row.get("key") or ""))
        workload = str(row.get("workload") or "")
        if not key or not workload:
            log.warning("[keys] skipping an incomplete store row workload=%s", workload)
            continue
        fingerprint, masked = _identify(key)
        out.append(
            Entry(
                workload=workload,
                slot=str(row.get("slot") or slot_for(key)),
                label=str(row.get("label") or f"API {masked}"),
                fingerprint=fingerprint,
                masked=masked,
                added_at=int(row.get("added_at") or 0),
                added_by=int(row.get("added_by") or 0),
                key=key,
            )
        )
    return out


def entries() -> list[Entry]:
    """Every runtime credential, in the order they were added.

    Raises :class:`StoreError` when the store exists but cannot be understood.
    """
    with _lock:
        return _entries_from(_read_raw())


def entries_or_empty() -> tuple[list[Entry], str]:
    """``(entries, error)`` — the shape the pool path wants.

    The pool must keep working when the store does not, so this never raises:
    it reports the failure and hands back nothing, which is the fail-closed
    direction — the environment's keys still load and no invented credential
    appears.
    """
    try:
        return entries(), ""
    except StoreError as exc:
        log.error("[keys] credential store unreadable: %s", exc)
        return [], exc.reason


def slots_for(workload: str) -> list[tuple[str, str]]:
    """``(slot, credential)`` pairs for one workload, for the pool to append.

    Empty for a workload the owner may not manage here, so a store that somehow
    contains a row for one of them still cannot widen that workload's pool.
    """
    if not is_managed(workload):
        return []
    found, _error = entries_or_empty()
    return [(e.slot, e.key) for e in found if e.workload == workload and e.key]


def entry_for(workload: str, slot: str) -> Entry | None:
    for entry in entries():
        if entry.workload == str(workload) and entry.slot == str(slot):
            return entry
    return None


# ── Writing ───────────────────────────────────────────────────────────────
def _write(entries_to_write: list[Entry]) -> None:
    """Replace the store atomically, with the mode set before anything is in it.

    The file is created by ``mkstemp`` and chmodded to ``0600`` while it is
    still empty, then written and renamed over the target. There is therefore no
    instant at which the store exists with the wrong permissions, and no instant
    at which a reader can see a half-written document — the rename is the commit.
    """
    target = path()
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    payload = json.dumps(
        {
            "version": STORE_VERSION,
            "keys": [
                {
                    "workload": e.workload,
                    "slot": e.slot,
                    "label": e.label,
                    "added_at": e.added_at,
                    "added_by": e.added_by,
                    "key": e.key,
                }
                for e in entries_to_write
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    handle_fd, temporary = tempfile.mkstemp(
        dir=directory, prefix=".gemini-keys-", suffix=".tmp"
    )
    try:
        os.fchmod(handle_fd, 0o600)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def add(
    workload: str,
    key: str,
    *,
    label: str = "",
    actor_id: int = 0,
    now: float | None = None,
) -> tuple[Entry, bool]:
    """Store one credential for one workload. Returns ``(entry, created)``.

    Idempotent on the credential itself: adding a key that is already stored for
    the same workload returns the existing entry with ``created`` False rather
    than a duplicate, because two slots holding one key is one Google project and
    one allowance, and the pool would collapse them anyway. The caller needs the
    flag to say "already there" instead of reporting a change that did not
    happen.

    Raises :class:`StoreError` for a workload the owner may not manage here, a
    value that is not a credential, a store that cannot be read, or a workload
    that is already at its ceiling. Every one of those is a refusal rather than a
    partial write.
    """
    workload = str(workload)
    if not is_managed(workload):
        raise StoreError("not_managed", workload)
    clean = normalise(key)
    if not looks_like_key(clean):
        raise StoreError("bad_shape", "not a single token")
    fingerprint, masked = _identify(clean)
    slot = SLOT_PREFIX + fingerprint[:8]
    moment = int(time.time() if now is None else now)
    with _lock:
        found, error = entries_or_empty()
        if error:
            # Writing now would replace a document we could not read, which is
            # how the other credentials in it are lost.
            raise StoreError("corrupt", "refusing to overwrite an unreadable store")
        for entry in found:
            if entry.workload == workload and entry.fingerprint == fingerprint:
                return entry, False
        managed = [e for e in found if e.workload == workload]
        ceiling = max(1, int(config.GEMINI_KEY_MAX_PER_WORKLOAD))
        if len(managed) >= ceiling:
            raise StoreError("too_many", f"{len(managed)}/{ceiling}")
        entry = Entry(
            workload=workload,
            slot=slot,
            label=(label or f"API {masked}"),
            fingerprint=fingerprint,
            masked=masked,
            added_at=moment,
            added_by=int(actor_id),
            key=clean,
        )
        _write([*found, entry])
    # Logged after the write, and without the credential: the masked tail is
    # enough to tell two keys apart in a log, and it cannot be used to call the
    # provider.
    log.info(
        "[keys] credential added workload=%s slot=%s masked=%s by=%s",
        workload,
        slot,
        masked,
        actor_id,
    )
    return entry, True


def remove(workload: str, slot: str, *, actor_id: int = 0) -> Entry | None:
    """Drop one runtime credential. Returns what was removed, or None.

    ``None`` means this slot was never a runtime credential — which is also the
    answer for an environment slot, so a crafted callback cannot use this to
    reach a key that came from ``.env``.
    """
    workload, slot = str(workload), str(slot)
    if not is_managed(workload):
        raise StoreError("not_managed", workload)
    with _lock:
        found, error = entries_or_empty()
        if error:
            raise StoreError("corrupt", "refusing to rewrite an unreadable store")
        keep: list[Entry] = []
        gone: Entry | None = None
        for entry in found:
            if entry.workload == workload and entry.slot == slot:
                gone = entry
            else:
                keep.append(entry)
        if gone is None:
            return None
        _write(keep)
    log.info(
        "[keys] credential removed workload=%s slot=%s masked=%s by=%s",
        workload,
        slot,
        gone.masked,
        actor_id,
    )
    return gone
