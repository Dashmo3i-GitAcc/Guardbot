"""The bridge: a request from the owner in Telegram, a coding agent on the host.

The shape of the problem, and why it is not "give the model a shell":

* Nexus already reads the group and already holds a tool surface, and every
  call it makes is re-authorised in ``app/admin_service.py`` against the real
  actor's Telegram id. A coding-agent request is *another tool*. It does not get
  a second authority model, a second prompt, or a second front door.
* The model may therefore *ask* for a coding task and may not decide anything
  about it. It cannot say it is the owner, cannot name a repository that is not
  on the allowlist, cannot mark an operation as approved, and cannot confirm its
  own dangerous operation. Those come from here and from ``app/rbac.py``.
* The agent itself does not run in this process. The container ships neither
  Node nor the CodeBuddy CLI (``Dockerfile`` copies ``app/`` and
  ``requirements.txt`` and nothing else), so the execution half is a separate
  host process — ``tools/agent_runner.py`` — and the two halves meet over the
  database and a spool directory under ``/data``, which the compose file already
  bind-mounts. See ``docs`` in ``AgentMD.md`` §39.

What this module owns
---------------------
* **The repository allowlist.** A logical name and the one directory it means.
  A request names a name; the path is looked up here, so nothing a model
  produced can become a filesystem path.
* **The operation vocabulary**, and which of them are dangerous.
* **The danger classifier.** A structured operation is the primary signal; the
  task text is scanned as well, and it can only ever *add* danger. A false
  positive costs one confirmation; a false negative costs an unconfirmed
  destructive action, so the asymmetry is deliberate.
* **Confirmation.** Who may confirm, what may be confirmed, and what a vague
  «اوکی» means. The rule is server-side: a bare confirmation resolves only when
  exactly one dangerous task is genuinely waiting, and otherwise it is refused
  with a question. The model never resolves it.
* **The lifecycle**, and the transitions that are legal.
* **The prompt**, and the transport for a long answer.

What it deliberately does not own
---------------------------------
* **Authority.** Nothing here is imported by ``app/rbac.py``, and the check that
  matters is ``admin_service``'s.
* **Execution.** There is no subprocess here and no shell. This module is pure,
  so "a member cannot start a task" is a unit test rather than a live
  experiment.
* **The awareness allowance.** Nothing here imports ``app/awareness.py`` or
  ``app/gemini_pool.py``. A coding task is not a model call on the awareness
  workload and must not spend its daily budget; a test asserts the import graph
  as well as the behaviour.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from . import config, db

log = logging.getLogger("guardbot.agent")

# ── The repository allowlist ──────────────────────────────────────────────
# A logical name and the one directory it means. The name is what a request
# carries; the path is resolved here. That indirection is the whole reason a
# model cannot ask the agent to work in ``/etc``: there is no expression it
# could produce that this dictionary would turn into an unlisted directory.
#
# ``root`` is the directory the agent is given. It is also the only thing the
# runner will pass to ``--add-dir``, and the runner re-checks it against its own
# copy of this list — two independent checks, because the runner is the process
# with the filesystem and the container is the process with the authority.
DEFAULT_REPOSITORIES = {
    "guardbot": "/root/guardbot",
    "vpn-bot": "/opt/vpn-bot",
}


def repositories() -> dict[str, str]:
    """The allowlist, from configuration. Never from a request."""
    return dict(getattr(config, "AGENT_REPOSITORIES", None) or DEFAULT_REPOSITORIES)


def repository_path(name: str) -> str:
    """The directory for a logical repository name, or ``""``.

    An unknown name is not an error to be reported back in detail: the caller
    answers with the list of names it does know, which is both more useful and
    less of a directory-enumeration oracle.
    """
    return repositories().get((name or "").strip().lower(), "")


def repository_names() -> list[str]:
    return sorted(repositories())


def parse_repository(value: str) -> str:
    """Read a repository out of whatever the model wrote.

    Accepts the logical name, or a path that is *exactly* one of the allowlisted
    roots, and returns the logical name. Anything else returns ``""``. The
    point of accepting the path at all is that the model has seen
    ``/root/guardbot`` in the trusted context and may echo it back; refusing
    that would produce a confusing refusal for a request that named a perfectly
    allowed repository.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in repositories():
        return lowered
    for name, path in repositories().items():
        if raw.rstrip("/") == path.rstrip("/"):
            return name
    return ""


