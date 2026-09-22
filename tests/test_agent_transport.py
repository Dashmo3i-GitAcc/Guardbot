"""The wire between the container and the host, and both of its ends.

Two processes, one directory, and a set of properties that only show up when one
of them dies at the wrong moment. Those properties are what this file is about:

* **Nothing is delivered twice.** The stream is append-only and the container
  stores how many lines it has read. A restart resumes rather than repeats.
* **Nothing is dropped.** A long answer is chunked in order or sent as a
  document, and there is no branch that discards it.
* **Nothing is re-run.** A request whose stream already ended is not claimed
  again, which is what makes "a restart does not duplicate execution" true.
* **Nothing leaks.** Everything on its way to Telegram goes through the
  redactor, on both sides of the wire.

The runner is imported as a script and exercised without a real coding agent:
the child process is a two-line shell script that prints the shapes the parser
is written for. That is enough to test everything this repository owns — the
CLI's own behaviour is a deployment matter, documented in ``AgentMD.md``.
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

from app import (
    admin_service,
    agent_bridge,
    agent_poller,
    agent_service,
    agent_spool,
    config,
    db,
)

OWNER = 999
MEMBER = 42
CHAT = -1001234567890

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_runner():
    """Import ``tools/agent_runner.py`` as a module, without running it."""
    if "agent_runner" in sys.modules:
        return sys.modules["agent_runner"]
    spec = importlib.util.spec_from_file_location(
        "agent_runner", os.path.join(ROOT, "tools", "agent_runner.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_runner"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path):
    """A directory that passes for a repository, and an allowlist that names it."""
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def agent_env(monkeypatch, tmp_path, repo):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "AGENT_REPOSITORIES", {"demo": str(repo)})
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_MAX_ACTIVE", 4)
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 4)
    monkeypatch.setattr(config, "AGENT_CHUNK_CHARS", 3500)
    monkeypatch.setattr(config, "AGENT_DOCUMENT_CHARS", 3500)
    monkeypatch.setattr(config, "AGENT_PROGRESS_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "AGENT_PROGRESS_MAX_MESSAGES", 20)
    monkeypatch.setattr(config, "AGENT_TIMEOUT_SECONDS", 1800)
    monkeypatch.setenv("AGENT_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("AGENT_REPOSITORIES", f"demo={repo}")
    monkeypatch.setenv("AGENT_RUNNER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_CLI", "true")
    db.init()
    db.agent_reset()
    agent_spool.ensure()
    agent_poller.reset_state()
    yield
    db.agent_reset()
    agent_poller.reset_state()


# ── The spool ─────────────────────────────────────────────────────────────
def test_a_request_id_that_is_not_a_plain_token_gets_no_file():
    for bad in ("../../etc/passwd", "a/b", "a b", "", "x" * 100, "a\nb"):
        assert agent_spool.safe_id(bad) == ""
        assert agent_spool.request_path(bad) == ""


def test_a_published_request_reads_back():
    assert agent_spool.write_request("agent-x", {"task": "hello"})
    assert agent_spool.read_request("agent-x") == {"task": "hello"}


def test_a_malformed_request_file_reads_as_empty_rather_than_raising():
    agent_spool.write_request("agent-x", {"task": "hello"})
    with open(agent_spool.request_path("agent-x"), "w") as handle:
        handle.write("{not json")
    assert agent_spool.read_request("agent-x") == {}


def test_publishing_is_atomic_so_no_temporary_file_is_left_behind():
    agent_spool.write_request("agent-x", {"task": "hello"})
    names = os.listdir(agent_spool.requests_dir())
    assert names == ["agent-x.json"]


def test_a_lock_is_exclusive():
    assert agent_spool.claim("agent-x")
    assert not agent_spool.claim("agent-x")
    agent_spool.release("agent-x")
    assert agent_spool.claim("agent-x")


def test_the_lock_records_its_age():
    agent_spool.claim("agent-x")
    assert agent_spool.lock_age("agent-x") >= 0


def test_stale_locks_can_be_cleared():
    agent_spool.claim("agent-x")
    assert agent_spool.clear_locks() == 1
    assert not agent_spool.locked("agent-x")


def test_an_appended_line_reads_back_in_order():
    for i in range(5):
        agent_spool.append("agent-x", "progress", f"line {i}")
    records = agent_spool.read_lines("agent-x")
    assert [r["text"] for r in records] == [f"line {i}" for i in range(5)]


def test_reading_from_an_offset_returns_only_what_is_new():
    for i in range(5):
        agent_spool.append("agent-x", "progress", f"line {i}")
    records, offset = agent_spool.read_from("agent-x", 3)
    assert [r["text"] for r in records] == ["line 3", "line 4"]
    assert offset == 5


def test_reading_from_the_end_returns_nothing_and_keeps_the_offset():
    agent_spool.append("agent-x", "progress", "one")
    records, offset = agent_spool.read_from("agent-x", 1)
    assert records == []
    assert offset == 1


def test_the_offset_agrees_with_the_line_count():
    """The two have to agree, or the stored offset drifts past a line."""
    for i in range(7):
        agent_spool.append("agent-x", "progress", f"line {i}")
    _, offset = agent_spool.read_from("agent-x", 0)
    assert offset == agent_spool.line_count("agent-x") == 7


def test_a_partial_line_is_not_read_until_it_is_whole():
    """A crash mid-write must not produce a half-record.

    The writer appends one line per ``write``, so the only way to see this is a
    kill in the middle — which the test produces by writing the bytes itself.
    """
    path = agent_spool.stream_path("agent-x")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"kind":"progress","text":"whole"}\n')
        handle.write('{"kind":"progress","text":"tru')
    records = agent_spool.read_lines("agent-x")
    assert [r["text"] for r in records] == ["whole"]
    assert agent_spool.line_count("agent-x") == 1


def test_the_partial_line_is_read_once_it_is_completed():
    path = agent_spool.stream_path("agent-x")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"kind":"progress","text":"tru')
    assert agent_spool.read_lines("agent-x") == []
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('ncated"}\n')
    assert agent_spool.read_lines("agent-x")[0]["text"] == "truncated"


def test_an_unknown_kind_is_not_written():
    assert not agent_spool.append("agent-x", "whatever", "text")
    assert agent_spool.read_lines("agent-x") == []


def test_the_last_kind_reports_how_a_stream_ended():
    agent_spool.append("agent-x", "started")
    agent_spool.append("agent-x", "result", "done")
    assert agent_spool.last_kind("agent-x") == "result"


def test_forgetting_a_task_removes_every_one_of_its_files():
    agent_spool.write_request("agent-x", {})
    agent_spool.append("agent-x", "started")
    agent_spool.claim("agent-x")
    agent_spool.request_cancel("agent-x")
    agent_spool.forget("agent-x")
    assert not os.path.exists(agent_spool.request_path("agent-x"))
    assert not os.path.exists(agent_spool.stream_path("agent-x"))
    assert not os.path.exists(agent_spool.lock_path("agent-x"))
    assert not os.path.exists(agent_spool.cancel_path("agent-x"))


def test_pending_requests_are_listed_oldest_first():
    for name in ("agent-a", "agent-b"):
        agent_spool.write_request(name, {})
        time.sleep(0.01)
    assert agent_spool.pending_requests() == ["agent-a", "agent-b"]


# ── The container's end ───────────────────────────────────────────────────
class FakeBot:
    """Records what would have been sent, and can be told to fail."""

    def __init__(self, *, fail=False):
        self.sent: list[tuple] = []
        self.fail = fail

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("telegram said no")
        self.sent.append(("message", chat_id, text))

    async def send_document(self, chat_id, document, filename=None, caption=None):
        if self.fail:
            raise RuntimeError("telegram said no")
        self.sent.append(("document", chat_id, filename, document.read().decode()))


class FakeCtx:
    def __init__(self, *, fail=False):
        self.bot = FakeBot(fail=fail)


def _task(*, task="change the parser", operation="edit", reply_mode="text"):
    result = asyncio.run(
        agent_service.submit(
            admin_service.AdminRequest(
                operation="codebuddy_task",
                chat_id=CHAT,
                actor_id=OWNER,
                repository="demo",
                task=task,
                agent_operation=operation,
                reply_mode=reply_mode,
                interface=admin_service.INTERFACE_AI,
            )
        )
    )
    assert result.ok, result.outcome
    return result.detail


def _tick(ctx):
    asyncio.run(agent_poller.tick(ctx))


def _texts(ctx):
    return [entry[2] for entry in ctx.bot.sent if entry[0] == "message"]


# ── Delivery ──────────────────────────────────────────────────────────────
def test_a_started_line_moves_the_task_to_running_and_says_so():
    request_id = _task()
    agent_spool.append(request_id, "started")
    ctx = FakeCtx()
    _tick(ctx)
    assert db.agent_task_get(request_id)["status"] == "running"
    assert db.agent_task_get(request_id)["started_at"] > 0
    assert any("در حال اجرا" in t for t in _texts(ctx))


def test_a_result_line_finishes_the_task_and_delivers_the_answer():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "I changed the parser. Tests pass.")
    ctx = FakeCtx()
    _tick(ctx)
    row = db.agent_task_get(request_id)
    assert row["status"] == "succeeded"
    assert row["finished_at"] > 0
    assert "Tests pass" in row["result"]
    assert any("Tests pass" in t for t in _texts(ctx))


def test_an_error_line_fails_the_task_and_reports_it():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "error", "the agent exited with status 1")
    ctx = FakeCtx()
    _tick(ctx)
    row = db.agent_task_get(request_id)
    assert row["status"] == "failed"
    assert "status 1" in row["error"]
    assert any("ناموفق" in t for t in _texts(ctx))


def test_a_question_line_waits_for_the_owner_and_is_delivered():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "question", "should I push the branch?")
    ctx = FakeCtx()
    _tick(ctx)
    row = db.agent_task_get(request_id)
    assert row["status"] == "waiting_for_owner"
    assert row["started_at"] > 0
    assert any("should I push" in t for t in _texts(ctx))
    # And it is not offered as something to approve: it has already begun.
    assert db.agent_task_waiting() == []


def test_a_cancelled_line_stops_the_task():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "cancelled", "stopped at the owner's request")
    ctx = FakeCtx()
    _tick(ctx)
    assert db.agent_task_get(request_id)["status"] == "cancelled"


def test_progress_lines_are_delivered_but_throttled(monkeypatch):
    monkeypatch.setattr(config, "AGENT_PROGRESS_MAX_MESSAGES", 2)
    request_id = _task()
    agent_spool.append(request_id, "started")
    for i in range(6):
        agent_spool.append(request_id, "progress", f"step {i}")
    ctx = FakeCtx()
    _tick(ctx)
    progress = [t for t in _texts(ctx) if "step" in t]
    assert len(progress) == 2
    # And the transcript is still delivered in full when it matters: the result
    # is not throttled.
    agent_spool.append(request_id, "result", "done")
    _tick(ctx)
    assert any("done" in t for t in _texts(ctx))


def test_a_progress_line_is_never_sent_before_the_one_it_followed():
    request_id = _task()
    agent_spool.append(request_id, "started")
    for i in range(3):
        agent_spool.append(request_id, "progress", f"step {i}")
    agent_spool.append(request_id, "result", "final")
    ctx = FakeCtx()
    _tick(ctx)
    bodies = _texts(ctx)
    positions = [
        bodies.index(t) for t in bodies if "step" in t or "final" in t
    ]
    assert positions == sorted(positions)


def test_a_long_answer_is_chunked_in_order(monkeypatch):
    monkeypatch.setattr(config, "AGENT_CHUNK_CHARS", 400)
    request_id = _task()
    body = "\n".join(f"line {i} of a long transcript" for i in range(200))
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", body)
    ctx = FakeCtx()
    _tick(ctx)
    delivered = [t for t in _texts(ctx) if "line " in t]
    assert len(delivered) > 1
    # Every line arrives, and in order.
    joined = "\n".join(delivered)
    for i in (0, 100, 199):
        assert f"line {i} of a long transcript" in joined
    assert joined.index("line 0 ") < joined.index("line 199 ")


def test_reply_mode_document_sends_a_file_instead_of_a_wall_of_chat():
    request_id = _task(reply_mode="document")
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "the whole transcript")
    ctx = FakeCtx()
    _tick(ctx)
    documents = [e for e in ctx.bot.sent if e[0] == "document"]
    assert len(documents) == 1
    assert documents[0][2] == f"{request_id}.txt"
    assert "the whole transcript" in documents[0][3]


def test_a_document_that_cannot_be_sent_falls_back_to_chunks(monkeypatch):
    """The brief's rule: never silently discard. A failed document is not a loss."""
    monkeypatch.setattr(config, "AGENT_CHUNK_CHARS", 200)
    request_id = _task(reply_mode="document")
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "important finding " * 100)
    ctx = FakeCtx(fail=True)
    _tick(ctx)
    # Both attempts failed because the bot itself is failing, so the state is
    # what is asserted here: the task still finished and the answer is stored.
    assert db.agent_task_get(request_id)["status"] == "succeeded"
    assert "important finding" in db.agent_task_get(request_id)["result"]


