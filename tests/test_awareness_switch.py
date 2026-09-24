"""The owner's awareness switch: two switches, one verb, and no restart.

The owner asked for a way to stop the assistant *reading the room* without
stopping it *answering* — to get the pre-awareness chat speed back by saying so
in the group, and to be able to say so again to undo it. The feature is small;
the failure modes around it are not, and this file exists to pin the ones that
would be invisible from inside a group:

* **The verb is shared and the nouns are not.** «آگاهی خاموش» and «نکسوس خاموش»
  both contain «خاموش». A router that read the verb alone would silence the
  *assistant* when the owner meant to silence the *reading*, which is the exact
  bug that produced this feature. Which switch was meant is decided by
  ``awareness.named`` and it wins over Nexus's own name.
* **Off must mean off everywhere.** Not "the pass is skipped" but "no capture,
  no context in the prompt, no request, and the API key never read". A gate
  missing on any one of those paths is the difference between a switch and a
  suggestion, so each is asserted separately.
* **Off must not be silence.** Ordinary chat keeps working, and the confirmation
  the owner reads must not be mistakable for "the assistant is off".
* **It must survive a restart, and the owner must be the only one who can do
  it.** The state is persisted, the authority is ``nexus.control``, and the
  actor is resolved from their Telegram id rather than from anything they wrote.

Nothing here talks to Telegram or to Google. ``chat.reply`` and the awareness
transport are replaced, so "did a model call happen" is exact.
"""
import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import admin_service, awareness, chat, config, db, main, nexus, rbac

OWNER = 999
ADMIN = 556
MODERATOR = 777
MEMBER = 42
CHAT = -1001234567890
BOT_ID = 1


# ── Harness ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def switch_env(monkeypatch):
    """A deployment with an owner, an administrator, and awareness on.

    The awareness names are pinned to the production default rather than left to
    the environment, because the whole point of the first tests below is *which
    spellings the owner actually types* — a suite that read them from a real
    ``.env`` would pass or fail depending on the machine. That includes «پایش»,
    which is the one entry that is a *description* of the layer rather than a
    name for it; it is in the list because the owner addresses the layer that
    way («قطع کن این پایش رو»).
    """
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin", f"{MODERATOR}:moderator"])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", True)
    monkeypatch.setattr(config, "NEXUS_OBSERVE_ADMINS", True)
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(
        config,
        "NEXUS_AWARENESS_NAMES",
        ["awareness", "اورنس", "آگاهی", "اگاهی", "پایش"],
    )
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "test-awareness-key")
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 0.0)

    db.init()
    db.admin_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    awareness.reset_switch()
    chat.reset_state()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_sweeping = False
    main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )
    yield
    db.admin_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    awareness.reset_switch()


class FakeBot:
    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[str] = []
        self.actions: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

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


def message(**fields):
    msg = SimpleNamespace(
        message_id=10, photo=None, video=None, animation=None, video_note=None,
        sticker=None, voice=None, audio=None, document=None, text=None,
        caption=None, reply_to_message=None,
    )
    for key, value in fields.items():
        setattr(msg, key, value)
    return msg


def run(handler, msg, bot, actor=MEMBER):
    asyncio.run(
        handler(
            SimpleNamespace(
                effective_message=msg,
                effective_chat=SimpleNamespace(id=CHAT, type="supergroup", title="G"),
                effective_user=SimpleNamespace(
                    id=actor, full_name="Tester", username="tester", is_bot=False
                ),
            ),
            SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot)),
        )
    )


def request_for(operation, *, actor, interface=admin_service.INTERFACE_PYTHON):
    return admin_service.AdminRequest(
        operation=operation,
        chat_id=CHAT,
        actor_id=actor,
        request_id=admin_service.new_request_id(),
        interface=interface,
        at=int(time.time()),
    )


class FakeGateway:
    def __init__(self):
        self.calls: list[tuple] = []

    async def bot_right(self, chat_id, right):
        return True

    async def mute(self, chat_id, user_id):
        self.calls.append(("mute", chat_id, user_id))

    async def unmute(self, chat_id, user_id):
        self.calls.append(("unmute", chat_id, user_id))

    async def ban(self, chat_id, user_id):
        self.calls.append(("ban", chat_id, user_id))

    async def unban(self, chat_id, user_id):
        self.calls.append(("unban", chat_id, user_id))

    async def delete(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))

    async def promote(self, chat_id, user_id, rights):
        self.calls.append(("promote", chat_id, user_id))

    async def demote(self, chat_id, user_id):
        self.calls.append(("demote", chat_id, user_id))

    async def warn(self, chat_id, user_id, reason):
        self.calls.append(("warn", chat_id, user_id, reason))

    async def member(self, chat_id, user_id):
        return {"status": "member"}


