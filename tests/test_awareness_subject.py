"""Nexus's self-awareness, end to end through the real awareness pass.

The unit tests for the subject reader live in ``test_subject.py``. This suite is
about the *behaviour*: given a room whose conversation is about Nexus — with or
without its name — does the pass understand that, decide correctly whether to
join, and quote the right message when it does?

The seam is ``chat.awareness``, so "the model was asked" and "what it was given"
are asserted exactly. The pass itself is the real ``main._awareness_pass``, the
window is the real database, and the send is the real ``_send_chat`` — so the
Telegram reply destination is asserted on the call the bot actually received.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import (
    awareness,
    awareness_context,
    chat,
    config,
    db,
    main,
    nexus,
)

OWNER = 999
ZAHRA = 111
ALI = 222
CHAT = -1001234567890
BOT_ID = 1


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_OBSERVE_ADMINS", True)
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_EXTRA_ACTION_WORDS", [])
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "BOT_ALIASES", [])
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "test-awareness-key")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_DEBOUNCE_SECONDS", 8.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_WAIT_SECONDS", 45.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 3600)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_ROWS", 400)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_PARTICIPATION_FLOOR", 60)

    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    chat.reset_state()
    awareness.reset_timers()
    awareness.reset_switch()
    awareness_context.reset_rooms()
    main._recently_deleted.clear()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._nexus_addressed.clear()
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )
    yield
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    awareness_context.reset_rooms()
    main._nexus_visibility.clear()
    main._nexus_addressed.clear()
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()


class FakeBot:
    """Records every send with the reply target it was given."""

    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=1000 + len(self.sent))

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


def ctx_for(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def capture(user_id, text, *, name="", message_id=0, directed=False,
            reply_user_id=0, reply_name="", role=awareness.ROLE_MEMBER):
    assert awareness.capture(
        CHAT, user_id, role, name or f"u{user_id}", text,
        message_id=message_id, directed=directed,
        reply_user_id=reply_user_id, reply_name=reply_name,
    ) is True


def pending_row(*, max_id=None):
    if max_id is None:
        real = [r for r in db.group_pending() if r["chat_id"] == CHAT]
        max_id = real[0]["max_id"] if real else 1
    now = int(time.time())
    return {
        "chat_id": CHAT,
        "oldest_at": now - 60,
        "newest_at": now - 60,
        "max_id": max_id,
        "pending": 1,
    }


def install(monkeypatch, decision):
    """Replace ``chat.awareness`` with the model's structured answer."""
    passes: list[dict] = []

    async def _awareness(transcript, context="", *, tools=None, on_tool=None):
        passes.append({"transcript": transcript, "context": context})
        return chat.AwarenessReply(text=json.dumps(decision), model="stub", turns=1)

    monkeypatch.setattr(main.chat, "awareness", _awareness)
    return passes


def run_pass(bot):
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))


# ══════════════════════════════════════════════════════════════════════════
# The subject reading is given to the model
# ══════════════════════════════════════════════════════════════════════════
def test_the_model_is_given_the_server_reading_of_the_subject(monkeypatch):
    capture(ZAHRA, "این ربات چقدر خوبه", message_id=10)
    passes = install(monkeypatch, {"relevant": True, "respond": False})
    run_pass(FakeBot())
    context = passes[0]["context"]
    assert "read by the server" in context
    assert "confidence" in context


def test_the_reading_is_persisted_for_the_next_pass(monkeypatch):
    capture(ZAHRA, "این ربات چقدر خوبه", message_id=10)
    install(monkeypatch, {"relevant": True, "respond": False})
    run_pass(FakeBot())
    stored = db.awareness_get(CHAT)
    assert stored["subject_kind"] == "about"
    assert stored["subject_confidence"] >= 60
    assert stored["subject_message_id"] == 10


def test_a_reply_to_nexus_is_read_as_the_subject(monkeypatch):
    capture(BOT_ID, "اینو بزن", message_id=10, role=awareness.ROLE_NEXUS)
    capture(ZAHRA, "ممنون از جوابت", message_id=11,
            reply_user_id=BOT_ID, reply_name="Nexus")
    install(monkeypatch, {"relevant": True, "respond": False})
    run_pass(FakeBot())
    assert db.awareness_get(CHAT)["subject_kind"] == "implicit"


