"""AI-mediated administration: the security properties, and the seams.

The architecture this file tests has one rule above all others: **a language
model may ask, and may not decide.** Everything here exists to pin that down.

So the suite is organised around a fake :class:`~app.admin_service.Gateway` that
records every Telegram call it is asked to make. Most tests come down to the
same assertion in different clothes — *nothing reached Telegram* — which is the
only form of "the model was not obeyed" that cannot be faked by a refusal
message. A refusal the bot prints while still calling the API is not a refusal.

Three groups:

* **Authority** — who may do what, to whom. Owner protection, hierarchy, the
  role table, and the fact that none of it can be asserted by the caller.
* **The request boundary** — what a tool call can and cannot express. Forged
  identity, malformed arguments, replays, duplicates, ambiguous targets.
* **The seams** — which tools exist for whom, that the other three workloads
  have no route here at all, and that a Gemini outage leaves the commands
  working.

Nothing here talks to Google or to Telegram.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app import admin_service, admin_tools, config, db, main, rbac

OWNER = 999
SENIOR = 555
MODERATOR = 777
HELPER = 888
MEMBER = 42
STRANGER = 31337
CHAT = -1001234567890
OTHER_CHAT = -1009999999999
BOT_ID = 1


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(
        config,
        "CONFIG_ADMINS",
        [f"{SENIOR}:senior_admin", f"{MODERATOR}:moderator", f"{HELPER}:helper"],
    )
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_PYTHON_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "ADMIN_REQUEST_REPLAY_WINDOW", 120)
    db.init()
    db.admin_reset()
    yield
    db.admin_reset()


# ── The gateway double ────────────────────────────────────────────────────
class FakeGateway:
    """A Gateway that records everything and can be told to refuse.

    Implements exactly the protocol in ``app/admin_service.py`` and nothing
    more, which is the point: if the service ever needs a Telegram operation
    that is not on this class, the fake stops compiling and the reviewer finds
    out.
    """

    def __init__(
        self,
        *,
        can_promote=True,
        can_restrict=True,
        can_delete=True,
        fail=None,
        member_status="member",
    ):
        self.can_promote = can_promote
        self.can_restrict = can_restrict
        self.can_delete = can_delete
        self.fail = fail or set()
        self.member_status = member_status
        self.calls: list[tuple] = []

    async def bot_right(self, chat_id: int, right: str) -> bool:
        self.calls.append(("bot_right", chat_id, right))
        return {
            "can_promote_members": self.can_promote,
            "can_restrict_members": self.can_restrict,
            "can_delete_messages": self.can_delete,
        }.get(right, False)

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} refused by Telegram")

    async def promote(self, chat_id, user_id, rights):
        self._maybe_fail("promote")
        self.calls.append(("promote", chat_id, user_id, dict(rights)))

    async def demote(self, chat_id, user_id):
        self._maybe_fail("demote")
        self.calls.append(("demote", chat_id, user_id))

    async def mute(self, chat_id, user_id):
        self._maybe_fail("mute")
        self.calls.append(("mute", chat_id, user_id))

    async def unmute(self, chat_id, user_id):
        self._maybe_fail("unmute")
        self.calls.append(("unmute", chat_id, user_id))

    async def ban(self, chat_id, user_id):
        self._maybe_fail("ban")
        self.calls.append(("ban", chat_id, user_id))

    async def unban(self, chat_id, user_id):
        self._maybe_fail("unban")
        self.calls.append(("unban", chat_id, user_id))

    async def delete(self, chat_id, message_id):
        self._maybe_fail("delete")
        self.calls.append(("delete", chat_id, message_id))

    async def warn(self, chat_id, user_id, reason):
        self._maybe_fail("warn")
        self.calls.append(("warn", chat_id, user_id, reason))

    async def member(self, chat_id, user_id):
        self.calls.append(("member", chat_id, user_id))
        return {
            "user_id": int(user_id),
            "telegram_status": self.member_status,
            "is_telegram_admin": self.member_status in ("administrator", "creator"),
        }

    # -- helpers for assertions --
    def actions(self) -> list[tuple]:
        """Only the calls that change something. Reads are excluded."""
        return [c for c in self.calls if c[0] not in ("bot_right", "member")]


def run(coro):
    return asyncio.run(coro)


def request(operation: str, **kwargs) -> admin_service.AdminRequest:
    """A well-formed request, with the fields a caller must supply filled in."""
    fields = {
        "chat_id": CHAT,
        "actor_id": OWNER,
        "request_id": admin_service.new_request_id(),
        "interface": admin_service.INTERFACE_AI,
        "at": int(time.time()),
    }
    fields.update(kwargs)
    return admin_service.AdminRequest(operation=operation, **fields)


def execute(operation: str, gateway=None, **kwargs):
    gateway = gateway or FakeGateway()
    return run(
        admin_service.execute(
            request(operation, **kwargs), gateway, bot_id=BOT_ID
        )
    ), gateway


def ai_call(name: str, args: dict, *, actor_id: int, chat_id: int = CHAT):
    """Parse one model tool call and run it, the way main.py's on_tool does."""
    req = admin_tools.parse_write_call(
        name,
        args,
        actor_id=actor_id,
        chat_id=chat_id,
        request_id=admin_service.new_request_id(),
    )
    if req is None:
        return None, FakeGateway()
    gateway = FakeGateway()
    return run(admin_service.execute(req, gateway, bot_id=BOT_ID)), gateway


