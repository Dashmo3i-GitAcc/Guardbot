"""How long the assistant takes, and where the time actually went.

The owner's report was that Nexus answers a question in the group too slowly.
Measuring the path rather than guessing at it produced three answers, and only
one of them was a defect:

* **The tick.** The policy is "read a room once it has been quiet for the
  debounce (8 s)". The sweeper asks whether that has happened every
  ``NEXUS_AWARENESS_TICK_SECONDS`` (15 s), so a room that was due at 8 seconds
  was read at the next multiple of 15 — 8 to 23 seconds, median 15.5, of which
  the policy accounts for 8 and the rest is a timer that was never about that
  room. This is the fix, and ``_awareness_ready_at`` is what it is made of.
* **The capture.** Every received message cost three commits (append, trim,
  purge). Measured at 10.9 ms median against 5.9 ms for one transaction. Real,
  but two orders of magnitude below the wait, and it is paid on every message
  rather than once per pass.
* **The prompt.** 26 KB of tool declarations against 5.4 KB of everything else.
  Measured, reported, and deliberately *not* cut — see the note at the end of
  this file.

So the tests below are mostly about timing policy: a deadline that is coalesced,
bounded and dropped once met; a ceiling that survives it; one pass at a time per
group; and an ordering property that says a pass can never mark newer messages
as read. The last group is the instrumentation, because the whole point of this
file is that the numbers above are measured rather than asserted.

Nothing here talks to Telegram or to Google, and no test sleeps: the clock is
driven by hand.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import admin_tools, awareness, chat, config, db, gemini_pool, main, nexus, rbac

OWNER = 999
ADMIN = 556
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999
BOT_ID = 1


@pytest.fixture(autouse=True)
def latency_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin"])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT, OTHER_CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_ACTORS_ONLY", True)
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "BOT_ALIASES", [])
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "test-awareness-key")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_TICK_SECONDS", 15.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_DEBOUNCE_SECONDS", 8.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_WAIT_SECONDS", 45.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 20.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_WINDOW_MESSAGES", 40)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_WINDOW_CHARS", 6000)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 3600)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_ROWS", 400)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_PURGE_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_CHATS_PER_TICK", 2)

    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    chat.reset_state()
    awareness.reset_timers()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._nexus_visibility[OTHER_CHAT] = "administrator"
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()
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
    awareness.reset_timers()
    main._nexus_visibility.clear()
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()


# ── Harness ───────────────────────────────────────────────────────────────
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


class FakeJobQueue:
    """Records what ``post_init`` registers, so the wiring can be asserted."""

    def __init__(self):
        self.repeating: list[tuple] = []

    def run_repeating(self, callback, interval=None, first=None, **kwargs):
        self.repeating.append((callback, interval, first))
        return SimpleNamespace(callback=callback)


def ctx_for(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


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
            id=actor, full_name="تستر", username="tester", is_bot=False
        ),
    )


def capture(chat_id=CHAT, user_id=MEMBER, role=awareness.ROLE_MEMBER, text="سلام"):
    assert awareness.capture(chat_id, user_id, role, "تستر", text) is True


def install_awareness(monkeypatch, *, decision=None, raw=None, delay=0.0, error=""):
    """Replace the awareness transport. Returns the recorded passes.

    ``delay`` is awaited *inside* the transport, which is how the
    one-pass-at-a-time tests hold a pass open without sleeping the test.
    """
    passes: list[dict] = []

    async def _awareness(transcript, context="", *, tools=None, on_tool=None):
        passes.append({"transcript": transcript, "tools": tools})
        if delay:
            await asyncio.sleep(delay)
        if error:
            return chat.AwarenessReply(error=error, model="stub")
        body = raw if raw is not None else json.dumps(decision or {})
        return chat.AwarenessReply(text=body, model="stub", turns=1)

    monkeypatch.setattr(main.chat, "awareness", _awareness)
    return passes


def age_the_room(chat_id=CHAT, *, seconds=60):
    """Backdate this room's captured messages, so the batch has really waited.

    A message captured a moment ago is deliberately *not* due — that is the
    debounce — and the timestamps the policy reads come from the rows
    themselves, so a test about what happens after the wait has to move the rows
    rather than hand a synthetic summary to one function. Moving the rows is
    also what makes the deadline tick's own query see a quiet room, which is the
    thing under test.
    """
    db._exec(
        "UPDATE group_messages SET at = at - ? WHERE chat_id = ?",
        (int(seconds), int(chat_id)),
    )


def room_is_due(chat_id=CHAT, *, age=60):
    """Capture a message, let the room go quiet, and return the real pending row."""
    capture(chat_id)
    age_the_room(chat_id, seconds=age)
    for row in awareness.pending():
        if row["chat_id"] == chat_id:
            return row
    raise AssertionError("the capture produced no pending row")


# ══ THE DEADLINE ══════════════════════════════════════════════════════════
def test_a_captured_message_arms_a_deadline_at_the_debounce():
    before = time.monotonic()
    main._awareness_schedule(CHAT)

    armed = main._awareness_ready_at[CHAT]
    assert before + config.NEXUS_AWARENESS_DEBOUNCE_SECONDS - 0.1 <= armed
    assert armed <= time.monotonic() + config.NEXUS_AWARENESS_DEBOUNCE_SECONDS + 0.1


def test_a_burst_of_messages_produces_one_deadline_not_one_per_message():
    """Coalescing is by assignment: twenty messages, one key, one wake-up."""
    for _ in range(20):
        capture()
        main._awareness_schedule(CHAT)

    assert list(main._awareness_ready_at) == [CHAT]
    # And the deadline is the *last* message's, which is the debounce.
    assert main._awareness_ready_at[CHAT] > time.monotonic() + 7.0


def test_the_deadline_tick_reads_a_quiet_room_without_the_sweeper():
    """This is the whole fix: the room is read because its own clock expired."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(monkeypatch, decision={"relevant": True})
        row = room_is_due()
        # The room went quiet more than a debounce ago. No sweeper tick has run.
        main._awareness_ready_at[CHAT] = time.monotonic() - 1.0
        assert main._awareness_sweeping is False

        asyncio.run(main._awareness_deadline_tick(SimpleNamespace(bot=FakeBot())))

        assert len(passes) == 1
        assert main._awareness_ready_at == {}
    finally:
        monkeypatch.undo()