# ══════════════════════════════════════════════════════════════════════════
# Participation
# ══════════════════════════════════════════════════════════════════════════
def test_a_room_talking_about_nexus_gets_a_reply_under_the_turn(monkeypatch):
    capture(ZAHRA, "این ربات چقدر خوبه", message_id=10)
    capture(ALI, "آره واقعاً", message_id=11)
    install(monkeypatch, {
        "relevant": True, "respond": True, "message": "ممنون 😄",
        "subject": "nexus", "participation": 85,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent, "Nexus joined a conversation that was about it"
    assert bot.sent[0]["text"] == "ممنون 😄"
    # The conversational turn it is answering, not an older message.
    assert bot.sent[0]["reply_to_message_id"] == 11


def test_a_general_discussion_about_ai_does_not_make_it_speak(monkeypatch):
    capture(ZAHRA, "ربات‌های تلگرام چطور کار می‌کنند؟", message_id=10)
    install(monkeypatch, {
        "relevant": True, "respond": True, "message": "بذار توضیح بدم",
        "subject": "general", "participation": 20,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent == [], "a general discussion is not an invitation"


def test_a_weak_claim_stays_silent(monkeypatch):
    capture(ZAHRA, "سلام بچه‌ها", message_id=10)
    install(monkeypatch, {
        "relevant": True, "respond": True, "message": "سلام",
        "subject": "none", "participation": 30,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent == []


def test_the_models_own_confidence_can_carry_a_weak_server_reading(monkeypatch):
    capture(ZAHRA, "فکر کنم اون یکی بهتر بود", message_id=10)
    install(monkeypatch, {
        "relevant": True, "respond": True, "message": "کدوم؟",
        "subject": "nexus", "participation": 90,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent, "a confident model claim is enough on its own"
    # The server had no destination to offer, so it is a plain message.
    assert bot.sent[0]["reply_to_message_id"] is None


def test_a_write_confirmation_is_never_filtered_by_the_floor(monkeypatch):
    """An action that already happened must be acknowledged."""
    capture(OWNER, "اینو بن کن", message_id=10, directed=True,
            role=awareness.ROLE_OWNER)

    async def _turn(ctx, chat_id, actor_id, *, speaker=None, counters=None):
        if counters is not None:
            counters["writes"] = 1
        return None, "", None

    monkeypatch.setattr(main, "_awareness_turn", _turn)
    install(monkeypatch, {
        "relevant": True, "respond": False, "subject": "general", "participation": 0,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent, "the confirmation is sent despite a low participation score"
    assert bot.sent[0]["text"] == config.NEXUS_AWARENESS_ACTION_TEXT


def test_the_duplicate_guard_still_wins(monkeypatch):
    capture(ZAHRA, "این ربات چقدر خوبه", message_id=10)
    install(monkeypatch, {
        "relevant": True, "respond": True, "message": "ممنون",
        "subject": "nexus", "participation": 90,
    })
    main._nexus_addressed[CHAT] = 10
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent == [], "an already-answered batch is not answered twice"


def test_the_destination_is_always_a_message_in_the_window(monkeypatch):
    """The quoted id comes from the stored rows, never from the model."""
    capture(ZAHRA, "این ربات چقدر خوبه", message_id=10)
    capture(ALI, "آره واقعاً", message_id=11)
    install(monkeypatch, {
        "relevant": True, "respond": True, "message": "ممنون",
        "subject": "nexus", "participation": 90,
        # A model that tries to choose an id of its own is ignored: there is no
        # field for it, and the destination is resolved server-side.
        "message_id": 999999,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent[0]["reply_to_message_id"] in (10, 11)


def test_the_model_can_decline_a_conversation_that_is_about_nexus(monkeypatch):
    """The server's reading is evidence; the model's judgement is the decision."""
    capture(ZAHRA, "نکسوس گفت که فردا میاد", message_id=10)
    install(monkeypatch, {
        "relevant": True, "respond": False, "subject": "nexus", "participation": 0,
    })
    bot = FakeBot()
    run_pass(bot)
    assert bot.sent == []


# ══════════════════════════════════════════════════════════════════════════
# The contract: the model's new fields are normalised, never trusted
# ══════════════════════════════════════════════════════════════════════════
def test_the_instruction_asks_for_the_subject_and_the_confidence():
    text = chat.AWARENESS_INSTRUCTION
    assert '"subject"' in text
    assert '"participation"' in text
    for word in awareness.SUBJECTS:
        assert f'"{word}"' in text


def test_the_subject_field_is_clamped_to_the_vocabulary():
    decision = awareness.parse_decision(
        json.dumps({"subject": "NEXUS", "participation": 80})
    )
    assert decision["subject"] == "nexus"
    for bogus in ("a sentence", None, 7, "self"):
        decision = awareness.parse_decision(json.dumps({"subject": bogus}))
        assert decision["subject"] == "none"


def test_the_participation_field_is_clamped_to_a_percentage():
    assert awareness.parse_decision(
        json.dumps({"participation": 150})
    )["participation"] == 100
    assert awareness.parse_decision(
        json.dumps({"participation": -20})
    )["participation"] == 0
    assert awareness.parse_decision(
        json.dumps({"participation": "high"})
    )["participation"] == 0
    assert awareness.parse_decision(
        json.dumps({"participation": 73.9})
    )["participation"] == 73
