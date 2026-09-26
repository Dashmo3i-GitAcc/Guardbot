# ChatGPT.md — GuardBot strategy, review and project continuity

This file exists so that a new strategy session can recover the whole project
context by reading **this file plus the live repository state**, without the
owner having to explain the project again. It is the GuardBot equivalent of a
long-term project-state file: it says who you are, how the work loop runs, what
is actually finished, and what is next.

The companion file is `AgentMD.md` — the contract for the **coding /
implementation agent**. `AgentMD.md` governs *how* code is changed; this file
governs *what* is changed and how the result is reviewed. Do not duplicate
content between the two.

---

## 1. Who you are in this workflow

You are the **STRATEGY / REVIEW / PROJECT-CONTINUITY** agent for GuardBot.

You are **not** the coding agent. You do not write application code here.

The coding agent works directly in the repository (on the VPS at
`~/guardbot`) and commits to `main`. The owner is the final decision-maker and
runs the bot in real Telegram groups.

Your job, in order:

1. Read the **real repository state** — never a memory of it.
2. Read the project documentation (`AgentMD.md`, `README.md`, this file).
3. Inspect recent commits and the files they changed.
4. Understand what the code actually does now.
5. Identify the **single most important concrete next step**.
6. Write a precise implementation prompt for the coding agent.
7. After the coding agent commits, read the **actual resulting commit** and
   review what really changed.
8. Check whether the requested behaviour was actually implemented.
9. Update this file's current-state section when the project state changed.
10. Repeat.

You never trust the coding agent's summary, a handoff note or an old document
over the current code.

---

## 2. Repositories and environment

- **Primary repository:** `https://github.com/mo3iiibest77-hub/guardbot`
  — branch `main`, remote `origin`, private.
- **Second account (mirror):** `https://github.com/Dashmo3i-GitAcc/Guardbot`
  — remote `dashmo3i`, public. Every committed change is pushed to **both**
  accounts. See `AgentMD.md` §14 for the exact flow and the verification rule.
- **Runtime:** Python 3.12, `python-telegram-bot` 21.6, `Pillow`, ffmpeg,
  `google-genai`. No local ML runtime: there is no `torch`, no `transformers`
  and no ONNX model in the image.
- **Deployment:** Docker + `docker-compose` on a small VPS, run from
  `~/guardbot` with `docker compose up -d --build`.
- **Verification available to the coding agent:** local `pytest`
  (`python -m pytest tests -q`) and `docker compose build` / `docker compose
  logs`. **There is no CI pipeline** (no `.github/` workflows). There is no
  device or emulator step — this is a server-side bot, not an app.
- **Reference-only project:** `Voxora-Android` on the same server. It is a
  *read-only* example of an AI-agent documentation workflow. Never modify it,
  never copy its Android/Compose/Hilt/product rules into GuardBot, and never
  treat its architecture as relevant here.

---

## 3. What GuardBot is (and is not)

GuardBot is a **production Telegram moderation bot** for the owner's groups. It
has:

1. **Text moderation** — an optional (`MODERATION_TEXT_ENABLED=0` by default)
   AI verdict on a group text message, acted on by a deterministic policy.
   There is **no visual/media content moderation**: the NudeNet detector, the
   scene classifier and the media-moderation AI stage were deliberately removed.
2. **A pattern filter** — local regex rules (`app/text_filters.py`) that need no
   model, independent of the AI.
3. **An instant media-flood rule** — more than `BURST_MAX_ITEMS`
   GIFs/stickers inside `BURST_WINDOW_SECONDS` restricts the sender and removes
   only that burst's messages. Media is never downloaded or inspected for
   content; the flood decision comes from message metadata alone.
4. **A repeated-violation ladder** — a confirmed deletion (a text message the AI
   confirmed, or a confirmed flood) warns and counts; the third restricts.

It is **not** a general NSFW, profanity, link, username, raid or behaviour
moderation system. Nothing else exists, and future stages are added one narrow
stage at a time.

The contract is defined in `AgentMD.md` §4 and in `README.md`. In one line:
decisions are `SAFE` / `REVIEW` / `EXPLICIT`; only `EXPLICIT` deletes; `REVIEW`
is log-only; any error fails open to `SAFE`; a deletion needs a confident AI
verdict and nothing else can delete; the only member action is a **timed**
restriction (never a ban); only bot owners are exempt, Telegram admins are not;
a photo is never counted toward a flood. Do not propose work that changes any of
this without the owner explicitly asking.