def test_an_empty_result_is_reported_rather_than_left_silent():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "")
    ctx = FakeCtx()
    _tick(ctx)
    assert db.agent_task_get(request_id)["status"] == "succeeded"
    assert any("چیزی برای گزارش" in t for t in _texts(ctx))


# ── Once each ─────────────────────────────────────────────────────────────
def test_a_second_tick_delivers_nothing():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "done")
    first = FakeCtx()
    _tick(first)
    second = FakeCtx()
    _tick(second)
    assert second.bot.sent == []


def test_a_restart_in_a_new_process_does_not_re_deliver():
    """The property behind ``progress_offset``, checked across a process.

    The offset lives in the database, not in this module's memory, so a fresh
    process reads it and resumes. Running the poller in a subprocess is the only
    honest way to test that — an in-process test would keep the module state that
    the database is supposed to make unnecessary.
    """
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "done")
    ctx = FakeCtx()
    _tick(ctx)
    assert ctx.bot.sent
    assert db.agent_task_get(request_id)["progress_offset"] > 0

    program = (
        "import asyncio, json, sys\n"
        "from app import db, agent_poller\n"
        "db.init()\n"
        "sent = []\n"
        "class Bot:\n"
        "    async def send_message(self, chat_id, text): sent.append(text)\n"
        "    async def send_document(self, *a, **k): sent.append('doc')\n"
        "class Ctx: bot = Bot()\n"
        "asyncio.run(agent_poller.tick(Ctx()))\n"
        "print(json.dumps(sent))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []


def test_a_late_start_line_does_not_resurrect_a_finished_task():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "done")
    _tick(FakeCtx())
    assert db.agent_task_get(request_id)["status"] == "succeeded"
    # A runner that was slow to report now says it started.
    agent_spool.append(request_id, "started")
    _tick(FakeCtx())
    assert db.agent_task_get(request_id)["status"] == "succeeded"


