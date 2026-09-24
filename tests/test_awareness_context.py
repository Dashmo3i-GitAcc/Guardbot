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
import datetime as dt
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
    message_id=0,
    reply_message_id=0,
):
    """One window row, with the columns ``db.group_window`` returns."""
    return {
        "user_id": int(user_id),
        "text": text,
        "role": role,
        "name": name,
        "at": int(at or time.time()),
        "message_id": int(message_id),
        "reply_user_id": int(reply_user_id),
        "reply_name": reply_name,
        "reply_message_id": int(reply_message_id),
        "directed": bool(directed),
        "actor": bool(actor),
    }


def ctx_of(messages=(), *, anchor=None, now=0, chat_id=CHAT):
    return awareness_context.build_ctx(
        chat_id, messages=list(messages), anchor=anchor, now=now
    )


def _epoch(iso: str) -> int:
    """A UTC instant, as epoch seconds. Fixed clocks make fixed dates."""
    return int(dt.datetime.fromisoformat(iso).timestamp())


def _other_blocks(ctx) -> str:
    """``blocks()`` with the calendar prefix taken off.

    The date renders for every batch, so every test about the *other* sources now
    has to step over it rather than count its lines as its own. Asserting the
    prefix is there first is what stops this from quietly swallowing a change to
    the block order.
    """
    out = awareness_context.blocks(ctx)
    date = awareness_context._render_calendar(ctx)
    assert date and out.startswith(date), out
    return out[len(date):]


def _source_blocks(ctx, name: str) -> str:
    """One named source's rendering, through the budget machinery.

    Asserting on the concatenation of every tier-0 block would make a test
    about *one* source's budget fail the day a second source is added — which is
    what happened. This asks the machinery for one block by name.
    """
    for source in awareness_context.SOURCES:
        if source.name == name:
            return awareness_context._rendered(source, ctx, source.budget)
    raise AssertionError(f"no source named {name!r}")


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
    out = _other_blocks(ctx_of(messages, anchor=messages[0]))
    shown = [line for line in out.splitlines() if line.startswith("- action-")]
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
    # One source by name: counting every "- " line in the concatenation would
    # make this test fail the day another source renders a list, which is what
    # happened when the reply graph was added.
    out = _source_blocks(ctx_of(messages, anchor=messages[2]), "referenced_people")
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
    """Widened from 300 when the date became the first block.

    At 300 the date alone filled the ceiling and the sources this test was
    written to watch never rendered — so it would have kept passing while
    checking nothing it was named for. 600 is enough for the date, the room and
    part of the roster, which is the competition between sources the ceiling
    exists to arbitrate.
    """
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 600)
    awareness_context.note_room(CHAT, "A" * 200, "supergroup")
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants=", ".join(f"member:Name{i}:{1000 + i}" for i in range(50)),
    )
    out = awareness_context.blocks(ctx_of([_msg(MEMBER)]))
    assert 0 < len(out) <= 600
    assert "A" * 50 in out, "the room block should have rendered too"


def test_a_single_source_cannot_exceed_its_own_budget(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 10_000)
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants=", ".join(f"member:Name{i}:{1000 + i}" for i in range(200)),
    )
    out = _source_blocks(ctx_of([_msg(MEMBER)]), "remembered_people")
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


def test_an_empty_room_produces_the_date_and_nothing_deeper():
    """This used to assert ``blocks() == ""``.

    It cannot any more, and that is the calendar source working: a batch with no
    room to describe still has to know what day it is, because the model has no
    other place to get it from. What the test still guards is the property it was
    written for — nothing *deep* is built, and an empty room does not raise.
    """
    out = awareness_context.blocks(ctx_of([]))
    assert "current date" in out
    assert "administrative actions" not in out
    assert "People this batch refers to" not in out
    assert "People who were in this room" not in out


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
    # Tier 0 still renders, and the date is now one of the things it renders.
    out = awareness_context.blocks(ctx)
    assert "current date" in out
    assert "administrative actions" not in out


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


