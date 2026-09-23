"""Nexus Awareness: the room, understood — and the boundary that does not move.

The feature this suite pins down is a distinction, and almost every test here is
one of its two halves:

* **Understanding is not authority.** A message is captured for everybody, read
  by Gemini, and summarised — and an ordinary member still cannot cause an
  action, because every tool call is authorised again from the actor's id in
  ``app/admin_service.py``. Awareness observes; it never authorises.
* **Understanding is not response.** The room is read whether or not anybody is
  being answered, and the decision to speak belongs to the model. Nexus must not
  answer every message, and must not stay silent merely because there was no
  mention or reply.

Nothing here talks to Telegram or to Google. ``chat.awareness`` is the awareness
transport seam and it is replaced, so "did a model call happen, and what was it
given" is asserted exactly rather than inferred from a log.
"""
import asyncio
import inspect
import time
from types import SimpleNamespace

import pytest

from app import (
    admin_tools,
    awareness,
    awareness_context,
    chat,
    config,
    db,
    gemini_pool,
    main,
    nexus,
    rbac,
)

OWNER = 999
SENIOR = 555
ADMIN = 556
MODERATOR = 777
MEMBER = 42
STRANGER = 31337
CHAT = -1001234567890
OTHER_CHAT = -1009999999999
BOT_ID = 1


# ── Harness ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def awareness_env(monkeypatch, tmp_path):
    """A deployment with an owner, a hierarchy, and awareness switched on."""
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(
        config,
        "CONFIG_ADMINS",
        [
            f"{SENIOR}:senior_admin",
            f"{ADMIN}:admin",
            f"{MODERATOR}:moderator",
        ],
    )
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", True)
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
    # Awareness settings, pinned so a test never depends on a tuned default.
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_DEBOUNCE_SECONDS", 8.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_WAIT_SECONDS", 45.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 20.0)
    # The window is deliberately *not* pinned: its shipped size is a property
    # this suite asserts (see ``test_the_default_window_covers_one_allowance_
    # interval``), and a test that wants a different window passes one in
    # explicitly. Pinning it here would make that assertion test the pin.
    monkeypatch.setattr(config, "NEXUS_AWARENESS_WINDOW_CHARS", 6000)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 3600)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_ROWS", 400)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_CHATS_PER_TICK", 2)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_MESSAGES", 20)

    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    chat.reset_state()
    awareness_context.reset_rooms()
    main._recently_deleted.clear()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_sweeping = False
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
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()


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

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.actions.append(("ban", chat_id, user_id))

    async def restrict_chat_member(self, chat_id, user_id, **kwargs):
        self.actions.append(("restrict", chat_id, user_id))

    async def delete_message(self, chat_id, message_id):
        self.actions.append(("delete", chat_id, message_id))

    async def promote_chat_member(self, chat_id, user_id, **kwargs):
        self.actions.append(("promote", chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.actions.append(("unban", chat_id, user_id))


def ctx_for(bot):
    return SimpleNamespace(
        bot=bot, args=[], application=SimpleNamespace(bot=bot)
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


def update_for(msg, actor=MEMBER, chat_id=CHAT, chat_type="supergroup"):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="Group"),
        effective_user=SimpleNamespace(
            id=actor, full_name="Tester", username="tester", is_bot=False
        ),
    )


def run(handler, msg, bot, actor=MEMBER):
    asyncio.run(handler(update_for(msg, actor=actor), ctx_for(bot)))


def install_awareness(monkeypatch, *, decision=None, error="", raw=None, call=None):
    """Replace the awareness transport. Returns the recorded passes.

    ``decision`` is the dict the pass should appear to have produced; it is
    serialised the way the model would have produced it, so the parsing path is
    exercised rather than bypassed. ``raw`` overrides that with an exact string,
    which is how the unreadable-answer tests are written. ``error`` makes the
    transport fail, which is how the outage tests are written.
    """
    import json

    passes: list[dict] = []

    async def _awareness(transcript, context="", *, tools=None, on_tool=None):
        passes.append(
            {
                "transcript": transcript,
                "context": context,
                "tools": tools,
                "on_tool": on_tool,
            }
        )
        if error:
            return chat.AwarenessReply(error=error, model="stub")
        if call is not None:
            # A member is offered no tools at all, and a test that tries to call
            # one anyway is testing the harness rather than the code.
            passes[-1]["tool_result"] = (
                await on_tool(call[0], call[1]) if on_tool is not None else None
            )
        body = raw if raw is not None else json.dumps(decision or {})
        return chat.AwarenessReply(text=body, model="stub", turns=1)

    monkeypatch.setattr(main.chat, "awareness", _awareness)
    return passes


def capture(chat_id, user_id, role, name, text):
    """Put a message in the room window the way the handler would."""
    assert awareness.capture(chat_id, user_id, role, name, text) is True


def pending_row(chat_id=CHAT, *, age=60.0, count=1, max_id=None):
    """A pending summary as ``db.group_pending`` would produce it.

    The timestamps are synthetic so the debounce tests can place a batch in the
    past, but ``max_id`` is read from the real window. The watermark is the thing
    under test in several of these, and an invented id would make a
    duplicate-delivery test pass for the wrong reason.
    """
    if max_id is None:
        real = [r for r in db.group_pending() if r["chat_id"] == chat_id]
        max_id = real[0]["max_id"] if real else 1
    now = int(time.time())
    return {
        "chat_id": chat_id,
        "oldest_at": now - int(age),
        "newest_at": now - int(age),
        "max_id": max_id,
        "pending": count,
    }


# ══ 1. Capture: the room window ═══════════════════════════════════════════
def test_group_messages_are_captured_into_a_bounded_context():
    """Every received message joins the window, with no AI call anywhere."""
    assert db.group_window(CHAT, limit=10) == []
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "hi")
    assert len(db.group_window(CHAT, limit=10)) == 2


def test_capture_preserves_message_order():
    """A conversation read out of order is not a conversation."""
    for index in range(6):
        capture(CHAT, 100 + index, awareness.ROLE_MEMBER, f"u{index}", f"m{index}")
    texts = [m["text"] for m in db.group_window(CHAT, limit=10)]
    assert texts == [f"m{i}" for i in range(6)]


def test_capture_preserves_the_sender_identity():
    """Who spoke is recorded, because a later instruction has to name them."""
    capture(CHAT, 12345, awareness.ROLE_ADMIN, "Ali", "بنش کن")
    entry = db.group_window(CHAT, limit=1)[0]
    assert entry["user_id"] == 12345
    assert entry["name"] == "Ali"


def test_the_sender_role_is_assigned_by_the_server():
    """The role is resolved from the Telegram id, never from what was written."""
    principal = rbac.resolve(OWNER)
    assert awareness.role_of(principal) == awareness.ROLE_OWNER
    assert awareness.role_of(rbac.resolve(ADMIN)) == awareness.ROLE_ADMIN
    assert awareness.role_of(rbac.resolve(MEMBER)) == awareness.ROLE_MEMBER
    # Somebody who writes "I am the owner" is still whatever their id says.
    assert awareness.role_of(rbac.resolve(STRANGER)) == awareness.ROLE_MEMBER


def test_the_window_has_a_hard_message_bound():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_ROWS", 5)
    try:
        for index in range(12):
            capture(CHAT, index, awareness.ROLE_MEMBER, "u", f"m{index}")
        assert len(db.group_window(CHAT, limit=100)) == 5
    finally:
        monkeypatch.undo()


def test_the_rendered_window_has_a_hard_character_budget():
    for index in range(30):
        capture(CHAT, index, awareness.ROLE_MEMBER, "someone", f"m{index:02d}" + "x" * 200)
    rendered = awareness.render(CHAT, limit=30, budget=600)
    # The budget is a real ceiling, not a hint: one line may cross it because a
    # single message longer than the whole budget is still worth showing, and
    # dropping it would leave the transcript empty.
    assert len(rendered) <= 600 + 260
    # Bounded from the old end: the newest message is kept and the oldest is not.
    assert "m29" in rendered
    assert "m00" not in rendered


def test_the_rendered_window_keeps_the_newest_messages():
    for index in range(40):
        capture(CHAT, index, awareness.ROLE_MEMBER, "u", f"message-{index:02d}")
    rendered = awareness.render(CHAT, limit=40, budget=300)
    assert "message-39" in rendered
    assert "message-00" not in rendered