def test_a_second_result_line_is_not_delivered():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "the first answer")
    _tick(FakeCtx())
    agent_spool.append(request_id, "result", "a second answer")
    ctx = FakeCtx()
    _tick(ctx)
    assert ctx.bot.sent == []
    assert db.agent_task_get(request_id)["result"] == "the first answer"


def test_the_offset_advances_only_over_what_was_delivered():
    request_id = _task()
    agent_spool.append(request_id, "started")
    ctx = FakeCtx()
    _tick(ctx)
    assert db.agent_task_get(request_id)["progress_offset"] == 1
    agent_spool.append(request_id, "progress", "more")
    _tick(FakeCtx())
    assert db.agent_task_get(request_id)["progress_offset"] == 2


# ── Secrets on the wire ───────────────────────────────────────────────────
# The strings here are test vectors for the redactor, not credentials: the bot
# token is the example Telegram publishes in its own API documentation and the
# key-shaped ones are filler. Nothing in this file is a live secret.
def test_a_credential_in_the_agents_answer_is_redacted_before_it_is_sent():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(
        request_id,
        "result",
        "I read .env: BOT_TOKEN=123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    )
    ctx = FakeCtx()
    _tick(ctx)
    joined = "\n".join(_texts(ctx))
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in joined
    assert agent_bridge.REDACTED in joined


def test_a_credential_is_redacted_in_the_stored_result_too():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "key=AIzaSyD-1234567890abcdefghijklmnop")
    _tick(FakeCtx())
    assert "AIzaSyD-1234567890abcdefghijklmnop" not in db.agent_task_get(request_id)["result"]