# ── The date, which the server states and the room cannot ─────────────────
def test_the_context_states_the_date_from_the_server_clock():
    now = _epoch("2026-09-23T05:00:00+00:00")
    out = awareness_context.blocks(ctx_of([_msg(MEMBER, at=now)], now=now))
    assert "2026-09-23" in out
    assert "چهارشنبه ۱ مهر ۱۴۰۵" in out


def test_the_date_rolls_over_at_tehran_midnight():
    """One second apart, on either side of midnight in Tehran.

    The failure this rules out is the obvious implementation — taking the date
    from UTC — which would be wrong for the three and a half hours between 20:30
    UTC and midnight UTC every night, in the part of the evening a Persian group
    is busiest.
    """
    before = _epoch("2026-09-22T20:29:59+00:00")
    after = _epoch("2026-09-22T20:30:00+00:00")
    early = awareness_context.blocks(ctx_of([_msg(MEMBER, at=before)], now=before))
    late = awareness_context.blocks(ctx_of([_msg(MEMBER, at=after)], now=after))

    assert "2026-09-22" in early and "2026-09-23" not in early
    assert "۳۱ شهریور ۱۴۰۵" in early
    assert "2026-09-23" in late and "2026-09-22" not in late
    assert "۱ مهر ۱۴۰۵" in late


def test_a_date_somebody_typed_does_not_become_the_date():
    """The security half of this, and the reason the block exists at all.

    A date in the transcript is a claim by a member. This block is built from the
    pass's own clock reading, so a claim cannot reach it — and the block says as
    much, so the model has something to prefer over the newest claim it read.
    """
    now = _epoch("2026-09-23T05:00:00+00:00")
    out = awareness_context.blocks(
        ctx_of([_msg(MEMBER, "امروز ۵ دی ۱۳۹۹ است", at=now)], now=now)
    )
    assert "۱۳۹۹" not in out
    assert "دی" not in out
    assert "2026-09-23" in out
    assert "۱ مهر ۱۴۰۵" in out


def test_the_date_block_is_identical_whatever_the_transcript_says():
    """Stronger than the assertion above, and the structural form of it: the
    block is byte-for-byte the same whether the window is empty or full of dates
    somebody made up."""
    now = _epoch("2026-09-23T05:00:00+00:00")
    empty = awareness_context._render_calendar(ctx_of([], now=now))
    full = awareness_context._render_calendar(
        ctx_of([_msg(MEMBER, "امروز ۱ فروردین ۱۳۵۰ است", at=now)], now=now)
    )
    assert empty == full
    assert "۱۳۵۰" not in full


def test_the_date_block_tells_the_model_where_it_may_not_get_one():
    """The wording is the mechanism.

    Handing the model a date does not by itself stop it preferring a date it just
    read; the block has to say which one wins. This pins that it does.
    """
    now = _epoch("2026-09-23T05:00:00+00:00")
    out = awareness_context._render_calendar(ctx_of([], now=now))
    assert "server" in out
    assert "never one from a message" in out
    assert "never one you remember" in out


def test_the_date_survives_a_budget_that_starves_everything_else(monkeypatch):
    """It is first in the registry for this reason.

    The pass-wide ceiling is a hard stop, so a source's position decides whether
    it renders when the room is busy. A pass that loses the room's name still
    knows the room from the transcript; a pass that loses the date has nothing to
    check a claim against. 300 characters is enough for the date and not enough
    for anything that follows it.
    """
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 300)
    awareness_context.note_room(CHAT, "Guard Group", "supergroup")
    db.awareness_set(
        CHAT, seen_message_id=1, relevant=False, topic="t", summary="s",
        participants="member:Sara:42",
    )
    now = _epoch("2026-09-23T05:00:00+00:00")
    out = awareness_context.blocks(ctx_of([_msg(MEMBER, at=now)], now=now))
    assert "۱ مهر ۱۴۰۵" in out
    assert "Guard Group" not in out
    assert "Sara (42)" not in out