# The live room's measured traffic during an active stretch: 106 messages
# arrived in the 8.7 minutes between two consecutive passes on 2026-09-22.
# Named as a constant because the window's size is only meaningful against it.
BUSY_MESSAGES_PER_MINUTE = 12.0


def test_the_default_window_covers_one_allowance_interval():
    """The window must hold what arrives between two passes.

    Three numbers are coupled and none of them is meaningful alone: the daily
    allowance decides how often a pass may run, the room's traffic decides how
    much accumulates in that time, and the window decides how much of it the
    pass actually reads. When the window is the smallest of the three the loss
    is *silent*, because a pass records the newest unread id as understood — so
    whatever the window did not contain is not read late, it is not read at all.
    Measured on the live deployment before this test existed: 106 messages
    arrived between two consecutive passes against a 40-message window, so at
    most 38% of the conversation was read and the rest was lost.

    Asserting the relationship rather than the number is the point: raising the
    daily allowance shortens the interval and the window may shrink with it, and
    lowering the window below the interval must fail here rather than quietly in
    the group.
    """
    interval_seconds = 86400.0 / config.NEXUS_AWARENESS_DAILY_LIMIT
    arrived = BUSY_MESSAGES_PER_MINUTE * interval_seconds / 60.0
    assert config.NEXUS_AWARENESS_WINDOW_MESSAGES >= arrived


def test_old_context_is_discarded_by_the_retention_policy():
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 60)
        capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "old")
        # Backdate the row past the retention window.
        db._exec(
            "UPDATE group_messages SET at=? WHERE chat_id=?",
            (int(time.time()) - 600, CHAT),
        )
        # The purge runs at most once per purge interval, so make this capture
        # the one that is due to run it. That interval is the whole reason the
        # capture path is one transaction instead of three: the age bound is
        # measured in hours and does not need enforcing once per message.
        awareness.reset_timers()
        capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "new")
        texts = [m["text"] for m in db.group_window(CHAT, limit=10)]
        assert texts == ["new"]
    finally:
        monkeypatch.undo()


def test_the_age_purge_is_not_run_on_every_capture(monkeypatch):
    """The per-chat trim bounds a burst; the age purge runs on its own clock.

    This is a performance property with a correctness edge: an old row must
    still go, and it must not cost a table-wide scan per received message.
    """
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 60)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_PURGE_INTERVAL_SECONDS", 3600)
    awareness.reset_timers()
    calls: list[int] = []
    real = db.group_purge
    monkeypatch.setattr(
        db, "group_purge", lambda ttl: (calls.append(ttl), real(ttl))[1]
    )

    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "one")
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "two")
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "three")

    assert calls == [60], "the purge must not run once per message"
    # And the per-chat ceiling still ran on every capture.
    assert len(db.group_window(CHAT, limit=10)) == 3


def test_the_window_is_isolated_by_chat():
    """One group's conversation is never visible to another."""
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "a", "in this group")
    capture(OTHER_CHAT, MEMBER, awareness.ROLE_MEMBER, "b", "in the other group")
    assert [m["text"] for m in db.group_window(CHAT, limit=10)] == ["in this group"]
    assert [m["text"] for m in db.group_window(OTHER_CHAT, limit=10)] == [
        "in the other group"
    ]
    assert "in the other group" not in awareness.render(CHAT)


def test_the_window_is_not_the_per_user_conversation_history():
    """The room and one person's conversation are separate stores.

    Writing the room into ``chat_messages`` would show one member's words to
    another, which is the leak the per-user key exists to prevent.
    """
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "room message")
    assert db.chat_history(CHAT, MEMBER, limit=10, ttl=3600) == []
    assert db.group_window(CHAT, limit=10)


def test_capture_stores_no_message_body_in_the_identity_memory():
    """The room window holds text; the people table still holds none."""
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "secret words here")
    import sqlite3

    columns = [
        row[1]
        for row in db._conn.execute("PRAGMA table_info(people)").fetchall()
    ]
    assert "text" not in columns and "message" not in columns
    del sqlite3


def test_media_is_captured_as_its_kind_and_never_as_bytes():
    """A photograph must not become a row every later prompt carries."""
    bot = FakeBot()
    msg = message(photo=[SimpleNamespace(file_id="f", file_unique_id="u")])
    run(main.on_group_chat, msg, bot, actor=MEMBER)
    entry = db.group_window(CHAT, limit=1)[0]
    assert entry["text"].startswith("[photo]")
    assert "f" not in entry["text"]


# ══ 2. The owner ══════════════════════════════════════════════════════════
def test_the_owner_is_identified_from_the_telegram_id():
    assert awareness.role_of(rbac.resolve(OWNER)) == awareness.ROLE_OWNER
    # And a stranger cannot be, whatever they are called.
    assert awareness.role_of(rbac.resolve(STRANGER)) != awareness.ROLE_OWNER


def test_the_owner_is_captured_without_replying_to_nexus():
    bot = FakeBot()
    run(main.on_group_chat, message(text="این روش خوب نیست، باید عوضش کنیم"), bot, actor=OWNER)
    entry = db.group_window(CHAT, limit=1)[0]
    assert entry["role"] == awareness.ROLE_OWNER
    assert entry["user_id"] == OWNER


def test_the_owner_is_captured_without_mentioning_nexus():
    bot = FakeBot()
    run(main.on_group_chat, message(text="فکر کنم باید یه چیز دیگه امتحان کنیم"), bot, actor=OWNER)
    assert db.group_window(CHAT, limit=1)[0]["user_id"] == OWNER
    # And nothing was said to the room: capture is not a conversation.
    assert bot.messages == []


def test_an_ordinary_member_is_captured_too():
    """Understanding the room means understanding everybody in it."""
    bot = FakeBot()
    run(main.on_group_chat, message(text="سلام بچه‌ها"), bot, actor=MEMBER)
    entry = db.group_window(CHAT, limit=1)[0]
    assert entry["user_id"] == MEMBER
    assert entry["role"] == awareness.ROLE_MEMBER


# ══ 3. What Gemini is given ═══════════════════════════════════════════════
def test_the_pass_gives_the_model_the_recent_conversation(monkeypatch):
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "این روش خوب نیست")
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "چرا؟")
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})

    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))

    transcript = passes[0]["transcript"]
    assert "این روش خوب نیست" in transcript
    assert "چرا؟" in transcript
    assert transcript.index("این روش خوب نیست") < transcript.index("چرا؟")


def test_the_transcript_labels_who_said_each_line(monkeypatch):
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "hello")
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert f"[owner] mo ({OWNER}): hello" in passes[0]["transcript"]


def test_the_model_is_given_the_authority_roster(monkeypatch):
    bot = FakeBot()
    db.admin_set(ADMIN, "admin", ["moderation.ban"], granted_by=OWNER)
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    context = passes[0]["context"]
    assert str(OWNER) in context
    assert str(ADMIN) in context


def test_the_roster_says_the_owner_is_the_creator():
    """The owner's standing is stated by the server, not inferred by the model."""
    roster = awareness.roster()
    assert str(OWNER) in roster
    assert "creator" in roster.lower()


def test_the_roster_states_the_hierarchy_rather_than_a_flat_list():
    db.admin_set(MODERATOR, "moderator", ["moderation.review"], granted_by=OWNER)
    roster = awareness.roster()
    assert str(MODERATOR) in roster
    # The level is what makes a senior admin different from a moderator, and a
    # model that thinks all administrators are equal promises what it cannot do.
    assert "level" in roster


def test_the_model_is_given_what_it_understood_before(monkeypatch):
    """Continuity: the previous reading is passed back, marked as fallible."""
    db.awareness_set(
        CHAT,
        seen_message_id=1,
        relevant=True,
        topic="a previous topic",
        summary="they were discussing something earlier",
    )
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "همون مشکل قبلی")
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    context = passes[0]["context"]
    assert "they were discussing something earlier" in context