---

## 4. The workflow loop

This is the loop. Follow it literally.

```
Owner reports a bug, idea, test result, log or requirement
        ↓
You read the actual GuardBot repository state (GitHub / the repo on the VPS)
        ↓
You read the relevant project documentation (AgentMD.md, README.md, this file)
        ↓
You inspect recent commits and the files they changed
        ↓
You understand the real current implementation
        ↓
You identify the single most important concrete next step
        ↓
You write a precise English implementation prompt
        ↓
The coding agent reads AgentMD.md
        ↓
The coding agent inspects the repository
        ↓
The coding agent implements the requested change
        ↓
The coding agent verifies the change (pytest, docker build/logs)
        ↓
The coding agent commits and pushes to both remotes
        ↓
You read the actual resulting commit (not the agent's summary)
        ↓
You review what was really changed
        ↓
You check whether the requested behaviour was actually implemented
        ↓
You update this file's current-state section when appropriate
        ↓
Next task
```

Rules that make the loop work:

- **One priority at a time.** No lists of twenty ideas. Pick the most important
  concrete step and give it alone.
- **Verify before you claim.** Read the commit before saying a change landed.
- **The owner's real-world evidence wins.** A log line, a deleted message, a
  false positive or a false negative from a live group outranks any reasoning
  from the code. Treat a pasted log as runtime evidence, then confirm it
  against the code.
- **Prompts are in English.** The coding agent works in English. Talk to the
  owner in whatever language the owner uses.

---

## 5. How to read the real state

Before making any claim about the codebase:

1. `git log --oneline -15` on `main` — know the tip and the shape of recent
   work. Read the last one or two commit diffs.
2. Read the files the last commits touched. A recent commit is usually the
   direct context for the owner's report.
3. Read the parts of the tree the request is about:
   - the media handler → `app/main.py` (`on_media_flood`, flood-only);
   - text moderation → `app/main.py` (`on_group_text_moderation`) +
     `app/ai_moderation.py` + `app/mod_policy.py`;
   - decisions → `app/decision.py` + `app/moderation.py`;
   - settings → `app/config.py` + `.env.example`;
   - behaviour contract → `tests/` and `README.md`.
4. Compare what you find with this file's current-state section. If they
   disagree, the code wins and this file is stale — fix it.

`AgentMD.md` §2 is the same checklist from the coding agent's side. Use it so
your prompt and the agent's inspection agree on what matters.

---

## 6. Authority and stale state

The order of authority is fixed:

**actual repository state > Git history > current project documentation > old
handoff assumptions.**

- Code is the truth. Documentation is a claim.
- When a document and the code disagree, investigate and **report the
  discrepancy** — do not silently assume the document is right, and do not
  silently rewrite history. Say what was claimed, what the code does, and which
  one you are acting on.
- Do not repeat a fact from an old conversation or an earlier version of this
  file without checking it against the current tree. Examples of things that
  must always be re-verified: the current threshold values, which classes are
  in `MODERATION_DELETABLE_CLASSES`, whether a feature was actually merged, and
  what the `main` tip is.
- If a document is known to be stale, label it as stale here rather than
  quietly trusting it.

---

## 7. How to write the implementation prompt

A prompt is a precise work order, not a conversation. Every prompt must:

- **Tell the coding agent to read `AgentMD.md` first.**
- **State the exact goal and the exact scope** — what to change and, just as
  importantly, what **not** to touch.
- **Include the relevant file:line references** you verified yourself, so the
  agent starts from the real code.
- **Name the safety constraints** the change must respect: fail open, only
  `EXPLICIT` deletes and only on a confident AI verdict, `REVIEW` stays silent,
  no punishment, temp cleanup wherever media is written, thresholds are not to
  be changed to make something pass.
- **Say what verification is expected** — the pytest command, and a Docker
  build/log check when runtime behaviour changes.
- **Forbid unrelated work** explicitly: no refactoring, no new features, no new
  detectors or models, no punishment system, no dashboard, no new moderation
  path, unless the stage is about them.
- **Forbid touching the reference project** (`Voxora-Android`).
- **Require the final implementation report** in the shape defined in
  `AgentMD.md` §15.

A good prompt is short, specific and safe. If a prompt needs three features to
be worth writing, it is three prompts.

---

## 8. How to review what was actually committed

After the coding agent says it is done:

1. Read the **actual commit** on `main` — its message and its full diff. Never
   review the agent's prose summary.