def test_the_deadline_tick_does_no_query_while_every_room_is_still_talking():
    """A tick with nothing expired must cost a dictionary scan and nothing else."""
    calls: list[int] = []
    monkeypatch = pytest.MonkeyPatch()
    try:
        real = awareness.pending
        monkeypatch.setattr(
            awareness, "pending", lambda: (calls.append(1), real())[1]
        )
        capture()
        main._awareness_schedule(CHAT)  # deadline is in the future

        asyncio.run(main._awareness_deadline_tick(SimpleNamespace(bot=FakeBot())))

        assert calls == [], "a tick with no expired deadline must not query"
        assert CHAT in main._awareness_ready_at, "and must not drop the deadline"
    finally:
        monkeypatch.undo()


def test_the_deadline_is_dropped_once_the_room_has_been_read():
    monkeypatch = pytest.MonkeyPatch()
    try:
        install_awareness(monkeypatch, decision={"relevant": False})
        row = room_is_due()
        main._awareness_ready_at[CHAT] = time.monotonic() - 1.0

        ran = asyncio.run(main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row))

        assert ran is True
        assert CHAT not in main._awareness_ready_at
    finally:
        monkeypatch.undo()


def test_a_refused_pass_rearms_the_deadline_instead_of_spinning():
    """A room read a moment ago waits out the minimum interval, once."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(monkeypatch, decision={"relevant": False})
        room_is_due()
        # A pass just ran, so ``due`` refuses with ``too_soon``.
        main._awareness_last_pass[CHAT] = time.time()
        main._awareness_ready_at[CHAT] = time.monotonic() - 1.0

        asyncio.run(main._awareness_deadline_tick(SimpleNamespace(bot=FakeBot())))

        assert passes == []
        rearmed = main._awareness_ready_at[CHAT]
        # Re-armed at the brake's deadline, not at "now", so it cannot spin.
        assert rearmed > time.monotonic() + config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS - 1
    finally:
        monkeypatch.undo()


def test_the_deadline_tick_reads_nothing_while_nexus_is_off():
    """OFF means off, applied to the fast path as well as to the sweeper."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(monkeypatch, decision={"relevant": True})
        room_is_due()
        main._awareness_ready_at[CHAT] = time.monotonic() - 1.0
        nexus.set_state(nexus.OFFLINE)
        nexus.reset_state()
        assert nexus.is_online() is False

        asyncio.run(main._awareness_deadline_tick(SimpleNamespace(bot=FakeBot())))

        assert passes == []
        assert main._awareness_ready_at == {}, "and the deadlines are dropped"
    finally:
        db.nexus_state_reset()
        nexus.reset_state()
        monkeypatch.undo()


