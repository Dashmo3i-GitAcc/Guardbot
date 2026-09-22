"""The audit trail: what it records, and what it must never record.

The brief lists what an audit row has to carry — actor, role, action, target,
chat, result, timestamp, request id, failure reason — and then lists what must
never reach it: tokens, API keys, passwords, private content. Both halves are
tested here, and the second half is the one that is easy to lose, because a
secret in a log is invisible until somebody reads the log.

Two of the brief's fields were missing when this file was written, and both for
the same reason — they are the fields nothing was asking for:

* **role.** ``admin_audit`` recorded *who* acted and never *with what
  authority*. That is exactly the question an audit trail exists to answer, and
  it stops being answerable the moment a role changes: an administrator who is
  later demoted leaves a trail that says they acted and not whether they were
  entitled to. The role is resolved from ``rbac`` at write time and never taken
  from the request, because the request is the thing being audited — a request
  that named its own authority would be writing its own alibi.
* **request id.** The outcome a person saw and the row that recorded it were
  linked only by matching actor, chat, operation and target by hand.

The hygiene half is asserted two ways, because either alone is weak. The
structural assertions pin the *shape* (``detail`` is truncated, no column is
wide enough to hold a message body); the sentinel test pins the *behaviour* (a
credential placed in the configuration does not appear anywhere it could be
read back from). A shape test can pass while a leak happens through a different
door, and a sentinel test can pass while a different secret leaks — so both.
"""
import asyncio
import logging
import time

import pytest

from app import admin_service, config, db, gemini_pool, main, rbac

OWNER = 999
HELPER = 888
MEMBER = 42
CHAT = -1001234567890
BOT_ID = 1

# A string that is not a credential and looks like one, so the redactor and the
# masking code treat it the way they treat a real key. It ends in four
# distinctive characters, because the pool describes an account by its masked
# tail — which is how the tests below prove they are looking at a pool that
# really was built from this value, rather than at an empty one.
SENTINEL = "AIzaSySENTINEL00000000000000000000wXyZ"
SENTINEL_TAIL = "****wXyZ"


@pytest.fixture(autouse=True)
def audit_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_PYTHON_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "ADMIN_REQUEST_REPLAY_WINDOW", 120)
    db.init()
    db.admin_reset()
    admin_service.prune_reset()
    yield
    db.admin_reset()


class FakeGateway:
    """The smallest Gateway that can carry out a mute, and nothing else.

    A fake that implements more than the test needs is a fake that can hide a
    call the test was supposed to notice.
    """

    def __init__(self):
        self.calls: list[tuple] = []

    async def bot_right(self, chat_id, right):
        return True

    async def mute(self, chat_id, user_id):
        self.calls.append(("mute", chat_id, user_id))

    async def member(self, chat_id, user_id):
        return {
            "user_id": int(user_id),
            "telegram_status": "member",
            "is_telegram_admin": False,
        }


def request(operation="mute_member", **kwargs):
    fields = {
        "chat_id": CHAT,
        "actor_id": OWNER,
        "target_id": MEMBER,
        "request_id": admin_service.new_request_id(),
        "interface": admin_service.INTERFACE_AI,
        "at": int(time.time()),
    }
    fields.update(kwargs)
    return admin_service.AdminRequest(operation=operation, **fields)


def execute(req):
    return asyncio.run(
        admin_service.execute(req, FakeGateway(), bot_id=BOT_ID)
    )


def last_audit() -> dict:
    rows = db.audit_recent(limit=1)
    assert rows, "the action was not audited at all"
    return rows[0]


# ══ COMPLETENESS: every field the brief lists ═════════════════════════════
def test_the_audit_row_carries_every_field_the_brief_lists():
    """One assertion per field, so a missing one names itself."""
    req = request()
    execute(req)

    row = last_audit()

    assert row["actor_id"] == OWNER, "actor"
    assert row["role"] == rbac.ROLE_OWNER, "role"
    assert row["action"] == "moderation.mute", "action"
    assert row["target_id"] == MEMBER, "target"
    assert row["chat_id"] == CHAT, "chat"
    assert row["outcome"] == admin_service.OUTCOME_OK, "result"
    assert row["at"] and abs(row["at"] - time.time()) < 60, "timestamp"
    assert row["request_id"] == req.request_id, "request id"
    assert row["interface"] == admin_service.INTERFACE_AI, "front door"


def test_a_refusal_is_recorded_with_its_reason():
    """The trail has to answer "who tried", not only "who succeeded"."""
    req = request(actor_id=MEMBER)
    result = execute(req)

    assert not result.ok
    row = last_audit()
    assert row["actor_id"] == MEMBER
    assert row["outcome"] == admin_service.OUTCOME_DENIED
    assert row["detail"] == rbac.REASON_NOT_ADMIN, "the failure reason, recorded"
    assert row["request_id"] == req.request_id


def test_the_request_id_links_the_audit_row_to_the_idempotency_record():
    """The join key the brief asks for, tested as a join rather than as a string."""
    req = request()
    execute(req)

    row = last_audit()
    stored = db.admin_request_get(req.request_id)

    assert stored is not None, "the idempotency record exists"
    assert stored["actor_id"] == row["actor_id"]
    assert stored["operation"] == req.operation
    assert stored["outcome"] == row["outcome"]