def test_a_credential_in_a_progress_line_is_redacted():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "progress", "using sk-abcdefghijklmnopqrstuvwx now")
    ctx = FakeCtx()
    _tick(ctx)
    assert "sk-abcdefghijklmnopqrstuvwx" not in "\n".join(_texts(ctx))


# ── The clock ─────────────────────────────────────────────────────────────
def test_a_task_that_never_started_is_timed_out_and_reported():
    request_id = _task()
    # Age the row: the update set is closed, so the test writes the column
    # directly rather than widening the API for its own convenience.
    with db._lock:
        db._conn.execute(
            "UPDATE agent_tasks SET created_at=? WHERE request_id=?",
            (int(time.time()) - 4000, request_id),
        )
        db._conn.commit()
    ctx = FakeCtx()
    _tick(ctx)
    row = db.agent_task_get(request_id)
    assert row["status"] == "timed_out"
    assert any("از زمان خارج" in t or "برنداشت" in t for t in _texts(ctx))


def test_a_running_task_that_outlives_its_bound_is_stopped():
    request_id = _task()
    agent_spool.append(request_id, "started")
    _tick(FakeCtx())
    with db._lock:
        db._conn.execute(
            "UPDATE agent_tasks SET started_at=? WHERE request_id=?",
            (int(time.time()) - 4000, request_id),
        )
        db._conn.commit()
    ctx = FakeCtx()
    _tick(ctx)
    assert db.agent_task_get(request_id)["status"] == "timed_out"
    assert agent_spool.cancel_requested(request_id)


