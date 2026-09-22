"""Tiny SQLite store: strikes (confirmed moderation actions) and the rest."""
import sqlite3
import threading
import time
import uuid

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
    # The actor's *role at the time of the action*, and the request id that ties
    # this row to the idempotency record.
    #
    # Role: "who did this" stops being answerable the moment a role changes. An
    # administrator who is later promoted or demoted leaves a trail that says
    # only that they acted, and whether they acted with the authority they held
    # is exactly the question an audit trail exists to answer. Resolved from
    # ``rbac`` at write time and never taken from the request, because the
    # request is what is being audited.
    #
    # Request id: the join key. The outcome a person saw and the row that
    # records it were previously linked only by matching actor, chat, operation
    # and target by hand.
    _ensure_column("admin_audit", "role", "TEXT NOT NULL DEFAULT ''")
    _ensure_column("admin_audit", "request_id", "TEXT NOT NULL DEFAULT ''")
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
    # A second index, for the retention sweep rather than for the dedup lookup.
    # The dedup index above ends in ``at``, so it cannot serve a range scan on
    # ``at`` alone — a delete by age would read the whole table. This table is
    # the one in the schema that grows with activity rather than with the number
    # of accounts or days, so it is the one that needs a bounded delete.
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemini_events_at ON gemini_events(at)"
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
    # Whether the awareness layer is running, as a fact about the deployment
    # rather than about the process.
    #
    # It is persisted for the same reason ``nexus_state`` is, and the reason is
    # the whole point of the switch: the owner turns awareness off to get the
    # conversational speed back, and a container restart must not silently turn
    # it back on. An in-memory flag would do exactly that, and it would do it at
    # the least convenient moment — a redeploy.
    #
    # Deliberately **not** a column on ``nexus_state``. The two are different
    # facts with different defaults and different consequences: Nexus off is
    # "the assistant is silent", awareness off is "the assistant is answering,
    # and not reading the room". Sharing a row would make one switch's default
    # decide the other's, which is the confusion this table exists to prevent.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS awareness_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 1,
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
    # The reply edge, stored as structure rather than as words in the body.
    #
    # This is the fix for the defect the owner reported as "it asks who you want
    # to mute, ten minutes after you told it". The reply target used to be
    # written into ``text`` as a bracketed sentence — «[در پاسخ به X (123)] ...» —
    # which put the one fact an instruction needs inside a string the model had
    # to parse, and which the trusted-context block then flatly contradicted by
    # saying there was no referent. As columns it is a fact about the row, the
    # renderer can show it as an edge, and the pass can read it without guessing.
    #
    # ``directed`` records whether the message addressed Nexus at capture time,
    # and ``actor`` whether its sender held any authority then. Both are hints
    # for choosing the anchor; neither is authority, because authority is
    # re-resolved from the id on every turn.
    _ensure_column("group_messages", "message_id", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column("group_messages", "reply_user_id", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column("group_messages", "reply_name", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        "group_messages", "reply_message_id", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column("group_messages", "directed", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column("group_messages", "actor", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column("group_messages", "kind", "TEXT NOT NULL DEFAULT ''")
    # The room's own newest id, so the anchor query does not have to scan. The
    # index above covers (chat_id, id) already; this one covers the directed
    # filter, which is the shape the anchor actually asks for.
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_messages_directed "
        "ON group_messages(chat_id, directed, id)"
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
    # One coding-agent task. The row is the *index*: it is written by the
    # container, which is the only writer, and read by the host runner, which
    # executes the agent. Everything the runner needs is here, and everything
    # here came from the server — the repository path is resolved from
    # configuration before the row is written, never from anything the model
    # produced, so a request cannot name a directory the deployment did not
    # already allow.
    #
    # ``status`` is the lifecycle the brief names: queued, running,
    # waiting_for_owner, succeeded, failed, cancelled, timed_out. It is in the
    # database rather than in memory because the whole point of persisting a
    # task is that a restart must not run it twice.
    #
    # ``progress_offset`` is how many bytes of the runner's progress file have
    # already been delivered to Telegram. It is stored here for the same
    # reason: a restart must resume the stream rather than repeat it.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_tasks (
            request_id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            actor_id INTEGER NOT NULL DEFAULT 0,
            chat_id INTEGER NOT NULL DEFAULT 0,
            repository TEXT NOT NULL DEFAULT '',
            repo_path TEXT NOT NULL DEFAULT '',
            task TEXT NOT NULL DEFAULT '',
            operation TEXT NOT NULL DEFAULT '',
            reply_mode TEXT NOT NULL DEFAULT 'text',
            status TEXT NOT NULL DEFAULT 'queued',
            danger TEXT NOT NULL DEFAULT '',
            confirmed_by INTEGER NOT NULL DEFAULT 0,
            confirmed_at INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL DEFAULT 0,
            finished_at INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            progress_offset INTEGER NOT NULL DEFAULT 0)"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status "
        "ON agent_tasks(status, created_at)"
    )
    # One row per Telegram update this bot has already handled.
    #
    # Telegram re-delivers an update whenever it is not certain the bot received
    # it — after a network failure, and after a restart, because the update
    # offset is not persisted and the bot asks for the backlog again. Without
    # this table the same message is answered twice, which is a real duplicate
    # rather than a cosmetic one: a second reply, a second moderation action, a
    # second model call.
    #
    # The primary key *is* the mechanism. ``INSERT OR IGNORE`` on a unique key
    # is atomic in SQLite, so two concurrent deliveries of the same id cannot
    # both win, and the check needs no lock of its own.
    #
    # The ids are Telegram's and are never reused, so a row here can only ever
    # mean "this exact update was handled" — which is why dropping a duplicate is
    # safe and cannot lose a real event. The table is bounded by age
    # (``seen_updates_prune``); a few hundred bytes per day is a cheaper price
    # than a duplicate reply.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS seen_updates (
            update_id INTEGER PRIMARY KEY,
            at INTEGER NOT NULL)"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seen_updates_at ON seen_updates(at)"
    )
    # A stable, opaque handle for one Telegram user.
    #
    # Telegram user ids are the *authority* everywhere in this codebase, and
    # this table does not change that: nothing reads a uuid to decide whether
    # somebody may do something. What it adds is a second name for the same
    # person that is not a phone-adjacent number, so operational records, logs
    # and the assistant's own answers can refer to somebody without repeating
    # their Telegram id in every line — and so a person's identity can be
    # correlated across chats without treating the numeric id as the only
    # possible key.
    #
    # It is deliberately global rather than per-chat: a Telegram user id is
    # global, and a per-chat uuid would make the same person two people the
    # moment they spoke in a second group.
    #
    # The uuid is generated once, on first sight, and never derived from the
    # Telegram id — a derived value would be reversible and would make the
    # opaque handle a weak alias for the number it is meant to stand apart
    # from. The column is unique so a collision (or a hand-edited row) is
    # refused by the database rather than silently shadowing somebody.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS identities (
            user_id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0)"""
    )
    # How identity lookups have been ending, as counts rather than as rows.
    #
    # This is the one awareness-side signal that cannot be derived from
    # anything else: a name that matches two people leaves no other trace, and
    # "resolution keeps coming back ambiguous" is the difference between a
    # group whose members share a first name and a resolver that is broken. It
    # is a counter table rather than a log because the question is a rate, and
    # a rate does not need the individual calls.
    #
    # Written only from ``app/identity.py``, and only on the admin tool path —
    # ``resolve`` is reached when a person asks about a person, not on the
    # message path — so this is not a write added to the hot path.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS identity_resolutions (
            outcome TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            last_at INTEGER NOT NULL DEFAULT 0)"""
    )
    # A VPN-side operation the owner has been asked to approve but has not yet.
    #
    # This exists because three of the six VPN operations move money or
    # bulk-reject orders, and a misheard number in a Persian sentence is not
    # recoverable. The row is the whole point of the confirmation: what the
    # model supplies at confirm time is a *reference* to one of these, never the
    # parameters, so the amount that eventually reaches the panel is the amount
    # that was recorded here — not something re-derived from a second message.
    #
    # ``expires_at`` is what stops a stale approval from being usable later; a
    # confirmation is about a request somebody made a moment ago, not a standing
    # permission. ``status`` moves pending → confirmed → done/failed, and the
    # claim is a compare-and-swap so two confirmations cannot both execute it.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS vpn_pending_ops (
            request_id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL DEFAULT 0,
            actor_id INTEGER NOT NULL DEFAULT 0,
            chat_id INTEGER NOT NULL DEFAULT 0,
            operation TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            confirmed_by INTEGER NOT NULL DEFAULT 0,
            confirmed_at INTEGER NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '')"""
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


def ai_day_seconds_left(now: float | None = None) -> float:
    """Seconds until the API day rolls over, in ``(0, 86400]``.

    The daily allowance is a *day's* budget, so anything that wants to spend it
    across the day rather than in the first hour needs the day's own clock. It
    is derived from the same offset as ``ai_day`` rather than from local
    midnight, because the reset it is counting down to is the provider's.
    """
    stamp = time.time() if now is None else float(now)
    return 86400.0 - ((stamp - _API_DAY_OFFSET) % 86400.0)


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
    role: str = "",
    request_id: str = "",
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

    ``role`` and ``request_id`` are the actor's authority and the request's
    identity, both supplied by the caller that resolved them — ``admin_service``
    resolves the role from ``rbac`` and never from the request. An empty value
    is written as empty rather than guessed at: a row that invents an authority
    is worse than a row that admits it does not have one.
    """
    _exec(
        "INSERT INTO admin_audit (at, actor_id, action, target_id, chat_id, "
        "outcome, detail, interface, role, request_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            int(time.time()),
            int(actor_id),
            str(action)[:80],
            int(target_id) if target_id is not None else None,
            int(chat_id) if chat_id is not None else None,
            str(outcome)[:40],
            str(detail)[:300],
            str(interface)[:16],
            str(role)[:32],
            str(request_id)[:120],
        ),
    )


