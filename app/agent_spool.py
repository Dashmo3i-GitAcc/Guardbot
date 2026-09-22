"""The wire between the container and the host runner: a directory of files.

Why not a socket, and why not the database
------------------------------------------
The two halves of the bridge are a container and a host process, and the only
thing they already share is the bind mount the compose file provides. That is
enough, and it is better than the alternatives:

* **Not the database.** SQLite here runs in its default rollback-journal mode
  with no ``busy_timeout`` (see ``app/db.py``), so a second writer would produce
  ``database is locked`` under exactly the load a coding task creates. The
  container owns the database; the runner owns nothing but files. One writer is
  the property that makes the rest of this safe.
* **Not a socket.** A port would need a listener, a firewall decision and a
  shared secret, and the runner would have to be trusted to enforce all three.
  A directory needs none of them, and its permissions are the filesystem's.
* **Not a database *table*.** The stream is append-only and unbounded, which is
  what a file is for and what a row is not.

The format
----------
One request, one stream, one lock::

    <spool>/requests/<request_id>.json     written by the container, read by the runner
    <spool>/streams/<request_id>.jsonl     appended by the runner, read by the container
    <spool>/locks/<request_id>.lock        created with O_EXCL, held while a run is in flight
    <spool>/control/<request_id>.cancel    created by the container to ask a run to stop

The stream is JSON Lines and **append-only**, and that is the whole of the
restart story. The container records how many lines it has already delivered in
``agent_tasks.progress_offset``; on restart it reads from that line onward and
delivers nothing twice. A line is written with one ``write`` and flushed, so the
only way to see a partial line is a crash mid-write, and :func:`read_lines`
treats a trailing line with no terminating newline as not-yet-written rather
than as data. Nothing is ever rewritten in place, so a reader can never observe
a torn file.

What this module deliberately does not do
-----------------------------------------
No project imports at all — not ``config``, not ``db``. That is not tidiness: the
host runner imports this module and must not thereby open the database, and
``app/db.py`` writes at import time. Keeping this file to the standard library
is what makes it safe for both sides to import.
"""
from __future__ import annotations

import json
import os
import time

# ── Where ─────────────────────────────────────────────────────────────────
# Defaults to the same path ``config.AGENT_SPOOL_DIR`` does. The duplication is
# deliberate and is the same indirection as the repository allowlist: the
# container reads its path from configuration, the runner takes it from the
# environment, and neither can be talked into a different directory by a
# request.
DEFAULT_SPOOL_DIR = "/data/agent"

# What a stream line can be. A closed set, because the reader switches on it and
# an unknown kind would be a line that is silently dropped.
KIND_STARTED = "started"
KIND_PROGRESS = "progress"
KIND_QUESTION = "question"
KIND_RESULT = "result"
KIND_ERROR = "error"
KIND_CANCELLED = "cancelled"
KINDS = (
    KIND_STARTED,
    KIND_PROGRESS,
    KIND_QUESTION,
    KIND_RESULT,
    KIND_ERROR,
    KIND_CANCELLED,
)

# The kinds that end a run. The container uses this to decide whether the lock
# still means anything.
TERMINAL_KINDS = (KIND_RESULT, KIND_ERROR, KIND_CANCELLED)


def spool_dir() -> str:
    """The spool root. From the environment, so both halves can agree."""
    return os.getenv("AGENT_SPOOL_DIR", DEFAULT_SPOOL_DIR)


def _sub(name: str) -> str:
    return os.path.join(spool_dir(), name)


def requests_dir() -> str:
    return _sub("requests")


def streams_dir() -> str:
    return _sub("streams")


def locks_dir() -> str:
    return _sub("locks")


def control_dir() -> str:
    return _sub("control")


def ensure() -> None:
    """Create the four directories. Idempotent, and never raises."""
    for path in (requests_dir(), streams_dir(), locks_dir(), control_dir()):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:  # noqa: PERF203 - a missing spool is reported by the caller
            pass


