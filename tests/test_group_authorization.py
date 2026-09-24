"""The group boundary: a group is served only if the server says so.

A Telegram group does **not** become authorized because the bot was added to it,
because the bot was made an administrator there, because of the group's title or
username, because of a member's display name, or because of anything a member
claims. The only source of truth is the server-side configuration
(``GROUP_IDS``), and the boundary is enforced **before any Chat/AI work** — no
identity write, no awareness capture, no model call for an unregistered room.

The two boundaries are separate and both required: the *room* boundary
(``main.authorized_group``) and the *speaker* boundary (``rbac`` /
``nexus.accepts``). Neither implies the other.

Nothing here talks to Telegram or to Google. ``chat.reply`` is replaced, so
"no model call happened" is exact rather than inferred from a log.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import chat, config, db, main, nexus, people, web_search

OWNER = 999
ADMIN = 556
MEMBER = 42
STRANGER = 31337
CHAT = -1001234567890
OTHER_CHAT = -1009999999999
BOT_ID = 1


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin"])
    # The one authorized room. ``OTHER_CHAT`` is deliberately *not* in here.
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", True)
    monkeypatch.setattr(config, "NEXUS_OBSERVE_ADMINS", True)
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_EXTRA_ACTION_WORDS", [])
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "test-awareness-key")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "BOT_ALIASES", [])
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr("app.gemini_pool._pools", {})

    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    people.reset_state()
    chat.reset_state()
    main._recently_deleted.clear()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()
    main._awareness_sweeping = False
    main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )
    yield
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    people.reset_state()
    main._nexus_visibility.clear()
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()


class FakeBot:
    """Just enough of a bot, and a record of what it sent.

    ``get_chat_member`` answers ``administrator`` on purpose: it stands for
    Telegram saying "this member is a group admin", which the tests assert is
    **not** the same as application authorization.
    """

    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, *args, **kwargs):
        pass

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status="administrator",
            can_restrict_members=True,
            can_delete_messages=True,
            can_promote_members=True,
            can_manage_chat=True,
        )


def message(**fields):
    msg = SimpleNamespace(
        message_id=10, photo=None, video=None, animation=None, video_note=None,
        sticker=None, voice=None, audio=None, document=None, text=None,
        caption=None, reply_to_message=None,
    )
    for key, value in fields.items():
        setattr(msg, key, value)
    return msg


def update_for(msg, *, actor=MEMBER, chat_id=CHAT, chat_type="supergroup",
               username="tester", full_name="Tester"):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="Group"),
        effective_user=SimpleNamespace(
            id=actor, full_name=full_name, username=username, is_bot=False
        ),
    )


def install_model(monkeypatch):
    """Replace ``chat.reply``. Returns the list of turns it was asked for."""
    calls: list[dict] = []

    async def _reply(chat_id, user_id, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        calls.append({"chat_id": chat_id, "user_id": user_id, "text": body,
                      "context": context})
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    async def _no_search(question, *, history="", now=0.0):
        return web_search.Finding(ok=False, text="", sources=())

    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    monkeypatch.setattr(main.web_search, "research", _no_search)
    return calls


def run(handler, msg, bot, *, actor=MEMBER, chat_id=CHAT, chat_type="supergroup",
        username="tester", full_name="Tester"):
    ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
    asyncio.run(
        handler(
            update_for(msg, actor=actor, chat_id=chat_id, chat_type=chat_type,
                       username=username, full_name=full_name),
            ctx,
        )
    )


# ── The helper is the boundary, and it is fail-closed ─────────────────────
def test_authorized_group_reads_only_the_configured_list():
    assert main.authorized_group(CHAT) is True
    assert main.authorized_group(OTHER_CHAT) is False


def test_authorized_group_is_fail_closed_with_no_groups(monkeypatch):
    monkeypatch.setattr(config, "GROUP_IDS", [])
    assert main.authorized_group(CHAT) is False
    assert main.authorized_group(OTHER_CHAT) is False


# ── Unregistered rooms are denied, whatever Telegram says ─────────────────
def test_an_unregistered_group_is_denied_when_the_bot_is_admin(monkeypatch):
    """The bot being an administrator in a group does not authorize it."""
    calls = install_model(monkeypatch)
    bot = FakeBot()
    main._nexus_visibility[OTHER_CHAT] = "administrator"

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=OWNER, chat_id=OTHER_CHAT)

    assert calls == []
    assert bot.messages == []


def test_an_unregistered_group_is_denied_when_the_bot_is_only_a_member(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()
    main._nexus_visibility[OTHER_CHAT] = "member"

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=OWNER, chat_id=OTHER_CHAT)

    assert calls == []
    assert bot.messages == []


def test_denial_happens_before_any_awareness_capture_or_model_call(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_group_chat, message(text="نکسوس اینو ببین"), bot,
        actor=OWNER, chat_id=OTHER_CHAT)

    assert calls == []
    # No capture: the room never got a deadline, so the awareness pass cannot
    # ever read it either.
    assert OTHER_CHAT not in main._awareness_ready_at
    assert bot.messages == []


def test_the_owner_does_not_bypass_the_group_boundary(monkeypatch):
    """Owner authorization is the *speaker* boundary; the room boundary is
    independent, so the owner is refused in an unregistered group too."""
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=OWNER, chat_id=OTHER_CHAT)

    assert calls == []


def test_the_awareness_pass_will_not_read_an_unregistered_room(monkeypatch):
    """A stale awareness row for a de-registered room is not read."""
    install_model(monkeypatch)
    row = {"chat_id": OTHER_CHAT, "max_id": 5}
    ran = asyncio.run(main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row))
    assert ran is False


# ── A registered room still enforces the speaker boundary ─────────────────
def test_a_registered_group_with_an_authorized_member_is_answered(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=ADMIN, chat_id=CHAT)

    assert len(calls) == 1
    assert calls[0]["chat_id"] == CHAT


def test_a_registered_group_with_an_unauthorized_member_is_denied(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=MEMBER, chat_id=CHAT)

    assert calls == []
    assert bot.messages == []


def test_telegram_admin_status_alone_does_not_authorize_a_member(monkeypatch):
    """``get_chat_member`` says 'administrator'; the application still refuses.

    The handler never asks Telegram who may be answered — authority comes from
    ``rbac``, resolved from the Telegram id and the bot's own tables.
    """
    calls = install_model(monkeypatch)
    bot = FakeBot()  # its get_chat_member returns "administrator" for everyone

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=MEMBER, chat_id=CHAT)

    assert calls == []


def test_a_spoofed_username_or_display_name_does_not_authorize(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=MEMBER, chat_id=CHAT,
        username="owner", full_name="Owner Nexus")

    assert calls == []


def test_the_owner_is_answered_in_a_registered_group(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_group_chat, message(text="نکسوس سلام"), bot,
        actor=OWNER, chat_id=CHAT)

    assert len(calls) == 1


# ── Private chat is unchanged ─────────────────────────────────────────────
def test_the_owner_is_answered_in_a_private_chat(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_private_text, message(text="سلام"), bot,
        actor=OWNER, chat_id=OWNER, chat_type="private")

    assert len(calls) == 1


def test_a_stranger_is_refused_in_a_private_chat(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()

    run(main.on_private_text, message(text="سلام"), bot,
        actor=STRANGER, chat_id=STRANGER, chat_type="private")

    assert calls == []
    assert bot.messages == []