def test_the_starvation_ceiling_survives_the_deadline():
    """A room that never falls quiet is still read, by the sweeper.

    The deadline can only make a pass earlier. This asserts the other half: the
    wait-for-quiet clause is bypassed once a message has waited past the
    ceiling, so pushing a deadline out for ever cannot become a silence.
    """
    capture()
    row = next(r for r in awareness.pending() if r["chat_id"] == CHAT)
    now = time.time()

    # The room is talking continuously: newest message is now.
    talking = dict(row, newest_at=int(now), oldest_at=int(now), pending=30)
    assert not awareness.due(talking, now=now, last_pass_at=0.0)
    assert awareness.due(talking, now=now, last_pass_at=0.0).reason == "room_still_talking"

    # The oldest unread message has waited past the ceiling.
    starved = dict(row, newest_at=int(now), oldest_at=int(now - 60), pending=30)
    assert awareness.due(starved, now=now, last_pass_at=0.0)


def test_the_urgent_hint_still_cannot_bypass_the_minimum_interval():
    """The brake is what stops a flood of actionable-sounding messages."""
    capture()
    row = next(r for r in awareness.pending() if r["chat_id"] == CHAT)
    now = time.time()

    fresh = dict(row, newest_at=int(now - 1), oldest_at=int(now - 1), pending=1)
    # Urgent skips wait-for-quiet ...
    assert awareness.due(fresh, now=now, last_pass_at=0.0, urgent=True)
    # ... and not the minimum interval.
    verdict = awareness.due(fresh, now=now, last_pass_at=now - 1, urgent=True)
    assert not verdict
    assert verdict.reason == "too_soon"


def test_the_tick_reads_only_the_room_whose_clock_expired():
    """Per-group isolation, on the fast path: one room's deadline is not another's."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(monkeypatch, decision={"relevant": False})
        quiet = room_is_due(CHAT)
        room_is_due(OTHER_CHAT)
        assert quiet["chat_id"] == CHAT
        # Only CHAT has expired; OTHER_CHAT is still talking.
        main._awareness_ready_at[CHAT] = time.monotonic() - 1.0
        main._awareness_ready_at[OTHER_CHAT] = time.monotonic() + 60.0

        asyncio.run(main._awareness_deadline_tick(SimpleNamespace(bot=FakeBot())))

        assert len(passes) == 1
        assert CHAT not in main._awareness_ready_at
        assert OTHER_CHAT in main._awareness_ready_at, "the talking room keeps its clock"
    finally:
        monkeypatch.undo()


def test_the_tick_reads_expired_rooms_oldest_deadline_first():
    """Ordering on the fast path, so a long-waiting room is not starved."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(monkeypatch, decision={"relevant": False})
        room_is_due(CHAT)
        room_is_due(OTHER_CHAT)
        now = time.monotonic()
        main._awareness_ready_at[CHAT] = now - 1.0
        main._awareness_ready_at[OTHER_CHAT] = now - 5.0

        assert main._awareness_deadline_passed(now) == [OTHER_CHAT, CHAT]

        asyncio.run(main._awareness_deadline_tick(SimpleNamespace(bot=FakeBot())))

        assert len(passes) == 2
    finally:
        monkeypatch.undo()


