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
    # When we last offered someone a VPN test, and what came of it. Kept in the
    # database rather than in memory on purpose: a restart must not hand the
    # same person a second invitation, and the group must not fill up with
    # repeated offers.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS intent_offers (
            chat_id INTEGER, user_id INTEGER,
            last_offered INTEGER, last_reason TEXT,
            PRIMARY KEY (chat_id, user_id))"""
    )
    # How many Gemini calls we have spent on a given API day. Persisted for the
    # same reason the cooldown is: the container restarts on every deploy, and a
    # daily quota that resets with the process is not a quota.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_usage (
            day TEXT PRIMARY KEY,
            calls INTEGER NOT NULL DEFAULT 0,
            relevant INTEGER NOT NULL DEFAULT 0,
            irrelevant INTEGER NOT NULL DEFAULT 0,
            malformed INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0)"""
    )
    # The conversational AI's history, and its own counters.
    #
    # Both are deliberately separate from the acquisition side. The two
    # workloads have different quotas on different keys, and the requirement is
    # that neither can exhaust the other — which is only true if they cannot
    # read, let alone increment, each other's counters.
    #
    # History is per (chat, user) so one person's conversation can never be
    # shown to another, and a group conversation is isolated from a private one
    # because the chat_id differs.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            at INTEGER NOT NULL)"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_turn "
        "ON chat_messages(chat_id, user_id, id)"
    )
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_usage (
            day TEXT PRIMARY KEY,
            calls INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            malformed INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0)"""
    )
    # The moderation workload's counters, and the transcription workload's.
    # Two more separate tables rather than two more columns, for the same reason
    # the chat table is separate: four workloads with four keys and four
    # allowances, and a shared counter would let one of them read — or exhaust —
    # another's budget.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS moderation_usage (
            day TEXT PRIMARY KEY,
            calls INTEGER NOT NULL DEFAULT 0,
            flagged INTEGER NOT NULL DEFAULT 0,
            allowed INTEGER NOT NULL DEFAULT 0,
            malformed INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0)"""
    )
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS transcript_usage (
            day TEXT PRIMARY KEY,
            calls INTEGER NOT NULL DEFAULT 0,
            transcripts INTEGER NOT NULL DEFAULT 0,
            malformed INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0)"""
    )
    # Who is allowed to do what, at the application level.
    #
    # Global rather than per-chat, because the authority this models is
    # "administrator of this bot" and the bot protects several groups. A
    # per-chat table would let the same person be a senior admin in one room and
    # a stranger in another, which is not a distinction the owner asked for and
    # is one more thing to get wrong.
    #
    # `permissions` is a comma-separated list of application permission keys,
    # validated against app/rbac.py's vocabulary on both write and read: an
    # unknown key is dropped, never honoured. Stored as text rather than a join
    # table because the set is small, read on every administrative action, and
    # the whole row is what the audit trail refers to.
    #
    # The owner is deliberately **not** stored here. The owner is
    # OWNER_USER_ID from the environment, so no row in this table can create,
    # modify or remove the primary authority — a privilege that lives in a
    # writable table is a privilege an attacker can ask for.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            permissions TEXT NOT NULL,
            granted_by INTEGER NOT NULL,
            granted_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '')"""
    )
    # Every sensitive administrative decision, allowed or refused.
    #
    # Written for refusals as well as successes, because "who tried" is the
    # question an operator asks after an incident, and a log that only records
    # successes cannot answer it. No secrets, no message content — identifiers,
    # an action name, an outcome, and a short reason.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_id INTEGER,
            chat_id INTEGER,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '')"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_at ON admin_audit(at)"
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


