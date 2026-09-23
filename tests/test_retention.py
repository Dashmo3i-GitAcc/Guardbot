"""Retention: the rules that existed and never ran, and the one table that needed one.

The brief asks for growth to be controlled without destroying accountability.
Auditing the codebase for that turned up something worse than a missing rule: two
rules that were already written, already documented as running, and had **no
caller at all**.

* ``db.daily_prune`` said it was "called on the pool path". It was not.
* ``admin_tools.prune`` said it was "called from the administrative path". It was
  not.

So the audit trail and the per-day spend table were both unbounded in practice,
and the only thing standing between them and the operator noticing was a docstring
that described a call site which did not exist. That is the failure mode these
tests exist to prevent, so most of them are about *wiring* rather than about the
DELETEs themselves: a retention rule that is not called is indistinguishable from
no retention rule, except that it reads as though the problem were handled.

Two things are deliberately not done, and both are asserted rather than described:

* ``admin_audit`` is never truncated, only *windowed*. Accountability survives a
  retention rule; it does not survive a rule that empties the table.
* The reporting tables (``ai_usage``, ``chat_usage``, ``moderation_usage``,
  ``transcript_usage``) get no window at all. They are one row per day each, so a
  year of them is 1,460 rows and a few tens of kilobytes — there is nothing to
  save, and a window would destroy the only month-over-month history the owner
  has. "Control growth" is not a licence to delete data that is not growing.
"""
import time

import pytest

from app import admin_service, admin_tools, config, db, gemini_pool, vpn_service

NOW = int(time.time())
DAY = 86400


@pytest.fixture(autouse=True)
def retention_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "retention.db"))
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    db.init()
    admin_service.prune_reset()
    gemini_pool.prune_reset()
    vpn_service.prune_reset()
    yield
    admin_service.prune_reset()
    gemini_pool.prune_reset()
    vpn_service.prune_reset()


def event(*, at: int, workload: str = "chat", kind: str = "rate_limited") -> None:
    db.pool_event_add(workload, kind, slot="1", model="m", reason="r", detail="", at=at)


def event_count() -> int:
    return len(db.pool_events(limit=1000))


# ── The pool's own tables ─────────────────────────────────────────────────
def test_the_events_sweep_drops_only_rows_past_the_window():
    event(at=NOW - 10 * DAY)
    event(at=NOW - 1 * DAY)
    event(at=NOW)

    dropped = db.events_prune(5 * DAY)

    assert dropped == 1, "exactly the row past the window"
    assert event_count() == 2, "and nothing else"


def test_a_zero_window_disables_the_events_sweep():
    """Zero is "keep everything", not "delete everything".

    The distinction matters: an operator who sets the window to zero to turn the
    sweep off must not lose the table, and ``DELETE ... WHERE at < now - 0``
    would do exactly that to every row.
    """
    event(at=NOW - 400 * DAY)
    event(at=NOW)

    assert db.events_prune(0) == 0
    assert event_count() == 2


def test_the_daily_sweep_drops_only_days_past_the_window():
    db.daily_add("chat", "1", "2020-01-01")
    db.daily_add("chat", "1", db.ai_day())

    dropped = db.daily_prune(90)

    assert dropped == 1
    assert db.daily_for("chat", db.ai_day()) == {"1": 1}


def test_the_pool_sweep_applies_both_windows():
    event(at=NOW - 400 * DAY)
    db.daily_add("chat", "1", "2020-01-01")

    gemini_pool.prune()

    assert event_count() == 0
    assert db.daily_for("chat", "2020-01-01") == {}


def test_the_pool_sweep_runs_on_the_request_path_not_on_every_request(monkeypatch):
    """The rule has to be called, and calling it per request would be its own bug.

    Two DELETEs per provider call, on the path a person is waiting on, to remove
    rows that at most a few hundred requests could have added. The counter is the
    same shape ``people`` uses.
    """
    calls: list[int] = []
    monkeypatch.setattr(gemini_pool, "prune", lambda: calls.append(1))
    monkeypatch.setattr(gemini_pool, "PRUNE_EVERY", 3)

    for _ in range(5):
        gemini_pool._maybe_prune()

    assert calls == [1], "five requests, one sweep"
    # And the counter resets, so it is every third rather than once ever.
    gemini_pool._maybe_prune()
    assert calls == [1, 1]


def test_the_pool_sweep_never_raises(monkeypatch):
    """A retention rule must not be the reason a reply fails."""

    def _boom(*args, **kwargs):
        raise RuntimeError("the disk is gone")

    monkeypatch.setattr(db, "events_prune", _boom)
    monkeypatch.setattr(db, "daily_prune", _boom)

    gemini_pool.prune()  # must not raise