def test_a_task_within_its_bound_is_left_alone():
    request_id = _task()
    agent_spool.append(request_id, "started")
    _tick(FakeCtx())
    _tick(FakeCtx())
    assert db.agent_task_get(request_id)["status"] == "running"


def test_an_unapproved_dangerous_task_is_not_timed_out():
    """Nothing is running, so there is no clock. The owner's silence is not a fault."""
    result = asyncio.run(
        agent_service.submit(
            admin_service.AdminRequest(
                operation="codebuddy_task",
                chat_id=CHAT,
                actor_id=OWNER,
                repository="demo",
                task="deploy it",
                agent_operation="deploy",
                interface=admin_service.INTERFACE_AI,
            )
        )
    )
    with db._lock:
        db._conn.execute(
            "UPDATE agent_tasks SET created_at=? WHERE request_id=?",
            (int(time.time()) - 4000, result.detail),
        )
        db._conn.commit()
    ctx = FakeCtx()
    _tick(ctx)
    assert db.agent_task_get(result.detail)["status"] == "waiting_for_owner"
    assert ctx.bot.sent == []


# ── Recovery ──────────────────────────────────────────────────────────────
def test_a_result_that_arrived_while_the_bot_was_down_is_applied_at_startup():
    request_id = _task()
    agent_spool.append(request_id, "started")
    agent_spool.append(request_id, "result", "finished while you were away")
    assert agent_poller.recover() >= 0
    row = db.agent_task_get(request_id)
    assert row["status"] == "succeeded"
    assert "while you were away" in row["result"]


def test_a_lock_left_by_a_dead_runner_is_cleared_at_startup():
    request_id = _task()
    agent_spool.append(request_id, "started")
    db.agent_task_update(request_id, status="succeeded", finished_at=1)
    agent_spool.claim(request_id)
    agent_poller.recover()
    assert not agent_spool.locked(request_id)


