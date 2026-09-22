"""One Telegram update, handled once.

Telegram re-delivers an update whenever it is not certain the bot received it —
after a network failure, and after a restart, because the update offset is not
persisted and the bot asks for the backlog again. Handled twice, a message is
answered twice, a moderation action runs twice, and a model call is paid for
twice.

Two properties have to hold at the same time, and the second is the one that is
easy to lose:

* the *same* update must not be handled twice;
* a *different* update must never be dropped. A guard that is too eager turns a
  duplicate into a silence, which is the worse failure and the one the owner
  named explicitly.
"""
import asyncio
import inspect
import threading
import time

import pytest
from telegram.ext import ApplicationHandlerStop

from app import config, db, main


# ── The claim ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def dedup_env(monkeypatch):
    monkeypatch.setattr(config, "UPDATE_DEDUP_ENABLED", True)
    monkeypatch.setattr(config, "UPDATE_DEDUP_TTL_SECONDS", 3600)
    db.init()
    db.seen_updates_reset()
    yield
    db.seen_updates_reset()


def test_the_first_delivery_is_claimed():
    assert db.update_claim(1001) is True
    assert db.update_seen(1001) is True


def test_a_second_delivery_is_not_claimed():
    assert db.update_claim(1002) is True
    assert db.update_claim(1002) is False
    assert db.update_claim(1002) is False


def test_different_updates_are_all_claimed():
    """The guard must not be a rate limit in disguise."""
    for update_id in (2001, 2002, 2003, 2004):
        assert db.update_claim(update_id) is True


def test_a_missing_or_zero_id_is_refused_rather_than_claimed():
    """Zero is not an update Telegram issues, and storing it would collide.

    Treating a missing id as claimable would put every such update on one row:
    the first would be handled and every later one dropped as a duplicate.
    """
    assert db.update_claim(0) is False
    assert db.update_claim(-1) is False
    assert db.update_claim(None) is False
    assert db.update_seen(0) is False


def test_concurrent_delivery_of_one_update_claims_it_exactly_once():
    """The race the atomic statement exists for.

    Two deliveries of the same update can arrive close enough together that a
    read-then-write would let both see "not seen yet" and both proceed. Eight
    threads, one winner.
    """
    results: list[bool] = []
    lock = threading.Lock()

    def deliver():
        claimed = db.update_claim(3001)
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 7


# ── The guard handler ─────────────────────────────────────────────────────
def update(update_id=4001):
    from types import SimpleNamespace

    return SimpleNamespace(update_id=update_id)


def ctx():
    from types import SimpleNamespace

    return SimpleNamespace(bot=SimpleNamespace())


def test_the_first_delivery_passes_the_guard():
    asyncio.run(main.on_any_update(update(4001), ctx()))


def test_a_duplicate_raises_and_stops_the_dispatcher():
    asyncio.run(main.on_any_update(update(4002), ctx()))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(main.on_any_update(update(4002), ctx()))


def test_an_update_without_an_id_is_let_through():
    """Refusing to guess is the safe direction: no id, no claim, handle it."""
    asyncio.run(main.on_any_update(update(0), ctx()))
    asyncio.run(main.on_any_update(update(0), ctx()))


def test_the_guard_is_transparent_when_switched_off(monkeypatch):
    monkeypatch.setattr(config, "UPDATE_DEDUP_ENABLED", False)
    asyncio.run(main.on_any_update(update(4003), ctx()))
    asyncio.run(main.on_any_update(update(4003), ctx()))
    assert db.update_seen(4003) is False


def test_a_database_failure_does_not_stop_the_bot(monkeypatch):
    """A dedup that cannot be written must not become a dropped message."""

    def boom(update_id):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(main.db, "update_claim", boom)
    asyncio.run(main.on_any_update(update(4004), ctx()))


def test_the_guard_is_registered_before_every_other_handler():
    """Group -1 is the whole mechanism; a later group would be too late.

    Asserted against the source rather than by building an ``Application``,
    which would need a live token and a network. The string is the registration
    line itself, so a change that moves it into group 0 fails here.
    """
    source = inspect.getsource(main.main)
    assert "TypeHandler(Update, on_any_update), group=-1" in source


# ── Retention ─────────────────────────────────────────────────────────────
def test_prune_drops_only_expired_ids():
    now = int(time.time())
    with db._lock:  # noqa: SLF001 - the test writes an aged row on purpose
        db._conn.execute(
            "INSERT OR REPLACE INTO seen_updates (update_id, at) VALUES (?,?)",
            (5001, now - 10_000),
        )
        db._conn.execute(
            "INSERT OR REPLACE INTO seen_updates (update_id, at) VALUES (?,?)",
            (5002, now),
        )
        db._conn.commit()

    dropped = db.seen_updates_prune(3600)

    assert dropped == 1
    assert db.update_seen(5001) is False
    assert db.update_seen(5002) is True


def test_the_reaper_does_nothing_when_the_guard_is_off(monkeypatch):
    monkeypatch.setattr(config, "UPDATE_DEDUP_ENABLED", False)
    db.update_claim(6001)
    asyncio.run(main.update_dedup_reaper(ctx()))
    assert db.update_seen(6001) is True