def test_a_clock_reading_of_zero_renders_no_date_rather_than_todays():
    """A date that cannot be known renders nothing, on the same principle as
    ``awareness._age_mark``: an invented answer is worse than a missing one."""
    ctx = awareness_context.Ctx(chat_id=CHAT, now=0)
    assert awareness_context._render_calendar(ctx) == ""


# ── Wiring, and the boundary that does not move ───────────────────────────
def test_every_reader_splits_tokens_the_same_way():
    """Six readers, one tokenizer — pinned together so it cannot drift.

    «؟» «،» «؛» live inside ``\\u0600-\\u06ff``, so a "split on anything that is
    not a Persian letter" class keeps them glued to the word before it. Every
    lexicon lookup on the last word of a message then fails: «این لینک؟» names no
    thing, «سارا؟» names nobody, «ممنون؟» is not a greeting, and «چی شده؟» is not
    the sentence «چی شده». The polarity reader is the most sensitive of the six,
    because the prohibitor is usually the *last* word: «میشه بنش نکنی؟» carries
    «نکنی؟», which is not «نکنی», and the prohibition read as a request *to* act.

    ``app/addressing.py`` is deliberately not in the set: it splits the same
    string but then keeps only alphanumerics (``_letters``), so the mark is
    removed either way and it never had the bug.

    The pattern is copied into each reader rather than imported, because each one
    is pure at import and importing a shared helper would be a new edge in a graph
    that is asserted elsewhere. The copies are therefore pinned here: if a seventh
    reader is added, or one of these is edited, the test says so.
    """
    import app.discourse as discourse
    import app.entities as entities
    import app.objects as objects
    import app.referents as referents
    import app.requests as requests
    import app.room_state as room_state

    readers = {
        "discourse": discourse,
        "entities": entities,
        "objects": objects,
        "referents": referents,
        "requests": requests,
        "room_state": room_state,
    }
    patterns = {name: module._TOKEN_SPLIT.pattern for name, module in readers.items()}
    assert len(set(patterns.values())) == 1, patterns

    # …and the one pattern splits the Arabic block's punctuation off the word.
    for name, module in readers.items():
        assert module._TOKEN_SPLIT.split("این لینک؟") == ["این", "لینک", ""], name
        assert module._TOKEN_SPLIT.split("سارا؟") == ["سارا", ""], name
        assert module._TOKEN_SPLIT.split("بود،") == ["بود", ""], name
        assert module._TOKEN_SPLIT.split("اینو بن کن؛") == ["اینو", "بن", "کن", ""], name


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


# ── Tier 1: the referent candidates ───────────────────────────────────────
# The block that answers "who does «این» mean" for an instruction the reply edge
# cannot settle. It is the one conditional source that serves the *correctness*
# of an action rather than its context, so the tests pin both when it fires and
# when it deliberately does not.
def _referent_ctx(anchor, messages):
    """A context whose window ends with the anchor, as a real pass always has."""
    return ctx_of([*messages, anchor], anchor=anchor, now=int(anchor["at"]))


def test_an_authority_deictic_instruction_renders_the_candidates():
    anchor = _msg(ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000)
    messages = [
        _msg(TARGET, "سلام", name="Reza", at=960),
        _msg(OTHER, "چطوری", name="Sara", at=980),
    ]
    ctx = _referent_ctx(anchor, messages)
    assert awareness_context._wants_referents(ctx) is True
    out = awareness_context.blocks(ctx)
    assert "اینو" in out
    assert str(TARGET) in out or str(OTHER) in out
    assert "evidence, not a decision" in out


def test_a_reply_instruction_leaves_the_referent_to_instruction_block():
    """A reply edge is the answer; a candidate list beside it is wasted tokens."""
    anchor = _msg(
        ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000,
        reply_user_id=TARGET, reply_name="Reza",
    )
    ctx = _referent_ctx(anchor, [_msg(TARGET, "سلام", name="Reza", at=960)])
    assert awareness_context._wants_referents(ctx) is False
    assert "evidence, not a decision" not in awareness_context.blocks(ctx)


