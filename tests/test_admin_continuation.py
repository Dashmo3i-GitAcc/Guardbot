"""Following an instruction up: «این کاربر رو ساکت کن» then «درش بیار».

The bug this file exists for is that the second sentence did nothing. The mute
worked, and the unmute that plainly referred to it came back as "I cannot do
that" — while the assistant held ``unmute_member`` the whole time.

Two separate defects produced that, and they are fixed in two different places,
so the tests are in two groups.

**The antecedent.** A tool call and its result live only inside the turn that
made them: ``chat._tool_turn`` builds the exchange in a local list and returns
the final text, and the conversation store can only hold ``user`` and ``model``
turns — a function turn has no representation in it. So the follow-up turn
started with a history in which the mute had never happened, and "him" had
nothing to point at. ``admin_tools.recent_actions_block`` fixes that by stating
the server's own record in the trusted block. It is read from the audit table,
which the execution layer writes *after* an action succeeded — so it cannot be
planted, it is scoped to this actor in this room, and it never lists anything
that did not actually happen.

**The persona.** ``chat.SYSTEM_INSTRUCTION`` is written for a turn with no
tools and said so in as many words: "you cannot change an account, place an
order, contact anyone, or run any operation". A request that says both "you
cannot do this" and "here is the tool that does this" is answered by refusing.
``chat.TOOL_AMENDMENT`` is appended for a turn that actually holds tools, and
the persona line is now scoped rather than absolute.

The security rule both halves have to respect is the one the brief states: the
context may resolve **what** was meant, and may never grant **authority**. So
the tests below assert the block exists *and* that a follow-up which resolves
to it still goes through the ordinary check — a demoted actor gets the same
refusal they would have got without the context.

Nothing here talks to Google or to Telegram.
"""
import asyncio
import time

import pytest

from app import admin_service, admin_tools, chat, config, db, main, rbac

OWNER = 999
SENIOR = 555
MODERATOR = 777
HELPER = 888
MEMBER = 42
OTHER_MEMBER = 43
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
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "ADMIN_REQUEST_REPLAY_WINDOW", 120)
    monkeypatch.setattr(config, "ADMIN_CONTEXT_WINDOW", 6 * 3600)
    db.init()
    db.admin_reset()
    yield
    db.admin_reset()


# ── The gateway double ────────────────────────────────────────────────────
class FakeGateway:
    """Records every Telegram call, and can be told to fail one."""

    def __init__(self, *, fail=None, can_restrict=True):
        self.fail = set(fail or ())
        self.can_restrict = can_restrict
        self.calls: list[tuple] = []

    async def bot_right(self, chat_id: int, right: str) -> bool:
        self.calls.append(("bot_right", chat_id, right))
        if right == "can_restrict_members":
            return self.can_restrict
        return right in ("can_promote_members", "can_delete_messages")

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} refused by Telegram")

    async def promote(self, chat_id, user_id, rights):
        self._maybe_fail("promote")
        self.calls.append(("promote", chat_id, user_id))

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
            "telegram_status": "member",
            "is_telegram_admin": False,
        }

    def actions(self) -> list[tuple]:
        return [c for c in self.calls if c[0] not in ("bot_right", "member")]


def run(coro):
    return asyncio.run(coro)


def act(operation: str, *, actor_id: int, target_id: int = 0, chat_id: int = CHAT,
        gateway=None):
    """One action through the real service, exactly as ``on_tool`` runs it."""
    gateway = gateway or FakeGateway()
    request = admin_service.AdminRequest(
        operation=operation,
        chat_id=chat_id,
        actor_id=actor_id,
        target_id=target_id,
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_AI,
        at=int(time.time()),
    )
    return run(admin_service.execute(request, gateway, bot_id=BOT_ID)), gateway


def block_for(user_id: int, *, chat_id: int = CHAT) -> str:
    """The antecedent block as the model would receive it, built for real."""
    return admin_tools.recent_actions_block(
        rbac.resolve(user_id), chat_id=chat_id
    )


def context_for(user_id: int, *, chat_id: int = CHAT, **kwargs) -> str:
    return admin_tools.build_context(
        principal=rbac.resolve(user_id), chat_id=chat_id, **kwargs
    )


# ══ THE ANTECEDENT ════════════════════════════════════════════════════════
def test_a_mute_leaves_the_muted_user_as_the_antecedent():
    result, _ = act("mute_member", actor_id=OWNER, target_id=MEMBER)
    assert result.ok

    block = block_for(OWNER)

    assert str(MEMBER) in block
    assert "moderation.mute" in block
    # The action name alone is not enough: the id is the whole point.
    assert "target" not in block.split("moderation.mute")[0].rsplit("-", 1)[-1]