def test_the_pool_sweep_is_wired_into_the_request_path():
    """The counter is useless if nothing calls it — which was the original bug.

    ``daily_prune`` was documented as "called on the pool path" and had no
    caller at all. Asserted against the source of the entry point, because that
    is the thing that was untrue last time.
    """
    import inspect

    source = inspect.getsource(gemini_pool.generate)

    assert "_maybe_prune()" in source, "the request path must apply the window"


# ── The administrative tables ─────────────────────────────────────────────
def test_the_admin_sweep_is_reached_by_every_recorded_request(monkeypatch):
    """``_record`` is the hook, because it runs for refusals too.

    A trail that only bounded itself on success would grow fastest on the
    requests that were denied, which are the ones a burst of probing produces.
    """
    calls: list[int] = []
    monkeypatch.setattr(admin_service, "prune", lambda: calls.append(1))
    monkeypatch.setattr(admin_service, "PRUNE_EVERY", 1)

    request = admin_service.AdminRequest(
        operation="mute_member", actor_id=1, chat_id=-100, target_id=2
    )
    admin_service._record(request, admin_service._result(request, "denied"))

    assert calls == [1], "recording a request is what applies the window"


def test_the_admin_sweep_applies_all_three_windows():
    db.audit_write(1, "mute_member", outcome="ok", chat_id=-100)
    db.admin_request_put(
        "old", actor_id=1, chat_id=-100, operation="mute_member", target_id=2,
        outcome="ok", at=NOW - 400 * DAY,
    )
    # A recorded action whose own window closed long ago: nothing can claim it,
    # because ``admin_pending_claim`` requires ``expires_at > now``.
    db.admin_pending_add(
        "lapsed", actor_id=1, chat_id=-100, operation="nexus_offline",
        subject="nexus_offline", payload="{}", expires_at=NOW - 400 * DAY,
    )

    admin_service.prune()

    assert db.audit_recent(limit=10), "a fresh audit row is kept"
    assert db.admin_request_get("old") is None, "an expired request id is dropped"
    assert db.admin_pending_get("lapsed") is None, "a lapsed proposal is dropped"


def test_the_audit_trail_is_windowed_and_never_emptied():
    """Accountability survives the retention rule. Asserted, not asserted-at.

    A test that only checked "old rows are gone" would pass for an implementation
    that deleted the whole table.
    """
    db.audit_write(1, "mute_member", outcome="ok", chat_id=-100)
    # Age the row rather than the clock: this is about the rule, not about time.
    db._exec(
        "UPDATE admin_audit SET at = ? WHERE action = ?",
        (NOW - 400 * DAY, "mute_member"),
    )
    db.audit_write(1, "ban_member", outcome="ok", chat_id=-100)

    admin_service.prune()

    remaining = db.audit_recent(limit=10)
    assert len(remaining) == 1, "the window removed one row, not the table"
    assert remaining[0]["action"] == "ban_member"


def test_the_admin_sweep_never_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("the disk is gone")

    monkeypatch.setattr(db, "audit_prune", _boom)
    monkeypatch.setattr(db, "admin_request_prune", _boom)
    monkeypatch.setattr(db, "admin_pending_prune", _boom)

    admin_service.prune()  # must not raise


def test_admin_tools_prune_delegates_to_the_service(monkeypatch):
    """One owner for one window. A second copy is a second answer."""
    calls: list[int] = []
    monkeypatch.setattr(admin_service, "prune", lambda: calls.append(1))

    admin_tools.prune()

    assert calls == [1]


# ── What is deliberately not bounded ──────────────────────────────────────
def test_the_reporting_tables_get_no_window():
    """One row per day is not growth, and the history is the point.

    Asserted as an absence: no prune function is applied to these four tables,
    so a later "add a TTL everywhere" pass has to delete this test deliberately
    rather than inherit the decision.
    """
    for name in ("ai_usage", "chat_usage", "moderation_usage", "transcript_usage"):
        assert hasattr(db, f"{name}_prune") is False, (
            f"{name} is one row per day and is reporting history, not growth"
        )


def test_the_events_sweep_has_an_index_to_use():
    """A TTL delete needs an index on the column it ranges over.

    The existing dedup index ends in ``at``, so it cannot serve
    ``WHERE at < ?`` — without this index the sweep would read the whole table
    every two hundred requests, which is a worse problem than the growth.
    """
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gemini_events'"
    ).fetchall()
    names = {r[0] for r in rows}

    assert "idx_gemini_events_at" in names


