"""The owner's spoken commands, and where they sit in the routing order.

Two halves, and they answer two different questions.

The **vocabulary** is pure: ``commands.action_for`` says what a string asks for,
and it is tested the way ``nexus.command_from`` is tested — as a table of
phrasings, including the ones that must resolve to nothing. This is the half that
must work with no model, no network and no allowance, so it is the half that is
cheapest to test exhaustively.

The **routing** is where the two vocabularies meet, and that is the part with a
real hazard in it. The assistant's own switch and this feature share a verb:
«نکسوس بیا» turns Nexus on, «نکسوس بیا بیرون» leaves a voice call. A router that
read only the verb would silence the assistant when the owner meant to leave a
call — or refuse to start a call because it thought it had been asked to shut
down. So the order is asserted structurally, and the stand-down is asserted by
behaviour.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app import config, db, main, nexus, rbac
from app.voice_live import commands as VC
from app.voice_live import errors as VE
from app.voice_live import session as VS

CHAT = -1001234567890
OWNER = 111
STRANGER = 999
BOT_ID = 424242

#: Captured before any fixture replaces it, so one test can put the real
#: credential lease back and prove what happens when there is nothing to lease.
_REAL_DEFAULT_PROVIDER = VS.VoiceSession._default_provider


# ══ Harness ═══════════════════════════════════════════════════════════════
class FakeBot:
    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, chat_id, action, **kwargs):
        pass


def ctx_for(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def update_for(text, *, actor=OWNER, chat_id=CHAT):
    msg = SimpleNamespace(
        message_id=10, text=text, caption=None, reply_to_message=None,
        photo=None, video=None, animation=None, video_note=None, sticker=None,
        voice=None, audio=None, document=None,
    )
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=actor, full_name="تستر", username="tester", is_bot=False
        ),
    )


class FakeProvider:
    """The provider surface the session uses, with nothing behind it."""

    def __init__(self, handle: str = ""):
        self.model = "fake-live"
        self.handle = str(handle or "")
        self.audio: list[bytes] = []
        self.contexts: list[str] = []
        self.tool_results: list[list[dict]] = []
        self._events: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        await self._events.put(None)

    async def send_audio(self, frame: bytes) -> None:
        self.audio.append(frame)

    async def send_context(self, text: str) -> None:
        self.contexts.append(text)

    async def send_tool_results(self, results) -> None:
        self.tool_results.append(list(results))

    async def receive(self):
        while True:
            item = await self._events.get()
            if item is None:
                return
            yield item


@pytest.fixture(autouse=True)
def voice_route_env(monkeypatch):
    """An opted-in deployment, a fake transport, and no calls in flight."""
    db.init()
    nexus.reset_state()
    VS.reset_state()
    monkeypatch.setattr(config, "GEMINI_LIVE_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_LIVE_TRANSPORT", "fake")
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    # The provider is replaced rather than leased: these tests are about
    # routing, and a real lease would need a credential they do not have.
    monkeypatch.setattr(
        VS.VoiceSession, "_default_provider", lambda self, handle: FakeProvider(handle)
    )
    yield
    VS.reset_state()


async def _arun(bot, text, *, actor=OWNER):
    """Drive the router once, inside the caller's event loop.

    One loop for the whole test rather than one per call, because that is what
    the process has: a session's loops belong to the loop that started them, and
    stopping it from a second loop is not something that happens in production.
    """
    ctx = ctx_for(bot)
    update = update_for(text, actor=actor)
    return await main._owner_voice_command(update, ctx, rbac.resolve(actor), text)


def _run(text, *, actor=OWNER):
    """Drive the router once, for the tests that never start a session."""
    bot = FakeBot()
    handled = asyncio.run(_arun(bot, text, actor=actor))
    return handled, bot.messages, VS.manager().active()


# ══ The vocabulary ════════════════════════════════════════════════════════
def test_the_configured_phrases_resolve_to_their_direction():
    for phrase in config.GEMINI_LIVE_JOIN_PHRASES:
        assert VC.action_for(phrase) == VC.JOIN, phrase
    for phrase in config.GEMINI_LIVE_LEAVE_PHRASES:
        assert VC.action_for(phrase) == VC.LEAVE, phrase


def test_a_phrase_is_matched_inside_a_sentence():
    assert VC.action_for("خب نکسوس برو ویس‌کال ببینم چی می‌گی") == VC.JOIN
    assert VC.action_for("نکسوس بیا بیرون دیگه") == VC.LEAVE


def test_a_contradiction_resolves_to_nothing():
    """Both directions in one message is not a request, and guessing at one half
    of a contradiction is how a call is opened when the owner meant to close
    one."""
    both = (
        config.GEMINI_LIVE_JOIN_PHRASES[0]
        + " و "
        + config.GEMINI_LIVE_LEAVE_PHRASES[0]
    )
    assert VC.action_for(both) == ""
    assert VC.action_for(
        f"نکسوس برو ویس‌کال بعد نکسوس بیا بیرون"
    ) == ""


def test_an_ordinary_message_is_not_a_command():
    for text in ("سلام بچه‌ها", "کی میاد ویس؟", "", "   "):
        assert VC.action_for(text) == "", text


def test_a_negated_command_is_not_a_command():
    """«برو ویس‌کال نکن» contains the join phrase and asks for the opposite of
    joining. Obeying the phrase would open the call the owner just declined."""
    assert VC.action_for("نکسوس برو ویس‌کال نکن") == ""
    assert VC.action_for("نکسوس بیا بیرون، الان نه") == ""
    assert nexus.negated("نکسوس برو ویس‌کال نکن") is True


def test_a_phrase_matches_whole_words_only():
    """The reason the matcher is not a substring search: these are short Persian
    words and they live inside longer ones."""
    # «ویس» alone is a *name* for the layer, not a direction, and a phrase
    # containing it must not fire on a word that merely starts with it.
    assert VC.action_for("ویسکالمون قطع شد") == ""


def test_the_layer_can_be_named():
    assert VC.names_layer("برو ویس‌کال") is True
    assert VC.names_layer("voice live") is True
    assert VC.names_layer("سلام بچه‌ها") is False


# ══ Where the router sits ═════════════════════════════════════════════════
def test_the_voice_router_runs_before_anything_conversational():
    """The requirement, asserted against the source because that is what the
    claim is about: these commands must be decided before a model is consulted,
    and before the assistant's own switch is read."""
    source = inspect.getsource(main.on_group_chat)
    voice = source.index("_owner_voice_command")
    switch = source.index("_owner_state_command")
    answer = source.index("_answer_conversationally")
    assert voice < switch
    assert voice < answer


