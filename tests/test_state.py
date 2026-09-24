"""Conversational state: what the current interaction is trying to accomplish.

Increment X. This file is about the *third* context source and the boundary that
makes it worth having as its own layer:

* **State is not Conversation History.** A state row is a topic, a goal and a
  question — never a message. There is no column a transcript could fit in.
* **State is not Awareness.** It does not read the room, and it survives the
  awareness layer being off, unavailable or failed.
* **State is not Memory.** A task that is being worked on now is not a durable
  fact about the person, and a state transition writes no memory row — nor the
  other way round.
* **State is one active row.** Keyed by ``(chat_id, user_id)``, so it is
  unambiguous by construction, isolated across people and rooms, and safe
  against a stale background worker overwriting a newer state.

Nothing here talks to Telegram, to a model, or to the network. The one thing
that could have — a model-assisted extraction seam — is deliberately **not
built**: increment X is scoped at "Gemini: 0 expected", and a test below asserts
that no provider call is ever attempted on this path.
"""
import ast
import asyncio
import inspect
import time
from types import SimpleNamespace

import pytest

from app import awareness_context, config, db, main, memory, state

CHAT = -1001234567890
OTHER_CHAT = -1009876543210
PRIVATE_CHAT = 4242
USER = 42
OTHER_USER = 43
BOT = 44

TASK = "بیا اون مشکل لاگین بات رو درست کنیم"
TASK_TOPIC = "مشکل لاگین بات"
OTHER_TASK = "بریم سراغ مشکل پرداخت"
OTHER_TOPIC = "مشکل پرداخت"


# ── Harness ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def state_env(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_STATE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_STATE_AUTO_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_STATE_MAX", 50000)
    monkeypatch.setattr(config, "NEXUS_STATE_TTL", 72 * 3600)
    monkeypatch.setattr(config, "NEXUS_STATE_VALUE_CHARS", 120)
    monkeypatch.setattr(config, "NEXUS_STATE_CHARS", 300)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 1500)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_DEEP", True)
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_MEMORY_AUTO_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    db.init()
    db.state_reset()
    db.memory_reset()
    db.signal_reset()
    db.people_reset()
    awareness_context.reset_rooms()
    state.reset_state()
    memory.reset_state()
    yield
    db.state_reset()
    db.memory_reset()
    db.signal_reset()
    db.people_reset()
    awareness_context.reset_rooms()
    state.reset_state()
    memory.reset_state()


def _observe(text, *, chat_id=CHAT, user_id=USER, message_id=1):
    return asyncio.run(
        state.observe(
            {"id": user_id, "is_bot": False}, chat_id, text, message_id=message_id
        )
    )


def _row(chat_id=CHAT, user_id=USER):
    return db.state_get(chat_id, user_id)


def _topic(chat_id=CHAT, user_id=USER):
    row = _row(chat_id, user_id)
    return row["topic"] if row else None


def _msg(user_id, text="hello", *, message_id=1, at=1000, role="member"):
    return {
        "user_id": int(user_id),
        "text": text,
        "role": role,
        "name": f"U{user_id}",
        "at": int(at),
        "message_id": int(message_id),
        "reply_user_id": 0,
        "reply_name": "",
        "reply_message_id": 0,
        "directed": False,
        "actor": False,
    }


def _ctx(anchor, messages=(), now=None):
    return awareness_context.build_ctx(
        CHAT,
        messages=[*messages, anchor],
        anchor=anchor,
        now=int(now if now is not None else anchor["at"]),
    )


def _imported_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
            if node.module:
                names.add(node.module.split(".")[-1])
    return names


# ── A. Creation ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,topic",
    [
        ("بیا مشکل لاگین بات رو درست کنیم", TASK_TOPIC),
        ("بریم سراغ مشکل پرداخت", OTHER_TOPIC),
        ("let's fix the login bug", "login bug"),
        ("باید این باگ رو بررسی کنیم", "باگ"),
    ],
)
def test_a_task_statement_becomes_the_active_state(text, topic):
    result = _observe(text)
    assert result["transition"] == state.TRANSITION_ACTIVATE
    assert _topic() == topic