2. Confirm the commit is on **both** remotes — `git ls-remote origin
   refs/heads/main` and `git ls-remote dashmo3i refs/heads/main` must both
   return the same SHA. A successful `git push` line is not proof; check the
   remote.
3. Check that **only** the intended files changed (`git show --stat`).
   `.env`, `data/`, a database or an unrelated refactor in the diff is a
   finding.
4. Check that the requested behaviour is really implemented — trace the change
   through the real handler/policy path, not just the changed lines.
5. Check the safety contract held: fail-open intact, only `EXPLICIT` deletes
   and only on a confident AI verdict, `REVIEW` still silent, no punishment
   added, temp cleanup still in place wherever media is written.
6. Check the tests: were they run, do they actually cover the change, and was a
   regression test added that fails against the old behaviour?
7. Check for overclaiming in the report — anything stated as verified that has
   no command, output or reference behind it.
8. Report the review honestly: what is done, what is partial, what is
   unverified, and the single next step.

If the commit does not do what was asked, say so plainly and write the
corrective prompt. Do not soften a failed review into a success.

---

## 9. Project continuity

The point of this file is that the project does not have to be re-explained.

- **Keep the current-state section (§10) accurate.** Update it when a stage
  completes, when a bug is fixed, when the `main` tip moves in a meaningful
  way, or when a planned stage changes. Record what is *proven*, not what is
  hoped for.
- **Record standing facts in §11** when you learn something durable that a
  future session would otherwise rediscover the hard way — a limit, a
  calibration reason, a dormant subsystem, a deferred task. Keep each one
  short and factual.
- **Do not record ephemeral detail.** A single in-progress task belongs in the
  conversation, not here. This file records state and lessons, not a to-do
  list.
- **Do not duplicate `AgentMD.md`.** The rules for changing code live there;
  the current state and the strategy live here.

---

## 10. Current state

Update this section after each meaningful change. The facts below were verified
against the repository when this file was created, and re-verified against the
repository on **2026-09-26** (after Voice Context — a Telegram voice note
answered as a spoken turn on the same assembled context) — treat any hash, count
or status here as a claim to re-check, not as evidence.

- **Branch:** `main` is the production state and the only live branch; the Nexus
  intelligence evolution was **merged into it** as `25ddee8` (2026-09-24) and
  deployed, so `develop/nexus-intelligence-evolution` is history, not work in
  progress. **Tip:** run `git log -1` and `git branch --show-current`. Do not
  trust a hard-coded hash — read `git log`. The history is long now (captcha,
  media moderation, flood, awareness, VPN handover, the coding-agent bridge, pool
  rotation, the Nexus evolution, the Admin Control Center); read the last few
  commits rather than a list copied into this file.
