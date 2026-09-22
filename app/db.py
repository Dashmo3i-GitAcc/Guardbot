"""Tiny SQLite store: strikes (confirmed moderation actions) and captchas."""
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _ensure_column(table: str, column: str, declaration: str) -> None:
    """Add a column to an existing table, once. A no-op if it is already there.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and `CREATE TABLE IF NOT EXISTS`
    does nothing at all to a table that already exists — so a column added to a
    schema after the first deploy reaches fresh installs and never reaches the
    one running in production. This is the missing half.

    Only ever additive, and only ever called from ``init()``: the alternative,
    rebuilding the table, would mean dropping and recreating a table whose whole
    purpose is to be an append-only record of who did what.
    """
    cols = {row[1] for row in _conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


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
            detail TEXT NOT NULL DEFAULT '',
            interface TEXT NOT NULL DEFAULT '')"""
    )
    # `interface` was added after the table was already in production, so it
    # needs a migration rather than only a CREATE TABLE: `IF NOT EXISTS` leaves
    # an existing table exactly as it was, which would mean the column silently
    # existed on fresh installs and not on the one that matters. Idempotent, and
    # additive — the column is what makes "was this the assistant or a person?"
    # answerable from the record itself rather than only from the log line.
    _ensure_column("admin_audit", "interface", "TEXT NOT NULL DEFAULT ''")
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_at ON admin_audit(at)"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_chat "
        "ON admin_audit(chat_id, at)"
    )
    # Replay and idempotency for administrative requests.
    #
    # The primary key is the request id, which is what makes "the same request
    # twice" a fact rather than a guess. Both outcomes are stored, not only the
    # successes: a denial that is replayed is also a replay, and re-running it
    # could produce a different answer if the actor's permissions changed in
    # between — which is exactly the window an attacker wants.
    #
    # `outcome` is kept so a duplicate can report what the original did rather
    # than a bare "already seen". Pruned on a retention window, because a table
    # that grows forever is a table that eventually stops being written to.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS admin_requests (
            request_id TEXT PRIMARY KEY,
            at INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            operation TEXT NOT NULL,
            target_id INTEGER NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT '')"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_requests_at ON admin_requests(at)"
    )
    # The Gemini account/model pool.
    #
    # Keyed by ``(workload, slot)`` rather than by slot alone, and that is the
    # whole point: the same API key serving two workloads is two independent
    # rows with two independent counters, two cooldowns and two failure states.
    # Workload isolation is then a property of the primary key instead of a
    # promise about how the code happens to call things. A key that is exhausted
    # for moderation must not silently silence the classifier.
    #
    # The key itself is never stored. ``fingerprint`` is a truncated hash, used
    # only to notice that two configured slots resolved to the same credential,
    # and ``masked`` is the last four characters for an operator to recognise.
    # Neither can be turned back into a credential.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS gemini_accounts (
            workload TEXT NOT NULL,
            slot TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT '',
            masked TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            cooldown_until INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            last_error_at INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            rate_limits INTEGER NOT NULL DEFAULT 0,
            quota_events INTEGER NOT NULL DEFAULT 0,
            last_request INTEGER NOT NULL DEFAULT 0,
            last_success INTEGER NOT NULL DEFAULT 0,
            last_failure INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (workload, slot))"""
    )
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS gemini_models (
            workload TEXT NOT NULL,
            slot TEXT NOT NULL,
            model TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            cooldown_until INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            rate_limits INTEGER NOT NULL DEFAULT 0,
            quota_events INTEGER NOT NULL DEFAULT 0,
            last_use INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (workload, slot, model))"""
    )
    # Model discovery results, cached per credential fingerprint rather than per
    # slot: two slots holding the same key share one project and therefore one
    # model list, and asking the provider twice for it is a wasted call.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS gemini_discovery (
            fingerprint TEXT PRIMARY KEY,
            models TEXT NOT NULL DEFAULT '',
            at INTEGER NOT NULL DEFAULT 0)"""
    )
    # Every meaningful pool event: a failover, a recovery, a pool that has run
    # out. Structured rows for diagnostics — this is what an operator reads when
    # they want to know what the pool has been doing, and it is deliberately not
    # a message queue.
    #
    # These events do **not** reach Telegram. Nothing in this bot sends pool
    # state to a chat automatically; the operator asks, with `/pool` or by
    # reading this table. That is a deliberate boundary rather than an omission
    # — a failover notice arriving in a group is noise nobody asked for, and it
    # puts operational detail where the group can see it.
    #
    # `notified` is a leftover from when this table drove a Telegram notice. It
    # is no longer read or written, and it is left in the schema only because
    # deployments already have the column and SQLite cannot drop one in place.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS gemini_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at INTEGER NOT NULL,
            workload TEXT NOT NULL,
            kind TEXT NOT NULL,
            slot TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            notified INTEGER NOT NULL DEFAULT 0)"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemini_events_dedup "
        "ON gemini_events(workload, kind, slot, model, at)"
    )
    # How many provider requests each account has spent *today*.
    #
    # Lifetime totals live on `gemini_accounts` and are that account's health
    # record; this is a per-day spend that has to be able to reset without
    # disturbing them, which is why it is its own table rather than more columns
    # on the account row. A "day" here is the API day (`ai_day`), not midnight
    # local, so an allowance resets when the provider's own quota does.
    #
    # It exists so a daily allowance can belong to an *account* rather than to
    # the deployment. One shared counter for the whole bot meant the second
    # configured key bought nothing: the cap was reached while a healthy account
    # with a full allowance sat unused. Per account, the pool spends one
    # account's day and then moves to the next — the same failover it already
    # performs for a 429.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS gemini_daily (
            workload TEXT NOT NULL,
            slot TEXT NOT NULL,
            day TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (workload, slot, day))"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemini_daily_day ON gemini_daily(day)"
    )
    # The conversational layer's runtime state.
    #
    # One row, enforced by the primary key rather than by a convention: there is
    # one Nexus, so "which state is it in" must have exactly one answer. A
    # per-chat row would let the bot be offline in one group and online in
    # another, which is not a distinction the owner asked for and is one more
    # way for the two to disagree.
    #
    # Persisted rather than held in memory because a restart must not silently
    # change a security posture. If the owner has switched Nexus off, a redeploy
    # has to bring it back up switched off — an in-memory flag would bring it up
    # answering, which is the failure this table exists to prevent.
    #
    # `changed_by` and `reason` are the audit breadcrumb that survives the audit
    # table's own retention window. They hold a Telegram user id and a short
    # machine string, never a message body.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS nexus_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT NOT NULL,
            changed_at INTEGER NOT NULL DEFAULT 0,
            changed_by INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '')"""
    )
    # Who has spoken in a monitored group, so a name can be resolved to the
    # Telegram user id that actually identifies somebody.
    #
    # This table holds **metadata only**: the name, the username and when they
    # were last seen. It never holds a message body, and there is no column for
    # one. It exists for exactly one job — turning "Milad" into an id — and it
    # is deliberately not an authorization source: nothing here grants a
    # permission, and a row is written for every speaker, including people with
    # no role at all.
    #
    # Keyed by (chat_id, user_id) rather than by user_id alone so a person's
    # context stays with the room they were seen in, and pruned on both a row
    # count and an age, because a table that only grows is a table that
    # eventually stops being written to.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS people (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            first_seen INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id))"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_people_last_seen ON people(last_seen)"
    )
    # ── Nexus Awareness: the bounded view of the room ──
    #
    # Two tables, and they are a *different thing* from ``chat_messages`` rather
    # than a second copy of it. ``chat_messages`` is the assistant's conversation
    # with **one person** — it is keyed by ``(chat_id, user_id)`` and its rows
    # are ``user``/``model`` turns, which is what lets the assistant answer that
    # person and notice when it repeats itself. This is the **room**: many
    # speakers in one chat, in the order they spoke, which is what lets the
    # assistant understand a conversation it is not part of.
    #
    # Keeping them apart is what stops the two from corrupting each other. A
    # group transcript written into ``chat_messages`` would appear in every
    # member's private history — one person's words shown to another, which is
    # exactly the leak the per-user key exists to prevent.
    #
    # ``role`` is assigned by the **server** from ``app/rbac.py`` at the moment
    # the message is captured, and it is the one field the model is told it can
    # trust. It is a snapshot rather than a live lookup: it records what the
    # speaker was when they spoke, and the live role of whoever is being
    # answered is stated separately in the trusted-context block. A stored role
    # is never an authority — nothing reads this column to decide anything.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            at INTEGER NOT NULL)"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_messages_chat "
        "ON group_messages(chat_id, id)"
    )
    # ``group_purge`` deletes by age across every room, so without this its
    # ``WHERE at < ?`` is a full scan of the table. The table is small — it is
    # bounded by ``NEXUS_AWARENESS_MAX_ROWS`` per room and by the retention
    # window — so this is not the difference between fast and slow; it is the
    # difference between a scan and a range seek on a statement that runs on a
    # timer, and it costs one index to keep it that way as rooms accumulate.
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_messages_at ON group_messages(at)"
    )
    # What Nexus currently understands about one room. One row per chat, and
    # every column is bounded: this is a *summary*, not a transcript. It exists
    # so the assistant's understanding survives between passes and across a
    # restart, and it is a derived cache — the window above is the source of
    # truth, so a lost or unreadable row costs nothing but a re-read.
    #
    # ``seen_message_id`` is the highest ``group_messages.id`` included in the
    # last completed pass, which is how "is there anything new?" is answered
    # without a second table.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS awareness_state (
            chat_id INTEGER PRIMARY KEY,
            updated_at INTEGER NOT NULL DEFAULT 0,
            seen_message_id INTEGER NOT NULL DEFAULT 0,
            passes INTEGER NOT NULL DEFAULT 0,
            relevant INTEGER NOT NULL DEFAULT 0,
            topic TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            participants TEXT NOT NULL DEFAULT '')"""
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