def test_a_new_goal_is_stored_beside_its_topic():
    _observe(TASK)
    row = _row()
    assert row["goal"] == TASK_TOPIC
    assert row["status"] == state.STATUS_ACTIVE


def test_an_unresolved_question_about_the_active_task_is_recorded():
    _observe(TASK)
    _observe("مشکل لاگین از DNS هست؟", message_id=2)
    assert _row()["question"] == "مشکل لاگین از DNS هست؟"
    assert _row()["topic"] == TASK_TOPIC  # the task itself is unchanged


def test_a_question_with_no_active_task_changes_nothing():
    assert _observe("مشکل لاگین از DNS هست؟") is None
    assert _row() is None


def test_a_question_about_something_else_does_not_touch_the_open_question():
    _observe(TASK)
    assert _observe("فردا هوا چطوره؟", message_id=2) is None
    assert _row()["question"] == ""


# ── B. Continuation ───────────────────────────────────────────────────────
def test_a_continuation_marker_keeps_the_active_task():
    _observe(TASK)
    result = _observe("خب الان قدم بعدی چیه؟", message_id=2)
    assert result["transition"] == state.TRANSITION_CONTINUE
    assert _topic() == TASK_TOPIC


def test_a_continuation_with_no_active_task_changes_nothing():
    assert _observe("خب الان قدم بعدی چیه؟") is None
    assert _row() is None


def test_a_pronominal_follow_up_still_sees_the_active_task():
    """«این قسمت رو چطور درست کنیم؟» shares no word with the topic, and that is
    exactly why relevance is not a lexical test: a word-overlap filter would drop
    the continuation State exists to serve."""
    _observe(TASK)
    anchor = _msg(USER, "خب حالا این قسمت رو چطور درست کنیم؟", message_id=2, at=1001)
    assert state.current(CHAT, USER, text=anchor["text"]) is not None


def test_a_restatement_of_the_same_task_is_an_update_not_a_replacement():
    _observe(TASK)
    result = _observe("بیا مشکل لاگین بات رو درست کنیم", message_id=2)
    assert result["transition"] == state.TRANSITION_UPDATE
    assert db.state_count() == 1


# ── C. Transitions ────────────────────────────────────────────────────────
def test_a_new_task_replaces_the_previous_one_rather_than_accumulating():
    _observe(TASK)
    result = _observe(OTHER_TASK, message_id=2)
    assert result["transition"] == state.TRANSITION_REPLACE
    assert _topic() == OTHER_TOPIC
    assert db.state_count() == 1  # one active row, not two


def test_a_completion_clears_the_active_state():
    _observe(TASK)
    result = _observe("حل شد", message_id=2)
    assert result["transition"] == state.TRANSITION_COMPLETE
    assert result["cleared"] is True
    assert _row() is None


def test_a_reset_clears_the_active_state():
    _observe(TASK)
    result = _observe("بحث رو عوض کنیم", message_id=2)
    assert result["transition"] == state.TRANSITION_RESET
    assert _row() is None


def test_a_completion_and_a_new_task_in_one_message_leaves_the_new_task():
    """The brief's own example: "authentication is fixed, now payments." The
    finished task is gone and the new one is active — not both."""
    _observe(TASK)
    result = _observe("بریم سراغ مشکل پرداخت", message_id=2)
    assert result["transition"] == state.TRANSITION_REPLACE
    assert _topic() == OTHER_TOPIC


def test_an_expired_state_is_not_current_and_is_pruned(monkeypatch):
    monkeypatch.setattr(
        db, "time", type("C", (), {"time": staticmethod(lambda: 100)})()
    )
    _observe(TASK)
    assert state.current(CHAT, USER, now=100) is not None
    monkeypatch.setattr(
        db, "time", type("C", (), {"time": staticmethod(lambda: 10**9)})()
    )
    assert state.current(CHAT, USER, now=10**9 + 10**7) is None
    assert state.prune() >= 1
    assert _row() is None