# ── The operation vocabulary ──────────────────────────────────────────────
# ``danger`` is the reason a confirmation is required, or empty. The list is
# closed: an operation outside it is refused, because an open vocabulary means
# the danger table can be bypassed by inventing a word.
#
# The split is not "read vs write". ``edit`` writes files and is not dangerous —
# it changes a working tree, which is a git repository's whole purpose and is
# recoverable. ``deploy`` is dangerous because it changes what is *serving*.
# ``migrate`` is dangerous because a migration can destroy data. ``delete``,
# ``reset`` and ``credentials`` are dangerous for the reasons their names say.
OPERATIONS: dict[str, str] = {
    "analyse": "",
    "test": "",
    "edit": "",
    "commit": "",
    "push": "",
    "deploy": "deploying changes to a running service",
    "migrate": "running a migration that may change or destroy data",
    "delete": "deleting files or branches",
    "reset": "resetting a repository or a service to an earlier state",
    "credentials": "changing credentials, keys or secrets",
}


def parse_operation(value: str, task: str = "") -> str:
    """Read an operation out of a model's tool call, or ``""``.

    Missing is allowed and means "work it out from the task": the model is not
    required to classify its own request, and a request that omits the field is
    classified by the text scan below. Present-but-unknown is refused.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return "" if not task else "edit"
    aliases = {
        "analyze": "analyse",
        "review": "analyse",
        "inspect": "analyse",
        "fix": "edit",
        "implement": "edit",
        "refactor": "edit",
        "write": "edit",
        "tests": "test",
        "run-tests": "test",
        "pytest": "test",
        "git-commit": "commit",
        "git-push": "push",
        "release": "deploy",
        "migration": "migrate",
        "rm": "delete",
        "drop": "delete",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in OPERATIONS else ""


# The text scan. Deliberately about *destructive shapes* rather than about
# topics, and deliberately over-inclusive: every pattern here can only add a
# confirmation step, and the cost of asking is one message while the cost of not
# asking is a production change nobody approved.
_DANGER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bdeploy\b|دیپلوی|\bریلیز\b|\brelease\b|روی سرور نصب|ریستارت سرویس",
     "deploying changes to a running service"),
    (r"\brm\s+-rf\b|\brm\s+-r\b|حذف کن|پاک کن\b|\bdelete\b|\bdrop\s+table\b",
     "deleting files or data"),
    (r"\bforce[- ]push\b|--force\b|\bpush\s+-f\b|force\s+push",
     "force-pushing over history"),
    (r"\breset\s+--hard\b|\bgit\s+reset\b|ریست کن|\breset\b.*\bproduction\b",
     "resetting a repository or a service to an earlier state"),
    (r"\bmigrat|\bمهاجرت\b|\bschema\s+change\b|\balter\s+table\b",
     "running a migration that may change or destroy data"),
    (r"\bcredential|\bapi[ _-]?key\b|\bsecret\b|\btoken\b|\bپسورد\b|\bرمز\b|\bکلید\s+خصوصی",
     "changing credentials, keys or secrets"),
    (r"\bproduction\b|\bprod\b|پروداکشن|سرور اصلی",
     "acting on the production service"),
)
_DANGER_RE = tuple((re.compile(p, re.IGNORECASE), why) for p, why in _DANGER_PATTERNS)


def danger_for(operation: str, task: str = "") -> str:
    """Why this request needs the owner's confirmation, or ``""``.

    The structured operation wins when it declares danger; the text can only
    add. There is deliberately no way for either to *remove* danger.
    """
    if operation in OPERATIONS and OPERATIONS[operation]:
        return OPERATIONS[operation]
    text = task or ""
    if text:
        for pattern, why in _DANGER_RE:
            if pattern.search(text):
                return why
    return ""


# ── Confirmation ──────────────────────────────────────────────────────────
class Confirm(str, Enum):
    """The answer to "may this go ahead?", and why not if not."""

    OK = "ok"
    NOT_OWNER = "not_owner"
    NOTHING_PENDING = "nothing_pending"
    AMBIGUOUS = "ambiguous"
    NOT_WAITING = "not_waiting"


@dataclass(frozen=True)
class Confirmation:
    answer: Confirm
    request_id: str = ""
    detail: str = ""
    candidates: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return self.answer is Confirm.OK


def resolve_confirmation(
    *,
    actor_id: int,
    is_owner: bool,
    named_request_id: str = "",
    waiting: list[dict] | None = None,
) -> Confirmation:
    """Whether a confirmation may release a task, decided here and not by a model.

    Four rules, and each is one of the brief's sentences:

    * **Only the owner confirms.** A dangerous operation is the one place where
      "an administrator asked" is not enough, and it is checked against the id
      rather than against anything the model said about the person.
    * **There must be something waiting.** «اوکی» with nothing pending is not an
      approval of anything, and treating it as one would be exactly the
      "blindly guess" failure the brief warns about.
    * **A named task must actually be waiting.** The model may name one — it is
      how a follow-up to a specific message is expressed — and the name is
      checked against the table rather than trusted.
    * **A bare confirmation resolves only when exactly one task is waiting.**
      With two, the answer is a question listing them. The resolution is
      server-side: the model supplies no id and this function does not ask it
      to guess either.
    """
    if not is_owner:
        return Confirmation(Confirm.NOT_OWNER, detail="only the owner may confirm")
    pending = list(waiting or [])
    if not pending:
        return Confirmation(
            Confirm.NOTHING_PENDING,
            detail="no dangerous operation is waiting for confirmation",
        )
    named = (named_request_id or "").strip()
    if named:
        for row in pending:
            if row.get("request_id") == named:
                return Confirmation(Confirm.OK, request_id=named)
        return Confirmation(
            Confirm.NOT_WAITING,
            detail="that task is not waiting for confirmation",
            candidates=tuple(str(r.get("request_id") or "") for r in pending),
        )
    if len(pending) == 1:
        return Confirmation(Confirm.OK, request_id=str(pending[0].get("request_id")))
    return Confirmation(
        Confirm.AMBIGUOUS,
        detail="more than one task is waiting",
        candidates=tuple(str(r.get("request_id") or "") for r in pending),
    )


# ── The lifecycle ─────────────────────────────────────────────────────────
# Which moves are legal. ``waiting_for_owner`` is reachable from ``running``
# because the agent may stop and ask; it is *not* reachable from ``queued``,
# which is the state a dangerous task sits in until the owner confirms, and
# conflating the two would make an unconfirmed dangerous task look like one
# whose question had been answered.
#
# ``waiting_for_owner`` has exactly one way out, and it is ``queued``. That is
# the invariant that makes "an unapproved dangerous task cannot start" a
# property of the table rather than a promise about the code: a dangerous task
# is recorded in ``waiting_for_owner`` and is never published to the runner, so
# no runner can produce the ``started`` line that would move it to ``running`` —
# and even if one somehow did, ``running`` is not a legal move from here. Both
# the release (``confirm``) and the answer (``resume``) go through ``queued``.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "queued": ("running", "cancelled", "failed"),
    "running": ("waiting_for_owner", "succeeded", "failed", "cancelled",
                "timed_out"),
    "waiting_for_owner": ("queued", "cancelled", "failed", "timed_out"),
    "succeeded": (),
    "failed": (),
    "cancelled": (),
    "timed_out": (),
}


def transition_allowed(current: str, new: str) -> bool:
    if current == new:
        return True
    return new in ALLOWED_TRANSITIONS.get(current, ())


def path_to(current: str, target: str, *, limit: int = 4) -> list[str]:
    """The states to pass through to get from ``current`` to ``target``.

    Used by recovery, and only by recovery. The case it exists for is a stream
    that ends with a result while the database still says ``queued``: the runner
    ran and finished, and the ``started`` line was never applied because the bot
    was down when it arrived. Applying the terminal state directly would be
    refused by the table — correctly, since a task that never started cannot
    have succeeded — so the path is walked instead, which is what the stream is
    evidence for.

    Returns ``[]`` when there is no legal path, and the caller then does
    nothing. That is the right answer for a task the table says cannot be in
    that state at all.
    """
    if current == target:
        return []
    if transition_allowed(current, target):
        return [target]
    for middle in ALLOWED_TRANSITIONS.get(current, ()):
        if transition_allowed(middle, target):
            return [middle, target]
    _ = limit
    return []


def status_label(status: str) -> str:
    """A short human label. Used in the owner's own language."""
    return {
        "queued": "در صف",
        "running": "در حال اجرا",
        "waiting_for_owner": "منتظر تأیید شما",
        "succeeded": "انجام شد",
        "failed": "ناموفق",
        "cancelled": "لغو شد",
        "timed_out": "از زمان خارج شد",
    }.get(status, status or "?")