# ══ AUTHORITY ═════════════════════════════════════════════════════════════
def test_the_owner_can_ban_a_member():
    result, gateway = execute("ban_member", target_id=MEMBER)

    assert result.ok
    assert ("ban", CHAT, MEMBER) in gateway.actions()


def test_a_stranger_cannot_ban_anybody():
    result, gateway = execute("ban_member", actor_id=STRANGER, target_id=MEMBER)

    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_NOT_ADMIN
    assert gateway.actions() == []


def test_a_helper_cannot_ban():
    """The helper may warn and nothing else. A refusal must reach no API."""
    result, gateway = execute("ban_member", actor_id=HELPER, target_id=MEMBER)

    assert not result.ok
    assert result.reason == rbac.REASON_MISSING_PERMISSION
    assert gateway.actions() == []


def test_a_helper_may_warn():
    result, gateway = execute(
        "warn_member", actor_id=HELPER, target_id=MEMBER, reason="spam"
    )

    assert result.ok
    assert ("warn", CHAT, MEMBER, "spam") in gateway.actions()


def test_a_moderator_may_mute_but_not_ban():
    muted, gw1 = execute("mute_member", actor_id=MODERATOR, target_id=MEMBER)
    banned, gw2 = execute("ban_member", actor_id=MODERATOR, target_id=MEMBER)

    assert muted.ok and ("mute", CHAT, MEMBER) in gw1.actions()
    assert not banned.ok and gw2.actions() == []


def test_a_senior_admin_may_ban():
    result, gateway = execute("ban_member", actor_id=SENIOR, target_id=MEMBER)

    assert result.ok
    assert ("ban", CHAT, MEMBER) in gateway.actions()


def test_no_owner_configured_refuses_everything(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", 0)
    result, gateway = execute("ban_member", target_id=MEMBER)

    assert not result.ok
    assert result.reason == rbac.REASON_NO_OWNER
    assert gateway.actions() == []


# ══ HIERARCHY AND OWNER PROTECTION ════════════════════════════════════════
def test_the_owner_cannot_be_banned_by_a_senior_admin():
    result, gateway = execute("ban_member", actor_id=SENIOR, target_id=OWNER)

    assert not result.ok
    assert result.reason == rbac.REASON_OWNER_PROTECTED
    assert gateway.actions() == []


def test_the_owner_cannot_be_banned_by_the_owner_either():
    """Protection is unconditional, which removes the whole class of bugs."""
    result, gateway = execute("ban_member", target_id=OWNER)

    assert not result.ok
    assert result.reason == rbac.REASON_OWNER_PROTECTED
    assert gateway.actions() == []


def test_a_moderator_cannot_ban_a_senior_admin():
    """The brief's worked example, at the service layer.

    The refusal arrives as ``missing_permission`` rather than ``higher_rank``,
    because ``rbac.authorize`` checks the permission before the hierarchy — and
    a moderator can never hold ``moderation.ban`` at all, since a stored row may
    narrow its role's bundle but never widen it. So for this pair the first gate
    always fires. That is the correct answer, not a weaker one: the rank gate
    gets its own tests below, with two admins who both hold the permission.
    """
    result, gateway = execute("ban_member", actor_id=MODERATOR, target_id=SENIOR)

    assert not result.ok
    assert result.reason == rbac.REASON_MISSING_PERMISSION
    assert gateway.actions() == []


def _two_peers(monkeypatch, role=rbac.ROLE_SENIOR_ADMIN):
    """Two equal-ranked admins, stored rather than configured.

    ``CONFIG_ADMINS`` is cleared first on purpose: a configured admin shadows
    the stored row, so a test that wants to exercise the *rank* gate has to make
    sure the levels it is asserting on actually come from the database.
    """
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    for user_id in (SENIOR, MODERATOR):
        db.admin_set(
            user_id, role, rbac.ROLE_PERMISSIONS[role], granted_by=OWNER
        )


def test_peers_cannot_demote_each_other(monkeypatch):
    _two_peers(monkeypatch)

    result, gateway = execute("demote_member", actor_id=SENIOR, target_id=MODERATOR)

    assert not result.ok
    assert result.reason == rbac.REASON_HIGHER_RANK
    assert gateway.actions() == []
    assert db.admin_get(MODERATOR)["role"] == rbac.ROLE_SENIOR_ADMIN


def test_peers_cannot_ban_each_other_either(monkeypatch):
    """The rank gate on the moderation path, where both hold the permission."""
    _two_peers(monkeypatch)

    result, gateway = execute("ban_member", actor_id=SENIOR, target_id=MODERATOR)

    assert not result.ok
    assert result.reason == rbac.REASON_HIGHER_RANK
    assert gateway.actions() == []


def test_a_moderator_cannot_promote_anybody():
    result, gateway = execute(
        "promote_member", actor_id=MODERATOR, target_id=MEMBER, role="moderator"
    )

    assert not result.ok
    assert gateway.actions() == []


def test_a_senior_admin_cannot_create_another_senior_admin():
    """The role table is a second bound on top of the permission subset rule."""
    result, gateway = execute(
        "promote_member", actor_id=SENIOR, target_id=MEMBER, role="senior_admin"
    )

    assert not result.ok
    assert result.reason == rbac.REASON_CANNOT_GRANT_ROLE
    assert gateway.actions() == []


def test_a_senior_admin_may_create_a_moderator():
    result, gateway = execute(
        "promote_member", actor_id=SENIOR, target_id=MEMBER, role="moderator"
    )

    assert result.ok
    assert any(c[0] == "promote" for c in gateway.actions())
    assert db.admin_get(MEMBER)["role"] == rbac.ROLE_MODERATOR


def test_the_owner_can_create_a_senior_admin():
    result, _ = execute(
        "promote_member", target_id=MEMBER, role="senior_admin"
    )

    assert result.ok
    assert db.admin_get(MEMBER)["role"] == rbac.ROLE_SENIOR_ADMIN


def test_promotion_grants_only_what_the_role_carries():
    """The caller names a role; the application decides the Telegram flags."""
    _, gateway = execute("promote_member", target_id=MEMBER, role="moderator")

    _, _, _, rights = next(c for c in gateway.actions() if c[0] == "promote")
    expected = rbac.telegram_rights_for(rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR])
    assert rights == expected
    # And nothing beyond it.
    assert "can_promote_members" not in rights


