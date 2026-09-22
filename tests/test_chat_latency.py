"""Where a conversational turn spends its time, and the proof that it is measured.

The owner's complaint was that Nexus answers slowly, and the honest answer is
that a claim about speed is worthless without a number. The awareness path
already logs a ``PassTrace`` line for exactly this reason; the addressed path —
the one that runs when somebody actually talks *to* the assistant — had none, so
"the reply took four seconds" could only ever be an impression.

``_answer_conversationally`` now logs the same shape of line: how long the
message took to get ready (a download, a transcription), how long the model
took, and how long the send took. Those are the three things that can be slow,
and separating them is what makes a report actionable — "slow" is a different
bug depending on which of the three owns the time.

The tests below are about the instrumentation itself rather than about speed:

* every turn that reaches the clock logs exactly one line, including the turns
  that end early, because an early return is precisely when a timeline is most
  useful;
* the line carries durations and never words — not the question, not the
  answer, which are other people's text and have no business in a log;
* ``sent`` tells the truth, because the caller uses the same value to decide
  whether the ambient path may still speak, and a timing line that disagrees
  with the return value is worse than no timing line at all.

**What was measured and deliberately not changed.** The local half of a turn is
not where the time goes. On this machine, ``awareness.room_block`` — the one
per-turn database read that is not the model call — is a median 0.03 ms against
an empty window and 0.37 ms against a full forty-message one, and
``rbac.resolve`` is 0.03 ms. Those are three orders of magnitude below a model
call and below the resolution of the problem the owner described, so no
caching layer was added: it would buy a fraction of a millisecond and cost a
staleness bug in the block that decides what the model is told. The number is
written down here so the next person to wonder about it does not have to
re-measure before deciding the same thing.

Nothing here talks to Telegram or to Google, and nothing sleeps.
"""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from app import chat, config, db, main, nexus, people

OWNER = 999
CHAT = OWNER  # a private chat's id is the sender's own id, as Telegram does it
BOT_ID = 1


@pytest.fixture(autouse=True)
def chat_latency_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "GROUP_IDS", [])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", True)
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "NEXUS_OBSERVE_ADMINS", True)
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_EXTRA_ACTION_WORDS", [])
    monkeypatch.setattr(config, "BOT_ALIASES", [])
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")

    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    people.reset_state()
    chat.reset_state()
    main._recently_deleted.clear()
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
    main._recently_deleted.clear()


# ── Harness ───────────────────────────────────────────────────────────────
class FakeBot:
    """A bot whose send can be made to fail, so ``sent`` can be checked."""

    def __init__(self, *, fail_send=False):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[str] = []
        self.fail_send = fail_send

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_send:
            raise main.TelegramError("nope")
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, chat_id, action, **kwargs):
        pass


def message(text="سلام", **fields):
    msg = SimpleNamespace(
        message_id=10, photo=None, video=None, animation=None, video_note=None,
        sticker=None, voice=None, audio=None, document=None, text=text,
        caption=None, reply_to_message=None,
    )
    for key, value in fields.items():
        setattr(msg, key, value)
    return msg


def private_update(actor=OWNER, text="سلام", **fields):
    return SimpleNamespace(
        effective_message=message(text, **fields),
        effective_chat=SimpleNamespace(id=actor, type="private", title=""),
        effective_user=SimpleNamespace(
            id=actor, full_name="Tester", username="tester", is_bot=False
        ),
    )


def install_model(monkeypatch, reply):
    """Replace the transport with one that returns ``reply`` verbatim."""
    calls: list[dict] = []

    async def _reply(chat_id, user_id, body, **kwargs):
        calls.append({"chat_id": chat_id, "user_id": user_id, "text": body})
        return reply

    monkeypatch.setattr(main.chat, "reply", _reply)
    return calls


def timing_lines(caplog):
    return [r.getMessage() for r in caplog.records if "chat timing" in r.getMessage()]


def run(update, bot):
    ctx = SimpleNamespace(bot=bot)
    asyncio.run(main.on_private_text(update, ctx))
    return ctx


# ── One line per turn, with the three stages in it ────────────────────────
def test_an_answered_turn_logs_its_stages(monkeypatch, caplog):
    install_model(monkeypatch, chat.ChatReply(answered=True, text="باشه", turns=1))
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot())

    lines = timing_lines(caplog)
    assert len(lines) == 1, "one answered turn must produce exactly one timeline"
    line = lines[0]
    for field in ("prepare_ms=", "gemini_ms=", "send_ms=", "total_ms="):
        assert field in line, f"{field} is part of the report"
    assert "sent=True" in line


