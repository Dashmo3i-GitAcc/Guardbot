"""The observation core: schema, redaction, correlation, store and collector.

These are the pieces every other observation test stands on, so they are tested
for their *contracts* rather than for one happy path: a row always has the fixed
shape, a malformed value never raises, the redaction is the project's own single
list rather than a copy, a turn's id is inherited by everything inside it, and
the archive is a separate SQLite file with its own connection.
"""
import asyncio
import json
import os

from app import config, observe
from app.observe import redact, retention, schema, store


def run(coro):
    return asyncio.run(coro)


# ── The vocabulary is closed ───────────────────────────────────────────────
def test_the_event_vocabulary_is_closed_and_named():
    """Every kind is a stable string, and the set is what the queries expect."""
    for name in (
        "KIND_UPDATE",
        "KIND_DEDUP",
        "KIND_BOUNDARY",
        "KIND_ROUTING",
        "KIND_TURN",
        "KIND_TURN_END",
        "KIND_CONTEXT",
        "KIND_AI",
        "KIND_AI_DONE",
        "KIND_DELIVERY",
        "KIND_AWARENESS",
        "KIND_QUEUE",
        "KIND_VOICE",
        "KIND_POOL",
        "KIND_ADMIN",
        "KIND_FLAG",
        "KIND_ERROR",
        "KIND_DEPLOY",
        "KIND_REPORT",
        "KIND_CLEANUP",
        "KIND_MEMORY",
    ):
        kind = getattr(schema, name)
        assert kind in schema.KINDS
        assert schema.known(kind)


def test_an_unknown_kind_is_recorded_as_an_error_not_invented():
    """A typo must not create a new category no query looks at."""
    row = schema.coerce({"kind": "not.a.real.kind"})
    assert row["kind"] == schema.KIND_ERROR


# ── Coercion never raises ──────────────────────────────────────────────────
def test_coercion_normalises_rather_than_raising():
    row = schema.coerce(
        {
            "kind": schema.KIND_UPDATE,
            "chat_id": "not-a-number",
            "user_id": None,
            "message_id": "42",
            "at": "nonsense",
            "ok": 0,
            "data": {"a": 1},
        }
    )
    assert row["chat_id"] == 0
    assert row["user_id"] == 0
    assert row["message_id"] == 42
    assert row["at"] > 0
    assert row["ok"] == 0
    assert json.loads(row["data"]) == {"a": 1}
    assert set(row) == set(schema.COLUMNS)


def test_coercion_survives_a_non_serialisable_data_value():
    """A caller handing over an object must cost its own record, not the batch."""
    row = schema.coerce({"kind": schema.KIND_ERROR, "data": {"obj": object()}})
    assert isinstance(row["data"], str)


# ── Redaction delegates to the project's single list ───────────────────────
def test_redaction_delegates_to_the_bridge():
    """One pattern list, not two: the archive uses `agent_bridge.redact`."""
    from app import agent_bridge

    # Built from parts and obviously synthetic, so no scanner — and no reader —
    # can mistake this for a live credential.
    secret = "000000000:" + "FAKEFAKEFAKE" * 3
    assert agent_bridge.redact(secret) == redact.REDACTED
    assert redact.redact(secret) == agent_bridge.redact(secret)
    assert secret not in redact.redact(f"token is {secret} here")


def test_clip_reports_exactly_what_it_dropped():
    text, dropped = redact.clip("abcdef", 3)
    assert dropped == 3
    assert text.startswith("abc")
    assert redact.clip("abc", 10) == ("abc", 0)


# ── Correlation ────────────────────────────────────────────────────────────
def test_deployment_id_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("GUARDBOT_BUILD_SHA", "abc123")
    monkeypatch.setattr(observe.context, "_deployment", None)
    assert observe.deployment_id() == "abc123"
    monkeypatch.setattr(observe.context, "_deployment", None)


def test_deployment_id_falls_back_to_a_build_info_file(monkeypatch, tmp_path):
    marker = tmp_path / "BUILD_INFO"
    marker.write_text("deadbeef\n", encoding="utf-8")
    monkeypatch.delenv("GUARDBOT_BUILD_SHA", raising=False)
    monkeypatch.setenv("GUARDBOT_BUILD_INFO", str(marker))
    monkeypatch.setattr(observe.context, "_deployment", None)
    assert observe.deployment_id() == "deadbeef"
    monkeypatch.setattr(observe.context, "_deployment", None)


def test_deployment_id_is_unknown_when_nothing_says_otherwise(monkeypatch):
    monkeypatch.delenv("GUARDBOT_BUILD_SHA", raising=False)
    monkeypatch.setenv("GUARDBOT_BUILD_INFO", "/nonexistent/BUILD_INFO")
    monkeypatch.setattr(observe.context, "_deployment", None)
    assert observe.deployment_id() == "unknown"
    monkeypatch.setattr(observe.context, "_deployment", None)


def test_process_id_is_unique_per_process():
    assert observe.process_id() == observe.process_id()
    assert "-" in observe.process_id()


def test_conversation_id_is_the_person_in_the_room():
    assert observe.conversation_id(-100, 7) == "-100:7"
    assert observe.conversation_id(0, 0) == "0:0"