# ── Names ─────────────────────────────────────────────────────────────────
# A request id reaches these functions from a database row, but the id is
# derived from an actor id and a task text (see ``agent_bridge.new_request_id``),
# so it is *not* trusted to be free of path separators by construction. Every
# name below goes through here first, and a name that is not a plain token is
# refused rather than sanitised — a request id that needed sanitising is one
# this module should not be creating a file for at all.
def safe_id(request_id: str) -> str:
    raw = str(request_id or "")
    if not raw or len(raw) > 64:
        return ""
    for ch in raw:
        if not (ch.isalnum() or ch in "-_"):
            return ""
    return raw


def request_path(request_id: str) -> str:
    name = safe_id(request_id)
    return os.path.join(requests_dir(), name + ".json") if name else ""


def stream_path(request_id: str) -> str:
    name = safe_id(request_id)
    return os.path.join(streams_dir(), name + ".jsonl") if name else ""


def lock_path(request_id: str) -> str:
    name = safe_id(request_id)
    return os.path.join(locks_dir(), name + ".lock") if name else ""


def cancel_path(request_id: str) -> str:
    name = safe_id(request_id)
    return os.path.join(control_dir(), name + ".cancel") if name else ""


# ── The request file ──────────────────────────────────────────────────────
def write_request(request_id: str, payload: dict) -> bool:
    """Publish one request. Atomic, so the runner never reads a half file.

    Written to a temporary name in the same directory and then renamed, which is
    the only way to make a file appear complete on a filesystem that gives no
    other guarantee.
    """
    path = request_path(request_id)
    if not path:
        return False
    ensure()
    tmp = path + f".tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def read_request(request_id: str) -> dict:
    """The published request, or ``{}``. A malformed file reads as ``{}``."""
    path = request_path(request_id)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def pending_requests() -> list[str]:
    """Every request id with a published request file, oldest first."""
    out: list[tuple[float, str]] = []
    try:
        names = os.listdir(requests_dir())
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json") or ".tmp" in name:
            continue
        path = os.path.join(requests_dir(), name)
        try:
            out.append((os.path.getmtime(path), name[: -len(".json")]))
        except OSError:
            continue
    out.sort()
    return [name for _, name in out]


# ── Claiming ──────────────────────────────────────────────────────────────
def claim(request_id: str) -> bool:
    """Take the lock for one request. False when somebody else holds it.

    ``O_CREAT | O_EXCL`` is the primitive: it is atomic on every filesystem this
    runs on, and it needs no lock manager. The pid and the time go inside for an
    operator reading the directory, not for the decision — the decision is the
    successful create.
    """
    path = lock_path(request_id)
    if not path:
        return False
    ensure()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode())
    finally:
        os.close(fd)
    return True


def release(request_id: str) -> None:
    """Drop the lock. Best effort: a stale lock is cleaned by :func:`clear_locks`."""
    path = lock_path(request_id)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def locked(request_id: str) -> bool:
    return bool(lock_path(request_id)) and os.path.exists(lock_path(request_id))


def lock_age(request_id: str) -> float:
    """How long the lock has been held, in seconds. ``0.0`` when there is none."""
    path = lock_path(request_id)
    if not path:
        return 0.0
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return 0.0


def clear_locks(older_than: float = 0.0) -> int:
    """Remove locks older than a bound. Returns how many went.

    Called by the container for tasks that have ended: a run that was killed
    outright cannot release its own lock, and a lock nobody can clear is a task
    that can never be retried.
    """
    removed = 0
    try:
        names = os.listdir(locks_dir())
    except OSError:
        return 0
    for name in names:
        path = os.path.join(locks_dir(), name)
        try:
            if older_than and (time.time() - os.path.getmtime(path)) < older_than:
                continue
            os.unlink(path)
            removed += 1
        except OSError:
            continue
    return removed