def test_a_member_deictic_does_not_render_the_candidates():
    """A member cannot act, so a ranked list of the room's people is pure cost."""
    anchor = _msg(MEMBER, "اینو بن کن", role="member", name="Someone", at=1000)
    ctx = _referent_ctx(anchor, [_msg(TARGET, "سلام", name="Reza", at=960)])
    assert awareness_context._wants_referents(ctx) is False


def test_a_directed_member_message_still_gets_the_candidates():
    """Nexus was asked something, and the referent is what it was asked about."""
    anchor = _msg(
        MEMBER, "نکسوس اینو بررسی کن", role="member", name="Someone", at=1000,
        directed=True,
    )
    ctx = _referent_ctx(anchor, [_msg(TARGET, "سلام", name="Reza", at=960)])
    assert awareness_context._wants_referents(ctx) is True


def test_the_candidate_list_is_bounded_by_config(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_REFERENTS", 2)
    anchor = _msg(ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000)
    messages = [
        _msg(uid, "سلام", name=f"User{uid}", at=1000 - uid)
        for uid in (TARGET, OTHER, 45, 46, 47)
    ]
    out = awareness_context._render_referent_candidates(_referent_ctx(anchor, messages))
    # One line per candidate, plus the header and the verdict line.
    candidate_lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(candidate_lines) == 2


def test_the_candidates_are_read_from_the_context_not_the_database():
    """It reads the window the pass already read, so it costs no query."""
    anchor = _msg(ADMIN, "ادمینه رو محدود کن", role="admin", name="Admin", at=1000)
    messages = [_msg(ADMIN, "سلام", role="admin", name="Admin", at=990)]
    ctx = _referent_ctx(anchor, messages)
    # Built from a hand-made window and a hand-made anchor: if the source read
    # the database it would find nothing, because nothing was captured.
    out = awareness_context._render_referent_candidates(ctx)
    assert "ادمینه" in out
    assert str(ADMIN) in out


def test_the_referent_block_is_bounded_by_the_pass_ceiling(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 120)
    anchor = _msg(ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000)
    messages = [_msg(TARGET, "سلام", name="Reza", at=960)]
    out = awareness_context.blocks(_referent_ctx(anchor, messages))
    assert len(out) <= 120


# ── The batch's own reading, and the room's open questions ────────────────
def test_the_anchor_act_is_rendered_for_the_model():
    anchor = _msg(ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000)
    out = awareness_context.blocks(_referent_ctx(anchor, []))
    assert "instruction" in out


def test_the_anchor_act_renders_nothing_when_the_words_carry_no_reading():
    """An abstention is silent, not a line saying «unknown» in the prompt."""
    anchor = _msg(MEMBER, "امروز خیلی شلوغ بود", name="Someone", at=1000)
    assert awareness_context._render_anchor_act(_referent_ctx(anchor, [])) == ""


# ── The act and the direction it points in are one block ──────────────────
# «بنش کن» and «بنش نکن» are the same reading to the act reader — both are
# ``instruction`` with the directive «بنش» — and one of them is the message where
# the room is protecting somebody. The direction therefore has to travel with the
# act, in the same source, or an "instruction" line can outlive the negation that
# reverses it.
def test_a_forbidden_action_renders_both_the_act_and_the_direction():
    anchor = _msg(ADMIN, "بنش نکن", role="admin", name="Admin", at=1000)
    out = awareness_context._render_anchor_act(_referent_ctx(anchor, []))
    assert "instruction" in out
    assert "negates" in out


def test_the_direction_line_comes_before_the_act_line():
    """A clip keeps whole lines from the front, so the warning must be first.

    If a budget ever bit into this block, the line that must survive is the one
    saying the message forbids the action — the act line alone is the half-truth.
    """
    anchor = _msg(ADMIN, "بنش نکن", role="admin", name="Admin", at=1000)
    out = awareness_context._render_anchor_act(_referent_ctx(anchor, []))
    assert out.index("negates") < out.index("instruction")


def test_a_bare_affirmative_command_adds_no_direction_line():
    """The act line already says ``instruction``; a direction line on every
    ordinary moderation message would be noise in the prompt."""
    anchor = _msg(ADMIN, "بنش کن", role="admin", name="Admin", at=1000)
    out = awareness_context._render_anchor_act(_referent_ctx(anchor, []))
    assert "instruction" in out
    assert "negates" not in out


def test_the_direction_is_not_rendered_by_any_other_source():
    """One source, so no budget can drop the direction and keep the act."""
    anchor = _msg(ADMIN, "بنش نکن", role="admin", name="Admin", at=1000)
    ctx = _referent_ctx(anchor, [])
    others = [
        source.name
        for source in awareness_context.SOURCES
        if source.name != "anchor_act"
    ]
    for name in others:
        assert "negates" not in _source_blocks(ctx, name), name


# ── …and what the request acts on, in the same block ──────────────────────
# "Instruction, the directive «پاک»" without "acts on a thing" is the other
# half-truth: the model has to join the directive to the object itself, and that
# join is where a person gets banned over a photograph.
def test_the_object_line_travels_with_the_act():
    anchor = _msg(ADMIN, "پاکش کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "[document] report.pdf", name="Reza", at=960)]
    out = awareness_context._render_anchor_act(_referent_ctx(anchor, window))
    assert "instruction" in out
    assert "not a person" in out


def test_the_object_line_comes_before_the_act_line():
    """Both contradicting lines come first: a clip keeps whole lines from the
    front, so what survives must be what contradicts a naive reading."""
    anchor = _msg(ADMIN, "پاکش کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "[document] report.pdf", name="Reza", at=960)]
    out = awareness_context._render_anchor_act(_referent_ctx(anchor, window))
    assert out.index("not a person") < out.index("instruction")


def test_a_person_object_says_so_and_does_not_warn_about_a_thing():
    anchor = _msg(ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "سلام", name="Reza", at=960)]
    out = awareness_context._render_anchor_act(_referent_ctx(anchor, window))
    assert "person" in out
    assert "not a person" not in out


def test_the_object_is_not_rendered_by_any_other_source():
    anchor = _msg(ADMIN, "پاکش کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "[document] report.pdf", name="Reza", at=960)]
    ctx = _referent_ctx(anchor, window)
    for source in awareness_context.SOURCES:
        if source.name == "anchor_act":
            continue
        assert "not a person" not in _source_blocks(ctx, source.name), source.name


def test_the_open_questions_are_rendered():
    anchor = _msg(ADMIN, "خب", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "قیمت چنده؟", name="Reza", at=900, message_id=5)]
    out = awareness_context._render_open_questions(_referent_ctx(anchor, window))
    assert "قیمت چنده؟" in out
    assert "no reply pointing at an answer" in out


def test_an_answered_question_is_not_rendered():
    anchor = _msg(ADMIN, "خب", role="admin", name="Admin", at=1000)
    window = [
        _msg(TARGET, "قیمت چنده؟", name="Reza", at=900, message_id=5),
        _msg(OTHER, "نمیدونم", name="Sara", at=920, reply_user_id=TARGET,
             reply_message_id=5),
    ]
    assert awareness_context._render_open_questions(_referent_ctx(anchor, window)) == ""


def test_the_question_block_reads_the_context_not_the_database():
    """A hand-made window nothing captured: a query would find nothing."""
    anchor = _msg(ADMIN, "خب", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "چرا؟", name="Reza", at=900, message_id=5)]
    ctx = _referent_ctx(anchor, window)
    out = awareness_context._render_open_questions(ctx)
    assert "چرا؟" in out


def test_the_new_sources_are_bounded_by_their_own_budget(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 10_000)
    anchor = _msg(ADMIN, "خب", role="admin", name="Admin", at=1000)
    window = [
        _msg(TARGET, "سوال " + "ب" * 200 + "؟", name="Reza", at=900 + i, message_id=i + 1)
        for i in range(6)
    ]
    ctx = _referent_ctx(anchor, window)
    assert len(_source_blocks(ctx, "open_questions")) <= 500
    assert len(_source_blocks(ctx, "anchor_act")) <= 420
    assert len(_source_blocks(ctx, "anchor_when")) <= 300
    assert len(_source_blocks(ctx, "reply_graph")) <= 600
    assert len(_source_blocks(ctx, "thread")) <= 500
    assert len(_source_blocks(ctx, "entities")) <= 600


def test_the_entities_block_corrects_the_person_lead():
    """The demonstrative may mean the photo, and the block says so.

    As evidence, not as an order: the block's own docstring promises "evidence
    framing, not an instruction", and the order belongs to the block that knows
    the side — the object line, which states it when the verb decides.
    """
    anchor = _msg(ADMIN, "اینو پاک کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "ببین", name="Reza", at=900, message_id=1)]
    window[0]["kind"] = "photo"
    out = awareness_context.blocks(_referent_ctx(anchor, window))
    assert "a photo by" in out
    assert "it is about a thing rather than a person" in out
    assert "do not" not in awareness_context._render_entities(
        _referent_ctx(anchor, window)
    ).lower()


def test_the_entities_block_names_the_class_the_message_uses():
    anchor = _msg(ADMIN, "این لینک چیه", role="admin", name="Admin", at=1000)
    window = [
        _msg(TARGET, "https://example.com/x", name="Reza", at=900, message_id=1)
    ]
    out = awareness_context.blocks(_referent_ctx(anchor, window))
    assert "a link to example.com" in out
    assert "names «لینک»" in out


def test_the_entities_block_is_silent_when_there_is_nothing_to_point_at():
    anchor = _msg(ADMIN, "سلام", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "سلام", name="Reza", at=900, message_id=1)]
    assert awareness_context._render_entities(_referent_ctx(anchor, window)) == ""


def test_the_entities_block_reads_the_context_not_the_database():
    anchor = _msg(ADMIN, "اینو پاک کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "ببین", name="Reza", at=900, message_id=1)]
    window[0]["kind"] = "video"
    ctx = _referent_ctx(anchor, window)
    assert "a video by" in awareness_context._render_entities(ctx)


def test_the_entities_block_does_not_claim_a_pointer_a_greeting_lacks():
    """The window holds a photograph, and the message is a greeting.

    The header says the message *may point at* the things under it, so it must
    not appear — and the block must not carry the "not about a person" line
    either, which reads as an instruction about a message that has no object.
    """
    anchor = _msg(ADMIN, "سلام بچه ها", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "ببین", name="Reza", at=900, message_id=1)]
    window[0]["kind"] = "photo"
    out = awareness_context.blocks(_referent_ctx(anchor, window))
    assert "Things this message may point at" not in out
    assert "not about a person" not in out


def test_the_entities_block_does_not_contradict_the_object_block():
    """The two blocks are read together, so they must agree about the side.

    «اینو بن کن» acts on a member. The object block says so; the entities block
    must not answer with the room's photograph and "do not act on a person".
    """
    anchor = _msg(ADMIN, "اینو بن کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "ببین", name="Reza", at=900, message_id=1)]
    window[0]["kind"] = "photo"
    out = awareness_context.blocks(_referent_ctx(anchor, window))
    assert "acts on a **person**" in out
    assert "Things this message may point at" not in out
    assert "not about a person" not in out


# ── Who is talking to whom, and whether this is still the same thread ─────
def test_the_reply_graph_is_rendered_for_the_model():
    anchor = _msg(ADMIN, "خب", role="admin", name="Admin", at=1000)
    window = [
        _msg(TARGET, "فایل رو فرستادم", name="Reza", at=900, message_id=1),
        _msg(OTHER, "فایل رو دیدم", name="Sara", at=920, message_id=2,
             reply_user_id=TARGET),
        _msg(MEMBER, "فایل مشکل داره", name="Nima", at=940, message_id=3,
             reply_user_id=TARGET),
    ]
    out = awareness_context.blocks(_referent_ctx(anchor, window))
    assert f"{OTHER} → {TARGET}" in out
    assert f"converged on {TARGET} (2 of 2)" in out


def test_the_thread_is_rendered_for_the_model():
    anchor = _msg(ADMIN, "فایل رو دوباره چک کن", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "فایل مشکل داره", name="Reza", at=900, message_id=1)]
    out = awareness_context.blocks(_referent_ctx(anchor, window))
    assert "continues the thread" in out
    assert "«فایل»" in out


def test_the_thread_renders_nothing_when_it_cannot_be_judged():
    """An abstention is silent, not a line saying "unclear"."""
    anchor = _msg(ADMIN, "باشه", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "فایل مشکل داره", name="Reza", at=900, message_id=1)]
    assert awareness_context._render_thread(_referent_ctx(anchor, window)) == ""