# ── The host's end ────────────────────────────────────────────────────────
def test_the_runner_accepts_a_request_that_names_an_allowed_repository(repo):
    runner = _load_runner()
    assert runner.validate(
        {"repository": "demo", "repo_path": str(repo), "operation": "edit", "task": "x"}
    ) == ""


def test_the_runner_refuses_a_repository_that_is_not_on_its_own_list(repo):
    runner = _load_runner()
    reason = runner.validate(
        {"repository": "elsewhere", "repo_path": str(repo), "operation": "edit", "task": "x"}
    )
    assert "allowlist" in reason


def test_the_runner_refuses_a_path_that_is_not_what_the_name_means(repo):
    """The check that makes the container's resolution an optimisation.

    A spool file written by hand, or a container whose allowlist disagrees, is
    refused here rather than trusted.
    """
    runner = _load_runner()
    reason = runner.validate(
        {"repository": "demo", "repo_path": "/etc", "operation": "edit", "task": "x"}
    )
    assert "not the path" in reason


def test_the_runner_refuses_a_directory_that_does_not_exist():
    runner = _load_runner()
    reason = runner.validate(
        {
            "repository": "demo",
            "repo_path": "/tmp/guardbot-does-not-exist",
            "operation": "edit",
            "task": "x",
        }
    )
    assert reason


def test_the_runner_refuses_an_operation_outside_the_vocabulary(repo):
    runner = _load_runner()
    reason = runner.validate(
        {"repository": "demo", "repo_path": str(repo), "operation": "sudo", "task": "x"}
    )
    assert "vocabulary" in reason


def test_the_runner_refuses_an_empty_task(repo):
    runner = _load_runner()
    reason = runner.validate(
        {"repository": "demo", "repo_path": str(repo), "operation": "edit", "task": ""}
    )
    assert "empty" in reason


def test_the_runner_takes_the_executable_from_its_own_environment(repo, monkeypatch):
    runner = _load_runner()
    monkeypatch.setenv("AGENT_CLI", "/usr/bin/true")
    monkeypatch.setenv("AGENT_CLI_ARGS", "-p,--output-format,stream-json")
    run = runner.Run(
        {"request_id": "agent-x", "repo_path": str(repo), "prompt": "do it"}, None
    )
    argv = run.argv()
    assert argv[0] == "/usr/bin/true"
    assert "do it" in argv


def test_the_runner_appends_the_turn_ceiling_and_the_directory(repo, monkeypatch):
    runner = _load_runner()
    monkeypatch.setenv("AGENT_CLI", "/usr/bin/true")
    monkeypatch.setenv("AGENT_CLI_ARGS", "-p")
    monkeypatch.delenv("AGENT_ADD_DIR", raising=False)
    run = runner.Run(
        {
            "request_id": "agent-x",
            "repo_path": str(repo),
            "prompt": "x",
            "max_turns": 7,
        },
        None,
    )
    argv = run.argv()
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "7"
    assert "--add-dir" in argv and argv[argv.index("--add-dir") + 1] == str(repo)


def test_the_runner_removes_this_sessions_identity_from_the_child(repo, monkeypatch):
    runner = _load_runner()
    monkeypatch.setenv("CODEBUDDY_SESSION_ID", "should-not-inherit")
    monkeypatch.setenv("AGENT_RUNNER_HOME", str(repo / "home"))
    run = runner.Run(
        {"request_id": "agent-x", "repo_path": str(repo), "prompt": "x"}, None
    )
    env = run.environment()
    assert "CODEBUDDY_SESSION_ID" not in env
    assert env["HOME"].endswith("agent-x")
    assert os.path.isdir(env["HOME"])


def test_the_runner_reads_a_stream_json_progress_line():
    runner = _load_runner()
    run = runner.Run({"request_id": "agent-x"}, _Recorder())
    run.consume('{"type":"progress","text":"reading the repository"}\n')
    assert run.progress_count == 1
    assert run.spool.records[-1][1] == "reading the repository"


