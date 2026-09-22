"""The staged awareness context: cheap always, deep only when the batch asks.

Two properties are what this suite is about, and they pull in opposite
directions:

* **Fullest useful picture.** A pass should know what the room is, who was here
  a moment ago, what has just been done administratively, and who the batch is
  about — facts the server already holds and the transcript does not say.
* **Not preloaded without reason.** None of the expensive half may be built for
  a batch that does not call for it. The awareness allowance is rationed in
  *requests*, so context nobody asked for is paid on every pass.

So the assertions come in pairs: a source renders when its predicate holds, and
does not render when it does not. The registry is also asserted to be
well-formed, because it is the seam the owner asked to be extensible — a new
source is an entry in ``SOURCES`` and nothing else.

Nothing here talks to Telegram, to a model, or to the network.
"""
import ast
import inspect
import time

import pytest

from app import awareness, awareness_context, config, db, main

OWNER = 999
ADMIN = 556
MEMBER = 42
TARGET = 43
OTHER = 44
CHAT = -1001234567890


# ── Harness ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def context_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin"])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 1500)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_DEEP", True)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ADMIN_ACTIONS", 5)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_REFERENCED_PEOPLE", 4)
    db.init()
    db.admin_reset()
    db.people_reset()
    db.awareness_reset()
    awareness.reset_switch()
    awareness_context.reset_rooms()
    yield
    db.admin_reset()
    db.people_reset()
    db.awareness_reset()
    awareness_context.reset_rooms()


def _msg(
    user_id,
    text="hello",
    *,
    role="member",
    name="Someone",
    at=0,
    reply_user_id=0,
    reply_name="",
    directed=False,
    actor=False,
):
    """One window row, with the columns ``db.group_window`` returns."""
    return {
        "user_id": int(user_id),
        "text": text,
        "role": role,
        "name": name,
        "at": int(at or time.time()),
        "reply_user_id": int(reply_user_id),
        "reply_name": reply_name,
        "directed": bool(directed),
        "actor": bool(actor),
    }


def ctx_of(messages=(), *, anchor=None, now=0, chat_id=CHAT):
    return awareness_context.build_ctx(
        chat_id, messages=list(messages), anchor=anchor, now=now
    )


def _audit(action="mute", *, actor=ADMIN, target=TARGET, at=None, chat_id=CHAT):
    db.audit_write(
        actor, action, outcome="ok", target_id=target, chat_id=chat_id,
        interface="python",
    )
    if at is not None:
        db._exec(
            "UPDATE admin_audit SET at=? WHERE actor_id=? AND action=?",
            (int(at), actor, action),
        )


def _imported_names(module) -> set[str]:
    """The modules a source file actually imports, parsed rather than grepped.

    A comment that mentions ``chat`` is not an import, and a test that cannot
    tell the difference fails on prose. This is the precise version of "does A
    depend on B".
    """
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


# ── Tier 0: always, and free ──────────────────────────────────────────────
def test_the_room_is_rendered_from_the_handlers_cache():
    awareness_context.note_room(CHAT, "Guard Group", "supergroup")
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "Guard Group" in out
    assert "supergroup" in out


def test_the_room_is_absent_rather_than_an_error_when_nothing_cached_it():
    """A room nobody has cached is a missing sentence, never a failure."""
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "The room this conversation is in" not in out


def test_the_people_the_last_pass_recorded_are_replayed():
    """``awareness.record`` has always written this; nothing read it back."""
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants="admin:Reza:556, member:Sara:42",
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "Reza (556)" in out
    assert "Sara (42)" in out


def test_a_display_name_containing_a_colon_survives_the_round_trip():
    """The name is whatever a person typed, so it may contain the separator."""
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants="member:Ali:Reza:42",
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "Ali:Reza (42)" in out


def test_an_unparseable_participant_entry_is_skipped_not_shown():
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants="garbage, member:Sara:42, :::",
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "Sara (42)" in out
    assert "garbage" not in out


def test_no_stored_row_means_no_remembered_people():
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "last reading" not in out