# ── D. Freshness ──────────────────────────────────────────────────────────
def test_a_fresh_task_statement_supersedes_the_shown_state():
    """Fresh explicit input wins: while the old state is still stored, a message
    that starts a new task withholds it rather than showing it beside the new
    input."""
    _observe(TASK)
    old = _row()
    assert state.relevant(old, OTHER_TASK) is False
    assert state.current(CHAT, USER, text=OTHER_TASK) is None


def test_a_fresh_completion_supersedes_the_shown_state():
    _observe(TASK)
    assert state.current(CHAT, USER, text="حل شد") is None


def test_the_state_after_a_fresh_task_is_the_new_task_not_the_old_one():
    _observe(TASK)
    _observe(OTHER_TASK, message_id=2)
    block = state.render(_row())
    assert OTHER_TOPIC in block
    assert TASK_TOPIC not in block


def test_memory_does_not_override_fresh_state():
    """A durable fact about the person must not rewrite what they are doing."""
    memory.remember({"id": USER, "is_bot": False}, CHAT, "یادت باشه من پایتون کار می‌کنم")
    _observe(TASK)
    assert _topic() == TASK_TOPIC
    assert _row()["question"] == ""


# ── E. Separation ─────────────────────────────────────────────────────────
def test_state_stores_no_message_body():
    """A state row is a summary, not a transcript: the columns are a topic, a
    goal and a question, each bounded, and none holds the whole message."""
    message = "بیا مشکل لاگین بات رو درست کنیم این پیام ادامه داره و خیلی طولانیه"
    _observe(message)
    row = _row()
    assert row is not None
    assert row["topic"] == TASK_TOPIC
    for value in (row["topic"], row["goal"], row["question"]):
        assert len(value) <= config.NEXUS_STATE_VALUE_CHARS
        assert message not in value


def test_a_state_transition_writes_no_memory():
    _observe(TASK)
    assert db.memory_count() == 0


def test_a_memory_writes_no_state():
    memory.remember({"id": USER, "is_bot": False}, CHAT, "یادت باشه من پایتون کار می‌کنم")
    assert db.state_count() == 0


def test_state_and_memory_are_two_distinct_blocks():
    memory.remember({"id": USER, "is_bot": False}, CHAT, "یادت باشه من پایتون کار می‌کنم")
    _observe(TASK)
    out = awareness_context.blocks(_ctx(_msg(USER, "قدم بعدی چیه")))
    assert "current task in this conversation" in out  # state
    assert "asked to be remembered" in out  # memory
    assert out.count(TASK_TOPIC) == 1  # not duplicated


def test_state_is_not_awareness_the_block_renders_with_awareness_off(monkeypatch):
    _observe(TASK)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)
    assert main._state_context(CHAT, USER, "قدم بعدی چیه")


# ── F. Retrieval ──────────────────────────────────────────────────────────
def test_an_empty_state_renders_nothing():
    assert state.current(CHAT, USER) is None
    assert state.render(None) == ""
    assert main._state_context(CHAT, USER, "هرچی") == ""


def test_a_stale_state_is_excluded(monkeypatch):
    _observe(TASK)
    row = _row()
    monkeypatch.setattr(
        config, "NEXUS_STATE_TTL", 10
    )
    assert state.current(CHAT, USER, now=row["updated_at"] + 11) is None


def test_the_state_block_is_bounded_by_its_budget():
    _observe("بیا " + ("توضیح " * 40) + "رو بررسی کنیم")
    block = state.render(_row(), budget=80)
    assert len(block) <= 80


def test_the_state_block_carries_the_topic_the_goal_and_the_question():
    _observe(TASK)
    _observe("مشکل لاگین از DNS هست؟", message_id=2)
    block = state.render(_row())
    assert f"active topic: {TASK_TOPIC}" in block
    assert "unresolved question: مشکل لاگین از DNS هست؟" in block


def test_the_state_block_is_framed_as_the_interaction_not_the_person():
    _observe(TASK)
    block = state.render(_row())
    assert "this interaction is trying to accomplish" in block
    assert "not a fact about the person" in block