_AUDIT_COLS = (
    "at, actor_id, action, target_id, chat_id, outcome, detail, interface, "
    "role, request_id"
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
        "role": r[8] or "",
        "request_id": r[9] or "",
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


def audit_for_user(
    user_id: int,
    *,
    chat_id: int | None = None,
    since: int = 0,
    limit: int = 20,
) -> list[dict]:
    """Audit rows in which this user was either the actor or the target.

    Both directions, because "why was this person never promoted?" and "what did
    this person do?" are the same lookup from opposite ends and the assistant is
    asked both. Bounded by a time window, a room and a count for the same reason
    every other read here is: this is operational history the assistant may
    summarise, not a copy of the audit table in a prompt.
    """
    sql = (
        f"SELECT {_AUDIT_COLS} FROM admin_audit "
        "WHERE (actor_id = ? OR target_id = ?) AND at >= ?"
    )
    args: list = [int(user_id), int(user_id), int(since)]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        args.append(int(chat_id))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
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
    in ``users`` are not administrative state and are not this function's to
    erase.
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


def events_prune(keep_seconds: int) -> int:
    """Drop pool events older than ``keep_seconds``. Returns rows removed.

    ``gemini_events`` is the one table here that grows with *activity* rather
    than with the number of accounts, days or people, so it is the one that
    needs a bound. It is telemetry rather than audit — the audit trail is
    ``admin_audit``, which has its own window and its own rule — so pruning it
    loses no record of who did what.

    What it does lose is diagnostic history, which is why the default window is
    generous: this table is what answered "why was Gemini rate-limited" during
    the incident that produced the awareness pacing change, and a window short
    enough to have discarded that evidence would have made the question
    unanswerable.
    """
    if keep_seconds <= 0:
        return 0
    cutoff = int(time.time()) - int(keep_seconds)
    with _lock:
        cur = _conn.execute("DELETE FROM gemini_events WHERE at < ?", (cutoff,))
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


def awareness_control_get() -> dict | None:
    """The stored awareness switch, or None when it has never been set.

    ``None`` means "nobody has ever touched this switch", which is the normal
    state of a fresh install and deliberately not the same fact as "off". The
    caller supplies the default, exactly as ``nexus_state_get`` does, so that a
    deployment which has never used the switch keeps the behaviour its
    configuration asked for.
    """
    with _lock:
        row = _conn.execute(
            "SELECT enabled, changed_at, changed_by, reason "
            "FROM awareness_control WHERE id=1"
        ).fetchone()
    if not row:
        return None
    return {
        "enabled": bool(row[0]),
        "changed_at": int(row[1] or 0),
        "changed_by": int(row[2] or 0),
        "reason": str(row[3] or ""),
    }


def awareness_control_set(
    enabled: bool, *, actor_id: int = 0, reason: str = ""
) -> dict:
    """Write the one switch row and return it. Last write wins, by design.

    The switch is a single fact about the deployment, not an append-only
    record, so an update is correct here where it would be wrong for the audit
    table: the audit trail keeps the history of who toggled it, and this keeps
    the current answer.
    """
    row = {
        "enabled": bool(enabled),
        "changed_at": int(time.time()),
        "changed_by": int(actor_id or 0),
        "reason": str(reason or "")[:200],
    }
    _exec(
        """INSERT INTO awareness_control
               (id, enabled, changed_at, changed_by, reason)
           VALUES (1,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               enabled=excluded.enabled,
               changed_at=excluded.changed_at,
               changed_by=excluded.changed_by,
               reason=excluded.reason""",
        (
            int(row["enabled"]),
            row["changed_at"],
            row["changed_by"],
            row["reason"],
        ),
    )
    return row


def awareness_control_reset() -> None:
    """Forget the switch row, so the configured default applies again."""
    with _lock:
        _conn.execute("DELETE FROM awareness_control")
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


# ── Identities: the opaque handle for a Telegram user ─────────────────────
def identity_ensure(user_id: int) -> dict:
    """Return this user's identity row, creating it on first sight.

    One statement to insert and one to read, rather than a read-then-insert:
    two processes handling the same person's first two messages concurrently
    would otherwise both see "no row" and both try to create one. ``INSERT OR
    IGNORE`` on the primary key makes the loser a no-op, and the read afterwards
    is what guarantees both callers return the same uuid rather than the one
    their own insert proposed.
    """
    user_id = int(user_id)
    if user_id <= 0:
        return {}
    now = int(time.time())
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO identities (user_id, uuid, created_at, last_seen) "
            "VALUES (?,?,?,?)",
            (user_id, uuid.uuid4().hex, now, now),
        )
        _conn.execute(
            "UPDATE identities SET last_seen=? WHERE user_id=?", (now, user_id)
        )
        _conn.commit()
        row = _conn.execute(
            "SELECT user_id, uuid, created_at, last_seen FROM identities WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "user_id": int(row[0]),
        "uuid": str(row[1]),
        "created_at": int(row[2] or 0),
        "last_seen": int(row[3] or 0),
    }