# ── The task envelope ─────────────────────────────────────────────────────
def new_request_id(actor_id: int, repository: str, task: str, *, now: float = 0.0) -> str:
    """A stable, collision-resistant id for one task.

    Derived from the content rather than random, so the *same* request made
    twice inside the idempotency window produces the same id — which is what
    makes the second one a duplicate rather than a second agent run. The actor
    is part of it, so two people asking the same thing are two tasks.
    """
    stamp = int(now or time.time())
    raw = f"{int(actor_id)}|{repository}|{task}|{stamp // 60}"
    return "agent-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class AgentRequest:
    """The validated envelope. Built here, from server-side values only.

    Every field is either resolved from configuration or copied from the
    ``AdminRequest`` the service already authorised. Nothing in it comes from
    the model's arguments except ``task`` and the *names* of the repository and
    operation, and both of those are resolved against a closed table before they
    are stored.
    """

    request_id: str
    actor_id: int
    chat_id: int
    repository: str
    repo_path: str
    task: str
    operation: str
    reply_mode: str = "text"
    danger: str = ""
    status: str = "queued"

    @property
    def is_dangerous(self) -> bool:
        return bool(self.danger)

    def as_row(self) -> dict:
        return {
            "request_id": self.request_id,
            "actor_id": self.actor_id,
            "chat_id": self.chat_id,
            "repository": self.repository,
            "repo_path": self.repo_path,
            "task": self.task,
            "operation": self.operation,
            "reply_mode": self.reply_mode,
            "danger": self.danger,
            "status": self.status,
        }