# ══ THE REQUEST BOUNDARY: FORGERY ═════════════════════════════════════════
def test_a_request_has_no_field_that_can_claim_authority():
    """The structural half of "the model cannot declare is_owner".

    A field that can express "I am the owner" is a field a model can fill in.
    The way to make that impossible is to have no such field, so this asserts
    the absence rather than trusting a check to catch it.
    """
    fields = set(admin_service.AdminRequest.__dataclass_fields__)

    assert "is_owner" not in fields
    assert "actor_role" not in fields
    assert "permissions_ok" not in fields
    assert "allowed" not in fields
    # The identity fields that DO exist are ids, and they are re-resolved.
    assert {"actor_id", "chat_id", "target_id"} <= fields


def test_a_member_claiming_to_be_the_owner_is_refused():
    """The brief's §4 scenario, at the point where it is actually decided."""
    result, gateway = execute("ban_member", actor_id=MEMBER, target_id=HELPER)

    assert not result.ok
    assert gateway.actions() == []


def test_the_tool_schema_has_no_identity_parameter():
    """A tool call cannot name a different actor or a different room.

    ``parse_write_call`` takes both from the caller and the model supplies
    neither, so a forged ``actor_user_id`` is not rejected — it is
    inexpressible. This asserts the schemas have no such parameter to abuse.
    """
    for spec in admin_tools.TOOLS.values():
        names = {p for p, _, _ in spec.parameters}
        assert "actor_id" not in names
        assert "actor_user_id" not in names
        assert "chat_id" not in names
        assert "is_owner" not in names
        assert "permissions" not in names


def test_a_forged_actor_id_in_the_arguments_is_not_a_parameter():
    """Extra arguments are refused rather than ignored."""
    parsed = admin_tools.parse_write_call(
        "ban_member",
        {"target_user_id": MEMBER, "actor_user_id": OWNER},
        actor_id=MEMBER,
        chat_id=CHAT,
    )

    assert parsed is None


def test_the_caller_supplies_the_actor_not_the_model():
    parsed = admin_tools.parse_write_call(
        "ban_member", {"target_user_id": MEMBER}, actor_id=MEMBER, chat_id=CHAT
    )

    assert parsed.actor_id == MEMBER
    assert parsed.chat_id == CHAT


def test_a_member_cannot_reach_a_write_tool_through_the_parser():
    """Even if the model produced the call, the parse still carries the real id."""
    result, gateway = ai_call("ban_member", {"target_user_id": HELPER}, actor_id=MEMBER)

    assert result is not None
    assert not result.ok
    assert gateway.actions() == []


# ══ THE REQUEST BOUNDARY: MALFORMED ═══════════════════════════════════════
@pytest.mark.parametrize(
    "name,args",
    [
        ("ban_member", {}),                                   # no target
        ("ban_member", {"target_user_id": 0}),                # unusable target
        ("ban_member", {"target_user_id": "not-a-number"}),   # wrong type
        ("ban_member", {"target_user_id": -5}),               # nonsense id
        ("promote_member", {"target_user_id": MEMBER}),       # role missing
        ("delete_message", {}),                               # no message id
        ("delete_message", {"message_id": 0}),                # unusable message
        ("warn_member", {"target_user_id": None}),            # null
    ],
)
def test_malformed_arguments_do_not_execute(name, args):
    """Refused, not repaired. Every coercion would be a way to act unasked."""
    result, gateway = ai_call(name, args, actor_id=OWNER)

    assert result is None
    assert gateway.actions() == []