def identity_get(user_id: int) -> dict | None:
    """The stored identity row for a Telegram user, or ``None`` if never seen."""
    with _lock:
        row = _conn.execute(
            "SELECT user_id, uuid, created_at, last_seen FROM identities WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": int(row[0]),
        "uuid": str(row[1]),
        "created_at": int(row[2] or 0),
        "last_seen": int(row[3] or 0),
    }


def identity_by_uuid(value: str) -> dict | None:
    """The identity row for an opaque uuid, or ``None``. Exact match only."""
    wanted = (value or "").strip().lower()
    if not wanted:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT user_id, uuid, created_at, last_seen FROM identities WHERE uuid=?",
            (wanted,),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": int(row[0]),
        "uuid": str(row[1]),
        "created_at": int(row[2] or 0),
        "last_seen": int(row[3] or 0),
    }


def identity_count() -> int:
    with _lock:
        return int(_conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0])


def identity_reset() -> None:
    """Forget every identity. For tests."""
    with _lock:
        _conn.execute("DELETE FROM identities")
        _conn.execute("DELETE FROM identity_resolutions")
        _conn.commit()


def identity_resolution_bump(outcome: str) -> None:
    """Count one identity lookup by how it ended. Never raises.

    A metrics write must not be able to break a lookup: if the counter cannot
    be written, the caller's answer is still correct and still worth returning.
    """
    try:
        with _lock:
            _conn.execute(
                "INSERT INTO identity_resolutions (outcome, count, last_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(outcome) DO UPDATE SET "
                "count = count + 1, last_at = excluded.last_at",
                (str(outcome)[:32], int(time.time())),
            )
            _conn.commit()
    except Exception:  # noqa: BLE001 - a counter is never worth an exception
        pass


