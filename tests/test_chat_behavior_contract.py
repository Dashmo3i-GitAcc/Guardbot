"""The Chat behavioural contract: one persona, driven by the conversation.

The reference is the historical Chat at commit ``3243067``. The rebuild keeps
that character — warm, informal, short, reactive — and folds the Nexus-era
amendments (a separate owner tone layer, a separate joke section, repetition
micro-rules) into a **single** persona, so no two instructions compete over the
same decision.

These tests assert the contract, not a generated sentence. Where a behaviour
cannot be checked without a live model they pin the *prompt* (the prompt is the
behaviour) and, where it can, they drive the **real** path
(``main._answer_conversationally`` / ``chat.reply``) with only the network seam
replaced — so the assertion is about the request the running code actually
builds, not a helper the test called directly.

The failure this file exists to prevent, in the owner's words: a reply to
«نخند حرومزاده» that came back as «چشم قربون‌سربازیت😂 بی‌خیال بابا». The
regression below pins the *mechanism* (servile address, laughter as punctuation,
canned filler, no reaction to the actual message), never a canned replacement.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import chat, config, db, groups, main, rbac, web_search

OWNER = 999
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999


@pytest.fixture(autouse=True)
def layer(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "chat-key-not-a-real-one")
    monkeypatch.setattr(config, "GEMINI_CHAT_MODEL", "test-chat-model")
    monkeypatch.setattr(config, "GEMINI_CHAT_HISTORY_TURNS", 8)
    monkeypatch.setattr(config, "GEMINI_CHAT_HISTORY_TTL", 1800)
    monkeypatch.setattr(config, "GEMINI_CHAT_RATE_LIMIT", 100)
    monkeypatch.setattr(config, "GEMINI_CHAT_RATE_WINDOW", 60.0)
    monkeypatch.setattr(config, "GEMINI_CHAT_DAILY_LIMIT", 100)
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_CHARS", 1500)
    monkeypatch.setattr(config, "GEMINI_CHAT_REPLY_CHARS", 3500)
    monkeypatch.setattr("app.gemini_pool._pools", {})

    db.init()
    db.authorized_groups_reset()
    groups.reset_state()
    chat.reset_state()
    yield
    db.authorized_groups_reset()
    groups.reset_state()
    chat.reset_state()


# ── Harnesses ─────────────────────────────────────────────────────────────
def _update(chat_id, user_id, text="سلام"):
    msg = SimpleNamespace(
        message_id=10, photo=None, video=None, animation=None, video_note=None,
        sticker=None, voice=None, audio=None, document=None, text=text,
        caption=None, reply_to_message=None,
    )
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=user_id, full_name="Tester", username="tester", is_bot=False
        ),
    )


def _ctx():
    class _Bot:
        id = 1
        username = "guardbot"

        async def send_message(self, chat_id, text, **kwargs):
            return SimpleNamespace(message_id=1)

        async def send_chat_action(self, *a, **k):
            pass

    bot = _Bot()
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def _context_for(monkeypatch, *, user_id=MEMBER, text="سلام"):
    """The trusted context the real conversational path hands to the model."""
    seen: list[str] = []

    async def _reply(chat_id, user_id_, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        seen.append(context)
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    async def _no_search(question, *, history="", now=0.0):
        return web_search.Finding(ok=False, text="", sources=())

    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    monkeypatch.setattr(main.web_search, "research", _no_search)

    asyncio.run(main._answer_conversationally(_update(CHAT, user_id, text), _ctx()))
    return seen[0]


class _Recorder:
    def __init__(self, *responses):
        self.responses = list(responses) or ["باشه."]
        self.calls: list[list] = []

    async def __call__(self, contents, *, context="", instruction=""):
        self.calls.append(contents)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _recorder(monkeypatch, *responses) -> _Recorder:
    recorder = _Recorder(*responses)
    monkeypatch.setattr(chat, "_request", recorder)
    return recorder


def _persona() -> str:
    return chat.SYSTEM_INSTRUCTION


# ── One coherent personality ──────────────────────────────────────────────
def test_there_is_exactly_one_personality_source():
    """No stacking: the conversation path appends data, never a second persona."""
    from google.genai import types

    text = _persona()
    assert text.count("You are Nexus") == 1
    plain = chat._generation_config(types, context="")
    assert plain.system_instruction == text
    # A tool turn adds the capability amendment — and it is not a personality.
    with_tools = chat._generation_config(types, context="", tools=[object()])
    assert with_tools.system_instruction == text + chat.TOOL_AMENDMENT
    assert "tone" not in chat.TOOL_AMENDMENT.lower()
    assert "personality" not in chat.TOOL_AMENDMENT.lower()


def test_owner_and_member_share_the_same_persona(monkeypatch):
    """The owner gets a data note, not a different personality."""
    from google.genai import types

    owner_ctx = _context_for(monkeypatch, user_id=OWNER)
    member_ctx = _context_for(monkeypatch, user_id=MEMBER)
    assert owner_ctx.startswith(chat.OWNER_NOTE)
    assert chat.OWNER_NOTE not in member_ctx
    # The persona underneath is identical for both, and the context is closed by
    # the same frame for both — it restates the persona's own background rule at
    # the end of the instruction, where it is read.
    owner = chat._generation_config(types, context=owner_ctx).system_instruction
    member = chat._generation_config(types, context=member_ctx).system_instruction
    assert owner == _persona() + owner_ctx + chat.CONTEXT_FRAME
    assert member == _persona() + member_ctx + chat.CONTEXT_FRAME


def test_the_trusted_context_carries_no_personality_directive(monkeypatch):
    ctx = _context_for(monkeypatch, user_id=MEMBER)
    assert _persona() not in ctx
    for directive in ("Never use titles", "laughter as punctuation", "tease back"):
        assert directive not in ctx


# ── Natural, context-driven conversation ──────────────────────────────────
def test_normal_conversation_is_a_normal_answer():
    text = _persona()
    assert "A normal question gets a normal answer" in text
    assert "not an essay" in text
    assert "Two or three sentences is usually right" in text


def test_informal_persian_is_the_default_without_forced_slang():
    text = _persona()
    assert "everyday, informal and direct" in text
    # Slang is conditional on the exchange, never the default register.
    assert "that is what the exchange is doing" in text


def test_friendly_conversation_lets_the_person_set_the_register():
    text = _persona()
    assert "Let them set the register" in text
    assert "casual when they are casual" in text


def test_serious_conversation_is_answered_seriously():
    text = _persona()
    assert "a serious message gets a serious one" in text
    assert "drop the joking entirely and answer normally" in text


def test_context_controls_tone_not_a_persona_performance():
    text = _persona()
    assert "Reply to what the person is actually doing" in text
    assert "do not perform warmth, humour or intimacy" in text
    assert "the moment did not ask for" in text


def test_history_continuity_reaches_the_model(monkeypatch):
    recorder = _recorder(monkeypatch, "باشه.", "خوبه.")
    asyncio.run(chat.reply(CHAT, MEMBER, "من درباره پایتون پرسیدم"))
    asyncio.run(chat.reply(CHAT, MEMBER, "پس جوابش چی بود؟"))
    second = recorder.calls[-1]
    assert [turn["role"] for turn in second] == ["user", "model", "user"]
    assert "پایتون" in second[0]["parts"][0]["text"]


# ── Humour and register are reactive ──────────────────────────────────────
def test_humour_is_reactive_not_automatic():
    text = _persona()
    assert "tease back" in text
    assert "because the moment calls for it" in text
    assert "not to sound human" in text
    assert "never fall into the same joke shape twice" in text


def test_contextual_slang_and_profanity_are_mirrored_not_initiated():
    text = _persona()
    assert "casual — even crude — Persian" in text
    assert "that is what the exchange is doing" in text
    # Mirroring is allowed; introducing it is not.
    assert "not to sound human" in text


def test_no_automatic_laughter_or_emoji():
    text = _persona()
    assert "never use laughter as punctuation" in text
    for mark in ("😂", "🤣", "خخخ", "ههه"):
        assert mark in text, "the marks are named only so they can be forbidden"


def test_no_canned_filler_or_forced_affection():
    text = _persona()
    for filler in ("بابا", "داداش", "قربونت"):
        assert filler in text
    assert "canned" in text
    assert "do not perform warmth, humour or intimacy" in text


def test_no_honorifics_for_anyone():
    text = _persona()
    for word in ("قربان", "سرور", "جناب"):
        assert word in text
    assert "Never use titles or servile address" in text
    assert "قربون‌سربازیت" in text
    # And the owner note carries no such address of its own.
    assert "قربان" not in chat.OWNER_NOTE


def test_no_forced_question_or_closing_invitation():
    text = _persona()
    assert "Do not ask a question just to keep the chat going" in text
    assert "do not close by offering more help" in text


def test_adult_joking_is_contextual_and_reactive():
    text = _persona()
    assert "adult or sexual joke" in text
    assert "you may answer in kind" in text
    assert "never bring that register into a conversation that was not already there" in text
    assert "never escalate an ordinary message into it" in text


# ── The failure-class regression ──────────────────────────────────────────
def test_the_failure_class_is_fixed_at_the_mechanism_not_the_sentence():
    """«نخند حرومزاده» must never come back as the old shape.

    The old reply was «چشم قربون‌سربازیت😂 بی‌خیال بابا». The test pins each
    mechanism that produced it — servile address, automatic laughter, canned
    filler, and no reaction to the actual message — and asserts the persona
    teaches no canned line. It deliberately does not pin a replacement sentence.
    """
    text = _persona()
    # 1. servile address is banned, for everyone.
    assert "قربون‌سربازیت" in text
    assert "Never use titles or servile address" in text
    # 2. laughter is not punctuation.
    assert "never use laughter as punctuation" in text
    # 3. the canned fillers are named and forbidden.
    for filler in ("بابا", "داداش", "قربونت"):
        assert filler in text
    # 4. react to the message; do not perform warmth.
    assert "Reply to what the person is actually doing" in text
    assert "do not perform warmth" in text
    # 5. no canned example that teaches the bad shape.
    for taught in ("خودتی", "کسخل", "مشنگ"):
        assert taught not in text
    # 6. a family insult is never mirrored back.
    assert "never attack anyone's family" in text


# ── The modern architecture is untouched ──────────────────────────────────
def test_an_unregistered_room_never_reaches_the_model(monkeypatch):
    """The room boundary still runs before any Chat work."""
    calls: list[int] = []

    async def _reply(*a, **k):
        calls.append(1)
        return chat.ChatReply(answered=True, text="x", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    asyncio.run(main.on_group_chat(_update(OTHER_CHAT, MEMBER, "نکسوس سلام"), _ctx()))
    assert calls == [], "an unregistered room must not reach the model"


def test_a_chat_turn_does_not_touch_the_acquisition_counters(monkeypatch):
    """AI isolation: Chat and the classifier still share no budget."""
    _recorder(monkeypatch, "باشه.")
    asyncio.run(chat.reply(CHAT, MEMBER, "سلام"))
    assert db.ai_usage()["calls"] == 0, "chat spent the classifier's quota"
    assert db.chat_usage()["calls"] == 1


def test_ownership_is_still_decided_by_id(monkeypatch):
    assert rbac.is_owner(OWNER) is True
    assert rbac.is_owner(MEMBER) is False
    assert chat.OWNER_NOTE not in _context_for(monkeypatch, user_id=MEMBER)