def test_the_room_state_reads_the_context_not_the_database():
    """A hand-made window nothing captured: a query would find nothing."""
    anchor = _msg(ADMIN, "خب", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "فایل مشکل داره", name="Reza", at=900, message_id=1,
                   reply_user_id=OTHER)]
    ctx = _referent_ctx(anchor, window)
    assert f"{TARGET} → {OTHER}" in awareness_context._render_reply_graph(ctx)


# ── When the anchor's own words point, from the server's clock ────────────
def test_the_anchor_when_is_rendered_for_the_model():
    """«دیروز» is placed by the server's clock, not the model's sense of time."""
    anchor = _msg(MEMBER, "دیروز چرا اینکارو کردی", name="Someone", at=1000)
    out = awareness_context.blocks(_referent_ctx(anchor, []))
    assert "دیروز" in out
    assert "backwards, before now" in out
    assert "server's clock" in out


def test_the_anchor_when_renders_nothing_without_a_time_word():
    """A message that says nothing about time contributes nothing."""
    anchor = _msg(MEMBER, "اینو بن کن", name="Someone", at=1000)
    assert awareness_context._render_anchor_when(_referent_ctx(anchor, [])) == ""


def test_the_anchor_when_states_how_old_the_window_is():
    """«قبلاً» needs something to be earlier *than* — the window's own age."""
    window = [_msg(TARGET, "سلام", name="Reza", at=700)]
    anchor = _msg(MEMBER, "قبلاً گفتم اینکارو نکن", name="Someone", at=1000)
    out = awareness_context._render_anchor_when(_referent_ctx(anchor, window))
    assert "starts" in out


def test_the_anchor_when_reads_the_context_not_the_database():
    """A hand-made window nothing captured: a query would find nothing."""
    anchor = _msg(MEMBER, "همین الان بنش کن", name="Someone", at=1000)
    ctx = _referent_ctx(anchor, [])
    out = awareness_context._render_anchor_when(ctx)
    assert "همین الان" in out
    assert "at the present moment" in out


def test_a_time_word_is_not_rendered_as_a_person_reference():
    """The two readers share the fact: «همین الان» is a time, not somebody.

    The anchor is an administrator's, so the referent source *is* asked — and the
    assertion is that it has nothing to offer, while the when-block places the
    time. A message like «همین الان ساعت چنده» names no person, and the temporal
    noun is what keeps the near demonstrative from being read as one.
    """
    anchor = _msg(ADMIN, "همین الان ساعت چنده", role="admin", name="Admin", at=1000)
    window = [_msg(TARGET, "سلام", name="Reza", at=900)]
    ctx = _referent_ctx(anchor, window)
    assert awareness_context._wants_referents(ctx) is True
    assert awareness_context._render_referent_candidates(ctx) == ""
    assert "at the present moment" in awareness_context._render_anchor_when(ctx)