def test_a_real_group_message_arms_the_deadline():
    """The wiring, end to end through the handler.

    Everything above tests the deadline as a mechanism. This is the one test
    that says the mechanism is actually reached: an ordinary group message,
    through ``on_group_chat``, leaves a deadline behind. Without it the fast
    path would be correct and never used, which is exactly the kind of failure
    that looks like "the fix did nothing".
    """
    main._awareness_ready_at.clear()
    msg = message(text="سلام، یه سوال داشتم")

    asyncio.run(
        main.on_group_chat(update_for(msg), ctx_for(FakeBot()))
    )

    assert CHAT in main._awareness_ready_at, "the message must arm a deadline"
    assert awareness.trigger_at(CHAT) > 0.0


def test_a_message_from_a_room_we_cannot_observe_arms_no_deadline():
    """Nothing is read for a room Telegram is not delivering, so nothing waits."""
    main._awareness_ready_at.clear()
    main._nexus_visibility[CHAT] = "member"
    msg = message(text="سلام")

    asyncio.run(main.on_group_chat(update_for(msg), ctx_for(FakeBot())))

    # The capture still happens (the window is not a delivery promise), but the
    # deadline tick refuses the room, so arming one would be a pointless wake-up.
    assert CHAT not in main._awareness_ready_at


def test_the_deadlines_are_per_group():
    main._awareness_schedule(CHAT)
    first = main._awareness_ready_at[CHAT]
    main._awareness_schedule(OTHER_CHAT)

    assert set(main._awareness_ready_at) == {CHAT, OTHER_CHAT}
    assert main._awareness_ready_at[CHAT] == first


def test_the_fast_tick_is_registered_and_is_much_faster_than_the_sweeper():
    """The wiring, asserted: two timers, and the fast one is the fast one."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(main, "load_identity", _noop)
        monkeypatch.setattr(main, "_nexus_visibility_report", _noop)
        queue = FakeJobQueue()
        app = SimpleNamespace(job_queue=queue, bot=FakeBot(), bot_data={})

        asyncio.run(main.post_init(app))

        intervals = {cb: interval for cb, interval, _ in queue.repeating}
        assert main.awareness_sweep in intervals
        assert main._awareness_deadline_tick in intervals
        assert intervals[main._awareness_deadline_tick] < intervals[main.awareness_sweep]
        assert (
            intervals[main._awareness_deadline_tick]
            <= main.AWARENESS_DEADLINE_TICK_SECONDS
        )
    finally:
        monkeypatch.undo()


async def _noop(*args, **kwargs):
    return None


# ══ ONE PASS AT A TIME, IN ORDER ══════════════════════════════════════════
def test_two_ticks_cannot_produce_two_passes_for_one_room():
    """Two callers, one room, one model call.

    ``_awareness_inflight`` is the guard, and this is the only test that
    exercises it with a pass that is genuinely still open — the transport holds
    it open while a second caller tries to start.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(
            monkeypatch, decision={"relevant": True}, delay=0.05
        )
        row = room_is_due()

        async def both():
            return await asyncio.gather(
                main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row),
                main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row),
            )

        results = asyncio.run(both())

        assert sorted(results) == [False, True]
        assert len(passes) == 1
    finally:
        monkeypatch.undo()


