"""The room allowlist: the database is the source of truth, and it fails closed.

``GROUP_IDS`` is a bootstrap, not the rule. On the first boot against an empty
table it seeds the allowlist so an existing deployment keeps its rooms; after
that the table is authoritative, which is what makes revocation possible — an
environment variable cannot be edited by an administrator at runtime.

Two properties are asserted over and over here:

* **The seed runs once.** The guard is "the table has ever held a row", not
  "the table is currently empty", so a soft-revoked room can never be
  resurrected by a restart.
* **A read that cannot be answered is "no rooms", never "all rooms".** The
  boundary is fail-closed, and the module turns any storage failure into an
  empty allowlist rather than a permissive one.

The key is the **canonical numeric** chat id, so a string form and an int form
of the same room are the same tenant.
"""
import pytest

from app import config, db, groups

CHAT = -1001234567890
OTHER = -1009999999999


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    db.init()
    db.authorized_groups_reset()
    groups.reset_state()
    yield
    db.authorized_groups_reset()
    groups.reset_state()


# ── The seed ──────────────────────────────────────────────────────────────
def test_the_first_load_seeds_from_group_ids():
    ids = groups.load()
    assert ids == frozenset({CHAT})
    assert groups.is_authorized(CHAT) is True


def test_the_seed_runs_only_once():
    groups.load()
    groups.register(OTHER, actor_id=1)
    # A later load must not re-seed; the table already held a row.
    assert groups.load() == frozenset({CHAT, OTHER})


def test_a_soft_revoked_room_is_not_resurrected_by_a_reseed():
    groups.load()
    groups.revoke(CHAT, actor_id=1)
    groups.reset_state()  # the process restarts
    assert groups.is_authorized(CHAT) is False
    assert db.authorized_group_any() is True


def test_a_seed_failure_does_not_seed_blindly(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "authorized_group_any", boom)
    assert groups.seed_if_empty() == 0
    assert groups.is_authorized(CHAT) is False


# ── Register, revoke, re-register ─────────────────────────────────────────
def test_register_is_idempotent_and_keeps_the_original_added_at():
    groups.register(CHAT, actor_id=7)
    first = db.authorized_group_get(CHAT)
    groups.register(CHAT, actor_id=7)
    second = db.authorized_group_get(CHAT)
    assert second["enabled"] is True
    assert second["added_at"] == first["added_at"]


def test_revoke_is_soft_and_records_who_and_when():
    groups.register(CHAT, actor_id=7)
    assert groups.revoke(CHAT, actor_id=9) is True
    row = db.authorized_group_get(CHAT)
    assert row is not None, "a revoke must not delete the tenant row"
    assert row["enabled"] is False
    assert row["revoked_by"] == 9
    assert row["revoked_at"] > 0


def test_revoking_twice_changes_nothing_the_second_time():
    groups.register(CHAT, actor_id=7)
    assert groups.revoke(CHAT, actor_id=9) is True
    assert groups.revoke(CHAT, actor_id=9) is False


def test_re_registering_a_revoked_room_clears_the_revocation():
    groups.register(CHAT, actor_id=7)
    groups.revoke(CHAT, actor_id=9)
    groups.register(CHAT, actor_id=7)
    row = db.authorized_group_get(CHAT)
    assert row["enabled"] is True
    assert row["revoked_by"] == 0
    assert row["revoked_at"] == 0
    assert groups.is_authorized(CHAT) is True


def test_register_clears_the_cache_so_the_next_read_sees_it():
    groups.load()
    assert groups.is_authorized(OTHER) is False
    groups.register(OTHER, actor_id=7)
    assert groups.is_authorized(OTHER) is True


def test_revoke_clears_the_cache_so_the_next_read_sees_it():
    groups.load()
    assert groups.is_authorized(CHAT) is True
    groups.revoke(CHAT, actor_id=7)
    assert groups.is_authorized(CHAT) is False


# ── The canonical key ─────────────────────────────────────────────────────
def test_the_key_is_the_canonical_numeric_chat_id():
    groups.register(CHAT, actor_id=7)
    assert db.authorized_group_get(int(CHAT))["chat_id"] == CHAT
    # A string form is the same room, because the key is coerced on the way in.
    assert groups.is_authorized(str(CHAT)) is True


def test_a_different_room_is_a_different_tenant():
    groups.register(CHAT, actor_id=7)
    assert groups.is_authorized(OTHER) is False
    assert db.authorized_group_get(OTHER) is None


# ── Reads ─────────────────────────────────────────────────────────────────
def test_ids_returns_only_enabled_rooms():
    groups.register(CHAT, actor_id=7)
    groups.register(OTHER, actor_id=7)
    groups.revoke(OTHER, actor_id=7)
    assert db.authorized_group_ids() == [CHAT]


def test_list_rows_can_be_restricted_to_enabled_rooms():
    groups.register(CHAT, actor_id=7)
    groups.register(OTHER, actor_id=7)
    groups.revoke(OTHER, actor_id=7)
    assert {r["chat_id"] for r in groups.list_rows()} == {CHAT, OTHER}
    assert {r["chat_id"] for r in groups.list_rows(enabled_only=True)} == {CHAT}


def test_count_is_the_number_of_enabled_rooms():
    groups.load()
    assert groups.count() == 1
    groups.register(OTHER, actor_id=7)
    assert groups.count() == 2


# ── Fail-closed ───────────────────────────────────────────────────────────
def test_a_failed_read_is_no_rooms_not_all_rooms(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "authorized_group_ids", boom)
    groups.reset_state()
    assert groups.load() == frozenset()
    assert groups.is_authorized(CHAT) is False


def test_an_unknown_chat_id_is_not_authorized():
    groups.load()
    assert groups.is_authorized(0) is False
    assert groups.is_authorized(-1) is False


def test_a_non_numeric_chat_id_is_not_authorized():
    groups.load()
    assert groups.is_authorized("not-a-number") is False
    assert groups.is_authorized(None) is False
