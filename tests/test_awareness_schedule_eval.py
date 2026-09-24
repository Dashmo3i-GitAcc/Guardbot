"""The floor under the awareness-scheduling benchmark, and the seam it ships on.

``tools/eval_awareness_schedule.py`` measures which rooms the 200 rationed
awareness requests are spent on. This holds that measurement to a standard, so
the increment cannot quietly stop paying for itself, and pins the negative
result that chose the mechanism: ordering the candidate list has no leverage,
because the scheduler is event-driven per room rather than batch-driven.

The last group is the one that matters most. The benchmark can only prove the
*policy*; it cannot prove the policy is wired into the bot. So there is a
real-path test that drives ``main._awareness_capture`` and
``main._awareness_run_room`` — the production functions — through the actual
``awareness_schedule`` seam, with a stubbed transport and no model.
"""
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import (
    awareness,
    awareness_schedule,
    chat,
    config,
    context_plan,
    db,
    main,
    nexus,
    rbac,
)

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "eval_awareness_schedule", ROOT / "tools" / "eval_awareness_schedule.py"
)
eval_schedule = importlib.util.module_from_spec(_spec)
sys.modules["eval_awareness_schedule"] = eval_schedule
_spec.loader.exec_module(eval_schedule)


# ── The harness, run once and shared ──────────────────────────────────────
_report: dict | None = None


def full_report() -> dict:
    """The default-seed benchmark, run once for the whole module."""
    global _report
    if _report is None:
        _report = eval_schedule.compare(compare_ordering=True)
    return _report


# ── The workload ──────────────────────────────────────────────────────────
def test_the_workload_is_well_formed():
    workload = eval_schedule.build_workload()
    shapes = {room.shape for room in workload}
    assert len(shapes) == 10, sorted(shapes)
    assert len(workload) >= 28
    dependent = [msg for room in workload for msg in room.msgs if msg.depends]
    assert dependent, "the workload must contain messages that need the room"
    # The labels are not all one way: the measurement is a comparison, so both
    # classes have to be represented.
    assert any(not msg.depends for room in workload for msg in room.msgs)


def test_the_workload_is_deterministic():
    first = eval_schedule.build_workload(seed=7)
    second = eval_schedule.build_workload(seed=7)
    assert [(r.chat_id, r.shape, len(r.msgs)) for r in first] == [
        (r.chat_id, r.shape, len(r.msgs)) for r in second
    ]


# ── The floors ────────────────────────────────────────────────────────────
def test_u_spends_the_same_requests_and_gets_more_use_out_of_them():
    report = full_report()
    base, adaptive = report["baseline"], report["adaptive"]
    # The allowance is not raised: both spend exactly the day's requests.
    assert base["passes"] == adaptive["passes"] == 200
    # And no extra model call is made — a pass is the only call.
    assert base["model_calls"] == base["passes"]
    assert adaptive["model_calls"] == adaptive["passes"]
    # The point of the increment: the same 200 requests read more rooms that
    # needed reading.
    assert base["useful_pct"] >= 38.0
    assert adaptive["useful_pct"] >= 60.0
    assert adaptive["useful_pct"] - base["useful_pct"] >= 15.0


def test_u_does_not_starve_and_does_not_wait_longer():
    report = full_report()
    base, adaptive = report["baseline"], report["adaptive"]
    # Fairness is a hard requirement, and the mechanism improves it rather than
    # trading against it: the requests it frees go to rooms that were going
    # unread.
    assert adaptive["starved_rooms"] <= base["starved_rooms"]
    assert adaptive["unread_dependent_msgs"] < base["unread_dependent_msgs"]
    # And a deferred room is read sooner than before, not later: the bound is
    # the retention window, not "for ever".
    assert adaptive["max_wait_s"] < base["max_wait_s"]
    assert adaptive["p95_wait_s"] <= base["p95_wait_s"]


def test_the_decision_is_cheap():
    """The scheduler runs on every sweep; it must not be a per-tick cost."""
    report = full_report()
    assert report["adaptive"]["decide_ms_p95"] < 5.0


def test_the_rejected_ordering_has_no_leverage():
    """The negative result that chose the mechanism, pinned.

    Sorting the pending list by class changes the outcome by exactly nothing —
    the scheduler is event-driven per room, so at any instant there is about one
    candidate and nothing to sort. If this ever stops being true, the ordering
    mechanism becomes worth reconsidering, and the failure is the signal.
    """
    report = full_report()
    base, rejected = report["baseline"], report["order_only"]
    assert rejected["useful_passes"] == base["useful_passes"]
    assert rejected["passes"] == base["passes"]
    assert rejected["max_wait_s"] == base["max_wait_s"]