class Rejected(Exception):
    """A request the bridge will not build. Carries a sentence for the owner."""

    def __init__(self, reason: str, message: str):
        super().__init__(reason)
        self.reason = reason
        self.message = message


def build_request(
    *,
    actor_id: int,
    chat_id: int,
    repository: str,
    task: str,
    requested_operation: str = "",
    reply_mode: str = "",
    now: float = 0.0,
) -> AgentRequest:
    """Validate a request and return the envelope, or raise ``Rejected``.

    Validation is here rather than in the runner because this is where the
    actor's identity is known and where the allowlist lives; the runner
    re-validates the parts that are its own safety boundary (the path, the
    operation) and never the actor.
    """
    name = parse_repository(repository)
    if not name:
        known = ", ".join(repository_names())
        raise Rejected(
            "unknown_repository",
            f"مخزن «{repository or '?'}» در فهرست مجاز نیست. مجازها: {known}",
        )
    path = repository_path(name)
    if not path:
        raise Rejected("unknown_repository", "آن مخزن مسیری ندارد.")

    body = (task or "").strip()
    if not body:
        raise Rejected("empty_task", "متن کار خالی است.")
    body = body[: int(db.AGENT_TASK_MAX_CHARS)]

    operation = parse_operation(requested_operation, body)
    if not operation:
        raise Rejected(
            "unknown_operation",
            f"عملیات «{requested_operation}» شناخته نشد. "
            f"مجازها: {', '.join(sorted(OPERATIONS))}",
        )

    mode = (reply_mode or "").strip().lower()
    if mode not in ("text", "document", "both"):
        mode = "text"

    danger = danger_for(operation, body)
    return AgentRequest(
        request_id=new_request_id(actor_id, name, body, now=now),
        actor_id=int(actor_id),
        chat_id=int(chat_id),
        repository=name,
        repo_path=path,
        task=body,
        operation=operation,
        reply_mode=mode,
        danger=danger,
        # A dangerous request is *created*, not run: it waits for the owner.
        status="waiting_for_owner" if danger else "queued",
    )