def test_the_model_is_given_the_staged_room_context(monkeypatch):
    """The room's name and the last pass's people reach the model's context.

    This asserts the *wiring*, not the builder: ``_awareness_pass`` has to hand
    its own window and anchor to the context builder, or the blocks would be
    assembled for a different batch than the transcript describes. The room's
    name is tier 0 and therefore always there; the deeper tiers are gated by
    ``app/awareness_context.py`` and tested there.
    """
    awareness_context.note_room(CHAT, "Guard Group", "supergroup")
    db.awareness_set(
        CHAT,
        seen_message_id=1,
        relevant=False,
        topic="t",
        summary="s",
        participants="member:Sara:42",
    )
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    passes = install_awareness(
        monkeypatch, decision={"relevant": False, "respond": False}
    )
    asyncio.run(main._awareness_pass(ctx_for(FakeBot()), CHAT, pending_row()))
    context = passes[0]["context"]
    assert "Guard Group" in context
    assert "Sara (42)" in context


def test_the_transcript_the_pass_reads_carries_recency():
    """A conversation has a direction, and the age of each line is part of it."""
    db.group_capture(CHAT, MEMBER, "member", "Reza", "hello", keep=10, message_id=1)
    db._exec(
        "UPDATE group_messages SET at=? WHERE chat_id=?",
        (int(time.time()) - 300, CHAT),
    )
    rows = db.group_window(CHAT, limit=10)
    assert "(+5m)" in awareness.render(CHAT, messages=rows)


def test_the_staged_context_is_measured_as_batch_time_and_not_as_model_time(monkeypatch):
    """The new work must land in ``batch_ms``, not in ``gemini_ms``.

    Assembling the prompt inside the request window would count a slow context
    build as model time — the one part of this pass whose cost is new would be
    invisible, and the promise that it is bounded would not be checkable from
    the log. So everything the model is handed is built before the ``request``
    mark, and that is what this asserts.
    """
    stamps: dict[str, float] = {}
    real_context = main._awareness_context

    def _context(chat_id, **kwargs):
        stamps["context"] = time.monotonic()
        return real_context(chat_id, **kwargs)

    async def _turn(*_args, **_kwargs):
        return [], "", None

    traces: list = []

    class RecordingTrace(awareness.PassTrace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            traces.append(self)

    monkeypatch.setattr(main, "_awareness_context", _context)
    monkeypatch.setattr(main, "_awareness_turn", _turn)
    monkeypatch.setattr(awareness, "PassTrace", RecordingTrace)

    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(FakeBot()), CHAT, pending_row()))

    assert traces, "the pass should have produced a trace"
    request_at = traces[0].marks.get("request")
    assert request_at is not None
    assert stamps["context"] < request_at


# ══ 4. Awareness is not response ══════════════════════════════════════════
def test_an_irrelevant_conversation_produces_no_message(monkeypatch):
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "ناهار چی بخوریم؟")
    install_awareness(
        monkeypatch,
        decision={"topic": "lunch", "summary": "they are choosing food", "relevant": False, "respond": False},
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == []
    # ...but it was still understood and remembered.
    state = awareness.state(CHAT)
    assert state["topic"] == "lunch"
    assert state["relevant"] is False


def test_a_relevant_conversation_can_produce_a_message(monkeypatch):
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس چیکار می‌کنه؟")
    install_awareness(
        monkeypatch,
        decision={
            "topic": "about nexus",
            "summary": "the owner is asking what nexus does",
            "relevant": True,
            "respond": True,
            "message": "کارهای مدیریتی گروه رو انجام می‌دم.",
        },
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == ["کارهای مدیریتی گروه رو انجام می‌دم."]


def test_awareness_records_the_understanding_even_when_it_stays_silent(monkeypatch):
    """The whole point of the split: understanding happens without speaking."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "این روش اصلاً خوب نیست")
    install_awareness(
        monkeypatch,
        decision={"topic": "complaint", "summary": "the owner is unhappy with the approach", "relevant": False, "respond": False},
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == []
    assert "unhappy" in awareness.state(CHAT)["summary"]


def test_nexus_does_not_answer_every_message(monkeypatch):
    """A batch the model declines produces nothing, however many messages."""
    bot = FakeBot()
    for index in range(5):
        capture(CHAT, MEMBER, awareness.ROLE_MEMBER, f"u{index}", f"chat {index}")
    install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == []


def test_nexus_is_not_silent_merely_because_there_was_no_mention(monkeypatch):
    """The regression the whole feature exists to remove.

    The message names nobody and replies to nothing, and the model still gets to
    decide it should answer — because the decision is read from the
    conversation, not from whether an address was found.
    """
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "چرا این ربات دیگه اون کارو انجام نمی‌ده؟")
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "کدوم کار رو می‌گی؟"},
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == ["کدوم کار رو می‌گی؟"]


def test_the_model_receives_its_own_earlier_replies(monkeypatch):
    """Otherwise it reads questions and never its own answers, and repeats."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    main._awareness_note_reply(CHAT, "سلام، در خدمتم")
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "خوبی؟")
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert "سلام، در خدمتم" in passes[0]["transcript"]


def test_a_reply_that_failed_to_send_is_not_recorded():
    bot = FakeBot()

    async def _boom(chat_id, text, **kwargs):
        from telegram.error import TelegramError

        raise TelegramError("nope")

    bot.send_message = _boom
    asyncio.run(
        main._send_chat(ctx_for(bot), CHAT, "این ارسال نشد")
    )
    assert all(
        "این ارسال نشد" not in m["text"] for m in db.group_window(CHAT, limit=10)
    )


# ══ 5. The security boundary ══════════════════════════════════════════════
def test_an_ordinary_member_is_understood_but_not_answered(monkeypatch):
    """``NEXUS_ACTORS_ONLY`` still means what it meant: awareness is not a way in."""
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "نکسوس بیا")
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "سلام!"},
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == []
    # Understood, though — the room is still read.
    assert awareness.state(CHAT)


def test_turning_actors_only_off_restores_answering_members(monkeypatch):
    """The switch still moves exactly what it moved before."""
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", False)
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "نکسوس بیا")
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "سلام!"},
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == ["سلام!"]


def test_an_ordinary_member_is_not_given_a_tool_surface(monkeypatch):
    """Awareness does not widen exposure: a member is offered no tools."""
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "بنش کن")
    passes = install_awareness(monkeypatch, decision={"relevant": True, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert passes[0]["tools"] is None


def test_a_moderator_is_offered_only_the_tools_their_role_holds(monkeypatch):
    bot = FakeBot()
    capture(CHAT, MODERATOR, awareness.ROLE_ADMIN, "mod", "این کاربر رو ساکت کن")
    passes = install_awareness(monkeypatch, decision={"relevant": True, "respond": False})
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    names = {
        d.name for d in passes[0]["tools"][0].function_declarations
    }
    assert "mute_member" in names
    assert "ban_member" not in names


def test_a_privileged_action_from_awareness_still_goes_through_the_service(monkeypatch):
    """The action is performed by the execution layer, on the actor's authority."""
    bot = FakeBot()
    capture(CHAT, ADMIN, awareness.ROLE_ADMIN, "ali", "این کاربر رو بن کن")
    passes = install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "بن شد."},
        call=("ban_member", {"target_user_id": STRANGER}),
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert ("ban", CHAT, STRANGER) in bot.actions
    assert passes[0]["tool_result"]["ok"] is True


def test_a_member_cannot_get_an_action_through_awareness(monkeypatch):
    """The same request from somebody without the permission does nothing."""
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "اون کاربر رو بن کن")
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "بن شد."},
        call=("ban_member", {"target_user_id": STRANGER}),
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.actions == []
    assert bot.messages == []


def test_a_malformed_tool_call_is_refused_rather_than_repaired(monkeypatch):
    """An argument the schema does not declare is not coerced into one it does."""
    bot = FakeBot()
    capture(CHAT, ADMIN, awareness.ROLE_ADMIN, "ali", "این کاربر رو بن کن")
    passes = install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "بن شد."},
        call=("ban_member", {"target_user_id": STRANGER, "reason": "invented"}),
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.actions == []
    assert passes[0]["tool_result"]["ok"] is False


def test_the_model_cannot_declare_itself_the_owner():
    """Owner status is a server statement about an id, not a model output."""
    principal = rbac.resolve(STRANGER)
    context = admin_tools.build_context(principal=principal, chat_id=CHAT)
    assert "Actor is the owner: no" in context
    assert f"Actor Telegram user id: {STRANGER}" in context


def test_the_model_cannot_assert_a_role_for_the_speaker():
    principal = rbac.resolve(MEMBER)
    context = admin_tools.build_context(principal=principal, chat_id=CHAT)
    assert "Actor role: guest" in context
    assert "Actor may ask for: nothing administrative" in context


