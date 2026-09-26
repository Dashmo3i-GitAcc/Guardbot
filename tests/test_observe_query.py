"""The investigation interface: queries, retention, reports and the CLI.

These tests populate an isolated archive with a small, realistic story — two
people in one room, one answered turn, one failed turn, a voice note, a pool
retry, a deployment — and then assert that each investigation question returns
the evidence an agent needs and nothing it should not.
"""
import asyncio
import json
import os

from app import observe
from app.observe import cli, query, report, retention, schema, store


def run(coro):
    return asyncio.run(coro)


def _flush():
    run(observe.flush())


async def _populate():
    """One deployment, two conversations, one failure, one voice turn."""
    await observe.start(worker=False)
    observe.mark_deployment(note="boot", image="guardbot:test")
    observe.flag("chat", True)

    first_id = ""
    with observe.turn(
        "chat", chat_id=-100, user_id=7, message_id=10, text="سلام نکسوس"
    ) as t:
        first_id = t.turn_id
        t.emit(schema.KIND_ROUTING, event="directed")
        t.emit(schema.KIND_CONTEXT, event="composed", text="[context] date=...")
        t.emit(schema.KIND_AI, event="chat", data={"message": "سلام نکسوس"})
        t.emit(schema.KIND_AI_DONE, event="chat", text="سلام! در خدمتم")
        t.emit(schema.KIND_DELIVERY, event="text", text="سلام! در خدمتم")
        t.finish("sent", text="سلام! در خدمتم")

    with observe.turn(
        "chat", chat_id=-100, user_id=8, message_id=11, text="این رو ببین"
    ) as t:
        t.emit(schema.KIND_AI, event="chat")
        t.emit(
            schema.KIND_ERROR,
            ok=False,
            event="chat.declined",
            error="daily_cap",
        )
        t.finish("withheld", reason="daily_cap")

    with observe.turn(
        "voice", chat_id=-100, user_id=7, message_id=12, text=""
    ) as t:
        t.emit(schema.KIND_VOICE, event="transcribe", ok=True, text="ویس گفتم")
        t.emit(schema.KIND_VOICE, event="tts", ok=False, error="package_failed")
        t.finish("failed", reason="tts")

    observe.emit(
        schema.KIND_POOL,
        ok=False,
        event="models_cooling",
        error="models_cooling",
        data={"workload": "chat", "attempts": 3, "failures": 2},
    )
    await observe.flush()
    return first_id


# ── Trace: one turn, completely ────────────────────────────────────────────
def test_trace_returns_the_turn_and_every_event_in_order(archive):
    turn_id = run(_populate())
    found = query.trace(turn_id)
    assert found["found"] is True
    assert found["turn"]["conversation_id"] == "-100:7"
    kinds = [event["kind"] for event in found["events"]]
    assert schema.KIND_ROUTING in kinds
    assert schema.KIND_CONTEXT in kinds
    assert schema.KIND_AI in kinds
    assert schema.KIND_AI_DONE in kinds
    assert schema.KIND_DELIVERY in kinds
    # Ordered oldest first.
    ats = [event["at"] for event in found["events"]]
    assert ats == sorted(ats)


def test_trace_by_trace_id_matches_trace(archive):
    turn_id = run(_populate())
    found = query.trace(turn_id)
    by_trace = query.trace_by_trace_id(found["turn"]["trace_id"])
    assert by_trace["found"] is True
    assert len(by_trace["events"]) == len(found["events"])


def test_a_missing_turn_is_reported_as_not_found(archive):
    run(_populate())
    assert query.trace("nope")["found"] is False


# ── Conversation: a person's thread, in order ──────────────────────────────
def test_conversation_returns_the_real_messages_in_order(archive):
    run(_populate())
    thread = query.conversation("-100:7")
    assert thread["conversation_id"] == "-100:7"
    assert thread["turns"]
    for turn in thread["turns"]:
        assert turn["user_id"] == 7
    # The real words, not metrics.
    assert any("سلام نکسوس" in (turn["inbound"] or "") for turn in thread["turns"])
    assert any("در خدمتم" in (turn["outbound"] or "") for turn in thread["turns"])


