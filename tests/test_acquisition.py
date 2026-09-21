"""The group handler: who gets offered a test, and what the group sees.

The handler is exercised against a fake bot so the assertions can be about the
actual message text and buttons. Two rules matter most here and are pinned
explicitly: the group never receives anything but the invitation, and a repeat
ask inside the cooldown produces nothing at all.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, User

from app import config, db, main, vpnbot

GROUP_ID = -1001234567890


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """A throwaway database per test, and no Telegram admin lookups."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "guardbot.db"))
    db.init()

    async def _not_admin(ctx, chat_id, user_id):
        return False

    monkeypatch.setattr(main, "is_admin", _not_admin)
    yield


@pytest.fixture(autouse=True)
def _group_is_watched(monkeypatch):
    monkeypatch.setattr(config, "GROUP_IDS", [GROUP_ID])


class FakeBot:
    """Captures what would have been sent, and can be told to fail."""

    def __init__(self, fail_first=False):
        self.sent = []
        self.fail_first = fail_first
        self._failed = False

    async def send_message(self, chat_id, text, **kwargs):
        if kwargs.get("reply_to_message_id") and self.fail_first and not self._failed:
            self._failed = True
            from telegram.error import TelegramError

            raise TelegramError("message to be replied to is not found")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))


def _update(text, user_id=555, chat_id=GROUP_ID):
    user = User(id=user_id, first_name="Sara", is_bot=False, username="sara")
    message = Message(
        message_id=77,
        date=None,
        chat=Chat(id=chat_id, type=Chat.SUPERGROUP),
        from_user=user,
        text=text,
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=message.chat,
        effective_user=user,
    )


def _run(coro):
    return asyncio.run(coro)


def _invited(deep_link="https://t.me/quietstorm_bot?start=acq_abc"):
    async def _fake(telegram_id, **kwargs):
        return {"ok": True, "deep_link": deep_link, "invite_id": 1}

    return _fake


# ── The happy path ────────────────────────────────────────────────────────
def test_a_detected_request_gets_an_invitation(monkeypatch):
    monkeypatch.setattr(vpnbot, "request_invite", _invited())
    bot = FakeBot()

    _run(main.on_group_text(_update("سلام، یه فیلترشکن خوب دارید؟"), SimpleNamespace(bot=bot)))

    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == GROUP_ID
    assert sent["reply_to_message_id"] == 77
    buttons = sent["reply_markup"].inline_keyboard
    assert buttons[0][0].text == config.GROUP_TRIAL_BUTTON
    assert buttons[0][0].url.startswith("https://t.me/")


def test_the_group_never_receives_anything_but_the_invitation(monkeypatch):
    """No subscription link, no config, no client id — ever."""
    monkeypatch.setattr(
        vpnbot,
        "request_invite",
        _invited("https://t.me/quietstorm_bot?start=acq_SECRETTOKEN"),
    )
    bot = FakeBot()

    _run(main.on_group_text(_update("فیلترشکن میخوام"), SimpleNamespace(bot=bot)))

    sent = bot.sent[0]
    blob = sent["text"] + str(sent["reply_markup"])
    for forbidden in ("/sub/", "vless://", "vmess://", "trojan://", "pbk=", "uuid"):
        assert forbidden not in blob, f"{forbidden} must never reach the group"


def test_an_unmatched_message_produces_nothing(monkeypatch):
    async def _explode(*args, **kwargs):
        raise AssertionError("the VPN bot must not be called")

    monkeypatch.setattr(vpnbot, "request_invite", _explode)
    bot = FakeBot()

    _run(main.on_group_text(_update("سلام صبح بخیر"), SimpleNamespace(bot=bot)))

    assert bot.sent == []


# ── Anti-spam ─────────────────────────────────────────────────────────────
def test_a_repeat_ask_inside_the_cooldown_is_silent(monkeypatch):
    calls = []

    async def _counting(telegram_id, **kwargs):
        calls.append(telegram_id)
        return {"ok": True, "deep_link": "https://t.me/bot?start=acq_x"}

    monkeypatch.setattr(vpnbot, "request_invite", _counting)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot)

    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))
    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))
    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))

    assert len(calls) == 1, "one request, not three"
    assert len(bot.sent) == 1, "one reply, not three"


def test_the_cooldown_survives_a_restart(monkeypatch):
    """It lives in the database, not in a dict that a redeploy would forget."""
    monkeypatch.setattr(vpnbot, "request_invite", _invited())
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot)

    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))
    assert len(bot.sent) == 1

    # A restart re-runs db.init() against the same file.
    db.init()

    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))
    assert len(bot.sent) == 1, "the cooldown must still be in force after a restart"


def test_the_cooldown_expires(monkeypatch):
    monkeypatch.setattr(vpnbot, "request_invite", _invited())
    monkeypatch.setattr(config, "INTENT_COOLDOWN_SECONDS", 0)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot)

    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))
    _run(main.on_group_text(_update("فیلترشکن میخوام"), ctx))

    assert len(bot.sent) == 2