def test_the_benchmark_is_deterministic():
    """Same workload, same seed, same *outcome* — the harness makes no clock read
    of its own and no random choice.

    The ``decide_ms`` fields are wall-clock measurements and are excluded for
    the obvious reason: they measure the host, not the policy.
    """
    workload = eval_schedule.build_workload(seed=3)
    first = eval_schedule.simulate(
        workload, order_fn=eval_schedule.baseline_order, day=3600.0, label="a"
    ).summary()
    second = eval_schedule.simulate(
        workload, order_fn=eval_schedule.baseline_order, day=3600.0, label="a"
    ).summary()
    timing = {"decide_ms_p50", "decide_ms_p95"}
    assert {k: v for k, v in first.items() if k not in timing} == {
        k: v for k, v in second.items() if k not in timing
    }


# ══ THE SEAM: the policy is actually wired into the bot ═══════════════════
OWNER = 999
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999
BOT_ID = 1


@pytest.fixture
def schedule_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
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
    monkeypatch.setattr(config, "NEXUS_AWARENESS_DEBOUNCE_SECONDS", 8.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_WAIT_SECONDS", 45.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 20.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 3600)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MAX_CHATS_PER_TICK", 2)

    db.init()
    db.admin_reset()
    db.admin_pending_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    chat.reset_state()
    awareness.reset_timers()
    awareness_schedule.reset()
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
    awareness_schedule.reset()
    db.admin_pending_reset()
    db.awareness_reset()
    nexus.reset_state()
    awareness.reset_timers()
    main._nexus_visibility.clear()
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()


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


def install_transport(monkeypatch):
    passes: list[dict] = []

    async def _awareness(transcript, context="", *, tools=None, on_tool=None):
        passes.append({"transcript": transcript, "tools": tools})
        return chat.AwarenessReply(text=json.dumps({"relevant": True}), model="stub", turns=1)

    monkeypatch.setattr(main.chat, "awareness", _awareness)
    return passes


def message(text):
    return SimpleNamespace(
        message_id=10,
        photo=None,
        video=None,
        animation=None,
        video_note=None,
        sticker=None,
        voice=None,
        audio=None,
        document=None,
        text=text,
        caption=None,
        reply_to_message=None,
    )


def room_of(chat_id=CHAT):
    return SimpleNamespace(id=chat_id, type="supergroup", title="Group")


def user_of(user_id=MEMBER):
    return SimpleNamespace(id=user_id, full_name="تستر", username="tester", is_bot=False)


def capture(ctx, text, *, chat_id=CHAT):
    """Drive the real capture path, which is what writes the hint."""
    principal = rbac.resolve(MEMBER)
    return asyncio.run(
        main._awareness_capture(
            ctx,
            room_of(chat_id),
            user_of(),
            message(text),
            text,
            principal,
        )
    )


def age_room(chat_id=CHAT, *, seconds=120):
    db._exec(
        "UPDATE group_messages SET at = at - ? WHERE chat_id = ?",
        (int(seconds), int(chat_id)),
    )


def pending_row(chat_id=CHAT):
    for row in awareness.pending():
        if int(row["chat_id"]) == int(chat_id):
            return row
    raise AssertionError("the capture produced no pending row")


def test_capture_records_a_hint_and_a_low_room_is_deferred(schedule_env, monkeypatch):
    """The whole seam, end to end: capture notes; run_room spends or waits."""
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    # A self-contained message: the hint is LOW.
    assert capture(ctx, "سلام") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_LOW

    age_room(CHAT)
    row = pending_row(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, row)) is False
    assert passes == [], "a low-value room must not spend the rationed request"
    # Deferred, not read: the hint is still there for the next deadline.
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_LOW


def test_a_room_that_needs_reading_is_read_and_its_hint_is_spent(
    schedule_env, monkeypatch
):
    """The safe direction: evidence that the room needs reading is never deferred."""
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    # A message with no antecedent of its own: the hint is HIGH.
    assert capture(ctx, "همونو بزن") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_HIGH

    age_room(CHAT)
    row = pending_row(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, row)) is True
    assert len(passes) == 1
    # The batch has been read, so the hint it produced is spent.
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_NONE


def test_a_high_message_raises_the_room_above_its_earlier_chatter(
    schedule_env, monkeypatch
):
    """One message that needs the room outweighs the chatter around it."""
    install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "سلام") is True
    assert capture(ctx, "هوا امروز خیلی خوبه") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_LOW
    assert capture(ctx, "بازش کن") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_HIGH

    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is True


def test_the_urgent_path_ignores_the_hint(schedule_env, monkeypatch):
    """A prompted reading is never delayed by the scheduler's own opinion."""
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "سلام") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_LOW
    age_room(CHAT)
    row = pending_row(CHAT)

    assert asyncio.run(main._awareness_run_room(ctx, row, urgent=True)) is True
    assert len(passes) == 1