def test_a_pass_never_marks_a_newer_message_as_read():
    """Ordering and idempotency: the watermark cannot jump ahead of the batch.

    A pass takes a snapshot of the room, and messages that arrive while it is
    running must survive it. Otherwise a message spoken during a pass would be
    marked understood without ever having been read.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(
            monkeypatch, decision={"relevant": True}, delay=0.05
        )
        row = room_is_due()
        snapshot_max = int(row["max_id"])

        async def run_and_interrupt():
            task = asyncio.create_task(
                main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row)
            )
            await asyncio.sleep(0.01)
            # Somebody speaks while the model is thinking.
            capture(text="و یه چیز دیگه هم بگم")
            await task

        asyncio.run(run_and_interrupt())

        assert len(passes) == 1
        state = awareness.state(CHAT)
        assert int(state.get("seen_message_id") or 0) == snapshot_max
        # The newer message is still unread, so the next pass will see it.
        still_pending = [
            r for r in awareness.pending() if r["chat_id"] == CHAT
        ]
        assert still_pending, "a message that arrived mid-pass must stay pending"
        assert int(still_pending[0]["max_id"]) > snapshot_max
    finally:
        monkeypatch.undo()


def test_the_assistants_own_reply_arms_no_deadline():
    """Its own words are not something it has to notice.

    Letting the assistant's reply start the clock would make every reply
    schedule the next pass, which is the feedback loop that once made it answer
    itself for ever.
    """
    awareness.reset_timers()
    main._awareness_note_reply(CHAT, "بله، در خدمتم")

    assert awareness.trigger_at(CHAT) == 0.0
    assert [r for r in awareness.pending() if r["chat_id"] == CHAT] == []


def test_a_member_message_moves_the_capture_clock_and_a_reply_does_not():
    capture(text="سوال دارم")
    after_member = awareness.trigger_at(CHAT)
    assert after_member > 0.0

    main._awareness_note_reply(CHAT, "جواب")
    assert awareness.trigger_at(CHAT) == after_member


def test_capture_is_a_single_write_path():
    """The three statements became one, and this is what keeps them one."""
    calls: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    try:
        real_capture = db.group_capture
        monkeypatch.setattr(
            db,
            "group_capture",
            lambda *a, **kw: (calls.append("capture"), real_capture(*a, **kw))[1],
        )
        monkeypatch.setattr(
            db, "group_append", lambda *a, **kw: calls.append("append")
        )
        monkeypatch.setattr(db, "group_trim", lambda *a, **kw: calls.append("trim"))

        capture()

        assert calls == ["capture"], "one transaction, not append + trim"
    finally:
        monkeypatch.undo()


# ══ INSTRUMENTATION ═══════════════════════════════════════════════════════
def test_the_trace_reports_every_stage_as_a_duration():
    trace = awareness.PassTrace(chat_id=CHAT, trigger_at=time.monotonic() - 9.0)
    trace.mark("batch")
    trace.mark("request")
    trace.mark("response")
    trace.mark("decision")
    trace.mark("send")
    trace.mark("end")

    line = trace.summary()

    for field in ("waited_ms", "batch_ms", "gemini_ms", "decide_ms", "send_ms",
                  "total_ms"):
        assert field in line
    # Nine seconds of wait is the number the owner was complaining about, and it
    # is reported as a duration rather than as a wall-clock time.
    assert 8500 <= trace.waited_ms() <= 9500


def test_the_trace_carries_no_content():
    """A trace is a log line, and other people's words must not be in it."""
    secret = "این پیام نباید در لاگ باشد"
    trace = awareness.PassTrace(chat_id=CHAT, trigger_at=time.monotonic())
    trace.mark("batch")
    trace.mark("end")

    line = trace.summary()

    assert secret not in line
    assert str(CHAT) in line
    # Durations only: every field is a number of milliseconds.
    assert "ms=" in line