def identity_resolution_counts() -> dict[str, int]:
    """``{outcome: count}`` for every outcome seen since the last reset."""
    with _lock:
        rows = _conn.execute(
            "SELECT outcome, count FROM identity_resolutions"
        ).fetchall()
    return {str(r[0]): int(r[1] or 0) for r in rows}


# ── VPN operations awaiting the owner's confirmation ──────────────────────
_VPN_PENDING_COLS = (
    "request_id, created_at, expires_at, actor_id, chat_id, operation, "
    "subject, payload, status, confirmed_by, confirmed_at, outcome, detail"
)


def _vpn_pending_row(row) -> dict:
    return {
        "request_id": str(row[0]),
        "created_at": int(row[1] or 0),
        "expires_at": int(row[2] or 0),
        "actor_id": int(row[3] or 0),
        "chat_id": int(row[4] or 0),
        "operation": str(row[5] or ""),
        "subject": str(row[6] or ""),
        "payload": str(row[7] or "{}"),
        "status": str(row[8] or ""),
        "confirmed_by": int(row[9] or 0),
        "confirmed_at": int(row[10] or 0),
        "outcome": str(row[11] or ""),
        "detail": str(row[12] or ""),
    }


def vpn_pending_add(
    request_id: str,
    *,
    actor_id: int,
    chat_id: int,
    operation: str,
    subject: str,
    payload: str,
    expires_at: int,
    now: int | None = None,
) -> bool:
    """Record one operation as awaiting approval. ``False`` if the id is taken."""
    stamp = int(now if now is not None else time.time())
    try:
        with _lock:
            _conn.execute(
                "INSERT INTO vpn_pending_ops "
                f"({_VPN_PENDING_COLS}) VALUES (?,?,?,?,?,?,?,?,'pending',0,0,'','')",
                (
                    str(request_id),
                    stamp,
                    int(expires_at),
                    int(actor_id),
                    int(chat_id),
                    str(operation),
                    str(subject),
                    str(payload),
                ),
            )
            _conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def vpn_pending_waiting(
    *, actor_id: int = 0, chat_id: int = 0, now: int | None = None
) -> list[dict]:
    """Unapproved operations that have not expired, oldest first.

    Scoped to the actor and the room when those are given, for the same reason
    ``audit_recent_actions`` is: one person's pending operation is not another's
    to approve, and one group's business stays out of another's.
    """
    stamp = int(now if now is not None else time.time())
    sql = (
        f"SELECT {_VPN_PENDING_COLS} FROM vpn_pending_ops "
        "WHERE status='pending' AND expires_at > ?"
    )
    args: list = [stamp]
    if actor_id:
        sql += " AND actor_id = ?"
        args.append(int(actor_id))
    if chat_id:
        sql += " AND chat_id = ?"
        args.append(int(chat_id))
    sql += " ORDER BY created_at ASC"
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_vpn_pending_row(r) for r in rows]