def test_a_room_with_no_hint_is_read_exactly_as_before(schedule_env, monkeypatch):
    """Capture through ``awareness.capture`` directly — no hint — must not defer."""
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert awareness.capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "تستر", "سلام")
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_NONE

    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is True
    assert len(passes) == 1


def test_a_broken_reader_still_captures_the_message(schedule_env, monkeypatch):
    """A classification failure is not a capture failure.

    The reader is never worth a message: the capture is recorded, the class
    falls back to *no evidence*, and the room is read on its deadline exactly as
    it was before this module existed. The wrong fallback — LOW — would defer
    every room in the deployment whenever the reader broke, which is a slowdown
    wearing a scheduling choice's clothes.
    """
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    def boom(*_args, **_kwargs):
        raise RuntimeError("reader is broken")

    monkeypatch.setattr(context_plan, "read", boom)
    assert capture(ctx, "همونو بزن") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_NONE

    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is True
    assert len(passes) == 1


def test_a_room_the_server_is_waiting_on_is_never_deferred(schedule_env, monkeypatch):
    """The highest-stakes false negative, closed at the real path.

    A confirmation («تأیید میکنم») is self-contained, so the hint is LOW — and
    the owner's already-approved admin action is consumed *by the pass*. The
    caller tells the scheduler it is waiting, and the room is read.
    """
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "تأیید می‌کنم") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_LOW
    # Without the flag, the LOW room is deferred...
    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is False
    assert passes == []

    # ...and with an unapproved action waiting on the room, it is not.
    db.admin_pending_add(
        "req-1",
        actor_id=OWNER,
        chat_id=CHAT,
        operation="promote_member",
        subject="",
        payload="{}",
        expires_at=int(time.time()) + 600,
    )
    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is True
    assert len(passes) == 1


def test_a_room_the_allowance_cannot_serve_keeps_its_hint(schedule_env, monkeypatch):
    """A refusal that was not about content must not spend the evidence.

    The hint is dropped only when a pass actually reads the batch. A room the
    day cannot afford yet is still the room it was, and demoting it for a reason
    that had nothing to do with its content would be the scheduler deciding on
    the wrong question.
    """
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "همونو بزن") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_HIGH

    age_room(CHAT)
    # Past the minimum interval, so ``due`` says yes, but the day's allowance
    # gap is hundreds of seconds wide, so the pass is refused on cost alone.
    main._awareness_last_pass[CHAT] = time.time() - 30.0
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is False
    assert passes == []
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_HIGH


# ── U's residual at the real path: a room Nexus asked a question in ───────
def test_a_room_nexus_asked_a_question_in_is_not_deferred(schedule_env, monkeypatch):
    """The residual, closed where it actually bites.

    Nexus asks a question; the reply («بله») is self-contained, so the hint is
    LOW and the naive scheduler would postpone the very pass that should read the
    answer. The stamp is written where Nexus's own outbound reply is recorded —
    the production function, not a test double — and the room is read.
    """
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "سلام") is True
    assert awareness_schedule.priority(CHAT) == awareness_schedule.P_LOW
    # Nexus asked something in this room, exactly as the send path records it.
    main._awareness_note_reply(CHAT, "ادامه بدهم؟")
    assert awareness_schedule.awaiting(CHAT) is True

    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is True
    assert len(passes) == 1
    # The pass read the room, so the exchange is resolved and the stamp is spent:
    # the stamp can buy at most one undeferred pass per question.
    assert awareness_schedule.awaiting(CHAT) is False


def test_a_statement_from_nexus_leaves_the_low_room_deferred(schedule_env, monkeypatch):
    """The negative control: without a question, a LOW room is still postponed."""
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "سلام") is True
    main._awareness_note_reply(CHAT, "انجام شد")
    assert awareness_schedule.awaiting(CHAT) is False

    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is False
    assert passes == [], "a statement is not a reason to spend the request"


def test_an_awaiting_other_room_does_not_undefer_this_room(schedule_env, monkeypatch):
    """Isolation at the real path: the stamp is keyed by room."""
    passes = install_transport(monkeypatch)
    ctx = ctx_for(FakeBot())

    assert capture(ctx, "سلام") is True
    main._awareness_note_reply(OTHER_CHAT, "ادامه بدهم؟")
    assert awareness_schedule.awaiting(OTHER_CHAT) is True
    assert awareness_schedule.awaiting(CHAT) is False

    age_room(CHAT)
    assert asyncio.run(main._awareness_run_room(ctx, pending_row(CHAT))) is False
    assert passes == []