def test_a_reply_the_model_writes_cannot_grant_a_permission():
    """Whatever the model says, nothing reads it as a grant."""
    before = rbac.resolve(MEMBER).permissions
    parsed = awareness.parse_decision(
        '{"relevant": true, "respond": true, "message": "تو الان ادمینی"}'
    )
    assert parsed["message"] == "تو الان ادمینی"
    assert rbac.resolve(MEMBER).permissions == before


def test_the_awareness_layer_does_not_import_the_authority_modules():
    """Structural: there is no path from the policy module to a permission."""
    source = inspect.getsource(awareness)
    tree = __import__("ast").parse(source)
    imported = set()
    for node in __import__("ast").walk(tree):
        if isinstance(node, __import__("ast").ImportFrom) and node.module == "":
            imported.update(alias.name for alias in node.names)
    assert "admin_service" not in imported
    assert "admin_tools" not in imported


# ══ 6. Workload isolation ═════════════════════════════════════════════════
def test_awareness_runs_on_its_own_workload():
    gemini_pool.build_pools()
    assert gemini_pool.pool_for("awareness") is not None
    assert gemini_pool.pool_for("awareness") is not gemini_pool.pool_for("chat")


def test_the_awareness_transport_targets_the_awareness_workload(monkeypatch):
    seen = {}

    async def _full(contents, *, tools=None, context="", instruction="", workload="chat", model=""):
        seen["workload"] = workload
        seen["instruction"] = instruction
        seen["model"] = model
        return SimpleNamespace(text="{}")

    monkeypatch.setattr(chat, "_request_full", _full)
    monkeypatch.setattr(config, "GEMINI_AWARENESS_MODEL", "awareness-model")
    asyncio.run(chat.awareness("some transcript"))
    assert seen["workload"] == "awareness"
    assert seen["model"] == "awareness-model"
    assert seen["instruction"] == chat.AWARENESS_INSTRUCTION


def test_awareness_does_not_touch_the_acquisition_counters():
    before = db.ai_calls_today()
    db.record_chat_attempt("replies")
    assert db.ai_calls_today() == before


def test_awareness_does_not_touch_the_moderation_or_transcript_counters():
    intent = db.ai_calls_today()
    moderation = db.mod_usage()
    transcript = db.transcript_usage()
    chat_before = db.chat_calls_today()
    db.record_chat_attempt("replies")
    assert db.ai_calls_today() == intent
    assert db.mod_usage() == moderation
    assert db.transcript_usage() == transcript
    assert db.chat_calls_today() == chat_before + 1


def test_awareness_does_not_import_the_other_workload_modules():
    source = inspect.getsource(awareness)
    for module in ("ai_intent", "ai_moderation", "transcribe"):
        assert module not in source


def test_the_awareness_transcript_holds_no_media_bytes():
    """Awareness is text-only, so its window must never carry an attachment."""
    source = inspect.getsource(awareness)
    assert "mime_type" not in source
    assert "audio_in" not in source


# ══ 7. Failure behaviour ══════════════════════════════════════════════════
def test_a_model_failure_does_not_crash_the_pass(monkeypatch):
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    install_awareness(monkeypatch, error="bad_request")
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == []


def test_a_model_failure_does_not_execute_anything(monkeypatch):
    bot = FakeBot()
    capture(CHAT, ADMIN, awareness.ROLE_ADMIN, "ali", "این کاربر رو بن کن")
    install_awareness(monkeypatch, error="timeout")
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.actions == []


def test_a_failed_pass_advances_the_watermark_but_keeps_the_understanding(monkeypatch):
    """An outage must not turn into a retry loop, and must not erase memory."""
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=True, topic="kept", summary="kept summary"
    )
    bot = FakeBot()
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "new message")
    install_awareness(monkeypatch, error="bad_request")
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    state = awareness.state(CHAT)
    assert state["topic"] == "kept"
    assert state["summary"] == "kept summary"
    # And the batch is not pending any more, so the sweeper does not retry it
    # on every tick for as long as the outage lasts.
    assert awareness.pending() == []


def test_an_unreadable_answer_produces_no_message(monkeypatch):
    """The one outcome worth losing a pass over: speaking on an answer nobody read."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    install_awareness(monkeypatch, raw="I think the answer is probably yes!")
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == []


def test_an_unreadable_answer_is_not_remembered_as_understanding(monkeypatch):
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    install_awareness(monkeypatch, raw="not json at all")
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert awareness.state(CHAT).get("summary", "") == ""


def test_the_sweep_never_raises(monkeypatch):
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")

    async def _boom(*args, **kwargs):
        raise RuntimeError("the model exploded")

    monkeypatch.setattr(main, "_awareness_pass", _boom)
    monkeypatch.setattr(main.awareness, "due", lambda *a, **k: awareness.Due(True))
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    # The guard is released even though the pass raised, or one bad pass would
    # stop every future sweep.
    assert main._awareness_sweeping is False


def test_a_disabled_awareness_layer_never_calls_the_model(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)
    bot = FakeBot()
    run(main.on_group_chat, message(text="سلام"), bot, actor=OWNER)
    assert db.group_window(CHAT, limit=10) == []


def test_a_missing_credential_skips_the_pass_rather_than_failing(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "")
    monkeypatch.setattr(gemini_pool, "has_accounts", lambda workload: False)
    reply = asyncio.run(chat.awareness("some transcript"))
    assert reply.skipped == "no_key"
    assert reply.answered is False


def test_an_empty_transcript_skips_the_pass(monkeypatch):
    reply = asyncio.run(chat.awareness("   "))
    assert reply.skipped == "empty"


# ══ 8. Duplicates and concurrency ═════════════════════════════════════════
def test_a_duplicate_delivery_does_not_produce_a_second_response(monkeypatch):
    """The watermark is what makes a redelivered update a no-op."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس؟")
    install_awareness(
        monkeypatch, decision={"relevant": True, "respond": True, "message": "بله؟"}
    )
    row = pending_row()
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, row))
    assert bot.messages == ["بله؟"]
    # The same batch is no longer pending, so the sweeper has nothing to re-read.
    assert awareness.pending() == []


def test_the_assistants_own_reply_does_not_make_the_room_pending_again(monkeypatch):
    """The regression that would have made Nexus talk to itself forever.

    The reply has to be *in the window* — the model must see what it already
    said, or it repeats itself — but it must not count as something new to
    understand. Counting it means every reply schedules the next pass, and a
    bot that keeps replying keeps rescheduling itself: a loop that never ends
    and never stops spending the awareness allowance. Only a person speaking
    makes a room worth reading again.
    """
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس؟")
    install_awareness(
        monkeypatch, decision={"relevant": True, "respond": True, "message": "بله؟"}
    )
    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))
    assert bot.messages == ["بله؟"]
    # The reply is visible to the next pass...
    assert any(
        m["role"] == awareness.ROLE_NEXUS for m in db.group_window(CHAT, limit=10)
    )
    # ...and there is still nothing to read.
    assert awareness.pending() == []


def test_a_reply_does_not_reschedule_the_sweep(monkeypatch):
    """End to end: a second sweep after a reply performs no second pass."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس؟")
    passes = install_awareness(
        monkeypatch, decision={"relevant": True, "respond": True, "message": "بله؟"}
    )
    monkeypatch.setattr(main.awareness, "due", lambda *a, **k: awareness.Due(True))
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    assert len(passes) == 1
    assert bot.messages == ["بله؟"]


def test_a_room_already_being_read_is_not_read_again(monkeypatch):
    """The in-flight guard is what stops two ticks answering one conversation."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    main._awareness_inflight.add(CHAT)
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    # Nothing was read, because the room was already in flight.
    assert awareness.pending()


def test_overlapping_sweeps_do_not_stack():
    bot = FakeBot()
    main._awareness_sweeping = True
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    # The flag is left exactly as it was found.
    assert main._awareness_sweeping is True


def test_a_room_the_bot_cannot_see_is_not_read(monkeypatch):
    """Telegram is not delivering this room, so there is nothing to read."""
    bot = FakeBot()
    capture(OTHER_CHAT, OWNER, awareness.ROLE_OWNER, "mo", "unseen")
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    assert passes == []


