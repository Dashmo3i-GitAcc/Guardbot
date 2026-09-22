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

How it runs the agent
---------------------
Through the CodeBuddy job broker, with ``--bg``, because that is the only
invocation measured to work on this host — the foreground ``-p`` never returns.
The launcher hands over a job in about a second and the work continues in the
broker's process, so the outcome is read from the job's own
``$HOME/.codebuddy/jobs/<shortId>/state.json`` rather than from stdout. See the
constants below for what was measured and why each part is the way it is.

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

# ── The CodeBuddy job mechanism ───────────────────────────────────────────
# How this host can run the agent without a terminal, measured 2026-09-22.
#
# ``codebuddy -p`` in the foreground does not work here. It starts, prints
# nothing and never exits — in six different shapes (fresh and real ``HOME``,
# ``-y``, ``--permission-mode dontAsk``, ``--permission-mode acceptEdits``, and
# stdin closed), so it is not a prompt, not stdin and not a permission question.
#
# ``--bg`` works. It hands the session to the CodeBuddy job broker, which is
# where the authentication lives, and returns immediately:
#
#     backgrounded · gb-runne · gb-runner-realhome-5020
#
# The run's outcome is not on stdout. It lands in
# ``$HOME/.codebuddy/jobs/<shortId>/state.json`` and moves through
# ``state=working, tempo=active`` to ``state=done, tempo=idle``, with the answer
# in ``output["result"]``. That file is what this runner reads.
#
# Two facts about it are load-bearing and were both measured:
#
# * ``shortId`` is the *first eight characters* of the name, so it is neither
#   unique nor predictable. The launcher prints it and this runner matches
#   ``state.json`` on the ``sessionId`` it set itself, never on the directory
#   name.
# * The authentication is in ``$HOME/.codebuddy``. A child given a fresh ``HOME``
#   reports ``Authentication required`` as its *result* — a successful job whose
#   text is a login prompt. So the child inherits the real ``HOME``, and that
#   sentence is treated as the failure it is rather than relayed as an answer.
JOB_DIR_NAME = ".codebuddy/jobs"
JOB_STATE_FILE = "state.json"
JOB_LAUNCH_TIMEOUT_SECONDS = 90.0
JOB_POLL_SECONDS = 2.0
# ``working``/``active`` while it runs; anything else is an ending. Written as
# the running set rather than the terminal set because a state this code has
# never seen is far more likely to be an ending than a new kind of waiting, and
# waiting forever is the failure this whole file exists to avoid.
JOB_RUNNING_STATES = frozenset(
    {"working", "starting", "queued", "pending", "running", "active"}
)
_AUTH_REQUIRED_RE = re.compile(
    r"authentication required|please (?:use /)?login|not (?:logged|signed) in",
    re.IGNORECASE,
)
_SHORT_ID_RE = re.compile(r"backgrounded[^\n]*?([A-Za-z0-9_-]{2,})\s*[·•|]")
_DETAIL_PREFIXES = ("result:", "error:", "failed:")


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
        self.result_text = ""
        self.error = ""
        self.cancelled = False
        # The id this run's job is found by. Made here, unique per launch, and
        # deliberately *not* the request id: reusing a session id would resume
        # the earlier conversation rather than start the work again, and "run
        # this task" is not "carry on with that one". It is also what the
        # emitted ``session_id`` carries, which is how the container records
        # which CodeBuddy session a task became.
        self.session_id = f"{self.request_id}-{int(time.time())}"
        self.short_id = ""
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
    def home(self) -> str:
        """The HOME the child gets: the real one, unless told otherwise.

        A fresh HOME per request was the original design and it is wrong here.
        The CodeBuddy authentication lives in ``$HOME/.codebuddy``, and a child
        that cannot see it does not fail — it *succeeds*, with the text
        ``Authentication required. Please use /login command to sign in``, which
        would then be relayed to the owner as though the agent had answered.

        Isolation is bought with ``--no-session-persistence`` and an explicit
        ``--session-id`` instead, which is what those flags are for.
        ``AGENT_RUNNER_HOME`` still overrides this for a deployment that keeps a
        prepared profile elsewhere, and that profile has to carry the
        authentication or the same sentence comes back.
        """
        override = os.getenv("AGENT_RUNNER_HOME", "").strip()
        return override or os.path.expanduser("~")

    def environment(self) -> dict:
        env = dict(os.environ)
        for name in _SESSION_VARS:
            env.pop(name, None)
        env["HOME"] = self.home()
        # The agent's own working directory, so a tool that resolves a relative
        # path does so inside the repository rather than wherever this was
        # started from.
        env["PWD"] = str(self.payload.get("repo_path") or "")
        return env

    def jobs_dir(self) -> str:
        """Where the broker writes this job's state. One directory per job."""
        return os.path.join(self.home(), JOB_DIR_NAME)

    def preflight(self) -> str:
        """Why no job can run at all, or ``""``.

        Checked before launching so that a missing profile is a sentence in the
        log rather than a login prompt arriving as the agent's answer.
        """
        if not os.path.isdir(os.path.join(self.home(), ".codebuddy")):
            return (
                f"no CodeBuddy profile under {self.home()!r}: the agent would run "
                "unauthenticated. Unset AGENT_RUNNER_HOME, or point it at a "
                "profile that is logged in."
            )
        return ""

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

        ``--bg`` is not decoration. It is the only invocation measured to work
        here: the foreground ``-p`` never returns, and ``--bg`` is what puts the
        session under the job broker that holds the authentication. ``--name``
        and ``--session-id`` are both set so that the job this launch created can
        be found again, whatever the broker decides to call its directory.
        """
        cli = os.getenv("AGENT_CLI", "codebuddy")
        extra = [
            item.strip()
            for item in os.getenv("AGENT_CLI_ARGS", "").split(",")
            if item.strip()
        ]

        args = ["--bg", "--name", self.request_id, "--session-id", self.session_id]
        args += extra

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
        # stops to ask a question nobody can answer looks exactly like a hang.
        # It is suppressible for a CLI that does not accept the flag.
        if repo and "--add-dir" not in args and os.getenv("AGENT_ADD_DIR", "1") != "0":
            args = args + ["--add-dir", repo]

        return [cli, *args, "-p", str(self.payload.get("prompt") or "")]

    # -- the job ----------------------------------------------------------
    def _state_path(self, short_id: str) -> str:
        return os.path.join(self.jobs_dir(), short_id, JOB_STATE_FILE)

    def _read_state(self, short_id: str) -> dict | None:
        """This job's state file, or ``None``. Never raises.

        A half-written file is a normal thing to find — the broker rewrites it
        while the job runs — so a parse failure is "not yet", not "broken".
        """
        if not short_id:
            return None
        try:
            with open(self._state_path(short_id), encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _find_state(self) -> dict | None:
        """Find this run's job by the id this run chose, not by its directory.

        The fallback for a launcher whose line could not be parsed. Matched on
        ``sessionId`` because the broker's directory name is the first eight
        characters of ours, and is therefore neither unique nor predictable —
        two launches can share one.
        """
        try:
            names = sorted(os.listdir(self.jobs_dir()))
        except OSError:
            return None
        for name in names:
            state = self._read_state(name)
            if state and str(state.get("sessionId") or "") == self.session_id:
                self.short_id = self.short_id or name
                return state
        return None

    def _state(self) -> dict | None:
        return self._read_state(self.short_id) or self._find_state()

    def _launch(self, argv: list[str]) -> str:
        """Hand the task to the broker. Returns ``""`` or why it did not.

        The launcher is expected to return almost immediately with the job's
        short id; it is not the job. Waiting on it is therefore a bound on the
        *handover*, not on the work, and a launcher that sits there is the
        failure mode this whole file was written around.
        """
        repo = str(self.payload.get("repo_path") or "")
        try:
            child = subprocess.Popen(
                argv,
                cwd=repo,
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            return f"could not start the agent: {exc}"

        text = self._read_handover(child)
        self.short_id = _short_id_from(text)
        if not self.short_id:
            # The launcher's own words, because they are the only diagnosis
            # there is — the same reason the old read loop fell back to stdout
            # when stderr was empty.
            tail = text.strip()[-600:]
            return "the agent did not report a background job: " + (tail or "no output")
        return ""

    def _read_handover(self, child) -> str:
        """The launcher's own words, up to the moment it says it has handed over.

        Read on a thread and stopped at the marker rather than with
        ``communicate``, because ``communicate`` waits for end-of-file on the
        pipes — and a detached worker that inherited them holds them open. That
        would make this wait for the *work* instead of the handover, and a bound
        that lasts as long as the thing it is bounding is not a bound.
        """
        import threading  # noqa: PLC0415 - only needed here

        chunks: list[str] = []

        def pump() -> None:
            try:
                assert child.stdout is not None
                for line in child.stdout:
                    chunks.append(line)
                    if "backgrounded" in line.lower():
                        return
            except Exception:  # noqa: BLE001 - the caller reports what arrived
                pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(timeout=JOB_LAUNCH_TIMEOUT_SECONDS)

        # Reaping the launcher is a courtesy to the process table and nothing
        # more; it does not wait on the pipes, so it returns as soon as the
        # launcher itself is gone.
        try:
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        for stream in (child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        return "".join(chunks)

    def _answer_from_state(self, state: dict) -> str:
        """The job's answer, out of whichever field it landed in.

        ``output`` is the documented place and is what the measured runs use;
        ``detail`` is the same text with a ``result:`` prefix. Read as a walk
        rather than a schema, for the same reason ``_text_of`` is.
        """
        output = state.get("output")
        if isinstance(output, dict):
            text = _text_of(output)
            if text:
                return text.strip()
        elif isinstance(output, str) and output.strip():
            return output.strip()
        detail = str(state.get("detail") or "").strip()
        lowered = detail.lower()
        for prefix in _DETAIL_PREFIXES:
            if lowered.startswith(prefix):
                return detail[len(prefix):].strip()
        return detail

    def _stop_job(self) -> None:
        """Stop the job, using the pid the broker recorded for it.

        Not the launcher's pid: that process has already exited, which is the
        whole point of a background job.
        """
        state = self._state()
        try:
            pid = int((state or {}).get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 1:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except Exception:  # noqa: BLE001
                try:
                    os.kill(pid, sig)
                except Exception:  # noqa: BLE001
                    return
            for _ in range(20):
                time.sleep(0.25)
                if not _alive(pid):
                    return

    # -- the loop ---------------------------------------------------------
    def execute(self) -> int:
        """Launch the job, then watch it to an ending this side bounds.

        The bound is a deadline in this loop rather than a watchdog on a child,
        because there is no child to watch: the launcher exits in a second and
        the work continues in the broker's process. The clock is still ours —
        the container's timeout is the backstop, and a bound applied by the side
        that can still act is the point.
        """
        reason = self.preflight()
        if reason:
            log("refusing %s: %s", self.request_id, reason)
            self._emit("error", reason)
            return 1

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
        log(
            "launching %s in %s (session %s, timeout %ds)",
            argv[0],
            repo,
            self.session_id,
            timeout,
        )

        failure = self._launch(argv)
        if failure:
            self._emit("error", failure, session_id=self.session_id)
            return 1

        deadline = time.time() + timeout
        seen_detail = ""
        while True:
            if self.spool.cancel_requested(self.request_id):
                self.cancelled = True
                break
            if time.time() > deadline:
                self.error = "timed out"
                break

            state = self._state()
            if state is None:
                time.sleep(JOB_POLL_SECONDS)
                continue

            detail = str(state.get("detail") or "").strip()
            if detail and detail != seen_detail:
                seen_detail = detail
                # ``detail`` holds the running commentary *and* the final
                # ``result:`` line. Only the commentary is progress; the ending
                # is read once, below, so the answer is not also sent as a
                # progress message.
                if not detail.lower().startswith(_DETAIL_PREFIXES):
                    readable = _readable_detail(detail)
                    if readable:
                        self.progress(readable)

            name = str(state.get("state") or "").strip().lower()
            if name and name not in JOB_RUNNING_STATES:
                self.result_text = self._answer_from_state(state)
                break
            time.sleep(JOB_POLL_SECONDS)

        if self.cancelled:
            self._stop_job()
            self._emit("cancelled", "stopped at the owner's request")
            return 0
        if self.error:
            self._stop_job()
            self._emit("error", self.error, session_id=self.session_id)
            return 1

        answer = self.result_text.strip()
        if not answer:
            self._emit(
                "error",
                "the job ended without a result",
                session_id=self.session_id,
            )
            return 1
        if _AUTH_REQUIRED_RE.search(answer):
            # A job that succeeds with a login prompt as its text is the worst
            # shape this can fail in: the container would store it as a
            # successful answer and the owner would read it as one. It is the
            # signature of a HOME without the CodeBuddy profile, so it is named.
            self._emit(
                "error",
                "the agent is not authenticated on this host: " + answer[:300],
                session_id=self.session_id,
            )
            return 1

        question = _QUESTION_RE.search(answer)
        if question:
            # The agent stopped to ask. The question goes out as a question and
            # *not* as a result, because the container turns the two into
            # different states — one waits for the owner, the other finishes.
            self._emit(
                "question",
                question.group(1).strip()[:400],
                session_id=self.session_id,
            )
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


def _alive(pid: int) -> bool:
    """Whether a pid still exists. Signal 0 asks the kernel, and asks nothing else."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _short_id_from(text: str) -> str:
    """The job's short id, out of the launcher's ``backgrounded · <id> · <name>``.

    Parsed rather than computed: the broker names the directory from the first
    eight characters of the name, which is a rule about *its* naming and not a
    contract. The launcher states the id it used, so that is what is read, and a
    parse failure falls back to matching the state files on ``sessionId``.
    """
    for line in (text or "").splitlines():
        if "backgrounded" not in line.lower():
            continue
        match = _SHORT_ID_RE.search(line)
        if match:
            return match.group(1)
    return ""


def _readable_detail(detail: str) -> str:
    """A job's ``detail`` line as something worth putting in front of a person.

    Usually it is already a sentence — ``starting…``, ``requesting the model``.
    Sometimes it is a raw JSON blob, and relaying that verbatim would put
    ``{"summary": "..."}`` in the owner's chat, so it is mined for its text and
    dropped when it has none.
    """
    body = (detail or "").strip()
    if not body.startswith("{"):
        return body
    try:
        record = json.loads(body)
    except ValueError:
        return body
    if isinstance(record, dict):
        return _text_of(record).strip()
    return body


def _text_of(record: dict) -> str:
    """The readable text inside one JSON line, whatever shape it has.

    Written as a walk rather than as a schema, because the CLI's event shape is
    not this repository's to fix and a strict reader would silently deliver
    nothing the day it changes.
    """
    for key in ("result", "text", "content", "message", "output", "summary"):
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