# ── Tier 1: administrative activity ───────────────────────────────────────
def test_admin_activity_is_not_preloaded_for_an_ordinary_member_batch():
    """The case the whole design exists to exclude."""
    _audit("mute")
    messages = [_msg(MEMBER, "سلام", at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "administrative actions" not in out


def test_admin_activity_is_rendered_when_the_anchor_has_authority():
    _audit("mute")
    messages = [_msg(ADMIN, "اینو ساکت کن", role="admin", at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "administrative actions" in out
    assert "mute" in out
    assert f"on {TARGET}" in out


def test_admin_activity_is_rendered_when_the_batch_carried_the_actor_hint():
    """The hint is enough even when the anchor itself is an ordinary member."""
    _audit("ban")
    messages = [
        _msg(MEMBER, "چی شد؟", at=time.time() - 10),
        _msg(ADMIN, "بنش کن", role="admin", actor=True, at=time.time() - 5),
    ]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "administrative actions" in out
    assert "ban" in out


def test_admin_activity_is_rendered_when_the_batch_addressed_nexus():
    _audit("delete")
    messages = [_msg(MEMBER, "نکسوس؟", directed=True, at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "administrative actions" in out


def test_no_audit_rows_means_no_admin_block_at_all():
    messages = [_msg(ADMIN, "اینو ساکت کن", role="admin", at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "administrative actions" not in out


def test_admin_activity_is_bounded_by_its_own_count(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ADMIN_ACTIONS", 2)
    for index in range(6):
        _audit(f"action-{index}", target=100 + index)
    messages = [_msg(ADMIN, "چه خبر؟", role="admin", at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    shown = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(shown) == 2


def test_an_audit_row_from_another_room_is_not_shown_here():
    """One group's administrative business never appears in another's context."""
    _audit("mute", chat_id=CHAT - 1)
    messages = [_msg(ADMIN, "اینو ساکت کن", role="admin", at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "administrative actions" not in out


# ── Tier 1: the people the batch refers to ────────────────────────────────
def test_nobody_is_described_when_the_batch_has_no_reply_edge():
    messages = [_msg(MEMBER, "بدون پاسخ", at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "People this batch refers to" not in out


def test_a_reply_edge_makes_its_target_worth_describing():
    db.people_remember(CHAT, TARGET, first_name="Sara", username="sara")
    messages = [
        _msg(TARGET, "سلام", name="Sara", at=time.time() - 20),
        _msg(MEMBER, "اینو ببین", reply_user_id=TARGET, reply_name="Sara",
             at=time.time() - 10),
    ]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[1]))
    assert "People this batch refers to" in out
    sara = [line for line in out.splitlines() if f"Sara ({TARGET})" in line]
    assert sara, out
    assert "role " in sara[0]
    assert "@sara" in sara[0]


def test_the_referenced_people_are_bounded_by_their_own_count(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_REFERENCED_PEOPLE", 1)
    messages = [
        _msg(TARGET, "a", at=time.time() - 30),
        _msg(OTHER, "b", at=time.time() - 20),
        _msg(MEMBER, "c", reply_user_id=TARGET, reply_name="Sara",
             at=time.time() - 10),
    ]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[2]))
    described = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(described) == 1


def test_a_person_with_no_record_is_still_described_by_id_and_role():
    """A thin line is better than a missing person: the id is what an action needs."""
    messages = [_msg(MEMBER, "اینو ببین", reply_user_id=TARGET, reply_name="?",
                     at=time.time() - 10)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert f"({TARGET})" in out


# ── Budgets and failure ───────────────────────────────────────────────────
def test_the_total_budget_is_a_hard_ceiling(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 300)
    awareness_context.note_room(CHAT, "A" * 200, "supergroup")
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants=", ".join(f"member:Name{i}:{1000 + i}" for i in range(50)),
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert 0 < len(out) <= 300


def test_a_single_source_cannot_exceed_its_own_budget(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 10_000)
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants=", ".join(f"member:Name{i}:{1000 + i}" for i in range(200)),
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert 0 < len(out) <= 400


def test_the_conditional_tier_can_be_switched_off(monkeypatch):
    """The kill switch removes every extra query a pass could make."""
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_DEEP", False)
    _audit("mute")
    awareness_context.note_room(CHAT, "Guard Group", "supergroup")
    messages = [_msg(ADMIN, "اینو سکوت کن", role="admin", at=time.time() - 5)]
    out = awareness_context.blocks(ctx_of(messages, anchor=messages[0]))
    assert "Guard Group" in out          # tier 0 is unaffected
    assert "administrative actions" not in out


def test_a_source_that_raises_is_skipped_and_the_rest_still_render(monkeypatch):
    def _boom(_ctx):
        raise RuntimeError("this source is broken")

    broken = awareness_context.Source(
        "broken", awareness_context.TIER_ALWAYS, 200, _boom
    )
    monkeypatch.setattr(
        awareness_context, "SOURCES", (broken,) + awareness_context.SOURCES
    )
    awareness_context.note_room(CHAT, "Guard Group", "supergroup")
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "Guard Group" in out
    assert "broken" not in out


def test_a_predicate_that_raises_is_treated_as_not_wanted(monkeypatch):
    def _boom(_ctx):
        raise RuntimeError("this predicate is broken")

    broken = awareness_context.Source(
        "broken", awareness_context.TIER_CONDITIONAL, 200,
        lambda _ctx: "SHOULD NOT APPEAR", _boom,
    )
    monkeypatch.setattr(awareness_context, "SOURCES", (broken,))
    assert awareness_context.blocks(ctx_of([_msg(MEMBER)])) == ""


def test_a_source_with_no_room_left_is_not_rendered_as_a_fragment(monkeypatch):
    """A cut-off sentence tells the model less than nothing."""
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 120)
    awareness_context.note_room(CHAT, "A" * 200, "supergroup")
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants="member:Sara:42",
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert "Sara (42)" not in out


def test_an_empty_room_produces_no_blocks_rather_than_raising():
    assert awareness_context.blocks(ctx_of([])) == ""


# ── The seam ──────────────────────────────────────────────────────────────
def test_every_source_is_well_formed():
    names = [source.name for source in awareness_context.SOURCES]
    assert names == sorted(set(names), key=names.index), "names must be unique"
    for source in awareness_context.SOURCES:
        assert source.tier in (
            awareness_context.TIER_ALWAYS,
            awareness_context.TIER_CONDITIONAL,
        ), source.name
        assert source.budget > 0, source.name
        assert callable(source.render), source.name
        assert callable(source.when), source.name


def test_the_always_tier_comes_before_the_conditional_one():
    tiers = [source.tier for source in awareness_context.SOURCES]
    assert tiers == sorted(tiers)


def test_a_member_only_batch_triggers_no_conditional_source():
    """The load-bearing assertion: nothing deep is built for ordinary talk."""
    _audit("mute")
    messages = [_msg(MEMBER, "سلام", at=time.time() - 5)]
    ctx = ctx_of(messages, anchor=messages[0])
    fired = [
        source.name
        for source in awareness_context.SOURCES
        if source.tier == awareness_context.TIER_CONDITIONAL and source.when(ctx)
    ]
    assert fired == []
    assert awareness_context.blocks(ctx) == ""


# ── Recency on the transcript ─────────────────────────────────────────────
def test_a_transcript_line_carries_how_long_ago_it_was_written():
    now = int(time.time())
    db.group_capture(CHAT, MEMBER, "member", "Reza", "hello", keep=10, message_id=1)
    db._exec("UPDATE group_messages SET at=? WHERE chat_id=?", (now - 120, CHAT))
    rows = db.group_window(CHAT, limit=10)
    rendered = awareness.render(CHAT, messages=rows)
    assert "(+2m)" in rendered


def test_a_line_has_no_age_when_the_caller_does_not_know_the_clock():
    """``_line`` keeps its old shape for a caller that passes no ``now``."""
    line = awareness._line(
        {"user_id": 1, "text": "hi", "name": "A", "role": "member"}
    )
    assert line == "[member] A (1): hi"
    assert awareness._age_mark({"at": int(time.time())}, 0) == ""


def test_a_message_from_the_future_is_not_given_a_negative_age():
    assert awareness._age_mark({"at": int(time.time()) + 60}, int(time.time())) == ""


# ── Wiring, and the boundary that does not move ───────────────────────────
def test_the_pass_context_carries_the_roster_and_the_staged_blocks():
    awareness_context.note_room(CHAT, "Guard Group", "supergroup")
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants="member:Sara:42",
    )
    messages = [_msg(MEMBER, "سلام", at=time.time() - 5)]
    context = main._awareness_context(CHAT, messages=messages, anchor=messages[0])
    assert "Group authority" in context      # the roster is still first
    assert "Guard Group" in context
    assert "Sara (42)" in context


def test_the_context_builder_is_not_wired_to_any_ai_or_action_pipeline():
    """Awareness reads the room; it must not reach a model, a pipeline or an action.

    ``awareness_context`` is imported by ``app/main.py`` and by nothing else, and
    it imports nothing that could send, delete, restrict or ask a model. The
    awareness boundary is unchanged by this stage, and this is the assertion that
    says so structurally rather than by inspection.
    """
    imported = _imported_names(awareness_context)
    assert not (
        imported
        & {
            "telegram",
            "chat",
            "gemini_pool",
            "ai_moderation",
            "ai_intent",
            "transcribe",
            "media",
            "moderation",
            "acquisition",
            "admin_service",
            "admin_tools",
            "agent_bridge",
            "agent_service",
            "agent_poller",
            "main",
            "responses",
        }
    ), imported


def test_the_context_builder_never_sends_or_acts():
    source = inspect.getsource(awareness_context)
    for forbidden in (
        "ctx.bot",
        "send_message",
        "delete_message",
        "ban_chat_member",
        "restrict_chat_member",
        "promote_chat_member",
    ):
        assert forbidden not in source, forbidden


def test_the_awareness_module_does_not_import_the_context_builder():
    """The dependency points one way: ``main`` wires the two together."""
    assert "awareness_context" not in _imported_names(awareness)
