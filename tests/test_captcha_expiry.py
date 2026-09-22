"""What happens when a join challenge runs out of time.

The brief's requirement is narrow and specific: expiry must not mean "banned".
A timer is not evidence that somebody is a bot, and the only thing a failed
challenge justifies is keeping them unverified — not removing them.

Three policies, and each is asserted here:

* ``kick`` (the default, and the long-standing behaviour) — removed, but
  immediately unbanned so they may rejoin. Never a permanent ban.
* ``restrict`` — kept in the group, kept unable to post, and handed a fresh
  challenge with a fresh deadline.
* ``none`` — no member action at all.

Plus the property that makes the setting safe to introduce: an unrecognised
value behaves as ``kick`` rather than silently disabling verification.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import config, db, main

CHAT = -1001234567890
JOINER = 4242


class FakeBot:
    """The Telegram calls the captcha flow can make, all recorded."""

    def __init__(self, *, full_name="Newcomer"):
        self.sent: list[tuple[int, str]] = []
        self.muted: list[tuple[int, int]] = []
        self.unmuted: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int]] = []
        self.unbanned: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.full_name = full_name
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._next_id)

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, **kw):
        can_post = getattr(permissions, "can_send_messages", None)
        if can_post:
            self.unmuted.append((chat_id, user_id))
        else:
            self.muted.append((chat_id, user_id))

    async def get_chat_member(self, chat_id, user_id):
        user = SimpleNamespace(id=user_id, full_name=self.full_name, is_bot=False)
        return SimpleNamespace(status="member", user=user)

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self.unbanned.append((chat_id, user_id))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def ctx_for(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def join_update(user_id=JOINER, chat_id=CHAT):
    user = SimpleNamespace(id=user_id, full_name="Newcomer", is_bot=False)
    return SimpleNamespace(
        chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            old_chat_member=SimpleNamespace(status="left"),
            new_chat_member=SimpleNamespace(status="member", user=user),
        )
    )


@pytest.fixture(autouse=True)
def captcha_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(config, "CAPTCHA_TIMEOUT_SEC", 120)
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "kick")
    monkeypatch.setattr(config, "CAPTCHA_TEXT", "سلام {name} — {timeout} ثانیه")
    monkeypatch.setattr(config, "CAPTCHA_RETRY_TEXT", "دوباره {name} — {timeout}")
    monkeypatch.setattr(config, "CAPTCHA_BUTTON", "من ربات نیستم")
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    db.init()
    main._admin_cache.clear()
    yield
    main._admin_cache.clear()


def join(bot):
    asyncio.run(main.on_member_update(join_update(), ctx_for(bot)))


def reap(bot):
    asyncio.run(main.captcha_reaper(ctx_for(bot)))


def expire():
    row = db.get_captcha(CHAT, JOINER)
    assert row is not None, "nothing to expire"
    db.add_captcha(CHAT, JOINER, row[0], int(time.time()) - 1)


# ── kick (the default) ────────────────────────────────────────────────────
def test_the_default_policy_is_kick():
    assert config.CAPTCHA_ON_EXPIRE == "kick"


def test_kick_removes_but_never_permanently_bans():
    bot = FakeBot()
    join(bot)
    expire()

    reap(bot)

    assert bot.banned == [(CHAT, JOINER)]
    assert bot.unbanned == [(CHAT, JOINER)], "a kick must unban in the same breath"
    assert db.get_captcha(CHAT, JOINER) is None


def test_an_unrecognised_policy_falls_back_to_kick(monkeypatch):
    """A typo must not silently turn verification off."""
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "kik")
    bot = FakeBot()
    join(bot)
    expire()

    reap(bot)

    assert bot.banned == [(CHAT, JOINER)]


# ── restrict ──────────────────────────────────────────────────────────────
def test_restrict_keeps_the_member_and_refreshes_the_challenge(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "restrict")
    bot = FakeBot()
    join(bot)
    expire()

    reap(bot)

    assert bot.banned == [], "restrict must never remove the member"
    assert bot.unbanned == []
    assert (CHAT, JOINER) in bot.muted, "the member must stay unable to post"
    row = db.get_captcha(CHAT, JOINER)
    assert row is not None, "a fresh challenge must exist"
    assert row[1] > int(time.time()), "the fresh deadline must be in the future"


def test_restrict_sends_a_fresh_button(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "restrict")
    bot = FakeBot(full_name="میلاد")
    join(bot)
    expire()
    before = len(bot.sent)

    reap(bot)

    assert len(bot.sent) == before + 1
    assert "میلاد" in bot.sent[-1][1]


def test_a_restricted_member_can_still_verify_with_the_fresh_challenge(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "restrict")
    bot = FakeBot()
    join(bot)
    expire()
    reap(bot)

    # The refreshed row is solvable, exactly as the first one was.
    row = db.get_captcha(CHAT, JOINER)
    assert db.claim_captcha(CHAT, JOINER, before=int(time.time())) is True
    assert row is not None


# ── none ──────────────────────────────────────────────────────────────────
def test_none_takes_no_member_action(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "none")
    bot = FakeBot()
    join(bot)
    expire()

    reap(bot)

    assert bot.banned == []
    assert bot.unbanned == []
    assert bot.muted == [(CHAT, JOINER)]  # only the join's own mute
    assert db.get_captcha(CHAT, JOINER) is None


def test_none_does_not_send_anything(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", "none")
    bot = FakeBot()
    join(bot)
    expire()
    before = len(bot.sent)

    reap(bot)

    assert len(bot.sent) == before


# ── The invariants that hold in every mode ────────────────────────────────
def test_a_verified_member_is_never_touched_in_any_mode(monkeypatch):
    for mode in ("kick", "restrict", "none"):
        monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", mode)
        db.remove_captcha(CHAT, JOINER)
        bot = FakeBot()
        join(bot)
        # Verify first: the row is gone.
        db.claim_captcha(CHAT, JOINER, before=int(time.time()) + 1000)
        bot.banned.clear()
        bot.unbanned.clear()

        reap(bot)

        assert bot.banned == [], f"{mode}: a verified member was kicked"


def test_no_mode_ever_leaves_a_member_permanently_banned(monkeypatch):
    """Every policy either does not ban, or unbans immediately."""
    for mode in ("kick", "restrict", "none"):
        monkeypatch.setattr(config, "CAPTCHA_ON_EXPIRE", mode)
        db.remove_captcha(CHAT, JOINER)
        bot = FakeBot()
        join(bot)
        expire()

        reap(bot)

        assert bot.banned == bot.unbanned, f"{mode}: ban without unban"