def test_the_chat_sweep_has_an_index_to_use():
    """Same rule, and this one is on the *hot* path rather than a sweep.

    ``chat_purge`` is called after every successful reply (``chat.py``), so
    without an index on ``at`` an ordinary conversation paid a full scan of the
    table on each turn. The turn index cannot serve it — it is keyed on
    ``chat_id`` first and the purge has no chat to start from — which is why
    this is a second index rather than a change to the existing one.
    """
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chat_messages'"
    ).fetchall()
    names = {r[0] for r in rows}

    assert "idx_chat_messages_at" in names


def test_the_chat_purge_uses_that_index():
    """The index exists *and* the planner reaches for it.

    Asserted through ``EXPLAIN QUERY PLAN`` rather than by reading the schema,
    because an index the query cannot use is the same as no index — which is
    exactly what the turn index was here.
    """
    plan = db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM chat_messages WHERE at < ?", (0,)
    ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)

    assert "idx_chat_messages_at" in detail, detail


# ── The VPN's recorded operations ─────────────────────────────────────────
def pending(
    request_id: str,
    *,
    status: str = "pending",
    expires_at: int = 0,
    confirmed_at: int = 0,
) -> None:
    """Record one operation, then put it in the state the test is about."""
    db.vpn_pending_add(
        request_id,
        actor_id=1,
        chat_id=-100,
        operation="vpn_balance",
        subject="u1",
        payload="{}",
        expires_at=expires_at or NOW + 900,
        now=NOW,
    )
    if status != "pending" or confirmed_at:
        db._exec(
            "UPDATE vpn_pending_ops SET status=?, confirmed_at=? WHERE request_id=?",
            (status, confirmed_at, request_id),
        )


def test_the_vpn_sweep_drops_a_finished_operation_past_the_window():
    pending("done-old", status="done", confirmed_at=NOW - 10 * DAY)
    pending("done-new", status="done", confirmed_at=NOW)

    dropped = db.vpn_pending_prune(DAY)

    assert dropped == 1, "exactly the receipt past the window"
    assert db.vpn_pending_get("done-old") is None
    assert db.vpn_pending_get("done-new") is not None


def test_the_vpn_sweep_drops_an_operation_that_expired_long_ago():
    pending("never-taken", expires_at=NOW - 10 * DAY)

    dropped = db.vpn_pending_prune(DAY)

    assert dropped == 1
    assert db.vpn_pending_get("never-taken") is None


def test_the_vpn_sweep_never_drops_an_operation_that_can_still_be_confirmed():
    """The safety property, and the reason the window is measured from expiry.

    ``vpn_pending_claim`` requires ``expires_at > now``, so a row inside its
    window is one somebody may still approve. A window measured from
    ``created_at`` instead would delete an operation the owner was in the middle
    of approving — a money operation that then silently does not happen, which is
    the one outcome this table exists to prevent.
    """
    pending("confirmable", expires_at=NOW + 900)
    db._exec(
        "UPDATE vpn_pending_ops SET created_at=? WHERE request_id=?",
        (NOW - 400 * DAY, "confirmable"),
    )

    dropped = db.vpn_pending_prune(DAY)

    assert dropped == 0
    row = db.vpn_pending_get("confirmable")
    assert row is not None and row["status"] == "pending"


def test_a_confirmed_operation_is_kept_while_it_is_still_in_flight():
    """Mid-execution is not garbage: ``vpn_pending_finish`` still has to find it."""
    pending("in-flight", status="confirmed", confirmed_at=NOW)

    assert db.vpn_pending_prune(DAY) == 0
    assert db.vpn_pending_get("in-flight") is not None


def test_the_vpn_sweep_never_raises(monkeypatch):
    """A retention rule must not be the reason an operation fails."""

    def _boom(*args, **kwargs):
        raise RuntimeError("the disk is gone")

    monkeypatch.setattr(db, "vpn_pending_prune", _boom)

    vpn_service.prune()  # must not raise


def test_the_vpn_sweep_runs_on_the_operation_path_not_on_every_operation(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(vpn_service, "prune", lambda: calls.append(1))
    monkeypatch.setattr(vpn_service, "PRUNE_EVERY", 3)

    for _ in range(5):
        vpn_service._maybe_prune()

    assert calls == [1], "five operations, one sweep"
    vpn_service._maybe_prune()
    assert calls == [1, 1], "and the counter resets rather than firing once ever"


def test_the_vpn_sweep_is_wired_into_the_operation_path():
    """The counter is useless if nothing calls it — which was the original bug.

    ``daily_prune`` was documented as "called on the pool path" and had no caller
    at all. Asserted against the source of the entry point, because that is the
    thing that was untrue last time.
    """
    import inspect

    source = inspect.getsource(vpn_service._record_pending)

    assert "_maybe_prune()" in source, "the operation path must apply the window"