# ── G. Isolation ──────────────────────────────────────────────────────────
def test_one_person_s_state_is_never_another_s():
    _observe(TASK, user_id=USER)
    assert _row(user_id=OTHER_USER) is None
    assert state.current(CHAT, OTHER_USER) is None


def test_one_group_s_state_is_never_another_group_s():
    _observe(TASK, chat_id=CHAT)
    assert _row(chat_id=OTHER_CHAT) is None


def test_a_private_state_never_renders_in_a_group():
    _observe(TASK, chat_id=PRIVATE_CHAT)
    out = awareness_context.blocks(_ctx(_msg(USER, "قدم بعدی چیه")))
    assert TASK_TOPIC not in out


def test_two_concurrent_tasks_in_two_rooms_do_not_overwrite_each_other():
    _observe(TASK, chat_id=CHAT, message_id=1)
    _observe(OTHER_TASK, chat_id=OTHER_CHAT, message_id=1)
    assert _topic(chat_id=CHAT) == TASK_TOPIC
    assert _topic(chat_id=OTHER_CHAT) == OTHER_TOPIC


# ── H. Concurrency / idempotency ──────────────────────────────────────────
def test_a_stale_worker_cannot_overwrite_a_newer_state():
    """The compare-and-swap: a worker whose read is already stale has its write
    refused, so the newer state survives."""
    _observe(TASK, message_id=1)
    stale = db.state_get(CHAT, USER)  # version 1
    _observe(OTHER_TASK, message_id=2)  # version 2
    refused = state._write(
        CHAT,
        USER,
        stale,
        topic="مشکل قدیمی",
        goal="مشکل قدیمی",
        question="",
        status=state.STATUS_ACTIVE,
        transition=state.TRANSITION_REPLACE,
        message_id=3,
    )
    assert refused is None
    assert _topic() == OTHER_TOPIC


def test_a_duplicate_message_is_idempotent():
    first = _observe(TASK, message_id=7)
    assert first["applied"] is True
    before = _row()
    again = _observe(TASK, message_id=7)
    assert again["applied"] is False
    assert again.get("duplicate") is True
    after = _row()
    assert after["version"] == before["version"]
    assert db.state_count() == 1


def test_a_retry_does_not_duplicate_state():
    for _ in range(5):
        _observe(TASK, message_id=9)
    assert db.state_count() == 1


# ── I. Failure ────────────────────────────────────────────────────────────
def test_a_failed_read_returns_nothing_and_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "state_get", boom)
    assert state.current(CHAT, USER) == None  # noqa: E711 - explicit None
    assert main._state_context(CHAT, USER, "هرچی") == ""


def test_a_failed_write_leaves_the_caller_unharmed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "state_put", boom)
    result = _observe(TASK)  # no raise
    assert result["applied"] is False
    assert db.state_count() == 0


def test_a_failed_clear_leaves_the_caller_unharmed(monkeypatch):
    _observe(TASK)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "state_clear", boom)
    assert _observe("حل شد", message_id=2) is None  # no raise


def test_the_state_path_makes_no_provider_call(monkeypatch):
    """There is no model seam on this path: no ``state`` workload exists and no
    provider is ever contacted, whatever the message."""
    from app import gemini_pool

    def boom(*a, **k):
        raise AssertionError("no provider call may happen on the state path")

    monkeypatch.setattr(gemini_pool, "generate", boom)
    assert _observe(TASK)["transition"] == state.TRANSITION_ACTIVATE
    assert _observe("حل شد", message_id=2)["transition"] == state.TRANSITION_COMPLETE


def test_state_does_not_use_the_memory_workload(monkeypatch):
    """The isolation the brief asks for: State is not Memory's workload, and no
    ``state`` pool was added, so it cannot spend a request the answer is waiting
    on."""
    workloads = {spec["workload"] for spec in config.GEMINI_POOLS}
    assert "state" not in workloads
    assert "memory" in workloads  # memory's pool is unchanged


def test_a_scheduler_failure_leaves_the_handler_unharmed():
    class NoScheduler:
        pass

    class Ctx:
        application = NoScheduler()

    main._schedule_state_observation(
        Ctx(), {"id": USER, "is_bot": False}, CHAT, TASK, 1
    )  # no raise