def test_a_switch_phrase_is_left_to_the_switch_router():
    """The shared verb. «نکسوس بیا» turns the assistant on and must never be
    read as a voice command; «نکسوس بیا بیرون» leaves a call and must never be
    read as the assistant's switch."""
    assert main._owner_voice_command is not None  # the function under test
    for switch_phrase in ("نکسوس بیا", "نکسوس خاموش شو", "نکسوس روشن شو"):
        handled, replies, active = _run(switch_phrase)
        assert handled is False, switch_phrase
        assert replies == []
        assert active == []
    # ...and the leave phrase is not claimed by the switch vocabulary.
    assert nexus.command_from("نکسوس بیا بیرون", names_layer=True) is None
    assert VC.action_for("نکسوس بیا بیرون") == VC.LEAVE


def test_a_non_owner_cannot_bring_nexus_into_a_call():
    """Resolved from the Telegram id, never from what the sender wrote about
    themselves."""
    handled, replies, active = _run("نکسوس برو ویس‌کال", actor=STRANGER)
    assert handled is False
    assert replies == []
    assert active == []


def test_a_contradictory_command_is_not_handled():
    both = (
        config.GEMINI_LIVE_JOIN_PHRASES[0]
        + "، بعد "
        + config.GEMINI_LIVE_LEAVE_PHRASES[0]
    )
    handled, replies, active = _run(both)
    assert handled is False
    assert replies == []
    assert active == []


# ══ Joining ═══════════════════════════════════════════════════════════════
def test_the_owner_can_bring_nexus_into_a_call():
    async def scenario():
        bot = FakeBot()
        handled = await _arun(bot, "نکسوس برو ویس‌کال")
        active = VS.manager().active()
        await VS.manager().stop_all()
        return handled, bot.messages, active

    handled, replies, active = asyncio.run(scenario())
    assert handled is True
    assert replies == [config.GEMINI_LIVE_JOINED_TEXT]
    assert active == [CHAT]


def test_a_second_join_in_the_same_group_says_it_is_busy():
    """Two calls in one room is two Nexus in one room."""
    async def scenario():
        bot = FakeBot()
        await _arun(bot, "نکسوس برو ویس‌کال")
        handled = await _arun(bot, "نکسوس برو ویس‌کال")
        active = VS.manager().active()
        await VS.manager().stop_all()
        return handled, bot.messages, active

    handled, replies, active = asyncio.run(scenario())
    assert handled is True
    assert replies == [config.GEMINI_LIVE_JOINED_TEXT, config.GEMINI_LIVE_BUSY_TEXT]
    assert active == [CHAT]


def test_a_join_with_no_credential_is_reported_rather_than_claimed(monkeypatch):
    """The pool is consulted, and a call that cannot be opened is not reported
    as opened. This is the fail-closed direction: a session with no model would
    sit in the voice chat answering nobody."""
    # Put the real lease back, so the pool is actually asked and actually has
    # nothing to offer.
    monkeypatch.setattr(
        VS.VoiceSession, "_default_provider", _REAL_DEFAULT_PROVIDER
    )
    handled, replies, active = _run("نکسوس برو ویس‌کال")
    assert handled is True
    assert replies == [config.GEMINI_LIVE_FAILED_TEXT]
    assert active == []


