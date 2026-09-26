"""The archive: a dedicated SQLite store, separate from the production database.

Why a second database and not a table in `guardbot.db`:

* **Isolation of failure.** The archive's writes are batched and dispatched from
  a background worker. If they slow down, fill the disk or corrupt, the worst
  outcome must be that evidence is lost — never that a person's reply is late or
  that the production database is locked behind a telemetry write.
* **Isolation of contention.** The production database serialises every write on
  one process-wide lock, on the event loop, because a group message costs eight
  commits. Sharing that lock with a high-volume append stream would put
  observation directly in the response path.
* **Isolation of blast radius.** Deleting or vacuuming the archive is an
  operator action with no effect on Nexus's own state.

It is still SQLite with WAL, because that is the locally appropriate durable
store here: append-oriented, crash-safe, one file to back up, and no server to
run. `synchronous=NORMAL` is the same trade the production database documents —
a power cut can roll back the last commits but cannot corrupt the file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

from .. import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def path() -> str:
    return config.OBSERVE_DB_PATH


def directory() -> str:
    return config.OBSERVE_PATH


def subdir(name: str) -> str:
    """A directory beside the archive (audio, reports, incidents), created lazily."""
    target = os.path.join(directory(), name)
    os.makedirs(target, exist_ok=True)
    return target


def connect() -> sqlite3.Connection:
    """Open the archive and set its pragmas. Never runs the production schema."""
    global _conn
    if _conn is not None:
        return _conn
    os.makedirs(os.path.dirname(path()) or ".", exist_ok=True)
    _conn = sqlite3.connect(path(), check_same_thread=False)
    # WAL keeps a reader from blocking the writer, which matters because the
    # CLI and an investigation both read while the bot is writing.
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    # The archive has two writers in the worst case — the bot and an operator's
    # `cleanup` — so a short wait beats an immediate "database is locked".
    _conn.execute("PRAGMA busy_timeout=4000")
    return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None


def init() -> None:
    """Create the archive's own tables. Idempotent, additive, never destructive."""
    conn = connect()
    with _lock:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                kind TEXT NOT NULL,
                deployment_id TEXT NOT NULL DEFAULT '',
                process_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                conversation_id TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER NOT NULL DEFAULT 0,
                event TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL DEFAULT '{}',
                ok INTEGER NOT NULL DEFAULT 1,
                error TEXT NOT NULL DEFAULT '',
                duration_ms REAL NOT NULL DEFAULT 0)"""
        )
        for index in (
            "CREATE INDEX IF NOT EXISTS idx_events_at ON events(at)",
            "CREATE INDEX IF NOT EXISTS idx_events_kind_at ON events(kind, at)",
            "CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id, at)",
            "CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_id, at)",
            "CREATE INDEX IF NOT EXISTS idx_events_msg ON events(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_fail ON events(ok, at)",
        ):
            conn.execute(index)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL DEFAULT '',
                conversation_id TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT '',
                deployment_id TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                inbound TEXT NOT NULL DEFAULT '',
                outbound TEXT NOT NULL DEFAULT '',
                duration_ms REAL NOT NULL DEFAULT 0)"""
        )
        for index in (
            "CREATE INDEX IF NOT EXISTS idx_turns_started ON turns(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_turns_chat ON turns(chat_id, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_turns_outcome ON turns(outcome, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_turns_msg ON turns(message_id)",
        ):
            conn.execute(index)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS deployments (
                deployment_id TEXT PRIMARY KEY,
                at REAL NOT NULL,
                sha TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                pid TEXT NOT NULL DEFAULT '')"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                window_seconds INTEGER NOT NULL DEFAULT 0,
                path TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '{}')"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_at ON reports(at)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '')"""
        )
        conn.commit()