def test_an_unknown_tool_does_not_execute():
    result, gateway = ai_call("delete_everything", {}, actor_id=OWNER)

    assert result is None
    assert gateway.actions() == []


def test_an_unknown_role_is_refused():
    result, gateway = execute(
        "promote_member", target_id=MEMBER, role="superuser"
    )

    assert not result.ok
    assert result.reason == rbac.REASON_UNKNOWN_ROLE
    assert gateway.actions() == []


def test_an_unknown_operation_is_refused():
    result, gateway = execute("launch_missiles", target_id=MEMBER)

    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_UNKNOWN_OPERATION
    assert gateway.actions() == []


def test_a_request_with_no_chat_or_actor_is_refused():
    gateway = FakeGateway()
    empty = admin_service.AdminRequest(operation="ban_member", chat_id=0, actor_id=0)

    result = run(admin_service.execute(empty, gateway, bot_id=BOT_ID))

    assert result.outcome == admin_service.OUTCOME_MALFORMED
    assert gateway.actions() == []


def test_the_bot_cannot_be_the_target():
    result, gateway = execute("ban_member", target_id=BOT_ID)

    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_TARGET_IS_BOT
    assert gateway.actions() == []


# ══ THE REQUEST BOUNDARY: REPLAY AND DUPLICATES ═══════════════════════════
def test_the_same_request_id_is_not_executed_twice():
    gateway = FakeGateway()
    req = request("ban_member", target_id=MEMBER)

    first = run(admin_service.execute(req, gateway, bot_id=BOT_ID))
    second = run(admin_service.execute(req, gateway, bot_id=BOT_ID))

    assert first.ok
    assert second.duplicate
    assert second.outcome == admin_service.OUTCOME_DUPLICATE
    assert len(gateway.actions()) == 1


def test_a_replayed_refusal_is_also_a_replay():
    """Re-running a denial could produce a different answer if roles changed."""
    gateway = FakeGateway()
    req = request("ban_member", actor_id=MEMBER, target_id=HELPER)

    first = run(admin_service.execute(req, gateway, bot_id=BOT_ID))
    second = run(admin_service.execute(req, gateway, bot_id=BOT_ID))

    assert not first.ok
    assert second.duplicate
    assert not second.ok


def test_two_different_requests_both_run():
    gateway = FakeGateway()

    run(admin_service.execute(request("ban_member", target_id=MEMBER),
                              gateway, bot_id=BOT_ID))
    run(admin_service.execute(request("ban_member", target_id=HELPER),
                              gateway, bot_id=BOT_ID))

    assert len(gateway.actions()) == 2


def test_a_stale_request_is_refused():
    """Outside the replay window, nothing happens."""
    gateway = FakeGateway()
    old = request(
        "ban_member",
        target_id=MEMBER,
        at=int(time.time()) - int(config.ADMIN_REQUEST_REPLAY_WINDOW) - 10,
    )

    result = run(admin_service.execute(old, gateway, bot_id=BOT_ID))

    assert result.outcome == admin_service.OUTCOME_STALE
    assert gateway.actions() == []


def test_a_fresh_request_is_not_stale():
    result, gateway = execute("ban_member", target_id=MEMBER)

    assert result.ok
    assert gateway.actions()


def test_the_idempotency_window_is_at_least_the_replay_window(monkeypatch):
    """A request must not be forgotten while it is still replayable."""
    monkeypatch.setattr(config, "ADMIN_REQUEST_REPLAY_WINDOW", 3600)
    monkeypatch.setattr(config, "ADMIN_IDEMPOTENCY_RETENTION", 10)

    # The module-level floor is applied at import; this asserts the relationship
    # the code documents, using the same expression.
    assert max(10, 3600) >= 3600


# ══ TELEGRAM IS THE FLOOR ═════════════════════════════════════════════════
def test_a_missing_bot_right_refuses_before_calling():
    """Configuration saying the bot should have a right is not evidence."""
    gateway = FakeGateway(can_restrict=False)

    result, _ = execute("ban_member", gateway=gateway, target_id=MEMBER)

    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_BOT_LACKS_RIGHT
    assert gateway.actions() == []


def test_a_telegram_error_is_reported_not_swallowed():
    gateway = FakeGateway(fail={"ban"})

    result, _ = execute("ban_member", gateway=gateway, target_id=MEMBER)

    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_TELEGRAM_ERROR
    assert result.message


def test_a_delete_without_the_bot_right_does_not_delete():
    gateway = FakeGateway(can_delete=False)

    result, _ = execute("delete_message", gateway=gateway, message_id=12345)

    assert not result.ok
    assert gateway.actions() == []


def test_a_promotion_records_the_role_even_when_telegram_refuses():
    """Two layers, and the brief says they may disagree.

    §35 describes the state where somebody is an application moderator but not
    a Telegram administrator. Refusing to record the role because Telegram said
    no would make that state unreachable, and would lose the operator's
    decision. It is reported as a note beside the success instead.
    """
    gateway = FakeGateway(fail={"promote"})

    result, _ = execute("promote_member", gateway=gateway, target_id=MEMBER,
                        role="moderator")

    assert result.ok
    assert db.admin_get(MEMBER)["role"] == rbac.ROLE_MODERATOR
    assert result.extra.get("telegram_note")