def test_the_trace_logs_a_completed_pass(caplog):
    monkeypatch = pytest.MonkeyPatch()
    try:
        install_awareness(monkeypatch, decision={"relevant": True})
        row = room_is_due()
        with caplog.at_level("INFO", logger="guardbot"):
            asyncio.run(
                main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row)
            )
        timing = [r for r in caplog.records if "awareness timing" in r.getMessage()]
        assert timing, "every pass must log its own timeline"
        line = timing[0].getMessage()
        for field in ("waited_ms=", "gemini_ms=", "total_ms="):
            assert field in line
    finally:
        monkeypatch.undo()


def test_the_trace_logs_even_when_the_pass_reads_nothing(caplog):
    """The early returns are the ones that most need a timeline."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        passes = install_awareness(monkeypatch, raw="not json at all")
        row = room_is_due()
        with caplog.at_level("INFO", logger="guardbot"):
            asyncio.run(
                main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row)
            )
        assert passes, "the pass must have reached the model"
        timing = [r for r in caplog.records if "awareness timing" in r.getMessage()]
        assert timing
    finally:
        monkeypatch.undo()


def test_the_trace_never_logs_the_reply(caplog):
    """The reply is somebody else's words; the timing line must not carry it."""
    secret = "متن پاسخ محرمانه"
    monkeypatch = pytest.MonkeyPatch()
    try:
        install_awareness(
            monkeypatch, decision={"relevant": True, "respond": True,
                                   "message": secret}
        )
        row = room_is_due()
        with caplog.at_level("INFO", logger="guardbot"):
            asyncio.run(
                main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row)
            )
        assert any(secret in r.getMessage() for r in caplog.records) is False, (
            "the reply itself must not appear in the log"
        )
        timing = [r for r in caplog.records if "awareness timing" in r.getMessage()]
        assert timing and secret not in timing[0].getMessage()
    finally:
        monkeypatch.undo()


# ── The cost that was measured and deliberately not cut ───────────────────
def test_the_tool_declarations_are_the_largest_part_of_the_prompt():
    """A recorded measurement, not a requirement.

    36 KB of declarations against 5.4 KB of everything else, and it is *kept*:
    the descriptions are what make the model call ``unmute_member`` with the
    right id rather than refusing, which was a real bug in this codebase. This
    test exists so the number cannot change silently — if it does, the decision
    to keep it should be taken again rather than inherited.

    The ceiling was raised from 40000 to 48000 deliberately, when the
    operational-history tools were added (``get_identity``, ``search_events``,
    ``get_nexus_diagnostics``, ``get_service_status``). They cost about 6.3 KB
    of declarations and buy the assistant the ability to answer "why did this
    happen" from the server's records instead of from memory. The cost is
    bounded in practice: the full set is only attached when the last human
    speaker in a room is an administrator, because ``tool_names_for`` returns
    nothing for a guest — so a member's message still costs no declarations at
    all. The measured size at the time of the raise was 42794.
    """
    principal = rbac.resolve(OWNER)
    declarations = admin_tools.declarations_for(principal)
    assert declarations, "the owner must be offered tools for this to mean anything"
    size = len(json.dumps([t.model_dump() for t in declarations], default=str))

    fixed = (
        len(chat.AWARENESS_INSTRUCTION)
        + len(awareness.roster())
        + len(admin_tools.build_context(principal=principal, chat_id=CHAT, ambient=True))
    )

    assert size > fixed, "declarations are the largest item"
    # A ceiling, so growth is noticed. Raise it deliberately, with a reason.
    assert size < 48000, f"tool declarations grew to {size} chars"


