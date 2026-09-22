#!/usr/bin/env python3
"""The host half of the bridge: claim a task, run the coding agent, stream back.

Why this is a separate process
------------------------------
The bot runs in a container built from ``Dockerfile``, which copies ``app/`` and
``requirements.txt`` and nothing else — no Node, no CodeBuddy CLI, and no
package manager to install one with. Adding them would multiply the image and
put a general-purpose coding agent inside the process that holds the bot token.
So the execution half lives here, on the host, and the two halves meet over the
spool directory that ``app/agent_spool.py`` describes.

Running it
----------
::

    sudo -u root /root/guardbot/.venv/bin/python /root/guardbot/tools/agent_runner.py

or, as a service, see ``AgentMD.md``. It runs until killed; ``--once`` performs
one pass and exits, which is what the tests use.

What it checks for itself
-------------------------
The container already validated the request — repository against the allowlist,
operation against the vocabulary, actor against ``rbac``. This process re-checks
the parts that are *its* safety boundary, because it is the one with a
filesystem and a shell:

* the logical repository name is on **its own** copy of the allowlist, and the
  path in the envelope is exactly the path that name means;
* the operation is one of the ten in the vocabulary;
* the task text is non-empty;
* the directory exists and is a git working tree.

A request that fails any of those is marked failed and never executed. That is
deliberate: a container that had been compromised, or a spool directory written
by hand, produces a refusal rather than a shell.

What it deliberately cannot do
------------------------------
It cannot write the database. It has no SQLite connection at all — it imports
``app.agent_spool``, which imports nothing but the standard library — so the
container remains the single writer and the "database is locked" failure cannot
happen. Everything it learns goes into the stream, and the container decides
what that means.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# The repository allowlist, checked *again* here. Deliberately a literal rather
# than a shared import: the point of two checks is that they are two, and a
# single source would make a mistake in it a mistake in both. It can be
# overridden from the environment for a deployment that keeps its repositories
# elsewhere, which is also how the container's copy is configured.
DEFAULT_REPOSITORIES = {
    "guardbot": "/root/guardbot",
    "vpn-bot": "/opt/vpn-bot",
}

# The operation vocabulary, checked again here for the same reason. The values
# are the dangerous ones — an operation in this set is *refused* by this runner
# even if it somehow reached the spool, because the container's job is to make
# that impossible and this one's is not to depend on it having succeeded.
DANGEROUS_OPERATIONS = frozenset(
    {"deploy", "migrate", "delete", "reset", "credentials"}
)
OPERATIONS = frozenset(
    {"analyse", "test", "edit", "commit", "push"}
    | DANGEROUS_OPERATIONS
)

# The environment variables that identify *this* CodeBuddy session. They are
# removed from the child's environment: a nested run that inherited them would
# believe it was resuming the conversation that started it, and the CLI's
# session bookkeeping would collide with the parent's.
_SESSION_VARS = (
    "CODEBUDDY_SESSION_ID",
    "CODEBUDDY_CONVERSATION_MESSAGE_ID",
    "CODEBUDDY_CONVERSATION_REQUEST_ID",
    "CODEBUDDY_ROOT_REQUEST_ID",
    "CODEBUDDY_PROJECT_DIR",
    "CODEBUDDY_IDE_PORT",
    "CLAUDE_CODE_SSE_PORT",
    "BAGGAGE",
)

_SECRET_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b\s*[:=]\s*\S+"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
)
REDACTED = "<redacted>"
QUESTION_MARKER = "QUESTION:"
_QUESTION_RE = re.compile(
    r"^\s*" + re.escape(QUESTION_MARKER) + r"\s*(.+)$", re.MULTILINE | re.IGNORECASE
)


def redact(text: str) -> str:
    """Strip credential-shaped substrings. Applied to everything that leaves."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def log(message: str, *args) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{stamp}] {message % args if args else message}\n")
    sys.stderr.flush()


# ── The spool, imported from the project ──────────────────────────────────
# ``app/agent_spool.py`` imports nothing but the standard library, which is what
# makes it safe for this process to import: no ``config`` (which would demand
# the bot's environment) and no ``db`` (which would open the database).
def _load_spool():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    from app import agent_spool  # noqa: PLC0415 - see the comment above

    return agent_spool