def vpn_pending_get(request_id: str) -> dict | None:
    with _lock:
        row = _conn.execute(
            f"SELECT {_VPN_PENDING_COLS} FROM vpn_pending_ops WHERE request_id = ?",
            (str(request_id),),
        ).fetchone()
    return _vpn_pending_row(row) if row else None


def vpn_pending_claim(request_id: str, *, now: int | None = None) -> bool:
    """Take ownership of one pending operation. ``True`` for the winner only.

    A compare-and-swap on one row, like the update-dedup claim and for the same
    reason: two confirmations arriving together must not both execute. The
    loser finds the row already claimed and does nothing.
    """
    stamp = int(now if now is not None else time.time())
    with _lock:
        cursor = _conn.execute(
            "UPDATE vpn_pending_ops SET status='confirmed', confirmed_at=? "
            "WHERE request_id = ? AND status='pending' AND expires_at > ?",
            (stamp, str(request_id), stamp),
        )
        _conn.commit()
        return cursor.rowcount == 1


def vpn_pending_finish(request_id: str, *, outcome: str, detail: str = "") -> None:
    with _lock:
        _conn.execute(
            "UPDATE vpn_pending_ops SET status='done', outcome=?, detail=? "
            "WHERE request_id = ?",
            (str(outcome)[:64], str(detail)[:400], str(request_id)),
        )
        _conn.commit()


def vpn_pending_release(request_id: str) -> None:
    """Put a claimed operation back, for a failure before anything happened."""
    with _lock:
        _conn.execute(
            "UPDATE vpn_pending_ops SET status='pending', confirmed_at=0 "
            "WHERE request_id = ? AND status='confirmed'",
            (str(request_id),),
        )
        _conn.commit()