def test_the_cooldown_is_per_user(monkeypatch):
    monkeypatch.setattr(vpnbot, "request_invite", _invited())
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot)

    _run(main.on_group_text(_update("فیلترشکن میخوام", user_id=1), ctx))
    _run(main.on_group_text(_update("فیلترشکن میخوام", user_id=2), ctx))

    assert len(bot.sent) == 2


# ── The VPN bot's decisions ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "reason,expect_button",
    [
        ("already_invited", False),
        ("already_used", False),
    ],
)
def test_a_refusal_gets_its_own_copy_and_no_button(monkeypatch, reason, expect_button):
    async def _refuse(telegram_id, **kwargs):
        return {"ok": False, "reason": reason}

    monkeypatch.setattr(vpnbot, "request_invite", _refuse)
    bot = FakeBot()

    _run(main.on_group_text(_update("فیلترشکن میخوام"), SimpleNamespace(bot=bot)))

    assert len(bot.sent) == 1
    assert bot.sent[0]["reply_markup"] is None
    assert "http" not in bot.sent[0]["text"]


def test_an_unreachable_vpn_bot_says_so_once(monkeypatch):
    async def _down(telegram_id, **kwargs):
        raise vpnbot.VpnBotError(vpnbot.ERR_UNREACHABLE, "connection refused")

    monkeypatch.setattr(vpnbot, "request_invite", _down)
    bot = FakeBot()

    _run(main.on_group_text(_update("فیلترشکن میخوام"), SimpleNamespace(bot=bot)))

    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == config.GROUP_TRIAL_UNAVAILABLE_TEXT
    # And the cooldown was recorded, so it does not repeat for an hour.
    assert db.seconds_since_offer(GROUP_ID, 555) is not None


def test_an_unconfigured_integration_stays_silent(monkeypatch):
    async def _off(telegram_id, **kwargs):
        raise vpnbot.VpnBotError(vpnbot.ERR_NOT_CONFIGURED)

    monkeypatch.setattr(vpnbot, "request_invite", _off)
    bot = FakeBot()

    _run(main.on_group_text(_update("فیلترشکن میخوام"), SimpleNamespace(bot=bot)))

    assert bot.sent == [], "a disabled integration must not post to the group"


# ── Robustness ────────────────────────────────────────────────────────────
def test_a_deleted_original_message_still_gets_the_invitation(monkeypatch):
    monkeypatch.setattr(vpnbot, "request_invite", _invited())
    bot = FakeBot(fail_first=True)

    _run(main.on_group_text(_update("فیلترشکن میخوام"), SimpleNamespace(bot=bot)))

    assert len(bot.sent) == 1
    assert "reply_to_message_id" not in bot.sent[0]


def test_messages_outside_the_watched_groups_are_ignored(monkeypatch):
    async def _explode(*args, **kwargs):
        raise AssertionError("must not run outside the configured groups")

    monkeypatch.setattr(vpnbot, "request_invite", _explode)
    bot = FakeBot()

    _run(main.on_group_text(_update("فیلترشکن میخوام", chat_id=-100999), SimpleNamespace(bot=bot)))

    assert bot.sent == []


def test_bots_and_admins_are_never_offered_a_test(monkeypatch):
    monkeypatch.setattr(vpnbot, "request_invite", _invited())
    bot = FakeBot()

    # A bot account.
    update = _update("فیلترشکن میخوام")
    update.effective_user = User(id=9, first_name="Spam", is_bot=True)
    _run(main.on_group_text(update, SimpleNamespace(bot=bot)))
    assert bot.sent == []

    # An admin of the group.
    async def _is_admin(ctx, chat_id, user_id):
        return True

    monkeypatch.setattr(main, "is_admin", _is_admin)
    _run(main.on_group_text(_update("فیلترشکن میخوام"), SimpleNamespace(bot=bot)))
    assert bot.sent == []


def test_a_command_is_not_treated_as_intent():
    """The registered filter is the thing under test, not a copy of it."""
    accepts = main.acquisition_message_filter()

    assert accepts.check_update(_ptb_update("فیلترشکن میخوام"))
    assert not accepts.check_update(_ptb_update("/start فیلترشکن میخوام"))
    assert not accepts.check_update(_ptb_update(""))


def test_a_private_chat_is_not_watched():
    accepts = main.acquisition_message_filter()
    assert not accepts.check_update(_ptb_update("فیلترشکن میخوام", private=True))


def _ptb_update(text, private=False):
    from telegram import MessageEntity, Update

    user = User(id=555, first_name="Sara", is_bot=False)
    chat = Chat(
        id=555 if private else GROUP_ID,
        type=Chat.PRIVATE if private else Chat.SUPERGROUP,
    )
    # Telegram marks commands with a bot_command entity; filters.COMMAND keys
    # off it, so a fake command message has to carry one to be realistic.
    entities = []
    if text.startswith("/"):
        command = text.split()[0]
        entities = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command))]
    message = Message(
        message_id=1,
        date=None,
        chat=chat,
        from_user=user,
        text=text,
        entities=entities or None,
    )
    return Update(update_id=1, message=message)
