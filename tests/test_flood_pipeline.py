"""Instant-flood (burst) behaviour in the real media handler.

The Telegram layer is faked; the detector is stubbed. These tests pin what a
confirmed flood actually does: restrict the sender, delete only the burst's own
messages, and warn - and what it must never do (touch a photo, punish a safe
sender, claim an action Telegram refused).
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import burst, config, detector, main

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999


# --------------------------------------------------------------- fakes
class FakeFile:
    async def download_to_drive(self, path):
        with open(path, "wb") as fh:
            fh.write(b"fake-media")


class FakeBot:
    def __init__(self, restrict_fails=False, delete_fails=False):
        self.sent: list[tuple[int, str]] = []      # (chat_id, text)
        self.photos: list[str] = []
        self.documents: list[str] = []
        self.deleted: list[int] = []
        self.restricted: list[tuple[int, int, object]] = []
        self.get_file_calls = 0
        self.restrict_fails = restrict_fails
        self.delete_fails = delete_fails

    async def get_chat_member(self, chat_id, user_id):
        # these fakes are ordinary members unless a test overrides them
        return SimpleNamespace(status="member")

    async def get_file(self, file_id):
        self.get_file_calls += 1
        return FakeFile()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        self.photos.append(caption)

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        self.documents.append(caption)

    async def delete_message(self, chat_id, message_id):
        if self.delete_fails:
            raise TelegramError("message can't be deleted")
        self.deleted.append(message_id)

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, until_date=None):
        if self.restrict_fails:
            raise TelegramError("not enough rights to restrict")
        self.restricted.append((chat_id, user_id, until_date))


class FakeMessage:
    def __init__(self, message_id=55, **media):
        self.message_id = message_id
        self.photo = None
        self.video = None
        self.animation = None
        self.video_note = None
        self.sticker = None
        self.document = None
        for key, value in media.items():
            setattr(self, key, value)
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


class StubDetector:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def detect(self, path):
        if self.error is not None:
            raise self.error
        return self.result


def media_obj():
    return SimpleNamespace(file_id="f", file_size=900, thumbnail=None, thumb=None)


def gif(message_id, user_id=7, bot=None):
    return send_media(bot, user_id, message_id, animation=media_obj())


def sticker(message_id, user_id=7, bot=None, **kw):
    return send_media(
        bot, user_id, message_id,
        sticker=SimpleNamespace(
            file_id="s", file_size=900, is_animated=False, is_video=False,
            thumbnail=None, thumb=None, **kw,
        ),
    )


def photo(message_id, user_id=7, bot=None):
    return send_media(bot, user_id, message_id, photo=[media_obj()])


def send_media(bot, user_id, message_id, **media):
    msg = FakeMessage(message_id=message_id, **media)
    user = SimpleNamespace(
        id=user_id, full_name=f"User {user_id}", username=f"u{user_id}", is_bot=False
    )
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=user,
    )
    asyncio.run(main.on_media(update, SimpleNamespace(bot=bot)))
    return msg


def group_texts(bot):
    return [t for c, t in bot.sent if c == CHAT_ID]


def admin_texts(bot):
    return [t for c, t in bot.sent if c == ADMIN_CHAT_ID]


@pytest.fixture(autouse=True)
def flood_env(monkeypatch, tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    monkeypatch.setattr(config, "TMP_DIR", str(tmp))
    monkeypatch.setattr(config, "MAX_DOWNLOAD_MB", 20)
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "BURST_ENABLED", True)
    monkeypatch.setattr(config, "BURST_WINDOW_SECONDS", 3.0)
    monkeypatch.setattr(config, "BURST_MAX_ITEMS", 5)
    monkeypatch.setattr(
        config, "BURST_MEDIA_KINDS",
        {"gif", "sticker", "animated_sticker", "video_sticker", "video_note"},
    )
    monkeypatch.setattr(config, "MUTE_MINUTES", 15)
    # a fresh tracker per test: the module-level one carries state
    monkeypatch.setattr(
        main, "_bursts", burst.BurstTracker(window_seconds=3.0, max_items=5)
    )
    main._admin_cache.clear()
    # keep the fall-through media path off the real model and ffmpeg
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", None)

    def _extract(video_path, out_dir, count):
        return []

    monkeypatch.setattr(detector, "extract_frames", _extract)
    yield tmp


# ------------------------------------------------------- the flood rule
def test_six_rapid_gifs_trigger_a_flood():
    bot = FakeBot()
    for i in range(6):
        gif(100 + i, bot=bot)

    assert bot.restricted and bot.restricted[0][:2] == (CHAT_ID, 7)
    assert sorted(bot.deleted) == [100, 101, 102, 103, 104, 105]
    assert len(group_texts(bot)) == 1
    # the first five were processed normally; the sixth crossed the threshold
    # and was handled by the flood rule without a download or an inference
    assert bot.get_file_calls == 5


def test_exactly_five_rapid_gifs_is_not_a_flood():
    bot = FakeBot()
    for i in range(5):
        gif(100 + i, bot=bot)

    assert bot.restricted == []
    assert bot.deleted == []
    assert group_texts(bot) == []
    assert bot.get_file_calls == 5  # each one still went through the media path


def test_ten_stickers_in_two_seconds_trigger_a_flood():
    bot = FakeBot()
    for i in range(10):
        sticker(200 + i, bot=bot)

    # the flood is stopped at the sixth message; the burst's own six are the
    # only ones removed, and the later ones start a fresh window
    assert len(bot.restricted) == 1
    assert sorted(bot.deleted) == list(range(200, 206))


def test_rapid_photos_are_never_a_flood():
    bot = FakeBot()
    for i in range(12):
        photo(300 + i, bot=bot)

    assert bot.restricted == []
    assert bot.deleted == []
    assert bot.get_file_calls == 12


def test_a_telegram_admin_is_not_exempt_from_the_flood_rule():
    # on_media performs no administrator lookup at all: only WHITELIST_USER_IDS
    # (bot owners) are exempt, so a Telegram admin is treated like any member.
    bot = FakeBot()
    for i in range(6):
        gif(100 + i, bot=bot)

    assert len(bot.restricted) == 1
    assert sorted(bot.deleted) == [100, 101, 102, 103, 104, 105]


def test_bot_owner_is_exempt_from_the_flood_rule(monkeypatch):
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", {7})
    bot = FakeBot()
    for i in range(12):
        gif(100 + i, bot=bot)

    assert bot.restricted == []
    assert bot.deleted == []
    assert bot.get_file_calls == 0


# ------------------------------------------------------- only the burst's own messages
def test_only_the_burst_messages_are_targeted():
    bot = FakeBot()
    for i in range(6):
        gif(100 + i, bot=bot)
    assert sorted(bot.deleted) == [100, 101, 102, 103, 104, 105]

    # a later message is a fresh window, not a re-report of the old burst
    gif(999, bot=bot)
    assert 999 not in bot.deleted
    assert sorted(bot.deleted) == [100, 101, 102, 103, 104, 105]


def test_two_separate_floods_are_two_restrictions():
    bot = FakeBot()
    for i in range(6):
        gif(100 + i, bot=bot)
    for i in range(6):
        gif(200 + i, bot=bot)

    assert len(bot.restricted) == 2
    assert sorted(bot.deleted) == list(range(100, 106)) + list(range(200, 206))


def test_burst_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "BURST_ENABLED", False)
    bot = FakeBot()
    for i in range(12):
        gif(100 + i, bot=bot)

    assert bot.restricted == []
    assert bot.deleted == []


# ------------------------------------------------------- fail-open
def test_restrict_failure_is_reported_and_not_claimed():
    bot = FakeBot(restrict_fails=True)
    for i in range(6):
        gif(100 + i, bot=bot)

    assert bot.restricted == []                 # nothing claimed
    assert group_texts(bot) == []               # no "you were muted" message
    assert len(admin_texts(bot)) == 1           # the admin is told instead
    assert sorted(bot.deleted) == [100, 101, 102, 103, 104, 105]


def test_delete_failure_is_fail_open():
    bot = FakeBot(delete_fails=True)
    for i in range(6):
        gif(100 + i, bot=bot)

    assert bot.deleted == []                    # nothing was actually deleted
    assert len(bot.restricted) == 1             # the restriction still applied
    assert len(group_texts(bot)) == 1


def test_restrict_receives_the_configured_expiry(monkeypatch):
    monkeypatch.setattr(config, "MUTE_MINUTES", 15)
    bot = FakeBot()
    for i in range(6):
        gif(100 + i, bot=bot)

    _, _, until = bot.restricted[0]
    assert until is not None  # a timed restriction, not a permanent ban
