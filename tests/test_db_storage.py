"""The storage engine's settings: what they are, and why they are set on every start.

``db.init`` opens one connection for the whole process and every write is its own
transaction, so the per-message cost of a group message is a small multiple of the
per-commit cost. On this host the rollback journal SQLite defaults to made that
multiple expensive: a trace callback counting ``COMMIT`` over one message's eight
writes measured 63.0 ms under ``delete``/``FULL``, 18.6 ms under ``wal``/``FULL``
and 1.7 ms under ``wal``/``NORMAL``.

These tests are about the configuration being *actually applied*, because the
failure they guard against is silent: a pragma that is never issued reads exactly
like a pragma that is, and the only symptom is latency. The two settings differ in
where they live, and that difference is the reason both are set in ``init`` rather
than once at build time:

* ``journal_mode`` is a property of the **file**, so it persists across restarts.
* ``synchronous`` is a property of the **connection**, so it does not — a fresh
  connection to the same file comes back at ``FULL``.

Both are asserted below, because a future edit that moves either one out of
``init`` would otherwise look harmless.
"""
import sqlite3

import pytest

from app import config, db


@pytest.fixture(autouse=True)
def storage_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "storage.db"))
    db.init()
    yield
    db.init()


def pragma(name: str):
    return db._conn.execute(f"PRAGMA {name}").fetchone()[0]


# ── The settings are applied ──────────────────────────────────────────────
def test_the_database_runs_in_wal_mode():
    assert pragma("journal_mode").lower() == "wal", (
        "the file must be in WAL, not the rollback journal the hot path was "
        "measured against"
    )


def test_the_connection_runs_at_normal_synchronous_inside_wal():
    # 1 is NORMAL, 2 is FULL. The whole point of the change is this word.
    assert pragma("synchronous") == 1


def test_a_contention_wait_is_still_configured():
    # Python's ``connect`` timeout already sets this, but it is the other half of
    # "the writer waits instead of failing", so it is pinned rather than assumed.
    assert pragma("busy_timeout") == 5000


# ── Why both pragmas are in init(), and not just one ──────────────────────
def test_the_journal_mode_belongs_to_the_file_and_outlives_a_reconnect(tmp_path):
    db.people_remember(1, 2, first_name="A")
    db._conn.close()

    # A plain connection that has never heard of init() still finds WAL, because
    # the mode is written into the file itself.
    raw = sqlite3.connect(config.DB_PATH)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert raw.execute("SELECT first_name FROM people").fetchone()[0] == "A"
    finally:
        raw.close()


def test_synchronous_belongs_to_the_connection_so_init_must_set_it_every_time():
    db._conn.close()
    raw = sqlite3.connect(config.DB_PATH)
    try:
        # A brand new connection to a WAL database is back at FULL: the file did
        # not carry the setting, which is exactly why init() cannot skip it.
        assert raw.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        raw.close()

    db.init()
    assert pragma("synchronous") == 1, "init() is the only thing that puts it back"


# ── The degradation path ──────────────────────────────────────────────────
def test_a_database_that_cannot_do_wal_still_starts(monkeypatch):
    """An in-memory database keeps its own mode instead of raising.

    The suite's default ``DB_PATH`` is ``:memory:`` and a network filesystem
    answers the same way, so asking for WAL has to be a request rather than an
    assertion. A version of this that raised would take the whole test suite —
    and any deployment whose volume cannot do WAL — down at startup.
    """
    monkeypatch.setattr(config, "DB_PATH", ":memory:")
    db.init()

    assert pragma("journal_mode").lower() == "memory"
    db.people_remember(1, 2, first_name="B")
    assert db._conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
