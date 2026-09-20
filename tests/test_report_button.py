"""The admin-report self-delete button.

A confirmed moderation report is sent to the admin-report chat with an inline
button. Any *current member* of that chat (not necessarily a Telegram
administrator) may press it to delete the report message itself - the moderated
message is already gone. A user who is not in the group, and a callback coming
from any other chat, must be refused and must delete nothing.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from app import config, main

ADMIN_CHAT_ID = -1003108143607
OTHER_CHAT_ID = -1009999999999


# --------------------------------------------------------------- fakes
class FakeBot:
    def __init__(
        self,
        status=ChatMemberStatus.MEMBER,
        member_error=None,
        photo_fails=False,
        document_fails=False,
    ):
        self.status = status
        self.member_error = member_error
        self.photo_fails = photo_fails
        self.document_fails = document_fails
        self.messages = []   # kwargs of every send_message
        self.photos = []     # kwargs of every send_photo
        self.documents = []  # kwargs of every send_document
        self.member_checks = []

    async def get_chat_member(self, chat_id, user_id):
        self.member_checks.append((chat_id, user_id))
        if self.member_error is not None:
            raise self.member_error
        return SimpleNamespace(status=self.status)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        if self.photo_fails:
            raise TelegramError("photo rejected")
        self.photos.append({"chat_id": chat_id, "caption": caption, **kwargs})

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        if self.document_fails:
            raise TelegramError("document rejected")
        self.documents.append({"chat_id": chat_id, "caption": caption, **kwargs})


class FakeReportMessage:
    def __init__(self, chat_id, message_id=55, delete_error=None):
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.delete_error = delete_error
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error


class FakeQuery:
    def __init__(
        self,
        bot,
        *,
        data=main.REPORT_DELETE_CALLBACK,
        chat_id=ADMIN_CHAT_ID,
        user_id=7,
        message_id=55,
        delete_error=None,
    ):
        self.bot = bot
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeReportMessage(chat_id, message_id, delete_error)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def press(query):
    update = SimpleNamespace(callback_query=query)
    asyncio.run(main.on_report_delete(update, SimpleNamespace(bot=query.bot)))
    return query


def button_of(markup):
    assert isinstance(markup, InlineKeyboardMarkup), "no inline keyboard attached"
    return markup.inline_keyboard[0][0]


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    yield


# --------------------------------------------- who may delete the report
def test_a_normal_member_can_delete_the_report():
    bot = FakeBot(status=ChatMemberStatus.MEMBER)
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 1
    assert bot.member_checks == [(ADMIN_CHAT_ID, 7)]
    assert q.answers and q.answers[0][1] is False  # answered, no alert


def test_a_telegram_administrator_can_delete_the_report():
    bot = FakeBot(status=ChatMemberStatus.ADMINISTRATOR)
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 1


def test_the_group_owner_can_delete_the_report():
    bot = FakeBot(status=ChatMemberStatus.OWNER)
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 1


def test_a_non_member_cannot_delete_the_report():
    bot = FakeBot(status=ChatMemberStatus.LEFT)
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 0
    assert q.answers[0][0] == "⛔ شما عضو این گپ نیستید."
    assert q.answers[0][1] is True


def test_a_banned_user_cannot_delete_the_report():
    bot = FakeBot(status=ChatMemberStatus.BANNED)
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 0
    assert q.answers[0][0] == "⛔ شما عضو این گپ نیستید."


def test_membership_check_failure_refuses_and_does_not_crash():
    # fail closed: an unverifiable member must not be able to delete
    bot = FakeBot(member_error=TelegramError("user not found"))
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 0
    assert q.answers[0][0] == "⛔ شما عضو این گپ نیستید."


# --------------------------------------------- callback safety
def test_callback_from_another_chat_cannot_delete_anything():
    bot = FakeBot(status=ChatMemberStatus.MEMBER)
    q = press(FakeQuery(bot, chat_id=OTHER_CHAT_ID))

    assert q.message.delete_calls == 0
    assert bot.member_checks == []  # refused before any membership check
    assert q.answers[0][1] is True


def test_handler_is_inert_when_no_admin_chat_is_configured(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", None)
    bot = FakeBot()
    q = press(FakeQuery(bot))

    assert q.message.delete_calls == 0


def test_already_deleted_report_is_handled_safely():
    bot = FakeBot(status=ChatMemberStatus.MEMBER)
    q = press(FakeQuery(bot, delete_error=TelegramError("message to delete not found")))

    assert q.message.delete_calls == 1
    assert q.answers and q.answers[0][1] is False  # answered, never crashed


# --------------------------------------------- the button on every report
def test_the_button_uses_a_dedicated_callback_prefix():
    assert main.REPORT_DELETE_CALLBACK == "report_delete"
    assert button_of(main._report_keyboard()).callback_data == "report_delete"


def test_button_is_attached_to_text_reports():
    bot = FakeBot()
    asyncio.run(main.report(SimpleNamespace(bot=bot), "hello"))

    assert len(bot.messages) == 1
    assert button_of(bot.messages[0]["reply_markup"]).callback_data == "report_delete"


def explicit_result(label="FEMALE_GENITALIA_EXPOSED", score=0.67):
    return SimpleNamespace(matched=SimpleNamespace(label=label, score=score))


class StubAnalysis:
    def __init__(self, frame):
        self._frame = frame

    def evidence_frame(self, label):
        return self._frame


def send_evidence_report(bot, analysis):
    chat = SimpleNamespace(id=ADMIN_CHAT_ID)
    user = SimpleNamespace(id=7, full_name="Tester", username="tester")
    msg = SimpleNamespace(message_id=55)
    asyncio.run(
        main._send_explicit_report(
            SimpleNamespace(bot=bot),
            chat, user, msg, "photo", analysis, explicit_result(), "",
        )
    )


def test_button_is_attached_to_evidence_photo_reports(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpg")
    bot = FakeBot()

    send_evidence_report(bot, StubAnalysis(str(frame)))

    assert len(bot.photos) == 1
    assert button_of(bot.photos[0]["reply_markup"]).callback_data == "report_delete"


def test_button_is_attached_to_evidence_document_reports(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpg")
    bot = FakeBot(photo_fails=True)  # force the document fallback

    send_evidence_report(bot, StubAnalysis(str(frame)))

    assert len(bot.documents) == 1
    assert button_of(bot.documents[0]["reply_markup"]).callback_data == "report_delete"


def test_button_is_attached_to_the_text_only_report_fallback(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpg")
    bot = FakeBot(photo_fails=True, document_fails=True)

    send_evidence_report(bot, StubAnalysis(str(frame)))

    assert len(bot.messages) == 1
    assert button_of(bot.messages[0]["reply_markup"]).callback_data == "report_delete"