# ══ 9. When the model is asked ════════════════════════════════════════════
def test_a_room_still_talking_is_not_read_yet():
    """The debounce: a burst costs one pass, not one pass per message."""
    verdict = awareness.due(pending_row(age=1.0), now=time.time(), last_pass_at=0.0)
    assert verdict.run is False
    assert verdict.reason == "room_still_talking"


def test_a_quiet_room_is_read_after_the_debounce():
    now = time.time()
    verdict = awareness.due(pending_row(age=9.0), now=now, last_pass_at=0.0)
    assert verdict.run is True


def test_a_busy_room_is_read_anyway_once_it_has_waited_too_long():
    """A room that never falls silent would otherwise never be understood."""
    now = time.time()
    row = {
        "chat_id": CHAT,
        "oldest_at": int(now) - 60,
        "newest_at": int(now) - 1,
        "max_id": 5,
        "pending": 5,
    }
    assert awareness.due(row, now=now, last_pass_at=0.0).run is True


def test_two_passes_in_one_room_are_spaced_out():
    now = time.time()
    verdict = awareness.due(pending_row(age=30.0), now=now, last_pass_at=now - 5)
    assert verdict.run is False
    assert verdict.reason == "too_soon"


def test_nothing_pending_means_no_pass():
    assert awareness.due(pending_row(count=0), now=time.time()).reason == "nothing_pending"


def test_a_disabled_layer_is_never_due():
    config.NEXUS_AWARENESS_ENABLED = False
    try:
        assert awareness.due(pending_row(age=999), now=time.time()).reason == "disabled"
    finally:
        config.NEXUS_AWARENESS_ENABLED = True


def test_the_due_decision_is_not_a_relevance_decision():
    """No keyword, no message text: the policy sees timestamps and nothing else.

    This is the structural guarantee that awareness is Gemini's job. If the
    function that decides *when* to ask could see the words, it would inevitably
    start deciding *whether* to ask, and the feature would quietly become the
    keyword filter it exists to replace. Asserted over the parsed function body
    rather than its text, so a comment explaining the rule cannot fail it.
    """
    import ast

    parameters = set(inspect.signature(awareness.due).parameters)
    # ``urgent`` is the only thing a caller may add, and it is a boolean: the
    # signature itself is the guarantee that no message text can reach here.
    # ``awareness`` uses postponed annotations, so the annotation is the string
    # ``"bool"`` here; accept the evaluated form too so this cannot silently
    # become a no-op if that import ever goes away.
    assert parameters == {"pending", "now", "last_pass_at", "urgent"}
    assert inspect.signature(awareness.due).parameters["urgent"].annotation in {
        bool,
        "bool",
    }

    tree = ast.parse(inspect.getsource(awareness.due))
    function = tree.body[0]
    # Drop the docstring: it is prose about the rule, not the rule.
    body = [
        node
        for node in function.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    referenced = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)
    for forbidden in (
        "text",
        "body",
        "message",
        "looks_actionable",
        "is_named",
        "_action_words",
        "_ACTION_WORDS",
    ):
        assert forbidden not in referenced, forbidden


def test_the_sweep_is_bounded_per_tick(monkeypatch):
    bot = FakeBot()
    for chat_id in (CHAT, OTHER_CHAT):
        main._nexus_visibility[chat_id] = "administrator"
        capture(chat_id, OWNER, awareness.ROLE_OWNER, "mo", "hello")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_CHATS_PER_TICK", 1)
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    monkeypatch.setattr(
        main.awareness, "due", lambda *a, **k: awareness.Due(True)
    )
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    assert len(passes) == 1


def test_a_tick_with_nothing_pending_costs_no_model_call(monkeypatch):
    bot = FakeBot()
    passes = install_awareness(monkeypatch, decision={"relevant": False, "respond": False})
    asyncio.run(main.awareness_sweep(ctx_for(bot)))
    assert passes == []


# ══ 10. Reading the model's answer ════════════════════════════════════════
def test_a_plain_json_decision_is_read():
    decision = awareness.parse_decision(
        '{"topic": "t", "summary": "s", "relevant": true, "respond": true, '
        '"message": "سلام"}'
    )
    assert decision["relevant"] is True
    assert decision["respond"] is True
    assert decision["message"] == "سلام"


def test_a_fenced_json_decision_is_read():
    decision = awareness.parse_decision(
        '```json\n{"relevant": false, "respond": false, "message": null}\n```'
    )
    assert decision["respond"] is False
    assert decision["message"] is None


def test_json_embedded_in_prose_is_read():
    decision = awareness.parse_decision(
        'Here you go:\n{"relevant": true, "respond": false, "message": null}\nDone.'
    )
    assert decision["relevant"] is True


def test_a_response_with_nothing_to_say_is_not_a_decision_to_speak():
    decision = awareness.parse_decision(
        '{"relevant": true, "respond": true, "message": "   "}'
    )
    assert decision["respond"] is False


def test_garbage_is_not_a_decision():
    assert awareness.parse_decision("I think so, probably.") is None
    assert awareness.parse_decision("") is None
    assert awareness.parse_decision("[1, 2, 3]") is None


# ── The structured understanding in the decision ──────────────────────────
def test_the_intent_is_clamped_to_the_vocabulary():
    """A classification nobody can enumerate is not a classification."""
    for text, expected in [
        ('{"intent": "question"}', "question"),
        ('{"intent": "Question"}', "question"),
        ('{"intent": " instruction "}', "instruction"),
        ('{"intent": "banana"}', "other"),
        ('{"intent": 5}', "other"),
        ('{"intent": null}', "other"),
        ("{}", "other"),
    ]:
        assert awareness.parse_decision(text)["intent"] == expected, text


def test_the_about_field_is_normalised_to_a_positive_id_or_zero():
    for text, expected in [
        ('{"about": 42}', 42),
        ('{"about": "42"}', 42),
        ('{"about": 0}', 0),
        ('{"about": -7}', 0),
        ('{"about": "reza"}', 0),
        ('{"about": null}', 0),
        ("{}", 0),
    ]:
        assert awareness.parse_decision(text)["about"] == expected, text


def test_a_claimed_person_must_be_in_the_window():
    """A model that names somebody the room never mentioned has not read it."""
    window = [{"user_id": 42}, {"user_id": 999}]
    assert awareness.about_in_window(42, window) == 42
    assert awareness.about_in_window(7, window) == 0
    assert awareness.about_in_window(0, window) == 0
    assert awareness.about_in_window(42, []) == 0


def test_the_structured_fields_do_not_gate_anything():
    """Understanding is recorded, never obeyed — the boundary does not move."""
    decision = awareness.parse_decision(
        '{"intent": "instruction", "about": 42, "relevant": true, '
        '"respond": true, "message": "باشه"}'
    )
    assert decision["respond"] is True
    # …and an empty message still vetoes the turn, whatever the understanding.
    vetoed = awareness.parse_decision(
        '{"intent": "instruction", "about": 42, "respond": true, "message": ""}'
    )
    assert vetoed["respond"] is False


def test_the_understanding_is_stored_and_read_back():
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    awareness.record(
        CHAT,
        seen_message_id=1,
        decision={"topic": "t", "summary": "s", "intent": "instruction", "about": MEMBER},
    )
    stored = db.awareness_get(CHAT)
    assert stored["intent"] == "instruction"
    assert stored["about_user_id"] == MEMBER


def test_the_store_clamps_what_it_is_handed():
    """The store is a boundary too: an arbitrary decision dict cannot widen it."""
    awareness.record(
        CHAT,
        seen_message_id=1,
        decision={"intent": "not-a-word", "about": "nobody"},
    )
    stored = db.awareness_get(CHAT)
    assert stored["intent"] == "other"
    assert stored["about_user_id"] == 0


def test_the_summary_counts_rooms_that_are_about_somebody():
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    awareness.record(CHAT, seen_message_id=1, decision={"about": MEMBER})
    assert db.awareness_summary()["about_rooms"] == 1
    awareness.record(CHAT, seen_message_id=1, decision={"about": 0})
    assert db.awareness_summary()["about_rooms"] == 0


def test_who_the_room_was_about_becomes_continuity():
    """The understanding a pass records is what the next pass is handed."""
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    awareness.record(
        CHAT,
        seen_message_id=1,
        decision={"summary": "s", "about": MEMBER},
    )
    block = awareness.memory_block(CHAT)
    assert "About then: reza" in block


