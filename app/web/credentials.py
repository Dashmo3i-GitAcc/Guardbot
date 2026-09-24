"""The operator's password, and the epoch that retires the sessions it protects.

Two facts live here, and both are about *change* rather than about login:

* **The hash.** ``DASHBOARD_PASSWORD_HASH`` in ``.env`` is the bootstrap
  credential. It is read once, at process start, and only a restart picks up an
  edit — which is fine for a deployment file and wrong for a button in the
  panel. So a password set from the panel is written here instead, and it takes
  effect on the next login. Precedence is **file → ``.env`` hash → ``.env``
  plaintext**, and ``ops/dashboard_passwd.py`` ``--apply``/``--clear`` moves
  between the two without editing ``.env``.

* **The epoch.** A counter, bumped on every change. It is stamped into each
  session when the session is minted and compared on every read, so a password
  change retires every session that existed before it — the owner's own
  included. That is the point: after changing a password you want to know that
  the *new* password is what gets you in, and a session that survived the change
  proves nothing. It also means a stolen cookie stops working the moment the
  password is changed, which is the main reason to change one.

Why a file rather than a row in the shared database: the SQLite database is
backed up, copied to a laptop and attached to bug reports, and ``.env``
deliberately keeps this credential out of it. A ``dashboard_credentials`` table
would undo that property for no gain. The file is written atomically
(temp + ``os.replace``) and mode ``600``.

Why the reads are synchronous: ``auth.verify_credentials`` and
``auth.read_session`` are called from the request path but are plain functions,
and making them ``async`` would change every call site for a single small read.
The cost is one ``stat`` per request (the file is re-read only when its mtime or
size changes) and one read per login attempt, on a single-operator panel. That
is a deliberate trade, not an oversight.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from app import config

# The epoch a deployment with no credential file reports. Sessions minted under
# it are valid until the first change bumps the counter past it.
INITIAL_EPOCH = 1

# The cached file contents, and the signature (mtime, size) they were read at.
# ``None`` means "not read yet"; a signature of ``None`` means "the file is
# absent".
_cache: dict | None = None
_cache_signature: tuple | None = None


def path() -> str:
    """Where the runtime credential lives. Re-read from config each call.

    Read through ``app.config`` at call time rather than captured at import, so
    a test — or an operator with a non-default ``DASHBOARD_CREDENTIALS_PATH`` —
    is honoured without reimporting the module.
    """
    return config.DASHBOARD_CREDENTIALS_PATH


def _signature() -> tuple | None:
    try:
        stat = os.stat(path())
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read_file() -> dict:
    """The parsed credential file, or an empty dict.

    Every failure mode — absent, unreadable, not JSON, wrong shape — means the
    same thing: this deployment has no panel-set password, so ``.env`` decides.
    A corrupt file must never lock the owner out of their own panel, and it must
    never be treated as a credential either.
    """
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    password_hash = data.get("password_hash")
    if not isinstance(password_hash, str) or not password_hash.startswith("scrypt$"):
        # A shape we do not recognise is not a hash we are willing to check a
        # password against. Refuse it rather than half-trust it.
        return {}
    try:
        epoch = int(data.get("epoch", INITIAL_EPOCH))
    except (TypeError, ValueError):
        epoch = INITIAL_EPOCH
    return {"password_hash": password_hash, "epoch": max(INITIAL_EPOCH, epoch)}


def _state() -> dict:
    """The current credential file contents, re-read only when it changed.

    The cache is keyed on (mtime, size), so the file is parsed once per process
    and once more per edit — including an edit made by
    ``ops/dashboard_passwd.py --apply`` while the dashboard is running, which is
    exactly the case a restart-only store would get wrong.
    """
    global _cache, _cache_signature
    signature = _signature()
    if signature != _cache_signature:
        _cache = _read_file()
        _cache_signature = signature
    return _cache or {}


def current_hash() -> str:
    """The panel-set hash, or ``''`` when the password still comes from ``.env``."""
    return _state().get("password_hash", "")


def current_epoch() -> int:
    """The epoch sessions are minted under and checked against."""
    return int(_state().get("epoch", INITIAL_EPOCH))


def is_file_managed() -> bool:
    """Whether a password set from the panel is currently in force."""
    return bool(current_hash())


def set_password_hash(password_hash: str) -> int:
    """Store a new hash, bump the epoch, and return the new epoch.

    Atomic: written to a temporary file in the same directory, flushed and
    ``fsync``ed, then ``os.replace``d over the target. A crash mid-write
    therefore leaves either the old credential or the new one, never a truncated
    file — which matters because the failure mode of a truncated credential file
    is an owner who cannot log in.
    """
    if not password_hash.startswith("scrypt$"):
        raise ValueError("password_hash must be a scrypt$... string")

    epoch = current_epoch() + 1
    payload = {
        "password_hash": password_hash,
        "epoch": epoch,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    target = path()
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)

    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".credentials-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

    # Refresh the cache in this process so the change is in force before the
    # next request rather than after the next stat.
    global _cache, _cache_signature
    _cache = {"password_hash": password_hash, "epoch": epoch}
    _cache_signature = _signature()
    return epoch


def clear() -> bool:
    """Remove the panel-set password, handing control back to ``.env``.

    Returns whether a file was actually removed. The in-process cache is
    refreshed either way, so ``ops/dashboard_passwd.py --clear`` is visible to a
    running dashboard without a restart.
    """
    global _cache, _cache_signature
    removed = False
    try:
        os.unlink(path())
        removed = True
    except OSError:
        pass
    _cache = None
    _cache_signature = None
    return removed


def reset_cache_for_tests() -> None:
    """Forget the cached read. Tests point the path at a temp file."""
    global _cache, _cache_signature
    _cache = None
    _cache_signature = None


__all__ = [
    "INITIAL_EPOCH",
    "clear",
    "current_epoch",
    "current_hash",
    "is_file_managed",
    "path",
    "reset_cache_for_tests",
    "set_password_hash",
]
