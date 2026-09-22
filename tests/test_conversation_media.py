"""Media and voice in an explicit conversation, and the routing that guards it.

The brief's requirements, as tests: a voice message is transcribed and answered
in context, a GIF is read as media rather than acknowledged as a MIME type, a
sticker is treated as part of the conversation, unreadable media gets an honest
fallback rather than a fabricated interpretation, and an ordinary group message
never enters any of it.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import chat, config, db, main, media, nexus, people, transcribe

CHAT_ID = -1001234567890
USER_ID = 7


@pytest.fixture(autouse=True)
def conv_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr(config, "GEMINI_CHAT_ALLOW_SHARED_KEY", False)
    monkeypatch.setattr(config, "GEMINI_CHAT_VOICE_REPLY", False)
    monkeypatch.setattr(config, "GEMINI_CHAT_MEDIA_MAX_PARTS", 3)
    monkeypatch.setattr(config, "TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "test-transcribe-key")
    monkeypatch.setattr(config, "TRANSCRIBE_ALLOW_SHARED_KEY", False)
    monkeypatch.setattr(config, "BOT_ALIASES", [])
    # The sender is the owner, because a private chat with the bot is the
    # owner's channel and nobody else's — see ``nexus.accepts_private`` and
    # ``tests/test_private_boundary.py``. The actor gate is what makes this
    # necessary and it has its own suite; running these as a guest, or as a
    # non-owner administrator, would test the gate rather than the media
    # pipeline. The owner is also an actor in a group, so the group tests below
    # exercise the same policy they did before.
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "OWNER_USER_ID", USER_ID)
    # And the administrative tool path is switched off, so a turn goes through
    # the plain transport these tests stub. What is under test here is how media
    # and voice are prepared and routed, not what a model can ask for; the tool
    # path has its own suite (tests/test_ai_admin.py).
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", False)
    chat.reset_state()
    transcribe.reset_state()
    nexus.reset_state()
    people.reset_state()
    db.init()
    main._recently_deleted.clear()
    main._bot_identity.update(id=1, username="guardbot", name="Guard",
                              aliases=(), resolved=True)
    yield
    chat.reset_state()
    transcribe.reset_state()
    nexus.reset_state()
    people.reset_state()


class FakeBot:
    def __init__(self):
        self.id = 1
        self.username = "guardbot"
        self.messages = []
        self.voices = []
        self.actions = []
        self.file_bytes = b""

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_voice(self, chat_id, voice=None, **kwargs):
        self.voices.append(voice)
        return SimpleNamespace(message_id=99)

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append(action)

    async def get_file(self, file_id):
        async def _download():
            return self.file_bytes

        return SimpleNamespace(download_as_bytearray=_download)


def voice_obj(duration=3, size=1000):
    return SimpleNamespace(file_id="voice", file_unique_id="u", file_size=size,
                           duration=duration, mime_type="audio/ogg")


def sticker_obj():
    return SimpleNamespace(file_id="sticker", file_unique_id="u", file_size=500,
                           mime_type="image/webp", is_animated=False, is_video=False,
                           thumbnail=None)


def gif_obj():
    return SimpleNamespace(file_id="gif", file_unique_id="u", file_size=500,
                           mime_type="video/mp4", duration=2, thumbnail=None)


def message(**fields):
    msg = SimpleNamespace(
        message_id=10, photo=None, video=None, animation=None, video_note=None,
        sticker=None, voice=None, audio=None, document=None, text=None,
        caption=None, reply_to_message=None,
    )
    for key, value in fields.items():
        setattr(msg, key, value)
    return msg


def update_for(msg, actor=USER_ID):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=SimpleNamespace(id=actor, full_name="Tester", username=None,
                                       is_bot=False),
    )


def install_chat(monkeypatch, *responses):
    """Replace chat._request and return the recorded payloads."""
    calls = []

    async def _request(contents):
        calls.append(contents)
        if not responses:
            raise AssertionError("more chat calls than responses")
        nxt = responses[len(calls) - 1] if len(calls) <= len(responses) else ""
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(chat, "_request", _request)
    return calls


def install_transcribe(monkeypatch, *responses):
    calls = []

    async def _request(data, mime_type):
        calls.append((data, mime_type))
        return responses[len(calls) - 1]

    monkeypatch.setattr(transcribe, "_request", _request)
    return calls


def run(handler, msg, bot, ctx=None):
    asyncio.run(handler(update_for(msg), ctx or SimpleNamespace(bot=bot, args=[])))


# ── Voice ─────────────────────────────────────────────────────────────────
def test_a_voice_message_is_transcribed_and_answered(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"audio"
    install_transcribe(monkeypatch, "سلام، امروز چه خبر؟")
    calls = install_chat(monkeypatch, "سلام! خبر خاصی نیست.")

    run(main.on_private_text, message(voice=voice_obj()), bot)

    assert bot.messages == ["سلام! خبر خاصی نیست."]
    # The transcript is the turn's text, so the conversation carries on as if
    # they had typed it.
    assert "سلام، امروز چه خبر؟" in str(calls[0])


def test_the_transcript_is_what_gets_remembered(monkeypatch):
    """So the next turn can understand a follow-up to what was said aloud."""
    bot = FakeBot()
    bot.file_bytes = b"audio"
    install_transcribe(monkeypatch, "من درباره پایتون میپرسیدم")
    install_chat(monkeypatch, "باشه.")

    run(main.on_private_text, message(voice=voice_obj()), bot)

    stored = db.chat_history(CHAT_ID, USER_ID, limit=10, ttl=3600)
    assert any("پایتون" in text for _role, text in stored)


def test_a_voice_message_with_no_speech_gets_the_right_sentence(monkeypatch):
    """`I heard nothing` and `I could not listen` are different sentences."""
    bot = FakeBot()
    bot.file_bytes = b"audio"
    install_transcribe(monkeypatch, "NOSPEECH")
    calls = install_chat(monkeypatch)

    run(main.on_private_text, message(voice=voice_obj()), bot)

    assert calls == [], "a silent clip must not spend a chat request"
    assert bot.messages == [config.TRANSCRIBE_EMPTY_TEXT]


def test_a_voice_message_that_cannot_be_transcribed_says_so(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"audio"

    async def _boom(data, mime_type):
        raise RuntimeError("boom")

    monkeypatch.setattr(transcribe, "_request", _boom)
    calls = install_chat(monkeypatch)

    run(main.on_private_text, message(voice=voice_obj()), bot)

    assert calls == []
    assert bot.messages == [config.GEMINI_CHAT_UNREADABLE_TEXT]


def test_a_voice_reply_is_sent_when_enabled(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"audio"
    monkeypatch.setattr(config, "GEMINI_CHAT_VOICE_REPLY", True)
    install_transcribe(monkeypatch, "سلام")
    install_chat(monkeypatch, "سلام، خوبم.")

    async def _tts(text):
        return b"pcm-bytes"

    monkeypatch.setattr(chat, "_tts_request", _tts)
    monkeypatch.setattr(chat, "_pcm_to_ogg", lambda pcm: b"ogg-bytes")

    run(main.on_private_text, message(voice=voice_obj()), bot)

    assert bot.voices == [b"ogg-bytes"]
    assert bot.messages == [], "a voice reply does not also send the text"


def test_a_failed_voice_reply_falls_back_to_text(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"audio"
    monkeypatch.setattr(config, "GEMINI_CHAT_VOICE_REPLY", True)
    install_transcribe(monkeypatch, "سلام")
    install_chat(monkeypatch, "سلام، خوبم.")

    async def _tts(text):
        return b"pcm"

    monkeypatch.setattr(chat, "_tts_request", _tts)
    monkeypatch.setattr(chat, "_pcm_to_ogg", lambda pcm: None)

    run(main.on_private_text, message(voice=voice_obj()), bot)

    assert bot.voices == []
    assert bot.messages == ["سلام، خوبم."]


def test_a_text_message_never_gets_a_voice_reply(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(config, "GEMINI_CHAT_VOICE_REPLY", True)
    install_chat(monkeypatch, "باشه.")

    run(main.on_private_text, message(text="سلام"), bot)

    assert bot.voices == []


# ── Visual media ──────────────────────────────────────────────────────────
def test_a_sticker_is_sent_as_media_not_as_a_mime_type(monkeypatch, tmp_path):
    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    calls = install_chat(monkeypatch, "چه استیکر بامزهای 😄")

    run(main.on_private_text, message(sticker=sticker_obj()), bot)

    payload = calls[0][-1]["parts"]
    assert any("mime_type" in part for part in payload), "the sticker was not sent"
    assert any("sticker" in str(part.get("text", "")) for part in payload), (
        "the model must be told what it is looking at"
    )


def test_a_gif_is_described_as_a_gif(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 40
    calls = install_chat(monkeypatch, "😂")

    run(main.on_private_text, message(animation=gif_obj()), bot)

    assert bot.messages == ["😂"]

    payload = calls[0][-1]["parts"]
    assert any("GIF" in str(part.get("text", "")) for part in payload)


def test_a_caption_travels_with_the_media(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    calls = install_chat(monkeypatch, "باشه")

    run(main.on_private_text,
        message(sticker=sticker_obj(), caption="این چیه؟"), bot)

    payload = calls[0][-1]["parts"]
    assert any(part.get("text") == "این چیه؟" for part in payload)


def test_media_that_cannot_be_read_gets_an_honest_fallback(monkeypatch):
    """Never a fabricated interpretation of a picture nobody could see."""
    bot = FakeBot()
    bot.file_bytes = b""  # an empty download: nothing to analyse
    calls = install_chat(monkeypatch)

    run(main.on_private_text, message(sticker=sticker_obj()), bot)

    assert calls == []
    assert bot.messages == [config.GEMINI_CHAT_UNREADABLE_TEXT]


def test_a_caption_alone_is_answered_when_the_media_is_unreadable(monkeypatch):
    """There is still something to reply to, so the media is simply absent."""
    bot = FakeBot()
    bot.file_bytes = b""
    calls = install_chat(monkeypatch, "باشه")

    run(main.on_private_text,
        message(sticker=sticker_obj(), caption="سلام"), bot)

    assert calls, "the caption is a turn, so the model was asked"
    assert bot.messages == ["باشه"]


def test_a_media_turn_is_recorded_as_a_marker(monkeypatch):
    """The history stays text, so a later turn knows media was sent without
    megabytes of it being replayed."""
    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    install_chat(monkeypatch, "باشه")

    run(main.on_private_text, message(sticker=sticker_obj()), bot)

    stored = db.chat_history(CHAT_ID, USER_ID, limit=10, ttl=3600)
    assert any("[sticker]" in text for _role, text in stored)


def test_the_temporary_directory_is_cleaned_up(monkeypatch, tmp_path):
    import os

    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    install_chat(monkeypatch, "باشه")

    run(main.on_private_text, message(sticker=sticker_obj()), bot)

    assert os.listdir(str(tmp_path)) == [], "temp media was not cleaned up"


# ── Routing ───────────────────────────────────────────────────────────────
def test_an_ordinary_group_photo_is_not_answered(monkeypatch):
    """Moderation handles it; the assistant must stay out of it."""
    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    calls = install_chat(monkeypatch, "should not happen")

    run(main.on_group_chat, message(sticker=sticker_obj()), bot)

    assert calls == []
    assert bot.messages == []


def test_a_group_sticker_addressed_by_reply_is_answered(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    calls = install_chat(monkeypatch, "😂")
    reply = SimpleNamespace(from_user=SimpleNamespace(id=bot.id))

    run(main.on_group_chat, message(sticker=sticker_obj(), reply_to_message=reply), bot)

    assert bot.messages == ["😂"]
    assert calls


def test_a_group_sticker_addressed_by_mention_is_answered(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    install_chat(monkeypatch, "باشه")

    run(main.on_group_chat,
        message(sticker=sticker_obj(), caption="@guardbot این چیه؟"), bot)

    assert bot.messages == ["باشه"]


def test_a_configured_alias_counts_as_addressing_the_bot(monkeypatch):
    monkeypatch.setattr(config, "BOT_ALIASES", ["گارد"])
    main._bot_identity.update(aliases=("گارد",))
    bot = FakeBot()
    install_chat(monkeypatch, "باشه")

    run(main.on_group_chat, message(text="گارد سلام"), bot)

    assert bot.messages == ["باشه"]


def test_an_alias_must_be_a_whole_word(monkeypatch):
    """`گاردین` contains `گارد` and must not count as addressing the bot."""
    monkeypatch.setattr(config, "BOT_ALIASES", ["گارد"])
    main._bot_identity.update(aliases=("گارد",))
    bot = FakeBot()
    calls = install_chat(monkeypatch, "should not happen")

    run(main.on_group_chat, message(text="گاردین رو دیدی؟"), bot)

    assert calls == []


def test_the_word_robot_alone_does_not_address_the_bot(monkeypatch):
    bot = FakeBot()
    calls = install_chat(monkeypatch, "should not happen")

    run(main.on_group_chat, message(text="این ربات خیلی خوبه"), bot)

    assert calls == []


def test_a_message_deleted_by_moderation_is_not_answered(monkeypatch):
    """Replying to a message that was just removed is confusing and wrong."""
    bot = FakeBot()
    calls = install_chat(monkeypatch, "should not happen")
    main.mark_deleted(CHAT_ID, 10)

    run(main.on_group_chat, message(text="@guardbot سلام"), bot)

    assert calls == []
    assert bot.messages == []


def test_a_different_message_is_not_affected_by_a_deletion(monkeypatch):
    bot = FakeBot()
    install_chat(monkeypatch, "باشه")
    main.mark_deleted(CHAT_ID, 999)

    run(main.on_group_chat, message(text="@guardbot سلام"), bot)

    assert bot.messages == ["باشه"]


# ── The transcription-only interface ──────────────────────────────────────
def test_the_transcribe_command_returns_only_the_transcript(monkeypatch):
    bot = FakeBot()
    bot.file_bytes = b"audio"
    install_transcribe(monkeypatch, "این متن گفتار است")
    calls = install_chat(monkeypatch, "should not happen")

    run(main.on_transcribe_command, message(voice=voice_obj()), bot)

    assert bot.messages == ["این متن گفتار است"]
    assert calls == [], "the transcription command must not become a conversation"


def test_the_transcribe_command_needs_audio():
    bot = FakeBot()

    run(main.on_transcribe_command, message(text="سلام"), bot)

    assert bot.messages == [config.TRANSCRIBE_NEED_AUDIO_TEXT]


def test_the_transcribe_command_reports_when_it_is_switched_off(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_ENABLED", False)
    bot = FakeBot()

    run(main.on_transcribe_command, message(voice=voice_obj()), bot)

    assert bot.messages == [config.TRANSCRIBE_UNAVAILABLE_TEXT]


def test_a_transcript_is_html_escaped_before_it_is_sent(monkeypatch):
    """A transcript is model output about a stranger's speech. It is escaped."""
    bot = FakeBot()
    bot.file_bytes = b"audio"
    install_transcribe(monkeypatch, "<b>bold</b>")

    run(main.on_transcribe_command, message(voice=voice_obj()), bot)

    assert "&lt;b&gt;" in bot.messages[0]