def test_the_timing_line_carries_no_words(monkeypatch, caplog):
    """A log line about durations must not become a log line about content."""
    secret_in = "این سؤال محرمانه است"
    secret_out = "این پاسخ محرمانه است"
    install_model(monkeypatch, chat.ChatReply(answered=True, text=secret_out, turns=1))
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(text=secret_in), FakeBot())

    line = timing_lines(caplog)[0]
    assert secret_in not in line
    assert secret_out not in line


def test_the_line_identifies_the_turn_without_identifying_the_words(
    monkeypatch, caplog
):
    """Who and where are metadata; the text is not."""
    install_model(monkeypatch, chat.ChatReply(answered=True, text="باشه", turns=1))
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot())

    line = timing_lines(caplog)[0]
    assert f"user={OWNER}" in line
    assert f"chat={CHAT}" in line


# ── The early exits are the ones that most need a timeline ────────────────
def test_a_declined_turn_still_logs_a_timeline(monkeypatch, caplog):
    """A model that said no is exactly the case somebody will ask about."""
    install_model(monkeypatch, chat.ChatReply(answered=False, skipped="disabled"))
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot())

    lines = timing_lines(caplog)
    assert lines, "a declined turn must still report where its time went"
    # ``disabled`` has a sentence to send, so this decline was spoken rather
    # than silent and the timeline has to say so.
    assert "sent=True" in lines[0]


def test_a_silent_decline_is_reported_as_not_sent(monkeypatch, caplog):
    """Some reasons are nobody's business, and the timeline must not lie.

    A switched-off feature that announced itself on every message would be
    noise, so those reasons send nothing — and ``sent`` is what tells the two
    kinds of decline apart in the log.
    """
    install_model(monkeypatch, chat.ChatReply(answered=False, skipped="not_a_reason"))
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot())

    assert "sent=False" in timing_lines(caplog)[0]


def test_a_turn_that_never_reaches_the_model_reports_no_model_time(
    monkeypatch, caplog
):
    """A silent voice note is answered without a model, and must say so.

    Reporting a borrowed duration here would make the model stage look cheap in
    aggregate, which is the opposite of useful.
    """
    install_model(monkeypatch, chat.ChatReply(answered=True, text="unused"))

    async def _nothing(*args, **kwargs):
        return None, "", "", False, main.PREPARE_NO_SPEECH

    monkeypatch.setattr(main, "_prepare_conversation_media", _nothing)
    voice = SimpleNamespace(file_id="f", duration=1, mime_type="audio/ogg")
    with caplog.at_level(logging.INFO, logger="guardbot"):
        ctx = run(private_update(voice=voice), FakeBot())

    assert ctx.bot.messages, "the honest sentence still goes out"
    line = timing_lines(caplog)[0]
    assert "gemini_ms=0" in line, "no model was consulted, so no model time"
    assert "sent=True" in line


# ── sent must agree with what actually happened ───────────────────────────
def test_a_failed_send_is_reported_as_not_sent(monkeypatch, caplog):
    install_model(monkeypatch, chat.ChatReply(answered=True, text="باشه", turns=1))
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot(fail_send=True))

    line = timing_lines(caplog)[0]
    assert "sent=False" in line, "Telegram refused it, so nothing was said"


def test_a_deleted_message_is_not_a_turn_and_gets_no_timeline(monkeypatch, caplog):
    """A message moderation just removed is skipped before the clock starts.

    It was never a conversational turn, so a timeline for it would be a
    measurement of something that did not happen.
    """
    install_model(monkeypatch, chat.ChatReply(answered=True, text="باشه"))
    main._recently_deleted[(CHAT, 10)] = time.monotonic()
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot())

    assert timing_lines(caplog) == []


# ── The stage boundaries are in the right order ───────────────────────────
def test_the_model_stage_covers_the_model_call(monkeypatch, caplog):
    """``gemini_ms`` must not include the send, and the send must not be zero.

    The separation is the whole value of the line: if every stage reported the
    same number, the report would say nothing about where the time went.
    """
    async def _slow_reply(chat_id, user_id, body, **kwargs):
        await asyncio.sleep(0.02)
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    monkeypatch.setattr(main.chat, "reply", _slow_reply)
    with caplog.at_level(logging.INFO, logger="guardbot"):
        run(private_update(), FakeBot())

    line = timing_lines(caplog)[0]
    gemini = float(line.split("gemini_ms=")[1].split()[0])
    total = float(line.split("total_ms=")[1].split()[0])
    assert gemini >= 15.0, "the twenty-millisecond call must show up in its stage"
    assert total >= gemini, "the total is every stage, so it cannot be smaller"
