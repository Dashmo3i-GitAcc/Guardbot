"""The repeated-violation ladder and the warning, through the real handler.

One confirmed explicit-media deletion is one violation. The warning is sent
each time; the timed restriction is applied when the count reaches
VIOLATION_MUTE_AFTER. A failed deletion must never count as a violation.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import config, db, detector, main

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999


# --------------------------------------------------------------- fakes
class FakeFile:
    async def download_to_drive(self, path):
        with open(path, "wb") as fh:
            fh.write(b"fake-media")


class FakeBot:
    def __init__(self, restrict_fails=False):
        self.sent: list[tuple[int, str]] = []
        self.photos: list[str] = []
        self.documents: list[str] = []
        self.deleted: list[int] = []
        self.restricted: list[tuple[int, int, object]] = []
        self.restrict_fails = restrict_fails

    async def get_file(self, file_id):
        return FakeFile()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        self.photos.append(caption)

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        self.documents.append(caption)

    async def delete_message(self, chat_id, message_id):
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
        self.delete_error = None

    async def delete(self):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error


class StubDetector:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def detect(self, path):
        if self.error is not None:
            raise self.error
        return self.result


def media_obj():
    return SimpleNamespace(file_id="f", file_size=1000, thumbnail=None, thumb=None)


def explicit_raw(label="FEMALE_GENITALIA_EXPOSED", score=0.67):
    return [{"class": label, "score": score, "box": [0, 0, 1, 1]}]


def run(bot, msg, user_id=7):
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


def photo(message_id, bot, user_id=7):
    return run(bot, FakeMessage(message_id=message_id, photo=[media_obj()]), user_id)


def group_texts(bot):
    return [t for c, t in bot.sent if c == CHAT_ID]


@pytest.fixture(autouse=True)
def violation_env(monkeypatch, tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    monkeypatch.setattr(config, "TMP_DIR", str(tmp))
    monkeypatch.setattr(config, "MAX_DOWNLOAD_MB", 20)
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "MUTE_MINUTES", 15)
    monkeypatch.setattr(config, "VIOLATION_MUTE_AFTER", 3)
    monkeypatch.setattr(detector, "_scene_pipe", None)
    main._admin_cache.clear()
    db.init()  # a fresh in-memory database for each test
    yield tmp
    if db._conn is not None:
        db._conn.close()
        db._conn = None


# ------------------------------------------------------- the ladder
def test_first_violation_warns_and_counts(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    photo(1, bot)

    assert db.get_strikes(CHAT_ID, 7) == 1
    assert bot.restricted == []
    assert len(group_texts(bot)) == 1  # the warning


def test_second_violation_counts_but_does_not_restrict(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    photo(1, bot)
    photo(2, bot)

    assert db.get_strikes(CHAT_ID, 7) == 2
    assert bot.restricted == []


def test_third_violation_restricts(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    for i in range(3):
        photo(i, bot)

    assert db.get_strikes(CHAT_ID, 7) == 3
    assert len(bot.restricted) == 1
    assert bot.restricted[0][:2] == (CHAT_ID, 7)
    assert bot.restricted[0][2] is not None  # timed, not permanent


def test_every_violation_at_or_after_the_threshold_restricts(monkeypatch):
    # a user who keeps violating past the threshold is restricted again, which
    # extends the existing restriction rather than silently ignoring it
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    for i in range(4):
        photo(i, bot)

    assert db.get_strikes(CHAT_ID, 7) == 4
    assert len(bot.restricted) == 2  # one at the 3rd, one at the 4th


def test_restrict_failure_still_warns_and_does_not_crash(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot(restrict_fails=True)
    for i in range(3):
        photo(i, bot)

    assert bot.restricted == []
    assert db.get_strikes(CHAT_ID, 7) == 3
    assert len(group_texts(bot)) == 3


def test_counts_are_per_user(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    photo(1, bot, user_id=7)
    photo(2, bot, user_id=8)

    assert db.get_strikes(CHAT_ID, 7) == 1
    assert db.get_strikes(CHAT_ID, 8) == 1
    assert bot.restricted == []


# ------------------------------------------------------- what must not count
def test_delete_failure_does_not_count_or_warn(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    msg = FakeMessage(message_id=1, photo=[media_obj()])
    msg.delete_error = TelegramError("message can't be deleted")
    run(bot, msg)

    assert msg.delete_calls == 1
    assert db.get_strikes(CHAT_ID, 7) == 0
    assert bot.restricted == []
    assert group_texts(bot) == []


def test_review_does_not_count_or_warn(monkeypatch):
    # REVIEW band: logged only, never deleted, never a violation
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw(score=0.30)))
    bot = FakeBot()
    photo(1, bot)

    assert db.get_strikes(CHAT_ID, 7) == 0
    assert group_texts(bot) == []
    assert bot.restricted == []


def test_safe_does_not_count_or_warn(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    bot = FakeBot()
    photo(1, bot)

    assert db.get_strikes(CHAT_ID, 7) == 0
    assert group_texts(bot) == []


def test_detector_failure_does_not_count_or_warn(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(error=RuntimeError("onnx boom")))
    bot = FakeBot()
    photo(1, bot)

    assert db.get_strikes(CHAT_ID, 7) == 0
    assert group_texts(bot) == []
    assert bot.restricted == []


# ------------------------------------------------------- exemption
def test_bot_owner_is_exempt_from_sexual_moderation(monkeypatch):
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", {7})
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    msg = photo(1, bot)

    assert msg.delete_calls == 0
    assert db.get_strikes(CHAT_ID, 7) == 0
    assert group_texts(bot) == []


def test_a_telegram_admin_is_not_exempt_from_sexual_moderation(monkeypatch):
    # only WHITELIST_USER_IDS are exempt; a Telegram admin is a normal member
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot = FakeBot()
    msg = photo(1, bot)

    assert msg.delete_calls == 1
    assert db.get_strikes(CHAT_ID, 7) == 1