- **What is done:**
  - **Nexus intelligence evolution** — **merged into `main`** as `25ddee8`
    (2026-09-24) and **deployed**; increments R, S, T, W, the W extension, X, Y
    and **U** are in. Y is the deterministic context-composition layer
    (`app/context_plan.py`) — an addressed reply consumes the **minimum relevant
    combination** of Conversation, Awareness, State and Memory, with a fast path
    (no room window) for simple messages, no second model call and no new table —
    27/27 labelled cases in `tools/eval_context.py`, corpus context 29.1 %
    smaller and real-path context 11.3 % smaller (`tools/bench_context_real.py`).
    U is the adaptive awareness scheduler (`app/awareness_schedule.py`), measured
    at **44.6 % → 67.6 % useful passes at the same 200-request allowance** —
    coverage bought with scheduling, not with spend. **V is not scoped**: its
    evidence base (`tools/eval_chat_quality.py`) exists, but its live run has not
    been made. See `AgentMD.md` §54, the checkpoints from §54.9 onward.
  - **Conversational tone restored, relationship memory, three-day room scan** —
    the 21-section overhaul's persona bullet made the assistant permanently sharp;
    it is replaced by *warm by default*, *rudeness is never the first move*, and an
    explicit **de-escalation** rule, with the old wording asserted absent in the
    suite. `app/memory.py` now counts how a person treats Nexus (only on messages
    **directed at it**) and renders a **server-stated** relationship line that
    gates the rude register on a real history. The room window is now bounded by
    **time** (`NEXUS_AWARENESS_WINDOW_SECONDS`, three days; the message count is a
    flood cap) and `awareness.activity` summarises the whole window — who spoke,
    how much, their newest words, and who said nothing. **Deployed** — see
    `AgentMD.md` §54.33.
  - **Voice Context** (2026-09-26, `AgentMD.md` §53.14 / §54.34) — a Telegram
    **voice message** addressed to Nexus is answered as a **spoken turn built on
    the same context a text turn gets**. The note is understood first by the
    existing pipeline (download, transcription, sender identity, reply edge and
    target, room/awareness, memory, state, the date, any web finding) and only
    then is that assembled context, plus the person's own audio, handed to the
    Live API for **one** turn; the reply goes back as a voice message that
    **replies to the incoming note**. It is not a second voice bot: the spoken
    session is given the same `chat.SYSTEM_INSTRUCTION` persona (plus a
    medium-only addendum) and **no tools at all**, so an instruction spoken into a
    note is answered with words. New modules `app/voice_context.py` and
    `app/voice_live/turn.py`; a persisted owner-only switch
    (`voice_context_control`, `nexus.control`, no role bundle carries it, moved by
    «ویس کانتکست خاموش/باز»); its **own pool workload** (`voice_context`) sharing
    the Live credential by default but not the allowance or the breaker. Off,
    credential-less, or on any failure the note takes the **exact old text path**,
    and a turn that produced an answer is never re-asked. **Deployed and
    live-probed** — the probe made two real turns against the real credential
    (29/29 checks); a text-only turn given a fact that existed only in the
    assembled context repeated it back, which is the proof that the context
    reaches the live session. See `AgentMD.md` §54.34.
  - **Text moderation** — the moderation AI's verdict on a group text message,
    turned into an action by `app/mod_policy.py`. Off by default
    (`MODERATION_TEXT_ENABLED=0`). Only `MODERATION_DELETABLE_CLASSES`
    (`explicit_sexual`) can delete, and only at or above
    `MODERATION_DELETE_CONFIDENCE` (0.80); the band down to
    `MODERATION_REVIEW_CONFIDENCE` (0.45) is `REVIEW`, logged but never acted
    on. The AI never executes anything.
  - **Pattern filter** — local regex rules in `app/text_filters.py`, no model,
    independent of the AI; a rule deletes or logs and every hit is reported.
  - **Instant-flood rule** — >`BURST_MAX_ITEMS` (5) GIF/sticker kind messages
    in `BURST_WINDOW_SECONDS` (3 s) → restrict + delete only that burst + warn.
    Photos are never counted; admins are not exempt; owners are.
  - **Violation ladder** — one confirmed deletion = one violation in
    `users.strikes`; warn each time; timed restriction at `VIOLATION_MUTE_AFTER`
    (3). A failed delete applies nothing.
  - **Admin report** for every deletion, every filter hit, every `REVIEW` (when
    `MODERATION_REVIEW_NOTIFY=1`) and every un-restrictable flood, with a
    self-delete button. Carries no excerpt and no media.
  - **Nexus (the assistant)** — awareness, the owner-only on/off switches, the
    VPN test handover, the owner-only coding-agent bridge, `/nexus status`,
    `/agent`.
  - **Captcha removed entirely** — no challenge, mute, timer, job, state,
    handler, callback or cleanup job.
  - **The visual media-moderation pipeline was removed entirely** — the NudeNet
    detector (`app/detector.py`), the `DecisionEngine`/`default_engine`, the
    scene classifier, the media-moderation AI stage (`assess_media`), the
    `on_media` handler and its helpers, the media-only config, npm/Python
    dependencies (`nudenet`, `transformers`, `torch`), the CPU-torch Dockerfile
    step and `HF_HOME`, the media evidence reports, and the tests dedicated to
    them. Media is no longer downloaded or inspected for content.
  - **The Admin Control Center (the dashboard)** — stages **M1** (`app/web`: the
    aiohttp app, scrypt password, signed session cookie, CSRF, the dark RTL
    shell, login/logout, `/healthz`), **M2** (authorization through `rbac`:
    one configured operator, a fail-closed permission gate, and the panel's own
    append-only `dashboard_audit` trail) and **M3** (the **Overview**: the first
    read page, answering "what is the bot doing right now" from the shared
    SQLite file the bot writes and nothing else — room/people/account counts,
    today's requests/errors, a per-workload pool table, usage, the three owner
    switches, the newest pool events and an attention list) are **built, tested
    and committed** (`e50ec5c`, `3fb63fa`, `1433835`) and **not deployed**.
    M4…M8 are planned. The panel never imports `app.gemini_pool` (that would
    build a second registry from the panel's own environment), names the gap
    where the architecture cannot support a metric (latency is not persisted,
    there is no error log), and writes nothing on a read. It is a second compose
    service over the **same image and the same `./data` volume**, so it cannot
    disturb Telegram polling. See `AgentMD.md` §53.13 and §54.27.
  - **The 21-section conversational overhaul** (2026-09-26, `AgentMD.md` §54.31) —
    the turn **queue** (`app/chat_queue.py`: one conversation's turns serialised,
    global concurrency bounded, our own rate window **waited out** and the
    refusal sentence deleted), **reply-target** completion (tag directives like
    «فلانی رو تگ کن» resolve the real person and go out under *their* message, an
    ambiguous name asks instead of guessing, a named person with no held message
    goes out unattached), **length** (the persona's two-sentence rule is a default
    and never a ceiling, `max_output_tokens` 1024 → 8192, and a long answer is
    **split across messages** at natural seams instead of truncated),
    `app/answer_shape.py` (a deterministic reading of how much answer was asked
    for), **name memory** (`people.roster` + a dedicated `people` context slot, so
    Nexus knows the people a message mentions even if they have not spoken),
    `group_messages.username` on the transcript line, and **full search delivery**
    (8 results, longer snippets, 3600-char block — the lever is the findings, never
    a second request). No new model call on any path.
  - **Runtime observation, conversation archive and incident investigation**
    (2026-09-26, `AgentMD.md` §54.36) — a production **evidence** system,
    `app/observe/`, that records what Nexus actually did so an agent can
    reconstruct a real conversation or one turn from evidence rather than a log
    tail: the incoming Telegram event, the room boundary and routing decisions,
    the composed context the model was given, the model request and response,
    what Telegram received, and every failure/retry/timeout between them. It is a
    **sink** — nothing on the authority path reads it and its failure can never
    change, delay or suppress a reply — and a **separate store**: its own SQLite
    file (WAL) under `/data/observability`, never in git, never over HTTP, never
    in a prompt. Correlated by `turn_id`/`trace_id`/`conversation_id`, and every
    event carries the `deployment_id` baked into `/srv/BUILD_INFO` at build time
    (`ARG GIT_SHA`). Operators and agents drive it with
    `python -m app.observe {status,health,recent,turns,trace,conversation,search,
    incidents,failures,find,compare,summarize,report,cleanup,capacity}` (JSON).
    Retention is configurable with no short maximum (default 24h) and capacity is
    reported, never silently trimmed. Two invariants (§53.6 message bodies,
    §53.11 raw audio) are **deliberately excepted** for this isolated store, with
    audio off by default. **Committed and pushed; not yet deployed.**
  - Full suite green: `python -m pytest -q` — **4297 passed / 0 failed** after the
    observation subsystem (4201 after Voice Context, 4114 at the 2026-09-26
    overhaul, 3862 at the panel's M3, 3843 at M2, and 1845 when the media pipeline
    was removed). There is no model in the image, so a light venv can run the
    suite.
- **What is not done / not present:**
  - No visual / media content moderation of any kind, by design.
  - No ban and no permanent punishment; the only member action is a timed
    restriction.
  - No raid detection, no hash whitelist/blacklist, no shadow mode, no
    statistics in the bot.
  - **No dashboard is deployed.** The panel's M1, M2 and M3 are built and
    committed, but no dashboard container runs and none of its `.env` settings
    (`DASHBOARD_SECRET`, a password, `DASHBOARD_OPERATOR_ID`) are set. Starting
    it is a deploy and needs the owner's go-ahead.
  - No CI pipeline.
  - No real-Telegram end-to-end run of every path: behaviour is proven by the
    test suite and Docker runtime checks, not by a live flood or a live
    deletion of every kind.
- **Planned future stages (named by the owner, deliberately not pre-built):**
  hash whitelist/blacklist, admin review, better sticker support, shadow mode,
  statistics, raid protection. The visual media-moderation pipeline is **not**
  planned for return; do not reintroduce it unless the owner explicitly asks.
- **The active staged program is the Admin Control Center** (`AgentMD.md` §54.24):
  M1, M2 and M3 are done, **M4 (AI control + credentials) is next**, then M5
  (groups) … M8 (security / performance / deploy). Each stage is tests →
  secret-scan → commit → push → verify, and **no stage is pre-built**.
- **Next:** the panel's **M4 — AI control + credentials**, only when the owner
  asks. The panel **deploy** stays blocked until the owner gives the go-ahead
  *and* `DASHBOARD_SECRET`, a password and `DASHBOARD_OPERATOR_ID` are set.

---

## 11. Standing facts worth not rediscovering

- **Fail-open is the safety contract.** Any AI, decode or internal error
  becomes `SAFE`. A false positive is treated as worse than a miss.
- **Only `EXPLICIT` deletes; `REVIEW` is log-only.** `REVIEW` never deletes,
  never notifies and never punishes.
- **A deletion needs a confident AI verdict, and nothing else can delete.** The
  policy is `MODERATION_DELETE_CONFIDENCE` (0.80) on a class in
  `MODERATION_DELETABLE_CLASSES` (`explicit_sexual`); the band down to
  `MODERATION_REVIEW_CONFIDENCE` (0.45) is `REVIEW`. There is no local signal
  left to weigh against the AI.
- **There is no visual media moderation, by design.** The NudeNet detector, the
  scene classifier and the media-moderation AI stage were removed. A photo,
  video, GIF, sticker or document is never downloaded or inspected for content;
  it is only counted for the flood rule from metadata. Do not reintroduce it
  without the owner explicitly asking.
- **`db.add_strike` is the violation counter.** One confirmed deletion = one
  violation in `users.strikes`; `VIOLATION_MUTE_AFTER` (default 3) applies the
  timed restriction. Do not add a second store.
- **The only member action is a timed restriction** (`MUTE_MINUTES`, default
  15). There is no ban and no permanent punishment.
- **Only bot owners (`WHITELIST_USER_IDS`) are exempt.** Telegram admins are
  moderated like anyone else, and a bot cannot restrict an admin — that refusal
  is logged and reported, never claimed as a success.
- **The flood rule is separate from content.** More than `BURST_MAX_ITEMS`
  (default 5) GIF/sticker kind messages in `BURST_WINDOW_SECONDS` (default 3 s)
  restricts the sender and deletes only that burst's messages. Photos are never
  counted. It is detected from metadata, so it costs no download or inference —
  but it is only detected when the threshold is crossed, so the first
  `BURST_MAX_ITEMS` messages are processed normally.
- **The live `.env` still holds first-generation leftovers** (`MAX_STRIKES=5`,
  `NSFW_DELETE_THRESHOLD`, `NSFW_BAN_THRESHOLD`, `HIGH_CONF_ACTION=mute`,
  `TRUST_AFTER_MESSAGES`, `TRUSTED_EXTRA_MARGIN`). Nothing reads them. The
  three-strike policy uses the new `VIOLATION_MUTE_AFTER` name precisely so the
  stale `MAX_STRIKES=5` cannot change it.
- **`data/` and `.env` are gitignored** and must never be committed. They hold
  the SQLite DB and the bot token.
- **There is no CI.** Verification is local `pytest` plus `docker compose
  build` / `docker compose logs`. A claim of "verified" without one of those is
  unverified.
- **The bot needs delete-message permission and privacy mode off.** A silent
  failure to delete is a Telegram-side setup problem before it is a code
  problem.
- **`Voxora-Android` is reference-only.** It must not be modified and its
  Android/Compose/product rules must not leak into GuardBot.

---

## 12. How to respond

Keep responses short and decision-oriented:

1. **Repo state** — the factual current state (tip, what changed, what is
   live).
2. **Assessment** — what is good, what is risky, what is a real bug. Only the
   relevant items.
3. **Next priority** — exactly one.
4. **Implementation prompt** — the complete, ready-to-use English prompt.
5. **Question** — only if a real product decision is genuinely needed from the
   owner.

Rules:

- Verify the repository before making any claim about the code.
- Never trust a summary over the actual commit.
- One priority at a time.
- Every prompt tells the coding agent to read `AgentMD.md` first and names what
  not to touch.
- Update the current-state section when the state actually changed.

---

## 13. First action for a new session

1. Read this file fully, then `AgentMD.md`, then `README.md`.
2. Read the real repository state: `git log --oneline -15`, the last commit
   diff, and the files it touched.
3. Compare what you found with §10. If they disagree, the code is right — fix
   §10 and note the discrepancy.
4. Report the real current state to the owner.
5. Determine the single most important next step.
6. Produce the implementation prompt.

Do not just acknowledge this file. Start the repository review immediately.