def vpn_pending_reset() -> None:
    """Forget every pending operation. For tests."""
    with _lock:
        _conn.execute("DELETE FROM vpn_pending_ops")
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

    Every column is returned, including the reply edge and the two capture-time
    hints, because the caller renders a *conversation* rather than a list of
    lines: who replied to whom is the structure that makes «این رو سکوت کن»
    resolvable at all.
    """
    limit = max(1, int(limit))
    sql = (
        "SELECT id, user_id, role, name, text, at, message_id, reply_user_id, "
        "reply_name, reply_message_id, directed, actor, kind FROM group_messages "
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
            "message_id": int(r[6] or 0),
            "reply_user_id": int(r[7] or 0),
            "reply_name": str(r[8] or ""),
            "reply_message_id": int(r[9] or 0),
            "directed": bool(r[10]),
            "actor": bool(r[11]),
            "kind": str(r[12] or ""),
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
    message_id: int = 0,
    reply_user_id: int = 0,
    reply_name: str = "",
    reply_message_id: int = 0,
    directed: bool = False,
    actor: bool = False,
    kind: str = "",
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

    The reply edge and the two hints are optional so that the two callers which
    have nothing to say about them — the assistant's own turn, and a test — do
    not have to pass them.
    """
    role = role if role in GROUP_ROLES else "member"
    keep = max(1, int(keep))
    with _lock:
        cur = _conn.execute(
            "INSERT INTO group_messages (chat_id, user_id, role, name, text, at, "
            "message_id, reply_user_id, reply_name, reply_message_id, directed, "
            "actor, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(chat_id),
                int(user_id),
                role,
                (name or "")[:120],
                (text or "")[:GROUP_MESSAGE_MAX_CHARS],
                int(time.time()),
                int(message_id or 0),
                int(reply_user_id or 0),
                (reply_name or "")[:120],
                int(reply_message_id or 0),
                1 if directed else 0,
                1 if actor else 0,
                (kind or "")[:32],
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
    """Forget every room's understanding and window, and the owner's switch.

    For tests. The switch belongs here as much as the rooms do: it is the one
    other piece of awareness state that outlives a single test, and a test that
    switched the layer off without clearing it would leave every later test in
    the process reading an empty room — a failure that reads as "awareness is
    broken" rather than as a leaked fixture.
    """
    with _lock:
        _conn.execute("DELETE FROM awareness_state")
        _conn.execute("DELETE FROM group_messages")
        _conn.execute("DELETE FROM awareness_control")
        _conn.commit()


def awareness_summary() -> dict:
    """Aggregate awareness counters across every room.

    Derived from the per-room rows that already exist rather than from a second
    counter store: two places recording the same number is two places for them
    to disagree, and the per-room row is the thing a pass actually writes. One
    query, no model, cheap enough to call from a status command.
    """
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(passes),0), COALESCE(SUM(relevant),0) "
            "FROM awareness_state"
        ).fetchone()
        replies = _conn.execute(
            "SELECT COUNT(*) FROM group_messages WHERE role='nexus'"
        ).fetchone()
    return {
        "rooms": int(row[0] or 0),
        "passes": int(row[1] or 0),
        "relevant": int(row[2] or 0),
        "replies": int(replies[0] or 0),
    }


def group_role_counts() -> dict:
    """How many captured messages each role has, as ``{role: count}``."""
    with _lock:
        rows = _conn.execute(
            "SELECT role, COUNT(*) FROM group_messages GROUP BY role"
        ).fetchall()
    return {str(r[0] or "member"): int(r[1] or 0) for r in rows}


# ── The coding-agent task store ───────────────────────────────────────────
# The lifecycle the brief names, and the storage vocabulary. A status outside
# this tuple is refused rather than written: an unrecognised state is a task
# that no reader can classify, and a task nothing can classify is one that is
# either retried for ever or never resumed.
AGENT_STATUSES = (
    "queued",
    "running",
    "waiting_for_owner",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
)

# The states a task is in while something is still expected of it.
AGENT_ACTIVE_STATUSES = ("queued", "running", "waiting_for_owner")

# The states nothing will move a task out of.
AGENT_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "timed_out")

# Bounds on what a task row may carry. The task text and the result are
# attacker-adjacent (the task came from a person, the result came from a model)
# and both are carried into a Telegram message, so neither is unbounded.
AGENT_TASK_MAX_CHARS = 4000
AGENT_RESULT_MAX_CHARS = 20000
AGENT_ERROR_MAX_CHARS = 1000

_AGENT_COLS = (
    "request_id, created_at, updated_at, actor_id, chat_id, repository, "
    "repo_path, task, operation, reply_mode, status, danger, confirmed_by, "
    "confirmed_at, started_at, finished_at, result, error, session_id, "
    "progress_offset"
)


def _agent_row(r) -> dict:
    return {
        "request_id": str(r[0]),
        "created_at": int(r[1] or 0),
        "updated_at": int(r[2] or 0),
        "actor_id": int(r[3] or 0),
        "chat_id": int(r[4] or 0),
        "repository": str(r[5] or ""),
        "repo_path": str(r[6] or ""),
        "task": str(r[7] or ""),
        "operation": str(r[8] or ""),
        "reply_mode": str(r[9] or "text"),
        "status": str(r[10] or "queued"),
        "danger": str(r[11] or ""),
        "confirmed_by": int(r[12] or 0),
        "confirmed_at": int(r[13] or 0),
        "started_at": int(r[14] or 0),
        "finished_at": int(r[15] or 0),
        "result": str(r[16] or ""),
        "error": str(r[17] or ""),
        "session_id": str(r[18] or ""),
        "progress_offset": int(r[19] or 0),
    }