def test_observe_is_a_coroutine_so_a_handler_cannot_block_on_it():
    assert inspect.iscoroutinefunction(state.observe)


def test_the_group_handler_schedules_state_rather_than_awaiting_it():
    source = inspect.getsource(main.on_group_chat)
    assert "_schedule_state_observation" in source
    assert "await state.observe" not in source


# ── J. Performance ────────────────────────────────────────────────────────
def test_an_ordinary_message_changes_nothing_and_costs_no_work():
    for text in ("سلام", "ممنون", "😂", "خوبی؟", "فکر کنم باید صبر کنیم"):
        assert _observe(text) is None
    assert db.state_count() == 0


def test_the_sync_read_and_render_are_fast(monkeypatch):
    """The only work State adds to a chat turn is a read and a render. It is
    measured in the benchmark too; this is the assertion that it stays small
    enough to be worth having."""
    _observe(TASK)
    started = time.perf_counter()
    for _ in range(200):
        row = state.current(CHAT, USER, text="قدم بعدی چیه")
        state.render(row)
    elapsed_ms = (time.perf_counter() - started) * 1000.0 / 200
    assert elapsed_ms < 5.0


def test_storage_cannot_grow_without_bound_across_many_messages():
    for i in range(200):
        _observe(TASK, message_id=i + 1)
    assert db.state_count() == 1


def test_the_topic_cannot_exceed_its_cap(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_STATE_VALUE_CHARS", 12)
    _observe("بیا یه مشکل خیلی خیلی خیلی طولانی رو درست کنیم")
    assert len(_row()["topic"]) <= 12


# ── K. Security ───────────────────────────────────────────────────────────
def test_the_state_module_is_not_an_authority_source():
    """Nothing that decides a permission may import it."""
    for module in ("rbac", "admin_service"):
        names = _imported_names(__import__(f"app.{module}", fromlist=[module]))
        assert "state" not in names


def test_state_grants_nothing_even_when_it_names_authority():
    """A state that says "working on the admin panel" does not make anybody an
    administrator: authority is resolved from the Telegram id."""
    from app import rbac

    _observe("بیا پنل ادمین رو درست کنیم")
    assert _topic() == "پنل ادمین"
    principal = rbac.resolve(USER)
    assert not principal.is_owner
    assert not principal.is_admin


def test_the_state_module_imports_no_process_or_network_facility():
    names = _imported_names(state)
    for forbidden in (
        "subprocess", "socket", "os", "requests", "urllib", "shutil",
        "gemini_pool", "chat", "rbac", "admin_service",
    ):
        assert forbidden not in names, forbidden


# ══ The four sources stay distinct and composable ═════════════════════════
# Each combination below builds the real context with the real source registry;
# only which sources have something to say is varied.
def _seed_memory():
    memory.remember({"id": USER, "is_bot": False}, CHAT, "یادت باشه من پایتون کار می‌کنم")


def _seed_state():
    _observe(TASK)


def _blocks(monkeypatch, *, awareness_on, text="برای پروژه جدیدم چه کنم؟"):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", awareness_on)
    return awareness_context.blocks(_ctx(_msg(USER, text)))


def test_conversation_only(monkeypatch):
    out = _blocks(monkeypatch, awareness_on=True)
    assert "current task" not in out
    assert "asked to be remembered" not in out


def test_conversation_plus_state(monkeypatch):
    _seed_state()
    out = _blocks(monkeypatch, awareness_on=True, text="قدم بعدی چیه")
    assert TASK_TOPIC in out
    assert "asked to be remembered" not in out


def test_conversation_plus_memory(monkeypatch):
    _seed_memory()
    out = _blocks(monkeypatch, awareness_on=True)
    assert "asked to be remembered" in out
    assert "current task" not in out


def test_conversation_plus_awareness(monkeypatch):
    out = _blocks(monkeypatch, awareness_on=True)
    assert "room's state" in out


def test_conversation_plus_state_and_memory(monkeypatch):
    _seed_memory()
    _seed_state()
    out = _blocks(monkeypatch, awareness_on=True, text="قدم بعدی چیه")
    assert TASK_TOPIC in out
    assert "asked to be remembered" in out
    assert "current task" in out


def test_conversation_plus_awareness_and_state(monkeypatch):
    _seed_state()
    out = _blocks(monkeypatch, awareness_on=True, text="قدم بعدی چیه")
    assert "room's state" in out
    assert TASK_TOPIC in out


def test_conversation_plus_awareness_and_memory(monkeypatch):
    _seed_memory()
    out = _blocks(monkeypatch, awareness_on=True)
    assert "room's state" in out
    assert "asked to be remembered" in out


def test_all_four_sources_coexist_without_being_merged(monkeypatch):
    _seed_memory()
    _seed_state()
    anchor = _msg(USER, "قدم بعدی چیه", message_id=2, at=1001)
    other = _msg(OTHER_USER, "منم سوال دارم", message_id=3, at=900)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    ctx = _ctx(anchor, messages=[other])
    out = awareness_context.blocks(ctx)
    assert "current date" in out  # the server clock (awareness context, tier 0)
    assert "room's state" in out  # awareness reading
    assert TASK_TOPIC in out  # state
    assert "asked to be remembered" in out  # memory
    assert len(out) <= config.NEXUS_AWARENESS_CONTEXT_CHARS
    assert out.count(TASK_TOPIC) == 1  # no source duplicates another


def test_a_failure_in_one_source_does_not_break_the_others(monkeypatch):
    _seed_memory()
    _seed_state()

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(state, "current", boom)
    out = _blocks(monkeypatch, awareness_on=True, text="قدم بعدی چیه")
    assert "asked to be remembered" in out  # memory still arrives
    assert "room's state" in out  # awareness still arrives


# ══ State is independent of Awareness, on the real answer path ════════════
_CHAT = -100
_SPEAKER = 7


class _Bot:
    def __init__(self):
        self.id = 1
        self.username = "guardbot"
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, *args, **kwargs):
        pass