# ── The stream ────────────────────────────────────────────────────────────
def append(request_id: str, kind: str, text: str = "", **extra) -> bool:
    """Append one line to a task's stream. Never rewrites, never raises.

    One ``write`` of one line that ends in a newline, so a reader either sees
    the whole line or does not see it yet.
    """
    path = stream_path(request_id)
    if not path or kind not in KINDS:
        return False
    ensure()
    record = {"at": int(time.time()), "kind": kind, "text": str(text or "")}
    for key, value in extra.items():
        if key in ("at", "kind", "text"):
            continue
        record[key] = value
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        return True
    except OSError:
        return False


def read_from(request_id: str, offset: int = 0) -> tuple[list[dict], int]:
    """Stream lines from ``offset`` onward, and where the next read starts.

    Returns ``(records, next_offset)``. The second value is the important one:
    the caller stores it, and it counts *raw lines* rather than records, because
    a blank or unparseable line is consumed by being skipped and must not be
    read again for ever. Returning it from the same read that produced the
    records is what makes the pair atomic — asking :func:`line_count` afterwards
    would race a runner that had appended in between, and the lines it appended
    would be skipped.
    """
    path = stream_path(request_id)
    if not path:
        return [], max(0, int(offset or 0))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except OSError:
        return [], max(0, int(offset or 0))
    start = max(0, int(offset or 0))
    if not body:
        return [], start
    chunks = body.split("\n")
    if not body.endswith("\n"):
        # The last element is a partial write. Dropping it here is what makes
        # "read from offset N" safe to repeat: nothing is consumed until it is
        # whole.
        chunks = chunks[:-1]
    elif chunks and chunks[-1] == "":
        chunks = chunks[:-1]
    out: list[dict] = []
    for index in range(start, len(chunks)):
        chunk = chunks[index].strip()
        if not chunk:
            continue
        try:
            record = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("kind") in KINDS:
            out.append(record)
    return out, max(start, len(chunks))


def read_lines(request_id: str, offset: int = 0) -> list[dict]:
    """The records from :func:`read_from`, for callers that only want them."""
    return read_from(request_id, offset)[0]


def line_count(request_id: str) -> int:
    """How many complete lines the stream holds.

    Counted by reading, not by tracking a size, because the answer is stored in
    the database and has to agree with what :func:`read_lines` will hand back
    after a restart.
    """
    path = stream_path(request_id)
    if not path:
        return 0
    try:
        with open(path, "rb") as handle:
            body = handle.read()
    except OSError:
        return 0
    if not body:
        return 0
    # A trailing fragment with no newline is not a line yet, and is counted the
    # same way the reader counts it — the two must agree or the stored offset
    # would drift past a line that was never delivered.
    return body.count(b"\n")


def last_kind(request_id: str) -> str:
    """The kind of the newest complete line, or ``""``."""
    path = stream_path(request_id)
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            body = handle.read()
    except OSError:
        return ""
    for chunk in reversed(body.split(b"\n")):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            record = json.loads(chunk.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            return str(record.get("kind") or "")
    return ""


def result_text(request_id: str) -> str:
    """The concatenated text of the stream's result lines, or ``""``.

    A run may emit its answer in several ``result`` lines; joining them here
    means the reader never has to decide which one was "the" result.
    """
    parts = [
        str(record.get("text") or "")
        for record in read_lines(request_id)
        if record.get("kind") == KIND_RESULT
    ]
    return "\n".join(p for p in parts if p)


def forget(request_id: str) -> None:
    """Remove a task's files. Called by the retention prune, never by a run."""
    for path in (
        request_path(request_id),
        stream_path(request_id),
        lock_path(request_id),
        cancel_path(request_id),
    ):
        if not path:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Cancellation ──────────────────────────────────────────────────────────
def request_cancel(request_id: str) -> bool:
    """Ask a running task to stop. The runner checks this between turns."""
    path = cancel_path(request_id)
    if not path:
        return False
    ensure()
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())))
        return True
    except OSError:
        return False


def cancel_requested(request_id: str) -> bool:
    return bool(cancel_path(request_id)) and os.path.exists(cancel_path(request_id))
