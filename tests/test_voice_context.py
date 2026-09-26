"""Voice Context: the switch, the spoken turn, and the path through chat.

What this file is proving, in the owner's own terms:

* **The same Nexus, not a second voice bot.** A voice note is understood first —
  downloaded, transcribed, identity and reply resolved, memory/awareness/state
  composed — and the *identical* context a text message would be given is what
  the spoken turn receives. The test below asserts that equality directly rather
  than trusting the wiring.
* **A real switch.** On by default, persisted, owner-only, and off means the
  exact old path: transcribed and answered as text, with no Live session.
* **A real spoken reply.** The answer comes back as a Telegram voice message
  that replies to the incoming voice note, and every failure falls back to text
  rather than going silent.
* **A bounded, isolated turn.** The provider session is opened, fed, read and
  closed within one turn; it is retried only when a retry could help; it is
  capped; and it never leaves a session open.

Nothing here talks to Telegram or to the provider: ``chat.reply``, the search
transport and the Live transport are all replaced, so "did a request happen" and
"what was the model told" are exact.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import (
    admin_service,
    awareness,
    chat,
    config,
    db,
    groups,
    main,
    nexus,
    rbac,
    voice_context,
    web_search,
)
from app.voice_live import errors as voice_errors
from app.voice_live import gemini_live

OWNER = 999
ADMIN = 556
MEMBER = 42
OTHER = 77
CHAT = -1001234567890
BOT_ID = 1
MESSAGE_ID = 500


# ── Harness ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def voice_env(monkeypatch, tmp_path):
    """A deployment with an owner, an administrator, and the layer switched on."""
    # The conversational path builds a work directory for the attachment under
    # ``config.TMP_DIR``. It exists in the container; point it at a per-test
    # directory so a test that reaches the media branch does not depend on a
    # directory on the host.
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setattr(config, "TMP_DIR", str(work))
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin"])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", True)
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(
        config,
        "NEXUS_AWARENESS_NAMES",
        ["awareness", "اورنس", "آگاهی", "اگاهی"],
    )
    monkeypatch.setattr(
        config, "NEXUS_SEARCH_NAMES", ["search", "سرچ", "جستجو"]
    )
    monkeypatch.setattr(
        config,
        "VOICE_CONTEXT_NAMES",
        ["voice context", "voice-context", "ویس کانتکست", "کانتکست صوتی"],
    )
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "test-awareness-key")
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", True)
    # Search off, so a voice note never reaches the web in these tests.
    monkeypatch.setattr(config, "GEMINI_SEARCH_ENABLED", False)
    monkeypatch.setattr(config, "VOICE_CONTEXT_ENABLED", True)
    monkeypatch.setattr(config, "VOICE_CONTEXT_SEND_AUDIO", True)
    monkeypatch.setattr(config, "VOICE_CONTEXT_MAX_ATTEMPTS", 1)
    monkeypatch.setattr("app.gemini_pool._pools", {})

    db.init()
    db.admin_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    db.search_control_reset()
    db.voice_context_control_reset()
    db.authorized_groups_reset()
    db.seen_updates_reset()
    nexus.reset_state()
    awareness.reset_switch()
    web_search.reset_state()
    voice_context.reset_state()
    chat.reset_state()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._bot_rights_cache.clear()
    main._nexus_addressed.clear()
    main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )
    yield
    db.admin_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    db.search_control_reset()
    db.voice_context_control_reset()
    nexus.reset_state()
    awareness.reset_switch()
    web_search.reset_state()
    voice_context.reset_state()


# ── The provider seam ─────────────────────────────────────────────────────
class StubTransport:
    """One provider session, in memory.

    The same five methods ``GeminiLiveTransport`` exposes, so the whole turn can
    be exercised with no network: connect, send audio, send a context block,
    receive events, close. Each attempt gets a fresh one, which is what a retry
    needs and what lets a test count how many sessions were opened.
    """

    def __init__(
        self,
        *,
        audio=b"",
        said="",
        heard="",
        connect_exc=None,
        context_turn_complete=None,
    ):
        self.audio = audio
        self.said = said
        self.heard = heard
        self.connect_exc = connect_exc
        self.sent: list[bytes] = []
        self.contexts: list[tuple[str, bool]] = []
        self.closed = False
        self.connected = False
        self.turn_complete_seen = None

    async def connect(self):
        if self.connect_exc:
            raise self.connect_exc
        self.connected = True

    async def close(self):
        self.closed = True

    async def send_audio(self, pcm):
        self.sent.append(pcm)

    async def send_context(self, text, *, turn_complete=False):
        self.contexts.append((text, turn_complete))
        self.turn_complete_seen = turn_complete

    async def receive(self):
        if self.audio:
            yield gemini_live.LiveEvent(gemini_live.AUDIO, audio=self.audio)
        if self.said:
            yield gemini_live.LiveEvent(gemini_live.TRANSCRIPT_OUT, text=self.said)
        if self.heard:
            yield gemini_live.LiveEvent(gemini_live.TRANSCRIPT_IN, text=self.heard)
        yield gemini_live.LiveEvent(gemini_live.TURN_COMPLETE)


@pytest.fixture
def live_seam(monkeypatch):
    """Replace the credential lease, the transport and both codecs.

    The codecs are ffmpeg subprocesses and the host test venv has no ffmpeg; the
    real ones were verified in the container. What is under test here is the
    wiring and the turn's own logic, not the resampler.
    """

    def install(stubs, *, lease=("test-live-model", "test-key"), decode=None):
        made: list[StubTransport] = []

        def _lease():
            if isinstance(lease, Exception):
                raise lease
            return lease

        def _factory(model, key):
            def build():
                # One fresh session per call, exactly as the production factory
                # opens a new provider session for a retry. The index advances
                # here rather than when the factory was built, so the second
                # attempt gets the second stub.
                index = len(made)
                stub = stubs[min(index, len(stubs) - 1)]
                made.append(stub)
                return stub

            return build

        monkeypatch.setattr(voice_context, "available", lambda: True)
        monkeypatch.setattr(voice_context, "_lease", _lease)
        monkeypatch.setattr(voice_context, "_transport_factory", _factory)
        monkeypatch.setattr(
            voice_context.voice_audio,
            "decode_to_pcm16",
            decode or (lambda data, **kw: b"\x00" * 640),
        )
        monkeypatch.setattr(
            voice_context.voice_audio, "encode_pcm24_to_ogg", lambda pcm, **kw: b"OGG"
        )
        return made

    return install


def run_answer(**kwargs):
    return voice_context.answer(**kwargs)


# ══ 1. The switch: persisted, owner-only, and off means the old path ═══════
def test_never_touched_means_on():
    assert db.voice_context_control_get() is None
    assert voice_context.running() is True
    assert voice_context.enabled() is True


def test_the_switch_survives_a_restart():
    voice_context.set_running(False, actor_id=OWNER, reason="test")
    voice_context.reset_switch()  # the restart: the cache is dropped

    assert voice_context.running() is False
    assert voice_context.enabled() is False


def test_on_survives_a_restart_too():
    voice_context.set_running(True, actor_id=OWNER, reason="test")
    voice_context.reset_switch()

    assert voice_context.running() is True


def test_config_off_wins_over_a_stored_on(monkeypatch):
    voice_context.set_running(True, actor_id=OWNER, reason="test")
    monkeypatch.setattr(config, "VOICE_CONTEXT_ENABLED", False)

    assert voice_context.configured() is False
    assert voice_context.running() is True
    assert voice_context.enabled() is False


def test_available_needs_a_credential(monkeypatch):
    """A deployment with no live credential is inert, not broken."""
    assert voice_context.enabled() is True
    assert voice_context.available() is False, "no pool account is configured"


def test_available_is_true_with_a_pool_account(monkeypatch):
    from app import gemini_pool

    pool = gemini_pool.Pool(
        voice_context.WORKLOAD,
        [("1", "test-live-key")],
        ["test-live-model"],
        frozenset({"audio_in", "audio_out", "live"}),
        allow_experimental=True,
        daily_budget=10,
    )
    monkeypatch.setattr(
        voice_context.gemini_pool, "pool_for", lambda name: pool
    )
    monkeypatch.setattr(voice_context.gemini_pool, "has_accounts", lambda name: True)

    assert voice_context.available() is True


# ══ 2. The commands: the layer is named, and only this switch moves ════════
def test_the_layer_is_named_only_by_its_own_words():
    assert voice_context.named("ویس کانتکست رو باز کن") is True
    assert voice_context.named("voice context off") is True
    assert voice_context.named("کانتکست صوتی") is True
    assert voice_context.named("نکسوس خاموش") is False
    assert voice_context.named("ویس کال") is False, "the call owns «voice»"


def test_the_direction_is_read_from_the_layer_vocabulary():
    assert voice_context.command_from("ویس کانتکست رو باز کن") == voice_context.ON
    assert voice_context.command_from("ویس کانتکست رو ببند") == voice_context.OFF
    assert voice_context.command_from("voice context off") == voice_context.OFF
    # The shared switch words move it because the layer is named.
    assert voice_context.command_from("ویس کانتکست روشن") == voice_context.ON
    assert voice_context.command_from("ویس کانتکست خاموش") == voice_context.OFF


def test_a_negation_cancels_the_command():
    assert voice_context.command_from("ویس کانتکست رو باز نکن") is None
    assert voice_context.command_from("ویس کانتکست رو روشن نکن") is None


def test_a_contradiction_resolves_to_nothing():
    assert voice_context.command_from("ویس کانتکست رو باز کن و ببند") is None


def test_an_ordinary_message_is_not_a_command():
    assert voice_context.command_from("سلام خوبی") is None


def test_the_owner_can_turn_it_off_out_loud():
    bot = FakeBot()
    run_group(main.on_group_chat, voice_msg(), bot, actor=OWNER, text="ویس کانتکست خاموش")

    assert voice_context.running() is False, "the switch was not moved"
    assert nexus.is_online() is True, "the assistant was silenced instead"
    assert awareness.running() is True, "the awareness layer was moved instead"
    assert web_search.running() is True, "search was moved instead"
    assert bot.messages and bot.messages[-1] == config.VOICE_CONTEXT_OFF_DONE_TEXT


def test_the_owner_can_turn_it_back_on():
    voice_context.set_running(False, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run_group(main.on_group_chat, voice_msg(), bot, actor=OWNER, text="ویس کانتکست باز")

    assert voice_context.running() is True
    assert bot.messages and bot.messages[-1] == config.VOICE_CONTEXT_ON_DONE_TEXT


def test_a_member_cannot_move_the_switch():
    bot = FakeBot()
    run_group(main.on_group_chat, voice_msg(), bot, actor=MEMBER, text="ویس کانتکست خاموش")

    assert voice_context.running() is True
    assert bot.messages == [], "being ignored is not announced"


def test_an_administrator_cannot_move_the_switch():
    run_group(main.on_group_chat, voice_msg(), FakeBot(), actor=ADMIN,
              text="ویس کانتکست خاموش")

    assert voice_context.running() is True


def test_a_no_op_reports_the_state_rather_than_changing_it():
    bot = FakeBot()
    run_group(main.on_group_chat, voice_msg(), bot, actor=OWNER, text="ویس کانتکست روشن")

    assert voice_context.running() is True
    assert bot.messages
    assert config.VOICE_CONTEXT_ON_LABEL in bot.messages[-1]


def test_the_config_off_sentence_is_said_when_the_deployment_is_off(monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTEXT_ENABLED", False)
    voice_context.set_running(False, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run_group(main.on_group_chat, voice_msg(), bot, actor=OWNER, text="ویس کانتکست روشن")

    assert bot.messages and bot.messages[-1] == config.VOICE_CONTEXT_CONFIG_OFF_TEXT


# ══ 3. The turn: opened, fed, read, closed — and never left open ══════════
def test_a_voice_turn_returns_spoken_audio(live_seam):
    stub = StubTransport(audio=b"\x00" * 960, said="سلام، خوبم")
    live_seam([stub])

    answer = asyncio.run(
        run_answer(context="CTX", transcript="حالت چطوره؟", audio=b"OGG-IN")
    )

    assert answer.ok is True
    assert answer.voice == b"OGG"
    assert answer.said == "سلام، خوبم"
    assert stub.closed is True, "the session must be closed after the turn"
    assert stub.sent, "the utterance must reach the session"


def test_the_server_context_and_the_transcript_reach_the_session(live_seam):
    stub = StubTransport(audio=b"\x00" * 960, said="باشه")
    live_seam([stub])

    asyncio.run(
        run_answer(context="SERVER-RECORD", transcript="متن گفتار", audio=b"OGG-IN")
    )

    assert len(stub.contexts) == 1
    block, turn_complete = stub.contexts[0]
    assert "SERVER-RECORD" in block
    assert "متن گفتار" in block
    assert "server's own record" in block, "the block is framed as data, not orders"
    assert turn_complete is False, "the audio completes the turn, not the block"


def test_the_audio_being_sent_does_not_stop_the_block_arriving(live_seam):
    """The block carries the server's reading of the very audio that follows."""
    stub = StubTransport(audio=b"\x00" * 960)
    live_seam([stub])

    asyncio.run(run_answer(context="C", transcript="t", audio=b"OGG-IN"))

    assert stub.contexts and stub.contexts[0][1] is False


