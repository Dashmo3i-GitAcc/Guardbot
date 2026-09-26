"""Isolation: observation can never change, delay or break Nexus.

This is the suite the brief cares about most. The archive is a *sink*: if it is
off, full, locked, corrupt or being monkeypatched to explode, Nexus must answer
exactly as it would have. Each test below breaks the archive in one specific way
and asserts that the failure is contained — a dropped record, a counted failure,
a returned False — and never an exception in the caller's path.

The last group pins the structural side of the same rule: the observation
package imports nothing that can act, and nothing on the authority path reads it.
"""
import ast
import asyncio
import glob
import os

from app import observe
from app.observe import schema, store


def run(coro):
    return asyncio.run(coro)


def _boom(*args, **kwargs):
    raise RuntimeError("archive exploded")


# ── Every failure is contained ─────────────────────────────────────────────
def test_start_stays_inert_when_the_archive_cannot_be_opened(archive, monkeypatch):
    monkeypatch.setattr(store, "init", _boom)

    async def scenario():
        return await observe.start()

    assert run(scenario()) is False
    assert observe.started() is False
    # And with no collector, emitting is a no-op rather than an error.
    assert observe.emit(schema.KIND_FLAG, event="x") is False


def test_a_failing_write_is_counted_and_the_worker_survives(archive, monkeypatch):
    async def scenario():
        await observe.start()
        monkeypatch.setattr(store, "insert_events", _boom)
        observe.emit(schema.KIND_FLAG, event="x")
        await observe.flush()
        counters = observe.counters()
        alive = observe.started()
        await observe.stop()
        return counters, alive

    counters, alive = run(scenario())
    assert counters["failed"] >= 1
    assert counters["last_error"]
    assert alive is True


def test_emit_never_raises_when_submission_fails(archive, monkeypatch):
    async def scenario():
        await observe.start()
        api = __import__("app.observe.api", fromlist=["api"])
        monkeypatch.setattr(api._COLLECTOR, "submit", _boom)
        return observe.emit(schema.KIND_FLAG, event="x")

    assert run(scenario()) is False


def test_emit_never_raises_on_a_malformed_field(archive):
    """A caller passing nonsense must cost its own record, not its turn."""

    async def scenario():
        await observe.start(worker=False)
        observe.emit(schema.KIND_AI, event="chat", duration_ms=object(), data=object())
        await observe.flush()

    run(scenario())  # no exception is the assertion
    assert store.counts()["events"] >= 1


def test_a_failing_retention_sweep_returns_an_error_not_an_exception(archive, monkeypatch):
    monkeypatch.setattr(store, "prune", _boom)

    async def scenario():
        await observe.start(worker=False)
        return await observe.sweep()

    result = run(scenario())
    assert "error" in result


def test_a_failing_report_returns_an_error_not_an_exception(archive, monkeypatch):
    async def scenario():
        await observe.start(worker=False)
        report = __import__("app.observe.report", fromlist=["report"])
        monkeypatch.setattr(report, "build", _boom)
        return await observe.report_now(3600)

    result = run(scenario())
    assert result["ok"] is False
    assert result.get("error")


def test_a_dead_store_does_not_block_a_caller(archive, monkeypatch):
    """A write that hangs must not be on the caller's path.

    ``emit`` only appends to a queue; it never waits for the write. This is
    asserted structurally — the caller's thread never enters ``store`` — by
    making every store write sleep and checking that ``emit`` still returns
    immediately.
    """
    import threading
    import time

    release = threading.Event()

    def _slow_insert(rows):
        release.wait(5.0)
        return len(rows)

    async def scenario():
        await observe.start()
        monkeypatch.setattr(store, "insert_events", _slow_insert)
        observe.emit(schema.KIND_FLAG, event="x")
        started = time.monotonic()
        for _ in range(50):
            observe.emit(schema.KIND_FLAG, event="y")
        elapsed = time.monotonic() - started
        release.set()
        await observe.stop()
        return elapsed

    elapsed = run(scenario())
    # Fifty non-blocking appends take microseconds, not the store's five seconds.
    assert elapsed < 1.0


# ── The package is a leaf ──────────────────────────────────────────────────
_FORBIDDEN = {
    "db",
    "main",
    "chat",
    "rbac",
    "admin_service",
    "moderation",
    "mod_policy",
    "media",
    "groups",
    "people",
    "nexus",
    "awareness",
    "gemini_pool",
    "transcribe",
    "voice_context",
    "web_search",
    "vpnbot",
    "state",
    "memory",
    "agent_service",
    "agent_poller",
    "classifier",
    "ai_intent",
}


def _observe_sources():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return sorted(glob.glob(os.path.join(root, "app", "observe", "*.py")))


def test_the_observation_package_imports_nothing_that_can_act():
    """It imports config and the standard library, and nothing that acts.

    A leaf is what lets the Gemini pool — which must never reach Telegram — emit
    to it safely, and what guarantees a telemetry bug cannot become a moderation
    bug. `agent_bridge` is the one deliberate exception: redaction delegates to
    the project's single pattern list rather than copying it, and that import is
    deferred inside the function.
    """
    for path in _observe_sources():
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root != "telegram", f"{path}: imports telegram"
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    root = (node.module or "").split(".")[0]
                    assert root != "telegram", f"{path}: imports telegram"
                    continue
                if node.level == 1:
                    # Within the observation package: `.schema`, `.store`, ...
                    # `from .cli import main` names a *function*, not `app.main`,
                    # so a bare name check here would be a false positive.
                    continue
                # `from .. import X` reaches the app package. X must be a leaf
                # (`config`) or the one deferred redaction dependency.
                for alias in node.names:
                    assert alias.name not in _FORBIDDEN, (
                        f"{path}: imports {alias.name} from the app package"
                    )
                module = (node.module or "").split(".")[-1]
                assert module not in _FORBIDDEN, f"{path}: imports {module}"


def test_nothing_on_the_authority_path_reads_the_archive():
    """The archive is a sink, never a source of authority.

    No module may read it back into a decision: `query` and `store.rows` are the
    read API, and they are reachable only from the observe package's own report
    and CLI. A future change that made a model's stored words loadable into a
    prompt, or let a stored event widen a permission, would fail here.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    authority = [
        os.path.join(root, "app", name)
        for name in ("rbac.py", "admin_service.py", "chat.py", "main.py", "nexus.py")
    ]
    for path in authority:
        source = open(path, encoding="utf-8").read()
        assert "observe.query" not in source, f"{path} reads the archive"
        assert "from .observe import query" not in source, f"{path} reads the archive"
        assert "store.rows(" not in source, f"{path} reads the archive"
        assert "store.one(" not in source, f"{path} reads the archive"


def test_the_read_api_is_imported_only_inside_observation():
    """`query` has exactly the importers it should: the report and the CLI."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    allowed = {"report.py", "cli.py"}
    for path in glob.glob(os.path.join(root, "app", "**", "*.py"), recursive=True):
        name = os.path.basename(path)
        if name in allowed:
            continue
        source = open(path, encoding="utf-8").read()
        assert "observe import query" not in source, f"{path} imports the read API"
        assert "observe.query" not in source, f"{path} reaches the read API"