def test_by_message_finds_the_whole_edge(archive):
    run(_populate())
    found = query.by_message(10)
    assert found["count"] >= 1
    assert any(event["text"] == "سلام نکسوس" for event in found["events"])


# ── Failures and incidents ─────────────────────────────────────────────────
def test_failures_lists_failed_events_and_turns(archive):
    run(_populate())
    found = query.failures()
    assert found["failed_events"] >= 1
    assert found["failed_turns"] >= 1
    reasons = {turn["reason"] for turn in found["turns"]}
    assert "daily_cap" in reasons or "tts" in reasons


def test_incidents_finds_a_described_bug(archive):
    run(_populate())
    found = query.incidents("daily_cap")
    assert len(found["turns"]) >= 1
    assert any(turn["reason"] == "daily_cap" for turn in found["turns"])
    assert "trace" in found["hint"]


def test_the_named_failure_queries_are_wired(archive):
    run(_populate())
    assert query.tts_failures()["count"] >= 1
    assert query.transcription_failures()["count"] == 0
    assert query.provider_failures()["count"] >= 1


def test_the_routing_failure_queries_match_their_labels(archive):
    """The label each finder searches for is the label the runtime emits."""

    async def scenario():
        await observe.start(worker=False)
        observe.emit(schema.KIND_ROUTING, event="missed", chat_id=-1, data={})
        observe.emit(
            schema.KIND_ROUTING, event="target_unresolved", chat_id=-1, data={}
        )
        observe.emit(schema.KIND_ROUTING, event="directed", chat_id=-1, data={})
        observe.emit(schema.KIND_POOL, event="retry", data={})
        observe.emit(schema.KIND_POOL, ok=False, event="time_budget", data={})
        await observe.flush()

    run(scenario())
    assert query.reply_target_failures()["count"] == 1
    assert query.named_target_failures()["count"] == 1
    assert query.retries()["count"] == 1
    assert query.timeouts()["count"] == 1


def test_the_query_labels_match_the_instrumentation_source():
    """A rename in the runtime without a rename here would silently find nothing.

    The finders above search for literal event labels; this pins that each label
    actually appears in the module that is supposed to emit it.
    """
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sources = {
        "main.py": open(os.path.join(root, "app", "main.py"), encoding="utf-8").read(),
        "transcribe.py": open(
            os.path.join(root, "app", "transcribe.py"), encoding="utf-8"
        ).read(),
        "chat.py": open(os.path.join(root, "app", "chat.py"), encoding="utf-8").read(),
        "gemini_pool.py": open(
            os.path.join(root, "app", "gemini_pool.py"), encoding="utf-8"
        ).read(),
    }
    assert '"missed"' in sources["main.py"]
    assert '"target_unresolved"' in sources["main.py"]
    assert 'event="transcribe"' in sources["transcribe.py"]
    assert 'event="tts"' in sources["chat.py"]
    assert '"retry"' in sources["gemini_pool.py"]


# ── Search ─────────────────────────────────────────────────────────────────
def test_search_events_finds_recorded_text(archive):
    run(_populate())
    found = query.search_events("در خدمتم")
    assert found["count"] >= 1


def test_search_conversations_finds_a_persons_words(archive):
    run(_populate())
    found = query.search_conversations("این رو ببین")
    assert found["count"] >= 1


# ── Summaries and deployments ──────────────────────────────────────────────
def test_summarize_counts_the_window(archive):
    run(_populate())
    summary = query.summarize(86400)
    assert summary["total_turns"] == 3
    assert summary["successful_turns"] == 1
    assert summary["failed_turns"] >= 1
    assert summary["voice_turns"] >= 1
    assert "latency_ms" in summary
    assert summary["newest_deployment"]


def test_deployments_are_recorded_with_their_version(archive):
    run(_populate())
    found = query.deployments()
    assert found
    assert any(row["note"] == "boot" for row in found)