def test_a_text_only_turn_closes_the_block_itself(live_seam, monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTEXT_SEND_AUDIO", False)
    stub = StubTransport(audio=b"\x00" * 960, said="جواب")
    live_seam([stub])

    answer = asyncio.run(run_answer(context="C", transcript="سؤال", audio=b"OGG-IN"))

    assert answer.ok is True
    assert stub.sent == [], "no audio may be sent when the audio path is off"
    assert stub.contexts and stub.contexts[0][1] is True, "the block is the message"


def test_an_empty_reply_is_retried_then_reported(live_seam, monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTEXT_MAX_ATTEMPTS", 2)
    first = StubTransport()  # no audio
    second = StubTransport()  # no audio again
    made = live_seam([first, second])

    answer = asyncio.run(run_answer(context="C", transcript="t", audio=b"OGG-IN"))

    assert answer.ok is False
    assert answer.reason == "empty_reply"
    assert answer.attempts == 2
    assert len(made) == 2, "each attempt opens its own session"
    assert first.closed and second.closed


def test_a_rejected_setup_is_not_retried(live_seam, monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTEXT_MAX_ATTEMPTS", 3)
    stub = StubTransport(connect_exc=voice_errors.SetupRejected("bad config"))
    made = live_seam([stub])

    answer = asyncio.run(run_answer(context="C", transcript="t", audio=b"OGG-IN"))

    assert answer.ok is False
    assert answer.reason == voice_errors.REASON_SETUP_REJECTED
    assert len(made) == 1, "a rejected configuration is not retried"
    assert stub.closed is True


def test_the_reply_is_capped(live_seam, monkeypatch):
    # The cap has a one-second floor in ``LiveTurn``, so the test uses a bound
    # above it: 2 s at 24 kHz mono s16le is 96,000 bytes, and the stub offers
    # ten seconds of speech.
    monkeypatch.setattr(config, "VOICE_CONTEXT_MAX_REPLY_SECONDS", 2.0)
    stub = StubTransport(audio=b"\x00" * 480_000, said="long")
    live_seam([stub])

    answer = asyncio.run(run_answer(context="C", transcript="t", audio=b"OGG-IN"))

    assert answer.ok is True
    assert answer.capped is True
    assert answer.seconds <= 2.0 + 0.001, "the speech must be cut at the ceiling"


def test_an_undecodable_clip_still_answers_from_the_transcript(live_seam, monkeypatch):
    stub = StubTransport(audio=b"\x00" * 960, said="جواب")
    live_seam([stub], decode=lambda data, **kw: b"")

    answer = asyncio.run(run_answer(context="C", transcript="سؤال", audio=b"BAD"))

    assert answer.ok is True
    assert stub.sent == [], "there is no audio to send, so none is sent"
    assert stub.contexts and stub.contexts[0][1] is True


def test_no_credential_is_a_fallback_not_a_failure(live_seam):
    live_seam([StubTransport()], lease=voice_context._Unavailable("no pool"))

    answer = asyncio.run(run_answer(context="C", transcript="t", audio=b"OGG-IN"))

    assert answer.ok is False
    assert answer.reason == voice_context.REASON_UNAVAILABLE


def test_nothing_to_send_is_refused_before_connecting(live_seam):
    made = live_seam([StubTransport()])

    answer = asyncio.run(run_answer(context="", transcript="", audio=b""))

    assert answer.ok is False
    assert answer.reason == voice_context.REASON_NOTHING
    assert made == [], "no session is opened for a turn with nothing in it"


def test_a_transcript_alone_is_still_a_turn(live_seam):
    """The words came through; only the audio is missing."""
    stub = StubTransport(audio=b"\x00" * 960, said="جواب")
    live_seam([stub])

    answer = asyncio.run(run_answer(context="C", transcript="سؤال", audio=b""))

    assert answer.ok is True
    assert stub.sent == []


def test_the_transport_is_closed_even_when_the_stream_raises(live_seam):
    class _Boom(StubTransport):
        async def receive(self):
            raise voice_errors.ConnectionLost("dropped")
            yield  # pragma: no cover - makes this a generator

    stub = _Boom()
    live_seam([stub])

    answer = asyncio.run(run_answer(context="C", transcript="t", audio=b"OGG-IN"))

    assert answer.ok is False
    assert stub.closed is True, "a dropped stream must not leak a session"


def test_the_concurrency_limiter_bounds_the_turns(live_seam, monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONTEXT_MAX_CONCURRENCY", 1)
    voice_context.reset_state()  # rebuild the limiter with the new bound
    live = {"now": 0, "peak": 0}

    class _Slow(StubTransport):
        async def connect(self):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.02)
            await super().connect()

        async def close(self):
            live["now"] -= 1
            await super().close()

    live_seam([_Slow(audio=b"\x00" * 960, said="x"), _Slow(audio=b"\x00" * 960, said="x")])

    async def _two():
        return await asyncio.gather(
            voice_context.answer(context="C", transcript="a", audio=b"A"),
            voice_context.answer(context="C", transcript="b", audio=b"B"),
        )

    results = asyncio.run(_two())

    assert all(r.ok for r in results)
    assert live["peak"] == 1, "one connection at a time when the bound is one"


# ══ 4. The path through the conversation ══════════════════════════════════
class FakeBot:
    def __init__(self, *, fail_voice=False):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[str] = []
        self.voices: list[dict] = []
        self.fail_voice = fail_voice

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_voice(self, chat_id, voice, reply_to_message_id=None, **kwargs):
        if self.fail_voice:
            raise TelegramError("voice refused")
        self.voices.append({"voice": voice, "reply_to": reply_to_message_id})
        return SimpleNamespace(message_id=len(self.voices))

    async def send_chat_action(self, chat_id, action, **kwargs):
        pass

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status="administrator",
            can_restrict_members=True,
            can_delete_messages=True,
            can_promote_members=True,
            can_manage_chat=True,
        )


def voice_msg(message_id=MESSAGE_ID, text=None, *, directed=False):
    """A voice note. ``directed`` aims it at Nexus.

    A voice message has no caption to put ``@guardbot`` in, so the
    Telegram-native way to aim one at the assistant is to send it as a reply to
    one of its messages; ``_addressed_to_bot`` reads exactly that edge. The
    switch tests leave it off, because an owner's spoken command is handled
    *before* the aimed-at-Nexus gate and must keep working whether or not the
    note carries a reply. The conversation-path tests turn it on, because a note
    that is not aimed at Nexus is left to the awareness layer and never reaches
    ``_answer_conversationally``.
    """
    replied = None
    if directed:
        replied = SimpleNamespace(
            message_id=message_id - 1,
            from_user=SimpleNamespace(
                id=BOT_ID, is_bot=True, full_name="Guard", username="guardbot"
            ),
            text="پرسش قبلی",
        )
    return SimpleNamespace(
        message_id=message_id,
        photo=None, video=None, animation=None, video_note=None,
        sticker=None, audio=None, document=None,
        voice=SimpleNamespace(file_id="voice-file", duration=3, mime_type="audio/ogg"),
        text=text, caption=None,
        reply_to_message=replied,
    )


def _update(msg, actor=MEMBER, chat=CHAT):
    return SimpleNamespace(
        update_id=1,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat, type="supergroup", title="G"),
        effective_user=SimpleNamespace(
            id=actor, full_name="Tester", username="tester", is_bot=False
        ),
    )


def _ctx(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def run_group(handler, msg, bot, *, actor=MEMBER, text=None):
    if text is not None:
        msg.text = text
    asyncio.run(handler(_update(msg, actor=actor), _ctx(bot)))


def install_voice_media(monkeypatch, *, transcript="حالت چطوره؟", audio=b"OGG-IN"):
    """Make the message look like a voice note that transcribed cleanly."""
    ref = SimpleNamespace(
        kind="voice",
        file_id="voice-file",
        file_size=100,
        duration=3,
        mime_type="audio/ogg",
        is_transcribable=True,
        is_visual=False,
    )

    class _Media:
        @staticmethod
        def describe(msg):
            return ref

    async def _download(ctx, fid):
        return audio

    async def _transcribe(candidate, *, download):
        assert await download("voice-file") == audio
        return SimpleNamespace(ok=True, text=transcript, no_speech=False,
                               error="", skipped="")

    monkeypatch.setattr(main, "media", _Media)
    monkeypatch.setattr(main, "_download_file", _download)
    monkeypatch.setattr(main.transcribe, "transcribe_ref", _transcribe)


def install_chat(monkeypatch, *, seen, reply=None):
    """Replace the text path, recording the context it was handed."""
    result = reply or chat.ChatReply(answered=True, text="پاسخ متنی", turns=1)

    async def _reply(chat_id, user_id, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        seen.append({"body": body, "context": context, "want_voice": want_voice})
        return result

    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat_queue, "reply", _reply)


def install_voice_answer(monkeypatch, *, seen, answer=None):
    async def _answer(*, context, transcript="", audio=b"", chat_id=0, user_id=0):
        seen.append(
            {
                "context": context,
                "transcript": transcript,
                "audio": audio,
                "chat_id": chat_id,
                "user_id": user_id,
            }
        )
        return answer or voice_context.Answer(ok=True, voice=b"OGG-OUT", said="پاسخ صوتی")

    monkeypatch.setattr(main.voice_context, "available", lambda: True)
    monkeypatch.setattr(main.voice_context, "answer", _answer)


def test_a_voice_note_is_answered_with_a_spoken_reply(monkeypatch):
    install_voice_media(monkeypatch)
    text_seen: list = []
    install_chat(monkeypatch, seen=text_seen)
    voice_seen: list = []
    install_voice_answer(monkeypatch, seen=voice_seen)
    bot = FakeBot()

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert len(voice_seen) == 1, "the spoken turn must run exactly once"
    assert voice_seen[0]["audio"] == b"OGG-IN"
    assert voice_seen[0]["transcript"] == "حالت چطوره؟"
    assert bot.voices, "the answer must go out as a voice message"
    assert bot.voices[0]["voice"] == b"OGG-OUT"
    assert bot.voices[0]["reply_to"] == MESSAGE_ID, "it must reply to the voice note"
    assert text_seen == [], "the text path must not also answer"


def test_the_spoken_turn_gets_the_same_context_a_text_turn_would(monkeypatch):
    """The whole point: it is the same Nexus, not a second voice bot.

    The two runs share one database, so the stores a turn *writes* — the room
    window, the personal memory and the conversational state — are cleared
    between them. Without that the second run would read the first run's reply
    out of the room window and the comparison would be measuring the harness
    rather than the wiring.
    """

    def _clear_volatile():
        db.awareness_reset()
        db.memory_reset()
        db.state_reset()
        main._nexus_addressed.clear()

    # Run 1 — the layer off: the ordinary text path, and the context it is given.
    monkeypatch.setattr(main.voice_context, "available", lambda: False)
    install_voice_media(monkeypatch)
    text_seen: list = []
    install_chat(monkeypatch, seen=text_seen)
    run_group(main.on_group_chat, voice_msg(directed=True), FakeBot())
    assert text_seen, "the text path must run when the layer is off"

    # Run 2 — the layer on: the spoken turn, and the context it is given.
    _clear_volatile()
    voice_context.reset_state()
    monkeypatch.setattr(main.voice_context, "available", lambda: True)
    voice_seen: list = []
    install_voice_answer(monkeypatch, seen=voice_seen)
    run_group(main.on_group_chat, voice_msg(directed=True), FakeBot())

    assert voice_seen, "the spoken turn must run when the layer is on"
    assert voice_seen[0]["context"] == text_seen[0]["context"], (
        "the voice turn must be given the identical assembled context"
    )


def test_off_keeps_the_exact_old_path(monkeypatch):
    """With the switch off, a voice note is transcribed and answered in text."""
    monkeypatch.setattr(main.voice_context, "available", lambda: False)
    install_voice_media(monkeypatch)
    text_seen: list = []
    install_chat(monkeypatch, seen=text_seen)
    called: list = []

    async def _never(**kwargs):  # pragma: no cover - must not be reached
        called.append(1)

    monkeypatch.setattr(main.voice_context, "answer", _never)
    bot = FakeBot()

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert called == [], "no Live session may be opened while the switch is off"
    assert text_seen and text_seen[0]["want_voice"] is True
    assert text_seen[0]["body"] == "حالت چطوره؟"


def test_a_failed_spoken_turn_falls_back_to_text(monkeypatch):
    install_voice_media(monkeypatch)
    text_seen: list = []
    install_chat(monkeypatch, seen=text_seen)
    install_voice_answer(
        monkeypatch,
        seen=[],
        answer=voice_context.Answer(ok=False, reason="provider_error"),
    )
    bot = FakeBot()

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert text_seen, "a failed turn must still get the person an answer"
    assert bot.voices == []


def test_a_refused_voice_upload_falls_back_to_the_transcript(monkeypatch):
    install_voice_media(monkeypatch)
    text_seen: list = []
    install_chat(monkeypatch, seen=text_seen)
    install_voice_answer(monkeypatch, seen=[])
    bot = FakeBot(fail_voice=True)

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert bot.messages, "the words must still be sent when the upload fails"
    assert "پاسخ صوتی" in bot.messages[-1]


def test_an_unpackable_answer_is_sent_as_text(monkeypatch):
    """The speech could not be encoded, but the words exist."""
    install_voice_media(monkeypatch)
    install_chat(monkeypatch, seen=[])
    install_voice_answer(
        monkeypatch,
        seen=[],
        answer=voice_context.Answer(
            ok=True, voice=None, said="پاسخ صوتی", reason=voice_context.REASON_NO_AUDIO
        ),
    )
    bot = FakeBot()

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert bot.messages and "پاسخ صوتی" in bot.messages[-1]
    assert bot.voices == []


def test_a_tag_directive_keeps_the_text_path(monkeypatch):
    """A voice message cannot carry Telegram's mention anchor."""
    install_voice_media(monkeypatch, transcript="میلاد رو تگ کن")
    text_seen: list = []
    install_chat(monkeypatch, seen=text_seen)
    voice_seen: list = []
    install_voice_answer(monkeypatch, seen=voice_seen)
    # Make the target resolver find a person named میلاد in this room. The name
    # memory is the table ``people.resolve`` reads, which is why this is
    # ``people_remember`` and not a captured message.
    db.people_remember(CHAT, OTHER, first_name="میلاد")
    bot = FakeBot()

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert voice_seen == [], "a mention request is not served by a voice reply"
    assert text_seen, "the text path is what can actually tag somebody"


def test_a_voice_note_is_understood_before_it_is_spoken(monkeypatch):
    """The end-to-end claim, in one turn: understand, then speak.

    A member sends a voice note that is a reply to Nexus. The server transcribes
    it, resolves who and what it is about, and assembles the room's context —
    including what it remembers about that person and what the room has been
    saying — and only then hands that context, and the person's own voice, to the
    spoken turn. The answer comes back as a voice message replying to the note.
    """
    install_voice_media(monkeypatch, transcript="یادت هست من چه قهوه‌ای دوست داشتم؟")
    # Something the server remembers about this person, in this room.
    db.memory_remember(
        CHAT,
        MEMBER,
        "قهوه",
        category="preference",
        value="او قهوه تلخ دوست دارد",
        source="chat",
        confidence=0.9,
    )
    # Something the room has been saying, captured by the awareness layer.
    db.group_capture(
        CHAT, OTHER, awareness.ROLE_MEMBER, "میلاد", "من فردا میام تهران", keep=50
    )
    voice_seen: list = []
    install_voice_answer(monkeypatch, seen=voice_seen)
    bot = FakeBot()

    run_group(main.on_group_chat, voice_msg(directed=True), bot)

    assert len(voice_seen) == 1, "one voice note is one spoken turn"
    context = voice_seen[0]["context"]
    assert "قهوه تلخ" in context, "the person's own memory must reach the turn"
    assert "تهران" in context, "the room's conversation must reach the turn"
    assert voice_seen[0]["transcript"] == "یادت هست من چه قهوه‌ای دوست داشتم؟"
    assert voice_seen[0]["audio"] == b"OGG-IN", "the voice itself is what is heard"
    assert voice_seen[0]["chat_id"] == CHAT
    assert voice_seen[0]["user_id"] == MEMBER
    assert bot.voices and bot.voices[0]["voice"] == b"OGG-OUT"
    assert bot.voices[0]["reply_to"] == MESSAGE_ID, "the answer replies to the note"


def test_one_person_s_memory_never_reaches_another_s_voice_turn(monkeypatch):
    """Memory is scoped by (chat, user); a voice turn does not widen it."""
    install_voice_media(monkeypatch, transcript="یادت هست من چه قهوه‌ای دوست داشتم؟")
    db.memory_remember(
        CHAT,
        OTHER,
        "قهوه",
        category="preference",
        value="او قهوه تلخ دوست دارد",
        source="chat",
        confidence=0.9,
    )
    voice_seen: list = []
    install_voice_answer(monkeypatch, seen=voice_seen)

    run_group(main.on_group_chat, voice_msg(directed=True), FakeBot(), actor=MEMBER)

    assert voice_seen, "the turn must still run"
    assert "قهوه تلخ" not in voice_seen[0]["context"], (
        "another person's memory must never enter this turn"
    )
    assert voice_seen[0]["user_id"] == MEMBER


def test_two_rooms_are_answered_as_two_turns(monkeypatch):
    """Concurrent voice notes in different rooms do not share a turn.

    The limiter is one process-wide bound — that is how many provider
    connections may be held — but it is not a shared context: each turn is
    handed its own room's record and its own sender's id, and both are answered.
    """
    other_room = CHAT - 1
    # Seed the primary room from ``GROUP_IDS`` *before* adding the second one:
    # the allowlist seeds itself only while it is empty, so a register first
    # would leave the primary room unregistered.
    groups.load()
    groups.register(other_room, actor_id=OWNER)
    main._nexus_visibility[other_room] = "administrator"
    install_voice_media(monkeypatch)
    seen: list = []
    install_voice_answer(monkeypatch, seen=seen)

    async def _both():
        await asyncio.gather(
            main.on_group_chat(
                _update(voice_msg(directed=True), actor=MEMBER, chat=CHAT),
                _ctx(FakeBot()),
            ),
            main.on_group_chat(
                _update(voice_msg(directed=True), actor=OTHER, chat=other_room),
                _ctx(FakeBot()),
            ),
        )

    asyncio.run(_both())

    assert len(seen) == 2, "both rooms were answered"
    rooms = {entry["chat_id"] for entry in seen}
    assert rooms == {CHAT, other_room}, "each turn carries its own room"
    senders = {entry["chat_id"]: entry["user_id"] for entry in seen}
    assert senders == {CHAT: MEMBER, other_room: OTHER}, (
        "each turn carries its own sender"
    )


def test_a_duplicate_delivery_cannot_produce_a_second_voice_reply():
    """The guard is upstream of the handler; a redelivery stops there.

    The claim itself is covered exhaustively in ``tests/test_update_dedup.py``;
    what matters here is that the voice path sits *behind* it, so a duplicate
    Telegram delivery never reaches a second spoken turn.
    """
    from telegram.ext import ApplicationHandlerStop

    assert db.update_claim(9001) is True
    assert db.update_claim(9001) is False
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(main.on_any_update(SimpleNamespace(update_id=9001),
                                       SimpleNamespace(bot=SimpleNamespace())))


# ══ 5. The execution layer, not the message, holds the authority ══════════
def request_for(operation, *, actor):
    return admin_service.AdminRequest(
        operation=operation,
        chat_id=CHAT,
        actor_id=actor,
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_PYTHON,
        at=int(time.time()),
    )


class FakeGateway:
    async def bot_right(self, chat_id, right):
        return True

    async def member(self, chat_id, user_id):
        return {"status": "member"}


def execute(operation, *, actor):
    return asyncio.run(
        admin_service.execute(request_for(operation, actor=actor), FakeGateway())
    )


def test_the_operations_are_owner_only():
    assert execute("voice_context_offline", actor=OWNER).ok is True
    assert voice_context.running() is False

    voice_context.set_running(True, actor_id=OWNER, reason="test")
    assert execute("voice_context_offline", actor=ADMIN).ok is False
    assert voice_context.running() is True


def test_no_role_bundle_carries_nexus_control():
    assert rbac.resolve(OWNER).can("nexus.control") is True
    assert rbac.resolve(ADMIN).can("nexus.control") is False
    for role, bundle in rbac.ROLE_PERMISSIONS.items():
        assert "nexus.control" not in bundle, role


def test_the_operations_work_while_nexus_is_off():
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")

    assert execute("voice_context_online", actor=OWNER).ok is True


def test_the_transition_is_audited():
    execute("voice_context_offline", actor=OWNER)

    rows = db.audit_since(chat_id=CHAT, since=0, limit=50)
    assert any(r.get("action") == "voice_context.offline" for r in rows)


def test_the_ai_tool_recognises_the_request_but_grants_nothing():
    from app import admin_tools

    spec = admin_tools.TOOLS["voice_context_offline"]
    assert spec.kind == admin_tools.KIND_WRITE
    assert spec.permission == "nexus.control"
    assert spec.operation == "voice_context_offline"
    # A member is offered nothing, so the model cannot even reach for it.
    assert admin_tools.tool_names_for(rbac.guest(MEMBER)) == ()


# ══ 6. What the operator sees ═════════════════════════════════════════════
def test_the_status_line_reports_the_switch():
    voice_context.set_running(False, actor_id=OWNER, reason="test")
    assert config.VOICE_CONTEXT_OFF_LABEL in main._nexus_status_text()

    voice_context.set_running(True, actor_id=OWNER, reason="test")
    assert config.VOICE_CONTEXT_ON_LABEL in main._nexus_status_text()


def test_the_diagnostic_reports_the_switch():
    from app import agent_data

    voice_context.set_running(False, actor_id=OWNER, reason="test")
    assert agent_data.nexus_diagnostics(chat_id=CHAT)["voice_context_enabled"] is False


def test_the_status_payload_carries_no_credential():
    payload = voice_context.status()
    assert payload["configured"] is True
    for value in payload.values():
        assert "test-key" not in str(value)


# ══ 7. The input bounds ═══════════════════════════════════════════════════
def test_a_clip_within_the_bounds_is_accepted():
    ref = SimpleNamespace(is_transcribable=True, duration=3, file_size=1000)
    assert voice_context.accepts(ref) is True


def test_a_clip_beyond_the_bounds_is_not_accepted():
    long_ref = SimpleNamespace(is_transcribable=True, duration=10_000, file_size=10)
    big_ref = SimpleNamespace(is_transcribable=True, duration=1, file_size=10**12)
    assert voice_context.accepts(long_ref) is False
    assert voice_context.accepts(big_ref) is False


def test_a_non_audio_attachment_is_not_accepted():
    assert voice_context.accepts(None) is False
    assert voice_context.accepts(
        SimpleNamespace(is_transcribable=False, duration=1, file_size=1)
    ) is False


# ══ 8. The instruction is the persona, plus the medium ════════════════════
def test_the_instruction_starts_from_the_conversational_persona():
    instruction = voice_context.instruction()

    assert instruction.startswith(chat.SYSTEM_INSTRUCTION)
    assert "spoken aloud" in instruction
    assert "voice message" in instruction