# ── The turn ───────────────────────────────────────────────────────────────
def test_a_turn_carries_one_id_into_everything_inside_it(archive):
    async def scenario():
        await observe.start(worker=False)
        with observe.turn("chat", chat_id=1, user_id=2, message_id=3, text="hi") as t:
            t.emit(schema.KIND_ROUTING, event="directed")
            t.emit(schema.KIND_AI, event="chat")
        await observe.flush()
        return t

    turn = run(scenario())
    rows = store.rows("SELECT kind, turn_id, trace_id, conversation_id FROM events")
    assert rows
    for row in rows:
        assert row["turn_id"] == turn.turn_id
        assert row["trace_id"] == turn.trace_id
        assert row["conversation_id"] == "1:2"


def test_a_turn_records_its_own_open_and_close(archive):
    async def scenario():
        await observe.start(worker=False)
        with observe.turn("chat", chat_id=5, user_id=6, message_id=7, text="in"):
            observe.emit(schema.KIND_DELIVERY, event="text", text="out")
        await observe.flush()

    run(scenario())
    row = store.one("SELECT * FROM turns WHERE chat_id = 5")
    assert row is not None
    assert row["inbound"] == "in"
    assert row["outbound"] == "out"
    assert row["ended_at"] >= row["started_at"]
    assert row["duration_ms"] >= 0


def test_finish_is_idempotent():
    turn = observe.turn("chat")
    turn.finish("sent")
    turn.finish("failed")
    assert turn.outcome == "sent"


def test_two_concurrent_turns_do_not_see_each_other(archive):
    """The current turn is context-local, so an interleave cannot cross them."""

    async def scenario():
        await observe.start(worker=False)
        seen = {}

        async def one(name, chat_id):
            with observe.turn("chat", chat_id=chat_id) as t:
                await asyncio.sleep(0)
                seen[name] = observe.current_turn()
                assert seen[name] is t

        await asyncio.gather(one("a", 1), one("b", 2))
        await observe.flush()
        return seen

    seen = run(scenario())
    assert seen["a"] is not seen["b"]


def test_the_turn_is_cleared_when_the_block_exits(archive):
    async def scenario():
        await observe.start(worker=False)
        with observe.turn("chat", chat_id=1):
            assert observe.current_turn() is not None
        assert observe.current_turn() is None

    run(scenario())


# ── The store is its own file ──────────────────────────────────────────────
def test_the_archive_is_a_separate_sqlite_file(archive):
    async def scenario():
        await observe.start(worker=False)
        observe.emit(schema.KIND_FLAG, event="x", data={"v": 1})
        await observe.flush()

    run(scenario())
    assert archive.exists()
    assert os.path.basename(str(archive)) == "observability.db"
    assert str(archive) == store.path()


def test_retention_prunes_events_and_turns_but_keeps_deployments(archive):
    async def scenario():
        await observe.start(worker=False)
        store.record_deployment("sha1", sha="sha1", image="img", note="boot", pid="p")
        with observe.turn("chat", chat_id=1, user_id=1) as t:
            t.emit(schema.KIND_AI, event="chat")
        await observe.flush()

    run(scenario())
    assert store.counts()["turns"] == 1
    removed = store.prune(cutoff=9_999_999_999)  # everything is older than this
    assert removed["events"] >= 1
    assert removed["turns"] == 1
    assert store.counts()["deployments"] == 1


def test_capacity_reports_size_and_counts(archive):
    async def scenario():
        await observe.start(worker=False)
        observe.emit(schema.KIND_FLAG, event="x")
        await observe.flush()

    run(scenario())
    cap = retention.capacity()
    assert cap["size_bytes"] > 0
    assert cap["counts"]["events"] >= 1
    assert cap["max_bytes"] > 0


# ── The collector ──────────────────────────────────────────────────────────
def test_a_full_queue_drops_and_counts_rather_than_waiting(archive, monkeypatch):
    async def scenario():
        monkeypatch.setattr(config, "OBSERVE_QUEUE_MAX", 64)  # the collector floor
        await observe.start(worker=False)
        accepted = 0
        for _ in range(200):
            if observe.emit(schema.KIND_FLAG, event="x"):
                accepted += 1
        counters = observe.counters()
        return accepted, counters

    accepted, counters = run(scenario())
    assert accepted < 200
    assert counters["dropped"] >= 1


def test_the_worker_writes_in_batches(archive):
    async def scenario():
        await observe.start()  # worker on
        for i in range(25):
            observe.emit(schema.KIND_FLAG, event="x", data={"i": i})
        await observe.flush()
        counters = observe.counters()
        await observe.stop()
        return counters

    counters = run(scenario())
    assert store.counts()["events"] >= 25
    assert counters["written"] >= 25
    assert counters["failed"] == 0
    assert counters["batches"] >= 1


def test_observation_off_is_a_noop(archive, monkeypatch):
    monkeypatch.setattr(config, "OBSERVE_ENABLED", False)
    assert observe.enabled() is False

    async def scenario():
        return await observe.start()

    assert run(scenario()) is False
    assert observe.emit(schema.KIND_FLAG, event="x") is False
    assert observe.started() is False