def claim_captcha(chat_id: int, user_id: int, *, before: int) -> bool:
    """Atomically take the challenge if it is still pending and unexpired.

    Returns True for exactly one caller. This is the fix for the bug where a
    member verified successfully and was banned anyway: verification used to be
    "read the row, await a network call, then delete the row", which left a
    window in which the reaper could read the same row and act on it. The
    deletion is now the claim, in one statement, so the two sides cannot both
    win — whichever DELETE returns a row is the only one that proceeds.

    ``before`` is the current time: a row whose deadline has already passed is
    not claimable here, because that is the reaper's case to handle.
    """
    with _lock:
        cur = _conn.execute(
            "DELETE FROM captchas WHERE chat_id=? AND user_id=? AND deadline>?",
            (int(chat_id), int(user_id), int(before)),
        )
        _conn.commit()
        return cur.rowcount == 1


def claim_expired_captcha(chat_id: int, user_id: int, *, now: int) -> bool:
    """The reaper's half of the same compare-and-swap.

    Returns True only when the row was still present *and* still expired at the
    moment of the delete. A member who verified a moment ago has no row left, so
    this returns False and the reaper does nothing — which is the behaviour the
    owner asked for: a completed verification can never be undone by a timer.
    """
    with _lock:
        cur = _conn.execute(
            "DELETE FROM captchas WHERE chat_id=? AND user_id=? AND deadline<=?",
            (int(chat_id), int(user_id), int(now)),
        )
        _conn.commit()
        return cur.rowcount == 1


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
    interface: str = "",
) -> None:
    """Record one administrative decision, allowed or refused.

    Never raises into a handler: an audit row that cannot be written must not be
    the reason a moderation action fails. The exception is logged by the caller's
    logger, which is the same place every other failure goes.

    ``interface`` is ``ai`` or ``python`` and says which of the two front doors
    the request came through. It is recorded here rather than only in the log
    line because "was this the assistant or a person?" is the first question
    asked about an action somebody disagrees with, and a log file is not a place
    to answer it from.
    """
    _exec(
        "INSERT INTO admin_audit (at, actor_id, action, target_id, chat_id, "
        "outcome, detail, interface) VALUES (?,?,?,?,?,?,?,?)",
        (
            int(time.time()),
            int(actor_id),
            str(action)[:80],
            int(target_id) if target_id is not None else None,
            int(chat_id) if chat_id is not None else None,
            str(outcome)[:40],
            str(detail)[:300],
            str(interface)[:16],
        ),
    )