def execute(request, gateway=None):
    return asyncio.run(admin_service.execute(request, gateway or FakeGateway()))


# ══ 1. Which switch the words are about ═══════════════════════════════════
def test_the_owners_own_spellings_all_name_the_layer():
    """The spellings the owner actually types, including the transliteration."""
    for text in ("آگاهی خاموش", "اگاهی خاموش", "اورنس خاموش", "awareness off"):
        assert awareness.named(text) is True, text


def test_a_name_inside_a_longer_word_is_not_the_layer():
    """Whole-word, or ordinary conversation reaches a switch."""
    assert awareness.named("بیداری") is False
    assert awareness.named("awarenessless") is False
    assert awareness.named("") is False


def test_the_shipped_default_names_every_spelling_the_owner_uses():
    """The fixture pins the names, so the *shipped* default is not covered here.

    That matters, because the bug this vocabulary work fixes was a missing entry
    in this default: the owner says «قطع کن این پایش رو», the router found no
    awareness name in it, and silenced the assistant. The list is a setting so an
    operator can trim it — but what ships has to include the words the owner
    actually says.
    """
    source = Path(config.__file__).read_text(encoding="utf-8")
    literal = re.search(
        r'os\.getenv\(\s*"NEXUS_AWARENESS_NAMES"\s*,\s*"([^"]*)"', source
    )
    assert literal, "the default is no longer a literal this test can read"
    shipped = {name.strip() for name in literal.group(1).split(",")}
    assert {"awareness", "اورنس", "آگاهی", "اگاهی", "پایش"} <= shipped


def test_the_layer_and_the_assistant_are_told_apart():
    """The disambiguation the whole feature rests on.

    «اورنس» is not a name Nexus answers to, and «نکسوس» is not a name the
    awareness layer answers to — so the two readings can never both be true, and
    the router's precedence rule has something unambiguous to prefer.
    """
    assert awareness.named("اورنس خاموش") is True
    assert nexus.is_named("اورنس خاموش") is False
    assert awareness.named("نکسوس خاموش") is False
    assert nexus.is_named("نکسوس خاموش") is True


def test_the_verb_alone_still_resolves_for_both():
    """``command_from`` is about the direction, and it is the same verb."""
    assert nexus.command_from("آگاهی خاموش") == nexus.OFFLINE
    assert nexus.command_from("نکسوس خاموش") == nexus.OFFLINE
    assert nexus.command_from("آگاهی روشن") == nexus.ONLINE


def test_the_owners_own_phrasings_are_understood_when_the_layer_is_named():
    """The report that produced this work: fourteen ways of saying it, one worked.

    Each phrase below is one the owner actually types. The layer is named in
    every one, which is what licenses the wider half of the vocabulary — see
    ``command_from``'s ``names_layer`` argument.
    """
    for text, expected in (
        ("آگاهی رو راه بنداز", nexus.ONLINE),
        ("اورنس رو بیار بالا", nexus.ONLINE),
        ("اورنس بیا بالا", nexus.ONLINE),
        ("اورنس بیدار کن", nexus.ONLINE),
        ("اورنس چشاتو باز کن", nexus.ONLINE),
        ("اورنس وصل شو به محیط", nexus.ONLINE),
        ("اورنس رو بیار پایین", nexus.OFFLINE),
        ("آگاهی رو خاموشش کن", nexus.OFFLINE),
        ("اورنس رو قطعش کن", nexus.OFFLINE),
        ("اورنس چشاتو ببند", nexus.OFFLINE),
        ("اورنس استراحت کن", nexus.OFFLINE),
        ("اورنس دیگه نبین", nexus.OFFLINE),
    ):
        assert nexus.command_from(text, names_layer=True) == expected, text


def test_the_same_phrases_say_nothing_without_a_name():
    """The other half of the contract, and the safety property.

    Every phrase above is also ordinary speech — «بیا پایین» is "come
    downstairs", «راه بنداز» is "get it going", «چشاتو باز کن» is "open your
    eyes". Without a name to attach them to they must resolve to nothing, or a
    moderator telling somebody to come downstairs would have silenced the bot.
    """
    for text in (
        "بیا پایین خونه ما",
        "برو پایین طبقه همکف",
        "کار رو راه بنداز",
        "بیا بالا ببینم چه خبره",
        "چشاتو باز کن",
        "چشاتو ببند",
        "استراحت کن",
        "دیگه نبین",
        "وصل شو به محیط",
        "راه بنداز",
    ):
        assert nexus.command_from(text) is None, text