# ── Validating a request ──────────────────────────────────────────────────
def repositories() -> dict[str, str]:
    raw = os.getenv("AGENT_REPOSITORIES", "")
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if "=" not in item:
            continue
        name, _, target = item.partition("=")
        name, target = name.strip().lower(), target.strip()
        if name and target:
            out[name] = target
    return out or dict(DEFAULT_REPOSITORIES)


def validate(payload: dict) -> str:
    """Why this request may not run, or ``""``.

    Order matters only in that the cheapest check is first; every one of them
    is a refusal rather than a repair, and none of them has a fallback.
    """
    name = str(payload.get("repository") or "").strip().lower()
    path = str(payload.get("repo_path") or "").strip()
    operation = str(payload.get("operation") or "").strip().lower()
    task = str(payload.get("task") or "").strip()

    known = repositories()
    if name not in known:
        return f"repository {name!r} is not on this runner's allowlist"
    expected = os.path.realpath(known[name])
    if os.path.realpath(path) != expected:
        # The container resolves the name to a path, and this checks that the
        # path it resolved is the path the name means *here*. A spool file
        # written by hand, or a container whose allowlist differs, is refused
        # rather than trusted — this is the check that makes the container's
        # resolution an optimisation instead of the only defence.
        return f"path {path!r} is not the path {name!r} means here"
    if not os.path.isdir(path):
        return f"the directory {path!r} does not exist on this host"
    if operation and operation not in OPERATIONS:
        return f"operation {operation!r} is not in the vocabulary"
    if not task:
        return "the task text is empty"
    return ""