def agent_task_create(
    request_id: str,
    *,
    actor_id: int,
    chat_id: int,
    repository: str,
    repo_path: str,
    task: str,
    operation: str,
    reply_mode: str = "text",
    status: str = "queued",
    danger: str = "",
) -> dict:
    """Record one agent task. Returns the stored row.

    Written before anything runs, which is what makes a restart safe: the row
    exists, so the work is either already claimed or still to be claimed, and
    never both.
    """
    if status not in AGENT_STATUSES:
        status = "queued"
    now = int(time.time())
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO agent_tasks ("
            + _AGENT_COLS
            + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,'','','',0)",
            (
                str(request_id)[:64],
                now,
                now,
                int(actor_id),
                int(chat_id),
                str(repository)[:80],
                str(repo_path)[:400],
                str(task)[:AGENT_TASK_MAX_CHARS],
                str(operation)[:40],
                str(reply_mode)[:16],
                status,
                str(danger)[:400],
            ),
        )
        _conn.commit()
        row = _conn.execute(
            f"SELECT {_AGENT_COLS} FROM agent_tasks WHERE request_id=?",
            (str(request_id)[:64],),
        ).fetchone()
    return _agent_row(row) if row else {}


def agent_task_get(request_id: str) -> dict | None:
    with _lock:
        row = _conn.execute(
            f"SELECT {_AGENT_COLS} FROM agent_tasks WHERE request_id=?",
            (str(request_id)[:64],),
        ).fetchone()
    return _agent_row(row) if row else None


def agent_task_update(request_id: str, **fields) -> dict | None:
    """Update the named columns. Unknown names are ignored, never interpolated.

    ``fields`` is a closed set rather than a formatted fragment, because the
    alternative — building the SET clause from the caller's keys — is how a
    value ends up in a column name.
    """
    allowed = {
        "status": (str, 20),
        "result": (str, AGENT_RESULT_MAX_CHARS),
        "error": (str, AGENT_ERROR_MAX_CHARS),
        "session_id": (str, 80),
        "danger": (str, 400),
        "started_at": (int, 0),
        "finished_at": (int, 0),
        "confirmed_by": (int, 0),
        "confirmed_at": (int, 0),
        "progress_offset": (int, 0),
        "repo_path": (str, 400),
        "repository": (str, 80),
        "operation": (str, 40),
        "reply_mode": (str, 16),
        "task": (str, AGENT_TASK_MAX_CHARS),
    }
    assignments: list[str] = []
    args: list = []
    for name, value in fields.items():
        spec = allowed.get(name)
        if spec is None:
            continue
        caster, limit = spec
        if caster is int:
            value = int(value or 0)
        else:
            value = str(value or "")
            if limit:
                value = value[:limit]
        if name == "status" and value not in AGENT_STATUSES:
            continue
        assignments.append(f"{name}=?")
        args.append(value)
    if not assignments:
        return agent_task_get(request_id)
    assignments.append("updated_at=?")
    args.append(int(time.time()))
    args.append(str(request_id)[:64])
    with _lock:
        _conn.execute(
            f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE request_id=?",
            tuple(args),
        )
        _conn.commit()
        row = _conn.execute(
            f"SELECT {_AGENT_COLS} FROM agent_tasks WHERE request_id=?",
            (str(request_id)[:64],),
        ).fetchone()
    return _agent_row(row) if row else None


def agent_task_active(*, repository: str = "", actor_id: int = 0) -> list[dict]:
    """Tasks that are still going, oldest first, optionally filtered."""
    placeholders = ",".join("?" for _ in AGENT_ACTIVE_STATUSES)
    sql = (
        f"SELECT {_AGENT_COLS} FROM agent_tasks WHERE status IN ({placeholders})"
    )
    args: list = list(AGENT_ACTIVE_STATUSES)
    if repository:
        sql += " AND repository=?"
        args.append(str(repository)[:80])
    if actor_id:
        sql += " AND actor_id=?"
        args.append(int(actor_id))
    sql += " ORDER BY created_at ASC, rowid ASC"
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_agent_row(r) for r in rows]


