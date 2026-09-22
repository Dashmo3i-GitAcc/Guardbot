"""What "unmute" actually sends to Telegram, and what the bot then reports.

This file exists because of a live bug with a long tail. ``FULL`` — the
permission set behind every unmute — listed only the ten *sending* permissions
and omitted ``can_change_info``, ``can_invite_users``, ``can_pin_messages`` and
``can_manage_topics``. A Bot API ``ChatPermissions`` treats an unspecified field
as false, so ``restrict_chat_member(permissions=FULL)`` wrote a restriction that
kept those four denied — and because the call carried no ``until_date``, the
record was permanent.

The result, measured against the live group: of nine members this bot had
muted, the one that was never unmuted read as ``member`` (its timed mute had
expired on its own) and all eight that were unmuted read as ``restricted``
forever. The assistant, reading ``telegram_status: restricted`` from
``get_member_status``, concluded the unmute had not worked and called
``unmute_member`` again — one member was unmuted three times.

So there are two properties worth pinning, and they are different:

* the permission set that lifts a restriction must be *complete*, and must stay
  complete as the Bot API grows fields; and
* the status the model is handed must not require it to infer "can this person
  speak" from a field that means something else.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telegram import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    ChatPermissions,
    User,
)

from app import admin_tools, config, db, main, rbac

CHAT = -1001234567890
TARGET = 4242
OWNER = 999

# The four fields the old hand-written `FULL` forgot. Named individually because
# this list *is* the bug: a test that only checked the sending permissions would
# have passed against the broken constant.
FORGOTTEN = (
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_topics",
)

# Every field of the request object, read from the library rather than written
# out, so a field added by a future Bot API shows up here without an edit.
PERMISSION_FIELDS = tuple(ChatPermissions.__slots__)


# ── Fakes ─────────────────────────────────────────────────────────────────
class RecordingBot:
    """A bot that records exactly what a restriction call carried."""

    def __init__(self, member=None):
        self.id = 1
        self.calls = []
        self._member = member

    async def restrict_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append(
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "permissions": kwargs.get("permissions"),
                "until_date": kwargs.get("until_date"),
            }
        )

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append({"chat_id": chat_id, "user_id": user_id, "ban": True})

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.calls.append({"chat_id": chat_id, "user_id": user_id, "unban": True})

    async def get_chat_member(self, chat_id, user_id):
        if isinstance(self._member, Exception):
            raise self._member
        return self._member


def gateway_for(bot):
    return main.TelegramGateway(SimpleNamespace(bot=bot))


def user():
    return User(id=TARGET, first_name="Sara", is_bot=False)


def restricted(**overrides):
    """A `ChatMemberRestricted` with everything allowed unless overridden.

    That is what a member should look like after a *correct* unmute, and it is
    also what the live group showed for the four fields the bug left behind — so
    the overrides below are how each case is expressed.
    """
    fields = {name: True for name in PERMISSION_FIELDS}
    fields["can_send_messages"] = True
    fields.update(overrides)
    return ChatMemberRestricted(
        user=user(),
        is_member=True,
        until_date=None,
        **fields,
    )


# ── The permission set that lifts a restriction ───────────────────────────
def test_the_unrestrict_permission_set_allows_every_permission():
    """Every field, read from the library. An omitted one is a denied one."""
    missing = [name for name in PERMISSION_FIELDS if getattr(main.FULL, name) is not True]
    assert missing == [], f"FULL leaves these denied: {missing}"


@pytest.mark.parametrize("field", FORGOTTEN)
def test_the_four_fields_the_bug_left_denied_are_allowed(field):
    """The specific regression, named so a reader knows what broke."""
    assert getattr(main.FULL, field) is True


def test_the_unrestrict_set_is_not_a_hand_written_list():
    """`all_permissions()` is the Bot API's own sentence for lifting a restriction.

    Asserted as a property rather than by calling the constructor again: what
    matters is that no field can be forgotten, and the parametrised test above
    is what checks that. This one checks the set matches what the library says
    "everything" means, so a future field cannot be missed by both.
    """
    expected = ChatPermissions.all_permissions()
    for name in PERMISSION_FIELDS:
        assert getattr(main.FULL, name) == getattr(expected, name), name


def test_the_mute_set_never_grants_anything():
    """A mute names one field and relies on "unspecified means false".

    That reliance is deliberate, so the property to pin is the one it buys: no
    field of a mute may be true except the one that is deliberately false.
    Filling the others in "for completeness" would turn a mute into a mute that
    still allows media.
    """
    assert main.MUTED.can_send_messages is False
    granted = [
        name
        for name in PERMISSION_FIELDS
        if name != "can_send_messages" and getattr(main.MUTED, name) is True
    ]
    assert granted == [], f"the mute grants: {granted}"


# ── The calls themselves ──────────────────────────────────────────────────
def test_unmute_sends_the_whole_permission_set():
    bot = RecordingBot()
    asyncio.run(gateway_for(bot).unmute(CHAT, TARGET))
    assert len(bot.calls) == 1
    sent = bot.calls[0]["permissions"]
    denied = [name for name in PERMISSION_FIELDS if getattr(sent, name) is not True]
    assert denied == [], f"the unmute left these denied: {denied}"


def test_unmute_carries_no_deadline():
    """Which is exactly why the permission set has to be complete.

    Without an `until_date` the restriction record is permanent, so an
    incomplete set is not a temporary inconvenience — it is a lasting one. This
    test documents that coupling rather than asserting a preference.
    """
    bot = RecordingBot()
    asyncio.run(gateway_for(bot).unmute(CHAT, TARGET))
    assert bot.calls[0]["until_date"] is None


def test_mute_is_timed_and_total():
    bot = RecordingBot()
    asyncio.run(gateway_for(bot).mute(CHAT, TARGET))
    call = bot.calls[0]
    assert call["permissions"].can_send_messages is False
    until = call["until_date"]
    assert until is not None
    expected = datetime.now(timezone.utc) + timedelta(
        minutes=max(1, int(config.MUTE_MINUTES))
    )
    assert abs((until - expected).total_seconds()) < 60


def test_the_test_account_unrestrict_sends_the_whole_set_too():
    """The same constant, on the other path that lifts a restriction."""
    import inspect

    source = inspect.getsource(main._test_unrestrict_job)
    assert "permissions=FULL" in source
    denied = [name for name in PERMISSION_FIELDS if getattr(main.FULL, name) is not True]
    assert denied == []


# ── What the model is told ────────────────────────────────────────────────
def status_for(member) -> dict:
    return asyncio.run(
        gateway_for(RecordingBot(member=member)).member(CHAT, TARGET)
    )


def test_a_plain_member_is_not_muted():
    answer = status_for(ChatMemberMember(user=user()))
    assert answer["telegram_status"] == "member"
    assert answer["can_send_messages"] is True
    assert answer["is_muted"] is False


def test_a_restricted_member_who_can_still_speak_is_not_reported_as_muted():
    """The live case that produced the duplicate unmutes.

    Telegram reports `restricted` for any denied permission, so a member denied
    only `can_pin_messages` reads as restricted while being perfectly able to
    talk. `is_muted` is what makes that unambiguous.
    """
    member = restricted(can_change_info=False, can_invite_users=False,
                        can_pin_messages=False, can_manage_topics=False)
    answer = status_for(member)
    assert answer["telegram_status"] == "restricted"
    assert answer["can_send_messages"] is True
    assert answer["is_muted"] is False, (
        "a member who can send messages must not be reported as muted; this is "
        "what made the assistant unmute them again"
    )


def test_a_genuinely_muted_member_is_reported_as_muted():
    answer = status_for(restricted(can_send_messages=False))
    assert answer["telegram_status"] == "restricted"
    assert answer["can_send_messages"] is False
    assert answer["is_muted"] is True


def test_a_banned_member_is_reported_as_muted():
    member = ChatMemberBanned(
        user=user(), until_date=None
    )
    answer = status_for(member)
    assert answer["is_muted"] is True
    assert answer["can_send_messages"] is False


def test_an_administrator_is_never_reported_as_muted():
    member = ChatMemberAdministrator(
        user=user(),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=True,
        can_restrict_members=True,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )
    answer = status_for(member)
    assert answer["is_muted"] is False
    assert answer["is_telegram_admin"] is True


def test_the_owner_is_never_reported_as_muted():
    answer = status_for(
        ChatMemberOwner(user=user(), is_anonymous=False)
    )
    assert answer["is_muted"] is False
    assert answer["is_telegram_admin"] is True


def test_the_payload_still_refuses_cleanly_when_telegram_cannot_answer():
    from telegram.error import TelegramError

    answer = status_for(TelegramError("nope"))
    assert answer == {"error": "not a member of this chat, or not readable"}


# ── Through the read tool, which is what the model actually calls ─────────
@pytest.fixture
def owner_principal(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    db.init()
    return rbac.resolve(OWNER)


def read_tool(name, args, gateway, principal):
    return asyncio.run(
        admin_tools.run_read_tool(
            name, args, principal=principal, chat_id=CHAT, gateway=gateway
        )
    )


def test_get_member_status_passes_is_muted_through(owner_principal):
    gateway = gateway_for(RecordingBot(member=restricted(can_send_messages=False)))
    answer = read_tool("get_member_status", {"user_id": TARGET}, gateway,
                       owner_principal)
    assert answer["is_muted"] is True


def test_get_member_status_does_not_call_a_talking_member_muted(owner_principal):
    gateway = gateway_for(
        RecordingBot(member=restricted(can_pin_messages=False))
    )
    answer = read_tool("get_member_status", {"user_id": TARGET}, gateway,
                       owner_principal)
    assert answer["is_muted"] is False
    assert answer["can_send_messages"] is True


def test_the_tool_description_tells_the_model_how_to_read_the_status():
    """The wording is the fix's other half: the model was told "restricted".

    A tool that reports a status the model can misread needs to say what the
    status means, and it needs to say not to unmute somebody who is not muted —
    which is the specific wrong action that was taken.
    """
    spec = admin_tools.TOOLS["get_member_status"]
    assert "is_muted" in spec.description
    assert "do not unmute" in spec.description.lower()


def test_get_member_carries_the_same_status(owner_principal):
    gateway = gateway_for(RecordingBot(member=restricted(can_send_messages=False)))
    answer = read_tool("get_member", {"user_id": TARGET}, gateway, owner_principal)
    assert answer["telegram"]["is_muted"] is True