def test_the_antecedent_is_told_to_the_model_that_the_action_already_happened():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    block = block_for(OWNER)

    assert "already happened" in block
    assert "newest first" in block


def test_a_follow_up_is_told_what_the_id_means():
    """«درش بیار» is the exact phrase the owner used. It has to be in there."""
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    block = block_for(OWNER)

    assert "درش بیار" in block
    assert "they mean" in block


def test_two_mutes_leave_two_candidates_and_the_model_is_told_not_to_pick():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)
    act("mute_member", actor_id=OWNER, target_id=OTHER_MEMBER)

    block = block_for(OWNER)

    assert str(MEMBER) in block
    assert str(OTHER_MEMBER) in block
    # The instruction is the server's, and it is the only thing standing between
    # an ambiguous follow-up and a wrong person being unmuted.
    assert "ask which one" in block
    assert "never pick between them" in block


def test_the_reply_target_of_an_addressed_mute_becomes_the_antecedent():
    """The other way a target arrives: the moderator replied to the message."""
    result, _ = act("mute_member", actor_id=MODERATOR, target_id=MEMBER)
    assert result.ok

    block = block_for(MODERATOR)

    assert str(MEMBER) in block


def test_only_a_successful_action_becomes_the_antecedent():
    ok, _ = act("mute_member", actor_id=OWNER, target_id=MEMBER)
    assert ok.ok
    failed, gateway = act(
        "mute_member",
        actor_id=OWNER,
        target_id=OTHER_MEMBER,
        gateway=FakeGateway(fail={"mute"}),
    )
    assert not failed.ok
    assert failed.outcome == admin_service.OUTCOME_TELEGRAM_ERROR

    block = block_for(OWNER)

    assert str(MEMBER) in block
    # The failed mute never happened, so nothing may resolve to it.
    assert str(OTHER_MEMBER) not in block


def test_a_refused_action_leaves_no_antecedent():
    """A helper may not mute. The refusal must not become a target either."""
    refused, gateway = act("mute_member", actor_id=HELPER, target_id=MEMBER)
    assert not refused.ok
    assert gateway.actions() == []

    assert block_for(HELPER) == ""


def test_a_refused_action_does_not_shadow_a_real_one():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)
    act("unmute_member", actor_id=HELPER, target_id=OTHER_MEMBER)  # refused

    block = block_for(OWNER)

    assert str(OTHER_MEMBER) not in block
    assert str(MEMBER) in block


def test_the_antecedent_is_your_own_actions_and_not_somebody_elses():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    # Same room, different administrator. Somebody else's mute is not a
    # referent they may act on, so it must not be offered to them.
    assert block_for(SENIOR) == ""
    assert str(MEMBER) not in block_for(SENIOR)


def test_the_antecedent_does_not_cross_groups():
    act("mute_member", actor_id=OWNER, target_id=MEMBER, chat_id=CHAT)

    assert block_for(OWNER, chat_id=OTHER_CHAT) == ""
    assert str(MEMBER) in block_for(OWNER, chat_id=CHAT)


def test_the_antecedent_is_bounded_by_the_context_window(monkeypatch):
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    # Move the clock past the window. ``audit_write`` stamps rows with the
    # current time, so the only way to age one is to age the clock.
    ahead = time.time() + config.ADMIN_CONTEXT_WINDOW + 60
    monkeypatch.setattr(db.time, "time", lambda: ahead)

    assert block_for(OWNER) == ""


def test_the_antecedent_is_bounded_to_a_few_actions():
    for target in (MEMBER, OTHER_MEMBER, 100, 101, 102):
        act("mute_member", actor_id=OWNER, target_id=target)

    block = block_for(OWNER)

    listed = [
        line for line in block.splitlines() if line.startswith("- moderation.")
    ]
    assert len(listed) <= admin_tools.RECENT_ACTIONS_MAX


def test_the_block_reaches_the_model_through_build_context():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    context = context_for(OWNER)

    assert "recent actions in this group" in context
    assert str(MEMBER) in context


def test_a_member_with_no_actions_gets_no_block():
    """The common case must cost nothing and say nothing."""
    assert block_for(MEMBER) == ""
    assert "recent actions in this group" not in context_for(MEMBER)


def test_the_block_is_empty_when_there_is_no_room():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    assert admin_tools.recent_actions_block(rbac.resolve(OWNER), chat_id=0) == ""


# ══ CONTEXT IS NOT AUTHORITY ══════════════════════════════════════════════
def test_the_block_says_the_action_still_goes_through_the_check():
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    block = block_for(OWNER)

    assert "whether you are allowed" in block
    assert "the action still goes through the usual check" in block