# ── The media builder is shared, the policy is not ────────────────────────
def test_the_assistant_uses_the_same_media_builder_as_moderation():
    """A sticker the moderator can see must be a sticker the assistant can see.

    Asserted structurally: there is one `describe` and one `build`, and both
    paths call them rather than keeping their own copy of the table.
    """
    import inspect

    source = inspect.getsource(main)
    assert "media.describe(" in source
    assert "media.build(" in source
    assert media.KINDS, "the shared table must exist"


def test_the_assistant_and_the_moderator_have_separate_limits():
    assert config.GEMINI_CHAT_MEDIA_MAX_PARTS != config.GEMINI_MOD_DAILY_LIMIT
    assert config.GEMINI_CHAT_DAILY_LIMIT != config.GEMINI_MOD_DAILY_LIMIT


# ── The wire format ───────────────────────────────────────────────────────
# These two are the reason `_wire` exists as its own function. Every other test
# in this file replaces `_request`, so a mistake inside the seam is invisible to
# the suite — and a mixed string/Part payload inside a dict is exactly the
# mistake that reached a live call. It is tested here, directly.
def test_the_wire_converts_a_text_turn():
    pytest = __import__("pytest")
    pytest.importorskip("google.genai")
    wire = chat._wire([{"role": "user", "parts": [{"text": "سلام"}]}])

    assert len(wire) == 1
    assert wire[0].role == "user"
    assert wire[0].parts[0].text == "سلام"


def test_the_wire_converts_a_media_turn_without_a_mixed_payload():
    pytest = __import__("pytest")
    pytest.importorskip("google.genai")
    wire = chat._wire(
        [
            {
                "role": "user",
                "parts": [
                    {"mime_type": "image/png", "data": b"bytes"},
                    {"text": "They sent you this sticker."},
                ],
            }
        ]
    )

    parts = wire[0].parts
    assert len(parts) == 2
    assert parts[0].inline_data is not None
    assert parts[0].inline_data.mime_type == "image/png"
    assert parts[1].text == "They sent you this sticker."


def test_the_wire_keeps_a_multi_turn_conversation_in_order():
    pytest = __import__("pytest")
    pytest.importorskip("google.genai")
    wire = chat._wire(
        [
            {"role": "user", "parts": [{"text": "یک"}]},
            {"role": "model", "parts": [{"text": "دو"}]},
            {"role": "user", "parts": [{"text": "سه"}]},
        ]
    )

    assert [turn.role for turn in wire] == ["user", "model", "user"]
    assert [turn.parts[0].text for turn in wire] == ["یک", "دو", "سه"]