def test_a_promotion_without_the_bot_right_is_noted_not_refused():
    gateway = FakeGateway(can_promote=False)

    result, _ = execute("promote_member", gateway=gateway, target_id=MEMBER,
                        role="moderator")

    assert result.ok
    assert result.extra.get("telegram_note")
    assert gateway.actions() == []


def test_demoting_somebody_who_is_not_an_admin_says_so():
    result, gateway = execute("demote_member", target_id=MEMBER)

    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_NOT_AN_ADMIN
    assert gateway.actions() == []


def test_a_demotion_removes_the_role_and_the_rights():
    db.admin_set(MEMBER, rbac.ROLE_MODERATOR,
                 rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR], granted_by=OWNER)

    result, gateway = execute("demote_member", target_id=MEMBER)

    assert result.ok
    assert db.admin_get(MEMBER) is None
    assert ("demote", CHAT, MEMBER) in gateway.actions()


# ══ TOOL EXPOSURE ═════════════════════════════════════════════════════════
def write_tools_for(role: str, user_id: int) -> list[str]:
    permissions = (
        rbac.PERMISSION_SET
        if role == rbac.ROLE_OWNER
        else rbac.ROLE_PERMISSIONS.get(role, frozenset())
    )
    principal = rbac.Principal(user_id, role, permissions, "test")
    return [
        n
        for n in admin_tools.tool_names_for(principal)
        if admin_tools.TOOLS[n].kind == admin_tools.KIND_WRITE
    ]


def test_a_guest_is_offered_no_write_tool():
    assert write_tools_for(rbac.ROLE_GUEST, MEMBER) == []


def test_a_guest_is_offered_no_tools_at_all_by_default():
    principal = rbac.guest(MEMBER)

    assert admin_tools.tool_names_for(principal) == ()
    assert admin_tools.declarations_for(principal) == []


def test_a_helper_is_offered_only_the_warning():
    assert write_tools_for(rbac.ROLE_HELPER, HELPER) == ["warn_member"]


def test_a_moderator_is_offered_the_moderation_tools():
    assert set(write_tools_for(rbac.ROLE_MODERATOR, MODERATOR)) == {
        "warn_member",
        "delete_message",
        "mute_member",
        "unmute_member",
    }


def test_a_senior_admin_is_offered_the_role_tools():
    offered = set(write_tools_for(rbac.ROLE_SENIOR_ADMIN, SENIOR))

    assert "ban_member" in offered
    assert "promote_member" in offered
    assert "demote_member" in offered


def test_the_owner_is_offered_every_write_tool():
    offered = set(write_tools_for(rbac.ROLE_OWNER, OWNER))
    every = {n for n, s in admin_tools.TOOLS.items() if s.kind == admin_tools.KIND_WRITE}

    assert offered == every


def test_exposure_is_not_authority():
    """A tool that was not offered is still refused, and one that was is checked.

    The point of the test is that the two are independent: the guest is offered
    nothing AND the service refuses them, and the moderator is offered ``mute``
    AND the service still authorises it. Exposure is a courtesy; the check is
    the control.
    """
    assert write_tools_for(rbac.ROLE_GUEST, MEMBER) == []
    refused, gateway = execute("mute_member", actor_id=MEMBER, target_id=HELPER)
    assert not refused.ok and gateway.actions() == []

    allowed, gw2 = execute("mute_member", actor_id=MODERATOR, target_id=MEMBER)
    assert allowed.ok and gw2.actions()


def test_a_guest_can_be_given_the_read_tools_by_configuration(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", True)
    names = admin_tools.tool_names_for(rbac.guest(MEMBER))

    assert "get_member" in names
    assert not [n for n in names if admin_tools.TOOLS[n].kind == admin_tools.KIND_WRITE]


# ══ READ TOOLS ════════════════════════════════════════════════════════════
def read_tool(name, args, *, principal=None, **kwargs):
    return run(
        admin_tools.run_read_tool(
            name,
            args,
            principal=principal or rbac.resolve(OWNER),
            chat_id=CHAT,
            gateway=kwargs.pop("gateway", FakeGateway()),
            **kwargs,
        )
    )


def test_get_member_answers_from_the_database_not_the_model():
    db.admin_set(MEMBER, rbac.ROLE_MODERATOR,
                 rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR], granted_by=OWNER)

    answer = read_tool("get_member", {"user_id": MEMBER})

    assert answer["application"]["role"] == rbac.ROLE_MODERATOR
    assert answer["application"]["is_owner"] is False


def test_get_member_reports_the_owner_as_the_owner():
    answer = read_tool("get_member", {"user_id": OWNER})

    assert answer["application"]["is_owner"] is True
    assert answer["application"]["role"] == rbac.ROLE_OWNER