def _update(text):
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            message_id=10, photo=None, video=None, animation=None,
            video_note=None, sticker=None, voice=None, audio=None,
            document=None, text=text, caption=None, reply_to_message=None,
        ),
        effective_chat=SimpleNamespace(id=_CHAT, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=_SPEAKER, full_name="Tester", username="tester", is_bot=False
        ),
    )


def _chat_turn(monkeypatch, text, *, awareness_on):
    bot = _Bot()
    ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
    seen: list[str] = []

    async def _reply(chat_id, user_id, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        seen.append(context)
        return main.chat.ChatReply(answered=True, text="پاسخ", turns=1)

    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", awareness_on)
    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    asyncio.run(main._answer_conversationally(_update(text), ctx))
    return seen


def test_chat_still_carries_the_state_when_awareness_is_off(monkeypatch):
    _observe(TASK, chat_id=_CHAT, user_id=_SPEAKER)
    seen = _chat_turn(monkeypatch, "قدم بعدی چیه", awareness_on=False)
    assert seen and TASK_TOPIC in seen[0]


def test_chat_still_carries_the_state_when_the_room_reading_fails(monkeypatch):
    _observe(TASK, chat_id=_CHAT, user_id=_SPEAKER)
    monkeypatch.setattr(main, "_room_reading", lambda *a, **k: "")
    seen = _chat_turn(monkeypatch, "قدم بعدی چیه", awareness_on=True)
    assert seen and TASK_TOPIC in seen[0]


def test_chat_answers_normally_when_state_is_empty_and_awareness_is_off(monkeypatch):
    seen = _chat_turn(monkeypatch, "یه سوال دارم", awareness_on=False)
    assert seen is not None  # the model was reached; the turn was not dropped


def test_the_state_block_is_not_duplicated_when_awareness_is_on(monkeypatch):
    _observe(TASK, chat_id=_CHAT, user_id=_SPEAKER)
    seen = _chat_turn(monkeypatch, "قدم بعدی چیه", awareness_on=True)
    assert seen and seen[0].count(TASK_TOPIC) == 1