def test_the_description_of_the_layer_counts_as_naming_it():
    """«قطع کن این پایش رو» was silencing the whole assistant.

    «پایش» is a *description* of what the layer does rather than a name for it,
    which is why it is a setting. It resolves to the layer like any other name,
    so the instruction lands on the reading and not on the assistant.
    """
    assert awareness.named("قطع کن این پایش رو") is True
    assert awareness.named("پایش رو قطع کن") is True
    # A name on its own is not an instruction.
    assert nexus.command_from("پایش وضعیت گروه خوبه؟", names_layer=True) is None


def test_a_refusal_survives_the_wider_vocabulary():
    """Widening the lists must not open a way past the negations."""
    for text in (
        "اورنس رو خاموش نکن",
        "نکسوس بیا پایین نشو",
        "اورنس رو خاموش کن بعد روشن کن",
        "آگاهی رو راه بنداز، ولی الان نه",
    ):
        assert nexus.command_from(text, names_layer=True) is None, text


# ══ 2. Routing: the right switch, from one sentence ═══════════════════════
def test_awareness_off_does_not_switch_the_assistant_off():
    """The bug this feature was asked for: «آگاهی خاموش» must not silence Nexus."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی خاموش"), bot, actor=OWNER)

    assert awareness.running() is False, "the layer was not switched off"
    assert nexus.is_online() is True, "the assistant was silenced instead"


def test_the_transliteration_routes_to_awareness_too():
    bot = FakeBot()
    run(main.on_group_chat, message(text="اورنس خاموش"), bot, actor=OWNER)

    assert awareness.running() is False
    assert nexus.is_online() is True


def test_stop_this_watching_lands_on_the_reading_not_the_assistant():
    """The phrase from the report, end to end.

    «قطع کن این پایش رو» names the layer by describing it, so it must reach the
    awareness switch. Before the vocabulary work the router read «قطع کن» alone
    and silenced the *assistant* — the exact failure the whole two-switch design
    exists to prevent.
    """
    bot = FakeBot()
    run(main.on_group_chat, message(text="قطع کن این پایش رو"), bot, actor=OWNER)

    assert awareness.running() is False, "the reading was not switched off"
    assert nexus.is_online() is True, "the assistant was silenced instead"


def test_an_idiom_with_no_layer_named_moves_no_switch():
    """The guard that makes the wider vocabulary safe, end to end.

    The message *is* aimed at the bot — it replies to one of its own — so the
    addressed check passes and the decision falls through to ``command_from``.
    «بیا پایین» is in the named-only half, and no layer is named, so it must
    still resolve to nothing. This is the case that used to silence the bot.
    """
    bot = FakeBot()
    run(
        main.on_group_chat,
        message(
            text="بیا پایین",
            reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID)),
        ),
        bot,
        actor=OWNER,
    )

    assert awareness.running() is True
    assert nexus.is_online() is True


def test_the_same_idiom_moves_the_switch_once_the_layer_is_named():
    """The positive half of the gate, so it is a gate and not a wall."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="اورنس بیا پایین"), bot, actor=OWNER)

    assert awareness.running() is False
    assert nexus.is_online() is True


def test_nexus_off_does_not_switch_awareness_off():
    """And the other direction, so the precedence is a rule and not a bias."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="نکسوس خاموش"), bot, actor=OWNER)

    assert nexus.is_online() is False
    assert awareness.running() is True


def test_the_owner_can_turn_the_layer_back_on():
    awareness.set_running(False, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی روشن"), bot, actor=OWNER)

    assert awareness.running() is True
    assert nexus.is_online() is True


def test_a_member_cannot_switch_the_layer_off():
    """Authority is the owner's Telegram id, not the words in the message."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی خاموش"), bot, actor=MEMBER)

    assert awareness.running() is True, "a member reached the owner's switch"


def test_an_administrator_cannot_switch_the_layer_off():
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی خاموش"), bot, actor=ADMIN)

    assert awareness.running() is True


def test_a_member_gets_no_reply_and_no_announcement():
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی خاموش"), bot, actor=MEMBER)
    assert bot.messages == [], "being ignored is not announced"