_AUDIT_COLS = (
    "at, actor_id, action, target_id, chat_id, outcome, detail, interface"
)

# The outcome string that means "this action actually happened". It is a copy of
# ``admin_service.OUTCOME_OK`` rather than an import, because ``admin_service``
# imports this module and a cycle would be worse than a duplicated literal. A
# test asserts the two agree, so the copy cannot drift silently.
AUDIT_OK = "ok"


def _audit_row(r) -> dict:
    return {
        "at": int(r[0]),
        "actor_id": int(r[1]),
        "action": r[2],
        "target_id": int(r[3]) if r[3] is not None else None,
        "chat_id": int(r[4]) if r[4] is not None else None,
        "outcome": r[5],
        "detail": r[6] or "",
        "interface": r[7] or "",
    }


def audit_recent(limit: int = 20) -> list[dict]:
    """The newest audit rows, newest first. For the operator's own inspection."""
    with _lock:
        rows = _conn.execute(
            f"SELECT {_AUDIT_COLS} FROM admin_audit ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [_audit_row(r) for r in rows]


def audit_since(
    *, chat_id: int | None = None, since: int = 0, limit: int = 20
) -> list[dict]:
    """Recent audit rows for one room, newest first.

    This is what the assistant is allowed to see of administrative history. Two
    bounds, and both matter: ``chat_id`` keeps one group's administrative
    business out of another group's conversation, and ``since`` keeps it to a
    window rather than the whole record. The brief's rule is bounded,
    privacy-conscious context — not a copy of the audit table in a prompt.
    """
    sql = f"SELECT {_AUDIT_COLS} FROM admin_audit WHERE at >= ?"
    args: list = [int(since)]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        args.append(int(chat_id))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_audit_row(r) for r in rows]


def audit_recent_actions(
    actor_id: int, *, chat_id: int, since: int = 0, limit: int = 3
) -> list[dict]:
    """What this actor has *successfully done* in this room, newest first.

    This is the antecedent for a follow-up. «این کاربر رو ساکت کن» followed by
    «درش بیار» is only answerable if something remembers who "him" was, and the
    thing that remembers it has to be the server: the audit row was written by
    the execution layer after the action actually succeeded, so it cannot be
    planted by anything anybody typed.

    Three filters, each load-bearing:

    * ``actor_id`` — it is *your own* actions. Somebody else's mute is not a
      referent you may act on, and showing it would invite exactly that.
    * ``chat_id`` — one group's administrative business stays out of another's.
    * ``outcome = 'ok'`` — a refused or failed action never happened, and a
      follow-up that resolved to it would be resolving to a fiction.

    Bounded by ``since`` and ``limit``: this is a conversational antecedent, not
    a copy of the audit table in a prompt.
    """
    sql = (
        f"SELECT {_AUDIT_COLS} FROM admin_audit "
        "WHERE actor_id = ? AND chat_id = ? AND outcome = ? AND target_id IS NOT NULL "
        "AND at >= ? ORDER BY id DESC LIMIT ?"
    )
    args = [
        int(actor_id),
        int(chat_id),
        AUDIT_OK,
        int(since),
        max(1, int(limit)),
    ]
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_audit_row(r) for r in rows]


