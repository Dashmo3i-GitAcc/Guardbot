"""End-to-end tests for the pattern filter in app.main.on_group_filter.

The Telegram layer is faked. What is real is the handler, the rule matcher, the
enforcement executor and the strike ladder — so these tests are about the
wiring: that a hit reaches the same executor every other violation uses, that
nothing reaches Telegram when there is no hit, and that the filter cannot
punish anybody when it was not supposed to.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from app import config, db, main, text_filters

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
MEMBER = 7
ADMIN = 8


class FakeBot:
    """Records every outgoing call. `restrict_fails` exercises the ladder."""

    def __init__(self, *, restrict_fails=False):
        self.messages: list[str] = []
        self.restricted: list[tuple[int, int, object]] = []
        self.restrict_fails = restrict_fails

    async def get_chat_member(self, chat_id, user_id):
        status = (
            ChatMemberStatus.ADMINISTRATOR if user_id == ADMIN
            else ChatMemberStatus.MEMBER
        )
        return SimpleNamespace(status=status)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)

    async def restrict_chat_member(self, chat_id, user_id, permissions=None,
                                   until_date=None):
        if self.restrict_fails:
            raise TelegramError("not enough rights to restrict")
        self.restricted.append((chat_id, user_id, until_date))


class FakeMessage:
    """The handler deletes through the message itself, so the count lives here."""

    def __init__(self, text, message_id=55, delete_fails=False):
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.delete_calls = 0
        self.delete_fails = delete_fails

    async def delete(self):
        self.delete_calls += 1
        if self.delete_fails:
            raise TelegramError("message to delete not found")


@pytest.fixture(autouse=True)
def filter_env(monkeypatch):
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "FILTER_ENABLED", True)
    monkeypatch.setattr(config, "FILTER_LINK_ACTION", "review")
    monkeypatch.setattr(config, "FILTER_WORD_ACTION", "delete")
    monkeypatch.setattr(config, "FILTER_PHISHING_ACTION", "delete")
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["badword"])
    monkeypatch.setattr(config, "FILTER_ALLOWED_DOMAINS", ["example.com"])
    monkeypatch.setattr(config, "FILTER_EXEMPT_ADMINS", True)
    monkeypatch.setattr(config, "FILTER_MIN_CHARS", 4)
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", False)
    monkeypatch.setattr(config, "VIOLATION_MUTE_AFTER", 3)
    monkeypatch.setattr(config, "MUTE_MINUTES", 15)
    monkeypatch.setattr(config, "TEST_USER_ID", 0)
    # is_admin caches for 300s and mark_deleted remembers a message for 120s.
    # Both are module-level, so without this a deletion in one test hides the
    # message from the next one and the suite passes for the wrong reason.
    main._admin_cache.clear()
    main._recently_deleted.clear()
    # The filter must never reach a model. Nothing here may make a network call.
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", False)
    db.init()  # a fresh in-memory database for each test
    yield
    main._admin_cache.clear()
    main._recently_deleted.clear()
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def run_filter(bot, text, *, user_id=MEMBER, chat_id=CHAT_ID, message_id=55,
               delete_fails=False):
    msg = FakeMessage(text, message_id=message_id, delete_fails=delete_fails)
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(
            id=user_id, full_name="Tester", username="tester", is_bot=False
        ),
    )
    asyncio.run(main.on_group_filter(update, SimpleNamespace(bot=bot)))
    return msg, bot


# ── The switch ────────────────────────────────────────────────────────────
def test_nothing_happens_when_the_filter_is_off(monkeypatch):
    monkeypatch.setattr(config, "FILTER_ENABLED", False)
    bot = FakeBot()

    msg, bot = run_filter(bot, "login here http://185.12.4.9/secure")

    assert msg.delete_calls == 0
    assert bot.messages == []


# ── A banned word ─────────────────────────────────────────────────────────
def test_a_banned_word_is_deleted_and_reported():
    msg, bot = run_filter(FakeBot(), "this has badword in it")

    assert msg.delete_calls == 1
    assert len(bot.messages) == 1


def test_the_report_names_the_rule_but_not_the_message():
    """The rule and the action, never an excerpt."""
    msg, bot = run_filter(FakeBot(), "this has badword in it")

    report = bot.messages[0]

    assert "banned_word_0" in report
    assert "badword" not in report
    assert "this has" not in report
    assert str(MEMBER) in report


def test_ordinary_text_is_left_alone():
    bot = FakeBot()

    msg, bot = run_filter(bot, "سلام، حال شما چطوره؟")

    assert msg.delete_calls == 0
    assert bot.messages == []


def test_a_banned_word_does_not_fire_from_inside_a_longer_word(monkeypatch):
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["ass"])
    bot = FakeBot()

    msg, bot = run_filter(bot, "this class is assorted")

    assert msg.delete_calls == 0


# ── Links ─────────────────────────────────────────────────────────────────
def test_an_unlisted_link_is_reviewed_and_not_deleted():
    msg, bot = run_filter(FakeBot(), "have a look at https://random-site.tld/x")

    assert msg.delete_calls == 0
    assert len(bot.messages) == 1
    assert "بازبینی" in bot.messages[0]


def test_an_allow_listed_link_is_left_alone():
    bot = FakeBot()

    msg, bot = run_filter(bot, "see https://example.com/page")

    assert msg.delete_calls == 0
    assert bot.messages == []


def test_a_link_can_be_configured_to_delete(monkeypatch):
    monkeypatch.setattr(config, "FILTER_LINK_ACTION", "delete")
    msg, bot = run_filter(FakeBot(), "have a look at https://random-site.tld/x")

    assert msg.delete_calls == 1


# ── Phishing ──────────────────────────────────────────────────────────────
def test_a_phishing_message_is_deleted():
    msg, bot = run_filter(FakeBot(), "send me your seed phrase to restore it")

    assert msg.delete_calls == 1
    assert "seed_phrase_lure" in bot.messages[0]


def test_an_ip_literal_login_is_deleted():
    msg, bot = run_filter(FakeBot(), "login here http://185.12.4.9/secure")

    assert msg.delete_calls == 1


# ── Who is exempt ─────────────────────────────────────────────────────────
def test_an_administrator_is_exempt():
    bot = FakeBot()

    msg, bot = run_filter(bot, "this has badword in it", user_id=ADMIN)

    assert msg.delete_calls == 0
    assert bot.messages == []


def test_the_administrator_exemption_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "FILTER_EXEMPT_ADMINS", False)
    msg, bot = run_filter(FakeBot(), "this has badword in it", user_id=ADMIN)

    assert msg.delete_calls == 1


def test_a_whitelisted_user_is_skipped(monkeypatch):
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", {MEMBER})
    bot = FakeBot()

    msg, bot = run_filter(bot, "this has badword in it")

    assert msg.delete_calls == 0


# ── Scope ─────────────────────────────────────────────────────────────────
def test_another_chat_is_not_filtered():
    bot = FakeBot()

    msg, bot = run_filter(bot, "this has badword in it", chat_id=-100999)

    assert msg.delete_calls == 0
    assert bot.messages == []


def test_a_bot_message_is_not_filtered():
    bot = FakeBot()
    msg = FakeMessage("this has badword in it")
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=SimpleNamespace(id=99, full_name="Bot", is_bot=True),
    )

    asyncio.run(main.on_group_filter(update, SimpleNamespace(bot=bot)))

    assert msg.delete_calls == 0


def test_a_message_already_deleted_is_not_deleted_twice():
    """Belt and braces: two handlers in the same group must not both act."""
    bot = FakeBot()
    main.mark_deleted(CHAT_ID, 55)

    msg, bot = run_filter(bot, "this has badword in it")

    assert msg.delete_calls == 0


# ── The violation ladder ──────────────────────────────────────────────────
def test_a_filter_hit_does_not_strike_by_default():
    """A deleted link and a deleted explicit image are not the same offence."""
    msg, bot = run_filter(FakeBot(), "this has badword in it")

    assert db.get_strikes(CHAT_ID, MEMBER) == 0


def test_a_filter_hit_can_be_made_to_count_as_a_violation(monkeypatch):
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)

    msg, bot = run_filter(FakeBot(), "this has badword in it")

    assert db.get_strikes(CHAT_ID, MEMBER) == 1


def test_the_third_counted_hit_restricts(monkeypatch):
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)
    bot = FakeBot()

    for _ in range(3):
        msg, bot = run_filter(bot, "this has badword in it", message_id=55 + _)

    assert len(bot.restricted) == 1
    assert bot.restricted[0][:2] == (CHAT_ID, MEMBER)
    assert bot.restricted[0][2] is not None  # timed, not permanent


def test_an_uncounted_hit_never_restricts(monkeypatch):
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", False)
    bot = FakeBot()

    for _ in range(5):
        msg, bot = run_filter(bot, "this has badword in it", message_id=55 + _)

    assert bot.restricted == []


def test_every_hit_at_or_after_the_threshold_restricts(monkeypatch):
    """Someone who keeps violating past the threshold is restricted again.

    The second restriction extends the first rather than being silently
    ignored, which is the behaviour the media pipeline's ladder had before it
    was removed — preserved here because it belongs to the ladder, not to the
    detector that used to feed it.
    """
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)
    bot = FakeBot()

    for i in range(4):
        msg, bot = run_filter(bot, "this has badword in it", message_id=55 + i)

    assert db.get_strikes(CHAT_ID, MEMBER) == 4
    assert len(bot.restricted) == 2  # one at the 3rd, one at the 4th


def test_counts_are_per_user(monkeypatch):
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)
    bot = FakeBot()
    other = 9  # a second ordinary member (ADMIN is exempt by config)

    run_filter(bot, "this has badword in it", user_id=MEMBER, message_id=1)
    run_filter(bot, "this has badword in it", user_id=other, message_id=2)

    assert db.get_strikes(CHAT_ID, MEMBER) == 1
    assert db.get_strikes(CHAT_ID, other) == 1
    assert bot.restricted == []  # neither has reached the threshold


# ── The safety contract ───────────────────────────────────────────────────
def test_a_failed_deletion_does_not_strike(monkeypatch):
    """A strike is only ever recorded for content that was actually removed."""
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)

    msg, bot = run_filter(FakeBot(), "this has badword in it", delete_fails=True)

    assert db.get_strikes(CHAT_ID, MEMBER) == 0
    # The deletion was attempted — and refused by Telegram.
    assert msg.delete_calls == 1


def test_a_failed_deletion_is_not_reported_as_a_success(monkeypatch):
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)

    msg, bot = run_filter(FakeBot(), "this has badword in it", delete_fails=True)

    assert bot.messages == []


def test_a_restrict_failure_still_warns_and_does_not_crash(monkeypatch):
    monkeypatch.setattr(config, "FILTER_COUNTS_AS_VIOLATION", True)
    bot = FakeBot(restrict_fails=True)

    for _ in range(3):
        msg, bot = run_filter(bot, "this has badword in it", message_id=55 + _)

    assert bot.restricted == []
    # The warnings still went out: one report plus one user notice per hit.
    assert len(bot.messages) >= 3


# ── Isolation ─────────────────────────────────────────────────────────────
def test_the_filter_module_cannot_reach_telegram_or_the_database():
    """It returns a verdict and nothing else. Stated as a property of the source."""
    source = open("app/text_filters.py").read()

    assert "import telegram" not in source
    assert "from telegram" not in source
    assert "app.db" not in source and "from . import db" not in source
    # No executor, no strike, no message object: it returns a verdict.
    assert "moderation.enforce" not in source
    assert "add_strike" not in source


def test_the_filter_never_consults_a_model(monkeypatch):
    """A rule that can be a pattern must not be a request against a quota."""
    called = []

    def _boom(*a, **k):
        called.append(a)
        raise AssertionError("the filter consulted a model")

    monkeypatch.setattr(main.ai_moderation, "assess_text", _boom)

    msg, bot = run_filter(FakeBot(), "this has badword in it")

    assert called == []