def test_the_role_is_resolved_by_the_application_not_asserted_by_the_request():
    """A request cannot write its own authority into the record.

    This is the same rule as ``AdminRequest`` having no ``is_owner`` field: the
    way to make a claim impossible is to leave nowhere to make it. Here the
    claim *is* possible — the field exists — so the test is that it is ignored.
    """
    req = request(actor_id=MEMBER, role=rbac.ROLE_OWNER, permissions=("admins.manage",))
    execute(req)

    row = last_audit()

    assert row["role"] == rbac.ROLE_GUEST, "the resolved role, not the claimed one"


def test_a_past_action_keeps_the_authority_it_was_taken_with():
    """History is not rewritten by a later role change.

    An audit row that recomputed the actor's role on read would say that every
    action a demoted administrator ever took was taken as a guest, which is both
    false and useless. The role is stamped at write time and never revisited.
    """
    db.admin_set(
        HELPER, rbac.ROLE_HELPER, rbac.ROLE_PERMISSIONS[rbac.ROLE_HELPER],
        granted_by=OWNER,
    )
    execute(request(actor_id=HELPER))
    assert last_audit()["role"] == rbac.ROLE_HELPER

    # The role is removed. The trail must not follow.
    db._exec("DELETE FROM admins WHERE user_id=?", (HELPER,))

    assert last_audit()["role"] == rbac.ROLE_HELPER, "stamped, not recomputed"
    assert rbac.resolve(HELPER).role == rbac.ROLE_GUEST, "and the role really did go"


def test_the_command_path_records_the_role_too():
    """Both front doors stamp the same fields, or the trail has two shapes."""
    main._audit(OWNER, "admin.list", admin_service.OUTCOME_OK, chat_id=CHAT)

    row = last_audit()
    assert row["role"] == rbac.ROLE_OWNER
    assert row["interface"] == admin_service.INTERFACE_PYTHON


# ══ HYGIENE: what must never be recorded ══════════════════════════════════
def test_no_credential_reaches_the_audit_trail():
    """A key in the configuration must not become a key in the record."""
    monkeypatch_ = pytest.MonkeyPatch()
    try:
        monkeypatch_.setattr(config, "GEMINI_CHAT_API_KEY", SENTINEL)
        monkeypatch_.setattr(config, "BOT_TOKEN", SENTINEL)
        monkeypatch_.setattr(config, "GEMINI_API_KEY", SENTINEL)

        execute(request())

        rows = db.audit_recent(limit=20)
        assert rows, "there is no row to inspect, so this test proves nothing"
        for row in rows:
            for field, value in row.items():
                assert SENTINEL not in str(value), f"credential in audit.{field}"
    finally:
        monkeypatch_.undo()


def test_no_credential_reaches_the_log(caplog):
    """Including the boot report, which is the one place a pool is described.

    ``GEMINI_POOLS`` is built from the environment at import time, so the spec
    has to be replaced rather than the individual setting — patching
    ``GEMINI_CHAT_API_KEY`` alone would leave every pool with no accounts and
    the assertion below with nothing to look at, which is a passing test that
    proves nothing.
    """
    monkeypatch_ = pytest.MonkeyPatch()
    try:
        monkeypatch_.setattr(config, "BOT_TOKEN", SENTINEL)
        monkeypatch_.setattr(
            config,
            "GEMINI_POOLS",
            [
                {
                    "workload": "chat",
                    "keys": [("1", SENTINEL)],
                    "models": ["gemini-flash-lite-latest"],
                    "capabilities": frozenset({"text"}),
                    "allow_experimental": False,
                    "retries": 0,
                    "backoff": 0.0,
                    "timeout": 10.0,
                }
            ],
        )
        with caplog.at_level(logging.DEBUG, logger="guardbot"):
            gemini_pool.build_pools()
            lines = list(gemini_pool.startup_lines())
            for line in lines:
                logging.getLogger("guardbot").info(line)
            execute(request())

        # Not vacuous: the pool really was built from this value, and the report
        # really does describe it — by its masked tail, which is the only form
        # that is allowed to appear anywhere.
        assert any(SENTINEL_TAIL in line for line in lines), (
            "the pool was not built from the sentinel, so this test proves nothing"
        )
        leaked = [r.getMessage() for r in caplog.records if SENTINEL in r.getMessage()]
        assert leaked == [], "a credential reached a log line"
    finally:
        monkeypatch_.undo()


def test_a_message_body_cannot_be_stuffed_into_the_audit_trail():
    """``detail`` is bounded, so no caller can use it as a text column.

    The bound is the structural half of the privacy rule: a caller that passed a
    message body would store 300 characters of it, not the whole thing, and the
    test that matters is that the *shape* cannot hold a conversation.
    """
    body = "این یک پیام خصوصی طولانی است " * 400

    db.audit_write(
        1, "mute_member", outcome="ok", chat_id=CHAT, detail=body,
    )

    row = last_audit()
    assert len(row["detail"]) <= 300
    assert row["detail"] != body


def test_the_audit_trail_has_no_column_wide_enough_for_a_conversation():
    """The other structural half: the schema itself has no text column.

    Asserted over the column list rather than over a sample, so adding a
    ``body TEXT`` column later is a deliberate act that fails this test rather
    than something a reviewer has to notice.
    """
    columns = {
        row[1]: (row[2] or "").upper()
        for row in db._conn.execute("PRAGMA table_info(admin_audit)")
    }

    assert set(columns) == {
        "id", "at", "actor_id", "action", "target_id", "chat_id", "outcome",
        "detail", "interface", "role", "request_id",
    }, "a new column is a new decision about what the trail records"
    # ``detail`` is the only free-text column, and it is capped on write.
    assert columns["action"].startswith("TEXT")
    assert columns["role"].startswith("TEXT")