def audit_prune(keep_seconds: int) -> int:
    """Drop audit rows older than the retention window. Returns rows removed.

    Called on the administrative path rather than on a timer, because this
    process has no scheduler and a retention rule that only runs when somebody
    remembers is not a retention rule.
    """
    if keep_seconds <= 0:
        return 0
    cutoff = int(time.time()) - int(keep_seconds)
    with _lock:
        cur = _conn.execute("DELETE FROM admin_audit WHERE at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


# ── Administrative request idempotency ────────────────────────────────────
def admin_request_get(request_id: str) -> dict | None:
    """The stored outcome for a request id, or None if it is new."""
    if not request_id:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT at, actor_id, chat_id, operation, target_id, outcome "
            "FROM admin_requests WHERE request_id=?",
            (str(request_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "at": int(row[0]),
        "actor_id": int(row[1]),
        "chat_id": int(row[2]),
        "operation": row[3],
        "target_id": int(row[4]),
        "outcome": row[5],
    }


def admin_request_put(
    request_id: str,
    *,
    actor_id: int,
    chat_id: int,
    operation: str,
    target_id: int,
    outcome: str,
    at: int | None = None,
) -> None:
    """Remember a handled request id. First write wins.

    ``INSERT OR IGNORE`` rather than an upsert: if two requests with the same id
    race, the first one to be recorded is the one that happened, and rewriting
    the row would let the loser claim it did something else.
    """
    _exec(
        "INSERT OR IGNORE INTO admin_requests "
        "(request_id, at, actor_id, chat_id, operation, target_id, outcome) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            str(request_id)[:120],
            int(at if at is not None else time.time()),
            int(actor_id),
            int(chat_id),
            str(operation)[:80],
            int(target_id),
            str(outcome)[:40],
        ),
    )


def admin_request_prune(keep_seconds: int) -> int:
    """Drop request ids older than the retention window. Returns rows removed.

    The window must be at least as long as the replay window, or a request could
    be forgotten while it is still replayable. ``app/config.py`` enforces that
    ordering when it reads the two settings.
    """
    if keep_seconds <= 0:
        return 0
    cutoff = int(time.time()) - int(keep_seconds)
    with _lock:
        cur = _conn.execute("DELETE FROM admin_requests WHERE at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


def admin_reset() -> None:
    """Drop all administrative state: roles, the trail, and the request ids.

    For tests and for an operator starting over, in the same spirit as
    :func:`pool_reset`. Note what it does *not* touch — the moderation strikes
    in ``users`` and the captcha rows are not administrative state and are not
    this function's to erase.
    """
    with _lock:
        _conn.execute("DELETE FROM admins")
        _conn.execute("DELETE FROM admin_audit")
        _conn.execute("DELETE FROM admin_requests")
        _conn.commit()


# ---- the Gemini account/model pool ----
# Counters, cooldowns and events for the multi-account provider layer. Read and
# written by ``app/gemini_pool.py``; nothing else should touch these tables.
#
# Everything here is persisted for the same reason the daily quota is: the
# container is rebuilt on every deploy, and a usage history that resets with the
# process cannot answer "is this account nearly spent?" — which is the only
# question the pool exists to answer.

# The account states. A closed set, so a typo cannot invent a state that the
# status report then has to render.
ACCOUNT_STATES = (
    "ACTIVE",
    "RATE_LIMITED",
    "QUOTA_EXHAUSTED",
    "INVALID",
    "UNAVAILABLE",
    "RECOVERING",
    "DISABLED",
)

# The counter columns. Named here so a caller cannot pass an arbitrary string
# that would become a column name in an UPDATE.
ACCOUNT_COUNTERS = (
    "requests",
    "successes",
    "failures",
    "rate_limits",
    "quota_events",
)
MODEL_COUNTERS = (
    "requests",
    "successes",
    "failures",
    "rate_limits",
    "quota_events",
)

_ACCOUNT_FIELDS = (
    "state",
    "cooldown_until",
    "last_error",
    "last_error_at",
    "last_request",
    "last_success",
    "last_failure",
) + ACCOUNT_COUNTERS


def pool_account_save(
    workload: str,
    slot: str,
    fingerprint: str = "",
    masked: str = "",
    **values,
) -> None:
    """Write one account's state and counters. Unknown fields are dropped.

    An upsert rather than an update, so a freshly added credential gets a row on
    its first use without a separate registration step.
    """
    clean = {k: v for k, v in values.items() if k in _ACCOUNT_FIELDS}
    columns = ["workload", "slot", "fingerprint", "masked", *clean]
    params = [str(workload), str(slot), str(fingerprint), str(masked), *clean.values()]
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{c}=excluded.{c}" for c in ("fingerprint", "masked", *clean))
    _exec(
        f"""INSERT INTO gemini_accounts ({",".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(workload, slot) DO UPDATE SET {updates}""",
        tuple(params),
    )


def pool_account_bump(workload: str, slot: str, counter: str, amount: int = 1) -> None:
    """Add to one counter column. The column is whitelisted, never interpolated
    from the caller."""
    if counter not in ACCOUNT_COUNTERS:
        return
    _exec(
        f"""INSERT INTO gemini_accounts (workload, slot, {counter}) VALUES (?,?,?)
            ON CONFLICT(workload, slot) DO UPDATE SET {counter} = {counter} + ?""",
        (str(workload), str(slot), int(amount), int(amount)),
    )


def pool_accounts(workload: str | None = None) -> list[dict]:
    """Account rows, optionally for one workload, ordered by slot."""
    sql = (
        "SELECT workload, slot, fingerprint, masked, "
        + ", ".join(_ACCOUNT_FIELDS)
        + " FROM gemini_accounts"
    )
    args: tuple = ()
    if workload is not None:
        sql += " WHERE workload=?"
        args = (str(workload),)
    sql += " ORDER BY workload, slot"
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    names = ("workload", "slot", "fingerprint", "masked", *_ACCOUNT_FIELDS)
    return [dict(zip(names, row)) for row in rows]


def pool_model_save(
    workload: str, slot: str, model: str, **values
) -> None:
    """Write one model's state and counters within one account."""
    allowed = ("state", "cooldown_until", "last_use") + MODEL_COUNTERS
    clean = {k: v for k, v in values.items() if k in allowed}
    columns = ["workload", "slot", "model", *clean]
    params = [str(workload), str(slot), str(model), *clean.values()]
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{c}=excluded.{c}" for c in clean)
    _exec(
        f"""INSERT INTO gemini_models ({",".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(workload, slot, model) DO UPDATE SET {updates}""",
        tuple(params),
    )


def pool_model_bump(
    workload: str, slot: str, model: str, counter: str, amount: int = 1
) -> None:
    """Add to one model counter column, whitelisted the same way."""
    if counter not in MODEL_COUNTERS:
        return
    _exec(
        f"""INSERT INTO gemini_models (workload, slot, model, {counter})
            VALUES (?,?,?,?)
            ON CONFLICT(workload, slot, model) DO UPDATE SET
                {counter} = {counter} + ?""",
        (str(workload), str(slot), str(model), int(amount), int(amount)),
    )


def pool_models(workload: str | None = None) -> list[dict]:
    """Model rows, optionally for one workload."""
    sql = (
        "SELECT workload, slot, model, state, cooldown_until, last_use, "
        + ", ".join(MODEL_COUNTERS)
        + " FROM gemini_models"
    )
    args: tuple = ()
    if workload is not None:
        sql += " WHERE workload=?"
        args = (str(workload),)
    sql += " ORDER BY workload, slot, model"
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    names = ("workload", "slot", "model", "state", "cooldown_until", "last_use",
             *MODEL_COUNTERS)
    return [dict(zip(names, row)) for row in rows]


def pool_event_add(
    workload: str,
    kind: str,
    slot: str = "",
    model: str = "",
    reason: str = "",
    detail: str = "",
    at: int | None = None,
) -> int:
    """Record one pool event. Returns its row id."""
    with _lock:
        cur = _conn.execute(
            "INSERT INTO gemini_events "
            "(at, workload, kind, slot, model, reason, detail) VALUES (?,?,?,?,?,?,?)",
            (
                int(time.time()) if at is None else int(at),
                str(workload)[:40],
                str(kind)[:40],
                str(slot)[:20],
                str(model)[:80],
                str(reason)[:60],
                str(detail)[:200],
            ),
        )
        _conn.commit()
        return int(cur.lastrowid)


def pool_events(limit: int = 20) -> list[dict]:
    """The newest pool events, newest first. For diagnostics, not for sending."""
    with _lock:
        rows = _conn.execute(
            "SELECT id, at, workload, kind, slot, model, reason, detail "
            "FROM gemini_events ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    names = ("id", "at", "workload", "kind", "slot", "model", "reason", "detail")
    return [dict(zip(names, row)) for row in rows]


def pool_last_event(
    workload: str, kind: str, slot: str = "", model: str = ""
) -> int:
    """When this exact event was last *recorded*, or 0.

    The deduplication key is ``(workload, kind, slot, model)``: one hundred
    consecutive 429s on one model are one row, but the same event on a second
    account is its own row, because that is the one that says the pool is
    shrinking.

    This used to be ``pool_last_notified`` and filtered on a ``notified`` flag,
    because it existed to decide whether to send the owner a Telegram message.
    Nothing sends a message any more, so the question it answers is now simply
    "have we already written this event down recently" — which is what keeps the
    events table a record of transitions rather than of every request. The
    counts live on the account and model rows; this table is the qualitative
    half, and deduplicating it is what keeps the two from being the same thing.
    """
    with _lock:
        row = _conn.execute(
            "SELECT MAX(at) FROM gemini_events "
            "WHERE workload=? AND kind=? AND slot=? AND model=?",
            (str(workload), str(kind), str(slot), str(model)),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def daily_add(workload: str, slot: str, day: str) -> int:
    """Count one provider request against an account's day. Returns the new total.

    A separate table from ``gemini_accounts`` on purpose. The counters there are
    *lifetime* totals and are the account's health record; this one is a
    per-day spend that must be able to reset without touching them, and a
    ``CREATE TABLE IF NOT EXISTS`` needs no migration where an added column
    would.

    The increment is an atomic upsert rather than a read-modify-write, for the
    same reason the lifetime counters are: two concurrent requests in this
    process must not be able to lose one another's count, or an account would
    serve more than its allowance precisely when the bot is busy.
    """
    key = str(day)
    with _lock:
        _conn.execute(
            """INSERT INTO gemini_daily (workload, slot, day, calls)
               VALUES (?,?,?,1)
               ON CONFLICT(workload, slot, day) DO UPDATE SET calls = calls + 1""",
            (str(workload), str(slot), key),
        )
        _conn.commit()
        row = _conn.execute(
            "SELECT calls FROM gemini_daily WHERE workload=? AND slot=? AND day=?",
            (str(workload), str(slot), key),
        ).fetchone()
    return int(row[0]) if row else 0


def daily_for(workload: str, day: str) -> dict[str, int]:
    """``slot -> calls`` for one workload on one day. Missing slots are absent.

    Absent means zero, and the caller is expected to treat it that way: an
    account that has never been used has no row, and inventing one on read
    would make a fresh day look like a fresh start for some slots and not
    others.
    """
    with _lock:
        rows = _conn.execute(
            "SELECT slot, calls FROM gemini_daily WHERE workload=? AND day=?",
            (str(workload), str(day)),
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def daily_prune(keep_days: int) -> int:
    """Drop day rows older than ``keep_days``. Returns rows removed.

    Called on the pool path rather than by a scheduler, for the same reason the
    audit retention is: this process has no scheduler, and a retention rule that
    only runs when somebody remembers is not a retention rule.
    """
    if keep_days <= 0:
        return 0
    cutoff = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - int(keep_days) * 86400 - _API_DAY_OFFSET)
    )
    with _lock:
        cur = _conn.execute("DELETE FROM gemini_daily WHERE day < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


def pool_counts(workload: str | None = None) -> dict:
    """Totals across every account, for the owner's report."""
    args: tuple = ()
    where = ""
    if workload is not None:
        where = " WHERE workload=?"
        args = (str(workload),)
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(requests),0), COALESCE(SUM(successes),0), "
            "COALESCE(SUM(failures),0), COALESCE(SUM(rate_limits),0), "
            "COALESCE(SUM(quota_events),0) FROM gemini_accounts" + where,
            args,
        ).fetchone()
    return dict(
        zip(
            ("accounts", "requests", "successes", "failures", "rate_limits",
             "quota_events"),
            (int(v or 0) for v in row),
        )
    )


def discovery_get(fingerprint: str, ttl: int) -> list[str] | None:
    """The cached model list for one credential, or None when stale or absent."""
    if not fingerprint:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT models, at FROM gemini_discovery WHERE fingerprint=?",
            (str(fingerprint),),
        ).fetchone()
    if not row:
        return None
    models, at = row[0] or "", int(row[1] or 0)
    if int(time.time()) - at > max(0, int(ttl)):
        return None
    return [m for m in models.split(",") if m]


def discovery_put(fingerprint: str, models) -> None:
    """Cache one credential's model list."""
    if not fingerprint:
        return
    _exec(
        """INSERT INTO gemini_discovery (fingerprint, models, at) VALUES (?,?,?)
           ON CONFLICT(fingerprint) DO UPDATE SET
               models=excluded.models, at=excluded.at""",
        (str(fingerprint), ",".join(models), int(time.time())),
    )


def pool_reset() -> None:
    """Drop all pool state. For tests and for an operator starting over."""
    with _lock:
        _conn.execute("DELETE FROM gemini_accounts")
        _conn.execute("DELETE FROM gemini_models")
        _conn.execute("DELETE FROM gemini_events")
        _conn.execute("DELETE FROM gemini_discovery")
        _conn.commit()


# ── Nexus: the conversational layer's runtime state ───────────────────────
def nexus_state_get() -> dict | None:
    """The stored state row, or None when nothing has ever been written.

    ``None`` is not the same fact as "offline": it means the deployment has
    never recorded a state, which is the normal state of a fresh install and the
    reason the caller supplies the default rather than this function inventing
    one.
    """
    with _lock:
        row = _conn.execute(
            "SELECT state, changed_at, changed_by, reason FROM nexus_state WHERE id=1"
        ).fetchone()
    if not row:
        return None
    return {
        "state": str(row[0] or ""),
        "changed_at": int(row[1] or 0),
        "changed_by": int(row[2] or 0),
        "reason": str(row[3] or ""),
    }


def nexus_state_set(state: str, *, actor_id: int = 0, reason: str = "") -> dict:
    """Write the one state row and return it. Last write wins, by design.

    The state is a single fact about the deployment rather than an append-only
    record, so an update is correct here where it would be wrong for the audit
    table. The audit trail keeps the history; this keeps the current answer.
    """
    row = {
        "state": str(state or ""),
        "changed_at": int(time.time()),
        "changed_by": int(actor_id or 0),
        "reason": str(reason or "")[:200],
    }
    _exec(
        """INSERT INTO nexus_state (id, state, changed_at, changed_by, reason)
           VALUES (1,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               state=excluded.state,
               changed_at=excluded.changed_at,
               changed_by=excluded.changed_by,
               reason=excluded.reason""",
        (row["state"], row["changed_at"], row["changed_by"], row["reason"]),
    )
    return row


def nexus_state_reset() -> None:
    """Forget the state row. For tests."""
    with _lock:
        _conn.execute("DELETE FROM nexus_state")
        _conn.commit()


# ── People: identity memory, metadata only ────────────────────────────────
def people_remember(
    chat_id: int,
    user_id: int,
    *,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
) -> None:
    """Record or refresh one speaker's name metadata.

    An upsert rather than an insert, because the useful thing is the *latest*
    name: Telegram lets a person rename themselves at any time, and a table that
    kept every historical name would answer "who is Milad" with a list of people
    who used to be called that.

    ``message_count`` is a number, not a message. It is the one signal that
    distinguishes somebody who speaks from somebody who was seen once, and it is
    named for what it is so that a reviewer reading the schema sees immediately
    that there is no column here capable of holding a conversation.
    """
    now = int(time.time())
    _exec(
        """INSERT INTO people
               (chat_id, user_id, first_name, last_name, username,
                message_count, first_seen, last_seen)
           VALUES (?,?,?,?,?,1,?,?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
               first_name=excluded.first_name,
               last_name=excluded.last_name,
               username=excluded.username,
               message_count=people.message_count + 1,
               last_seen=excluded.last_seen""",
        (
            int(chat_id),
            int(user_id),
            (first_name or "")[:120],
            (last_name or "")[:120],
            (username or "")[:120],
            now,
            now,
        ),
    )


def people_rows(chat_id: int | None = None, *, limit: int = 0) -> list[dict]:
    """The recorded people, most recently seen first.

    ``limit`` of 0 means "no bound from here" — the caller is expected to pass
    the configured ceiling. The ordering is what makes a bounded read useful: if
    the table has to be cut short, the people who have actually been in the room
    recently are the ones worth keeping.
    """
    sql = (
        "SELECT chat_id, user_id, first_name, last_name, username, message_count, "
        "first_seen, last_seen FROM people"
    )
    args: tuple = ()
    if chat_id is not None:
        sql += " WHERE chat_id=?"
        args = (int(chat_id),)
    sql += " ORDER BY last_seen DESC"
    if limit:
        sql += " LIMIT ?"
        args = args + (int(limit),)
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    return [
        {
            "chat_id": int(row[0]),
            "user_id": int(row[1]),
            "first_name": str(row[2] or ""),
            "last_name": str(row[3] or ""),
            "username": str(row[4] or ""),
            "message_count": int(row[5] or 0),
            "first_seen": int(row[6] or 0),
            "last_seen": int(row[7] or 0),
        }
        for row in rows
    ]


def people_prune(*, keep: int = 0, max_age: int = 0) -> int:
    """Apply the two retention bounds. Returns how many rows were dropped.

    Both bounds are needed and they are not alternatives. The age bound drops
    somebody who has not been seen for months; the row bound drops the least
    recently seen rows once the table is over its ceiling. A table with only the
    first would still grow without limit in a busy group, and one with only the
    second would keep a person who left a year ago because nobody new arrived.
    """
    dropped = 0
    if max_age and max_age > 0:
        cutoff = int(time.time()) - int(max_age)
        with _lock:
            cur = _conn.execute("DELETE FROM people WHERE last_seen < ?", (cutoff,))
            _conn.commit()
            dropped += cur.rowcount
    if keep and keep > 0:
        with _lock:
            cur = _conn.execute(
                "DELETE FROM people WHERE (chat_id, user_id) NOT IN "
                "(SELECT chat_id, user_id FROM people "
                " ORDER BY last_seen DESC LIMIT ?)",
                (int(keep),),
            )
            _conn.commit()
            dropped += cur.rowcount
    return dropped


def people_count() -> int:
    with _lock:
        return int(_conn.execute("SELECT COUNT(*) FROM people").fetchone()[0])


def people_reset() -> None:
    """Forget every recorded person. For tests."""
    with _lock:
        _conn.execute("DELETE FROM people")
        _conn.commit()


# ── Nexus Awareness: the room window and the understanding of it ──────────
# The roles a captured message may carry. The server writes one of these from
# ``app/rbac.py``; nothing else does, and nothing reads them back to decide
# anything. They exist so the model can be told, in the transcript, that the
# person who said something was the owner rather than a stranger.
GROUP_ROLES = ("owner", "admin", "member", "nexus")

# The text stored per message. Attacker-controlled, so it is truncated hard, on
# the same reasoning as ``chat_append``: a pasted novel must not become a row
# that every later prompt has to carry.
GROUP_MESSAGE_MAX_CHARS = 2000


def group_append(
    chat_id: int, user_id: int, role: str, name: str, text: str
) -> int:
    """Record one message the bot actually received. Returns its row id.

    No model call and no decision: this is the capture half of awareness, and it
    is deliberately the cheapest thing in the pipeline. The row id is returned
    because it is what the awareness pass records as "understood up to here".
    """
    role = role if role in GROUP_ROLES else "member"
    with _lock:
        cur = _conn.execute(
            "INSERT INTO group_messages (chat_id, user_id, role, name, text, at) "
            "VALUES (?,?,?,?,?,?)",
            (
                int(chat_id),
                int(user_id),
                role,
                (name or "")[:120],
                (text or "")[:GROUP_MESSAGE_MAX_CHARS],
                int(time.time()),
            ),
        )
        _conn.commit()
        return int(cur.lastrowid or 0)


def group_window(
    chat_id: int, *, limit: int, ttl: int = 0
) -> list[dict]:
    """The bounded recent view of one room, oldest first.

    Two bounds again, for the same reason ``chat_history`` has two: a count
    bound alone would let a message from last week reappear, and an age bound
    alone would let a busy hour produce an unbounded prompt.

    Scoped by ``chat_id`` and by nothing else, which is the isolation: a room's
    conversation is never visible to another room, because no query here can be
    asked without a chat id.
    """
    limit = max(1, int(limit))
    sql = (
        "SELECT id, user_id, role, name, text, at FROM group_messages "
        "WHERE chat_id=?"
    )
    args: list = [int(chat_id)]
    if ttl and ttl > 0:
        sql += " AND at>=?"
        args.append(int(time.time()) - int(ttl))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    # Reversed: the query takes the newest N, the prompt wants them in the order
    # they were said.
    return [
        {
            "id": int(r[0]),
            "user_id": int(r[1]),
            "role": str(r[2]),
            "name": str(r[3]),
            "text": str(r[4]),
            "at": int(r[5]),
        }
        for r in reversed(rows)
    ]


def group_capture(
    chat_id: int,
    user_id: int,
    role: str,
    name: str,
    text: str,
    *,
    keep: int,
) -> int:
    """Append one message and trim the room, in **one** transaction.

    The same two statements as ``group_append`` followed by ``group_trim``, and
    they are together here because the split cost three commits per received
    message where one will do. Every commit is an fsync, this runs on the
    message handler's own path, and it runs for every message the bot can see
    whether or not it will ever be answered — so the per-message cost is paid
    constantly and had no reason to be three times what it needs to be.

    The trim is not optional and not deferred: a flood that outruns the age
    bound is what makes the window grow, and the two statements are only safe
    apart because neither can be seen without the other.
    """
    role = role if role in GROUP_ROLES else "member"
    keep = max(1, int(keep))
    with _lock:
        cur = _conn.execute(
            "INSERT INTO group_messages (chat_id, user_id, role, name, text, at) "
            "VALUES (?,?,?,?,?,?)",
            (
                int(chat_id),
                int(user_id),
                role,
                (name or "")[:120],
                (text or "")[:GROUP_MESSAGE_MAX_CHARS],
                int(time.time()),
            ),
        )
        row_id = int(cur.lastrowid or 0)
        _conn.execute(
            "DELETE FROM group_messages WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM group_messages WHERE chat_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (int(chat_id), int(chat_id), keep),
        )
        _conn.commit()
    return row_id


def group_trim(chat_id: int, *, keep: int) -> int:
    """Keep only the newest ``keep`` messages for one room.

    Called after every capture, which is what stops a flood from growing the
    table faster than the age bound removes it.
    """
    keep = max(0, int(keep))
    with _lock:
        cur = _conn.execute(
            "DELETE FROM group_messages WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM group_messages WHERE chat_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (int(chat_id), int(chat_id), keep),
        )
        _conn.commit()
        return cur.rowcount


def group_purge(ttl: int) -> int:
    """Drop every captured message older than ``ttl``, across all rooms."""
    cutoff = int(time.time()) - max(1, int(ttl))
    with _lock:
        cur = _conn.execute("DELETE FROM group_messages WHERE at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


def group_pending() -> list[dict]:
    """Every room with a message the awareness pass has not yet read.

    One query, grouped by chat, and the numbers it returns are exactly what the
    debounce policy needs: when the oldest unread message arrived (how long this
    has been waiting), when the newest one arrived (whether the room has gone
    quiet), and the highest id (what to record as understood once the pass
    finishes).

    A room with no ``awareness_state`` row is pending by definition, because
    ``COALESCE`` treats "never analysed" as "understood nothing".

    **The assistant's own messages are excluded, and that is load-bearing.** They
    are in the window, because the model has to see what it already said or it
    repeats itself — but they must not make the room *pending*. Counting them
    would mean every reply scheduled the next pass, which would mean a pass
    every tick for as long as the bot kept talking: a conversation with itself
    that never ends and never stops spending the awareness allowance. Only a
    human speaking makes a room worth reading again.
    """
    with _lock:
        rows = _conn.execute(
            "SELECT g.chat_id, MIN(g.at), MAX(g.at), MAX(g.id), COUNT(*) "
            "FROM group_messages g "
            "LEFT JOIN awareness_state a ON a.chat_id = g.chat_id "
            "WHERE g.role != 'nexus' AND g.id > COALESCE(a.seen_message_id, 0) "
            "GROUP BY g.chat_id"
        ).fetchall()
    return [
        {
            "chat_id": int(r[0]),
            "oldest_at": int(r[1] or 0),
            "newest_at": int(r[2] or 0),
            "max_id": int(r[3] or 0),
            "pending": int(r[4] or 0),
        }
        for r in rows
    ]


def awareness_get(chat_id: int) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT chat_id, updated_at, seen_message_id, passes, relevant, "
            "topic, summary, participants FROM awareness_state WHERE chat_id=?",
            (int(chat_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "chat_id": int(row[0]),
        "updated_at": int(row[1] or 0),
        "seen_message_id": int(row[2] or 0),
        "passes": int(row[3] or 0),
        "relevant": bool(row[4]),
        "topic": str(row[5] or ""),
        "summary": str(row[6] or ""),
        "participants": str(row[7] or ""),
    }


def awareness_set(
    chat_id: int,
    *,
    seen_message_id: int = 0,
    relevant: bool = False,
    topic: str = "",
    summary: str = "",
    participants: str = "",
) -> dict:
    """Record what a completed pass understood about one room.

    Every field is truncated here rather than by the caller: the values come
    from a model, and a model that answers at length must not be able to grow a
    row without bound.
    """
    now = int(time.time())
    with _lock:
        _conn.execute(
            "INSERT INTO awareness_state "
            "(chat_id, updated_at, seen_message_id, passes, relevant, topic, "
            " summary, participants) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "updated_at=excluded.updated_at, "
            # Monotonic: a pass over an older window must never move the
            # watermark backwards, or the messages it skipped would be re-read
            # on every tick.
            "seen_message_id=MAX(awareness_state.seen_message_id, "
            "                    excluded.seen_message_id), "
            "passes=awareness_state.passes + 1, "
            "relevant=excluded.relevant, "
            "topic=excluded.topic, "
            "summary=excluded.summary, "
            "participants=excluded.participants",
            (
                int(chat_id),
                now,
                int(seen_message_id),
                1,
                1 if relevant else 0,
                (topic or "")[:400],
                (summary or "")[:1200],
                (participants or "")[:400],
            ),
        )
        _conn.commit()
    return awareness_get(chat_id) or {}


def awareness_advance(chat_id: int, *, seen_message_id: int) -> None:
    """Move the watermark without recording an understanding.

    Used when a pass did not complete — the model was unreachable, or its answer
    could not be read. The watermark has to move anyway, because otherwise the
    sweeper would retry the same batch on every tick for as long as the outage
    lasted. Nothing permanent is lost by doing so: the window still holds the
    messages, so the next pass that *does* complete re-reads them and the
    understanding is rebuilt from the source of truth rather than from the
    cache.
    """
    with _lock:
        _conn.execute(
            "INSERT INTO awareness_state (chat_id, updated_at, seen_message_id, "
            "passes) VALUES (?,?,?,0) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "seen_message_id=MAX(awareness_state.seen_message_id, "
            "                    excluded.seen_message_id)",
            (int(chat_id), int(time.time()), int(seen_message_id)),
        )
        _conn.commit()


def awareness_reset() -> None:
    """Forget every room's understanding and window. For tests."""
    with _lock:
        _conn.execute("DELETE FROM awareness_state")
        _conn.execute("DELETE FROM group_messages")
        _conn.commit()