# ---- group acquisition cooldown ----
def seconds_since_offer(chat_id: int, user_id: int) -> int | None:
    """Seconds since this user was last offered a test, or ``None``.

    ``None`` means "never offered", which is different from ``0`` and is what
    lets the caller distinguish a first-time ask from a repeat.
    """
    with _lock:
        row = _conn.execute(
            "SELECT last_offered FROM intent_offers WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if not row or row[0] is None:
        return None
    return max(0, int(time.time()) - int(row[0]))


def mark_offered(chat_id: int, user_id: int, reason: str = "") -> None:
    """Record that we replied to this user's intent just now."""
    _exec(
        "INSERT OR REPLACE INTO intent_offers VALUES (?,?,?,?)",
        (chat_id, user_id, int(time.time()), (reason or "")[:32]),
    )


def last_offer_reason(chat_id: int, user_id: int) -> str:
    with _lock:
        row = _conn.execute(
            "SELECT last_reason FROM intent_offers WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return (row[0] or "") if row else ""


# ---- Gemini quota and counters ----
# The Gemini API's free tier counts requests per day and resets at midnight
# Pacific. Ours is counted against a fixed UTC-8 boundary rather than local
# midnight. In winter (PST, UTC-8) that is exactly the API's reset; in summer
# (PDT, UTC-7) the API resets at 07:00 UTC and we do not roll over until 08:00,
# so for that one hour we are still counting yesterday's spend against today.
# That is deliberately *stricter* than the API, and stricter is the only
# direction that cannot produce a surprise 429: the failure we are avoiding is
# believing we have allowance the API still considers spent.
_API_DAY_OFFSET = 8 * 3600

# Outcomes that may be recorded for one request. A whitelist rather than a free
# string, so a typo cannot invent a new column of counters that nothing reads.
OUTCOMES = ("relevant", "irrelevant", "malformed", "errors")


def ai_day(now: float | None = None) -> str:
    """The API day a call made at ``now`` belongs to, as ``YYYY-MM-DD``."""
    stamp = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(stamp - _API_DAY_OFFSET))


def ai_calls_today(day: str | None = None) -> int:
    """How many Gemini requests have been spent on ``day`` (default: today)."""
    key = day or ai_day()
    with _lock:
        row = _conn.execute(
            "SELECT calls FROM ai_usage WHERE day=?", (key,)
        ).fetchone()
    return int(row[0]) if row else 0


def ai_usage(day: str | None = None) -> dict:
    """The full counter row for ``day``. All zeros when nothing has happened."""
    key = day or ai_day()
    with _lock:
        row = _conn.execute(
            "SELECT calls, relevant, irrelevant, malformed, errors, skipped "
            "FROM ai_usage WHERE day=?",
            (key,),
        ).fetchone()
    values = row or (0, 0, 0, 0, 0, 0)
    return dict(
        zip(("calls", "relevant", "irrelevant", "malformed", "errors", "skipped"), values)
    )


def record_ai_attempt(outcome: str) -> int:
    """Count one request that was actually sent, and how it turned out.

    ``calls`` is the spend counter and it moves for every attempt, including the
    ones that failed — a request that timed out still used the quota. That is
    also what the daily cap reads, so a retry cannot be mistaken for a free
    second chance.
    """
    key = ai_day()
    # Looked up in a tuple, never interpolated from the caller: `outcome` becomes
    # a column name in the statement below, and a column name cannot be a bound
    # parameter. An unknown value is counted as an error rather than rejected,
    # because losing the count would be worse than mislabelling it.
    outcome = outcome if outcome in OUTCOMES else "errors"
    _exec(
        f"""INSERT INTO ai_usage (day, calls, {outcome}) VALUES (?, 1, 1)
            ON CONFLICT(day) DO UPDATE SET
                calls = calls + 1,
                {outcome} = {outcome} + 1""",
        (key,),
    )
    return ai_calls_today(key)


def record_ai_skip() -> int:
    """Count a request we chose *not* to send.

    Kept apart from ``record_ai_attempt`` so restraint is visibly not spending:
    this is the only counter that leaves ``calls`` alone.
    """
    _exec(
        """INSERT INTO ai_usage (day, calls, skipped) VALUES (?, 0, 1)
           ON CONFLICT(day) DO UPDATE SET skipped = skipped + 1""",
        (ai_day(),),
    )
    return ai_calls_today()


# ---- conversational AI: history, and its own counters ----
# Nothing below touches `ai_usage`. A chatty user must not be able to spend the
# acquisition classifier's quota, and an acquisition burst must not silence the
# chat — the two are separate budgets on separate keys, and separate rows here
# is what makes that a property of the code rather than a promise.
CHAT_ROLES = ("user", "model")

# The outcome whitelist for the chat counters. Same reasoning as OUTCOMES: a
# typo must not invent a column nothing reads.
CHAT_OUTCOMES = ("replies", "malformed", "errors")


def chat_history(
    chat_id: int, user_id: int, *, limit: int, ttl: int
) -> list[tuple[str, str]]:
    """Recent turns for one conversation, oldest first.

    Two bounds, both applied here rather than by the caller: a maximum number of
    turns, and an age cutoff. Either alone leaves a hole — a turn limit alone
    would let a conversation from last week reappear, and an age cutoff alone
    would let one long session grow without limit.

    Scoped by ``(chat_id, user_id)`` so histories cannot leak between people, or
    between a group and a private chat with the same person.
    """
    cutoff = int(time.time()) - max(1, int(ttl))
    with _lock:
        rows = _conn.execute(
            "SELECT role, text FROM chat_messages "
            "WHERE chat_id=? AND user_id=? AND at>=? "
            "ORDER BY id DESC LIMIT ?",
            (int(chat_id), int(user_id), cutoff, max(1, int(limit))),
        ).fetchall()
    # Reversed: the query takes the newest N, the model wants them oldest-first.
    return [(row[0], row[1]) for row in reversed(rows)]


def chat_append(chat_id: int, user_id: int, role: str, text: str) -> None:
    """Record one turn. Truncated hard, because this is attacker-controlled text."""
    role = role if role in CHAT_ROLES else "user"
    _exec(
        "INSERT INTO chat_messages (chat_id, user_id, role, text, at) "
        "VALUES (?,?,?,?,?)",
        (int(chat_id), int(user_id), role, (text or "")[:4000], int(time.time())),
    )


def chat_trim(chat_id: int, user_id: int, *, keep: int) -> int:
    """Keep only the newest ``keep`` turns for one conversation.

    Called after every append, which is what stops a single determined user from
    growing the table without limit. Returns how many rows were dropped.
    """
    keep = max(0, int(keep))
    with _lock:
        cur = _conn.execute(
            "DELETE FROM chat_messages WHERE chat_id=? AND user_id=? AND id NOT IN "
            "(SELECT id FROM chat_messages WHERE chat_id=? AND user_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (int(chat_id), int(user_id), int(chat_id), int(user_id), keep),
        )
        _conn.commit()
        return cur.rowcount


def chat_clear(chat_id: int, user_id: int) -> int:
    """Forget one conversation. Returns how many turns were removed."""
    with _lock:
        cur = _conn.execute(
            "DELETE FROM chat_messages WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        _conn.commit()
        return cur.rowcount


def chat_purge(ttl: int) -> int:
    """Drop every conversation older than ``ttl``.

    The per-conversation trim bounds one conversation; this bounds the table.
    Conversations that are simply abandoned would otherwise sit there forever,
    since nothing else would ever come back to trim them.
    """
    cutoff = int(time.time()) - max(1, int(ttl))
    with _lock:
        cur = _conn.execute("DELETE FROM chat_messages WHERE at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


def chat_calls_today(day: str | None = None) -> int:
    key = day or ai_day()
    with _lock:
        row = _conn.execute(
            "SELECT calls FROM chat_usage WHERE day=?", (key,)
        ).fetchone()
    return int(row[0]) if row else 0


def chat_usage(day: str | None = None) -> dict:
    """The chat counter row for ``day``. All zeros when nothing has happened."""
    key = day or ai_day()
    with _lock:
        row = _conn.execute(
            "SELECT calls, replies, malformed, errors, skipped "
            "FROM chat_usage WHERE day=?",
            (key,),
        ).fetchone()
    values = row or (0, 0, 0, 0, 0)
    return dict(zip(("calls", "replies", "malformed", "errors", "skipped"), values))


def record_chat_attempt(outcome: str) -> int:
    """Count one conversational request that was actually sent."""
    key = ai_day()
    outcome = outcome if outcome in CHAT_OUTCOMES else "errors"
    _exec(
        f"""INSERT INTO chat_usage (day, calls, {outcome}) VALUES (?, 1, 1)
            ON CONFLICT(day) DO UPDATE SET
                calls = calls + 1,
                {outcome} = {outcome} + 1""",
        (key,),
    )
    return chat_calls_today(key)


def record_chat_skip() -> int:
    """Count a conversational request we chose not to send."""
    _exec(
        """INSERT INTO chat_usage (day, calls, skipped) VALUES (?, 0, 1)
           ON CONFLICT(day) DO UPDATE SET skipped = skipped + 1""",
        (ai_day(),),
    )
    return chat_calls_today()


# ---- the moderation and transcription workloads: their own counters ----
# Two more tables, and one shared implementation rather than two more copies of
# the same INSERT. The tables stay separate — that is the isolation — but the
# SQL is the same shape, so it lives in one place.
#
# The outcome is looked up in the table's own tuple and never interpolated from
# the caller, because it becomes a column name and a column name cannot be a
# bound parameter.
MOD_OUTCOMES = ("flagged", "allowed", "malformed", "errors")
TRANSCRIPT_OUTCOMES = ("transcripts", "malformed", "errors")

# The column order used by the read functions below, so the dict a caller gets
# back always has the same keys.
_MOD_COLUMNS = ("calls", "flagged", "allowed", "malformed", "errors", "skipped")
_TRANSCRIPT_COLUMNS = ("calls", "transcripts", "malformed", "errors", "skipped")


def _usage(table: str, columns: tuple[str, ...], day: str | None) -> dict:
    key = day or ai_day()
    with _lock:
        row = _conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE day=?", (key,)
        ).fetchone()
    return dict(zip(columns, row or (0,) * len(columns)))


def _calls_today(table: str, day: str | None = None) -> int:
    key = day or ai_day()
    with _lock:
        row = _conn.execute(
            f"SELECT calls FROM {table} WHERE day=?", (key,)
        ).fetchone()
    return int(row[0]) if row else 0


def _attempt(table: str, outcomes: tuple[str, ...], outcome: str) -> int:
    key = ai_day()
    outcome = outcome if outcome in outcomes else "errors"
    _exec(
        f"""INSERT INTO {table} (day, calls, {outcome}) VALUES (?, 1, 1)
            ON CONFLICT(day) DO UPDATE SET
                calls = calls + 1,
                {outcome} = {outcome} + 1""",
        (key,),
    )
    return _calls_today(table, key)


def _skip(table: str) -> int:
    _exec(
        f"""INSERT INTO {table} (day, calls, skipped) VALUES (?, 0, 1)
           ON CONFLICT(day) DO UPDATE SET skipped = skipped + 1""",
        (ai_day(),),
    )
    return _calls_today(table)


def mod_calls_today(day: str | None = None) -> int:
    return _calls_today("moderation_usage", day)


def mod_usage(day: str | None = None) -> dict:
    return _usage("moderation_usage", _MOD_COLUMNS, day)


def record_mod_attempt(outcome: str) -> int:
    """Count one moderation request that was actually sent.

    ``flagged`` means the model returned a deletable classification at or above
    the configured confidence — it is *not* the same as "something was deleted",
    because the policy engine decides that separately and may still choose to
    allow. ``allowed`` is every other answer, including a clean one.
    """
    return _attempt("moderation_usage", MOD_OUTCOMES, outcome)


def record_mod_skip() -> int:
    """Count a moderation request we chose not to send."""
    return _skip("moderation_usage")


def transcript_calls_today(day: str | None = None) -> int:
    return _calls_today("transcript_usage", day)


def transcript_usage(day: str | None = None) -> dict:
    return _usage("transcript_usage", _TRANSCRIPT_COLUMNS, day)


def record_transcript_attempt(outcome: str) -> int:
    return _attempt("transcript_usage", TRANSCRIPT_OUTCOMES, outcome)


def record_transcript_skip() -> int:
    return _skip("transcript_usage")


# ---- application administrators and the audit trail ----
def admin_get(user_id: int) -> dict | None:
    """One stored administrator, or None. The owner is not in this table."""
    with _lock:
        row = _conn.execute(
            "SELECT user_id, role, permissions, granted_by, granted_at, "
            "updated_at, note FROM admins WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "user_id": int(row[0]),
        "role": row[1],
        "permissions": _split_perms(row[2]),
        "granted_by": int(row[3]),
        "granted_at": int(row[4]),
        "updated_at": int(row[5]),
        "note": row[6] or "",
    }


def admin_list() -> list[dict]:
    """Every stored administrator, newest grant first."""
    with _lock:
        rows = _conn.execute(
            "SELECT user_id, role, permissions, granted_by, granted_at, "
            "updated_at, note FROM admins ORDER BY granted_at DESC, user_id"
        ).fetchall()
    return [
        {
            "user_id": int(r[0]),
            "role": r[1],
            "permissions": _split_perms(r[2]),
            "granted_by": int(r[3]),
            "granted_at": int(r[4]),
            "updated_at": int(r[5]),
            "note": r[6] or "",
        }
        for r in rows
    ]


def admin_set(
    user_id: int, role: str, permissions, *, granted_by: int, note: str = ""
) -> None:
    """Create or replace one administrator's application permissions.

    Upsert rather than insert: promoting somebody who is already an admin is a
    permission change, not a duplicate. ``granted_by`` records who did it, and
    on an update the original ``granted_at`` is kept while ``updated_at`` moves —
    so the audit trail keeps both "when did this start" and "when did it last
    change".
    """
    now = int(time.time())
    joined = ",".join(sorted({str(p) for p in permissions if p}))
    _exec(
        """INSERT INTO admins
               (user_id, role, permissions, granted_by, granted_at, updated_at, note)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               role=excluded.role,
               permissions=excluded.permissions,
               granted_by=excluded.granted_by,
               updated_at=excluded.updated_at,
               note=excluded.note""",
        (int(user_id), str(role), joined, int(granted_by), now, now, str(note)[:200]),
    )


def admin_remove(user_id: int) -> int:
    """Revoke one administrator. Returns how many rows were removed."""
    with _lock:
        cur = _conn.execute("DELETE FROM admins WHERE user_id=?", (int(user_id),))
        _conn.commit()
        return cur.rowcount


def _split_perms(value: str) -> list[str]:
    return [p for p in (value or "").split(",") if p]


def audit_write(
    actor_id: int,
    action: str,
    *,
    outcome: str,
    target_id: int | None = None,
    chat_id: int | None = None,
    detail: str = "",
) -> None:
    """Record one administrative decision, allowed or refused.

    Never raises into a handler: an audit row that cannot be written must not be
    the reason a moderation action fails. The exception is logged by the caller's
    logger, which is the same place every other failure goes.
    """
    _exec(
        "INSERT INTO admin_audit (at, actor_id, action, target_id, chat_id, "
        "outcome, detail) VALUES (?,?,?,?,?,?,?)",
        (
            int(time.time()),
            int(actor_id),
            str(action)[:80],
            int(target_id) if target_id is not None else None,
            int(chat_id) if chat_id is not None else None,
            str(outcome)[:40],
            str(detail)[:300],
        ),
    )


def audit_recent(limit: int = 20) -> list[dict]:
    """The newest audit rows, newest first. For the operator's own inspection."""
    with _lock:
        rows = _conn.execute(
            "SELECT at, actor_id, action, target_id, chat_id, outcome, detail "
            "FROM admin_audit ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [
        {
            "at": int(r[0]),
            "actor_id": int(r[1]),
            "action": r[2],
            "target_id": int(r[3]) if r[3] is not None else None,
            "chat_id": int(r[4]) if r[4] is not None else None,
            "outcome": r[5],
            "detail": r[6] or "",
        }
        for r in rows
    ]
