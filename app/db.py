"""Tiny SQLite store: strikes (confirmed moderation actions) and captchas."""
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER, user_id INTEGER,
            strikes INTEGER DEFAULT 0,
            first_seen INTEGER,
            PRIMARY KEY (chat_id, user_id))"""
    )
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS captchas (
            chat_id INTEGER, user_id INTEGER,
            message_id INTEGER, deadline INTEGER,
            PRIMARY KEY (chat_id, user_id))"""
    )
    _conn.commit()


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def _ensure_user(chat_id: int, user_id: int) -> None:
    _exec(
        "INSERT OR IGNORE INTO users (chat_id, user_id, first_seen) VALUES (?,?,?)",
        (chat_id, user_id, int(time.time())),
    )


def add_strike(chat_id: int, user_id: int) -> int:
    """Record a confirmed moderation action. Only call after a successful delete."""
    _ensure_user(chat_id, user_id)
    _exec(
        "UPDATE users SET strikes = strikes + 1 WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    return get_strikes(chat_id, user_id)


def get_strikes(chat_id: int, user_id: int) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT strikes FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return row[0] if row else 0


# ---- captcha ----
def add_captcha(chat_id: int, user_id: int, message_id: int, deadline: int) -> None:
    _exec(
        "INSERT OR REPLACE INTO captchas VALUES (?,?,?,?)",
        (chat_id, user_id, message_id, deadline),
    )


def get_captcha(chat_id: int, user_id: int):
    with _lock:
        return _conn.execute(
            "SELECT message_id, deadline FROM captchas WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()


def remove_captcha(chat_id: int, user_id: int) -> None:
    _exec("DELETE FROM captchas WHERE chat_id=? AND user_id=?", (chat_id, user_id))


def expired_captchas(now: int):
    with _lock:
        return _conn.execute(
            "SELECT chat_id, user_id, message_id FROM captchas WHERE deadline<=?",
            (now,),
        ).fetchall()