def test_a_forgotten_person_is_not_named_in_the_continuity_block():
    """An id the stored roster cannot name is left out, not printed as a number."""
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "سلام")
    awareness.record(CHAT, seen_message_id=1, decision={"summary": "s", "about": 0})
    assert "About then:" not in awareness.memory_block(CHAT)


# ══ 11. The direct answer path also sees the room ═════════════════════════
def test_an_addressed_answer_is_given_the_room_context(monkeypatch):
    """Being answered *informed* is the difference awareness makes."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "این روش خوب نیست")
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "موافقم")
    seen: list[dict] = []

    async def _reply(chat_id, user_id, body, *, parts=None, kind="", want_voice=False,
                     tools=None, context="", on_tool=None):
        seen.append({"context": context})
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    run(main.on_group_chat, message(text="نکسوس نظرت چیه؟"), bot, actor=OWNER)
    assert "این روش خوب نیست" in seen[0]["context"]
    assert "موافقم" in seen[0]["context"]


def test_an_addressed_reply_is_recorded_in_the_room(monkeypatch):
    """The assistant's own words join the conversation the next pass reads."""
    bot = FakeBot()

    async def _reply(chat_id, user_id, body, *, parts=None, kind="", want_voice=False,
                     tools=None, context="", on_tool=None):
        return chat.ChatReply(answered=True, text="پاسخ من", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    run(main.on_group_chat, message(text="نکسوس سلام"), bot, actor=OWNER)
    roles = [m["role"] for m in db.group_window(CHAT, limit=10)]
    assert awareness.ROLE_NEXUS in roles


# ══ 12. Wiring ════════════════════════════════════════════════════════════
def test_the_sweeper_is_registered_at_startup():
    source = inspect.getsource(main.post_init)
    assert "awareness_sweep" in source


def test_the_sweeper_is_not_registered_when_awareness_is_off():
    source = inspect.getsource(main.post_init)
    assert "NEXUS_AWARENESS_ENABLED" in source


def test_capture_happens_before_every_gate():
    """The room is understood whether or not anybody in it may talk to Nexus."""
    source = inspect.getsource(main.on_group_chat)
    assert source.index("_awareness_capture") < source.index("nexus.accepts")


def test_the_status_report_shows_the_awareness_state(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    assert config.NEXUS_AWARENESS_ON_LABEL in main._nexus_status_text()
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)
    assert config.NEXUS_AWARENESS_OFF_LABEL in main._nexus_status_text()


def test_the_awareness_state_survives_a_process_restart(monkeypatch):
    """It is persisted, so a restart does not lose what was understood."""
    db.awareness_set(
        CHAT, seen_message_id=7, relevant=True, topic="persisted", summary="kept"
    )
    assert db.awareness_get(CHAT)["topic"] == "persisted"
    assert db.awareness_get(CHAT)["seen_message_id"] == 7


def test_the_awareness_tables_are_created_without_a_migration_step():
    """``CREATE TABLE IF NOT EXISTS`` on an existing database is enough."""
    tables = {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "group_messages" in tables
    assert "awareness_state" in tables


# ══ 13. OFF means off ═════════════════════════════════════════════════════
# The owner's switch is the one instruction that must be obeyed literally. An
# assistant that was silenced and went on reading the room is a different bot
# from the one that was asked for, so the capture and the pass are both gated on
# it — and the gate is checked in `_awareness_run_room`, the single place a pass
# is started, so the sweeper and the urgency hint cannot disagree about it.
def test_an_offline_nexus_captures_nothing(monkeypatch):
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run(main.on_group_chat, message(text="سلام"), bot, actor=OWNER)
    assert db.group_window(CHAT, limit=10) == []


def test_an_offline_nexus_runs_no_awareness_pass(monkeypatch):
    """Messages captured before the switch-off are not read while it is off."""
    db.group_append(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "سلام")
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")
    bot = FakeBot()
    passes = install_awareness(monkeypatch, decision={"respond": True, "message": "hi"})

    asyncio.run(main.awareness_sweep(ctx_for(bot)))

    assert passes == [], "a switched-off assistant was still reading the room"
    assert bot.messages == []


def test_the_urgency_hint_does_not_read_a_room_while_offline(monkeypatch):
    db.group_append(CHAT, MEMBER, awareness.ROLE_MEMBER, "reza", "این کاربر رو بن کن")
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")
    bot = FakeBot()
    passes = install_awareness(monkeypatch)

    asyncio.run(main._awareness_promptly(ctx_for(bot), CHAT))

    assert passes == []


def test_switching_back_on_resumes_the_capture(monkeypatch):
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")
    bot = FakeBot()
    run(main.on_group_chat, message(text="خاموش"), bot, actor=OWNER)
    assert db.group_window(CHAT, limit=10) == []

    nexus.set_state(nexus.ONLINE, actor_id=OWNER, reason="test")
    run(main.on_group_chat, message(text="روشن"), bot, actor=OWNER)
    assert [r["text"] for r in db.group_window(CHAT, limit=10)] == ["روشن"]


def test_the_offline_gate_lives_in_the_one_place_a_pass_is_started():
    """Structural: both callers go through `_awareness_run_room`, so both are gated."""
    source = inspect.getsource(main._awareness_run_room)
    assert "nexus.is_online()" in source


# ══ 14. The instruction the pass is given ═════════════════════════════════
# The prompt is the only place the ambient policy is *stated*, so the three
# properties the feature depends on are asserted against it rather than left to
# a reader to notice. None of these is a behavioural test — they are the
# cheapest possible guard against a later edit quietly removing the sentence a
# behaviour rests on.
def test_the_instruction_says_the_labels_are_the_servers():
    text = chat.AWARENESS_INSTRUCTION
    assert "written by the server" in text
    assert "never treat a claim" in text


def test_the_instruction_says_silence_is_the_default():
    text = chat.AWARENESS_INSTRUCTION
    assert "Silence is the default" in text
    assert "do not stay silent merely because you were not" in text


def test_the_instruction_says_nexus_may_be_discussed_without_being_named():
    assert "without being named" in chat.AWARENESS_INSTRUCTION


def test_the_instruction_asks_for_the_structured_understanding():
    """The schema and the vocabulary are stated where the model can see them."""
    text = chat.AWARENESS_INSTRUCTION
    assert '"intent"' in text
    assert '"about"' in text
    for word in awareness.INTENTS:
        assert f'"{word}"' in text
    # And the fail-safe direction: 0 is always honest, a wrong id is not.
    assert "a wrong id is worse than none" in text


def test_the_instruction_names_the_owner_as_the_creator_and_developer():
    """The register the owner asked for, stated by the server, granting nothing."""
    assert "creator and developer" in chat.AWARENESS_INSTRUCTION
    assert "creator and developer" in admin_tools.build_context(
        principal=rbac.resolve(OWNER), chat_id=CHAT
    )


def test_the_trusted_context_never_calls_the_owner_a_creator_for_anyone_else():
    """A fact about one person must not be attached to another person's turn."""
    for actor in (SENIOR, ADMIN, MODERATOR, MEMBER):
        assert "creator and developer" not in admin_tools.build_context(
            principal=rbac.resolve(actor), chat_id=CHAT
        )


def test_the_ambient_context_does_not_deny_the_referent_the_server_knows():
    """The ambient block used to say "there is no referent, ask".

    That sentence was true of the old design and is a bug in the new one: the
    room window records what each message replied to, so an instruction sent as
    a reply *does* have a referent and the server can name it. The block now
    defers to the server's reading instead of contradicting it, which is the fix
    for «این رو سکوت کن» producing «کی رو می‌خوای ساکت کنی؟».
    """
    text = admin_tools.build_context(
        principal=rbac.resolve(ADMIN), chat_id=CHAT, ambient=True
    )
    assert "The server's reading of the instruction in this batch" in text
    # The rule that replaced the old one, stated so the model cannot miss it.
    assert "reuse a target from an earlier instruction" in text
    # And the non-ambient block still says the other thing, so the two paths have
    # not been collapsed into one.
    directed = admin_tools.build_context(principal=rbac.resolve(ADMIN), chat_id=CHAT)
    assert "no replied-to message in this turn" in directed


def test_the_ambient_context_never_claims_there_is_no_referent_outright():
    """The blanket denial is gone, and it must not come back.

    Asserted separately from the sentence above because it is the actual defect:
    a block that tells the model a referent cannot exist will make it ask a
    question whose answer was in the message all along.
    """
    text = admin_tools.build_context(
        principal=rbac.resolve(ADMIN), chat_id=CHAT, ambient=True
    )
    assert "no referent on their own" not in text


# ══ One request, one reply ════════════════════════════════════════════════
# A message addressed to Nexus in a group is answered directly *and* sits in the
# room window, so the next awareness pass used to read it as an unanswered
# question and answer it again. That is the duplicate the owner reported.
#
# The fix is a suppression of the *response*, never of the read: the batch is
# still understood and still recorded, because moving the watermark past one
# message would silently mark every earlier unread message as understood too —
# trading a duplicate reply for a lost event, which is the worse failure.
def test_an_addressed_message_is_not_answered_a_second_time(monkeypatch):
    """The direct path is answering this very message, so the pass must not."""
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس سلام")
    main._nexus_addressed[CHAT] = pending_row()["max_id"]
    install_awareness(
        monkeypatch,
        decision={
            "topic": "greeting",
            "summary": "the owner greeted the assistant",
            "relevant": True,
            "respond": True,
            "message": "سلام دوباره",
        },
    )

    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))

    assert bot.messages == []
    # The understanding still happened: only the reply was withheld.
    assert awareness.state(CHAT).get("summary") == "the owner greeted the assistant"