def agent_task_running(*, repository: str = "") -> list[dict]:
    """Tasks a runner has claimed and not finished."""
    sql = f"SELECT {_AGENT_COLS} FROM agent_tasks WHERE status='running'"
    args: list = []
    if repository:
        sql += " AND repository=?"
        args.append(str(repository)[:80])
    sql += " ORDER BY started_at ASC, rowid ASC"
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_agent_row(r) for r in rows]


def agent_task_recent(limit: int = 5, *, actor_id: int = 0) -> list[dict]:
    """The newest tasks, for a status report. Bounded."""
    sql = f"SELECT {_AGENT_COLS} FROM agent_tasks"
    args: list = []
    if actor_id:
        sql += " WHERE actor_id=?"
        args.append(int(actor_id))
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(max(1, min(int(limit), 50)))
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_agent_row(r) for r in rows]


def agent_task_waiting(*, actor_id: int = 0) -> list[dict]:
    """Tasks waiting for the owner's explicit confirmation, oldest first.

    ``started_at=0`` is the whole distinction between the two reasons a task
    sits in ``waiting_for_owner``. A dangerous task that was never approved has
    never run, so its ``started_at`` is zero; a task that *did* run and stopped
    to ask the owner a question has a ``started_at``. Only the first kind is
    released by an approval — confirming the second would re-run work that had
    already begun, which is the opposite of what the owner asked for.
    """
    sql = (
        f"SELECT {_AGENT_COLS} FROM agent_tasks "
        "WHERE status='waiting_for_owner' AND started_at=0"
    )
    args: list = []
    if actor_id:
        sql += " AND actor_id=?"
        args.append(int(actor_id))
    sql += " ORDER BY created_at ASC, rowid ASC"
    with _lock:
        rows = _conn.execute(sql, tuple(args)).fetchall()
    return [_agent_row(r) for r in rows]


def agent_task_prune(keep_seconds: int) -> int:
    """Drop finished tasks older than the window. Best effort."""
    cutoff = int(time.time()) - max(1, int(keep_seconds))
    placeholders = ",".join("?" for _ in AGENT_TERMINAL_STATUSES)
    with _lock:
        cur = _conn.execute(
            f"DELETE FROM agent_tasks WHERE status IN ({placeholders}) "
            "AND finished_at > 0 AND finished_at < ?",
            tuple(list(AGENT_TERMINAL_STATUSES) + [cutoff]),
        )
        _conn.commit()
        return cur.rowcount


def agent_reset() -> None:
    """Forget every task. For tests."""
    with _lock:
        _conn.execute("DELETE FROM agent_tasks")
        _conn.commit()


# ── Update deduplication ──────────────────────────────────────────────────
def update_claim(update_id: int) -> bool:
    """Claim one Telegram update. ``True`` the first time, ``False`` afterwards.

    The whole duplicate-delivery defence is this one statement, and the reason it
    is a statement rather than a read-then-write is the race: two deliveries of
    the same update can arrive close enough together that a ``SELECT`` followed
    by an ``INSERT`` would let both see "not seen yet" and both proceed.
    ``INSERT OR IGNORE`` against the primary key cannot: exactly one of them
    inserts a row, and the other's ``rowcount`` is zero.

    An unknown or zero id is refused rather than stored. Telegram does not issue
    ``update_id`` 0, and treating a missing id as claimable would make every
    update without one collide on a single row.
    """
    update_id = int(update_id or 0)
    if update_id <= 0:
        return False
    with _lock:
        cur = _conn.execute(
            "INSERT OR IGNORE INTO seen_updates (update_id, at) VALUES (?,?)",
            (update_id, int(time.time())),
        )
        _conn.commit()
        return cur.rowcount > 0


def update_seen(update_id: int) -> bool:
    """Whether this update has already been claimed. For reporting and tests."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM seen_updates WHERE update_id=?", (int(update_id or 0),)
        ).fetchone()
    return row is not None


def seen_updates_prune(keep_seconds: int) -> int:
    """Forget update ids older than the window. Best effort.

    The window only has to outlast Telegram's willingness to re-deliver, and
    beyond that a row is dead weight. Nothing is lost by dropping one: an id that
    old will not be sent again, and if it somehow were, the update would simply
    be handled — which is the behaviour before this table existed.
    """
    cutoff = int(time.time()) - max(1, int(keep_seconds))
    with _lock:
        cur = _conn.execute("DELETE FROM seen_updates WHERE at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


def seen_updates_reset() -> None:
    """Forget every claimed update. For tests."""
    with _lock:
        _conn.execute("DELETE FROM seen_updates")
        _conn.commit()