def test_the_feature_being_off_is_said_plainly(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_LIVE_ENABLED", False)
    handled, replies, active = _run("نکسوس برو ویس‌کال")
    assert handled is True
    assert replies == [config.GEMINI_LIVE_OFF_TEXT]
    assert active == []


def test_the_assistant_being_off_is_a_different_sentence(monkeypatch):
    """A call *is* the assistant, so a call cannot start while the assistant has
    been told to stop — and the sentence must name the switch that is in the way,
    because the owner's next action is different for each."""
    monkeypatch.setattr(nexus, "_state", nexus.OFFLINE)
    handled, replies, active = _run("نکسوس برو ویس‌کال")
    assert handled is True
    assert replies == [config.GEMINI_LIVE_NEXUS_OFF_TEXT]
    assert active == []


def test_a_double_is_not_a_way_to_hold_a_production_call(monkeypatch):
    """A fake transport joins nothing, and a bot that reported a call it is not
    in would be worse than any failure the double prevents."""
    monkeypatch.setattr(VS, "_testing", lambda: False)
    handled, replies, active = _run("نکسوس برو ویس‌کال")
    assert handled is True
    assert replies == [config.GEMINI_LIVE_UNAVAILABLE_TEXT]
    assert active == []


# ══ Leaving ═══════════════════════════════════════════════════════════════
def test_the_owner_can_take_nexus_out_of_a_call():
    async def scenario():
        bot = FakeBot()
        await _arun(bot, "نکسوس برو ویس‌کال")
        handled = await _arun(bot, "نکسوس بیا بیرون")
        return handled, bot.messages, VS.manager().active()

    handled, replies, active = asyncio.run(scenario())
    assert handled is True
    assert replies == [config.GEMINI_LIVE_JOINED_TEXT, config.GEMINI_LIVE_LEFT_TEXT]
    assert active == []


def test_leaving_when_there_is_no_call_says_so():
    handled, replies, active = _run("نکسوس بیا بیرون")
    assert handled is True
    assert replies == [config.GEMINI_LIVE_NOT_IN_CALL_TEXT]
    assert active == []


def test_a_leave_is_not_answered_with_the_switch_vocabulary():
    """«بیا بیرون» is not an on/off phrase, and the sentence proves which router
    answered."""
    async def scenario():
        bot = FakeBot()
        await _arun(bot, "نکسوس برو ویس‌کال")
        await _arun(bot, "نکسوس بیا بیرون")
        return bot.messages

    replies = asyncio.run(scenario())
    assert config.NEXUS_OFFLINE_DONE_TEXT not in replies
    assert replies[-1] == config.GEMINI_LIVE_LEFT_TEXT


# ══ The manager's gate is the only gate ═══════════════════════════════════
def test_every_refusal_sentence_comes_from_a_manager_reason():
    """The sentence is chosen from the gate's reason, so the four ways a call
    cannot start cannot drift from the four sentences."""
    assert main._voice_refusal_text(VE.REASON_DISABLED) in (
        config.GEMINI_LIVE_OFF_TEXT,
        config.GEMINI_LIVE_NEXUS_OFF_TEXT,
    )
    assert main._voice_refusal_text(VE.REASON_BUSY) == config.GEMINI_LIVE_BUSY_TEXT
    assert main._voice_refusal_text(VE.REASON_TRANSPORT_UNAVAILABLE) == (
        config.GEMINI_LIVE_UNAVAILABLE_TEXT
    )
    # An unknown reason is reported as "not available here" rather than as
    # silence, which is the safe direction: the owner learns the call did not
    # start.
    assert main._voice_refusal_text("something_new") == (
        config.GEMINI_LIVE_UNAVAILABLE_TEXT
    )


def test_a_failed_join_leaves_no_session_registered(monkeypatch):
    """The manager inserts before starting and takes the entry back out when the
    start raises, so a failed join does not lock the room out of the next one."""
    async def _boom(self, handle=""):
        raise VE.QuotaExhausted("no credential")

    monkeypatch.setattr(VS.VoiceSession, "_open_provider", _boom)
    handled, replies, active = _run("نکسوس برو ویس‌کال")
    assert handled is True
    assert replies == [config.GEMINI_LIVE_FAILED_TEXT]
    assert active == []


# ══ The status line ═══════════════════════════════════════════════════════
def test_the_status_line_reports_the_flag_and_the_counts():
    """A machine-key line, like the awareness one: an operator answers "why did
    it not join" from it without reading a log."""
    line = VS.status_line()
    assert line.startswith("voice[on]:")
    assert f"transport={config.GEMINI_LIVE_TRANSPORT}" in line
    assert f"model={config.GEMINI_LIVE_MODEL}" in line
    assert "calls=-" in line
    assert "AIza" not in line and "AQ." not in line


def test_the_status_line_says_off_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_LIVE_ENABLED", False)
    assert VS.status_line().startswith("voice[off]:")


def test_the_status_line_names_the_calls_that_are_up():
    async def scenario():
        bot = FakeBot()
        await _arun(bot, "نکسوس برو ویس‌کال")
        line = VS.status_line()
        await VS.manager().stop_all()
        return line

    line = asyncio.run(scenario())
    assert f"calls={CHAT}" in line