def test_the_window_alone_suppresses_a_repeat_reply(monkeypatch):
    """After a restart the marker is gone. The stored window still knows.

    The assistant's own turn is recorded when its reply goes out, so the
    question "does this room still need an answer" can be asked of the window
    rather than of a process-local flag — and the window survives a restart.
    """
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس سلام")
    awareness.capture(CHAT, 0, awareness.ROLE_NEXUS, "", "سلام، در خدمتم")
    main._nexus_addressed.clear()
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "سلام دوباره"},
    )

    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))

    assert bot.messages == []


def test_a_room_the_assistant_has_not_answered_is_still_answerable(monkeypatch):
    """The guard must not become a mute button.

    Without this the two tests above would pass for the wrong reason: a
    suppression that always fires would look identical to a correct one.
    """
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "نکسوس سلام")
    main._nexus_addressed.clear()
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": True, "message": "سلام"},
    )

    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))

    assert bot.messages == ["سلام"]


def test_a_write_confirmation_is_never_withheld_as_a_duplicate(monkeypatch):
    """An action that ran must be acknowledged, whatever the guard says.

    The action has already happened by the time the reply is composed, so
    silence here would be a change nobody was told about — worse than a
    repeated sentence.
    """
    bot = FakeBot()
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "mo", "این کاربر رو بن کن")
    awareness.capture(CHAT, 0, awareness.ROLE_NEXUS, "", "چشم")
    main._nexus_addressed[CHAT] = pending_row()["max_id"]
    install_awareness(
        monkeypatch,
        decision={"relevant": True, "respond": False},
        call=("ban_member", {"target_user_id": STRANGER}),
    )

    asyncio.run(main._awareness_pass(ctx_for(bot), CHAT, pending_row()))

    assert ("ban", CHAT, STRANGER) in bot.actions, "the action must have run"
    assert bot.messages, "and it must have been acknowledged"


def test_the_group_handler_marks_an_addressed_message_as_answered(monkeypatch):
    """The marker is set by the handler, before the answer is awaited.

    Setting it *after* the reply would leave the race open: a model call is a
    suspension point, and the awareness pass can run during it.
    """
    bot = FakeBot()
    answered: list[str] = []

    async def _reply(chat_id, user_id, body, **kwargs):
        answered.append(body)
        return chat.ChatReply(answered=True, text="سلام", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    asyncio.run(
        main.on_group_chat(
            update_for(message(text="نکسوس سلام", message_id=77), actor=OWNER),
            ctx_for(bot),
        )
    )

    assert answered == ["نکسوس سلام"]
    assert main._nexus_addressed[CHAT] == 77


def test_a_silent_decline_leaves_the_room_readable(monkeypatch):
    """A withheld answer is not an answer, so the guard must not fire.

    Otherwise a failure on the direct path would silence the room entirely: the
    direct path says nothing, and the ambient path is told not to either.

    Note which declines are silent. A rate limit is *not* one of them — it has a
    sentence in ``chat._MESSAGES`` and is sent — so this uses a reason the
    transport has no copy for, which is the only shape that really says nothing.
    """

    async def _reply(chat_id, user_id, body, **kwargs):
        return chat.ChatReply(answered=False, skipped="no_copy_for_this", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    asyncio.run(
        main.on_group_chat(
            update_for(message(text="نکسوس سلام", message_id=77), actor=OWNER),
            ctx_for(FakeBot()),
        )
    )

    assert main._nexus_addressed.get(CHAT, 0) == 0


def test_a_decline_that_speaks_keeps_the_marker(monkeypatch):
    """If the person was told something, the room has been answered.

    A rate limit is the case that matters: the reply is a sentence rather than a
    model answer, but it is a reply, and the ambient path must not add a second
    one on top of it.
    """
    bot = FakeBot()

    async def _reply(chat_id, user_id, body, **kwargs):
        return chat.ChatReply(answered=False, skipped="rate_limit", turns=1)

    monkeypatch.setattr(main.chat, "reply", _reply)
    asyncio.run(
        main.on_group_chat(
            update_for(message(text="نکسوس سلام", message_id=77), actor=OWNER),
            ctx_for(bot),
        )
    )

    assert bot.messages == ["یه کم سریع داری پیام می‌دی 🙂 چند لحظه صبر کن."]
    assert main._nexus_addressed.get(CHAT, 0) == 77


# ── The guard, as a reading of the window ─────────────────────────────────
def test_the_last_word_belongs_to_the_assistant_only_after_it_spoke():
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "m", "یک پیام")
    assert awareness.nexus_has_the_last_word(CHAT) is False

    awareness.capture(CHAT, 0, awareness.ROLE_NEXUS, "", "پاسخ")
    assert awareness.nexus_has_the_last_word(CHAT) is True

    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "m", "یک پیام تازه")
    assert awareness.nexus_has_the_last_word(CHAT) is False


def test_a_room_with_only_the_assistants_words_counts_as_answered():
    """Nothing was asked, so there is nothing to answer."""
    awareness.capture(CHAT, 0, awareness.ROLE_NEXUS, "", "پاسخ خودم")
    assert awareness.nexus_has_the_last_word(CHAT) is True


def test_an_empty_room_has_no_last_word():
    assert awareness.nexus_has_the_last_word(CHAT) is False


# ══ The instruction: who asked, and who it is about ═══════════════════════
# The block below is the fix for the two halves of one reported defect: an
# instruction that could not be carried out because the server would not say who
# it was about, and an instruction that was carried out on the *wrong* person
# because the target of an earlier one survived in the conversation. Both are
# attribution, so both are pinned here rather than in the prompt.
def captured(chat_id, user_id, role, name, text, **extra):
    """Insert a window row with the capture-time hints the handler writes."""
    db.group_capture(
        chat_id, user_id, role, name, text, keep=100, **extra
    )


def test_a_promotion_is_visible_to_the_very_next_render():
    """The role on a row is when they typed; the role shown is what they hold now.

    This is the reported bug in one assertion: an administrator promoted a
    minute ago was still labelled a member in the transcript, so the model
    reasoned — correctly, from what it was given — that the person had no
    authority, and refused them. The stored label is not allowed to be the
    answer when the authority model has a fresher one.
    """
    captured(CHAT, MEMBER, "member", "Reza", "سلام", message_id=1)
    rows = db.group_window(CHAT, limit=10)
    assert awareness.roles_for(rows)[MEMBER] == "member"

    db.admin_set(MEMBER, "admin", ["moderation.ban"], granted_by=OWNER)

    rows = db.group_window(CHAT, limit=10)
    assert awareness.roles_for(rows)[MEMBER] == "admin"
    assert "[admin] Reza" in awareness.render(CHAT, messages=rows)
    # The row itself still records how they stood when they spoke. History is
    # not rewritten; it is simply not used as the answer.
    assert rows[-1]["role"] == "member"