def test_a_demoted_actor_still_gets_the_refusal(monkeypatch):
    """The antecedent survives a role change. The permission does not.

    This is the rule the whole design turns on: the block may tell the model
    *who* was meant, and the service still decides whether the actor may act.
    A moderator who muted somebody and was then demoted has a real antecedent
    and no authority, and the follow-up has to fail.
    """
    result, _ = act("mute_member", actor_id=MODERATOR, target_id=MEMBER)
    assert result.ok

    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{SENIOR}:senior_admin"])
    assert block_for(MODERATOR) != ""  # context still resolves the target

    follow_up, gateway = act("unmute_member", actor_id=MODERATOR, target_id=MEMBER)

    assert not follow_up.ok
    assert follow_up.outcome == admin_service.OUTCOME_DENIED
    assert follow_up.reason == rbac.REASON_NOT_ADMIN
    assert gateway.actions() == []


def test_a_member_cannot_act_on_a_block_they_can_see():
    """Even handed the exact id, an ordinary member reaches nothing."""
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    follow_up, gateway = act("unmute_member", actor_id=MEMBER, target_id=MEMBER)

    assert not follow_up.ok
    assert gateway.actions() == []


def test_a_member_is_offered_no_tool_surface_at_all():
    """No tools means no context block either — nothing is built for them."""
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    tools, context, on_tool = asyncio.run(
        main._admin_turn_core(
            actor_id=MEMBER, chat_id=CHAT, gateway=FakeGateway(), bot_id=BOT_ID
        )
    )

    assert tools is None
    assert context == ""
    assert on_tool is None


def test_an_administrator_is_offered_the_tool_that_undoes_their_own_action():
    """The other half of the bug: the tool has to be there to be called."""
    act("mute_member", actor_id=OWNER, target_id=MEMBER)

    tools, context, on_tool = asyncio.run(
        main._admin_turn_core(
            actor_id=OWNER, chat_id=CHAT, gateway=FakeGateway(), bot_id=BOT_ID
        )
    )

    assert tools  # the declaration list the model is handed
    names = admin_tools.tool_names_for(rbac.resolve(OWNER))
    assert "unmute_member" in names
    assert "mute_member" in names
    assert str(MEMBER) in context


def test_the_owner_can_complete_the_mute_then_unmute_sequence():
    """The reported bug, end to end, against the real service."""
    muted, _ = act("mute_member", actor_id=OWNER, target_id=MEMBER)
    assert muted.ok

    # The follow-up turn's context is what makes "him" resolvable.
    assert str(MEMBER) in block_for(OWNER)

    unmuted, gateway = act("unmute_member", actor_id=OWNER, target_id=MEMBER)

    assert unmuted.ok
    assert ("unmute", CHAT, MEMBER) in gateway.actions()


# ══ THE PERSONA ═══════════════════════════════════════════════════════════
def test_the_persona_no_longer_claims_it_cannot_run_operations():
    text = chat.SYSTEM_INSTRUCTION

    assert "With no tool for it" in text
    # The claim survives, but scoped. What must be gone is the flat version that
    # a tool-bearing turn was reading as a statement about itself.
    assert "you cannot do — you cannot change an account" not in text
    assert "With no tool for it, you cannot change an account" in text


def test_a_turn_with_tools_is_told_the_persona_does_not_apply():
    from google.genai import types

    with_tools = chat._generation_config(types, tools=[object()])
    without = chat._generation_config(types)

    assert chat.TOOL_AMENDMENT in with_tools.system_instruction
    assert chat.TOOL_AMENDMENT not in without.system_instruction
    # The persona is still there — the amendment is appended, not substituted.
    assert chat.SYSTEM_INSTRUCTION in with_tools.system_instruction


def test_the_amendment_says_the_tool_is_permission_checked():
    text = chat.TOOL_AMENDMENT

    assert "checked again by the server" in text
    assert "Having the tool is not permission" in text
    assert "never guess" in text


def test_an_awareness_pass_keeps_its_own_instruction(monkeypatch):
    """A caller that supplied an instruction is not a conversation.

    The awareness pass has its own rules about tools and must not have the
    conversational persona — or its amendment — smuggled in behind them.
    """
    from google.genai import types

    config_ = chat._generation_config(
        types, tools=[object()], instruction=chat.AWARENESS_INSTRUCTION
    )

    assert config_.system_instruction.startswith(chat.AWARENESS_INSTRUCTION)
    assert chat.TOOL_AMENDMENT not in config_.system_instruction
    assert chat.SYSTEM_INSTRUCTION not in config_.system_instruction