def test_status_reports_the_archive(archive):
    run(_populate())
    state = query.status()
    assert state["path"] == store.path()
    assert state["retention_seconds"] > 0
    assert state["counts"]["events"] >= 1


def test_health_reports_the_writer_and_the_archive(archive):
    run(_populate())
    state = query.health()
    assert state["ok"] is True
    assert state["checks"]
    assert state["counters"]["running"] is True
    assert state["status"]["counts"]["events"] >= 1


# ── Retention ──────────────────────────────────────────────────────────────
def test_retention_keeps_recent_and_removes_old(archive):
    async def scenario():
        await observe.start(worker=False)
        store.insert_events(
            [
                schema.coerce(
                    {
                        "kind": schema.KIND_FLAG,
                        "at": 1_000.0,
                        "event": "old",
                        "data": {},
                    }
                ),
                schema.coerce(
                    {
                        "kind": schema.KIND_FLAG,
                        "at": 10_000_000_000.0,
                        "event": "new",
                        "data": {},
                    }
                ),
            ]
        )
        result = retention.sweep(now=10_000_000_000.0)
        remaining = store.rows("SELECT event FROM events")
        return result, [row["event"] for row in remaining]

    result, remaining = run(scenario())
    assert result["removed"]["events"] == 1
    assert remaining == ["new"]


def test_a_sweep_is_itself_recorded(archive):
    run(_populate())
    retention.sweep()
    _flush()
    found = query.find(kind=schema.KIND_CLEANUP)
    assert found
    assert found[0]["event"] == "sweep"


# ── Reports ────────────────────────────────────────────────────────────────
def test_a_report_is_written_as_json_and_markdown(archive):
    run(_populate())
    result = report.write(86400)
    assert result["ok"] is True
    assert os.path.exists(result["json"])
    assert os.path.exists(result["markdown"])
    with open(result["json"], encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["summary"]["total_turns"] == 3
    with open(result["markdown"], encoding="utf-8") as handle:
        text = handle.read()
    assert "# Nexus observation report" in text
    assert "## Failures and anomalies" in text


def test_the_report_is_recorded_in_the_archive(archive):
    run(_populate())
    report.write(86400)
    _flush()
    assert report.latest()
    assert query.find(kind=schema.KIND_REPORT)


# ── The CLI ────────────────────────────────────────────────────────────────
def _cli(*argv):
    return cli.main(list(argv))


def test_the_cli_status_runs(archive, capsys):
    run(_populate())
    assert _cli("status") == 0
    out = capsys.readouterr().out
    assert "observability" in out or "events" in out


def test_the_cli_trace_runs(archive, capsys):
    turn_id = run(_populate())
    assert _cli("trace", turn_id) == 0
    out = capsys.readouterr().out
    assert turn_id[:8] in out


def test_the_cli_conversation_runs(archive, capsys):
    run(_populate())
    # `--` so argparse does not read the negative chat id as an option.
    assert _cli("conversation", "--", "-100:7") == 0
    assert "در خدمتم" in capsys.readouterr().out


def test_the_cli_search_runs(archive, capsys):
    run(_populate())
    assert _cli("search", "این رو ببین") == 0
    assert capsys.readouterr().out


def test_the_cli_failures_runs(archive, capsys):
    run(_populate())
    assert _cli("failures") == 0
    assert capsys.readouterr().out


def test_the_cli_report_write_runs(archive, capsys):
    run(_populate())
    assert _cli("report", "--write") == 0
    out = capsys.readouterr().out
    assert "report" in out.lower()


def test_the_cli_cleanup_dry_run_changes_nothing(archive, capsys):
    run(_populate())
    before = store.counts()["events"]
    assert _cli("cleanup", "--dry-run") == 0
    assert store.counts()["events"] == before


def test_the_cli_rejects_an_unknown_command(archive):
    # argparse exits non-zero on an unknown subcommand.
    try:
        _cli("not-a-command")
    except SystemExit as exc:
        assert exc.code != 0
    else:  # pragma: no cover - argparse always exits here
        raise AssertionError("the CLI accepted an unknown command")