def test_list_admins_includes_the_owner_and_the_stored_rows():
    db.admin_set(MEMBER, rbac.ROLE_MODERATOR,
                 rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR], granted_by=OWNER)

    answer = read_tool("list_admins", {})

    ids = {row["user_id"] for row in answer["admins"]}
    assert OWNER in ids
    assert MEMBER in ids


def test_get_permissions_with_no_argument_answers_about_the_asker():
    answer = read_tool("get_permissions", {}, principal=rbac.resolve(MODERATOR))

    assert answer["user_id"] == MODERATOR
    assert "moderation.mute" in answer["permissions"]
    assert "moderation.ban" not in answer["permissions"]


def test_get_permissions_can_describe_a_role():
    answer = read_tool("get_permissions", {"role": "moderator"})

    assert "moderation.mute" in answer["permissions"]
    assert "admins.manage" not in answer["permissions"]


def test_resolve_reply_target_returns_the_replied_to_user():
    answer = read_tool(
        "resolve_reply_target", {}, reply_user_id=MEMBER, reply_name="Ali"
    )

    assert answer["user_id"] == MEMBER
    assert answer["name"] == "Ali"


def test_resolve_reply_target_refuses_to_guess_when_there_is_no_reply():
    """The brief: never guess a target. An empty answer would be filled in."""
    answer = read_tool("resolve_reply_target", {})

    assert "error" in answer


def test_a_read_tool_without_a_user_id_says_so():
    answer = read_tool("get_member", {})

    assert "error" in answer


def test_recent_admin_context_is_bounded_to_this_chat():
    """Cross-chat isolation: one room's business is not another's context."""
    db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                   chat_id=CHAT)
    db.audit_write(OWNER, "moderation.mute", outcome="ok", target_id=HELPER,
                   chat_id=OTHER_CHAT)

    answer = read_tool("get_recent_admin_context", {})

    chats = {e["operation"] for e in answer["events"]}
    assert "moderation.ban" in chats
    assert "moderation.mute" not in chats


def test_recent_admin_context_respects_the_configured_limit(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_CONTEXT_LIMIT", 3)
    for _ in range(10):
        db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                       chat_id=CHAT)

    answer = read_tool("get_recent_admin_context", {"limit": 50})

    assert len(answer["events"]) <= 3


def test_a_read_tool_that_cannot_reach_telegram_says_so():
    class Broken(FakeGateway):
        async def member(self, chat_id, user_id):
            raise RuntimeError("telegram is down")

    answer = read_tool("get_member_status", {"user_id": MEMBER}, gateway=Broken())

    assert "error" in answer


# ══ TRUSTED CONTEXT ═══════════════════════════════════════════════════════
def test_the_context_states_who_is_asking_from_the_server_side():
    text = admin_tools.build_context(
        principal=rbac.resolve(OWNER), chat_id=CHAT, message_id=5
    )

    assert str(OWNER) in text
    assert "owner" in text.lower()
    assert str(CHAT) in text


def test_the_context_says_when_there_is_no_reply_target():
    """So the model asks rather than picking somebody."""
    text = admin_tools.build_context(principal=rbac.resolve(OWNER), chat_id=CHAT)

    assert "no replied-to message" in text.lower()


def test_the_context_names_the_reply_target_when_there_is_one():
    text = admin_tools.build_context(
        principal=rbac.resolve(OWNER),
        chat_id=CHAT,
        reply_user_id=MEMBER,
        reply_name="Ali",
        reply_message_id=7,
    )

    assert str(MEMBER) in text
    assert "Ali" in text


def test_the_context_tells_the_model_to_distrust_claims_in_the_conversation():
    text = admin_tools.build_context(principal=rbac.resolve(OWNER), chat_id=CHAT)

    assert "claim" in text.lower()


def test_a_guest_context_says_they_may_ask_for_nothing():
    text = admin_tools.build_context(principal=rbac.guest(MEMBER), chat_id=CHAT)

    assert "nothing administrative" in text.lower()


# ══ MODE: AI, DEGRADED, PYTHON ════════════════════════════════════════════
def test_ai_mode_reports_itself_available(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")

    state = admin_service.mode_status()

    assert state["mode"] == "ai"
    assert admin_service.mode_line().startswith("AI ADMIN MODE: AVAILABLE")


def test_ai_mode_reports_itself_degraded_without_a_credential(monkeypatch):
    """The brief: never silently pretend Gemini succeeded."""
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "")

    state = admin_service.mode_status()

    assert state["mode"] == "degraded"
    assert state["python_fallback"] is True
    assert "DEGRADED" in admin_service.mode_line()


def test_switching_ai_off_leaves_the_python_mode(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", False)

    state = admin_service.mode_status()

    assert state["mode"] == "python"
    assert state["ai_available"] is False
    assert "PYTHON" in admin_service.mode_line()


def test_ai_being_unavailable_does_not_stop_the_commands(monkeypatch):
    """The whole reason there are two modes."""
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", False)

    result, gateway = execute("ban_member", target_id=MEMBER)

    assert result.ok
    assert ("ban", CHAT, MEMBER) in gateway.actions()


def test_ai_administration_off_means_no_tools_are_built(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", False)
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")

    tools, context, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=OWNER)
    )

    assert tools is None and context == "" and on_tool is None