# ── Running the agent ─────────────────────────────────────────────────────
class Run:
    """One child process, its output, and how it ended."""

    def __init__(self, payload: dict, spool):
        self.payload = payload
        self.spool = spool
        self.request_id = str(payload.get("request_id") or "")
        self.stdout_tail: list[str] = []
        self.result_text = ""
        self.error = ""
        self.cancelled = False
        self.session_id = ""
        self.progress_count = 0

    # -- emitting ---------------------------------------------------------
    def _emit(self, kind: str, text: str, **extra) -> None:
        self.spool.append(self.request_id, kind, redact(text), **extra)

    def progress(self, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        self.progress_count += 1
        self._emit("progress", body[: int(self.payload.get("progress_max_chars") or 600)])

    # -- the environment --------------------------------------------------
    def environment(self) -> dict:
        env = dict(os.environ)
        for name in _SESSION_VARS:
            env.pop(name, None)
        home = os.path.join(
            os.getenv("AGENT_RUNNER_HOME", "/run/guardbot-agent"), self.request_id
        )
        os.makedirs(home, mode=0o700, exist_ok=True)
        env["HOME"] = home
        # The agent's own working directory, so a tool that resolves a relative
        # path does so inside the repository rather than wherever this was
        # started from.
        env["PWD"] = str(self.payload.get("repo_path") or "")
        return env

    # -- the command ------------------------------------------------------
    def argv(self) -> list[str]:
        """How to run the agent. Decided here, not by the container.

        The container sends the task; this process decides what executes it, and
        it does not read a binary name or an argument list out of the request.
        The reason is that those are a trust edge with nothing on the other side
        of it: the container's job is *what* to do and this one's is *how*, and a
        container that could name an executable could name one that is not a
        coding agent. ``AGENT_CLI`` and ``AGENT_CLI_ARGS`` are the deployment's,
        set in the runner's own environment where the owner can see them.
        """
        cli = os.getenv("AGENT_CLI", "codebuddy")
        args = [
            item.strip()
            for item in os.getenv("AGENT_CLI_ARGS", "").split(",")
            if item.strip()
        ]
        if not args:
            args = ["-p", "--output-format", "stream-json"]

        # The two bounds this runner owns. The turn ceiling is its own cost
        # control and the directory is its own safety boundary; both can be
        # overridden from the environment, and neither can be widened by the
        # request beyond what the deployment allows.
        turns = int(
            os.getenv("AGENT_MAX_TURNS") or self.payload.get("max_turns") or 40
        )
        if "--max-turns" not in args:
            args = args + ["--max-turns", str(max(1, turns))]

        repo = str(self.payload.get("repo_path") or "")
        # ``--add-dir`` is what makes a headless run non-interactive. Without it
        # the CLI may stop and ask permission to touch a path, and a run that
        # stops to ask a question nobody can answer looks exactly like a hang —
        # which is one of the failures this bridge was written after. It is
        # suppressible for a CLI that does not accept the flag.
        if repo and "--add-dir" not in args and os.getenv("AGENT_ADD_DIR", "1") != "0":
            args = args + ["--add-dir", repo]

        prompt = str(self.payload.get("prompt") or "")
        if "-p" in args or "--print" in args:
            return [cli, *args, prompt]
        return [cli, *args, "-p", prompt]

    # -- the loop ---------------------------------------------------------
    def execute(self) -> int:
        """Run the child, bounded by a clock this side controls.

        The timeout is enforced by a watchdog rather than by the read loop,
        because the read loop is exactly what a hung child stops doing. A CLI
        that starts, prints nothing and never exits — which is what the loopback
        port collision produces — would otherwise hold a task in ``running``
        until the container's own timeout, and the point of a bound is that it
        is applied by the side that can still act.
        """
        import threading  # noqa: PLC0415 - only needed here

        argv = self.argv()
        repo = str(self.payload.get("repo_path") or "")
        timeout = max(
            60,
            int(
                os.getenv("AGENT_TIMEOUT_SECONDS")
                or self.payload.get("timeout_seconds")
                or 1800
            ),
        )
        log("running %s in %s (timeout %ds)", argv[0], repo, timeout)

        try:
            child = subprocess.Popen(
                argv,
                cwd=repo,
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            self._emit("error", f"could not start the agent: {exc}")
            return 127

        deadline = time.time() + timeout
        stopped = threading.Event()

        def watchdog() -> None:
            while not stopped.wait(2.0):
                if self.spool.cancel_requested(self.request_id):
                    self.cancelled = True
                    self._stop(child)
                    return
                if time.time() > deadline:
                    self.error = "timed out"
                    self._stop(child)
                    return

        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()

        stderr_tail = ""
        try:
            assert child.stdout is not None
            for line in child.stdout:
                self.consume(line)
            child.wait(timeout=30)
            if child.stderr is not None:
                stderr_tail = child.stderr.read() or ""
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            if not self.error and not self.cancelled:
                self.error = str(exc)[:400]
            self._stop(child)
        finally:
            stopped.set()
            watcher.join(timeout=5)

        if child.returncode not in (0, None) and not self.error and not self.cancelled:
            if not self.result_text.strip():
                self.error = f"the agent exited with status {child.returncode}"

        if self.cancelled:
            self._emit("cancelled", "stopped at the owner's request")
            return 0
        if self.error:
            detail = self.error
            if stderr_tail.strip():
                detail += "\n" + stderr_tail.strip()[-1500:]
            elif self.stdout_tail:
                # Nothing on stderr, which is the shape a CLI that fails inside
                # its own event loop produces — it reports the fault on stdout
                # and then hangs rather than exiting. Without this the owner
                # would be told "timed out" and nothing else, and the line that
                # says *why* would be sitting in a transcript nobody can read.
                detail += "\n" + "\n".join(self.stdout_tail[-8:])[-1500:]
            self._emit("error", detail, session_id=self.session_id)
            return 1

        answer = self.answer(stderr_tail)
        question = _QUESTION_RE.search(answer or "")
        if question:
            # The agent stopped to ask. The question goes out as a question and
            # *not* as a result, because the container turns the two into
            # different states — one waits for the owner, the other finishes.
            self._emit("question", question.group(1).strip()[:400],
                       session_id=self.session_id)
            return 0
        self._emit("result", answer, session_id=self.session_id)
        return 0

    def _stop(self, child) -> None:
        """Stop the child and everything it started. Never leaves an orphan."""
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                child.terminate()
            except Exception:  # noqa: BLE001
                return
        try:
            child.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    # -- reading a line ---------------------------------------------------
    def consume(self, line: str) -> None:
        """Read one line of the agent's output.

        Two output shapes are supported on purpose. ``stream-json`` gives one
        JSON object per line and is what the default arguments ask for; plain
        text is what a CLI that ignored the flag produces, and the runner has to
        cope with both because the invocation is a deployment detail this code
        cannot verify. A line that is JSON is mined for text and classified; a
        line that is not is progress, and also accumulates as the answer so that
        a plain-text run still ends with something to report.
        """
        raw = line.rstrip("\n")
        if not raw.strip():
            return
        self.stdout_tail.append(raw)
        if len(self.stdout_tail) > 4000:
            del self.stdout_tail[:1000]

        record = None
        if raw.lstrip().startswith("{"):
            try:
                record = json.loads(raw)
            except ValueError:
                record = None

        if not isinstance(record, dict):
            self.progress(raw)
            return

        kind = str(record.get("type") or record.get("kind") or "")
        session = record.get("session_id") or record.get("sessionId")
        if session and not self.session_id:
            # Kept on the run rather than emitted as its own line: the id is
            # recorded on whichever line ends the run, and a line whose only
            # content is an id would be a message with nothing in it.
            self.session_id = str(session)[:80]

        text = _text_of(record)
        if kind in ("result", "done", "complete"):
            if text:
                self.result_text = text
            return
        if kind in ("error",):
            self.error = text or "the agent reported an error"
            return
        if text:
            self.progress(text)

    def answer(self, stderr_tail: str) -> str:
        """The final answer: the agent's own result, or the tail of its output."""
        if self.result_text.strip():
            return self.result_text.strip()
        body = "\n".join(self.stdout_tail).strip()
        if body:
            return body
        if stderr_tail.strip():
            return stderr_tail.strip()
        return ""


def _text_of(record: dict) -> str:
    """The readable text inside one JSON line, whatever shape it has.

    Written as a walk rather than as a schema, because the CLI's event shape is
    not this repository's to fix and a strict reader would silently deliver
    nothing the day it changes.
    """
    for key in ("result", "text", "content", "message", "output"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("message", "content", "delta", "data"):
        value = record.get(key)
        if isinstance(value, dict):
            inner = _text_of(value)
            if inner:
                return inner
        if isinstance(value, list):
            parts = [
                _text_of(item) if isinstance(item, dict) else str(item)
                for item in value
            ]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    return ""


# ── One pass ──────────────────────────────────────────────────────────────
def process(spool, request_id: str) -> int:
    """Claim and run one request. Returns a shell-ish status."""
    payload = spool.read_request(request_id)
    if not payload:
        return 0
    if spool.last_kind(request_id) in spool.TERMINAL_KINDS:
        # Already finished, and the container has not pruned it yet. Re-running
        # it would be the duplicate execution the brief forbids.
        return 0
    if not spool.claim(request_id):
        return 0  # another runner holds it

    try:
        reason = validate(payload)
        if reason:
            log("refusing %s: %s", request_id, reason)
            spool.append(request_id, "error", f"refused: {reason}")
            return 1

        run = Run(payload, spool)
        spool.append(request_id, "started", "", pid=os.getpid())
        return run.execute()
    finally:
        spool.release(request_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run coding tasks for GuardBot.")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument(
        "--interval", type=float, default=3.0, help="seconds between passes"
    )
    parser.add_argument("--task", default="", help="run one request id and exit")
    args = parser.parse_args(argv)

    spool = _load_spool()
    spool.ensure()

    if args.task:
        return process(spool, args.task)

    if args.once:
        seen = 0
        for request_id in spool.pending_requests():
            seen += 1
            process(spool, request_id)
        return 0

    log(
        "agent runner starting: spool=%s repositories=%s",
        spool.spool_dir(),
        ",".join(sorted(repositories())),
    )
    while True:
        try:
            for request_id in spool.pending_requests():
                process(spool, request_id)
        except KeyboardInterrupt:
            log("stopping")
            return 0
        except Exception as exc:  # noqa: BLE001 - the loop outlives its faults
            log("pass failed: %s", exc)
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":
    sys.exit(main())
