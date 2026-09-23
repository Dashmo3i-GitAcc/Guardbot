# The coding-agent bridge

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is verbatim; it was not edited during the move.

## Contents

- [39. The coding-agent bridge: Telegram → Nexus → CodeBuddy → Telegram](#s39)

---

<a id="s39"></a>

## 39. The coding-agent bridge: Telegram → Nexus → CodeBuddy → Telegram

### 39.1 What was asked for, and the one thing that shaped the design

The request was that the owner be able to talk to Nexus in the group in natural
language and have it hand real coding work to a coding agent, with the result
coming back to the same conversation. That is four separate problems wearing one
name: recognising the request, authorising it, executing it somewhere with a
shell, and carrying a long answer back through a chat.

The design was decided by one measurement, taken before anything was written:

```
$ docker exec guardbot which node codebuddy
NO_NODE
NO_CODEBUDDY
$ docker inspect guardbot --format '{{json .Mounts}}'
[{"Source":"/root/guardbot/data","Destination":"/data",...}]
```

The container ships `app/` and `requirements.txt` and nothing else. There is no
Node, no CodeBuddy CLI, and no package manager to install one with. So the
execution half cannot be in the container, and the bridge is **two processes
that meet over a directory**: the container owns the decision and the database,
the host owns the shell and the repository.

### 39.2 The bridge is an operation, not a front door

The single most important structural decision: `codebuddy_task` is a row in
`admin_service.OPERATIONS`, with `kind=OP_SYSTEM` and `permission="agent.request"`.
It is reached exactly the way `ban_member` is reached — the model calls a
declared tool, `admin_tools.parse_write_call` turns it into a typed
`AdminRequest`, and `admin_service.execute` authorises it against the actor's
real Telegram id, checks the replay window, checks the idempotency table, writes
an audit row, and only then calls `_apply`.

Nothing about that pipeline was forked. A coding request gets the same audit
trail, the same idempotency, the same authority model and the same refusal
vocabulary as a ban. The alternative — a second handler that "just" forwards a
message — would have been a second answer to "who may do this", which is the
thing §34 exists to prevent.

### 39.3 The model may ask; it may not decide

`agent.request` is held by **no role bundle**, exactly like `nexus.control`. An
administrator who is promoted to every role still does not hold it, and
`rbac.authorize_grant` cannot express it, so the promotion dialog cannot hand it
out either. That is what makes "only the owner may ask for a coding task" a
property of the tables rather than a check somebody has to remember.

What the model supplies, and what it cannot:

| the model supplies | the server derives |
|---|---|
| the repository **name** | the path, from the allowlist |
| the task, in the owner's words | the actor id, from the Telegram update |
| a *claim* about the kind of work | whether that kind is dangerous |
| a reply-mode preference | whether the actor may do any of it |
| — | whether deploy is allowed |
| — | whether the request is approved |

`AdminRequest` has no field for `is_owner`, `is_admin`, `approved` or `allowed`,
and `parse_write_call` refuses any argument the tool schema does not declare, so
a smuggled `owner=true` is dropped before it can reach anything. Both facts are
tested.

### 39.4 The repository allowlist is an indirection, not a filter

`agent_bridge.DEFAULT_REPOSITORIES` maps a logical name to one directory. A
request carries the *name*; `repository_path(name)` produces the path. A path is
accepted as input only when it is exactly an allowlisted root, and it is then
converted back to the name — so `repo_path` in a stored row is always the output
of that lookup and never a string a model produced.

The consequence is worth stating plainly: there is no expression the model can
write that becomes a directory this bot will hand to a process with a shell.
`tests/test_agent_bridge.py` asserts that over the function's *output* rather
than over a list of suspicious inputs.

The host runner checks the same list again, from its own literal copy, and
refuses a request whose `repo_path` is not what the name means *there*. Two
independent checks, because they are two: a single shared source would make a
mistake in it a mistake in both.

### 39.5 The operation vocabulary, and the danger classifier

Ten operations, and the list is closed — an operation outside it is refused,
because an open vocabulary means the danger table can be bypassed by inventing a
word. Five are dangerous:

| operation | dangerous | why |
|---|---|---|
| `analyse`, `test`, `edit`, `commit`, `push` | no | recoverable; a working tree is a git repository's purpose |
| `deploy` | yes | it changes what is *serving* |
| `migrate` | yes | a migration can destroy data |
| `delete` | yes | deletes files or branches |
| `reset` | yes | resets a repository or a service |
| `credentials` | yes | changes keys or secrets |

The split is deliberately not "read vs write". `edit` writes files and is not
dangerous; `deploy` writes nothing and is.

The classifier takes the structured operation as the primary signal and scans
the task text as well, and **the text can only add danger, never remove it**.
The asymmetry is the point: a false positive costs one confirmation, a false
negative costs an unconfirmed production change.

### 39.6 A dangerous request is recorded and not published

This is the central safety property, and it is enforced by the *absence* of a
file. A dangerous request is written to `agent_tasks` with
`status='waiting_for_owner'`, and the request file is **not** written to the
spool. The runner's only source of work is that directory, so a task the owner
has not approved is not merely refused by the runner — it is invisible to it.

Belt and braces: `waiting_for_owner` has exactly one legal exit, and it is
`queued`. `running` is not reachable from it, so even a forged `started` line
could not move an unapproved task into execution. Both are tested.

### 39.7 Approval is the owner's, server-side, and unambiguous

The brief's rule about vague language is implemented in
`agent_bridge.resolve_confirmation`, which is pure and takes the waiting list as
an argument:

* **only the owner**, checked by id and never by anything the model said;
* **there must be something waiting** — «اوکی» with nothing pending is not an
  approval of anything;
* **a named task must actually be waiting**, so a model that names one is making
  a reference and not a decision;
* **a bare confirmation resolves only when exactly one task is waiting**; with
  two, the answer is a question listing both ids, and the model is instructed to
  ask which.

`confirm` is reachable two ways and both go through the same function: the
`confirm_agent_task` tool, and `/agent confirm <id>`. The typed command is the
fallback for the situation the bridge exists in — the owner wants to know
whether his request went anywhere and asking the assistant is the thing that is
broken.

Two meanings share `waiting_for_owner`, and they are told apart by `started_at`:

* `started_at = 0` — dangerous and never approved. `db.agent_task_waiting`
  returns these, and `confirm` releases them.
* `started_at > 0` — it ran and stopped to ask a question. These are **not** in
  the waiting list, because confirming one would re-run work that was already
  under way. They are answered with `answer_agent_task` instead.

### 39.8 Answering a question is not a backdoor approval

`agent_service.resume` appends the owner's answer to the task and requeues it —
and it **recomputes the danger** over the enriched text. An answer that turns an
ordinary task into "yes, and then deploy it" is caught: the task goes back to
`waiting_for_owner` rather than inheriting an approval the original question did
not carry. A task that never started cannot be resumed at all, because an answer
to a question it never asked is not an approval of it.

### 39.9 The two halves meet over a directory

`app/agent_spool.py` defines the wire, and it imports nothing but the standard
library — which is what makes it safe for the host runner to import. That is not
tidiness: `app/db.py` writes at import time, and a second writer would produce
`database is locked` under exactly the load a coding task creates.

```
<spool>/requests/<id>.json     container writes, runner reads
<spool>/streams/<id>.jsonl     runner appends, container reads
<spool>/locks/<id>.lock        O_CREAT|O_EXCL, held while a run is in flight
<spool>/control/<id>.cancel    container asks a run to stop
```

The stream is JSON Lines and **append-only**, and that is the whole of the
restart story. The container records how many lines it has delivered in
`agent_tasks.progress_offset`; on restart it reads from that line onward. A line
is written with one `write` and flushed, so the only way to see a partial line
is a crash mid-write, and the reader treats a trailing line with no terminating
newline as not-yet-written. Nothing is ever rewritten in place, so a reader can
never observe a torn file.

Why not a socket: a port would need a listener, a firewall decision and a shared
secret, and the runner would have to be trusted to enforce all three. A
directory needs none of them, and its permissions are the filesystem's.

### 39.10 The host runner, and what it refuses

`tools/agent_runner.py` claims a request by creating a lock with `O_CREAT|O_EXCL`
— atomic everywhere, no lock manager — and then re-validates the parts that are
its own safety boundary: the repository name is on its own copy of the
allowlist, the path is what that name means, the directory exists, the operation
is in the vocabulary, and the task is non-empty. A request that fails any of
those is marked failed and never executed.

It does **not** take the executable or its arguments from the request. Which
binary runs is the host's decision, set in the runner's own environment where
the owner can see it; a container that could name an executable could name one
that is not a coding agent. It also strips this session's `CODEBUDDY_*`
identity variables from the child's environment. It does **not** give each run
its own `HOME`: the authentication lives in `$HOME/.codebuddy`, and a child
given a fresh `HOME` does not fail — it *succeeds*, with an "Authentication
required" answer, which is classified as a failure rather than relayed
(§39.15). A fresh `HOME` per request was the original design and it is wrong
here. `AGENT_RUNNER_HOME` still overrides the real `HOME` for a deployment that
keeps its own profile.

The timeout is enforced by a watchdog thread rather than by the read loop,
because the read loop is exactly what a hung child stops doing. A CLI that
starts, prints nothing and never exits is the failure this was written after.

### 39.11 Carrying a long answer back

`agent_bridge.reply_plan` decides between ordered chunks, a document, or both,
and returns a plan rather than performing it — so the decision is testable
without Telegram, and so the caller cannot accidentally implement a fourth
option. There is deliberately no branch that drops the answer, and
`tests/test_agent_bridge.py` asserts that as a property over every mode and
every length.

Delivery is ordered and once-only. The offset is written *after* the lines are
sent, so a crash mid-delivery repeats at most the messages in flight, while the
opposite ordering would lose them — and repeating a progress line is a smaller
fault than losing an answer. Progress is throttled by count and by interval,
because a chatty agent must not become a chatty bot; the result is never
throttled.

A running task narrates itself in **one message, edited in place**, rather than
one message per progress line. The header sent when the task starts is that
message; each progress line rewrites it with a count, how long the task has been
running, and the newest line. The throttle above therefore limits *edits* rather
than messages. The answer at the end is always a new message — progress is
overwritten, never the answer — and when the task ends the message is forgotten
so a late line cannot rewrite it. Two failures are handled rather than raised: a
missing id (a restart dropped it, or the feature is off) and a refused edit (the
message was deleted, or is older than the edit window). Either falls back to
sending a new message, because silence is worse than a second message.
`AGENT_WORKING_MESSAGE=0` restores a message per progress line.

### 39.12 Secrets

The brief is explicit that no API key, bot token, Gemini key, DeepSeek key, SSH
credential or server password may appear in Telegram, in a log, in a commit, in
a test or in a document. Three things enforce it:

* the agent is **told**, in its prompt, never to print one, and to write
  `<redacted>` instead;
* the runner **redacts** everything it emits — a rule enforced only by having
  asked politely is not enforced;
* the container redacts again on the way to Telegram and before storing, so the
  stored result is clean as well as the delivered message.

`agent_bridge.redact` matches bot tokens, Google keys, OpenAI-style and
OpenRouter keys, GitHub tokens, `key = value` assignments for the usual names,
and PEM private-key blocks. The bridge's own tests and this document contain no
credential, and neither does the audit row.

### 39.13 Isolation from the awareness allowance

A coding task must not consume the assistant's daily allowance. The mechanism is
that the bridge never reaches the Gemini pool at all: the agent is a host
process authenticated by the owner's own CodeBuddy credential, and no Gemini key
of this deployment is used, no pool account is touched and no counter moves.

`agent_bridge.allowance_account()` returns `"agent"` so the property has a name
a test can assert — and the test checks the import graph as well as the
behaviour, so a future change that routed the agent through the pool would have
to do it deliberately.

### 39.14 The lifecycle, and what a restart does

`queued → running → succeeded | failed | cancelled | timed_out`, with
`waiting_for_owner` reachable from `running` (the agent asked a question) and
exiting only to `queued` (approved or answered). Terminal states are terminal.

Every task has a `request_id`, an actor id, a repository, `created_at`, a
status, a result or an error, and an optional CodeBuddy session id. No task body
is stored beyond what the owner wrote, and the status report shows ids,
repositories and states — never a task body, which is tested.

A restart is handled in `agent_poller.recover` and `agent_service.recover`:

* a task recorded as `queued` with no request file — the process died between
  the two writes — is republished, so it is not stranded;
* a task whose stream already ended while the bot was down is walked to its
  terminal state through `agent_bridge.path_to`, because a stream that ends with
  a result while the row still says `queued` is evidence that it ran;
* a lock left by a killed runner is cleared for a task that is no longer active,
  which is what lets it be retried;
* a running task is never republished, so a restart cannot duplicate execution.

### 39.15 How it runs the agent, and the deployment

The bridge is complete on both sides, and the runner drives the CLI through the
mechanism that actually works on this host — measured, not assumed.

The foreground invocation does **not** work here:

```
$ codebuddy -p "Reply with exactly: READY"
(zero output, never exits)
```

Six shapes were tried — fresh `HOME`, the real `HOME`, `-y`,
`--permission-mode dontAsk`, `--permission-mode acceptEdits`, and stdin closed —
and every one of them started, printed nothing and never returned. So it is not
a permission prompt, not stdin, and not the environment.

`--bg` works. It hands the session to the CodeBuddy job broker, which is where
the authentication lives, and returns in about a second:

```
$ codebuddy --bg --name gb-probe -p "Reply with exactly: READY"
backgrounded · gb-runne · gb-probe
```

The outcome is not on stdout. It lands in
`$HOME/.codebuddy/jobs/<shortId>/state.json`, moving from
`state=working, tempo=active` to `state=done, tempo=idle`, with the answer in
`output["result"]`. Two properties of that file are load-bearing:

* `shortId` is the **first eight characters of the name**, so it is neither
  unique nor predictable — two launches can share one directory. The runner
  therefore finds its job by the `sessionId` it chose itself, never by the name.
* The authentication is in `$HOME/.codebuddy`. A child given a fresh `HOME` does
  not fail: it **succeeds**, with `Authentication required. Please use /login
  command to sign in` as its text, which the container would otherwise store as
  the agent's answer. That is why the runner no longer gives each request its own
  `HOME`, and why that sentence is classified as a failure rather than relayed.

Two further surfaces exist and are deliberately unused: `--serve --port N` serves
a REST API (it needs a printed password and an `x-codebuddy-request` header), and
`--acp` answers an `initialize` handshake over stdio but never returns from
`session/new`.

**There is no credential to hand over.** The authentication is the host's own
CodeBuddy profile, so the deployment step is only to start the runner.

The full sequence:

1. `git pull` in `/root/guardbot`, and confirm the commit SHA.
2. Confirm the host has Node and the CodeBuddy CLI: `which node codebuddy`.
3. Confirm the CLI is authenticated **in the environment the runner will use**:
   `codebuddy --bg --name probe -p "Reply with exactly: READY"`, then read
   `~/.codebuddy/jobs/*/state.json` and check `output["result"]` says `READY`.
4. Leave `AGENT_CLI` as `codebuddy` unless the binary is elsewhere. `AGENT_CLI_ARGS`
   is *extra* flags only — the runner supplies `--bg`, `--name`, `--session-id`
   and `-p` itself, and an argument list that repeats them is a misconfiguration.
5. Set `AGENT_REPOSITORIES` to the real allowlist — the same string on both
   sides, container and runner.
6. Confirm `AGENT_SPOOL_DIR` is inside the bind mount: `/data/agent` in the
   container is `/root/guardbot/data/agent` on the host.
7. `mkdir -p /root/guardbot/data/agent` and check it is writable by both.
8. Add the `AGENT_*` block to `/root/guardbot/.env` (see `.env.example`).
9. `docker compose up -d --build` and confirm the container is healthy.
10. Read the startup log for the `Coding agent:` line and the repository list.
11. Run the runner once by hand: `python tools/agent_runner.py --once`.
12. In the group, ask the owner's account for something harmless:
    «توی guardbot یه تست ساده اضافه کن».
13. Confirm Nexus called `codebuddy_task` and the reply names a task id.
14. Confirm the task appears in `/agent`.
15. Confirm progress lines arrive in the same chat.
16. Confirm the final answer arrives — chunked, or as a document.
17. Ask for something dangerous: «آخرین تغییرات رو دیپلوی کن».
18. Confirm it is recorded as waiting and **nothing ran**.
19. Confirm «اوکی» releases it, and that with two waiting tasks it asks which.
20. Restart the container mid-task and confirm nothing runs twice.
21. Run the suites: `pytest` in `/root/guardbot`, and the VPN Bot suite in
    `/opt/vpn-bot`.

### 39.16 Tests

| file | tests | what it covers |
|---|---|---|
| `tests/test_agent_bridge.py` | 66 | the allowlist, the operation vocabulary, the danger classifier, confirmation, the lifecycle, idempotent ids, concurrency bounds, the prompt, redaction, chunking, the reply plan, and the import-graph isolation from the awareness allowance |
| `tests/test_agent_service.py` | 61 | submit, the member and administrator refusals, the dangerous path, approval and ambiguity, answering a question, cancelling, the status report, recovery, and the same path through `admin_service.execute` and `parse_write_call` |
| `tests/test_agent_transport.py` | 59 | the spool's atomicity and offsets, partial-line handling, delivery order, once-only delivery across a process, chunking and documents, redaction on the wire, the timeout, recovery, and the runner's own validation, argv and stream parsing |

### 39.17 Configuration

| variable | default | what it does |
|---|---|---|
| `AGENT_ENABLED` | `true` | the master switch |
| `AGENT_REPOSITORIES` | `guardbot=/root/guardbot,vpn-bot=/opt/vpn-bot` | the allowlist, `name=path` |
| `AGENT_MAX_ACTIVE` | `2` | tasks in flight at once |
| `AGENT_MAX_PER_REPOSITORY` | `1` | tasks on one repository |
| `AGENT_SPOOL_DIR` | `/data/agent` | where the two halves meet |
| `AGENT_POLL_SECONDS` | `3.0` | how often the container looks for news |
| `AGENT_TIMEOUT_SECONDS` | `1800` | the run's bound, on both sides |
| `AGENT_MAX_TURNS` | `40` | the agent's turn ceiling |
| `AGENT_CLI` | `codebuddy` | the executable — **the runner's environment** |
| `AGENT_CLI_ARGS` | `-p,--output-format,stream-json,--model,deepseek-v4.1-flash,--permission-mode,acceptEdits,--no-session-persistence` | its arguments — **the runner's environment** |
| `AGENT_ADD_DIR` | `1` | pass `--add-dir <repo>`; `0` for a CLI without the flag |
| `AGENT_RUNNER_HOME` | `/run/guardbot-agent` | where each run's isolated `HOME` goes |
| `AGENT_CHUNK_CHARS` | `3500` | how long a chunk may be |
| `AGENT_DOCUMENT_CHARS` | `3500` | when a file is kinder than chat |
| `AGENT_PROGRESS_MAX_CHARS` | `600` | how long a progress line may be |
| `AGENT_PROGRESS_MIN_INTERVAL_SECONDS` | `10` | the throttle |
| `AGENT_PROGRESS_MAX_MESSAGES` | `20` | the progress ceiling (edits, when the working message is on) |
| `AGENT_WORKING_MESSAGE` | `1` | narrate in one edited message; `0` for a message per line |
| `AGENT_RETENTION_SECONDS` | `1209600` | how long a finished task is kept |

The `AGENT_*_TEXT` and `AGENT_*_HEADER` variables are the Persian copy for each
outcome, in the same place as every other outcome's sentence and reached through
the same `admin_service.message_for` table — so the assistant and the typed
commands cannot describe the same state two ways.

`agent_tasks` is created with `CREATE TABLE IF NOT EXISTS`, so there is no
migration step and an existing database picks it up on restart.