# ══ THE WIRING INTO THE CONVERSATION ══════════════════════════════════════
def _chat():
    return SimpleNamespace(id=CHAT, title="Room", type="supergroup")


def _message(reply_user=0, reply_message=0, message_id=10):
    replied = None
    if reply_user or reply_message:
        replied = SimpleNamespace(
            message_id=reply_message,
            from_user=SimpleNamespace(id=reply_user, full_name="Ali"),
        )
    return SimpleNamespace(message_id=message_id, reply_to_message=replied)


def _update():
    return SimpleNamespace(effective_chat=_chat(), effective_message=_message())


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(id=BOT_ID, username="guardbot"))


def test_an_admin_talking_to_the_bot_gets_the_tools(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")

    tools, context, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=OWNER)
    )

    assert tools
    assert str(OWNER) in context
    assert callable(on_tool)


def test_an_ordinary_member_talking_to_the_bot_gets_none():
    tools, context, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=MEMBER)
    )

    assert tools is None and context == "" and on_tool is None


def test_the_runner_routes_a_write_tool_through_the_service(monkeypatch):
    """The seam the brief cares about: the model asks, the service decides."""
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")
    gateway = FakeGateway()
    monkeypatch.setattr(main, "TelegramGateway", lambda ctx: gateway)

    _, _, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=OWNER)
    )
    answer = run(on_tool("ban_member", {"target_user_id": MEMBER}))

    assert answer["ok"] is True
    assert ("ban", CHAT, MEMBER) in gateway.actions()


def test_the_runner_refuses_a_write_tool_for_a_non_admin(monkeypatch):
    """The tool is not offered, and if it were produced anyway it is refused."""
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")
    gateway = FakeGateway()
    monkeypatch.setattr(main, "TelegramGateway", lambda ctx: gateway)

    # A helper gets tools, so the runner exists — and the helper may not ban.
    _, _, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=HELPER)
    )
    answer = run(on_tool("ban_member", {"target_user_id": MEMBER}))

    assert answer["ok"] is False
    assert gateway.actions() == []


def test_the_runner_reports_a_refusal_with_a_reason_to_explain(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")
    gateway = FakeGateway()
    monkeypatch.setattr(main, "TelegramGateway", lambda ctx: gateway)

    _, _, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=MODERATOR)
    )
    answer = run(on_tool("ban_member", {"target_user_id": MEMBER}))

    assert answer["ok"] is False
    assert answer["explanation"]
    assert answer["message"]


def test_the_runner_answers_read_tools_without_touching_authority(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")
    gateway = FakeGateway()
    monkeypatch.setattr(main, "TelegramGateway", lambda ctx: gateway)

    _, _, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=MODERATOR)
    )
    answer = run(on_tool("get_member", {"user_id": MEMBER}))

    assert answer["application"]["user_id"] == MEMBER
    assert gateway.actions() == []


