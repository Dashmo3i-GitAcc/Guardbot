"""Schema migrations: the half of a column addition that CREATE TABLE misses.

`CREATE TABLE IF NOT EXISTS` does nothing at all to a table that already exists,
so a column added to the schema after the first deploy reaches a fresh install
and never reaches the one in production. `db._ensure_column` is the missing
half, and these tests are about that specific failure rather than about any
particular column.
"""
import sqlite3

import pytest

from app import config, db

# The table exactly as it existed before `interface` was added.
_OLD_ADMIN_AUDIT = """
CREATE TABLE admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_id INTEGER,
    chat_id INTEGER,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '')
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """A database carrying the pre-migration schema, wired into the module."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(_OLD_ADMIN_AUDIT)
    conn.execute(
        "INSERT INTO admin_audit (at, actor_id, action, outcome, detail) "
        "VALUES (1, 7, 'moderation.ban', 'ok', 'a row written before the column')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    db.init()
    return path


def _columns(path: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(admin_audit)")]
    finally:
        conn.close()


def test_the_column_is_absent_before_init(old_db, tmp_path):
    """The premise. Without this the rest of the file proves nothing."""
    fresh = str(tmp_path / "untouched.db")
    conn = sqlite3.connect(fresh)
    conn.execute(_OLD_ADMIN_AUDIT)
    conn.commit()
    conn.close()

    assert "interface" not in _columns(fresh)


def test_init_adds_the_column_to_an_existing_table(old_db):
    assert "interface" in _columns(old_db)


def test_running_init_twice_is_harmless(old_db):
    db.init()
    db.init()

    assert _columns(old_db).count("interface") == 1


def test_a_row_written_before_the_migration_still_reads(old_db):
    """The column is additive: old rows must not become unreadable."""
    rows = db.audit_recent(5)

    assert len(rows) == 1
    assert rows[0]["action"] == "moderation.ban"
    assert rows[0]["detail"] == "a row written before the column"
    # Defaulted, not NULL and not missing.
    assert rows[0]["interface"] == ""


def test_a_row_written_after_the_migration_carries_the_column(old_db):
    db.audit_write(7, "moderation.mute", outcome="ok", chat_id=-100, interface="ai")

    newest = db.audit_recent(1)[0]

    assert newest["interface"] == "ai"


def test_ensure_column_leaves_an_existing_column_alone(old_db):
    """It must not try to re-add, and must not touch data while checking."""
    db._ensure_column("admin_audit", "interface", "TEXT NOT NULL DEFAULT 'nope'")
    db.audit_write(7, "moderation.ban", outcome="ok", chat_id=-100, interface="python")

    assert db.audit_recent(1)[0]["interface"] == "python"


def test_init_adds_the_authority_columns_to_an_existing_table(old_db):
    """The same migration, for the fields the brief lists and the table lacked.

    ``role`` and ``request_id`` were added to a table that was already in
    production, so they need exactly the treatment ``interface`` needed — and a
    fresh install would have had them from the start, which is why the failure
    this guards against is invisible in development.
    """
    columns = _columns(old_db)

    assert "role" in columns
    assert "request_id" in columns


def test_a_row_written_before_the_authority_columns_reads_as_empty(old_db):
    """Additive means the rows already there stay readable.

    Empty rather than guessed at: a row from before the columns existed has no
    recorded authority, and inventing one on read would be the record lying
    about its own completeness.
    """
    row = db.audit_recent(1)[0]

    assert row["action"] == "moderation.ban"
    assert row["role"] == ""
    assert row["request_id"] == ""


def test_a_row_written_after_the_authority_columns_carries_them(old_db):
    db.audit_write(
        7, "moderation.mute", outcome="ok", chat_id=-100,
        role="moderator", request_id="req-123",
    )

    row = db.audit_recent(1)[0]

    assert row["role"] == "moderator"
    assert row["request_id"] == "req-123"


# ── The two tables the Nexus layer added ──────────────────────────────────
# `CREATE TABLE IF NOT EXISTS` is enough for a *new* table in a way it is not for
# a new column: an existing database simply does not have it and gets it built.
# That is the property these tests pin — an upgrade must not need a migration
# step, and must not disturb the rows already there.
_OLD_DB = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    strikes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id))
"""


@pytest.fixture
def pre_nexus_db(tmp_path, monkeypatch):
    """A database that predates the Nexus tables, with one row of history."""
    path = str(tmp_path / "pre_nexus.db")
    conn = sqlite3.connect(path)
    conn.execute(_OLD_DB)
    conn.execute("INSERT INTO users (chat_id, user_id, strikes) VALUES (-100, 7, 3)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    db.init()
    return path


def _tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_the_nexus_tables_are_created_on_an_existing_database(pre_nexus_db):
    tables = _tables(pre_nexus_db)
    assert "nexus_state" in tables
    assert "people" in tables


def test_running_init_twice_does_not_disturb_the_nexus_state(pre_nexus_db):
    db.nexus_state_set("offline", actor_id=999, reason="test")
    db.init()
    db.init()

    assert db.nexus_state_get()["state"] == "offline"


def test_an_existing_database_keeps_its_rows(pre_nexus_db):
    """The new tables are additive: nothing that was there is lost."""
    assert db.get_strikes(-100, 7) == 3


def test_the_nexus_state_round_trips(pre_nexus_db):
    assert db.nexus_state_get() is None
    db.nexus_state_set("offline", actor_id=999, reason="owner said so")
    row = db.nexus_state_get()
    assert row["state"] == "offline"
    assert row["changed_by"] == 999
    assert row["reason"] == "owner said so"
    assert row["changed_at"] > 0


def test_the_people_table_round_trips(pre_nexus_db):
    db.people_remember(-100, 7, first_name="Milad", last_name="R", username="milad")
    db.people_remember(-100, 7, first_name="Milad", last_name="R", username="milad")
    rows = db.people_rows(-100)
    assert len(rows) == 1, "an upsert, not a second row"
    assert rows[0]["message_count"] == 2


# ── The tables Group Awareness added ──────────────────────────────────────
# Same property, one layer later: an upgrade from the Nexus-era database must
# not need a migration step, and the room window must survive a restart.
def test_the_awareness_tables_are_created_on_an_existing_database(pre_nexus_db):
    tables = _tables(pre_nexus_db)
    assert "group_messages" in tables
    assert "awareness_state" in tables
    # The owner's spoken switch, added later still, and additive for the same
    # reason: an existing database picks it up on restart with no migration.
    assert "awareness_control" in tables


def test_the_room_window_survives_an_restart(pre_nexus_db):
    db.group_append(-100, 7, "member", "Milad", "سلام")
    db.group_append(-100, 8, "admin", "Sara", "بنش کن")
    db.init()
    db.init()

    rows = db.group_window(-100, limit=10)
    assert [r["text"] for r in rows] == ["سلام", "بنش کن"]
    assert rows[1]["role"] == "admin"


def test_the_awareness_state_round_trips(pre_nexus_db):
    assert db.awareness_get(-100) == {} or db.awareness_get(-100) is None
    db.awareness_set(
        -100, seen_message_id=42, relevant=True, topic="t", summary="s", participants="p"
    )
    row = db.awareness_get(-100)
    assert row["seen_message_id"] == 42
    assert row["relevant"] is True
    assert row["topic"] == "t"

    # One row per chat, updated rather than appended.
    db.awareness_set(
        -100, seen_message_id=43, relevant=False, topic="t2", summary="s2", participants=""
    )
    assert db.awareness_get(-100)["seen_message_id"] == 43