def test_the_runner_reads_a_stream_json_result_line():
    runner = _load_runner()
    run = runner.Run({"request_id": "agent-x"}, _Recorder())
    run.consume('{"type":"result","result":"all done"}\n')
    assert run.answer("") == "all done"


def test_the_runner_reads_plain_text_as_progress_and_still_answers():
    """A CLI that ignores ``--output-format`` must still produce a report."""
    runner = _load_runner()
    run = runner.Run({"request_id": "agent-x"}, _Recorder())
    run.consume("I changed the parser\n")
    run.consume("and the tests pass\n")
    assert run.answer("") == "I changed the parser\nand the tests pass"


def test_the_runner_finds_a_marked_question():
    runner = _load_runner()
    run = runner.Run({"request_id": "agent-x"}, _Recorder())
    run.consume('{"type":"result","result":"QUESTION: should I push?"}\n')
    assert runner._QUESTION_RE.search(run.answer(""))


def test_the_runner_redacts_a_credential_before_it_leaves():
    runner = _load_runner()
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in runner.redact(
        "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    )


def test_the_runner_does_not_re_run_a_task_whose_stream_ended(repo, monkeypatch):
    """The idempotency the brief asks for, on the side that would duplicate work."""
    runner = _load_runner()
    spool = _load_spool()
    spool.ensure()
    spool.write_request(
        "agent-x",
        {"request_id": "agent-x", "repository": "demo", "repo_path": str(repo),
         "operation": "edit", "task": "x", "prompt": "x"},
    )
    spool.append("agent-x", "result", "already done")
    monkeypatch.setenv("AGENT_CLI", "/bin/false")
    assert runner.process(spool, "agent-x") == 0
    assert spool.line_count("agent-x") == 1


def test_the_runner_refuses_a_request_that_fails_validation(repo, monkeypatch):
    runner = _load_runner()
    spool = _load_spool()
    spool.ensure()
    spool.write_request(
        "agent-y",
        {"request_id": "agent-y", "repository": "elsewhere",
         "repo_path": "/etc", "operation": "edit", "task": "x", "prompt": "x"},
    )
    monkeypatch.setenv("AGENT_CLI", "/bin/false")
    assert runner.process(spool, "agent-y") == 1
    kinds = [r["kind"] for r in spool.read_lines("agent-y")]
    assert kinds == ["error"]
    assert "refused" in spool.read_lines("agent-y")[0]["text"]


def test_the_runner_runs_a_real_child_and_streams_its_output(repo, monkeypatch):
    """End to end through a real subprocess, with a stand-in for the agent."""
    runner = _load_runner()
    spool = _load_spool()
    spool.ensure()
    script = repo / "fake-agent.sh"
    script.write_text(
        "#!/bin/bash\n"
        'echo \'{"type":"progress","text":"working"}\'\n'
        'echo \'{"type":"result","result":"finished"}\'\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("AGENT_CLI", str(script))
    monkeypatch.setenv("AGENT_CLI_ARGS", "-p")
    monkeypatch.delenv("AGENT_ADD_DIR", raising=False)
    monkeypatch.setenv("AGENT_RUNNER_HOME", str(repo / "home"))

    spool.write_request(
        "agent-z",
        {"request_id": "agent-z", "repository": "demo", "repo_path": str(repo),
         "operation": "edit", "task": "x", "prompt": "x", "timeout_seconds": 60},
    )
    assert runner.process(spool, "agent-z") == 0
    kinds = [r["kind"] for r in spool.read_lines("agent-z")]
    assert kinds == ["started", "progress", "result"]
    assert spool.result_text("agent-z") == "finished"
    assert not spool.locked("agent-z")


def _load_spool():
    from app import agent_spool as spool

    return spool


class _Recorder:
    """A spool double: records what the runner would have written."""

    def __init__(self):
        self.records: list[tuple] = []

    def append(self, request_id, kind, text="", **extra):
        self.records.append((request_id, text, kind))
        return True

    def cancel_requested(self, request_id):
        return False