def scope_check(request: AgentRequest, *, active: list[dict] | None = None) -> str:
    """Why this request cannot start now, or ``""``.

    Three bounds, all about concurrency rather than about meaning: the same
    request twice, too many tasks at once, and two tasks on one repository. The
    last is the one that matters — two agents editing one working tree produce a
    state neither of them can describe.
    """
    rows = list(active or [])
    for row in rows:
        if row.get("request_id") == request.request_id:
            return "duplicate"
    if len(rows) >= max(1, int(config.AGENT_MAX_ACTIVE)):
        return "busy"
    same = [r for r in rows if r.get("repository") == request.repository]
    if len(same) >= max(1, int(config.AGENT_MAX_PER_REPOSITORY)):
        return "repository_busy"
    return ""


# ── The prompt ────────────────────────────────────────────────────────────
# What the agent is told. Three properties are load-bearing and are asserted:
#
#   * it is told the directory it may work in, and that it may not work
#     anywhere else;
#   * it is told never to print a credential, because its output is relayed to
#     a Telegram chat and into a log;
#   * it is told the exact shape of a question back to the owner, because that
#     marker is what the runner turns into ``waiting_for_owner`` — without it a
#     question would be indistinguishable from a finding.
QUESTION_MARKER = "QUESTION:"

PROMPT_TEMPLATE = (
    "You are a coding agent working on the repository `{repository}` at "
    "`{path}`.\n"
    "\n"
    "The task, as the owner of the system stated it:\n"
    "---\n"
    "{task}\n"
    "---\n"
    "\n"
    "Rules:\n"
    "* Work only inside `{path}`. Do not read or write anything outside it.\n"
    "* Read the repository's own AGENTS.md / AgentMD.md before changing "
    "anything, and follow it.\n"
    "* Make the change, then run the tests that cover it. A claim that the "
    "tests pass must be a test run you actually did.\n"
    "* Do not deploy, do not restart a service, do not run a migration, and do "
    "not push to a remote unless the task above asks for exactly that and says "
    "it is approved.\n"
    "* **Never print a credential.** No API keys, no bot tokens, no passwords, "
    "no SSH material, no values out of a `.env` file — not in your output, not "
    "in a file you write, and not in a command you echo. Your output is relayed "
    "to a chat and to a log. Refer to such a value as `<redacted>`.\n"
    "* Be concise. Progress should be short lines, not an essay.\n"
    "* If you need a decision from the owner before you can continue, write a "
    "line beginning with `{marker}` followed by one short question, and stop. "
    "Do not guess.\n"
    "\n"
    "Finish with a short summary: what you changed, what you ran, and what the "
    "result was.\n"
)


def build_prompt(request: AgentRequest) -> str:
    """The prompt for one task. Bounded, and never carries a secret."""
    return PROMPT_TEMPLATE.format(
        repository=request.repository,
        path=request.repo_path,
        task=request.task,
        marker=QUESTION_MARKER,
    )


# ── Reading the agent's stream ────────────────────────────────────────────
_QUESTION_RE = re.compile(r"^\s*" + re.escape(QUESTION_MARKER) + r"\s*(.+)$",
                           re.MULTILINE | re.IGNORECASE)

# The shapes a credential takes in an output stream. This is a *redactor*, not
# a detector: it is applied to everything on its way to Telegram and to the log,
# because the agent was told not to print a secret and a rule that is only
# enforced by having asked politely is not enforced.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),                 # bot token
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                    # google key
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                     # openai-ish
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),                 # openrouter
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                # github token
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)
REDACTED = "<redacted>"