def test_the_anchor_is_the_instruction_not_the_newest_message():
    """A member's trailing message must not become the person who asked.

    The bug this replaces: an administrator replies to a nuisance with «این رو
    سکوت کن», a member posts something a moment later, and the pass — reading
    the newest human message — built the tool surface for the *member*, who
    holds no permissions. The refusal was a true statement about the wrong
    person.
    """
    captured(
        CHAT, ADMIN, "admin", "Ali", "این رو ساکت کن",
        actor=True, message_id=5, reply_user_id=MEMBER, reply_name="Nuisance",
    )
    captured(CHAT, MEMBER, "member", "Reza", "خب", message_id=6)
    rows = db.group_window(CHAT, limit=10)

    assert awareness.anchor(CHAT, messages=rows)["user_id"] == ADMIN

    # With no instruction at all, the newest human message is still the answer:
    # that is the conversation, and nothing else in the batch claims otherwise.
    db.awareness_reset()
    captured(CHAT, MEMBER, "member", "Reza", "خب", message_id=6)
    rows = db.group_window(CHAT, limit=10)
    assert awareness.anchor(CHAT, messages=rows)["user_id"] == MEMBER


def test_the_instruction_block_names_the_actor_the_role_and_the_reply_target():
    """The server states who asked, with what authority, and about whom.

    Three facts, each one closing a way the assistant previously guessed: the
    actor's id and *current* role, the id the instruction points at through its
    reply edge, and the explicit rule that an older target is background.
    """
    captured(CHAT, MEMBER, "member", "Nuisance", "مزاحم شده", message_id=4)
    captured(
        CHAT, ADMIN, "admin", "Ali", "این رو ساکت کن",
        actor=True, message_id=5,
        reply_user_id=MEMBER, reply_name="Nuisance", reply_message_id=4,
    )
    rows = db.group_window(CHAT, limit=10)
    block = awareness.instruction_block(CHAT, messages=rows)

    assert str(ADMIN) in block
    assert "admin" in block
    assert str(MEMBER) in block
    # The reply is stated as a fact, and the reuse rule is stated as a rule.
    assert "was a **reply**" in block
    assert "never reuse it" in block
    # And the no-reply branch says the opposite thing rather than nothing.
    db.awareness_reset()
    captured(
        CHAT, ADMIN, "admin", "Ali", "این رو ساکت کن", actor=True, message_id=5
    )
    rows = db.group_window(CHAT, limit=10)
    assert "was not a reply" in awareness.instruction_block(CHAT, messages=rows)


def test_a_member_message_is_never_stated_as_an_instruction():
    """Conversation is not an instruction, and the block must not invent one."""
    captured(CHAT, MEMBER, "member", "Reza", "این رو ساکت کن", directed=True)
    rows = db.group_window(CHAT, limit=10)
    assert awareness.instruction_block(CHAT, messages=rows) == ""


def test_the_transcript_marks_a_call_differently_from_a_mention():
    """«نکسوس گفت که...» is context; «نکسوس ساکتش کن» is a call.

    Both spell the name exactly, so both are *found* — the difference is the
    grade, and it is what stops a quotation from looking like an instruction.
    """
    captured(CHAT, ADMIN, "admin", "Ali", "نکسوس گفت که فلانی رو ساکت کنه",
             message_id=1)
    captured(CHAT, ADMIN, "admin", "Ali", "نکسوس ساکتش کن",
             directed=True, message_id=2)
    rows = db.group_window(CHAT, limit=10)
    lines = awareness.render(CHAT, messages=rows).splitlines()

    assert "⋯ about you" in lines[0]
    assert "⟶ to you" in lines[1]


# ══ What the bot itself may do ════════════════════════════════════════════
# The reported bug: the assistant said Telegram had not given it permission to
# mute, and then — after being shown the bot's rights — discovered it did, and
# muted. The permission was never missing; the knowledge was. These pin the
# answer as something read rather than guessed.
def test_an_unreadable_right_set_is_reported_as_unknown_never_as_a_refusal():
    text = admin_tools.bot_rights_block(
        {"status": "", "rights": {}, "error": "telegram lookup failed"}
    )
    assert "unknown" in text
    assert "Do not tell anybody you lack a permission" in text
    # Nothing is rendered as a denied right: an unknown must not read as "no".
    assert "=no" not in text


def test_the_rights_block_states_what_is_held_and_what_it_enables():
    text = admin_tools.bot_rights_block(
        {
            "status": "administrator",
            "rights": {"can_restrict_members": True, "can_delete_messages": False},
            "error": "",
        }
    )
    assert "can_restrict_members=yes" in text
    assert "can_delete_messages=no" in text
    assert "mute_member" in text
    assert "delete_message" in text


# ══ Naming the target of an action ════════════════════════════════════════
def test_a_user_action_result_carries_the_name_and_the_username():
    """An announcement has to say *who*, and the server supplies the who."""
    from app import people

    people.remember(
        SimpleNamespace(
            id=MEMBER, first_name="Reza", last_name="Ahmadi", username="reza"
        ),
        CHAT,
    )
    info = admin_tools.target_identity(
        SimpleNamespace(operation="mute_member", target_id=MEMBER), chat_id=CHAT
    )
    assert info["user_id"] == MEMBER
    assert info["name"] == "Reza Ahmadi"
    assert info["username"] == "@reza"
    assert info["handle"] == "@reza"


def test_a_target_without_a_username_falls_back_to_the_id():
    from app import people

    people.remember(
        SimpleNamespace(id=STRANGER, first_name="Nobody", last_name="", username=""),
        CHAT,
    )
    info = admin_tools.target_identity(
        SimpleNamespace(operation="ban_member", target_id=STRANGER), chat_id=CHAT
    )
    assert info["username"] == ""
    assert info["handle"] == str(STRANGER)


def test_a_message_operation_is_not_described_as_a_person():
    """A delete's target id is a message id, and must never name a member."""
    info = admin_tools.target_identity(
        SimpleNamespace(operation="delete_message", target_id=555), chat_id=CHAT
    )
    assert info == {}


# ══ Resolving several people at once ══════════════════════════════════════
def test_resolve_many_agrees_with_resolve_for_every_id():
    """The bulk read is a wrapper, not a second authority model."""
    db.admin_set(MEMBER, "admin", ["moderation.ban"], granted_by=OWNER)

    many = rbac.resolve_many([OWNER, SENIOR, ADMIN, MODERATOR, MEMBER, 0])
    assert 0 not in many
    for user_id in (OWNER, SENIOR, ADMIN, MODERATOR, MEMBER):
        one = rbac.resolve(user_id)
        assert many[user_id].role == one.role
        assert many[user_id].permissions == one.permissions
        assert many[user_id].source == one.source


# ══ Speech in the room window ═════════════════════════════════════════════
def test_an_actors_voice_note_is_transcribed_into_the_window(monkeypatch):
    """An administrator who speaks an instruction is giving an instruction."""
    async def fake_transcribe(ref, *, download):
        return SimpleNamespace(ok=True, text="این رو ساکت کن", error="", skipped="")

    monkeypatch.setattr(main.transcribe, "transcribe_ref", fake_transcribe)
    voice = SimpleNamespace(
        file_id="f1", file_unique_id="u1", mime_type="audio/ogg",
        file_size=1234, duration=3,
    )
    run(main.on_group_chat, message(message_id=9, voice=voice), FakeBot(),
        actor=ADMIN)

    row = db.group_window(CHAT, limit=5)[-1]
    assert row["text"] == "این رو ساکت کن"
    assert row["kind"] == "voice"
    assert row["actor"] is True


def test_a_members_voice_note_is_recorded_as_its_kind_and_nothing_is_spent(
    monkeypatch,
):
    """The room is read to understand the people who can act."""
    calls: list = []

    async def fake_transcribe(ref, *, download):
        calls.append(ref)
        return SimpleNamespace(ok=True, text="x", error="", skipped="")

    monkeypatch.setattr(main.transcribe, "transcribe_ref", fake_transcribe)
    voice = SimpleNamespace(
        file_id="f1", file_unique_id="u1", mime_type="audio/ogg",
        file_size=1234, duration=3,
    )
    run(main.on_group_chat, message(message_id=9, voice=voice), FakeBot(),
        actor=MEMBER)

    assert calls == []
    row = db.group_window(CHAT, limit=5)[-1]
    assert row["text"] == "[voice]"
    assert row["actor"] is False