# ── Writes ────────────────────────────────────────────────────────────────
def insert_events(rows: Iterable[dict[str, Any]]) -> int:
    """Append a batch. One transaction, one commit, never a partial batch."""
    batch = list(rows)
    if not batch:
        return 0
    conn = connect()
    with _lock:
        conn.executemany(
            """INSERT INTO events (
                at, kind, deployment_id, process_id, turn_id, trace_id,
                conversation_id, chat_id, user_id, message_id, event, text,
                data, ok, error, duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    row["at"], row["kind"], row["deployment_id"], row["process_id"],
                    row["turn_id"], row["trace_id"], row["conversation_id"],
                    row["chat_id"], row["user_id"], row["message_id"],
                    row["event"], row["text"], row["data"], row["ok"],
                    row["error"], row["duration_ms"],
                )
                for row in batch
            ],
        )
        conn.commit()
    return len(batch)


def open_turn(row: dict[str, Any]) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            """INSERT OR REPLACE INTO turns (
                turn_id, trace_id, conversation_id, chat_id, user_id, message_id,
                kind, deployment_id, started_at, ended_at, outcome, reason,
                inbound, outbound, duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["turn_id"], row["trace_id"], row["conversation_id"],
                row["chat_id"], row["user_id"], row["message_id"], row["kind"],
                row["deployment_id"], row["started_at"], row.get("ended_at", 0.0),
                row.get("outcome", ""), row.get("reason", ""),
                row.get("inbound", ""), row.get("outbound", ""),
                row.get("duration_ms", 0.0),
            ),
        )
        conn.commit()


def close_turn(
    turn_id: str, *, ended_at: float, outcome: str, reason: str, outbound: str,
    duration_ms: float,
) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            """UPDATE turns SET ended_at=?, outcome=?, reason=?, outbound=?,
               duration_ms=? WHERE turn_id=?""",
            (ended_at, outcome, reason, outbound, duration_ms, turn_id),
        )
        conn.commit()


def record_deployment(
    deployment_id: str, *, sha: str, image: str, note: str, pid: str
) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            """INSERT INTO deployments (deployment_id, at, sha, image, note, pid)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(deployment_id) DO UPDATE SET
                 at=excluded.at, image=excluded.image, note=excluded.note,
                 pid=excluded.pid""",
            (deployment_id, time.time(), sha, image, note, pid),
        )
        conn.commit()


def record_report(*, window_seconds: int, path: str, summary: dict) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO reports (at, window_seconds, path, summary) VALUES (?,?,?,?)",
            (time.time(), int(window_seconds), path, json.dumps(summary, ensure_ascii=False)),
        )
        conn.commit()


def set_meta(key: str, value: str) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def get_meta(key: str, default: str = "") -> str:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# ── Retention and capacity ────────────────────────────────────────────────
def prune(cutoff: float) -> dict[str, int]:
    """Delete events and turns older than `cutoff`. Reports what it removed.

    Deployment markers are deliberately **not** pruned: they are a handful of
    rows and they are the anchor that lets a later investigation ask "did this
    start after deployment X" about a window that has already aged out.
    """
    conn = connect()
    with _lock:
        events = conn.execute("DELETE FROM events WHERE at < ?", (cutoff,)).rowcount
        turns = conn.execute("DELETE FROM turns WHERE started_at < ?", (cutoff,)).rowcount
        conn.commit()
    return {"events": max(0, events), "turns": max(0, turns)}


def counts() -> dict[str, int]:
    conn = connect()
    with _lock:
        out = {}
        for table in ("events", "turns", "deployments", "reports"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:  # noqa: BLE001
                out[table] = -1
    return out


def size_bytes() -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path() + suffix)
        except OSError:
            pass
    return total


def oldest() -> float:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT MIN(at) FROM events").fetchone()
    return float(row[0]) if row and row[0] else 0.0


def newest() -> float:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT MAX(at) FROM events").fetchone()
    return float(row[0]) if row and row[0] else 0.0


# ── Reads (used by the query layer) ───────────────────────────────────────
def rows(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql, args).fetchall())
        finally:
            conn.row_factory = None


def one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    found = rows(sql, args)
    return found[0] if found else None


def vacuum() -> None:
    conn = connect()
    with _lock:
        conn.execute("VACUUM")
        conn.commit()