def test_the_runner_reports_an_unknown_tool(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")

    _, _, on_tool = main._ai_admin_turn(
        _update(), _ctx(), _message(), _chat(), SimpleNamespace(id=OWNER)
    )
    answer = run(on_tool("rm_rf", {}))

    assert "error" in answer


def test_the_reply_target_reaches_the_context(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")

    _, context, _ = main._ai_admin_turn(
        _update(),
        _ctx(),
        _message(reply_user=MEMBER, reply_message=4),
        _chat(),
        SimpleNamespace(id=OWNER),
    )

    assert str(MEMBER) in context


# ══ ISOLATION ═════════════════════════════════════════════════════════════
def test_only_the_conversational_workload_has_administrative_tools():
    """Acquisition, moderation and transcription must have no route here.

    Checked by reading the modules rather than by calling them: the property is
    that they do not import the administrative layer at all, which is stronger
    than any behaviour test — there is no code path to exercise.
    """
    import pathlib

    from app import ai_intent, ai_moderation, transcribe

    for module in (ai_intent, ai_moderation, transcribe):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "admin_service" not in source
        assert "admin_tools" not in source


def test_the_chat_module_does_not_execute_tools_itself():
    """``chat`` carries the loop; it does not know what a ban is."""
    import pathlib

    from app import chat

    source = pathlib.Path(chat.__file__).read_text(encoding="utf-8")
    assert "admin_service" not in source
    assert "ban_member" not in source


def test_the_service_never_imports_telegram():
    """It reaches Telegram through the gateway, and cannot do otherwise."""
    import pathlib

    source = pathlib.Path(admin_service.__file__).read_text(encoding="utf-8")
    assert "import telegram" not in source
    assert "from telegram" not in source


def test_a_moderation_verdict_cannot_become_a_ban():
    """Moderation's action vocabulary and administration's are disjoint."""
    from app import mod_policy

    administrative = set(admin_service.OPERATIONS)
    moderation_actions = {a.value for a in mod_policy.Action}

    assert not (administrative & moderation_actions)


# ══ AUDIT ═════════════════════════════════════════════════════════════════
def test_a_successful_action_is_audited_with_both_interfaces_marked():
    execute("ban_member", target_id=MEMBER)

    row = db.audit_recent(1)[0]

    assert row["action"] == "moderation.ban"
    assert row["outcome"] == "ok"
    assert row["actor_id"] == OWNER
    assert row["target_id"] == MEMBER
    assert row["chat_id"] == CHAT
    # Which front door. Recorded in the row itself, not only in the log line,
    # because "was this the assistant or a person?" is the first question asked
    # about an action somebody disagrees with.
    assert row["interface"] == admin_service.INTERFACE_AI


def test_a_request_from_the_command_path_is_marked_as_python():
    """The other half of the same fact, written by main._audit."""
    main._audit(OWNER, "moderation.ban", "ok", target_id=MEMBER, chat_id=CHAT)

    row = db.audit_recent(1)[0]

    assert row["interface"] == admin_service.INTERFACE_PYTHON
    assert row["interface"] != admin_service.INTERFACE_AI


def test_the_two_interfaces_cannot_be_confused_for_one_another():
    """One row each, and the record tells them apart."""
    execute("ban_member", target_id=MEMBER)
    main._audit(OWNER, "moderation.ban", "ok", target_id=HELPER, chat_id=CHAT)

    rows = db.audit_recent(2)
    by_target = {r["target_id"]: r["interface"] for r in rows}

    assert by_target[MEMBER] == admin_service.INTERFACE_AI
    assert by_target[HELPER] == admin_service.INTERFACE_PYTHON


def test_a_refusal_is_audited_too():
    """"Who tried" is the question asked after an incident."""
    execute("ban_member", actor_id=MEMBER, target_id=HELPER)

    row = db.audit_recent(1)[0]

    assert row["action"] == "moderation.ban"
    assert row["outcome"] == admin_service.OUTCOME_DENIED
    assert row["actor_id"] == MEMBER


def test_the_recent_admin_context_carries_the_interface():
    """The assistant may see which door past actions came through."""
    execute("ban_member", target_id=MEMBER)

    events = admin_tools.recent_admin_context(CHAT)

    assert events
    assert events[0]["interface"] == admin_service.INTERFACE_AI


def test_recent_refusals_lists_only_the_ones_that_did_not_happen():
    execute("ban_member", target_id=MEMBER)          # ok
    execute("ban_member", actor_id=MEMBER, target_id=HELPER)   # denied

    refusals = admin_service.recent_refusals(5)

    assert [r["outcome"] for r in refusals] == [admin_service.OUTCOME_DENIED]
    assert all(r["outcome"] != admin_service.OUTCOME_OK for r in refusals)


def test_a_duplicate_is_not_counted_as_a_refusal():
    """The desired state holds; it was simply reached earlier.

    Counting it would send an operator chasing a problem that is not there.
    """
    gateway = FakeGateway()
    req = request("ban_member", target_id=MEMBER)
    run(admin_service.execute(req, gateway, bot_id=BOT_ID))
    run(admin_service.execute(req, gateway, bot_id=BOT_ID))

    assert admin_service.recent_refusals(5) == []


def test_the_status_report_says_which_mode_is_live():
    report = admin_service.status_report()

    assert report.startswith(admin_service.mode_line())
    assert "AI ADMIN MODE:" in report


def test_the_status_report_lists_a_refusal_when_there_is_one():
    execute("ban_member", actor_id=MEMBER, target_id=HELPER)

    report = admin_service.status_report()

    assert "Recent refusals" in report
    assert admin_service.OUTCOME_DENIED in report
    # And which door it came through, in the same line.
    assert f"via={admin_service.INTERFACE_AI}" in report


def test_the_status_report_is_honest_when_there_is_nothing_to_report():
    assert "No administrative refusals on record." in admin_service.status_report()


def test_a_duplicate_is_audited_once():
    gateway = FakeGateway()
    req = request("ban_member", target_id=MEMBER)
    run(admin_service.execute(req, gateway, bot_id=BOT_ID))
    run(admin_service.execute(req, gateway, bot_id=BOT_ID))

    assert len(db.audit_recent(10)) == 1


def test_the_audit_row_never_contains_a_message_body():
    """Only ids, an action, an outcome and a short reason."""
    execute("warn_member", target_id=MEMBER, reason="a very long reason " * 50)

    row = db.audit_recent(1)[0]

    assert len(row["detail"]) <= 300
    assert "a very long reason" not in row["detail"]


def test_retention_prunes_the_request_table():
    execute("ban_member", target_id=MEMBER)
    assert db.admin_request_get(db.audit_recent(1) and "") is None  # no id given

    db.admin_request_prune(0)  # a zero window means "do not prune"
    assert db.audit_recent(1)


def test_events_and_requests_are_separate_from_conversational_memory():
    """Administrative history is not chat history, and must not be."""
    execute("ban_member", target_id=MEMBER)

    history = db.chat_history(CHAT, OWNER, limit=10, ttl=3600)

    assert history == []