def redact(text: str) -> str:
    """Remove credential-shaped substrings. Never raises."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def question_in(text: str) -> str:
    """The first question the agent asked the owner, or ``""``."""
    match = _QUESTION_RE.search(text or "")
    return (match.group(1).strip()[:400] if match else "")


def summarise(text: str, limit: int = 0) -> str:
    """The tail of the agent's output, which is where its summary is.

    The head of a transcript is setup and the tail is the conclusion, and a
    Telegram message has room for one of them.
    """
    body = (text or "").strip()
    cap = int(limit or db.AGENT_RESULT_MAX_CHARS)
    if len(body) <= cap:
        return body
    return "…" + body[-cap:]


# ── Transport for a long answer ───────────────────────────────────────────
def chunk_text(text: str, *, limit: int = 0) -> list[str]:
    """Split a long answer into ordered, sendable pieces.

    Ordered and lossless: joining the pieces returns the input. Splitting on a
    line boundary where one is available, because a message cut mid-sentence
    reads as a corrupted message, and never on a byte count — Telegram's limit
    is characters.
    """
    body = text or ""
    cap = max(200, int(limit or config.AGENT_CHUNK_CHARS))
    if len(body) <= cap:
        return [body] if body else []
    pieces: list[str] = []
    remaining = body
    while len(remaining) > cap:
        window = remaining[:cap]
        cut = window.rfind("\n")
        if cut < cap // 2:
            # No usable line break; cut on a space, and failing that at the
            # limit. A hard cut is worse than a soft one but far better than
            # dropping the text.
            cut = window.rfind(" ")
        if cut < cap // 2:
            cut = cap
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        pieces.append(remaining)
    return pieces


def needs_document(text: str, *, limit: int = 0) -> bool:
    """Whether the answer is long enough that a file is kinder than a wall of chat."""
    return len(text or "") > max(200, int(limit or config.AGENT_DOCUMENT_CHARS))


def reply_plan(text: str, reply_mode: str = "text") -> dict:
    """How to deliver one answer: chunks, a document, or both.

    The brief's rule is that a long response is chunked in order, or sent as a
    document, or both — and is never silently discarded. This returns the plan
    rather than performing it, so the decision is testable without Telegram, and
    so the caller cannot accidentally implement a fourth option (dropping it).
    """
    body = (text or "").strip()
    if not body:
        return {"chunks": [], "document": False, "mode": "empty"}
    mode = (reply_mode or "text").strip().lower()
    if mode not in ("text", "document", "both"):
        mode = "text"
    long = needs_document(body)
    if mode == "document" or (mode == "both" and long):
        return {"chunks": [], "document": True, "mode": mode}
    if mode == "both":
        # "both" and short: a document would be silly, so the chat copy is the
        # answer. Nothing is lost — the whole text is in the chunks.
        return {"chunks": chunk_text(body), "document": False, "mode": mode}
    return {"chunks": chunk_text(body), "document": False, "mode": mode}


# ── Isolation from the awareness allowance ────────────────────────────────
def allowance_account() -> str:
    """Which budget a coding task spends. Its own, and never the assistant's.

    The brief is explicit that the bridge must not consume the Awareness daily
    allowance. The mechanism is that the agent is a *host process* authenticated
    by the owner's own CodeBuddy credential: no Gemini key of this deployment is
    used, no ``gemini_pool`` account is touched, and no ``gemini_daily`` counter
    moves. This function exists so the property has a name that a test can
    assert, and so a future implementation that *did* route the agent through
    the pool would have to change it deliberately.
    """
    return "agent"


def status_lines(*, actor_id: int = 0, limit: int = 5) -> list[str]:
    """The owner's view of the bridge. No task bodies, no results."""
    active = db.agent_task_active(actor_id=actor_id)
    lines = [f"وظایف فعال: {len(active)}"]
    for row in active[:limit]:
        lines.append(
            f"- {row['request_id']} | {row['repository']} | "
            f"{status_label(row['status'])} | {row['operation']}"
        )
    recent = db.agent_task_recent(limit=limit, actor_id=actor_id)
    done = [r for r in recent if r["status"] not in db.AGENT_ACTIVE_STATUSES]
    if done:
        lines.append("آخرین‌ها:")
        for row in done[:limit]:
            lines.append(
                f"- {row['request_id']} | {row['repository']} | "
                f"{status_label(row['status'])}"
            )
    return lines