# ══ THE ALLOWANCE ═════════════════════════════════════════════════════════
# The second timing question, and the one that was actually broken. The floor
# interval (20 s) and the daily allowance (200) are two numbers about the same
# thing, and they disagreed by a factor of twenty-one: a busy room reaches the
# floor every twenty seconds, so the allowance was spent in about an hour and
# every pass after that failed with ``pool_empty`` until the API day rolled
# over. On the deployment that produced this work: 203 requests spent, then 141
# consecutive failed passes.
#
# The fix is to pace, so the tests below are about the pacing rule and about the
# two things it must not do — slow a room down while there is allowance to
# spend, and let a spent allowance become a silent failure.
class FakeAllowance:
    """A pool that reports exactly the allowance a test wants to describe."""

    def __init__(self, *, budget: int, remaining: int):
        self.daily_budget = budget
        self._remaining = remaining

    def daily_remaining(self, now=None) -> int:
        return self._remaining


def install_allowance(monkeypatch, *, budget: int, remaining: int):
    fake = FakeAllowance(budget=budget, remaining=remaining)
    monkeypatch.setattr(gemini_pool, "pool_for", lambda workload: fake)
    return fake


def pin_day_clock(monkeypatch, *, seconds_left: float):
    """Hold the allowance's clock still, so a refusal is not a race.

    The tests that ask ``_awareness_affordable`` a question default to the real
    clock, and the real clock is within a minute of the API rollover once a day
    — at which point the gap legitimately shrinks to the floor and a test that
    expected a refusal would see a pass instead. Pinning the clock is what makes
    the assertion about the rule rather than about when the suite was run.
    """
    monkeypatch.setattr(
        db, "ai_day_seconds_left", lambda now=None: float(seconds_left)
    )


def at_day_start(offset_days: int = 3) -> float:
    """A stamp exactly on the API day boundary: the whole day is ahead."""
    return db._API_DAY_OFFSET + offset_days * 86400.0


def test_the_day_clock_counts_down_to_the_providers_reset():
    """The allowance is a *day's*, and the day is the provider's, not local."""
    assert db.ai_day_seconds_left(at_day_start()) == 86400.0
    assert db.ai_day_seconds_left(at_day_start() + 1) == 86399.0
    assert db.ai_day_seconds_left(at_day_start() + 86399) == 1.0
    # And it never leaves the day it belongs to.
    for hour in range(0, 24):
        left = db.ai_day_seconds_left(at_day_start() + hour * 3600)
        assert 0.0 < left <= 86400.0


def test_a_spent_allowance_waits_for_the_rollover(monkeypatch):
    """Zero left is not "try again in twenty seconds"; it is "try tomorrow"."""
    install_allowance(monkeypatch, budget=200, remaining=0)

    gap = main._awareness_allowance_gap(at_day_start() + 3600)

    assert gap == pytest.approx(86400.0 - 3600.0), (
        "a spent allowance must wait for the reset, not spin against the pool"
    )


def test_a_small_allowance_is_spread_across_the_rest_of_the_day(monkeypatch):
    """Two hundred passes must cover a day, which is one every seven minutes."""
    install_allowance(monkeypatch, budget=200, remaining=200)

    gap = main._awareness_allowance_gap(at_day_start())

    assert gap == pytest.approx(86400.0 / 200), "the allowance defines the pace"


def test_a_generous_allowance_never_slows_a_room_below_the_floor(monkeypatch):
    """Pacing must not become a second, hidden rate limit.

    With enough allowance for the whole day the gap is the configured floor and
    nothing else, which is the property that keeps this change from making
    awareness slower in the case where it was never the problem.
    """
    install_allowance(monkeypatch, budget=5000, remaining=5000)

    assert main._awareness_allowance_gap(at_day_start()) == (
        config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS
    )


def test_the_end_of_the_day_spends_what_is_left(monkeypatch):
    """Late in the day the remaining allowance is worth spending, not saving.

    Saving it would be saving it for nobody: the counter resets at the rollover
    whether or not it was used.
    """
    install_allowance(monkeypatch, budget=200, remaining=200)

    gap = main._awareness_allowance_gap(at_day_start() + 86399)

    assert gap == config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS


def test_a_pool_without_a_budget_is_not_paced(monkeypatch):
    """A workload nobody capped has no allowance to spread.

    Inventing a spacing for it would be this function deciding a policy that
    belongs in configuration, and it would silently throttle every workload the
    moment somebody added one without a budget.
    """
    install_allowance(monkeypatch, budget=0, remaining=0)

    assert main._awareness_allowance_gap(at_day_start()) == (
        config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS
    )


def test_a_room_that_has_never_been_read_is_not_held_back(monkeypatch):
    """The floor is a gap between passes, not a debt owed before the first one."""
    install_allowance(monkeypatch, budget=200, remaining=200)
    main._awareness_last_pass.pop(CHAT, None)

    assert main._awareness_affordable(CHAT) is True


def test_the_allowance_brake_is_per_room(monkeypatch):
    """A shared allowance is not a reason to starve the second room.

    The allowance is one number for the workload, but the pace is per room: a
    room read a moment ago must not stop a different room from being read, or
    the first room to speak would own the whole day.
    """
    install_allowance(monkeypatch, budget=200, remaining=200)
    now = time.time()
    main._awareness_last_pass[CHAT] = now
    main._awareness_last_pass[OTHER_CHAT] = 0.0

    assert main._awareness_affordable(CHAT, now) is False
    assert main._awareness_affordable(OTHER_CHAT, now) is True


def test_a_room_is_not_read_once_the_allowance_is_spent(monkeypatch):
    """The failure this fixes, stated as a behaviour: no allowance, no pass.

    Before the change the room was read, the transcript was rendered, the tool
    declarations were built, and the call came back ``pool_empty`` — every
    twenty seconds for the rest of the day.
    """
    install_allowance(monkeypatch, budget=200, remaining=0)
    pin_day_clock(monkeypatch, seconds_left=3600)
    passes = install_awareness(monkeypatch, decision={"relevant": True})
    row = room_is_due()
    # Read a minute ago: due by the floor, unaffordable by the allowance.
    main._awareness_last_pass[CHAT] = time.time() - 60

    ran = asyncio.run(main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row))

    assert ran is False, "a pass the pool cannot serve must not be started"
    assert passes == [], "and must not reach the model"


def test_the_allowance_is_checked_before_the_prompt_is_built(monkeypatch):
    """The refusal has to be cheap, or the day is spent on refusals.

    141 failed passes on the deployment each rendered a transcript and built
    26 KB of tool declarations before the pool told them it was empty. The
    check belongs in front of that work, not behind it.
    """
    install_allowance(monkeypatch, budget=200, remaining=0)
    pin_day_clock(monkeypatch, seconds_left=3600)
    install_awareness(monkeypatch, decision={"relevant": True})
    row = room_is_due()
    main._awareness_last_pass[CHAT] = time.time() - 60

    rendered: list[int] = []
    monkeypatch.setattr(
        main.awareness, "render", lambda *a, **k: rendered.append(1) or ""
    )
    built: list[int] = []
    monkeypatch.setattr(
        main, "_awareness_turn", lambda *a, **k: built.append(1) or (None, "", None)
    )

    asyncio.run(main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row))

    assert rendered == [], "the transcript must not be rendered for a refused pass"
    assert built == [], "and the tool declarations must not be built"


def test_a_readable_room_is_still_read_when_the_allowance_allows_it(monkeypatch):
    """The brake must not be a wall. With allowance, the pass runs as before."""
    install_allowance(monkeypatch, budget=200, remaining=200)
    passes = install_awareness(monkeypatch, decision={"relevant": True})
    row = room_is_due()
    main._awareness_last_pass[CHAT] = time.time() - 100000

    ran = asyncio.run(main._awareness_run_room(SimpleNamespace(bot=FakeBot()), row))

    assert ran is True
    assert len(passes) == 1