def test_the_confirmation_does_not_say_the_assistant_is_off():
    """The one reading the owner must never take from this reply."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی خاموش"), bot, actor=OWNER)

    assert bot.messages, "the owner is told what happened"
    reply = bot.messages[-1]
    assert reply == config.NEXUS_AWARENESS_OFF_DONE_TEXT
    assert "نکسوس خاموش شد" not in reply


def test_a_no_op_reports_the_state_it_is_already_in():
    """«آگاهی خاموش» twice must say "already off", not "off" again.

    The state label is the regression: it is read from the same source the gate
    reads, so the sentence cannot report a state the switch is not in.
    """
    awareness.set_running(False, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی خاموش"), bot, actor=OWNER)

    assert bot.messages[-1] == config.NEXUS_AWARENESS_ALREADY_TEXT.format(
        state=config.NEXUS_AWARENESS_OFF_LABEL
    )


def test_a_no_op_on_nexus_reports_the_nexus_state_label():
    """And the Nexus branch reports Nexus's own label, not a confirmation."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="نکسوس روشن"), bot, actor=OWNER)

    assert bot.messages[-1] == config.NEXUS_ALREADY_TEXT.format(
        state=config.NEXUS_STATE_ONLINE_LABEL
    )


# ══ 3. Off means off on every path ════════════════════════════════════════
def test_off_stops_capture():
    """No window is kept, so there is nothing to read later either."""
    awareness.set_running(False, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run(main.on_group_chat, message(text="یک پیام معمولی"), bot, actor=MEMBER)

    assert db.group_window(CHAT, limit=10) == []


def test_on_captures():
    """The other half, so the test above is measuring the switch."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="یک پیام معمولی"), bot, actor=MEMBER)

    assert [r["text"] for r in db.group_window(CHAT, limit=10)] == ["یک پیام معمولی"]


def test_off_removes_the_room_block_from_the_chat_prompt():
    """The one piece of awareness that reaches the conversational path."""
    awareness.capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "m", "سلام")
    assert awareness.room_block(CHAT) != ""

    awareness.set_running(False, actor_id=OWNER, reason="test")
    assert awareness.room_block(CHAT) == ""


def test_off_makes_no_awareness_request_and_never_reads_the_key(monkeypatch):
    """The promise: with the layer off, no call is built and no key is touched.

    The transport is replaced with one that records, and the key is replaced with
    a sentinel that raises if it is read — so "no request" and "no key use" are
    two separate assertions rather than one inference from a log.
    """
    called = []

    async def _transport(*args, **kwargs):  # pragma: no cover - must not run
        called.append(args)
        raise AssertionError("the awareness transport was reached while off")

    class _ExplodingKey(str):
        def __eq__(self, other):  # pragma: no cover - must not run
            raise AssertionError("the awareness key was read while off")

    monkeypatch.setattr(chat, "_awareness_full", _transport)
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", _ExplodingKey("k"))
    awareness.set_running(False, actor_id=OWNER, reason="test")

    reply = asyncio.run(chat.awareness("some transcript"))

    assert reply.skipped == "disabled"
    assert called == []


def test_off_makes_no_pass(monkeypatch):
    """The sweeper and the urgency hint both refuse, and a pass costs nothing."""
    monkeypatch.setattr(config, "NEXUS_AWARENESS_DEBOUNCE_SECONDS", 0.0)
    awareness.capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "m", "یک سوال")
    awareness.set_running(False, actor_id=OWNER, reason="test")

    bot = FakeBot()
    row = {"chat_id": CHAT, "max_id": 1, "pending": 1}
    ran = asyncio.run(
        main._awareness_run_room(SimpleNamespace(bot=bot), row, urgent=True)
    )

    assert ran is False


def test_off_leaves_normal_chat_working(monkeypatch):
    """Off is not silence: an addressed message is still answered."""
    calls = []

    async def _reply(chat_id, user_id, body, **kwargs):
        calls.append(body)
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    awareness.set_running(False, actor_id=OWNER, reason="test")

    bot = FakeBot()
    run(main.on_group_chat, message(text="نکسوس سلام"), bot, actor=OWNER)

    assert calls == ["نکسوس سلام"], "the conversational path was lost"
    assert bot.messages == ["باشه"]


# ══ 4. It survives a restart, and it starts on ════════════════════════════
def test_never_touched_means_on():
    """A deployment that never used the switch behaves as its config asks."""
    assert db.awareness_control_get() is None
    assert awareness.running() is True
    assert awareness.enabled() is True


def test_off_survives_a_restart():
    """The cache is dropped, as a restart would; the state comes back off."""
    awareness.set_running(False, actor_id=OWNER, reason="test")
    awareness.reset_switch()  # the restart

    assert awareness.running() is False
    assert awareness.enabled() is False


def test_on_survives_a_restart_too():
    awareness.set_running(True, actor_id=OWNER, reason="test")
    awareness.reset_switch()

    assert awareness.running() is True


def test_config_off_wins_over_a_stored_on(monkeypatch):
    """The effective state is both halves, and either may say no."""
    awareness.set_running(True, actor_id=OWNER, reason="test")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)

    assert awareness.configured() is False
    assert awareness.running() is True
    assert awareness.enabled() is False


def test_the_master_switch_cannot_be_overridden_by_a_message(monkeypatch):
    """«آگاهی روشن» must not claim a state the deployment will not enter.

    The stored switch and the deploy-time setting are two halves of one answer,
    and a spoken command can only move one of them. Reporting the half that
    changed would be the same class of lie this feature exists to remove, so the
    reply says a restart is needed instead.
    """
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی روشن"), bot, actor=OWNER)

    assert bot.messages[-1] == config.NEXUS_AWARENESS_CONFIG_OFF_TEXT
    assert awareness.enabled() is False, "the layer must not run while the master is off"


# ══ 5. The execution layer, not the message, holds the authority ══════════
def test_the_operation_is_owner_only():
    assert execute(request_for("awareness_offline", actor=OWNER)).ok is True
    assert awareness.running() is False

    awareness.set_running(True, actor_id=OWNER, reason="test")
    assert execute(request_for("awareness_offline", actor=ADMIN)).ok is False
    assert awareness.running() is True


def test_the_operation_works_while_nexus_is_off():
    """A switch that needed the assistant awake would be unreachable."""
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")

    result = execute(request_for("awareness_online", actor=OWNER))

    assert result.ok is True, result.outcome


def test_the_transition_is_audited():
    execute(request_for("awareness_offline", actor=OWNER))

    rows = db.audit_since(chat_id=CHAT, since=0, limit=50)
    assert any(r.get("action") == "awareness.offline" for r in rows)


# ══ 6. What the operator sees is the effective state ══════════════════════
def test_the_metric_reports_the_effective_state():
    """Config on, owner off — the metric must say off, or it describes a lie."""
    assert config.NEXUS_AWARENESS_ENABLED is True
    assert awareness.metrics()["enabled"] is True

    awareness.set_running(False, actor_id=OWNER, reason="test")

    assert awareness.metrics()["enabled"] is False
    assert "awareness[off]" in awareness.metrics_line()


def test_the_status_line_reports_the_effective_state():
    awareness.set_running(False, actor_id=OWNER, reason="test")
    text = main._nexus_status_text()

    assert config.NEXUS_AWARENESS_OFF_LABEL in text


def test_the_diagnostic_reports_the_effective_state():
    awareness.set_running(False, actor_id=OWNER, reason="test")
    from app import agent_data

    out = agent_data.nexus_diagnostics(chat_id=CHAT)
    assert out["awareness_enabled"] is False


# ══ 7. It never leaks a secret ════════════════════════════════════════════
def test_no_reply_ever_contains_the_key():
    awareness.set_running(False, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run(main.on_group_chat, message(text="آگاهی روشن"), bot, actor=OWNER)

    assert all("test-awareness-key" not in m for m in bot.messages)


def test_room_block_uses_the_window_it_was_handed(monkeypatch):
    """One read per reply: the caller hands in what it already read.

    The addressed path reads the room once and gives the same rows to the
    transcript and to the reading beside it. If ``room_block`` read the window
    again, that promise would be false and a reply would cost two queries.
    """
    awareness.capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "m", "سلام")
    rows = db.group_window(CHAT, limit=10)

    def _must_not_read(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("room_block read the window it was handed")

    monkeypatch.setattr(awareness, "window", _must_not_read)
    assert awareness.room_block(CHAT, limit=10, messages=rows) != ""


def test_room_block_with_an_empty_window_renders_nothing(monkeypatch):
    """An empty handed-in window is empty, not a reason to read the database."""
    def _must_not_read(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("an empty window was re-read")

    monkeypatch.setattr(awareness, "window", _must_not_read)
    assert awareness.room_block(CHAT, limit=10, messages=[]) == ""
