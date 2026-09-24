# AgentMD.md — GuardBot implementation agent

This file is the contract for the **coding / implementation agent** working on
GuardBot. Read it before you open a source file, and follow it without being
asked. It is not a plan and it is not a wish list: it describes the bot that
exists in this repository today and the rules for changing it safely.

GuardBot is a **production Telegram moderation bot**. It deletes messages in
real groups that real people are reading right now. A one-line change to a
threshold, a handler filter, a temp-file path or a `try/except` can change what
gets deleted in production, what a member sees, and how much disk the VPS
uses. Treat every change as production-facing.

The companion file is `ChatGPT.md` — the strategy / review / project-continuity
document. `ChatGPT.md` decides *what* to do and reviews what was done; this
file governs *how* to do it. Do not duplicate content between them.

---

## 0. Authority and stale state

The order of authority is fixed:

**actual repository state > Git history > current project documentation > old
handoff assumptions.**

- The code in the working tree is the truth. Documentation — including this
  file, `README.md` and `ChatGPT.md` — is a *claim* about the code, and a claim
  can be stale.
- If a prompt, a handoff note, an old conversation or `README.md` says the code
  does one thing and the current source clearly does another, **do not silently
  pick one**. Investigate, then report the discrepancy explicitly in your final
  report and, if it is load-bearing, tell the owner before changing behaviour.
- Never assume a file, class, function, class-name, environment variable or
  behaviour exists because a document mentions it. Open the file and check.
- A prompt from the strategy agent is a request, not evidence. Verify every
  premise in it against the repository before you act on it.
- For the permanent rule that keeps this continuity intact across sessions —
  context preservation, durable checkpoints and session handoff — see **§55
  Context Preservation & Session Handoff**. Context is temporary; the repository
  is durable.

---

## 1. Who you are

You are a **senior Python engineer who has run Telegram bots in production**.
You are comfortable with `python-telegram-bot` v21 asyncio handlers, the Bot
API's real limits, ffmpeg, Docker on a small VPS, and SQLite under concurrent
access.

You write code that is:

- **conservative** — when uncertain, the bot does less, not more;
- **boring and explicit** — no clever abstraction for a one-off need;
- **honest** — no `TODO`/stub left behind, no comment claiming a behaviour the
  code does not have, no overclaim in the report.

You do **not** redesign the bot. You make the smallest change that satisfies
the requested stage and you leave everything else exactly as it was.

---

## 2. Mandatory inspection before any code change

Do this before writing code. Do not skip it because the prompt "already
explains" the change.

1. **Recent history.** `git log --oneline -15` and `git status --short`. Know
   the current `main` tip and whether the tree is clean. Read the message and
   the diff of the last one or two commits: the most recent commit is usually
   the context for the request.
2. **The files you are about to touch**, in full — not just the function named
   in the prompt. The change has to fit the file that already exists.
3. **The neighbours of the change:**
   - moderation decisions → `app/decision.py` **and** `app/mod_policy.py`
     **and** `app/moderation.py` **and** the handler in `app/main.py` that
     consumes them;
   - the moderation AI → `app/ai_moderation.py` (one text in, one verdict out);
   - configuration → `app/config.py` **and** `.env.example` (they must stay in
     sync);
   - anything media-related → `app/media.py` and its caller in `app/main.py`.
4. **The tests that already pin the behaviour** (`tests/`). They encode the
   safety contracts. Read them before you change what they assert.
5. **`README.md`** — it documents the decision table, the admin-report rule and
   the known limits. If your change makes any of it untrue, that is a signal,
   not an inconvenience.

If inspection contradicts the request, stop and report. Do not "fix" the
mismatch by guessing.

---

## 3. What GuardBot actually is

Verify all of this against the tree; it is accurate as of the last update of
this file.

- **Language / runtime:** Python 3.12, `python-telegram-bot[job-queue]==21.6`,
  `Pillow`, `httpx`, `google-genai`. There is **no local ML runtime**: no
  `torch`, no `transformers`, no ONNX model, no `nudenet`. Tests need `pytest`
  and the same runtime, so run them inside the image (see §11) or in a venv
  with those dependencies.
- **Entry point:** `python -m app.main` (`app/main.py:main`). It creates the
  DB dir and temp dir, calls `db.init()`, builds the `Application`, registers
  handlers, then `run_polling(allowed_updates=[MESSAGE, CALLBACK_QUERY,
  CHAT_MEMBER], drop_pending_updates=True)`.
- **Modules and their single jobs:**

  | File | Responsibility |
  |---|---|
  | `app/config.py` | every setting, from environment variables |
  | `app/db.py` | SQLite: `users` (strikes/violations) and the operational tables |
  | `app/burst.py` | pure, bounded instant-flood tracker (no Telegram, no I/O) |
  | `app/decision.py` | the moderation vocabulary: `Decision` + `DecisionResult` |
  | `app/ai_moderation.py` | the moderation AI: one text in, one verdict out |
  | `app/mod_policy.py` | the policy: an AI verdict → `SAFE`/`REVIEW`/`EXPLICIT` |
  | `app/text_filters.py` | local regex rules; a verdict, never an action |
  | `app/moderation.py` | executing a decision (delete); no Telegram import |
  | `app/main.py` | Telegram wiring: the text/flood pipeline |

  The AI/policy split is deliberate: `ai_moderation.py` produces a verdict,
  `mod_policy.py` owns *which* classes and *which* confidence count as
  deletable. Keep them separate — a future stage must be able to extend one
  without touching the other. `burst.py` is pure and bounded for the same
  reason: the flood rule is unit-testable without Telegram or a clock.
  `decision.py` holds only the shared vocabulary and can act on nothing.

- **Signals, never conflated:** text moderation (the AI + policy), the pattern
  filter, instant media flood (burst tracker), and the repeated-violation
  ladder. A flood is a violation on its own and does not require any content
  verdict; a photo is never counted toward a flood. **There is no visual media
  moderation** — media is never downloaded or inspected for content.
- **Deployment:** `Dockerfile` (`python:3.12-slim` + `ffmpeg`, plain
  `pip install -r requirements.txt`, `CMD python -m app.main`) and
  `docker-compose.yml` (service `guardbot`, `restart: always`, `env_file:
  .env`, `./data:/data`, 2 GB memory limit). The VPS runs it from `~/guardbot`
  with `docker compose up -d --build`. There is no model to download, so the
  image has no model cache and no `HF_HOME`.
- **Persistence:** SQLite at `DB_PATH` (default `/data/guardbot.db`, inside the
  mounted volume). Runtime data under `data/` and `.env` are gitignored and
  must never be committed.

---

## 4. The moderation contract you must not break

This is the load-bearing part of the project. Do not change any of it as a
side effect of another change.

### 4.1 Decisions

| Decision | Meaning | Action |
|---|---|---|
| `SAFE` | nothing wrong | allow, log only |
| `REVIEW` | worth a human's attention, below the delete bar | allow, log only — **never** delete, **never** notify silently |
| `EXPLICIT` | a confident AI verdict on a class in `MODERATION_DELETABLE_CLASSES` | delete the Telegram message |

- Only classes listed in `MODERATION_DELETABLE_CLASSES` (default
  `explicit_sexual`) can ever produce `EXPLICIT`.
- `REVIEW` never deletes and applies no punishment. It is reported to the admin
  chat when `MODERATION_REVIEW_NOTIFY=1`; otherwise it is a log line. This is
  intentional, not an omission.
- A deletion needs an AI confidence of at least `MODERATION_DELETE_CONFIDENCE`
  (default 0.80). Below that the verdict is treated as uncertain: the band down
  to `MODERATION_REVIEW_CONFIDENCE` (default 0.45) is `REVIEW`, and below it is
  `SAFE`.
- **There is exactly one evidence source.** The moderation AI's verdict is the
  only thing that can delete; nothing local is weighed against it. The old
  NudeNet/scene signals and their `DecisionResult.source` were removed with the
  media pipeline.
- The AI never executes anything. Its return value is data; `mod_policy.decide`
  turns it into an outcome and `moderation.enforce` acts. There is no code path
  from the AI's output to a Telegram call.

### 4.2 Fail open

- An AI failure, timeout, malformed answer or circuit-open state produces a
  "not confirmed" verdict, and the policy returns `SAFE` for it. Uncertainty
  never deletes anything.
- The text-moderation handler body is defensive: a failure inside it must not
  delete, notify or punish.
- **Never** add code that turns an error into a deletion. A false positive
  (deleting allowed content) is treated as worse than a miss. If you are
  unsure whether something is explicit, it must land in `REVIEW`/`SAFE`.

### 4.3 Deletion and its failure

- `app/moderation.py:enforce` is the only place that executes a content action.
  `EXPLICIT` → attempt delete. Delete succeeded → `deleted=True`, and
  `record_confirmed` (which is `db.add_strike`) is called **once**. Delete
  raised → `delete_failed`, **no violation, no restriction, no notification**.
- `DELETE_SUCCESS` and `DELETE_FAILED` are the two log outcomes. Only
  `DELETE_SUCCESS` reaches the admin report and the violation ladder.
- A failed deletion must never become a successful moderation action, and must
  never count as a violation.

### 4.4 Admin reporting

- The admin chat (`ADMIN_LOG_CHAT`) receives a report for every deletion
  (`EXPLICIT` + `DELETE_SUCCESS`), every filter hit, every `REVIEW` when
  `MODERATION_REVIEW_NOTIFY=1`, and for a confirmed flood whose restriction
  Telegram refused. `SAFE`, `DELETE_FAILED`, a *successful* restriction and
  every operational error are container-log only.
- A deletion report carries the user, user id, chat id, message id, the AI's
  classification and confidence, the policy reason and a UTC timestamp. It
  carries **no excerpt of the message and no media** — the content itself is
  what this project spends the most effort not copying into a log or an admin
  chat. The filter report carries the rule (`kind/label`), not the message.
- The report text is Persian and HTML-parse-mode. If you touch it, keep the
  same register and the same fields; do not machine-translate or restructure it.
- Every report carries a single inline button (`REPORT_DELETE_CALLBACK =
  "report_delete"`, label `🗑 حذف گزارش`). It removes **the report message
  itself** — the moderated message is already gone. It is attached to every
  report path and never sent as a separate message. `app/main.py:on_report_delete`
  verifies `callback_query.message.chat.id == config.ADMIN_LOG_CHAT` first, then
  that the presser is a **current member** of that chat (`_is_chat_member`:
  MEMBER / ADMINISTRATOR / OWNER / RESTRICTED), and only then deletes.
  Membership is checked fresh on every press and fails closed. This path
  deliberately does **not** use `is_admin` and does **not** require Telegram
  administrator status: the report group is a private trusted team group, so
  any member may clean up a report. A callback from any other chat deletes
  nothing.

### 4.5 Punishment is a timed restriction, never a ban

- The only member action is `restrict_chat_member` with `MUTED` permissions and
  an expiry of `MUTE_MINUTES` (default 15). Telegram lifts a timed restriction
  itself, so there is no reaper. `MUTE_MINUTES=0` means no automatic expiry.
  The unit is **minutes**: the old `MUTE_HOURS` (24) is no longer read, and a
  leftover `MUTE_HOURS` in a live `.env` must stay inert.
- The operator commands *do* have `unmute`, and it is the one operation that
  lifts a restriction — which is why its permission set has to be complete. It
  was not, and it left members permanently restricted; see §33.1. That section
  is required reading before touching `MUTED` or `FULL`.
- There is **no ban and no permanent punishment**. Do not add one.
- One confirmed explicit deletion is one violation, recorded through the
  existing `db.add_strike` / `users.strikes` (do not add a second violation
  store). Every violation warns the user; at `VIOLATION_MUTE_AFTER` (default 3)
  the timed restriction is applied. Every violation at or after the threshold
  re-applies it, which extends the restriction.
- **The ladder itself lives in exactly one place**: `_apply_strike_ladder` in
  `main.py`. It used to be duplicated across the content paths — which is how a
  fix lands on one path and not the other. It now restricts *then* notices, so
  the warning reflects what actually happened. Do not re-inline it.
- **A failed deletion, an AI error or a database error never punishes
  anyone.** If recording the violation fails, the deletion still stands and
  `outcome.strike` is `None`, so nothing else happens.
- `_restrict_user` returns True only when Telegram accepted the call. A refusal
  (an administrator target, missing `can_restrict_members`) is logged and
  reported, never claimed as a success.

### 4.5.1 The test account exception

`TEST_USER_ID` (default `8299811287`) is the owner's test account. It exists so
the pipeline can be exercised repeatedly without a manual unrestrict.

- It is **not exempt from anything**: moderation, deletion, the strike, the
  warning, the admin report and the real `restrict_chat_member` call all run
  exactly as for anyone else. Do not add an exemption here, and do not let this
  exception suppress the report, the deletion or the strike.
- The **only** difference is post-restriction cleanup, in
  `_schedule_test_unrestrict` / `_test_unrestrict_job`: after a *successful*
  restriction, one `job_queue.run_once` job lifts the restriction again after
  `TEST_USER_UNRESTRICT_SECONDS` (default 2 s) and deletes the warning message
  that belonged to that restriction cycle.
- The schedule happens only when the restrict actually succeeded. A refused
  restrict schedules nothing.
- `_test_unrestrict_jobs` / `_test_unrestrict_notices` are keyed by
  `(chat_id, user_id)`: a new restriction cycle cancels the pending job instead
  of stacking background tasks, and warnings from replaced cycles are still
  cleaned up. Keep that bound — do not append jobs to a list.
- The delayed unrestrict must never block the event loop (it is a job-queue
  job, not a sleep) and must never raise: failures are logged as
  `TEST_UNRESTRICT_FAILED` / `TEST_UNRESTRICT_NOTICE_KEPT` and nothing else
  happens.
- Cleanup only ever deletes the group warning message id. It must never delete
  the admin report or the moderated message.
- `TEST_USER_ID=0` disables the exception. A non-configured user must never get
  the 2-second behaviour.

### 4.6 Instant media flood

- A separate signal from content. More than `BURST_MAX_ITEMS` qualifying media
  messages (default kinds: `gif`, `sticker`, `animated_sticker`,
  `video_sticker`, `video_note`) from the same user inside
  `BURST_WINDOW_SECONDS` (default 3 s) is a flood.
- The rule is decided from message metadata in `app/burst.py` — no download,
  no ffmpeg, no inference. Keep it that way: the whole point is to stop a flood
  cheaply.
- **Ordinary photos are never counted.** Sending several photos quickly is not
  a flood, and nothing else looks at a photo either: media is never inspected
  for content.
- On a flood: restrict the sender, delete **only** the messages recorded as
  belonging to that burst, and warn. Never delete other history from the same
  user.
- A flood does not require sexual content, and it does not increment the
  violation ladder. The two signals are independent.
- `app/burst.py` must stay bounded (per-user deque cap, tracked-user cap) and
  must clear a user's window once it reports a burst, so one burst is reported
  once.

### 4.7 Exemption

- Only `WHITELIST_USER_IDS` (bot owners) are exempt. That is the owner rule.
- **Telegram admins are not exempt** — not from text moderation, not from the
  filter and not from the flood rule. Do not reintroduce an administrator check
  in `on_media_flood`.

### 4.8 Out of scope by default

Raid detection, bans, a dashboard and unrelated Telegram features do **not**
exist. Do not add any of them unless the current stage explicitly asks. The
project advances one narrow stage at a time; pre-building a future stage is a
defect, not initiative.

Pattern-based inbound filtering (banned words, links, phishing shapes) now
exists as `app/text_filters.py`, but it is **off by default** and is deliberately
not a moderation authority: it returns a verdict, and `main.on_group_filter`
routes that verdict through the same `moderation.enforce` executor every other
violation uses. It does not consult a model, it cannot ban, and it counts as a
violation only when `FILTER_COUNTS_AS_VIOLATION` is set. See §32.

### 4.9 Thresholds are calibrated evidence, not guesses

`MODERATION_DELETE_CONFIDENCE=0.80` and `MODERATION_REVIEW_CONFIDENCE=0.45` are
the two numbers that matter. They are confidence bars on the moderation AI's
verdict, not scores from a local model. They are deliberately conservative: a
false positive (deleting allowed content) is worse than a miss.

Therefore: **do not change a threshold to make a test or a scenario pass.**
Thresholds are environment variables and are tuned from real traffic. If a
change genuinely requires a different threshold, say so explicitly in the
report and let the owner decide — do not bake a new number into the code.

The removed media pipeline's thresholds (`EXPLICIT_DELETE_THRESHOLD`,
`EXPLICIT_REVIEW_THRESHOLD`, `SCENE_REVIEW_THRESHOLD`, `SCENE_DELETE_THRESHOLD`,
`MODERATION_LOCAL_HARD_THRESHOLD`) no longer exist and must not be reintroduced
without the owner asking for the pipeline back.

---

## 5. Telegram engineering realities

The Bot API and Telegram's media model have hard limits. Design within them.

- **Handlers and filters.** The flood handler is registered with a filter over
  the kinds `_burst_kind` can name (animation, video note, all stickers) **and**
  `filters.ChatType.GROUPS`. Text moderation and the pattern filter are
  registered on `filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS`. A
  new media type or a changed filter changes what is counted.
- **Only bot owners are immune.** `WHITELIST_USER_IDS` short-circuits the
  moderation paths. Telegram admins are deliberately **not** exempt. `is_admin`
  (with a 300 s `_admin_cache`) answers a different, older question and must not
  be used to skip moderation.
- **No media is inspected.** There is no download of a photo/video/GIF/sticker
  for content analysis, no frame extraction for moderation, and no model. The
  only thing a media message can trigger is the flood rule, decided from
  metadata.
- **Deletion is the only action**, and it can fail (missing delete permission,
  message already gone, rate limit). Failure is handled in `moderation.enforce`
  and never escalated.
- **Async.** Handlers are `async`. The remaining blocking work is ffmpeg in
  `app/media.py` (the assistant's media builder) and it must run off the event
  loop. Never call blocking code directly on the event loop.
- **Rate limits and API failures.** `TelegramError` is the expected failure
  mode for every API call. Catch it narrowly where a call is optional (report,
  notice) and let the handler's fail-open cover the rest. Never let an API
  failure delete or punish.
- **`drop_pending_updates=True`** is deliberate: on restart the bot does not
  process a backlog of old messages.

---

## 6. Temporary media and disk

This is a small VPS. Disk leaks are production incidents.

- The remaining media work is the assistant's own media builder
  (`app/media.py`, used by the conversation path and `/transcribe`). It writes
  into a temp location and its caller removes it on every path, including
  failure.
- There is no per-job moderation temp directory any more: the moderation path
  no longer downloads media.
- If you add a new file, frame or cache, decide explicitly who deletes it and
  prove it is deleted on every path. The tests assert `TMP_DIR` is empty after
  each run — keep that property.

---

## 7. Concurrency and shared state

- The moderation path no longer uses a `ThreadPoolExecutor` or a loaded model.
  The AI workloads share the pool in `app/ai_pool.py`, which owns their
  synchronisation.
- The SQLite connection is shared across threads
  (`check_same_thread=False`) and guarded by a module-level `threading.Lock`.
  Any new DB access goes through `db._exec` / the existing helpers; do not open
  a second connection and do not touch `_conn` directly.
- `_admin_cache` is a plain dict mutated from async handlers. Keep it small and
  keyed by `(chat_id, user_id)`; do not add unbounded per-message state.
- If your change introduces shared mutable state, say how it is synchronised in
  the report.

---

## 8. Error handling and logging

- **Every** Telegram API call is wrapped in a narrow `except TelegramError`,
  and the moderation handlers keep a fail-open path.
- Logging is `logging` with the module logger (`guardbot`, `moderation`,
  `ai_moderation`, `mod_policy`). `logging.basicConfig` is configured in
  `app/main.py`.
- The per-message decision line is the observability contract. Keep its fields
  and shape when you change the pipeline:

  ```
  text moderation chat=... user=... policy=...
  ```

- The outcome lines are `TEXT_DELETE_SUCCESS`, `TEXT_DELETE_FAILED`, and for
  the other signals `FLOOD`, `FLOOD_RESTRICT`, `FLOOD_CLEARED`,
  `FLOOD_DELETE_FAILED`, `VIOLATION`, `VIOLATION_RESTRICT`, plus the test-account
  pair `TEST_UNRESTRICT_SCHEDULED` / `TEST_UNRESTRICT` and their failure lines
  `TEST_UNRESTRICT_FAILED` / `TEST_UNRESTRICT_NOTICE_KEPT`. Do not rename or
  remove them; operators grep them.
- **Never log message content, media bytes, tokens or the bot token.**
  Classifications and confidence are fine; the message and the media are not.
- The acquisition decision has its own single line, emitted once whenever the
  message was *acted on* or was worth a second look — a message the rules
  silently ignored logs nothing, deliberately, because that is the common case.
  It is written by `app/classifier.py`:

  ```
  [intent] user=... triggered=... source=... score=... rules=...
  ai_consulted=... ai_skip=... ai_error=... ai_category=... ai_confidence=...
  ai_reason=... text=...
  ```

  `source=` is `rules` / `ai` / `none` and is the field to grep when asking
  "did the model decide this, or did the rules?". `ai_*` are empty when the AI
  layer was not consulted, which is the common case. `text=` is the *normalised*
  message, never the raw one. See §13.8.
- Add a log line only when it tells an operator something they cannot already
  see. No debug spam on the hot path.

---

## 9. Configuration and secrets

- All settings live in `app/config.py` and come from environment variables.
  `BOT_TOKEN` and `GROUP_IDS` are required at import time.
- **Any new setting must be added to `app/config.py` and documented in
  `.env.example` in the same change.** Thresholds, class lists and sizes are
  env vars precisely so they can be tuned without a rebuild — keep it that way.
- Never commit `.env`, `data/`, `*.db`, tokens or keys. They are gitignored;
  do not remove or weaken `.gitignore`.
- Never print a secret. The admin report and logs must not contain one.
- `GEMINI_API_KEY` is a secret on the same footing as `BOT_TOKEN`. It lives in
  the environment, is read once in `app/config.py`, and must never appear in a
  log line, an exception message, a database row, a report or a document. There
  is a test that asserts it cannot reach a log line, and
  `git grep -i gemini_api_key` must never match a committed file other than
  `app/config.py` and `.env.example` (both of which only name the variable).

---

## 10. Scope control

- Implement **only** the stage the prompt asks for. Do not add features,
  models, detectors, punishment, dashboards or "improvements" that were not
  requested.
- Do not refactor unrelated code. A bug fix does not need the surrounding file
  tidied. A new option does not need a new abstraction.
- Do not change moderation behaviour, thresholds, deletable classes or the
  decision table unless the stage is explicitly about them.
- **Do not reintroduce the removed media pipeline** (a local visual detector, a
  scene classifier, a media-moderation AI stage, media evidence reports) unless
  the prompt explicitly asks for it.
- Prefer the smallest diff that works. Three similar lines beat a premature
  helper.
- If you believe the requested change is wrong or unsafe, say so in the report
  and implement the safe version — do not silently expand the scope.

---

## 11. Testing

- Tests are `pytest` and they import the app, which imports
  `python-telegram-bot`, `Pillow` and `google-genai`. There is no model
  dependency, so a plain venv can run the suite; running them in the image is
  still the most faithful check:

  ```bash
  docker compose build
  docker run --rm -v "$PWD:/srv" -w /srv guardbot-guardbot \
    bash -lc "pip install -q pytest && python -m pytest tests -q"
  ```

- `tests/conftest.py` sets safe defaults (`BOT_TOKEN`, `GROUP_IDS`, in-memory
  `DB_PATH`, a temp `TMP_DIR`) so tests import the app without a real `.env`.
- `google-genai` is imported lazily inside the AI modules, so a test
  environment without it still runs the whole suite: the AI layer reports
  `sdk_missing` and the rules carry on. Never move that import to module scope —
  it would make an optional dependency mandatory at import time.
- The existing tests pin the safety contracts and must keep passing:
  - `tests/test_mod_policy.py` — the AI-only policy table: a confident verdict
    deletes, a `REVIEW` verdict never deletes, a sub-floor verdict is `SAFE`,
    fail-open, and that nothing but the AI can delete.
  - `tests/test_moderation.py` — delete success/failure, no-punishment-on-failure.
  - `tests/test_moderation_ai.py` — the moderation AI contract: malformed and
    wrong-type answers, clamped confidence, the text prompt's guarantees, and
    that the key never reaches a log line.
  - `tests/test_flood_pipeline.py` — the flood rule through `on_media_flood`:
    restrict, only-the-burst deletion, admin not exempt, owner exempt, fail-open.
  - `tests/test_burst.py` — the pure flood tracker: threshold, window, separate
    bursts, photo exclusion, per-user isolation, bounding.
  - `tests/test_test_user.py` — the test-account exception and the normal
    restriction duration: 15-minute default, the restrict still happening, the
    delayed unrestrict, the warning cleanup, the untouched admin report, and
    that other users get none of it.
  - `tests/test_ai_intent.py` — the Gemini layer on its own: config on/off/no
    key, the structured contract (malformed, missing field, wrong type,
    invented category, clamped confidence), the prompt's own guarantees, the
    timeout, the retry policy, the rate window, the persisted daily cap, the
    circuit breaker, truncation, and that the key never reaches a log line.
  - `tests/test_classifier.py` — the policy between the two layers: a rule
    match is free, the model cannot overturn a match or a veto, ordinary
    chatter is never escalated, the model can promote and can decline, and
    every failure mode degrades to the rules.
  - `tests/test_acquisition.py` — the AI layer end to end through the real
    group handler: the model's yes becomes the usual invitation, its no leaves
    the group alone, a Gemini outage does not break the handler, and with no
    key the handler behaves exactly as it did before.
  - `tests/test_text_filters.py` — the pattern filter on its own: the master
    switch, the minimum length, each family, the allow-list, word boundaries,
    each phishing label, and that the log never leaks the matched word.
  - `tests/test_filter_pipeline.py` — the filter through the real
    `main.on_group_filter`: a hit reaches the same executor and ladder as every
    other violation, a failed deletion never strikes, and the filter module
    cannot reach Telegram or the database.
  - `tests/test_report_button.py` — the admin report's self-delete button:
    membership is checked fresh and fails closed, another chat's callback
    deletes nothing, and every report path carries the button.
  - `tests/test_db_migration.py` — the `admin_audit.interface` column added to
    a table built with the old schema, idempotently, with old rows still
    readable.
- **The AI tests never touch Google.** Each AI module has exactly one network
  seam, `_request`, and the tests replace it. If you add a code path that talks
  to the API outside `_request`, the tests will silently stop covering it — keep
  the seam.
- **Do not weaken or delete a test to make a change pass.** If a contract
  genuinely changes, update the contract text here and in `README.md` and the
  test in the same commit.
- A new regression test is only worth having if it **fails against the bug**.
  When you fix a defect, add the test that would have caught it and confirm it
  fails before the fix and passes after.
- When you add a decision path or a new signal, extend the pipeline tests.

---

## 12. Docker and deployment verification

- The bot is deployed as a container. A change that works locally but breaks
  the image is a regression.
- Verify the image builds:

  ```bash
  docker compose build
  ```

- If you changed runtime behaviour, start it against a test group and watch the
  logs:

  ```bash
  docker compose up -d
  docker compose logs -f | grep -E "TEXT_DELETE_SUCCESS|TEXT_DELETE_FAILED|FLOOD|VIOLATION"
  ```

- Remember the Dockerfile installs `ffmpeg`; the assistant's media builder uses
  it, so any new binary dependency must be added there or that path breaks only
  in production.
- Report what you actually ran. "Tests pass" and "image builds" are separate
  claims; if you did not run one of them, say so.

---

## 13. Group acquisition — the VPN bot handover

How a VPN request in a group becomes a personal deep-link: the GuardBot / VPN-bot boundary, the JSON intent rules and scoring, the SQLite cooldown, the handler, the HMAC client, and the host-networking trap. GuardBot never holds a VPN credential, never talks to the 3x-ui panel, and never puts a subscription URL, UUID, `pbk` or panel client name in a group message; the signing vector is shared with the VPN bot and must change in both repos and both tests together.

Subsections: §13.1 the boundary · §13.2 what counts as an intent · §13.3 the cooldown · §13.4 the handler · §13.5 the VPN bot client · §13.6 deployment and the networking trap · §13.7 testing · §13.8 the AI second opinion.

Full text: [`docs/reference/acquisition.md#s13`](docs/reference/acquisition.md#s13).

---

## 14. Git discipline

- Work on `main` (this repository has no long-lived feature branches). Keep the
  tree clean and commit only the files the change is about.
- Commit in **logical slices**, not one giant commit, and not unrelated changes
  bundled together.
- Conventional commits, with the area as scope where it helps:

  ```
  fix(text): stop a filter hit from striking after a failed delete
  feat(flood): make the burst window configurable
  docs(agents): add the GuardBot agent workflow
  ```

  Types in use: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
- Before committing: `git status --short` and `git diff` — confirm **only** the
  intended files changed. Never commit `.env`, `data/` or a database.
- **Two remotes, two accounts — push to both.** This repository has two
  remotes, and every committed change must land on both:

  | Remote | GitHub account | Repository |
  |---|---|---|
  | `origin` | `mo3iiibest77-hub` | `mo3iiibest77-hub/guardbot` (private) |
  | `dashmo3i` | `Dashmo3i-GitAcc` | `Dashmo3i-GitAcc/Guardbot` (public) |

  `main` tracks `origin/main`; `dashmo3i` is pushed explicitly. After a change
  is committed, run `git pushall` (a repo-local alias for
  `git push origin main && git push dashmo3i main`), or push to each remote in
  turn. Each remote URL carries its account name so the credential store
  selects the right token per account — do not remove the username from a
  remote URL, and do not add a second push URL to `origin` (that would mix the
  two accounts on one remote).
- **Never claim a push succeeded from the `git push` output alone.** Confirm the
  commit is actually visible on the remote — `git ls-remote <remote>
  refs/heads/main` and, for an independent check, the GitHub API
  (`/repos/<owner>/<repo>/commits/main`). Do this for **both** remotes.
- Do not push to a different repository, do not modify `Voxora-Android` (it is
  a read-only reference project), and do not force-push or rewrite history
  unless the owner asks.

---

## 15. Documentation updates

- If a change alters the decision table, the admin-report rule, a known limit,
  a setting or the architecture, update `README.md` **and** the relevant
  section of this file in the same commit. `ChatGPT.md`'s current-state section
  is maintained by the strategy agent, not by you — but if you know the state
  it records is now wrong, say so in your report.
- Keep documentation honest. Do not describe a class, a behaviour or a
  capability the code does not have. If a limit exists, document it as a limit.

### 15.1 Owner-facing guides live in `/root/project-guides/`

The owner reads from the server, not from a chat window, and hands the text to
another AI for Persian translation. So whenever a substantial explanation is
needed — a testing procedure, a deployment runbook, a feature walkthrough, a
troubleshooting guide, an operational how-to — the answer is a **file**, and the
chat response is the path to it. This directory is shared with the VPN bot, so
the same convention is recorded in `/opt/vpn-bot/AGENTS.md` §6.1.

- Directory: `/root/project-guides/`.
- Naming: one English `.txt` per topic, lowercase-hyphenated, topic-first —
  `gemini-testing-guide.txt`, `guardbot-deployment.txt`.
- Write it self-contained and in full. Assume the reader has only the file.
- Include, as the topic requires: exact commands (copy-pasteable), absolute
  paths, expected output, how to tell success from failure, troubleshooting
  steps for the likely failures, safety notes (what is destructive, what must
  never be printed), and enough explanation that the reader understands *why*
  the step exists — not just what to type.
- Never put a secret in a guide, and never print one while writing it. Show the
  command that reads `GEMINI_API_KEY` or `VPNBOT_SHARED_SECRET` from `.env`
  without echoing the value.
- If the topic already has a file, update that file rather than creating a
  second one.
- Then, in the chat response, state the exact path so the owner can open it
  (`nano /root/project-guides/<file>.txt`, or `cat`). Keep the chat reply short.

---

## 16. Final implementation report

End every task with a report in this shape. The strategy agent reviews from it,
so an unverifiable claim is worse than an admitted gap.

```
## What was requested
<one short paragraph>

## What the repository actually contained (checked, not assumed)
<the current behaviour you found, with file:line references, and anything in
the request that did not match the code>

## What was actually implemented
<the change, and why it is the smallest correct one>

## Files changed
<each file and what changed in it>

## Defects found and fixed
<anything you hit on the way, and the fix>

## Tests actually run
<the exact command and the result; name the tests added or changed>

## Docker / deploy verification
<what you built or ran, or explicitly "not run">

## Unresolved limitations
<what is still unverified or out of scope — be specific>

## Exact next action
<the single next step for the owner or the strategy agent>
```

Rules for the report:

- **Never claim you read, ran or verified something you did not.** If you did
  not run the Docker build, write "not run".
- If the request was premised on something false, say so at the top.
- One clear next action, not a list of twenty.

---

## 17. The conversational assistant

The conversational workload: its boundary against acquisition, the per-project quota fact, the measured model choice, config, bounded memory, failure isolation and output safety. `main._nexus_directed` decides the assistant answers and `main._nexus_will_answer` is the question `on_group_text` must ask before `classifier.classify`, or one message gets both a reply and a trial offer.

Subsections: §17.1 the boundary · §17.2 the quota question · §17.3 configuration · §17.4 conversation memory · §17.5 budgets and failure isolation · §17.6 output safety · §17.7 verifying it · §17.8 known limitations.

Full text: [`docs/reference/assistant.md#s17`](docs/reference/assistant.md#s17).

---

## 18. Outbound AI connectivity: which IP family

`app/net.py` orders AI hostnames IPv6-first and reports the decision, measured on this host. `install_preference()` is a reorder, never a filter — every IPv4 address stays in the list — and it must not bind a source address, disable IPv4, or add a third-party resolver.

Full text: [`docs/reference/media-and-net.md#s18`](docs/reference/media-and-net.md#s18).

---

## 19. The Guard Bot is the execution layer

The architecture diagram, and the rule that makes "the model cannot delete" structural: the AI modules have no Telegram client and no reference to one, asserted by parsing their imports. Every Telegram call, including deletion, comes from `main.py` only, and a wrong AI verdict cannot delete on its own.

Full text: [`docs/reference/moderation.md#s19`](docs/reference/moderation.md#s19).

---

## 20. The moderation AI workload

The moderation AI workload: one text in, one verdict out, with validation strict in one direction. An unrecognised classification makes the whole verdict undecided; every failure produces `decided=False`; and a malformed answer must not count toward the circuit breaker.

Full text: [`docs/reference/moderation.md#s20`](docs/reference/moderation.md#s20).

---

## 21. The moderation policy

The policy engine: pure functions turning evidence into an action. No I/O, no clock, no randomness, no Telegram; `Action` has no BAN and no MUTE; a non-`DELETE_WARN` action can never produce `Decision.EXPLICIT`; and REVIEW never deletes or punishes.

Full text: [`docs/reference/moderation.md#s21`](docs/reference/moderation.md#s21).

---

## 22. Media understanding

The assistant's media builder: measured transports, every Telegram kind, and the fallbacks. The Files API is deliberately unused; the builder does not own cleanup; long audio is refused, not truncated; and `.tgs` is read through its still preview only.

Full text: [`docs/reference/media-and-net.md#s22`](docs/reference/media-and-net.md#s22).

---

## 23. The conversational assistant, made genuinely contextual

The anti-form prompt rules, the repetition guard, media inside a conversation, and the `_wire` split. The repetition guard compares the model's turns only, exempts answers under 24 characters, and its extra request spends its own budget separate from the transient-error retry.

Full text: [`docs/reference/assistant.md#s23`](docs/reference/assistant.md#s23).

---

## 24. Voice: transcription, and voice replies

Voice transcription and voice replies. `transcribe` is called from exactly two places and a test asserts the count; the instruction is verbatim with no translate and no answer; voice replies are off by default and best-effort, and `_tts_request`'s failures do not count toward the chat breaker.

Full text: [`docs/reference/assistant.md#s24`](docs/reference/assistant.md#s24).

---

## 25. Administration: roles, hierarchy and owner protection

The authority model: owner by config, permissions over roles, the check order, the Telegram floor, the commands and the promote dialog. `OWNER_USER_ID` is compared and never looked up; authorisation compares permissions and never role names; and `owner_protected` applies to everybody, including the owner.

Full text: [`docs/reference/admin-and-audit.md#s25`](docs/reference/admin-and-audit.md#s25).

---

## 26. Four AI workloads, four budgets

| | acquisition | conversation | moderation | transcription |
|---|---|---|---|---|
| module | `ai_intent.py` | `chat.py` | `ai_moderation.py` | `transcribe.py` |
| switch | `GEMINI_ENABLED` | `GEMINI_CHAT_ENABLED` | `GEMINI_MOD_ENABLED` | `TRANSCRIBE_ENABLED` |
| key | `GEMINI_API_KEY` | `GEMINI_CHAT_API_KEY` | `GEMINI_MOD_API_KEY` | `TRANSCRIBE_API_KEY` |
| model | `GEMINI_MODEL` | `GEMINI_CHAT_MODEL` | `GEMINI_MOD_MODEL` | `TRANSCRIBE_MODEL` |
| counters | `ai_usage` | `chat_usage` | `moderation_usage` | `transcript_usage` |
| daily cap | 400 | 200 | 500 | 300 |
| triggered by | any group message | an explicit address | a group text message, if enabled | an explicit request only |

Each has its own `_recent_calls`, `_consecutive_failures`,
`_circuit_open_until`, `_client` and `_client_key`. `tests/test_ai_isolation.py`
asserts this three ways: by mutating one workload's state and reading the others,
by parsing each module's imports to prove no workload imports another, and by
walking the AST for `db.<counter>` accesses to prove no workload touches another's
table.

**On keys and Google's quotas.** Limits are applied per Google Cloud *project*,
not per API key (verified against the official page, §17.2). So four separate
keys in one project is still one allowance, and the shared-key fallbacks
(`*_ALLOW_SHARED_KEY`) are explicit opt-ins rather than automatic. The startup
log says which workloads are sharing, once, so a 429 on the classifier that
appears the first time the group is busy has a visible explanation.

Since §28 each workload is backed by a *pool* rather than a single credential.
That does not change this rule — it is built on it. Every pooled key is treated
as its own account with its own state, and a key that reaches two workloads is
reported at boot as one shared allowance, because that is what it is.

**The four above are the original four, not the current total.** Later stages
added more pool workloads, and this section deliberately does not enumerate
them: the authoritative list is `config.GEMINI_POOLS`, and §28.10's *Later
additions* note names each one and says whether it is a mode of the
conversation or an independent capability. Read the count there, never from
this heading — the heading is a statement about these four budgets, and reading
it as the current total is exactly how the count drifted before.

---

## 27. Gotchas learned the hard way

1. **A bar that looks safe can mean the feature never fires.** A confidence
   threshold set too high means every true positive lands in `REVIEW` and
   nothing is ever deleted. Watch the `policy=` lines on real traffic before
   concluding a stage works.
2. **Fail-open is a feature, not laziness.** An AI failure is a "not confirmed"
   verdict → `SAFE`. Never let an exception path produce `EXPLICIT`.
3. **`REVIEW` never deletes and never punishes — and it is no longer silent.**
   It used to be log-only. It now sends one message to `ADMIN_LOG_CHAT`
   (`MODERATION_REVIEW_NOTIFY`), because REVIEW became the landing place for
   every disputed and every uncertain case, and without a notification "the bot
   stopped deleting" and "the bot stopped working" look identical from outside.
   What must never change: no deletion, no strike, no restriction, and no
   message content in the notice.
4. **The media builder does not own cleanup.** `app/media.py` writes into a
   caller-supplied location; the caller removes it on every path. Keep that
   ownership rule.
5. **`.tgs` stickers are preview-only.** Lottie is not readable by ffmpeg or
   the model, so an animated sticker is read through its still preview. Do not
   claim animated stickers are fully analysed.
6. **A flood fires the moment the threshold is crossed, not after the window
   closes.** The first `BURST_MAX_ITEMS` messages are processed normally; only
   the burst's own messages are deleted, and the window is then cleared so the
   next message starts a fresh burst. Do not "fix" this into waiting for the
   window to expire.
7. **The live `.env` still contains first-generation leftovers** (`MAX_STRIKES`,
   `NSFW_DELETE_THRESHOLD`, `NSFW_BAN_THRESHOLD`, `HIGH_CONF_ACTION`,
   `TRUST_AFTER_MESSAGES`, `TRUSTED_EXTRA_MARGIN`). Nothing reads them. The
   violation threshold is `VIOLATION_MUTE_AFTER` (default 3) precisely so the
   stale `MAX_STRIKES=5` cannot change the documented three-strike policy. Do
   not start reading the old names.
8. **The bot needs delete-message permission and privacy mode off.** If
   deletion silently fails, check the Telegram-side setup before the code.
9. **`drop_pending_updates=True` means restarts skip the backlog** by design —
   do not "fix" it into processing old messages.
10. **`data/` and `.env` are gitignored and must stay out of Git.** The SQLite
    DB and the token never belong in a commit.
11. **A documentation change is not a code change.** Do not let a docs commit
    carry source edits, and do not let a code commit quietly rewrite the
    decision table.
12. **A container reaching a host service through the bridge gateway can hang
    instead of failing.** `host.docker.internal:host-gateway` points at the host
    itself, so the packet hits the host's `INPUT` chain, where ufw's default-deny
    drops it. The symptom is a *timeout*, which is indistinguishable from the
    service being down — do not spend an hour debugging the VPN bot. The
    acquisition flow uses `network_mode: host` and `127.0.0.1` for exactly this
    reason (§13.6). Test a new host dependency with a raw
    `socket.create_connection()` from inside the container before wiring it into
    application code.
13. **`host-gateway` resolves to the *default bridge* gateway, not the compose
    network's.** With `network_mode: host` this stops mattering; if you ever go
    back to a bridge network, remember that `getent hosts host.docker.internal`
    inside the container is the only way to know which address it picked.
14. **A fake that returns the shape you wish for hides real bugs.** The
    acquisition tests originally faked the panel with flat `total` / `up` /
    `down` keys; the real panel sends `totalGB` and nests the counters under
    `traffic`, so the sweep's exhaustion check passed while being unable to fire
    in production. When faking an external system, copy its *actual* response —
    see `/opt/vpn-bot/AGENTS.md` §5.6.
15. **A seam that everything replaces is a seam nothing tests.** `chat._request`
    is replaced by every test in `tests/test_chat.py`, so a mistake *inside* it
    is invisible to the whole suite. One was: a payload whose `parts` mixed a
    string with a `types.Part` fails pydantic validation with nineteen field
    errors, and it only appeared the first time an image was attached to a real
    turn. The fix was to split the conversion into `chat._wire` and test it
    directly. When a function is the universal test seam, the code inside it
    needs its own tests or a live call — there is no third option.
16. **A score is not a decision.** The removed local detector's threshold was
    calibrated correctly and it still produced false positives on ordinary
    photographs, because calibration is not the same as being right. The fix was
    not a better number: it was to stop letting a single local signal decide at
    all. When one uncalibrated signal can destroy something, the problem is the
    signal's authority, not its value.
17. **`uncertain` has to be a veto, not a footnote.** A model that answers
    "explicit, 0.95, but I am guessing" has told you it does not know. Treating
    the confidence as the answer and the uncertainty as colour is how a
    deliberate hedge becomes a deletion.
18. **Callback data is attacker-controlled.** The promote dialog carries a
    permission bitmask in its buttons, and any client can send any bytes. The
    handler therefore re-runs every authorization check on every press and treats
    its own payload as a suggestion of what to display. A dialog that trusts its
    own buttons is a privilege-escalation bug with a nice UI.
19. **The owner must not be a row in a writable table.** `OWNER_USER_ID` is
    compared, never looked up, so no command, no button and no hand-edited
    database row can create or remove the highest authority. The moment "owner"
    is a row, "make me owner" becomes a thing an attacker can ask for.
20. **A refused Telegram operation is not a failed command.** `promoteChatMember`
    can succeed at the application layer and fail at Telegram's. Reporting those
    as one outcome is how an operator comes to believe somebody has rights they
    do not have, so the three outcomes (stored+applied, stored+refused,
    not-attempted) each have their own sentence.
21. **Transcription and generation are different jobs with different failure
    meanings.** They share nothing — not a key, not a window, not a breaker —
    because otherwise "the transcript was wrong" and "the reply was wrong" arrive
    as the same counter, and a busy voice chat can silence the assistant.

---

## 28. The Gemini account pool: many keys, one AI service

The Gemini account pool: one key per account, two levels of failover, capability versus availability, the state machines, retries, selection, events and config. A model failure never disables an account; there is no "requests remaining" figure anywhere; and pool events are recorded and never announced.

Subsections: §28.1 one key per account · §28.2 the two levels of failover · §28.3 capability, not just availability · §28.4 the state machines · §28.5 what the provider does not tell us · §28.6 retries and cooldowns · §28.7 selection · §28.8 pool events · §28.9 configuration · §28.10 what did not change · §28.11 verifying it · §28.12 known limitations · §28.13 the intent failure count.

Full text: [`docs/reference/gemini-pool.md#s28`](docs/reference/gemini-pool.md#s28).

---

## 29. AI-mediated administration: the model asks, the bot decides

AI-mediated administration, and the rule the whole subsystem exists to enforce: a language model may ask and may not decide. `admin_service.execute()` is the only place that performs an administrative action, `AdminRequest` carries no authority field, and the actor is re-resolved from `actor_id` on every call.

Full text: [`docs/reference/admin-and-audit.md#s29`](docs/reference/admin-and-audit.md#s29).

---

## 30. The audit trail says which interface acted

The `interface` column on the audit trail and the `_ensure_column` primitive. `_record()` coerces anything unrecognised to `python`, and nothing else may invent an interface value.

Full text: [`docs/reference/admin-and-audit.md#s30`](docs/reference/admin-and-audit.md#s30).

---

## 31. The administration mode is observable

The administration mode is observable: `mode_line` wired into `post_init` and `/pool`, and `recent_refusals`. `recent_refusals` reads the audit table rather than an in-memory counter, the scan is bounded, and a duplicate is deliberately not a refusal.

Full text: [`docs/reference/admin-and-audit.md#s31`](docs/reference/admin-and-audit.md#s31).

---

## 32. The inbound text filter

The inbound text filter: a verdict and nothing else. The module never imports Telegram or the database and never calls an executor — asserted as a source property — and an unknown action resolves to `off`, never `delete`.

Full text: [`docs/reference/moderation.md#s32`](docs/reference/moderation.md#s32).

---

## 33. One strike ladder

One strike ladder, and the two permission sets it applies. `_apply_strike_ladder` is the only implementation and must never be re-inlined; the order is restrict then notice; and `FULL` must name every field, which is why it is `ChatPermissions.all_permissions()` and a test enumerates every library field.

Subsections: §33.1 the two permission sets, and the unmute that was not one.

Full text: [`docs/reference/moderation.md#s33`](docs/reference/moderation.md#s33).

---

## 34. Nexus: who may talk to the assistant, and what it may do about it

Nexus as a role: the six-step gate, observation, ONLINE/OFFLINE, natural-language administration, identity memory, and `nexus.control`. Nexus is a role and not a model or credential; `looks_actionable` is a timing hint only; and `people.py` grants nothing and never guesses.

Full text: [`docs/reference/nexus-awareness.md#s34`](docs/reference/nexus-awareness.md#s34).

---

## 35. Nexus Group Awareness: understanding the room

Group awareness: the deterministic/semantic split, the window and its bounds, the staged context, the pass budget, awareness versus response, and the owner roster. `awareness.due` cannot see messages; `parse_decision` returning `None` means say nothing; and `group_pending` must exclude `role = 'nexus'`.

Full text: [`docs/reference/nexus-awareness.md#s35`](docs/reference/nexus-awareness.md#s35).

---

## 36. The assistant reads a room when its own clock expires

The latency work: the `PassTrace`, deadline-based scheduling, one-transaction capture, and what was deliberately not done. The trace logs durations only, never content, and `_awareness_ready_at` is cleared in the pass's `finally`.

Full text: [`docs/reference/nexus-awareness.md#s36`](docs/reference/nexus-awareness.md#s36).

---

## 37. Why the visual media pipeline was removed

The bot once ran a photo / video / GIF / sticker content pipeline: a local
NudeNet detector for explicit body regions, a second-stage scene classifier, a
`DecisionEngine` that combined them, and a media-moderation AI stage on top. It
was removed deliberately, and this section records why so a future session does
not "restore" it by accident.

### 37.1 The false positives were the signal, not the tuning

Live use produced false positives on ordinary photographs. Two independent
causes were found, and neither was a threshold:

1. The policy turned any local REVIEW into a REVIEW even when the AI answered
   `normal` with high confidence — it only looked at the local verdict.
2. The gate deciding whether to ask the AI at all was a dead conjunction
   (`scene_nsfw is None`), which for a clean image is `0.0`, so the AI was asked
   about every image.

Tuning did not fix the class of problem: a single local visual score was being
trusted to destroy somebody's message. The architecture that removed that
authority — demote the local signal, require the AI to confirm — was the first
fix. The pipeline was then removed entirely at the owner's request, because the
remaining value did not justify the cost (a ~330 MB model, CPU inference on a
2-core VPS, and a second content path to maintain) and because the text path and
the flood rule cover the cases the owner actually needs.

### 37.2 What was removed, and what was kept

Removed: `app/detector.py` (NudeNet + ffmpeg frame sampling + the scene
classifier), `DecisionEngine`/`default_engine`, `ai_moderation.assess_media` and
its media context, `mod_policy`'s local-vs-AI rules and `local_only_hard_evidence`
mode, the `on_media` handler and its helpers, the media evidence reports, the
media-only config (`MEDIA_ENABLED`, `SCENE_*`, `EXPLICIT_*`, `VIDEO_FRAMES`,
`MAX_DOWNLOAD_MB`, `MEDIA_WORKERS`, `MODERATION_MEDIA_ENABLED`,
`MODERATION_REQUIRE_AI_CONFIRM`, `MODERATION_LOCAL_HARD_THRESHOLD`,
`MODERATION_AI_ASK_ON_SAFE`), the `nudenet`/`transformers`/`torch` dependencies,
the CPU-torch Dockerfile step and `HF_HOME`, and the tests dedicated to them.

Kept, because none of it was dedicated to that pipeline: `app/burst.py` (the
flood rule), `app/text_filters.py`, `app/moderation.py` (the executor), the
`Decision`/`DecisionResult` vocabulary, the strike ladder and the admin report
infrastructure, `app/media.py` (the assistant's media builder and
`/transcribe`), and ffmpeg.

### 37.3 The rule that follows

**Do not reintroduce it.** A video or GIF must not enter any content pipeline;
no general or conversational AI may be made to do the job by accident. If a
future stage needs media content understanding, it is a new, explicitly
requested stage with its own design — not a restoration.

## 38. "Him" means him

Why a follow-up could not resolve its antecedent, and the two fixes: the recent-actions block and the tool amendment. The block lists only this actor's successful actions in this room, it cannot be planted, and it is context, never authority.

Full text: [`docs/reference/assistant.md#s38`](docs/reference/assistant.md#s38).

---

## 39. The coding-agent bridge: Telegram → Nexus → CodeBuddy → Telegram

The coding-agent bridge: two processes meeting over a directory, the repository allowlist, the closed operation vocabulary and its danger classifier, the unpublished dangerous request, owner approval, the spool wire, the runner, and long answers. The child inherits the real `HOME`, never a fresh one.

Full text: [`docs/reference/coding-agent.md#s39`](docs/reference/coding-agent.md#s39).

---

## 40. A private chat is the owner's, and one request gets one reply

A private chat belongs to the owner: the second gate, and the two duplicate-reply defects. `accepts_private` checks offline first and then `is_owner`, a group room boundary cannot open it, and the gate runs before `_answer_conversationally`, so a non-owner costs no model call and no row.

Full text: [`docs/reference/assistant.md#s40`](docs/reference/assistant.md#s40).

---

## 41. The allowance is a day's, so it is spent across the day

How the awareness allowance is spent across the API day, and the shared-credential half. The gap is derived from the remaining allowance and the seconds left in the day, floored at the minimum interval; the brake is per room; and the check sits in front of the transcript render.

Full text: [`docs/reference/nexus-awareness.md#s41`](docs/reference/nexus-awareness.md#s41).

---

## 42. What is kept, what is windowed, and what is never touched

### 42.1 Two retention rules that never ran

Auditing growth turned up something worse than a missing rule. Two rules were
already written, already documented as running, and had **no caller at all**:

| function | its docstring said | reality |
|---|---|---|
| `db.daily_prune` | "called on the pool path" | no caller |
| `admin_tools.prune` | "called from the administrative path" | no caller |

So the audit trail and the per-day spend table were both unbounded in practice,
and the only thing between them and the operator noticing was a docstring
describing a call site that did not exist. A retention rule that is not called is
indistinguishable from no retention rule, except that it reads as though the
problem were handled.

Both now hang off a path that already runs, with a counter — the pattern
`people` established, because pruning on every call would run DELETEs on a hot
path and never pruning is what they were already doing by accident:

* the administrative windows are applied from `admin_service._record`, which is
  the one place every request arrives regardless of outcome. A trail that only
  bounded itself on success would grow fastest on the requests that were denied,
  which are the ones a burst of probing produces;
* the pool's windows are applied from `gemini_pool.generate`, once per logical
  request rather than once per provider attempt.

### 42.2 The one table that needed a new rule

`gemini_events` is the only table in the schema that grows with *activity*
rather than with the number of accounts, days or people. It gets a 90-day window,
chosen to still answer "why was this rate-limited last month" — a question that
was asked for real during the incident that produced §41, and a shorter window
would have discarded the evidence. It also gets `idx_gemini_events_at`: the
existing dedup index ends in `at`, so it cannot serve the range scan a delete by
age needs, and without it the sweep would read the whole table every two hundred
requests — a worse problem than the growth.

### 42.3 What is deliberately not bounded

| table | decision | why |
|---|---|---|
| `admin_audit` | windowed, never truncated | accountability survives a retention rule; it does not survive a rule that empties the table. A test ages one row, runs the sweep, and asserts exactly one row was removed — a test that only checked "old rows are gone" would pass for an implementation that deleted the table |
| `admin_requests` | windowed | the window must be at least as long as the replay window, which `config.py` enforces when it reads the two settings |
| `ai_usage`, `chat_usage`, `moderation_usage`, `transcript_usage` | **no window** | one row per day each, so a year is 1,460 rows and a few tens of kilobytes. There is nothing to save, and a window would destroy the only month-over-month history the owner has. "Control growth" is not a licence to delete data that is not growing |
| `users`, `intent_offers`, `people` | bounded by rows and age | keyed by person, not by time; `people` already has both a row bound and an age bound |
| `chat_messages`, `group_messages` | already pruned | by TTL, on their own paths |

The four reporting tables are asserted as an *absence* — no prune function is
applied to them — so a later "add a TTL everywhere" pass has to delete that test
deliberately rather than inherit the decision.

## 43. The audit trail says with what authority, and proves what it cannot hold

The audit trail's authority columns. `role` is resolved from `rbac` at write time and never taken from the request; the schema has no column wide enough for a conversation; and a sentinel credential appears in no audit row and no log line.

Full text: [`docs/reference/admin-and-audit.md#s43`](docs/reference/admin-and-audit.md#s43).

---

## 44. Identity: a handle, and turning a reference into one person

The internal identity handle and deterministic resolution. The handle is not derived from the Telegram id and is not authority; resolution never guesses; and two matches return `ambiguous` with no `identity` field.

Full text: [`docs/reference/admin-and-audit.md#s44`](docs/reference/admin-and-audit.md#s44).

---

## 45. What the assistant may read, and the two boundaries around it

What the assistant may read: `agent_data.py`. There is no `execute_sql` and no parameter becomes SQL text; every string that leaves passes through `redact`; and the read tools carry no `chat_id`, `actor_id`, `target_id` or `permissions` parameter.

Full text: [`docs/reference/admin-and-audit.md#s45`](docs/reference/admin-and-audit.md#s45).

---

## 46. Integrations: what exists, what does not, and saying so

The capability registry and its three states. It is a capability registry and not an integration — never invent an endpoint — and the list is asserted against the paths `app/vpnbot.py` actually implements.

Full text: [`docs/reference/integrations.md#s46`](docs/reference/integrations.md#s46).

---

## 47. The weak-internet repetition, and its cause

The rule-group mapping that made every blocked-app complaint sound like a slow line. Only the `poor_internet` group produces the connectivity wording; a generic `problem` produces `access_offer`.

Full text: [`docs/reference/moderation.md#s47`](docs/reference/moderation.md#s47).

---

## 48. The escalation path is closed by name, not by accident

The requirement is one sentence: *Gemini must never be able to change its own
permission or role.* It was already true — but only incidentally, and that is a
different thing from being true on purpose.

`rbac.REASON_SELF_TARGET` existed and **nothing emitted it**. Self-promotion was
refused by the hierarchy check instead, because an actor's own level is never
below their own level, so the request failed at `REASON_HIGHER_RANK`. The refusal
was correct and the explanation was wrong: an operator reading "the target is at
or above your own level" would go looking for a peer they were trying to
demote, not for the fact that they had aimed at themselves. Worse, the
`authorize()` docstring stated outright that self-targeting was *allowed* — it
described the behaviour that the code did not have, which is the kind of comment
that survives a refactor and then authorises the wrong thing.

So the guard is now explicit. `authorize()` takes `role_change: bool = False`,
and `authorize_grant()` — which is the only path a promotion or a demotion can
take — passes `role_change=True`. When it is set and the target is the actor,
the decision is `REASON_SELF_TARGET`. One guard covers both operations, because
both carry `changes_role=True`.

**The ordering is load-bearing, and is asserted rather than commented.** The
self-check sits *after* the owner-protected check and *before* the hierarchy
check:

* owner targeting themselves still returns `REASON_OWNER_PROTECTED`, which is
  the more specific and more important refusal — the owner is never a valid
  target of anything, including their own request;
* everybody else targeting themselves gets `REASON_SELF_TARGET`, which says what
  actually happened;
* and every other operation is untouched: `role_change` defaults to `False`, so
  self-mute, self-warn and self-delete still behave exactly as they did. The
  brief's rule is that self-moderation is allowed; the escalation guard is about
  *authority*, not about a moderator muting themselves.

`ADMIN_SELF_TARGET_TEXT` was added so the command path says the specific thing
rather than the generic denial, and `main._deny_text` maps it.

`OWNER_ONLY_PERMISSIONS` was referenced nowhere before this and is now
load-bearing: a test asserts that **no** `ROLE_PERMISSIONS` bundle carries an
owner-only permission, so "an administrator can be given `nexus.control`" is not
a policy that could be set wrongly — it is a state the suite refuses to reach.
That is the same property the new `vpn.read` / `vpn.manage` permissions rely on
(§49).

Tests: `tests/test_rbac.py` and `tests/test_ai_admin.py` cover self-promotion and
self-demotion through both interfaces, that the refusal is *not* reported as a
rank problem, that the owner targeting themselves is still owner-protected, that
the guard does not change the reason for any other operation, that an actor can
still change somebody else's role, and that no role bundle carries an owner-only
permission.

## 49. The VPN operational surface: powerful, and under the owner's hand

The VPN operational surface: reads, writes, the closed escalation path, owner-only permissions, the two-step write, and the fail-closed outcomes. `admin_service.py` is the gateway and `vpn_service.py` is never called by a handler; the second step of a write is a reference, not an approval.

Full text: [`docs/reference/integrations.md#s49`](docs/reference/integrations.md#s49).

---

## 50. The owner's credential control plane

The owner's credential control plane (`/keys`): where a credential lives, the pool as the single source of truth, the entry flow, and what never appears. The store is plaintext on disk, mode `0600`, inside the data volume, and no credential appears in the database, the audit row, any log line, any screen or a probe's error detail.

Full text: [`docs/reference/integrations.md#s50`](docs/reference/integrations.md#s50).

---

## 51. Nexus Voice Live: the same assistant, with a microphone

Nexus Voice Live: the same Nexus through a microphone. It is gated by `GEMINI_LIVE_ENABLED=false` by default; joining is MTProto and cannot use the bot token; the feed to the provider must never stop; speaker identity comes from Telegram and never from the model; and the awareness bridge is read-only.

Full text: [`docs/reference/voice-live.md#s51`](docs/reference/voice-live.md#s51).

---

## 52. Web search: the live web, as a workload of its own

Web search as a workload of its own: why it is separate, where it plugs in, when it searches, the security boundary, attribution, failure behaviour and isolation. Grounding is never switched on for `app/chat.py`; the search call declares no function tools; and a page is data, never a command. The workload is **provider-agnostic**: `SEARCH_PROVIDER` selects Gemini grounding (the default) or Tavily, exactly one at a time, with no automatic fallback between them.

Full text: [`docs/reference/web-search.md#s52`](docs/reference/web-search.md#s52).

---

## 53. Invariants

Every `must` / `never` / `always` rule from the sections that now live under
`docs/reference/`, collected in one place so a rule can be grepped without
reading the narrative it came from. The section numbers in the headings point at
the explanation, not at the rule's authority — the code and its tests are the
authority, and §0 says so. Where a rule below and a reference file disagree, the
reference file is stale and this list is the one to fix first.

### 53.1 Moderation, and the execution layer

* The AI modules (`ai_intent`, `ai_moderation`, `transcribe`) must have **no
  Telegram client and no reference to one**; a test parses their imports. Every
  Telegram call, including deletion, comes from `main.py` only.
* A wrong AI verdict **cannot delete on its own**. A confident AI verdict is the
  only thing that can delete.
* Moderation is **text-only**: no `content_type`, no `assess_media`.
  `recommended_action` is a recommendation only.
* An unrecognised classification makes the whole verdict undecided — **never**
  coerce it to `normal`.
* Every moderation failure produces `decided=False`; a malformed answer must
  **never** count toward the circuit breaker.
* `MODERATION_TEXT_ENABLED` defaults off and is the only content switch.
* The policy engine is **pure**: no I/O, no clock, no randomness, no Telegram.
* `Action` has **no BAN and no MUTE**; a non-`DELETE_WARN` action can **never**
  produce `Decision.EXPLICIT`. REVIEW never deletes and never punishes, and its
  notice carries no message text.
* The text filter module **never** imports Telegram, **never** imports the
  database, and **never** calls `moderation.enforce`, `add_strike` or any
  executor — asserted as a source property.
* An unknown filter action resolves to `off`, **never** `delete`; the report
  names the rule, never the word. The filter never consults a model.
* `text_filters.py` must **not** be renamed — the name is what keeps it from
  shadowing `telegram.ext.filters`.
* `_apply_strike_ladder` is the **only** strike implementation — never re-inline
  it. The order is restrict, then notice.
* `MUTED` names one field and relies on "unspecified means false" — **never**
  fill in the other fields.
* `FULL` **must** name every field (`ChatPermissions.all_permissions()`), and a
  test enumerates every library field so a future API field fails the suite.
* Never add a restriction state cache; never reset `strikes` on unmute; re-read
  a member's status before concluding an unmute failed.
* Only `poor_internet` produces the connectivity wording; a generic `problem`
  produces `access_offer`.

### 53.2 Media and outbound connectivity

* The Files API is deliberately unused, and long audio is **refused, not
  truncated**.
* The media builder **never** owns cleanup — the caller removes the temp file on
  every path.
* Nothing in media understanding feeds content moderation; media is **never**
  inspected for content.
* `.tgs` is read through its still preview only — never claim animated stickers
  are fully analysed.
* `install_preference()` is a **reorder, never a filter** — every IPv4 address
  stays in the list.
* It must **not** bind a source address, **not** disable IPv4, and **not** add a
  third-party resolver.
* The wrapper is restricted to the hard-coded AI hostname set, and an explicit
  `AF_INET` is passed through unsorted. It is called before anything opens a
  socket, and it declines rather than guesses.

### 53.3 The AI workloads and the Gemini pool

* Each configured key is a **separate account/project**; a duplicate key is
  collapsed to one by fingerprint, and `shared_credentials()` reports the rest.
* A model failure **never** disables an account; an account failure is **never**
  a model problem. `classify_error` reads the response body; when neither scope
  is named, the conservative reading is a model limit.
* An unrecognised model name returns `None` from the capability table — **never**
  assume multimodal.
* Discovery failing means **do not filter**, never *no models*. `RECOVERING`
  becomes `ACTIVE` on load.
* There must be **no "requests remaining" figure anywhere**; the status prints
  "Not exposed by provider".
* Retries are bounded on four axes; when pooled the pool owns the retry policy
  (`attempts = 1 if pooled`); a `SCOPE_REQUEST` failure stops immediately.
* `GEMINI_POOL_MAX_ATTEMPTS` is a **total shared across accounts**, not a budget
  for the first one: each account is capped at
  `max(1, budget // accounts_remaining)`, so every account gets a turn before any
  account gets a second round. **Never** spend it depth-first — that made four
  configured accounts behave as one, and reported `attempt_budget` when the truth
  was that accounts 2–4 were never asked. **Never** fix a distribution problem by
  raising the ceiling.
* Pool events are recorded and **never announced**: the pool must not import
  telegram, `Pool.record()` stays synchronous, and nothing replaces the notices
  — no queue, no digest, no filter.
* `/pool` is owner-only and is the only way pool state reaches a human.
* `GEMINI_TIMEOUT_SECONDS` stays at the API's 10s floor; **never** raise a
  deadline to green a metric, and **never** trim the fallback model list on
  selection-effect data.
* Two deadlines, and they are different numbers: `Pool.timeout` is our
  `asyncio.wait_for` bound and may be short, while the `HttpOptions.timeout` the
  SDK client is built with is the **API's** and may **never** go below
  `config.MIN_GEMINI_DEADLINE_SECONDS` — the API refuses such a request per call,
  so it would present as every account failing at once. `_deadline_ms` is the one
  place that floor is applied, and the client cache is keyed by its result.
* Each workload keeps its own `_recent_calls`, `_consecutive_failures`,
  `_circuit_open_until`, `_client` and `_client_key`; no workload imports
  another, and no workload touches another's counter table.
* `GEMINI_KEY_MANAGED_WORKLOADS` is a closed set and is deliberately not an env
  var.
* The number of accounts is not hard-coded, and a shared-pool key is used only
  when that workload's own opt-in is on.
* **Credential allocation is priority- and health-aware, never a fixed
  percentage.** Chat and Awareness are the two highest-priority Gemini
  workloads, and together they receive the dominant share of healthy capacity —
  **Chat normally the larger share**, because it is the primary conversational
  workload and a person is waiting on the reply. **Awareness must never be
  starved, left empty, or given token capacity**: it is not optional and must not
  be treated as disposable. The lower-priority workloads (`intent`, `moderation`,
  `transcribe`, `tts`, `live_voice`, `search`, `memory`) keep the capacity they
  genuinely require for their measured demand and for their isolation, but must
  **not** consume healthy capacity that Chat or Awareness needs without evidence
  of demand. When new keys are added, evaluate **every** reachable key by actual
  health, quota/rate-limit state, recent failures, cooldown/bench state, workload
  demand and isolation **before** assigning it — and **never** move a key that is
  failing, cooling, benched or quota-exhausted merely to satisfy a count. If
  runtime evidence shows a different split is right, follow the evidence rather
  than a hard-coded number. **Never** revert to putting nearly every healthy key
  into Chat while starving Awareness. See §54.15 for the 2026-09-24 rebalance
  that established this.

### 53.4 Acquisition

* GuardBot must **never** hold a VPN credential, **never** talk to the 3x-ui
  panel, and **never** put a subscription URL, UUID, `pbk` or panel client name
  in a group message.
* The signing string is a shared fixed vector; change it in **both** repos and
  both tests together.
* `network_mode: host` is required; reverting to bridge needs
  `INTERNAL_API_HOST=0.0.0.0` **and** a firewall rule together.
* Intent rules are **data**; normalise before matching; a bare VPN mention is
  not an intent; `ignore` is a hard veto; the cooldown must live in SQLite, not
  memory.
* Classifier order: veto final → rule match is a decision → candidate gate →
  only then the model. The model can **never** overturn a rule match or a veto.
* The model picks a key; every word the group reads is a constant in
  `config.py`.
* `ai_intent.MIN_DEADLINE_SECONDS = 10.0` and the clamp must reach
  `HttpOptions`. Function calling is disabled explicitly.
* **Never `if ai` — always `ai is not None`.**

### 53.5 Authority and administration

* `OWNER_USER_ID` is **compared, never looked up**; no function may create,
  modify or remove the primary authority; `OWNER_USER_ID=0` refuses every
  administrative command.
* `rbac` **never** reads a username; a username where an id belongs is a
  `ValueError`. Keep `OWNER_USER_ID` in step with the VPN bot's `ADMIN_IDS` /
  `EXEMPT_TELEGRAM_IDS`.
* Authorisation compares **permissions, never role names**. Order: `no_owner` →
  `missing_permission` → `owner_protected` (unconditional, **for everybody
  including the owner**) → `higher_rank` (equal refused).
* Application permissions can only **restrict**; a test asserts every
  `PERMISSION_TELEGRAM_RIGHT` name is a real field.
* Promotion reports three outcomes and **never** claims success it did not get;
  demotion clears every flag. The promote callback re-authorises; `_unmask`
  decodes and does not authorise.
* `admin_service.execute()` is the **only** place that performs an
  administrative action; both interfaces end there.
* `AdminRequest` must have **no** `is_owner` / `actor_role` / `allowed` field,
  and the actor must be re-resolved from `actor_id` via `rbac.resolve()` on
  **every** call.
* A gated operation (`Operation.needs_confirmation`) **never** executes on the
  model's say-so: from `INTERFACE_AI` it is recorded and answers
  `admin_awaiting_confirmation`, and nothing runs until the owner releases it.
  The gate sits **after** the whole authority pipeline, so a proposal the
  proposer could not make is refused before anything is written.
* The gated set is **exactly** the six assistant switches
  (`nexus`/`awareness`/`search` × `offline`/`online`) plus `promote_member` and
  `demote_member`. Moderation is **never** gated — a ban the model asks for is a
  ban, because a moderation bot that must ask permission to moderate is not one.
* A typed command (`INTERFACE_PYTHON`) is **not** gated: a person acting
  directly is the authority, and only the model has to ask.
* The confirmation is a **reference, not an approval**. What runs is re-read
  from the recorded row, so a confirming request **cannot** change the target,
  the role or the operation.
* Only the **owner** releases a recorded action, and only **once** — a
  compare-and-swap claim, so two approvals arriving together cannot both promote
  somebody. The rule lives in `agent_bridge.resolve_confirmation` and is
  **never** reimplemented; a bare approval with more than one action waiting is
  a **question**, and the model **never** picks.
* `pending_id` may reach an `AdminRequest` from a model **only** through the two
  confirm tools, and the gate is skipped for a request that carries one — so no
  other tool may ever declare the parameter, and an undeclared argument stays
  refused.
* A lapsed proposal (`expires_at` passed) is unclaimable and answers `expired`;
  the retention window drops a finished row and **never** a live one.
* `parse_write_call` takes `actor_id` and `chat_id` from the **caller**, never
  from the model's arguments; an undeclared argument is refused, not ignored.
  **Refuse rather than repair** — never coerce a missing id or role.
* `promote_member` must have **no** parameter for Telegram rights.
* Exposure is a courtesy and authority is the rule: every call is re-authorised
  whether or not it was offered, and **no write tool is offered to a principal
  with no permissions** even with `ADMIN_TOOL_GUEST_TOOLS` on.
* A target equal to the bot's id is refused; resolution **never** guesses by
  display name.
* A replay older than the window is `stale`; idempotency is `INSERT OR IGNORE`
  on `request_id`, first write wins, and a duplicate returns the stored outcome.
* `admin_service` **never** imports telegram; the Gateway Protocol has **exactly
  ten** methods.
* Only the conversational workload has administrative tools; a moderation
  verdict can **never** become a ban.
* Refusals are audited; the action vocabulary is **never** forked; the detail
  column **never** holds a message body.
* Retention is enforced on the administrative path; `ADMIN_IDEMPOTENCY_RETENTION`
  is floored at the replay window in config. `admin_service.prune()` applies
  **three** windows — `admin_audit`, `admin_requests` and `admin_pending_ops`
  — and the audit trail is **windowed, never truncated**.
* The model's judgement is **not** a security control; prompt injection is
  defanged, not solved; a refusal is only a refusal if **nothing reached
  Telegram**.
* The chat allowance is **per account**; `Pool.daily_exhausted()` is the only
  question that may produce the quota message, it is `all` over accounts, and
  cooldowns are ignored.
* `daily_calls` / `daily_exhausted` / `daily_remaining` / `Pool.daily_exhausted`
  take **no clock** and read `time.time()` themselves.
* Four workloads set `daily_budget` — `chat`, `awareness`, `live_voice` and
  `search`; the rest have none, and a workload with no allowance gets no counter
  from a refund. `0` means unlimited. The charge stays in `note_request` and is
  refunded — **never** moved to after the call.

**The group boundary — the ROOM, not the SPEAKER (2026-09-24, authoritative).**
The security boundary for a Telegram group is the **room**. Nexus MUST be
allowed to chat with **all normal members** inside an explicitly
authorized/registered group. Member and administrator status do **not** gate
ordinary conversation.

* The authoritative source is the **database** allowlist — the
  `authorized_groups` table (`app/db.py`), read only through `app/groups.py` and
  `main.authorized_group()`. `config.GROUP_IDS` is a **bootstrap, not the rule**:
  it seeds the table **once**, on the first boot against an empty table, and
  thereafter the table is authoritative. A **soft revoke** keeps the row, so the
  one-time seed can never resurrect a room the owner turned off.
* A room is registered by an **explicit server-side act** — the Owner, or a
  server-side administrative workflow (`register_group` / `unregister_group`
  through `admin_service.execute`, and `/registergroup` / `/unregistergroup`).
  Being **added** to a group, being made an **administrator** in it, its title
  or username, a member's display name, and anything a member **claims** are
  Telegram facts and are **not** authorization.
* **Registered → every member may chat.** There is **no** per-member gate on
  ordinary conversation: `nexus.accepts_in_group(room_authorized=...)` consults
  the room and the online state and **nothing about the sender**.
  `NEXUS_ACTORS_ONLY` is retired as a gate and read nowhere.
* **Unregistered → Nexus does not participate at all.** The boundary is enforced
  **before any Chat/AI work** — before the identity write, the awareness capture
  and every model call — so an unregistered room produces **no** `chat.reply` and
  **no** Gemini request. `_awareness_run_room` and `_awareness_pass` check it
  too, so a stale awareness row for a removed room is not read or answered.
* The boundary is **fail-closed**: with no registered rooms, nothing is
  authorized. A read that cannot be answered is **no rooms**, never all rooms.
* The key is the **canonical numeric chat id**, coerced on the way in, so a
  string form and an int form of the same room are one tenant.
* Authorization is **server-side and deterministic**; the model never decides
  whether a group or a member is authorized, and an AI verdict is never an
  authorization decision.
* **Private chat is unchanged**: it is the owner's alone
  (`nexus.accepts_private`), and a group boundary does **not** widen it.
* Being able to **talk** is not being able to **act**: a member is answered but
  offered **no** write tool; every tool call is re-authorised from the actor's id
  by `admin_service` exactly as before.

### 53.6 The audit trail and identity

* `_record()` coerces anything unrecognised to `python`; nothing else may invent
  an interface value.
* `_ensure_column` is idempotent by construction and is called immediately after
  the schema is created; the declaration carries `NOT NULL DEFAULT ''`.
* `role` is resolved from `rbac` **at write time** and never taken from the
  request. The typed-command path leaves `request_id` **empty** rather than
  filling it with something that only looks like an identifier.
* The schema must have **no column wide enough for a conversation**; `detail` is
  truncated to 300 characters; the column list itself is asserted.
* A sentinel credential must appear in **no** audit row and **no** log line,
  including the boot report.
* `recent_refusals` reads the **audit table**, not an in-memory counter; a
  duplicate is not a refusal; each line ends `via=<interface>`.
* The identity handle is **not** derived from the Telegram id, is global not
  per-chat, and is **not authority** — nothing reads a uuid to decide anything.
* Resolution **never** guesses; two matches return `ambiguous` with candidates
  and **no** `identity` field; name matching is exact and normalised and
  delegates to `people.py` — **never** grow a second, weaker matcher. **Never**
  mint on a read path.
* The resolution counter is bumped in the public `resolve` wrapper and is
  guarded; a counter that raises must still leave the lookup correct.
* There is **no `execute_sql`** and no parameter becomes SQL text; nothing does
  `SELECT *` into a return value.
* Every string that leaves `agent_data` passes through `redact`, which delegates
  to `agent_bridge.redact` — **one** pattern list.
* Read tools carry **no** `chat_id` / `actor_id` / `target_id` / `permissions`
  parameter.
* `run_read_tool` refuses any tool `tool_names_for` would not have offered the
  same principal; exposure and enforcement are one function **on purpose**.
* The database runs in **WAL** with `synchronous=NORMAL`, and both pragmas are
  issued from `db.init()` on **every** start — `journal_mode` belongs to the file
  and survives a restart, `synchronous` belongs to the connection and does not.
  Measured, not assumed: the eight commits an ordinary group message costs fell
  from 63.0 ms to 1.7 ms (assistant.md §17.4.1). Do not raise `synchronous` back
  to `FULL` to "be safe" without saying so out loud — it is a 11× latency
  regression, and the trade is documented in `admin-and-audit.md`.

**Tenant isolation (2026-09-24).** One **shared** database, never one physical
DB per group, with strict **group/tenant scoping** of every group-bearing store.
This is a security and privacy boundary, not a performance optimisation.

* The tenant key is the **canonical numeric `chat_id`** on every group-scoped
  table: `group_messages`, `awareness_state`, `conversation_state`, `user_memory`,
  `people`, `agent_tasks`, `admin_audit` (`chat_id`), `admins` is **global** by
  design. Every group-scoped query is bound to its `chat_id`.
* A Telegram user has **one canonical global identity** — the numeric user id
  (and the internal uuid) — but their **group-specific context and memory never
  mix across groups**. The name memory is **per room**: `people.resolve` and
  `identity._names_for` read only the rows for the room they were asked about.
  Group membership (`chats_seen`) and the audit trail (`recent_audit`) are
  **group-specific** facts and are returned **only** on the unscoped operator
  view — never assembled into a group's prompt.
* The coding-agent task id is **content-derived from `(chat_id, actor_id,
  repository, task, minute)`**, so two groups cannot collide on a request id or
  read each other's task or result; `agent_task_view` refuses a chat mismatch.
* Analytics are scoped: `awareness_summary`, `group_pending`,
  `group_role_counts` and `awareness.metrics`/`metrics_line` all take an
  optional `chat_id`, and a room's `/nexus status` passes its own id.
  `admin_service.recent_refusals`/`status_report` take `chat_id` and the
  group-facing `/pool` passes `room.id`.
* Retention row ceilings are **per group** (`ROW_NUMBER() OVER (PARTITION BY
  chat_id …)`), so a busy group can never evict another group's rows. The **age**
  bound stays global, because "nobody has seen this person for months" is a fact
  about the person, not the room.
* The design scales to a future **physical** per-tenant split without a redesign:
  every read is already bound to one `chat_id`, so a tenant can be migrated by
  moving its rows rather than by rewriting the queries.
* An **optional** context source (Memory, Awareness) is **non-fatal and off the
  sync path**: it must never delay or crash chat, and each source is
  independently reachable (see `feedback_optional_sources_non_fatal`).

### 53.7 Nexus and awareness

* Nexus is a **role**, not a model or credential; nothing in `nexus.py` names a
  model.
* Identity comes from the Telegram id and **nothing else**; `rbac.resolve` takes
  one integer argument.
* `looks_actionable` is a **timing hint only** — it can never make a message
  relevant or acted on.
* Nothing may fake the observation capability; if the bot is demoted,
  observation stops and the report says why.
* An unreadable stored state falls back to **online**; state changes only through
  `admin_service.execute` needing `nexus.control`; `nexus.set_state` contains
  **no** permission check; while offline the AI interface is refused but the
  typed commands are not.
* A negation or contradiction in a spoken state command resolves to **nothing**.
* `people.py` grants **nothing**, **never** guesses (exact normalised
  comparison, never similarity), and stores no conversation. Queries under three
  characters are refused.
* `nexus.control` is in **no** role bundle and cannot be expressed in a grant.
* The owner-only permissions are **appended** to the end of `PERMISSIONS`, never
  inserted in the middle: that tuple is the promotion dialog's bitmask by index
  (`main._MASK_PERMISSIONS`), so inserting anywhere else silently re-points every
  stored mask. The current tail order is `nexus.control`, `agent.request`,
  `vpn.read`, `vpn.manage`.
* **No store contains a message body** except the bounded conversation history.
* Deterministic gates are for **infrastructure and security only**; relevance,
  action and speech are the model's exclusively.
* `awareness.due` **cannot see messages** — its signature is asserted.
* The window preserves order, carries the sender id and a **server-derived**
  role, is keyed by `chat_id` alone, and applies **both** a count and a character
  bound.
* `NEXUS_AWARENESS_WINDOW_MESSAGES` is **sized against the pass cadence**;
  lowering it below the interval re-introduces silent loss.
* Media is recorded as its **kind**, never as bytes.
* `calendar` renders **first** and derives only from `Ctx.now`, **Tehran not
  UTC**.
* A block with less room than `MIN_BLOCK_CHARS` is not rendered; a source that
  raises is logged and skipped.
* Adding a context source is adding a `Source` to `SOURCES`; neither `blocks`
  nor its caller changes. (`blocks` gained one optional argument in T — `skip` —
  for the addressed conversation, which borrows the same reading minus the
  blocks its prompt already states and minus the database-backed room memory it
  does not pay for. A *source* is still one tuple entry.)
* **The server's reading reaches both consumers of the same room.** The
  awareness pass renders `awareness_context.blocks(ctx)` for its anchor; the
  addressed conversation (`main._room_reading`) renders the same blocks for the
  message being answered, skipping `CONVERSATION_SKIP` (the date and the room
  the chat prompt already states, plus the three database-backed sources). The
  window is read **once** and handed to both the transcript and the reading
  (`awareness.room_block` takes `messages`), so a reply costs one window query,
  not two, and **+1 read** for the roles the readers need. It adds **no** model
  call and **no** schema: the reading is a pure function of the persisted window,
  the anchor and `rbac`, so it never needs to be stored. It is context — the
  answer goes out exactly as before when it cannot be built.
* Long-term user memory (`app/memory.py`) is **explicit-only**: a clause is
  stored **only** when a person asks to be remembered, matched by a deterministic
  trigger over the message they typed. The server **never infers** a fact from
  ordinary conversation — an ordinary «من ادمینم» stores nothing — because
  extracting one would need a model call the evidence rule forbids and because a
  guessed fact can be wrong about a real person.
* A memory **grants nothing**. It is a sentence for the model to read; no
  authority module imports `memory`, and nothing gates a reply, an action or a
  permission on it. Authority stays in `rbac`, resolved from the Telegram id.
* A memory is keyed by **`(chat_id, user_id)`**, so one person's memory is never
  another's, one group's is never another's, and a private-chat memory can never
  render in a group. Isolation is by construction — there is no read that does
  not name the room.
* The store is **bounded three ways** — `NEXUS_MEMORY_MAX_PER_USER`,
  `NEXUS_MEMORY_RETENTION` and `NEXUS_MEMORY_MAX` — applied on the observation
  path (no scheduler). The per-person delete is the indexed one and runs on the
  write; the whole-table bounds run every `PRUNE_EVERY` recordings. The value is
  clipped to `NEXUS_MEMORY_VALUE_CHARS`, and the rendered block to
  `NEXUS_MEMORY_CHARS` including its own framing line.
* The memory block is rendered as **the person's own words**, never as a fact the
  server asserts, and only for the person the batch is **about**
  (`ctx.anchor_id()`), never for whoever happened to speak.
* **Automatic memory is a second write path, never a replacement.** The explicit
  request remains the strongest legitimate signal; `memory.observe` handles it
  first, through the same `remember`, and both paths share the same validation,
  bounds and isolation.
* Automatic extraction is **a statement, not a guess**: a closed vocabulary of
  slots (`memory.SLOTS`) matched by deterministic rules over the message the
  person typed. A transient state, a question, a request and a statement about
  somebody else are each a **whole-message refusal**, not a filter — so
  "I'm tired today" cannot become "tired: yes" and "my brother is a programmer"
  cannot become the speaker's occupation. The rules read statements about *what*
  somebody is, never how they feel; no personality or sensitive trait is inferred.
* **The slot is the key**, so a new value for the same slot replaces the old one:
  create, update, replace, merge and deduplicate are what a slot-scoped upsert
  *is*, not four algorithms. The vocabulary is closed and small, so a person's
  automatic memories are bounded by the vocabulary rather than by how much they
  type — the store stays a fact set, never a log.
* **Behaviour is counted, not assumed.** A style (`style.playful`,
  `preference.style`) is remembered only after `NEXUS_MEMORY_SIGNAL_THRESHOLD`
  observations, in a separate bounded counter table that holds a number and never
  a message, and decays by age (`NEXUS_MEMORY_SIGNAL_RETENTION`) so a person who
  was playful a year ago is not labelled playful for ever. One playful message is
  not a personality.
* **Humour is a stated preference and nothing else.** The humour slots are
  reached only by an explicit statement; the material is never stored, and the
  flag is not permission — it changes no safety rule, authorises no content and
  grants nothing. The chat layer's own policy is unaffected by it.
* Model output is **untrusted candidate data**. The seam is off unless
  `NEXUS_MEMORY_EXTRACT_MODEL` **and** an account in the isolated `memory`
  workload both exist; `memory.validate_candidate` checks the slot against the
  closed vocabulary and the value against the same rejections — including on the
  value *as the model wrote it*, so stripping a leading "my " cannot hide
  somebody else's attribute. Nothing reaches the table without passing it.
* The `memory` workload is **isolated end to end**: its own key slots, models,
  timeout, retries, backoff, breaker, daily allowance and counters
  (`app/memory_extract.py` imports no chat, awareness, intent, moderation, search,
  transcription or voice module). A memory backlog cannot spend, delay or exhaust
  the request somebody is waiting on.
* Memory is **off the answer path**. `main._schedule_memory_observation`
  schedules `memory.observe` as a background task, so no extraction, write,
  provider call, retry or failure can delay a reply; the worst case is that a
  memory is not learned. The only synchronous work is the bounded read and
  render (measured **0.06 ms p50** against **0.0004 ms** with the feature off).
* Memory is **independent of awareness**. The memory block reaches an addressed
  answer through the room reading when that reading exists, and through
  `main._memory_context` when awareness is off or the reading cannot be built —
  so turning awareness off (or losing it) does not take the person's memory with
  it, and the block is never duplicated.
* Retrieval is **relevance-first**. Rows that describe the person — explicit,
  identity, preference, style, humour — are always relevant; an **interest** must
  be mentioned to be shown, so a favourite game does not appear in an answer
  about a programming project. Relevance is a deterministic lexical overlap plus
  a small per-slot hint table, never a model judgement.
* The automatic path writes only against a **closed slot vocabulary**, so a
  hallucinated slot from the model seam is dropped rather than stored under a new
  key: the server owns the allowed keys, the value size, the item count, the
  scope, the retention and the storage limits.
* Conversational state (`app/state.py`, increment X) is **one active row per
  `(chat_id, user_id)`** — a topic, a goal, an unresolved question and a status,
  never a transcript. There is no column a message body could fit in, and the
  row *is* the bound: "how many tasks" has one answer. It answers *what the
  current interaction is trying to accomplish*, which is a **different layer**
  from Memory (a durable fact about the person) and from Awareness (what the room
  is doing).
* State is **not Memory and not Awareness**. A state transition writes no memory
  row and a memory writes no state; the state block is rendered from its own
  `conversation_state` source and is never merged with the memory or room blocks.
  A state that names authority — «بیا پنل ادمین رو درست کنیم» — **grants
  nothing**: `rbac` and `admin_service` do not import `state`, and authority
  stays resolved from the Telegram id.
* State is **deterministic and costs no request**. The transitions are regexes
  over the message; there is **no model seam and no `state` Gemini workload**, so
  State cannot spend the request an answer is waiting on. `tools/eval_state.py`
  asserts the model-call count is 0.
* The lifecycle is **explicit**: `activate` / `update` / `replace` /
  `complete` / `reset` / `continue`, with a closed status vocabulary. A
  **completion or a reset clears the row** rather than leaving a finished task
  looking active; a new task **replaces** the old one (one row, never a growing
  set). An ordinary message — a greeting, an acknowledgement, a reaction, a
  claim of authority, a request for an action — matches nothing and changes
  nothing.
* State is **off the answer path**. `main._schedule_state_observation` schedules
  `state.observe` as a background task (the same `_schedule_background` the memory
  observation uses), so no read, write or failure can delay a reply. The only
  synchronous work is the bounded read and render (measured **0.06 ms p50**
  against **0.0016 ms** with the feature off).
* State is **freshness- and supersession-gated, not lexically gated**. A task
  older than `NEXUS_STATE_TTL` is not "current"; a message that starts a new task,
  completes the current one or resets the subject **withholds** the old state
  rather than showing it beside the fresh input. Relevance is deliberately **not**
  word overlap, because continuation is pronominal — «خب الان قدم بعدی چیه؟»
  shares no word with the topic — and a lexical filter would drop exactly the
  continuations State exists to serve.
* The write is **compare-and-swap on a version**, so an older background worker
  whose read is already stale has its update **refused** rather than clobbering a
  newer state; and the message id makes a **duplicate delivery a no-op** rather
  than a second transition. Both are counted in `tools/eval_state.py`.
* State is **keyed by `(chat_id, user_id)`** and read only by naming both, so one
  person's task is never another's, one group's is never another's, and a private
  task can never render in a group. It is **independent of awareness**: it
  reaches an addressed answer through the room reading when that exists and
  through `main._state_context` when awareness is off or the reading cannot be
  built, and it is never duplicated.
* `awareness.record` runs on **every** completed pass; `wants_to_speak` is
  `respond` **or** a write that actually ran.
* `parse_decision` returning `None` means **say nothing** — never send the raw
  text; `respond: true` with an empty message becomes `false`.
* The decision carries a **structured understanding** — `intent`, clamped to
  `awareness.INTENTS`, and `about`, a claimed user id. Both are **recorded, never
  obeyed**: nothing gates a reply, an action or a permission on them. An unknown
  intent normalises to `other`, and a claimed id is dropped to 0 unless
  `awareness.about_in_window` finds it in the window — a model that names
  somebody the room never mentioned has not read the room. The row's
  `about_user_id` is rendered back as "About then: …" in `memory_block`, read out
  of the stored participants rather than looked up again.
* `awareness_state.intent`/`about_user_id` were added **after** the first deploy
  and reach production through `_ensure_column`, not `CREATE TABLE`; they are
  additive and a row from before them reads as `''`/`0`.
* `db.group_pending` must exclude `role = 'nexus'` — the watermark is a
  **conversation** watermark.
* The owner is identified only by `OWNER_USER_ID`; the roster is stated by the
  server, bounded (`ROSTER_MAX = 12`), and says "may ask for", **never** "may
  do".
* `app/awareness.py` imports `config`, `db` and `rbac` and **nothing else**; a
  role in the window is a label, **never** a check.
* `awareness.anchor` picks the message the pass is about; a member's trailing
  message can **never** become the anchor while an administrator's instruction
  is in the batch.
* Awareness has its **own credential with no fallback**, its own allowance,
  breaker, counters, model and instruction.
* **OFF means off on every path**, each gated separately.
* `awareness.named` is asked **before** Nexus's own name; `command_from` is a
  dead-man's switch; the named phrases are consulted only when the message names
  a layer (`names_layer` defaults `False`); **negations win over everything**.
* The awareness trace (`PassTrace`) logs **durations only**, never content.
* `_awareness_ready_at` is cleared in the pass's `finally`; a room in flight is
  **never** scheduled twice.
* **Never** add a keyword detector, reply to everything, trade a security
  property, run concurrent passes, or drop to a smaller model.
* The awareness gap is **derived** from the remaining allowance and the seconds
  left in the API day, floored at the minimum interval; the brake is **per
  room**; and the check sits **in front of** the transcript render.
* `db.ai_day_seconds_left` uses the same UTC-8 offset as `db.ai_day`. The chat
  timing line logs durations only, and logs **exactly one** line per exit that
  reaches the clock.
* **Do not** advance the awareness watermark past an addressed message to
  suppress a re-answer; `_nexus_addressed` is set **before** the answer is
  awaited and cleared if nothing went out.
* The update guard runs in handler group `-1`, raises `ApplicationHandlerStop`,
  **never** advances a watermark, and refuses a missing or zero `update_id`.
* In a private chat Nexus answers the owner and **nobody else**, and it is **not
  a setting**: `accepts_private` checks offline first and then `is_owner`, the
  group room boundary cannot open it, and being an administrator cannot open it.
* The private gate runs **before** `_answer_conversationally`: no model call and
  **no row written** for a non-owner.
* `app/referents.py` is **evidence, never a decision**: it ranks who a pronoun
  may mean and reports `ambiguous` instead of picking. The model chooses, and
  the chosen id is re-authorised from the actor's Telegram id like every other
  request. It is pure at import time — no `db`, no `config`, no pool, no `rbac`
  — and a test asserts the import set.
* A **reply edge is the answer, not a hint**: `resolve` reports it `confident`
  outright, and no other candidate may make it ambiguous.
* An **anaphoric** expression — «همون»/«اون» (`distance == "far"`) or the object
  clitic (`KIND_CLITIC`, e.g. «ساکتش کن») — is settled by a room whose replies
  have **all** been aimed at one person, and there is **more than one** of them
  (`_about_focus`). A split room, a single reply edge, a bare «این» and «قبلی»
  are **not** anaphoric and stay hints among hints.
* The **speaker is not the referent**. The runtime hands the resolver the window
  **including the anchor** — that is why `_recent_scores` skips the anchor's own
  user — and the **role signal skips them too**: the owner who says «ادمینه رو
  محدود کن» holds a role, and offering them as a candidate for «ادمینه» made an
  unambiguous instruction render as *"could not tell the top candidates apart,
  ask"*. The exclusion is for the role **inference**, not for the explicit
  facts: a name, a stated id and a reply edge still name whoever they name.
* A **role signal never runs, replaces or overrides the anaphoric/conversational
  convergence**, and two holders of a role are an **ask, never a choice**. The
  two mechanisms are disjoint *by construction*: the role signal runs only for
  `KIND_ROLE` and never calls `_about_focus`, which is gated on
  `expression.anaphoric()` (far demonstrative / object clitic) and needs ≥ 2
  replies all aimed at one person. So «ادمینه» with two admins reads `ambiguous`,
  recency only **orders** the candidate list, and an anaphor on the same room is
  settled by convergence while the role holders stay out of the reading. A role
  must never be chosen *because* it is a role; the benchmark floors
  `role_two_admin_confident_cases == 0` and `role_focus_used_cases == 0`, both
  read from the candidate **evidence**, not the verdict (§61).
* A block **states what its reader found and does not order the model about a
  fact its reader does not hold**. Two of the prompt's orders were replaced by
  evidence for this reason: the entity block's closing line (§54) and
  `objects.render`'s "Do not read it as aimed at anybody in the room" (§59). The
  object line knows the **object** side only, and it was denying people that
  `referents`' **explicit** sources — the reply edge, a stated id, a name — had
  already named beside it, because those sources are kept *precisely* for the case
  where the request acts on a thing.
* The **benchmark scores the resolution the prompt renders**. `evaluate` resolves
  with the same `messages` (window **+ anchor**) and the same `roles`
  (`awareness.roles_for`) the renderer passes, and the harness's world is built
  from **every row the corpus labels**, not only the anchors — `roles_for`
  overrides a row's own `role` field, so a window speaker the corpus calls an
  admin but the world calls a member is a different room than the corpus
  describes. It used to resolve the window *without* the anchor and with no
  roles; the two readings disagreed on five cases.
* The resolver is **bounded** (`CANDIDATE_LIMIT`) and **cheap** — pure Python,
  no query — and the block it renders is capped and labelled "evidence, not a
  decision".
* `awareness_context` may import `referents`; `referents` may import nothing that
  could send, delete, restrict or ask a model. `awareness_context` stays the
  only importer.
* The benchmark is the number behind any claim: `tools/eval_intent.py` over
  `tools/eval_cases.json`, with the floors asserted in `tests/test_intent_eval.py`
  (`wrong_confident == 0`, top-1 and ambiguity recall **and** precision,
  `provided_before < provided_after`). A change to the lexicon moves those
  numbers **on purpose**, by editing the corpus — never by loosening the floor.
* `app/discourse.py` reads what a message is **doing** (`intent`'s deterministic
  half) and which questions in the window no reply points at. It is **evidence,
  never a gate** — nothing branches on it — and it is pure at import time.
* The act vocabulary is **closed** (`question`, `instruction`, `correction`,
  `social`, `report`) and the reader **abstains** (`unknown`) when no marker it
  can defend fires. Abstention is not a failure: a wrong act in the prompt is
  worse than no act, so the benchmark floors `act_claimed_precision` and
  `act_false_positives == 0`, and reports `coverage` beside them.
* Precedence is **`correction > report > instruction > social > question`**, and
  each step is load-bearing: a correction is a statement *about* the
  conversation, a report is a quotation (reading «نکسوس گفت اینو بن کن» as an
  order is the false positive `addressing` already guards), and a greeting
  outranks the question mark inside it.
* The imperative lexicons are **explicit token lists**, never a suffix rule. A
  suffix rule read «نمیکن» as an instruction and bought nothing the lists did
  not already have; the bare «کن»/«بده» are listed as whole tokens instead.
* The **copula «ه» is not a clitic**, and it was in the clitic list the act
  reader strips before a lexicon lookup. «ه» ends a *predicate*, so peeling it
  turned questions into orders: «ادمینه کیه؟» read as "the directive «ادمینه»",
  «چرا ساکته؟» as "the directive «ساکته»", and the subjunctive «کنه» as the
  imperative «کن». The prompt then told the model an ordinary question was an
  order, quoting the copula as the word that asked for it. «ه» is removed from
  `_CLITICS`, and the colloquial question words that genuinely take a copula
  («کیه»، «چقده»، «چقدره»، «کدومه»، «کدامه»، «چطوره») are listed in
  `_QUESTION_WORDS` explicitly — the same call the bullet above records. The
  benchmark scores the artifact on the *rendered* directive, not the token:
  `act_copula_directives == 0`, the count of cases whose act sentence quotes a
  copula form of a directive word.
* `open_questions` tests the **reply edge and nothing else**, and the block it
  renders says "no reply pointing at an answer", **never** "unanswered" — a room
  answers questions without using Telegram's reply as often as with it, and the
  second phrasing would be a claim about meaning.
* The act and the open-question block are **tier-0 sources** in
  `awareness_context.SOURCES`; they read `Ctx`, never the database.
* `app/temporal.py` reads what a message's **time words** point at, and it
  reports the **direction and the granularity**, never a date. `seconds` is the
  offset the words *state* and is 0 when they state none: «چند دقیقه پیش» points
  back at a scale of minutes and contains no count, and turning it into one would
  be a claim. It is pure at import time, evidence only, and `now` is **passed in**
  by the caller — there is exactly one clock in a pass, the server's.
* The phrase table is scanned **in order and the first hit wins**, so the longer
  phrase must come first («نیم ساعت پیش» before «ساعت پیش») and the words that
  **state** a direction must come before the demonstrative forms that inherit it
  («فردا اون موقع» is the future, not the past). Both are asserted by tests.
* `TEMPORAL_NOUNS` is the **shared** fact between three readers: `temporal` builds
  its demonstrative phrases from it, `referents` will not read a demonstrative
  before a time noun as a person, and `discourse` will not read a question word
  before a time noun as a question («چند دقیقه پیش» is a duration, not "how
  many"). Each borrows it late and guarded, as every cross-module reach here does.
* The folded phrase table is **cached lazily** on first use — not at import, so
  it is folded under the same environment the reads happen in. Folding 88 phrases
  per call cost ~0.9 ms; the cache makes a read a substring scan (~0.1 ms).
* The `anchor_when` block is a **tier-0 source**, declared **last** among them:
  it is the shortest and the one that renders least often, so it is the cheapest
  thing to lose if the pass-wide ceiling ever bites.
* **The sentence must agree with the reading it came from.** The direction is
  stated in words (`_DIRECTION`) and the offset in words (`_ago`/`_ahead`), and
  both come from one `When` — so they must not point opposite ways. They did:
  the span was worded by `_ago` whatever the reading pointed at, so «فردا» reached
  the model as *"points forwards, after now at a scale of days — about 1 day(s)
  ago"*, and **8 of the 94 phrases** did it. The magnitude is now computed once
  (`_magnitude`) and only the tail differs (`_ago` / `_ahead`, chosen by
  `_span`); a `repeat` reading renders **no** offset, because its span would be a
  period rather than an age. The window's own age keeps `_ago` — it is always in
  the past. A **property test over every phrase** holds it, not a sample.
* The benchmark scores the **sentence**, not only the reading:
  `when_prose_contradictions` must be `0`, and `when_prose_cases` must stay ≥ 20
  so the check cannot pass by rendering nothing. `when_ok` compares `kind` and
  `unit`, which is exactly why this defect survived: the reading was right and
  the sentence was wrong.
* `app/room_state.py` reads **who is talking to whom** and whether the anchor
  **continues the thread**. The reply graph and the focus are the room's own
  record — a stored `reply_user_id` column and a count over it — while the thread
  is a reading of meaning (content-word overlap) and carries its evidence. It is
  evidence, never a gate, pure at import time, and `awareness_context` stays the
  only importer.
* **One reply edge is not a convergence.** `RoomState.converged()` needs **more
  than one** reply aimed at the same person; the single-edge case reports the
  edge and says "that is not a convergence" rather than borrowing the word.
* The thread reading **abstains** unless the anchor carries at least
  `MIN_TOPIC_TOKENS` content words. «باشه» shares nothing with anything, and
  reading that as "the topic changed" would fire on half the traffic in a room.
* The **stopword list, not a length cutoff**, removes function words. A length
  floor of three dropped «چک» — two characters, and exactly what a message about
  a file is about — and made «فایل رو چک کن» too short to judge. The floor is now
  two, and a single character is never a topic.
* A **clitic is not a content word**. The fold turns the ZWNJ into a space, so
  «بچهها» and «بچه ها» both arrive as two tokens and «ها» passed the length floor
  and the stopword list — two messages sharing any plural noun "continued" each
  other, and the reason rendered to the model named «ها» beside the word that
  mattered. The closed plural/possessive paradigm is in `_CLITIC` and filtered
  beside `_STOP`. Only the **bare** clitic token is dropped; the glued spelling
  («بچهها» with no separator) stays one token and is a separate recall matter,
  not this one — suffix-stripping would over-strip «رها» and «تنها».
* The anchor's **own row is excluded** from "what came before", by `message_id`
  when it has one and by the `(user_id, at, text)` triple when it does not — or
  its own words would overlap themselves and every message would look like a
  continuation. A message that arrived **after** the anchor is not prior either.
* `reply_graph` and `thread` are **tier-0 sources**; they read `Ctx`, never the
  database. Each calls `read_state` itself rather than sharing a cached one: a
  source that raises must cost only its own block, and the scan it repeats is a
  pass over rows already in memory.
* `app/entities.py` answers the question `referents` cannot: what a demonstrative
  points at when it points at **a thing** — the media row, the link, the message
  it replies to. It is evidence, never a gate, pure at import time (no `db`, no
  `config`, no `pool`, no `rbac`, and **not** `media`), and `awareness_context`
  stays the only importer.
* A media row is read from its **stored `kind` column** first and the `[kind]`
  text prefix only as a fallback: the same fact is written in both places, and
  the column can be empty on a row captured before it existed. A link is a URL
  and only a URL — the scheme form or a bare `www.` host — because a rule that
  guessed at bare domains would match ordinary Persian words with a dot in them.
* The reply target is stated **only when the anchor names a message** («این پیام
  رو پاک کن»). A reply edge always has a target, so pointing at it unconditionally
  would print the transcript's own text back to the model on every reply.
* `thing_kind` is the **single-token** form of the noun lookup, exported so
  `referents` can ask about one position without re-tokenizing or keeping a second
  copy of the list. The table holds **stems**, and exactly **one** clitic is
  stripped — accepted only when the stripped form is a known noun, so «فایده»,
  «عکاس» and «پیامدش» are not invented into things.
* `referents` will **not** read a demonstrative immediately followed by a thing
  word as a person pointer («این لینک», «اون عکس», «همین پیام»), exactly as it
  already refuses a time word. The check reads the **raw** token: this module's
  stripper turns «پیام» into «پی», so a stripped token would never reach
  `thing_kind`.
* The word that guard reads is `entities.thing_word`, **not** `thing_kind`: the
  union of the room-held kinds and the **domain's own thing nouns** («کانفیگ»,
  «سرور», «تنظیمات», «اشتراک», «اکانت», «پنل», «لایسنس», «کانال», «سرویس» and
  their Latin forms). The room holds no «کانفیگ» as a row, so `thing_kind` and
  the entity block are **unchanged** — «همون کانفیگ رو بده» binds «همون» to a
  config and is not about anybody, and offering the room's members for it is the
  same wrong lead «این لینک» was. `thing_word` widens the **resolver's** reading
  only, and a person noun immediately after the demonstrative still wins.
* The benchmark reports the resolver's error in **both directions**:
  `expression_false_positives` (the corpus says no person, the resolver offered
  one — the wrong lead) and `expression_false_negatives` (the safe direction).
  The floor is `expression_false_positives == 0`.
* The entity block is a **tier-0 source** (`entities`, budget 600) that reads
  `Ctx`, never the database, and renders **nothing** when there is nothing to
  point at and nothing named — a block saying "no things found" would spend
  tokens to tell the model what the transcript already shows.
* The entity block states the things and then says **"not about a person"**: it
  is the correction the resolver's person-candidates need, because acting on a
  person when the message was about a photograph is the worst mistake available
  here. It states it as **evidence, never as an order** — the module's own
  contract — and the order belongs to `app/objects.py`, which knows the side
  because the verb decided it. The block closed with "do not act on a person
  unless the message names one" for a dozen increments; under the resolver's
  ranked people it ordered the model to disregard the block above it, and nothing
  could see it because the two blocks had never been rendered together. The
  benchmark now assembles the context and fails if the block gives an order.
* **The entity block's correction has conditions, and it is a claim like any
  other.** Two rules narrow it, and both are in `Entities.offered`:
  * **the message must point at something.** The header says the message *may
    point at* the things under it, so it may not appear for a message that
    carries no pointing expression — a greeting, a bare «پاک کن». This is the
    same rule `app/objects.py` states for the object it reports ("a bare «پاک
    کن» points at nothing, and the room's newest photograph is not its object
    just because the room has one"), applied to the candidates this block
    offers. `pointing` is a demonstrative **or** an expression
    `referents.find_expression` reads (which is how «پاکش کن» qualifies with no
    demonstrative at all).
  * **the request must not act on a member.** `«ساکتش کن»` asks for a member to
    be muted, and offering the room's photographs beside it — carrying "do not
    act on a person unless the message names one" — says the opposite of the
    object block printed next to it. `_acts_on_a_person` is the mirror of
    `referents`' guard with the same three clauses, so a message that asks for
    both (`«ساکتش کن و اینو پاک کن»`) keeps every candidate.
  * Both rules narrow the **rendering only**: `read_entities` still reports
    every item and `Entities.of_kind` still sees every item. And both readings
    are taken **only when there is an item to narrow** — with no candidates they
    cannot change anything, and the lexicons they borrow are most of the
    reader's cost (measured: +60 µs/call unconditional, +7.6 µs/call taken only
    where they can matter). `tools/eval_intent.py` scores the rendered block
    against the reader's own evidence and fails on either count, and its
    non-vacuity floor keeps a guard that suppressed everything visible as such.
* **The benchmark assembles the context, and no source may be silently dead.**
  `tools/eval_intent.py` builds a `Ctx` per case the way `main._awareness_context`
  does and counts which sources render. `referent_candidates` read **0 of 127**
  until this existed — `_wants_referents` asks `is_authority`, which reads
  `rbac`, which reads `config.OWNER_USER_ID`, which the harness had never set, so
  the block carrying person resolution to the model never fired. The
  database-backed sources (`remembered_people`, `admin_activity`,
  `referenced_people`) are excluded **by name**, and the test asserts that set
  plus the rendered set is exactly `SOURCES` — so a new source must be classified
  before it can be dead.
* The Arabic block's **punctuation is a separator, not part of a word**: «؟»
  «،» «؛» sit *inside* `\u0600-\u06ff`, so a "split on anything that is not a
  Persian letter" class keeps them glued to the word before it. Every reader that
  looks a word up in a lexicon must exclude them by name, or «این لینک؟» names no
  thing, «سارا؟» names nobody, «ممنون؟» is not a greeting, and «چی شده؟» is not
  the sentence «چی شده». The pattern is **copied** into each of the five readers
  (they are pure at import, and a shared helper would be a new edge in the graph)
  and `tests/test_awareness_context.py` pins the copies together so they cannot
  drift. `addressing` is immune by construction — `_letters` keeps only
  alphanumerics — and is deliberately left alone.
* A **trailing mark never changes a reading**: `discourse`'s act, `referents`'s
  name/id matching and thing guard, `entities`'s noun lookup, `requests`'s
  prohibition and `room_state`'s content words all hold with the mark attached.
  `tools/eval_intent.py` has a `punctuation` category and a floor that fails if
  any of those regress.
* `app/requests.py` reads whether a message **asks for the action or forbids
  it** — the half-truth `discourse` cannot see, because «بنش کن» and «بنش نکن» are
  the same `instruction` with the same directive to it. It reports the directive
  (quoted, never mapped to an action category), the **polarity**
  (`affirmative`/`negated`/`""`) and the **manner** (`command`/`request`). It is
  evidence, never a gate, pure at import time, and `awareness_context` stays the
  only importer.
* The directive lexicon is **borrowed from `discourse`, never copied** — a second
  list would be a second answer that drifts. `discourse.directives` is the public
  form, and `read_act` uses the same function so the act line and the direction
  line can never disagree about *which* word made the message an instruction.
* The two negation rules point in **opposite directions on purpose**. The rule
  that **claims** a negation is scoped tightly — the prohibitor must be the token
  immediately **after** the directive (Persian: «بنش نکن»), or a negator within
  two tokens **before** it (English: «don't ban him», which the tokenizer delivers
  as «don» + «t»). The rule that **downgrades** is deliberately broad: any other
  negation in the message means the reader reports `""`, **never**
  `affirmative`, because it cannot tell what the negation scopes. Breadth is
  affordable in the downgrade direction — a false hit costs an abstention, where a
  false hit in the directive lexicon costs a false instruction.
* A **Persian word before a directive is never a negator**: «نه بنش کن» is "no,
  ban him", and the look-back window therefore lists **English forms only**. A
  Persian negation the reader cannot scope still downgrades through the broad
  rule; it never makes a claim.
* The negative past is read by a **stem rule** («ن» + a known past stem →
  «نکرد», «نگفت», «ندید», «نرفت»), not forty spelled-out forms. A bare «ن» would
  not be safe («نگاه», «نام», «نوع» all start with it), so the stem is what makes
  it a negation, and the rule only ever downgrades — «نبرد» ("battle") is a false
  hit that costs an abstention.
* The reading is about the **first** directive, because that is the one whose
  neighbourhood decides the direction. A **later negated directive** —
  «بنش کن، پاکش نکن» asks for a ban *and* forbids a deletion — makes the reader
  **abstain**, because a one-line summary cannot hold two directions and
  reporting `affirmative` for the first half would be the dangerous direction
  again.
* The polarity is rendered into the **same source as the act**
  (`anchor_act`, budget 320), and the polarity line comes **first**. An act line
  that says `instruction` while the message forbids the action is exactly the
  half-truth this reader exists for, so a budget must never be able to drop the
  direction and keep the act; `_clip` keeps whole lines from the front, so the
  warning goes first.
* A **bare affirmative command renders nothing** — the act line already says
  `instruction`, and a direction line on every ordinary moderation message would
  be noise. The block grows only where the direction is not the obvious one.
* The floors for the direction live in `tests/test_intent_eval.py`:
  `request_accuracy == 1.0`, `request_negated_recall == 1.0`, and above all
  **`request_false_affirmative == 0`** — a message that forbids the action read as
  asking for it, which is the mistake counted. A change to the lexicon moves
  those numbers **on purpose**, by editing the corpus, never by loosening the
  floor.
* `app/objects.py` reads **what the request acts on** — the join the model
  otherwise had to make itself between *"instruction, the directive «پاک»"* and
  *"things — media"*. It reports a closed class (`person`/`media`/`link`/
  `message`/`thing`), the surface word the message used, and **how** it knows
  (`named`/`pointed`/`verb`). It is evidence, never a gate, pure at import time,
  and `awareness_context` stays the only importer.
* **A named noun wins over the verb**, and the kind is **never guessed from the
  room**: «پاک کن» acts on a thing and does not say which, so the reading is
  `thing` rather than the room's newest photograph. Only a message that actually
  *points* — a clitic or a demonstrative — borrows a kind from the window.
* The split of the directive lexicon by **what each verb acts on** lives in
  `app/discourse.py`, beside the lexicon it splits, and is borrowed by
  `objects` and by `referents` (which scopes its guessing with it). Two tests
  hold it: it must
  **cover** `addressing.ACTION_WORDS`, and no word may be on both sides — so a
  word added to the moderation lexicon fails the suite until somebody decides its
  side.
* `discourse.acts_on` answers `"person"`, `"thing"`, or **`""`** — and the third
  answer is the load-bearing one. A verb in neither list (a generic imperative, or
  a word an operator added through `NEXUS_EXTRA_ACTION_WORDS` without a side)
  answers nothing and the readers that ask **abstain**. Guessing a side is the
  mistake the split exists to prevent: a guessed *person* for a message about a
  file is the worst direction available here.
* The object line renders into the **same source as the act and the direction**
  (`anchor_act`, budget 420 — the longest block the corpus produces is 350 at
  141 cases), and
  the two lines that contradict a naive reading come **first**: `_clip` keeps
  whole lines from the front, so what survives a tight budget is the warning, not
  the claim it warns about.
* `tools/eval_intent.py` reports **`object_person_offered_for_a_thing`** — a
  thing-object request for which `referents` still lists a person — and its
  **`…_wrong`** split, the leads that are not the person the corpus labels. The
  resolver guard below drove the *guess* from 5 to 0, and
  `tests/test_intent_eval.py` pins `…_wrong == 0` **and** `… >= 3`: the surviving
  leads are the explicit ones (reply edge, stated id, name), and a corpus that
  quietly lost those shapes must fail rather than read as a win. "A thing" there is the labelled
  classes, and the **abstention is not one of them**: a case whose expected class
  is `""` says the server has no reading of what the request acts on, so a person
  offered there is the resolver doing its ordinary job, not a thing-lead.
* `referents` **scopes its guessing** with `_acts_on_a_thing`: when the message
  carries at least one directive, **none** of them acts on a person, and at least
  one acts on a thing, the three heuristic sources (`_about_scores`, the
  anaphoric `_about_focus`, `_recent_scores`) do not run. They are what turned
  «پاکش کن» into a list of the room's members. The rule is
  **one-directional**: a message that carries both — «پاکش کن، بنش کن» — keeps
  every source, because losing a ban target is worse than a lead the object line
  corrects.
* The scoping is of the **guessing, never of the facts**. A person the message
  *names*, an id it *states*, and the **reply edge** still run and still identify
  the author of the thing — «اینو از گروه حذف کن» as a reply to مهدی is about
  مهدی's message. A verb nobody classified answers `""` from `discourse.acts_on`,
  and **an unknown side is unknown, not a thing**, so the guard stays out of the
  way rather than guessing.
* When the guard fires and no explicit source found anybody, the resolution
  carries **no candidates** and `render` returns **nothing at all**. "The server
  looked and found nobody" invites the model to ask which person; "the question
  does not apply" must not, and the object line in the act block already says
  what the request acts on. A corpus case that expects an **ambiguous person** for
  a thing-object request is a wrong lead by construction: `entity-media-newest`
  and `entity-media-and-link` carry `ambiguous: false`, the same reading
  `entity-media-single` already gets for the same words.
* **Increment Y's composition is selection, never retrieval.** `app/context_plan.py`
  holds **no data of its own**: it reads the blocks the existing readers already
  rendered and only *selects*, *orders*, *de-duplicates* and *bounds* them. It
  reads no database, calls no model and stores nothing — removing it leaves every
  source working exactly as it does now. There is deliberately **no**
  `UniversalContext`, no `ContextMemory`, no `NexusContextStore` and **no second
  Gemini call** to choose context; the selection is deterministic, and
  `tools/eval_context.py` asserts the module's source contains no model client.
* **The four sources stay four sources.** Conversation, Awareness, State and
  Memory keep their own readers, their own switches and their own fail-soft
  behaviour; Y only decides which of them a given message needs. Awareness stays
  optional: with the layer off, unavailable or failed, the answer still carries
  the other three.
* **A source is read only when it is wanted, and the plan enforces it.** The
  addressed path (`main._answer_conversationally`) fetches the room window and
  reading only when the reading asks for the room, and the memory/state blocks
  only when it asks for them; `compose` then **enforces the same selection**, so a
  block the reading rejected is not in the plan even if a caller rendered it.
  `ContextPlan.selected()` is the decision, not the caller's discipline.
* **`NEXUS_CONTEXT_CHARS` bounds only what it can remove.** The ceiling applies to
  the four **selectable** sources together and drops whole sources in reverse
  precedence (memory → state → room), never slicing a rendered block. The
  administrative roster, the server date and the web findings are **never
  dropped** and are therefore **never counted**: a ceiling that counted a roster
  larger than the limit would have nothing left to remove, and its only effect
  would be to strip the room out of an answer that needs it.
* **The composed context is data in the system instruction.** Y appends to the
  same system-level context the blocks already used, so the prompt-injection
  boundary is unchanged: the transcript is people's text and is never presented
  as a statement the server is making. The plan is logged as **names, reasons and
  sizes only** — never a word of the message or of a block — and is never
  persisted and never rendered to the model.
* **Y grants nothing.** Nothing in `rbac.py` or `admin_service.py` imports
  `context_plan`, and no decision it makes reaches a permission check. A memory or
  a role selected into the context is a sentence for the model to read, not an
  authority the server will honour.
* **Increment U spends the rationed request, and the decision cannot see the
  message.** `app/awareness_schedule.py` holds one word per room — the strongest
  class seen among the room's **unread** messages — and nothing else. The content
  is read **once**, at capture time, by the project's existing
  `context_plan.read`, and reduced to `high` / `low` before it is stored.
  `awareness.due` is not touched, is asked exactly as before, and its signature is
  still asserted (`tests/test_awareness.py`); the deferral is applied *after*
  `due` has said a room is eligible and *before* the allowance is consulted, and
  the urgent path (`_awareness_promptly`) never consults it at all.
* **A hint can only postpone a routine reading.** `defer` returns `True` only for
  a room whose batch is `low` and whose oldest unread message has waited less than
  `NEXUS_AWARENESS_RETENTION_SECONDS`. A room with **no** hint is never deferred
  (an unknown room and a quiet room are read the same), a room whose batch says it
  needs reading is never deferred, and a room the **server is waiting on** — a
  pending admin confirmation, `db.admin_pending_waiting` — is never deferred. It
  can never admit a room, bypass a cooldown, a brake, a breaker, the allowance or
  the switch, or execute anything: every safety gate is *above* this decision.
* **`wants_awareness` is not "the pass does not need to run".** A confirmation
  («تأیید میکنم») is self-contained, so it is correctly `low`, and the pass is
  nevertheless the thing that consumes it. This is the one place the mechanism can
  be *wrong* rather than merely slow, and the server's own waiting flag is what
  closes it. A residual remains and is recorded: a bare answer to the assistant's
  own question carries no room dependency and no server flag, so it can be
  postponed like any other chatter.
* **A broken reader contributes nothing.** `read` never raises; on a reader error
  the class is `P_NONE` (*no evidence*), which `note` refuses to store, so the
  room is read exactly as before this module existed. The wrong fallback — `low`
  — would defer every room whenever the reader broke: a deployment-wide slowdown
  wearing a scheduling choice's clothes.
* **The hint store is bounded, expiring and isolated.** `chat_id -> (class,
  stamp)`, at most `MAX_ROOMS` entries evicted oldest-first, every entry expiring
  at the retention window, and no read that does not name the room. It holds no
  message body and no numeric score — three words and a float.
* **The deferral bound is derived, not configured**: `_bound()` is
  `NEXUS_AWARENESS_RETENTION_SECONDS`, the same window whose rows a hint
  describes, so there is no second number to drift. A mis-set retention floors at
  1 s rather than becoming "hold for ever".
* **Ordering the pending list is not the mechanism, and the reason is
  architectural.** The scheduler is event-driven per room, not batch-driven, so at
  any instant there is about one candidate and nothing to sort; measured over
  eight seeds, ordering changed the outcome by exactly zero passes. The shipped
  mechanism is the per-room *spend-or-wait* decision; the rejected ordering is
  kept behind `--compare-ordering` in `tools/eval_awareness_schedule.py` so the
  negative result stays reproducible, and `tests/test_awareness_schedule_eval.py`
  pins it.
* **A claim about answer quality requires a measurement, and a measurement may
  never report a number it did not measure.** `tools/eval_chat_quality.py` is the
  only thing in this repo that scores the assistant's own text: a labelled
  corpus, a deterministic scorer with **no LLM judge**, and a harness that drives
  the real addressed path and the real `chat.reply`. A `(scenario, arm)` pair
  whose every sample was skipped or errored is **NOT RUN** and leaves every
  denominator — a gap is never scored as a zero. The report prints its own
  limits with every run: it measures text against authored rules and never tone;
  `ChatReply.model` is the **requested** model, not necessarily the serving one;
  temperature is 0.8, so it reports samples and spread rather than one number as
  a verdict. Nothing else may justify a routing change (V).

### 53.8 The assistant

* `main._nexus_directed` decides the assistant answers; the word «ربات» is not an
  address, and the name forms (`addressing.addressed`, e.g. «نکسی») are.
* A name that is the **subject of a reporting verb** is a mention, not a call:
  `addressing._quoted` demotes «نکسوس گفت که...» to the weak grade. The rule is
  **one token wide** — the verb must come immediately after the name — so
  «نکسوس بگو...», «نکسوس جان» and «میشه نکسوس اینو بررسی کنی؟» stay calls.
* A name that is the **object of a preposition** is a mention too:
  `addressing._complement` demotes «من با نکسوس کار نکردم» and «i never worked
  with nexus». Also **one token wide** — only a preposition *immediately* before
  the name — so «میشه نکسوس اینو بررسی کنی؟» («میشه» is not a preposition) and
  «با اجازه نکسوس اینو پاک کن» (the token before the name is «اجازه») stay calls.
  «to» and «for» are deliberately **not** in `_PREPOSITIONS`: both can head a
  line that addresses somebody («to nexus: ...»), and demoting on them would
  silence a real call.
* A demotion is **not a silencing**: both rules leave `mentioned` true, so the
  line still reaches the model as "your name came up here" context. Only the
  *immediate* reply is withdrawn. `_PREPOSITIONS` is a **closed grammatical
  class**, not a phrase list; `_quoted` and `_complement` share one helper
  (`_reading`) so a new strong-reading branch cannot forget to apply them.
* `on_group_text` **must** return before `classifier.classify` when the message
  will be answered — ask `main._nexus_will_answer`, **never** the narrower
  `_addressed_to_bot`, or a name-addressed message gets both a reply and a trial
  offer. The two handlers must ask the same question.
* `on_group_text` binds a local `chat`; use `main._chat_active()`, **never**
  `chat.is_enabled()`.
* Gemini limits are **per project, not per key**; a separate key buys a separate
  budget only in a different project. `db.ai_day()` is the Pacific boundary, not
  UTC.
* `chat.py` **never** reads `db.ai_*`; `ai_intent` **never** reads `db.chat_*`;
  `reply()` **never** raises.
* There is **no** code path from a reply to an action; output is HTML-escaped
  text only; the prompt forbids our own prices, plan details, links and
  credentials. A **public** figure (crypto, gold, FX, stock, index) may be stated
  **only** when it is in that turn's web search results — never from memory, never
  estimated — and the results stay untrusted data.
* **Never** ask an already-answered question, re-greet, close by offering more,
  repeat a sentence, or narrate helpfulness.
* **Do not** pretend to be human; **do not** announce being an AI.
* The repetition guard compares the **model's** turns only, exempts answers
  under 24 characters, and its extra request has its **own** budget separate
  from the transient-error retry.
* Unreadable media gets an **honest answer, never a guess**. Media turns are
  recorded as their kind; a voice turn as its transcript.
* A thinking model returning empty text means **raise the output budget** —
  never edit the prompt.
* `transcribe` is called from **exactly three** places — the awareness read, the
  conversational path and the transcription command — and a test asserts the
  count. Nothing transcribes a group voice note on arrival.
* The transcription instruction is **verbatim**, with no translate and no
  answer; `NOSPEECH` / `UNINTELLIGIBLE` only as the **whole** answer.
* Voice replies are off by default and best-effort; `_tts_request` is a separate
  seam whose failures **do not** count toward the chat breaker.
* `recent_actions_block` lists only this actor's **successful** actions in this
  room; it cannot be planted, and it is **context, never authority**.
* `TOOL_AMENDMENT` is appended **after** the persona for a turn that holds
  tools; `TOOL_AMENDMENT` stays in `chat.__all__`.
* The trusted context (`context`) **must** reach the model on **every**
  conversational path — the tool-aware one *and* the plain one. `chat._request`
  and `chat._pooled_request` therefore take `context`/`instruction` and pass them
  to `_generation_config`; a path that builds its config with the defaults drops
  the room window, the search findings and the date on the floor and the model
  answers as if the room and the web did not exist. This was a real bug: the
  plain path called `_request(contents)`, so only actors holding admin tools ever
  saw the context.
* The conversational system instruction **states the server's own date**, in both
  calendars, from Tehran (`main._today_block`). Without it the model treats the
  newest date it read — in the room or in a web brief — as today, and answers
  "امروز چندمه" from its training. Awareness has always done this for its pass;
  the direct answer path must too.
* A regression test asserts the context reaches `_request`; another asserts the
  date block is in the context the conversational path builds.

**The conversational contract (rebuilt 2026-09-24).** The persona is the
behavioural contract and lives in `chat.SYSTEM_INSTRUCTION` — the **single
behavioural source of truth** for Chat. Its behavioural reference is the
**historical Chat** at commit **`3243067`** (`app/chat.py`): warm, informal,
short, Persian, context-aware, answering the actual message. The Nexus era
appended a large trusted-context block and grew the persona into a policy
document — separate tone, joke, owner and repetition sections — and several of
them competed over the same decision. That competition produced the reply the
owner flagged: «نخند حرومزاده» answered with «چشم قربون‌سربازیت😂 بی‌خیال بابا»
— servile address, automatic laughter and canned filler instead of a reaction to
what was actually said. The rebuild keeps the historical *behaviour* on the
*current* architecture and puts **one** personality in charge. The rules, all
asserted in `tests/test_chat.py` and `tests/test_chat_behavior_contract.py`:

* **Short and conversational.** Two or three sentences is usually right; spoken
  Persian, not formal written Persian; **no** headings, numbered sections, bullet
  lists or Markdown for an ordinary reply. Do not restate the question, repeat
  yourself or what was already said, open with a used greeting, or close by
  offering more help. Say it in the fewest words that carry it — but never trim
  away the point just to be short.
* **No assistant tics.** No closing invitation to continue, no generic follow-up
  question to keep the chat alive, no self-introduction, no announcing being an
  AI unless asked. The named filler — «حتماً», «البته», «بسیار خوب», «در خدمت
  شما هستم», «با کمال میل», «اگر سؤال دیگری دارید» — is forbidden.
* **Context controls tone.** Reply to what the person is actually *doing*: a
  normal question gets a normal answer, a serious message a serious one,
  frustration a calm, direct reply rather than an apology loop, a joke a reaction
  rather than a lecture. The person sets the register; Nexus does not perform
  warmth, humour or intimacy the moment did not ask for. Stay on the topic and
  follow a subject change; keep continuity across turns.
* **Humour and register are reactive, never automatic.** Nexus may be funny,
  tease back, and use casual — even crude — Persian **when that is what the
  exchange is doing**, because the moment calls for it, not to sound human:
  **never** use laughter as punctuation («😂», «🤣», «خخخ», «ههه»), **never**
  reach for canned «بابا»/«داداش»/«قربونت», never repeat the same joke shape.
  Adult or sexual joking is answered in kind **only** when the person initiates
  it and the moment supports it — Nexus never brings that register into a
  conversation that was not already there and never escalates an ordinary
  message into it.
* **Never titles or servile address** — «قربان», «سرور», «جناب», «بنده»,
  «قربون‌سربازیت» — **for anyone, ever**. This is now a rule of the persona
  itself, so it applies to every member and not only to the owner.
* **The hard boundaries are stated:** never threaten anyone, never use slurs,
  never attack anyone's family (no «ناموسی» insults, no insults about a
  mother/sister/father/child), never humiliate anyone sexually, never attack
  somebody over who they are. If the person is genuinely upset or serious, drop
  the joking entirely and answer normally.
* **No false humanity.** No body, no real-world experiences, no memories outside
  the conversation. Natural tone, never a claim to be human.
* **The appended background is background.** The room, state, memory, date and
  search findings the server appends are material to use — not a subject to
  summarise or list, and not a change of register or topic. This sentence is the
  specific fix for the reported degradation.
* **Every safety clause is preserved verbatim** — no-human, our prices, links,
  credentials, the public-figure-from-search-only rule, the injection defence,
  the no-system-message rule. The wiring is unchanged: `_generation_config` still
  sets the persona plus the appended context, `temperature=0.8`,
  `max_output_tokens=1024`.

**The owner-aware layer (rebuilt 2026-09-24).** When the person being answered is
the owner, `chat.OWNER_NOTE` is **prepended to the trusted context** — and so
lands in the system instruction immediately after the persona. It is **data**,
not a personality: it states *who* is speaking and no rule of its own.

* Ownership is decided **only** by the server, from the configured id:
  `main._answer_conversationally` calls `rbac.is_owner(user.id)` and nothing
  else. It is never read from a username, a display name, a Telegram admin
  status, a role, or anything the speaker wrote.
* How to talk to somebody you know — familiar and relaxed, felt in continuity and
  wording, **never announced** — is a principle in `SYSTEM_INSTRUCTION`, stated
  once, alongside the ban on titles. There is no separate owner personality; an
  earlier version had one, which both competed with the persona and left a
  non-owner with no familiarity rule at all.
* It bypasses no gate — every authority check (including the group boundary
  above) has already run — and it is only ever the owner's turn that carries it.

### 53.9 The coding-agent bridge

* The container has no Node or CLI; the bridge is two processes meeting over a
  directory, and `codebuddy_task` is an `admin_service.OPERATIONS` row —
  **nothing is forked**.
* `agent.request` is held by **no** role bundle and cannot be granted.
* A request carries the repository **name**; a path is accepted only when it is
  **exactly** an allowlisted root; the runner re-checks from its **own literal
  copy**.
* The operation vocabulary is **closed**; the task text can only **add** danger,
  never remove it.
* A dangerous request is recorded and **never** written to the spool;
  `waiting_for_owner` reaches `running` only through `queued`, and an approval
  nobody answered **lapses** to `timed_out` on the poller's bound — it may not
  hold a repository's only slot for ever.
* Approval is the owner's, checked by id; a bare confirmation resolves only when
  **exactly one** task is waiting; answering a question recomputes the danger; a
  task that never started cannot be resumed.
* `agent_spool` imports **only the standard library**; the stream is append-only
  and nothing is rewritten in place.
* The runner **never** takes the executable or its arguments from the request;
  the lock is `O_CREAT|O_EXCL`; the timeout is a watchdog thread.
* The child inherits the **real** `HOME` — a fresh `HOME` makes the CLI succeed
  unauthenticated, which **must** be classified as a failure.
* `reply_plan` has **no** branch that drops the answer; the offset is written
  **after** the lines are sent; progress is throttled, the result **never** is.
* Secrets are handled three times: the agent is told, the runner redacts, the
  container redacts again.
* The bridge **never** reaches the Gemini pool; a restart **never** republishes
  a running task.

### 53.10 Integrations and credentials

* It is a **capability registry, not an integration** — never invent an
  endpoint; the three states are distinct, and an operation with no code behind
  it **must not** be named.
* The integration list is asserted against the paths `app/vpnbot.py` actually
  implements; the shared secret is read only to decide *whether* the client is
  configured and **must never** appear in the report.
* `admin_service.py` **is** the gateway; there is no second gateway, and
  `vpn_service.py` is **never** called by a Telegram handler. An operation
  against an unconfigured integration is refused **before anything is
  recorded**.
* `vpn.read` / `vpn.manage` are **appended** to the end of `PERMISSIONS` like
  the other owner-only permissions (§53.7) and are in **no** role bundle.
* The second step of a VPN write is a **reference, not an approval**: everything
  is re-read from the stored row, `pending_id` is named differently from
  `request_id`, and a forged confirmation cannot smuggle different values.
* `vpn_admin` refuses `vpn_confirm` as a value for its `operation` parameter;
  the four confirmation rules are **not** reimplemented and live in
  `agent_bridge.resolve_confirmation`.
* The claim is a compare-and-swap taken **before** the call and released **only**
  on a transport failure.
* An unconfigured or unreachable VPN bot is recorded as a **refusal** in
  `admin_audit` and **never** reported as done.
* The VPN redactor is **local** and must not be added to
  `agent_bridge._SECRET_PATTERNS` — a bare 32-hex rule would rewrite the
  identity handle. Views copy an allowlist field by field and **never** `**raw`.
* **No** new tool declares a parameter in the forbidden set.
* Every parameterised VPN endpoint is **POST with a signed JSON body**; there
  are **no query parameters**, because the HMAC excludes the query string.
* `operator_id` is **asserted** by guardbot and not independently verified by
  the VPN bot.
* `/keys` is **not** a second pool: one registry, one set of counters. The store
  is **plaintext on disk** at `GEMINI_KEY_STORE_PATH`, mode `0600`, inside the
  data volume, and must not be in the database, the audit trail, a log line or a
  Telegram message.
* Environment slots come **first**; `GEMINI_KEY_MANAGED_WORKLOADS` is a **closed
  set**, deliberately not an env var.
* The entry handler is registered in **group 0** and raises
  `ApplicationHandlerStop`; verification is **not awaited in the handler**; a
  group is refused as a place to type a key.
* A message with a space in it or under twenty characters is left alone and the
  prompt stays armed.
* Authority is re-decided on **every** press; a crafted payload can only choose
  which screen opens.
* **Nothing is stored on a failed verification**; the client is built with
  `build_client`, not `client_for`.
* **No credential** appears in the database, the audit row, any log line, any
  screen, or a probe's error detail. Deleting the store file restores the
  environment-only pool.

### 53.11 Voice Live

* It is the **same Nexus**, not a second assistant: same awareness, same
  authority model, same audit trail. It is gated by
  `GEMINI_LIVE_ENABLED=false` by default.
* `fa-IR` is first-class; the native-audio model rejects it. Joining a voice
  chat is **MTProto** and cannot be done with the bot token.
* **The feed to the provider must never stop** (the silence pump). The
  resampler refuses a mixed ratio; `to_bytes` **clamps rather than wraps**.
* An unattributed utterance has actor `0`; there is **no** method that accepts
  an identity from the model; a stale speaker is unknown.
* `VoiceActionRequest` has **no authority field**; the vocabulary is a strict
  subset; the owner-only switches, promotions, the coding agent and the VPN are
  **not reachable** from a voice chat.
* Local refusals in `actions.py` are **never** written to `admin_audit`.
* The awareness bridge is **read-only** and one `chat_id`: it will not write,
  will not cross rooms, and will not pass a secret through.
* A barge-in **flushes**; a reconnect **resumes with the handle**; a session
  that ends on its own timer **must leave** the voice chat; both `leave` and
  `close` are needed.
* The join and leave commands are owner-only and matched **before** any
  conversational path; the voice router stands down for anything that reads as a
  switch.
* An unknown failure reason defaults to **not retryable**; starting fails
  **closed**.
* **No raw audio is persisted**; no transcript is logged or stored; `Metrics`
  has no field that could hold a string.
* `py-tgcalls`, `telethon` and `ntgcalls` are declared **directly** — the
  telethon line is required because it is an *extra*, not a base dependency.
* The session file is **never** in the image and **never** printed.

### 53.12 Web search

* `search` is a **separate pool workload**; grounding is **never** switched on
  for `app/chat.py`. The integration point is `main._answer_conversationally`,
  never `nexus.py` and never `chat.py`.
* `chat` and `web_search` must **not** import one another.
* The search call declares **no function tools** and automatic function calling
  is disabled.
* Web content is untrusted data in a delimited block appended to the system
  instruction; **no second context system** is built.
* A search brief is full of dates **pages** wrote, and Tavily is sent only the
  question — it has no date of its own. The model can date a finding only because
  the conversational context states the server's date (§53.8); without it the
  brief's oldest claim reads as the newest news.
* The model is told **not** to write URLs, and the **application sends no
  sources at all**. A grounded result's `sources` are internal grounding — used
  only to judge whether the finding is usable. There is **no footer**, no «منابع»
  line and no domain list: `sources_block` does not exist and `main` has no
  `_send_search_sources`. A link reaching the group would mean the grounding
  leaked into chat.
* A grounded result with no source is `ungrounded` and gets the "could not
  check" note.
* Search has a **persistent operator switch** (`search_control`), exactly like
  the awareness layer: `search_offline` / `search_online`, both gated by the
  owner-only `nexus.control` permission, both `requires_nexus_online=False`, and
  the state survives a restart (read from SQLite, cached in `_running`). Turning
  search **off** makes `research()` return early — **no provider request and no
  credit** — while the conversation, the assistant and the awareness layer are
  untouched. The switch is handled **before** the conversational AI, so it never
  depends on a model being reachable, and `GEMINI_SEARCH_ENABLED=false` still
  wins over a stored "on".
* **Not every question searches.** The *shape* of an informational question
  («چیست/چیه/کیست/کیه/چرا/چگونه/چطور/درباره/توضیح بده/تفاوت/مقایسه/معنی/تعریف/
  کاربرد») and a bare `?`/`؟` are **not** reasons to search. Only an **explicit
  request** («سرچ کن», «جستجو کن», «بگرد», «گوگل کن», English equivalents) or a
  **genuinely time-sensitive** question («الان», «امروز», «آخرین/جدیدترین»,
  «اخبار», «قیمت/نرخ/وضعیت فعلی», current/latest/today/now/recent news) searches
  on its own.
* An **inferred** search — a live subject with no "now" (e.g. «قیمت بیتکوین
  چنده؟») — is **offered, not performed**: the bot asks «برات سرچ کنم؟» and runs
  the search only on an affirmative answer, against the **stored** topic. A
  non-answer clears the offer, so one question can spend at most one search.
* `GEMINI_SEARCH_ENABLED` is true by default but the workload is **inert without
  a credential**; the search credential is deliberately **not** in
  `GEMINI_KEY_MANAGED_WORKLOADS`.
* The workload is **provider-agnostic**: `SEARCH_PROVIDER` selects `gemini` (the
  default) or `tavily`, and **exactly one is active**. There is **no automatic
  cross-provider fallback** — a fallback would spend two requests on one question
  and would let a failure on one provider draw on the other's allowance. An
  unknown value falls back to `gemini` and warns once.
* Tavily's credential is **its own** (`TAVILY_API_KEY`), never shared with any
  Gemini workload and never eligible for the shared pool. It travels **only** in
  the `Authorization` header — never in the request body, the URL or a log line.
* Only the **question** is sent, never the room history; neither the question
  nor the credential is ever logged.
* One addressed turn makes **exactly one** search call. The only multiplier is
  the retry loop, which runs **only after a failure** (never after a success),
  is hard-bounded by `GEMINI_SEARCH_MAX_RETRIES`, and **does not retry a Tavily
  429** — Tavily asks us to reduce the request rate, so the breaker is the
  backoff. Credit waste is a design concern: `search_depth` is `basic` (1 credit),
  and `include_answer`/`include_raw_content` are off.
* The **awareness pass does not search**.
* The trigger reads the **same text for a voice transcript as for typed text** —
  a spoken question is not a second path to search.
* `/nexus` status shows the search switch next to awareness, and
  `agent_data.nexus_diagnostics` reports `search_enabled`; the credential is
  never shown by either.

### 53.13 The Admin Control Center (`app/web`)

The panel is built in stages (M1…M8, §54.24/§54.25). These rules are
non-negotiable for every stage:

* The dashboard is a **separate process** from the bot — its own compose service
  running `python -m app.web`. It must **never** run inside the bot's event loop,
  and a problem in the web layer must never be able to disturb Telegram polling.
* **The dashboard identity is not a Telegram identity.** Being an administrator
  of a Telegram group grants **nothing** in the panel. The panel **never** trusts
  a role, a group scope or an owner claim supplied by the client; authorization
  is resolved server-side from the authenticated identity.
* The panel **reads and writes through the existing modules** — `rbac`,
  `admin_service`, `groups`, `key_store`. It must never bypass them, and must
  never reimplement an authority check of its own.
* **The panel's authority is `rbac`'s authority.** A session resolves to an
  `rbac.Principal` and a route is allowed only if `rbac.authorize` allows it.
  The panel must **never append to `rbac.PERMISSIONS`**: that tuple is also the
  wire format of the bot's promotion-dialog bitmask, so adding to it adds a tick
  box to the bot's own UI — a change to the bot's behaviour, which the panel
  must not make. The panel expresses its gates with the vocabulary that already
  exists (`config.manage`).
* **The panel authorizes exactly one identity, and it comes from configuration.**
  `DASHBOARD_OPERATOR_ID` (default `OWNER_USER_ID`), and nothing else — never the
  `admins` table, never `CONFIG_ADMINS`, never a request. A Telegram group
  administrator is **not** a dashboard administrator. The bound id is stamped
  into the session and re-checked on every read, so re-pointing the panel at a
  different identity retires the sessions minted under the old one.
* **A route that declares no permission is refused, not opened.** The permission
  is declared on the handler (`@authz.requires`) and enforced by a middleware, so
  a new page cannot forget it; and a route-inventory test enumerates the router
  so the omission fails in the suite rather than in production.
* **The panel never applies migrations**, and it must **tolerate their absence**:
  it creates only its own table, and a resolution that fails because the bot's
  overlay is not there is a **guest, not a crash** (`rbac.resolve_many`'s rule,
  applied to `authz.principal`).
* **The panel's trail is its own table.** `dashboard_audit` — append-only,
  written by `app/web/audit.py`, read by the panel alone. Panel events must
  **never** be written to `admin_audit`: that would put logins in front of the
  bot's own audit view, which is a change to the bot's behaviour. A caller
  supplied value is truncated before it reaches a column, and an address that is
  blocked may add **at most one row per window**, so the login form cannot be
  used to grow the table from outside.
* **One image, two processes.** The dashboard must not introduce a second image,
  a second base, or a second data volume. Its own dependencies live in
  `requirements-dashboard.txt`, in their own Docker layer.
* Every state-changing request is **server-authorized** and carries the
  session's **CSRF token**; both are middlewares, so a new route cannot forget
  either.
* A password, an API key or `DASHBOARD_SECRET` is **never** logged, returned in a
  response, rendered, or placed in a URL. Only a masked fingerprint may be shown.
* The session cookie is **`HttpOnly` + `SameSite=Lax`**, and **`Secure` whenever
  TLS terminates in front of the panel** (`DASHBOARD_SECURE_COOKIES`) — the
  default is off only because the default bind is loopback-only.
* Changing the password **retires every session** (the epoch), and the current
  password is required to change it, so a stolen cookie cannot lock the owner out.
* The panel **must not claim a capability it does not have.** There is no
  multi-bot support today and the panel must not pretend otherwise (§54.24
  decision 5).

## 54. Nexus intelligence evolution — checkpoint (2026-09-24)

A durable checkpoint for continuing the Intent/Awareness evolution. `§53.7` is
the invariant set; this section is the **state**, not a rule, and is meant to be
replaced as the work advances.

**Where the work is.** Branch `develop/nexus-intelligence-evolution`, HEAD
`5c1f0b6` plus increment Q (pushed to both remotes `origin`/`dashmo3i`). `main`
and the rollback base are untouched at `00c5d1d`. Increments A–Q are done; the
deterministic benchmark in `tools/eval_intent.py` is clean over 137 cases
(`tests/test_intent_eval.py` holds the floor) and the full suite is
**3240 passed / 0 failed**. **No merge, no deploy** without the owner's explicit
go-ahead.

**Increment O — `66a1e94`, "the copula is not a clitic".** The act reader's
`_CLITICS` wrongly held «ه», so «ادمینه کیه؟» folded to «ادمین» + «کی» and read
as an *instruction*. Removed; the copula question words were listed explicitly
in `_QUESTION_WORDS`. `act_copula_directives` 7/134 → 0/134, act accuracy
97.0% → 100.0%, corpus v14 → v15.

**Increment P — "the benchmark scores the resolution the prompt renders".** Two
coupled divergences, both measured on the corpus:

1. `tools/eval_intent.py` `evaluate()` resolved with
   `referents.resolve(anchor, messages=window)` — the **window without the
   anchor** and **no roles** — while the renderer
   (`app/awareness_context.py` `_render_referent_candidates`) resolves with
   `messages=ctx.messages` (window **+ anchor**, what `main._awareness_pass`
   passes) and `roles=ctx.roles`. The scored verdict could differ from the block
   the model reads. (That `_recent_scores` deliberately skips the anchor's own
   user is evidence the intended input *includes* the anchor.)
2. `app/referents.py` `resolve()`'s **role signal counted the anchor's own
   speaker** — unlike `_recent_scores` — so the person *giving* «ادمینه رو محدود
   کن» was offered as a candidate for «ادمینه», rendering an unambiguous case as
   *"could not tell the top candidates apart, ask"*.

Measured (A = harness as it was, B = the renderer's inputs, before the reader
fix; same 134 cases):

| | top-1 | ambiguity recall | ambiguity precision | wrong-confident | confident & correct | #ambiguous |
|---|---|---|---|---|---|---|
| A (harness) | 1.000 | 1.000 | 1.000 | 0 | 0.639 | 4 |
| B (renderer inputs) | 1.000 | 1.000 | **0.571** | 0 | 0.556 | 7 |
| after the reader fix | 1.000 | 1.000 | **1.000** | 0 | **0.649** | 4 |

Five cases move between A and B (`role-single-admin`, `member-cannot-be-role`,
`mixed-role-admin`, `role-two-admins`, `state-anchor-is-reply`); after the fix
the three role cases are confident and correct again, the two-admin tie stays
ambiguous, and the reply case names its target.

Three fixes: the role signal skips the anchor's own speaker (the exclusion is
for the role **inference**; a name, a stated id and a reply edge still name
whoever they name); `evaluate()` resolves with the renderer's `messages` and
`roles`; and the harness's world is built from **every row the corpus labels**,
not only the anchors (`roles_for` *overrides* a row's own `role` field, so a
window speaker the corpus calls an admin but the world calls a member was a
different room than the corpus describes). One exposed label was corrected:
`state-anchor-is-reply` had `referent: null` while its anchor is a reply to 11
and every other reply-anchor case labels the reply target. Corpus v15 → v16.

**What the suite caught, and what it did not.** Two failures, neither a
regression of P's own logic:

* `tests/test_awareness_context.py::test_the_candidates_are_read_from_the_context_not_the_database`
  asserted that the anchor's **own speaker** appears as a candidate — the exact
  reading P removes. The test's real claim (candidates come from the hand-made
  window, not the database) is now made with a *different* configured admin in
  the window, and it additionally asserts the speaker is **not** offered. This
  is a regression test for the guard.
* `tests/test_identity.py::test_the_handle_is_not_derived_from_the_telegram_id`
  was a **pre-existing flake**, unrelated to P: `assert "500" not in handle`
  tests a property of `uuid4().hex` (which contains the substring "500" about
  0.6% of the time — 30 windows × 16⁻³), so the suite failed roughly once in 170
  runs. It passes in isolation. Replaced with two deterministic checks: the
  handle is not the id zero-padded to 32 in either decimal or hex.

**Latency.** The guard only removes a candidate from the scoring loop; a clean
interleaved A/B over the corpus puts it **within noise** (73.7 vs 72.3 µs per
case median, 59.0 vs 59.8 minimum). An earlier reading of 65.3 vs 77.5 µs was
taken with the full suite running on the same box and is noise-dominated — it is
not the number, and P is not sold as a speed-up. 0 Gemini calls, no DB change, no
source/budget/runtime-path change; the only production file touched is
`app/referents.py`.

**Increment Q — "the object line ordered the model to ignore the block beside
it".** §54 moved the entity block's order to "the block that knows the side", and
that reasoning held only half: `app/objects.py` knows the **object** side, not the
people side. It said "Do not read it as aimed at anybody in the room" whenever the
object was a room-held thing — including when an explicit source (reply edge,
stated id, name) had already named somebody, which is exactly the case
`app/referents.py` keeps those sources for. On the reachable shape the two blocks
disagreed inside one prompt: the object line denied people, and four lines below,
the referent block said *"The server is confident in the first candidate"*.

**Zero of the 134 cases had both halves**, so the corpus could not see it — the
same blind spot §54 found, one level down. The fix: `objects.render`'s room-held
branches state their own half and stop ("The thing is not a member of the room"),
which keeps the half-truth the line exists for (the object is a thing, not a
person) and drops the claim about people. Corpus gained the shape once per explicit
source (`object-media-reply-author`, `object-link-reply-author`,
`object-media-named-author`), v16 → v17, 134 → 137 cases. Harness gained
`object_denies_a_person_cases` (reads both rendered blocks) and
`object_person_offered_for_a_thing_wrong` (splits the surviving explicit lead from
the guess).

Measured: `object_denies_a_person_cases` **3 → 0**; object exact (class·source)
1.0; resolution top-1 / ambiguity precision 1.0; wrong-but-confident 0; a person
offered for a thing-object request 0 of 8 → **3 of 11, 0 wrong**; object block
chars mean/max 60.4/123 → 59.0/123; assembled context 923.7/1498 unchanged;
`objects.render` 1.085 → 1.064 µs/case (noise); 0 Gemini calls, no DB change, no
source/budget/runtime-path change; the only production file touched is
`app/objects.py`. Suite 3236 → **3240 passed, 0 failed**. Non-vacuity: the unfixed
renderer reads 3, reconstructed by string in `tests/test_intent_eval.py`.

**Next increment (R), in priority order.** The owner's list still stands (1–5
intent/Awareness understanding, 10–12 scheduling/adaptivity/integration, 13 model
routing only if benchmarks prove it, 14 verification only where measurable). What
Q leaves:

1. Two prose claims still have nothing checking them: the **room-state graph's
   "converged on"** wording and the **act block's "why" wording** beyond
   `act_copula_directives`. Both need a corpus case that renders them beside a
   block that can contradict them — that is what made Q findable.
2. `role-two-admins` stays ambiguous by design; check whether the corpus should
   carry a case where the room's reply convergence *should* break the tie.

**Constraints carried:** speed-first (no unnecessary Gemini calls; benchmark
latency before/after every change), rollback safety, no merge, no deploy.

### 54.1 Checkpoint (2026-09-24, after Q) — resume here

**State, verified against the repository.** Branch
`develop/nexus-intelligence-evolution`, HEAD
`a0e4f3bb4701981b3f656b521f43442339ca744e` (increment Q), pushed to both remotes
(`origin` = mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc) and confirmed with
`git ls-remote`. 25 commits on the branch. `main` and the annotated tag
`release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**.
Suite **3240 passed, 0 failed**. Corpus **137 cases, version 17**. Benchmark
clean: top-1 / ambiguity precision / ambiguity recall 1.0, `wrong_confident == 0`,
`act_accuracy == 1.0`, `object_accuracy == 1.0`, `expression_accuracy == 1.0`,
`edges_exact == cases`, `request_false_affirmative == 0`,
`when_prose_contradictions == 0`, `entity_gives_an_order_cases == 0`,
`object_denies_a_person_cases == 0`, assembled context mean/max 940/1498 under
the 1500 ceiling. **Not merged, not deployed.**

**The roadmap.** `docs/intent-awareness-roadmap.txt` is the durable, standalone
plan: completed increments A–Q with their measurements, the partially-completed
and unfixed findings, everything not yet implemented grounded in the repository,
the open threads, the remaining increments R–V with files / runtime-path /
Gemini / evidence / dependencies, and the end-state definition. Read it before
starting anything; it is written for an agent with no chat history.

**What Q leaves, recorded so it is not re-discovered:**

1. The "score the rendered product" programme covers 5 of 7 rendered blocks. Not
   scored: the **room-state graph's "converged on"** wording and the **act
   block's `why[0]`** wording. → increment **R**.
2. `objects.Object.source` (`named`/`pointed`/`verb`) is computed and scored by
   the harness but **never rendered in the prompt** — the same "dead field" tell
   K found in `entities.pointing`. Open design question, not a defect.
3. `requests.render` hardcodes ONE reason for the two different routes to
   `polarity == ""` ("a later negated directive" vs "a negation elsewhere"); the
   accurate reason is in `request.why` and unused. Weak, unfixed, unmeasured.
4. `objects.render`'s `CLASS_THING` branch duplicates its noun: "The request acts
   on a thing — a thing, not a person." Cosmetic, unfixed.
5. Two stale claims in §53.7 were corrected in this checkpoint: the residual-lead
   floor (`…_wrong == 0` **and** `… >= 3`, not "pinned at 0") and the act block's
   longest corpus output (**350**, not 297).

**The open thread carried from P — `role-two-admins`.** The case reads
`ambiguous: true` **by design**: the resolver refuses to pick between two people
who genuinely hold the role. `_about_focus` fires only on a unanimous, repeated
reply signal (≥ 2 replies all aimed at one person) and only for **anaphoric**
expressions (far demonstrative or the object clitic); «ادمینه» is a **role**
expression, so convergence does not run for it. Open question: should the
room's reply convergence be able to break a **role** tie? Needs evidence, not an
opinion — either the corpus gains the case and `app/referents.py` changes, or the
case stays ambiguous and the reason is written down. → increment **S**. No
increment has started this.

**Exact next step.** INCREMENT R — "the graph's word and the act's why". Start
with the **baseline**: render `room_state.render_graph`'s block and
`discourse.render_act`'s block for every corpus case and READ THE SENTENCES,
exactly as J, K, L, M, O, P and Q did. If neither sentence claims more than its
reader found, record that finding with a should-be-zero metric **and a
non-vacuity proof**, then STOP — do not invent a change. Do not start S, T, U or
V. Do not merge. Do not deploy.

**Rollback.** Every increment is independently revertable: `git revert <sha>` on
this branch, or reset to the previous increment's SHA. The whole evolution is
revertable by leaving the branch unmerged — `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state, and it is an
**ancestor** of the branch (`git merge-base --is-ancestor main HEAD` = yes), so
leaving the branch unmerged is a complete revert. **No lettered increment (A–Q)
changed the DB schema or the runtime path**: verified per file against main,
`app/main.py`, `app/chat.py`, `app/awareness.py`, `app/db.py` and `app/config.py`
are unchanged by A–Q, and the only runtime-adjacent file they touch is
`app/awareness_context.py` (one `Source` entry per reader — the documented seam).
The ONE DB change on the branch is the foundation's `151b1e1` (two ADDITIVE
`awareness_state` columns via `_ensure_column`, which also touched main/chat/
awareness); no data rollback is involved. Increment T would be the first DB
change since it, and must carry its own forward/backward proof and rollback
procedure.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt`, then verify: `git status` (clean),
`git rev-parse HEAD` (a0e4f3b…), `git rev-parse main` (00c5d1d…), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`
(a0e4f3b…). Then start R's baseline.

### 54.2 Checkpoint (2026-09-24, after R) — resume here (supersedes §54.1)

**State, verified against the repository.** Branch
`develop/nexus-intelligence-evolution`, HEAD
`0697ed05e9e6fee6c5fb141a4b242b8eb76075e6` (increment R), pushed to both remotes
(`origin` = mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc). The branch carries
27 commits through R's code commit `0697ed0`, plus the checkpoint docs commits
on top (a commit that names its own count would be wrong the moment it lands). `main` and the annotated tag `release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
(the tag is local-only; neither remote carries tags). Suite **3245 passed, 0
failed**. Corpus **137 cases, version 17**. Benchmark clean: top-1 / ambiguity
precision / ambiguity recall 1.0, `wrong_confident == 0`, `act_accuracy == 1.0`,
`object_accuracy == 1.0`, `expression_accuracy == 1.0`, `edges_exact == 137`,
`graph_claims_convergence_cases == 0`, `act_quote_not_in_anchor_cases == 0`,
assembled context mean/max 940.3/1498 under the 1500 ceiling. **Not merged, not
deployed.**

**What R did.** The last increment of the "score the rendered product" programme
(J's method). Two claims scored:

1. **The graph overclaimed.** `room_state.render_graph`'s "The room's replies
   have converged on X (N of M)" was gated on `focus_count >= 2` — the *edge*
   count — so two replies from one member read as the room converging. The
   corpus case `anaphoric-split-room` (one member replying twice to each of two
   people; the corpus's own note calls that room "split") rendered "converged on
   22 (2 of 4)". Fix: `RoomState.focus_sources` (distinct members at the focus)
   and `converged()` now requires `len(focus_sources) >= 2`; `render_graph`'s
   weaker branch says "N replies were aimed at X, all from one member; that is
   not a convergence". Metric `graph_claims_convergence_cases` **1 → 0**; the
   four genuine convergences still render "converged on".
2. **The act was already correct.** All 112 rendered act sentences read: the
   template reports `why[0]` verbatim, the evidence word is always a token of
   the message, and a claimed act always has a non-empty why. **No production
   change.** The harness now renders the act sentence (it held only `act.why`
   before) and floors `act_quote_not_in_anchor_cases` at 0.

Only production file touched: `app/room_state.py`. 0 Gemini calls; no DB change;
no source, budget or runtime-path change. Graph block chars mean 115.8 → 116.0,
max 239 → 265. `converged()` 0.233 → 0.861 µs (called once per graph render);
`read_state` + `render_graph` within noise. 5 new tests (1 `test_room_state.py`,
4 `test_intent_eval.py` incl. 2 non-vacuity). Narrative: §60 of
`docs/reference/nexus-awareness.md`.

**The programme is complete.** Every block the prompt renders now has a check
re-derived from the rendered prose: the time sentence, the entity block's two
claims, the thread's named words, the act block's quoted directive, the referent
block, the object line, the graph's focus sentence, and the act sentence's
evidence word. See roadmap §2.1.

**Unresolved (do not guess at):**
- **The tie.** A room split evenly between two people *with distinct members on
  each side* (e.g. 22→11, 33→11 and 44→22, 55→22) still renders "converged on
  22" via the most-recent-edge tie-break. No corpus case demonstrates it, so R
  did not change it. Open thread.
- **`role-two-admins`** (thread 4.1) — still `ambiguous: true` by design; the
  role signal never runs the anaphoric convergence. → increment **S**.
- The partially-completed findings still open: the dead field
  `objects.Object.source`, `requests.render`'s weak reason wording, the
  `objects.render` CLASS_THING duplication (roadmap §2.2–§2.4).

**Exact next step.** INCREMENT **S** — "the tie the resolver refuses to break":
decide, with a corpus case, whether the room's reply convergence may break a
**role** tie; implement it only if the case proves the desired behaviour,
otherwise record why not. Do NOT start S without the owner's explicit go-ahead.
Do not start T, U or V.

**Rollback.** Every increment is independently revertable: `git revert <sha>` on
this branch. The whole evolution reverts by leaving the branch unmerged — `main`
at `00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch. **No lettered increment (A–R) changed the DB schema
or the runtime path**; the only DB change on the branch is the foundation's
`151b1e1` (two ADDITIVE `awareness_state` columns via `_ensure_column`). Increment
T would be the first DB change since it and must carry its own forward/backward
proof and rollback procedure.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt`, then verify: `git status` (clean),
`git rev-parse HEAD` (0697ed0…, the R increment, or a docs commit on top of it),
`git rev-parse main` (00c5d1d…), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`
(0697ed0… or later).

### 54.3 Checkpoint (2026-09-24, after S) — resume here (supersedes §54.2)

**State, verified against the repository.** Branch
`develop/nexus-intelligence-evolution`, HEAD
`e936aefdaf5f03fac2b31759e7965143f965d039` (increment S; R was `0697ed0`,
checkpointed at `3b51fdd`), pushed to both remotes (`origin` = mo3iiibest77-hub,
`dashmo3i` = Dashmo3i-GitAcc). Docs commits sit on top of S's code commit (a
commit that names its own count would be wrong the moment it lands). `main` and
the annotated tag `release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
(the tag is local-only; neither remote carries tags). Suite **3251 passed, 0
failed**. Corpus **141 cases, version 18**. Benchmark clean: top-1 / ambiguity
precision / ambiguity recall 1.0, `wrong_confident == 0`, `act_accuracy == 1.0`,
`edges_exact == 141`, `graph_claims_convergence_cases == 0`,
`role_two_admin_confident_cases == 0`, `role_focus_used_cases == 0`, assembled
context mean/max 953.8/1498 under the 1500 ceiling. **Not merged, not deployed.**

**What S did — and did NOT do.** S is "the tie the resolver refuses to break"
(roadmap §1.2c / thread §4.1). It resolved the role-two-admins thread with
evidence and **changed no production code**: `app/referents.py` is UNCHANGED.
The baseline showed the behaviour is already correct, so pinning it and recording
why is the whole increment (the increment's own stop rule allows — indeed
prefers — a no-change outcome).

The invariant S exists to protect: **a role signal must never run, replace or
override the anaphoric/conversational convergence**, and must never manufacture
certainty from the fact that two people share a role. The runtime path proves the
two mechanisms are disjoint *by construction*: the role signal in
`referents.resolve()` runs only for `KIND_ROLE`, skips the anchor's own speaker,
and never calls `_about_focus`; `_about_focus` runs only when
`expression.anaphoric()` (far demonstrative or the object clitic) and needs ≥ 2
replies all aimed at one person. So «ادمینه» never reaches convergence, two
holders read `ambiguous` (never confident), recency only orders the list, and an
anaphor on the same room *is* settled by convergence while the role holders stay
out of the reading.

Pinned with **4 corpus cases** (v17 → v18, 137 → 141), covering the six named
shapes: `role-two-admins-converge-one`, `role-admin-vs-member-converge`,
`role-two-admins-recency`, `role-anaphoric-beats-admins`. The harness gained two
**should-be-zero** metrics — `role_two_admin_confident_cases` (0) and
`role_focus_used_cases` (0) — a report line and a failures clause. Both checks
read the candidate **evidence** (the `why` list), not the final verdict, so a
right-looking answer reached by the wrong mechanism still fails. **Non-vacuity:**
dropping `CONFIDENT_MIN` *and* `MARGIN` together makes the tied admins read
confident (the metric rises); removing the anaphoric gate lets a role tie run
convergence (the metric rises). 6 new tests (2 `test_referents.py`, 4
`test_intent_eval.py` incl. 2 non-vacuity). 0 Gemini calls; no DB change; no
source, budget or runtime-path change. Context chars mean 940.3 → 953.8, max
1498 (ceiling 1500).

**Unresolved (do not guess at):**
- **The evenly-split room.** A room split evenly between two people *with
  distinct members on each side* still renders "converged on" via the
  most-recent-edge tie-break (the R thread). No corpus case demonstrates it; R and
  S both left it. Open thread — do NOT solve it without a case.
- The partially-completed findings still open: the dead field
  `objects.Object.source`, `requests.render`'s weak reason wording, the
  `objects.render` CLASS_THING duplication (roadmap §2.2–§2.4).

**Exact next step.** INCREMENT **T** — "what the server read, kept" (Intent ↔
Awareness integration, roadmap §5/T). It is the **first DB change since the
foundation's `151b1e1`** and the **first runtime-path change in the lettered
increments**, so it must carry a forward/backward compatibility proof for the
(additive, `_ensure_column`-only) column AND a documented rollback procedure AND
a live probe — "tests pass" is not enough. Do NOT start T without the owner's
explicit go-ahead. Do not start U or V.

**Rollback.** Every increment is independently revertable: `git revert <sha>` on
this branch. S reverts with `git revert e936aef` (it touches only the harness,
the corpus and tests — no production file). The whole evolution reverts by
leaving the branch unmerged — `main` at `00c5d1dd412e033c6ac15599b28bc0fbcb54d709`
is the production state and is an **ancestor** of the branch. **No lettered
increment (A–S) changed the DB schema or the runtime path**; the only DB change
on the branch is the foundation's `151b1e1` (two ADDITIVE `awareness_state`
columns via `_ensure_column`). Increment T would be the first DB change since it.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt`, then verify: `git status` (clean),
`git rev-parse HEAD` (`e936aef…`, the S increment, or a docs commit on top of
it), `git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`
(`e936aef…` or later).

### 54.4 Checkpoint (2026-09-24, after T) — resume here (supersedes §54.3)

**State, verified against the repository.** Branch
`develop/nexus-intelligence-evolution`, HEAD
`5904791459b699a5b9bc4ee1a773e21caa5f70a7` (increment T; S was `e936aef`,
checkpointed at `896d260`), pushed to both remotes (`origin` = mo3iiibest77-hub,
`dashmo3i` = Dashmo3i-GitAcc). Docs commits sit on top of T's code commit (a
commit that names its own count would be wrong the moment it lands). `main` and
the annotated tag `release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
(the tag is local-only; neither remote carries tags). Suite **3266 passed, 0
failed**. Corpus **141 cases, version 18**. Benchmark clean: top-1 / ambiguity
precision / ambiguity recall 1.0, `wrong_confident == 0`, `act_accuracy == 1.0`,
`edges_exact == 141`, `graph_claims_convergence_cases == 0`,
`role_two_admin_confident_cases == 0`, `role_focus_used_cases == 0`; pass context
mean/max 954/1498, borrowed reading 517/1146, both under the 1500 ceiling.
**Not merged, not deployed.**

**What T did — the first runtime-path change in the lettered increments, and NO
DB change.** The roadmap called T "what the server read, kept" and proposed
recording the deterministic reading with the awareness row. The baseline showed
that was unnecessary, so T became "the reading reaches the conversation".

* **The gap, traced not assumed.** The awareness pass
  (`main._awareness_read`) hands the model the roster + `memory_block` +
  `awareness_context.blocks(ctx)` — the server's *reading* of the anchor (act,
  direction, object, reply graph, thread, entities, time, questions, referent
  candidates). The addressed conversation (`main._answer_conversationally`)
  built its context from the trusted block + `awareness.room_block` (the raw
  transcript) + `_today_block`. So «همون کاربر رو بن کن» reached the model as a
  resolved instruction on a pass and as raw text with no resolution on the path
  where a person is waiting for an answer. Same shape as the date `_today_block`
  already fixed once.
* **The change.** `awareness_context.blocks` gained `skip`, and a named
  `CONVERSATION_SKIP = {calendar, room, remembered_people, admin_activity,
  referenced_people}`; `main._room_reading` renders the reading of the message
  being answered from the window the reply path already reads;
  `awareness.room_block` gained `messages` so the window is read **once** and
  handed to both the transcript and the reading.
* **Why no DB change.** The reading is a pure function of the window
  (`group_messages`, persisted) + the anchor + `rbac`. Nothing needs to survive
  the pass — the next pass re-reads the window, which contains the previous
  anchor, and re-derives. Recording it would add a column and a write per pass
  for what the window already lets any caller rebuild. **No column, no
  migration, no rollback procedure**; the `151b1e1` precedent is not needed.
* **Measured.** Borrowed reading over 141 cases: chars mean **517** / max
  **1146** (pass reading 954 / 1498); **28** cases carry the resolver's
  candidates; **0** exceed the pass reading; **0** exceed the ceiling; ~**0.6 ms**
  per reply (the transcript beside it ~0.5 ms); **+1 DB read** per addressed
  reply (`rbac.resolve_many` → `db.admin_list()`, once, for the roles);
  **0 Gemini/provider calls** added; every other benchmark number unchanged.
* **Fail-soft.** Message not in the window, layer off, or a resolver that raises
  all return `""`; the answer goes out exactly as before.
* **Tests.** 15 new (11 `test_awareness_context.py`, 2 `test_awareness_switch.py`,
  2 `test_intent_eval.py`). No corpus case was added: T's evidence is over the
  existing 141, and a synthetic case that could not demonstrate an invariant was
  deliberately not added. Narrative: reference §62.

**Unresolved (do not guess at):**
- **The quality effect is unproven.** T delivers the reading to the addressed
  prompt; the deterministic benchmark cannot score a model's answer, so *that the
  answers got better* is a question for a live probe and is **not claimed**. This
  is the honest open item.
- **The evenly-split room** (R's thread): a room split evenly with distinct
  members on each side still renders "converged on" via the most-recent-edge
  tie-break — no corpus case demonstrates it; R, S and T all left it.
- The partially-completed findings still open: `objects.Object.source`,
  `requests.render`'s weak reason wording, the `objects.render` CLASS_THING
  duplication (roadmap §2.2–§2.4).

**Exact next step.** INCREMENT **U** — "the room that should go next" (adaptive
Awareness scheduling, items 10/11). FIRST resolve the open design question in
roadmap §4.3: the seam that does not violate "`awareness.due` cannot see
messages" (its signature is asserted by a test). Then measure coverage/priority
at a **fixed** 200-request allowance, assert no room is starved, keep the
per-room brake and debounce, and take a live probe. **Do NOT start U without the
owner's explicit go-ahead.** Do not start V.

**Rollback.** Every increment is independently revertable: `git revert <sha>` on
this branch. T reverts with `git revert 5904791` (it touches `app/main.py`,
`app/awareness.py`, `app/awareness_context.py`, the harness and tests — and **no
schema**, so the revert needs no data step). The whole evolution reverts by
leaving the branch unmerged — `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch. **No lettered increment (A–T) changed the DB
schema**; the only DB change on the branch is the foundation's `151b1e1` (two
ADDITIVE `awareness_state` columns via `_ensure_column`). T is the first lettered
increment to change the **runtime path**.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt`, then verify: `git status` (clean),
`git rev-parse HEAD` (`5904791…`, the T increment, or a docs commit on top of
it), `git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`
(`5904791…` or later).

### 54.5 Checkpoint (2026-09-24, after Phase Zero of the W→V mission) — resume here (supersedes §54.4)

**State, verified against the repository (Phase Zero).** Branch
`develop/nexus-intelligence-evolution`, HEAD
`ec3b29ea05143b7598358a596f52b5ec3627124a` (T docs commit; T code is
`5904791459b699a5b9bc4ee1a773e21caa5f70a7`), pushed to both remotes (`origin` =
mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc). Working tree **clean**. `main`
and the annotated tag `release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
(the tag is local-only; neither remote carries tags). Suite **3266 passed, 0
failed**. Corpus **141 cases, version 18**. **Not merged, not deployed.**

**T is VERIFIED, not reimplemented.** The invariant holds in the code as
committed: `main._room_reading` (app/main.py:2628) builds
`awareness_context.build_ctx(...)` for the answered message and renders
`awareness_context.blocks(ctx, skip=awareness_context.CONVERSATION_SKIP)`
(awareness_context.py:690/729); `awareness.room_block` takes `messages`
(awareness.py:1075) so the window is read **once** and handed to both the
transcript and the reading; the T commit touched **no `app/db.py`** (no
migration); the reading adds no Gemini call and is bounded by the same 1500
ceiling; it is context, never authority. Nothing to fix.

**What this checkpoint adds.** Phase Zero's second deliverable: the durable
roadmap now formally scopes the three new increments, in dependency order
**T → W → X → Y → U → V**, with the rationale for why W/X/Y precede U/V:

* **W — long-term User Memory** (roadmap §5/W). Bounded, structured, durable
  facts/preferences per user; NOT history, NOT Awareness, NOT Intent. Likely the
  first genuinely-needed new **table** (the memory is not derivable from what is
  already stored) — additive only, migration test, drop-table rollback. Start by
  BENCHMARKING the bounded model (items/user, categories, retention, growth at
  ~3000 members / ≤200 MB), not by copying the brief's 20–50.
* **X — Stateful Long-term Nexus** (roadmap §5/X). Bounded conversational STATE
  (active topic/referent, pending question/action, continuity), explicitly
  distinct from Memory; scoped by (chat_id, user_id) or (chat_id, task), never a
  global state. Fresh explicit input wins over stale state; ambiguity stays
  ambiguous.
* **Y — Chat Quality / Context Intelligence** (roadmap §5/Y). Only after W and
  X: the addressed path consumes the MINIMUM relevant combination of Intent +
  referents + Awareness + Memory + State + history (+ search), fast path for
  simple messages. Requires a controlled **live-probe** evaluation — "the
  deterministic benchmark is still green" is explicitly NOT evidence of better
  answers.
* **U** (adaptive scheduling, fixed 200-request allowance, no new calls) and
  **V** (model routing, only if Y's quality measurement justifies it) come last
  because U spends the rationed request budget and V needs an answer-quality
  measurement that does not exist until Y.

**Why Phase Zero and not implementation.** This session reached the context
ceiling the brief's own rules set (~90%): "do not start broad new work; finish
the smallest safe checkpoint." W is a DB-bearing stage and must not be started
without room to test and measure it. The repository is coherent and the next
step is unambiguous.

**Exact next step.** INCREMENT **W** — long-term User Memory. FIRST: inspect the
existing storage architecture for a suitable bounded mechanism; if none exists,
design the additive table and BENCHMARK the bounded model from real data before
writing it. Read roadmap §5/W and §4.3, then `app/db.py`'s `_ensure_column`
convention and `151b1e1`. **Do NOT start W without the owner's explicit
go-ahead.** Do not skip to X, Y, U or V.

**Note on the mission brief.** The brief's Phase U tail arrived truncated
("spend reques…"); the U/V requirements must be re-stated in full before those
phases are attempted. W, X and Y were specified completely.

**Rollback.** Every increment is independently revertable: `git revert <sha>` on
this branch. Phase Zero added **documentation only** (no code, no schema), so it
reverts with a docs revert. The whole evolution reverts by leaving the branch
unmerged — `main` at `00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production
state and is an **ancestor** of the branch. **No lettered increment (A–T) changed
the DB schema**; the only DB change on the branch is the foundation's `151b1e1`
(two ADDITIVE `awareness_state` columns via `_ensure_column`). W would be the
first lettered increment to add a table and must carry its own forward/backward
proof and rollback procedure.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§5 for W/X/Y/U/V, §7 for the continuation
point), then verify: `git status` (clean), `git rev-parse HEAD` (`ec3b29e…` or
later), `git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`
(`ec3b29e…` or later). Then begin W.

### 54.6 Checkpoint (2026-09-24, after W) — resume here (supersedes §54.5)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`, HEAD
`ddc79aadac92d36752fc85df9b1b056563d3f2bf` (increment W; this checkpoint is the
docs commit that sits on top of it), pushed to both remotes (`origin` =
mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc). Working tree **clean**. `main`
and the annotated tag `release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**.
Suite **3314 passed, 0 failed**. Corpus **141 cases, version 18**. **Not merged,
not deployed.**

**Increment W — "what the server may remember about a person" (DONE).**
Long-term user memory: a bounded set of clauses a person explicitly asked to be
remembered, keyed by `(chat_id, user_id)`.

* **The branch's first genuinely necessary table.** The roadmap's stop rule was
  applied first: `people` (identity metadata + a count), `identities` (an opaque
  uuid), `awareness_state` (the room) and `chat_messages` (one person's
  conversation) were all inspected, and none holds a durable fact about a person.
  So `user_memory` was added **additively as a new table** — no existing table
  altered, no existing row touched, rollback is `DROP TABLE user_memory`.
* **The cap is 30, chosen by measurement.** 202 bytes/row means 3000 members × 30
  items is **17.4 MB** against a 200 MB budget, so disk is not the binding
  constraint; the ~300-character block that can surface ~4 clauses is. 30 leaves a
  ~7× recall margin while keeping the table a fact set rather than a log. Three
  bounds: `NEXUS_MEMORY_MAX_PER_USER`, `NEXUS_MEMORY_RETENTION`,
  `NEXUS_MEMORY_MAX`, applied on the observation path (no scheduler); the indexed
  per-person delete runs on the write, the whole-table bounds every `PRUNE_EVERY`.
* **Writes are explicit-only.** A deterministic trigger over the text the person
  typed; no model call, nothing inferred. «من ادمینم» stores nothing. A memory
  **grants nothing** — no authority module imports `app/memory.py`.
* **Isolation is by construction.** The key names the room, so one person's memory
  is never another's, one group's is never another's, and a private memory can
  never render in a group.
* **Files.** New: `app/memory.py`, `tests/test_memory.py`, `tools/eval_memory.py`.
  Changed: `app/db.py` (the table + helpers), `app/config.py` (six knobs),
  `app/awareness_context.py` (one `Source`), `app/main.py` (the write),
  `tests/conftest.py`, `tests/test_intent_eval.py`, `tools/eval_intent.py`.
* **Measured.** Extraction precision **1.0** / recall **1.0**, **0** false
  positives; write **0.22 ms p50**; retrieval **0.005 ms p50**; **0** Gemini
  calls. The block renders on **141/141** corpus cases; the assembled context
  moved mean 954 → **1046** / max 1498 → **1489**, the addressed reading mean 517
  → **615** / max 1146 → **1244** — all under the 1500 ceiling.
* **Deliberately NOT built** (recorded, not half-done): semantic extraction from
  ordinary conversation (needs a model call or an awareness-prompt change), and
  memory for a referenced person other than the anchor.

**Exact next step.** INCREMENT **X** — Stateful Long-term Nexus (roadmap §5/X):
bounded conversational STATE (active topic/referent, pending question/action,
continuity), explicitly distinct from Memory, scoped by `(chat_id, user_id)` or
`(chat_id, task)`, never a global state; fresh explicit input wins over stale
state; ambiguity stays ambiguous. It reuses W's bounded-store pattern. **Do NOT
start X without the owner's explicit go-ahead.** Do not skip to Y, U or V.

**Note on the mission brief.** The brief's Phase U tail arrived truncated
("spend reques…"); the U/V requirements must be re-stated in full before those
phases are attempted. W, X and Y were specified completely.

**Rollback.** Every increment is independently revertable: `git revert <sha>` on
this branch. W is `ddc79aa` (code) plus this docs commit; reverting W is a code
revert and a `DROP TABLE user_memory` — the table is new, so nothing else moves.
The whole evolution reverts by leaving the branch unmerged — `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch. The only DB changes on the branch are the
foundation's `151b1e1` (two additive `awareness_state` columns) and W's new
table.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§5 for W/X/Y/U/V, §7 for the continuation
point), then verify: `git status` (clean), `git rev-parse HEAD` (this checkpoint's
commit or later), `git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`. Then
begin X.

### 54.7 Checkpoint (2026-09-24, after the W extension) — resume here (supersedes §54.6)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`. The
W-extension **code** commit is `c6ab4c36268a72181efcadfa06efb78d0ddf0d79` (this
checkpoint is the docs commit on top of it, as W was `ddc79aa` + `e55d48f`), and
it sits on §54.6 (`70dd7e0`). Pushed to both remotes (`origin` =
mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc); both verified with
`git ls-remote`. Working tree **clean**. `main` and the annotated tag
`release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**,
and it is an ancestor of HEAD. Suite **3417 passed, 0 failed** (was 3314).
**Not merged, not deployed.**

**The four context sources, and the boundary between them.** These are
complementary and deliberately *not* one generic "context":

* **Conversation History** — what was recently said. The room window
  (`group_messages`, one hour) and the transcript; short-lived and chronological.
* **Awareness** — what is happening around Nexus now. The room-scoped reading
  (`app/awareness.py` + `app/awareness_context.py`): who spoke, the reply graph,
  the addressed-message reading, the participants. Room-scoped and bounded by the
  existing debounce, starvation, cooldown and daily-budget rules.
* **Stateful Nexus / State** — what the current interaction is trying to
  accomplish. **NOT BUILT YET**; it is increment **X** and its boundary is
  reserved here so Memory is not asked to do its job. Memory must never be used
  as a substitute for state.
* **Long-Term User Memory** — what is worth remembering about this person.
  `app/memory.py`, keyed `(chat_id, user_id)`, bounded, and never a raw archive.

Each source contributes its own bounded block; the composition takes the minimum
relevant combination rather than injecting all four. Memory adds **one** block,
bounded by `NEXUS_MEMORY_ITEMS` rows and `NEXUS_MEMORY_CHARS` characters.

**What the W extension adds (DONE).**

* **A second write path, not a replacement.** `memory.observe` handles the
  explicit clause first (through the same `remember`), then the automatic layer.
  Explicit requests stay the strongest signal and share every validation, bound
  and isolation rule.
* **A statement, not a guess.** A closed slot vocabulary (`memory.SLOTS`, 17
  slots) matched by deterministic rules over the message. A transient state, a
  question, a request and a third-party statement are **whole-message refusals**.
  Nothing psychological or sensitive is inferred; the rules read *what* somebody
  is, never how they feel.
* **The slot is the key**, so a new value replaces the old one — create, update,
  replace, merge and deduplicate are one slot-scoped upsert. The vocabulary is
  closed, so a person's automatic memories are bounded by it rather than by how
  much they type.
* **Behaviour is counted.** `style.playful` and `preference.style` are remembered
  only past `NEXUS_MEMORY_SIGNAL_THRESHOLD`, in a separate bounded counter table
  (`user_memory_signal`) that holds a number and never a message, decaying by age.
* **Humour is a stated preference and nothing else.** Reached only by an explicit
  statement; the material is never stored and the flag changes no safety rule.
* **Relevance-first retrieval.** Explicit, identity, preference, style and humour
  rows are always relevant; an **interest** must be mentioned. So a favourite game
  does not appear in an answer about a programming project, while a known
  programming language always does.
* **The model seam is off and isolated.** `app/memory_extract.py` uses the
  isolated `memory` Gemini workload; it is enabled only by
  `NEXUS_MEMORY_EXTRACT_MODEL` **and** an account. Its output is untrusted —
  `memory.validate_candidate` re-checks the slot and the value before storage.
  There is no `GEMINI_MEMORY_API_KEY` on this host, so the seam is unexercised.
* **Off the answer path.** `main._schedule_memory_observation` schedules
  `memory.observe` as a background task. Nothing about memory is awaited by a
  handler; a slow provider, a locked database or a broken rule costs a memory,
  never a reply.
* **Independent of awareness.** Memory reaches an addressed answer through the
  room reading when it exists, and through `main._memory_context` when awareness
  is off, the message is not in the window, or the reading cannot be built — and
  it is never duplicated. Awareness stays an optional source.

**Measured** (`python3 tools/eval_memory.py`, deterministic and offline):

* explicit extraction precision **1.0** / recall **1.0** (12 pos / 13 neg), 0 FP;
* automatic extraction precision **1.0** / recall **1.0** (16 pos / 15 neg), 0 FP;
* gate: of 18 ordinary messages, 5 answered by the rules and **1 (5.6%)** reached
  the seam; **0 provider calls**;
* storage: 90 000 rows **16.91 MB**, **197 bytes/row**, projected **16.91 MB** at
  3000 members (budget 200 MB); write 0.293 ms p50 / 0.678 ms p95;
* lifecycle: 8 accepted / 4 rejected, 2 duplicates, 6 replacements, **5 rows** at
  the end of the scripted conversation;
* **sync cost** (the only work memory adds to a chat turn): read+render
  **0.074 ms p50 / 0.176 ms p95**, against **0.0004 ms** with the feature off;
* retrieval 0.049 ms p50 / 0.128 ms p95; block mean/max **145** chars (budget 300).

**Live probe — NOT RUN, and why.** The probe compares a baseline (Conversation +
Awareness) against Conversation + Awareness + relevant Memory and measures
repeated-question reduction, relevance, incorrect/inappropriate memory, latency,
model calls, context size and provider latency. It **cannot be run on this host**:
there is no `GEMINI_MEMORY_API_KEY`, so the model seam is disabled and the
deterministic suite never exercises a provider. The measurements above are the
**server's** contribution — what is extracted, stored, rendered and what it costs
— and they are **not** evidence that answer quality improved. Claiming otherwise
would be exactly the overclaiming this project refuses. **Known limitation:** the
model seam's validation path is tested with a synthetic untrusted payload
(`tests/test_memory_auto.py`), not with a real provider answer.

**Known limitations.** (1) Relevance is a deterministic lexical overlap plus a
small per-slot hint table, so an interest stored in one script may not match a
topic written in another unless a hint covers it. (2) The deterministic rules
cover a fixed set of phrasings; the model seam exists for the remainder and is
off. (3) The global prune is a whole-table statement (266 ms measured at 90 k
rows) that runs every `PRUNE_EVERY` recordings once the table is over
`NEXUS_MEMORY_MAX`; it is off the answer path but worth revisiting at scale.
(4) State is not built, so continuity across turns is still the conversation
window's job.

**Rollback.** The extension is independently revertable: `git revert <sha>` on
this branch, plus `DROP TABLE user_memory_signal` (the one new table, additive —
no existing table was altered). W itself remains `ddc79aa` + `e55d48f` +
`70dd7e0`, rollback `DROP TABLE user_memory`. The whole evolution reverts by
leaving the branch unmerged; `main` at `00c5d1dd412e033c6ac15599b28bc0fbcb54d709`
is the production state and is an **ancestor** of the branch.

**Exact next step.** INCREMENT **X** — Stateful Long-term Nexus (roadmap §5/X):
bounded conversational STATE, explicitly distinct from Memory, scoped by
`(chat_id, user_id)` or `(chat_id, task)`, never global; fresh explicit input wins
over stale state; ambiguity stays ambiguous. It reuses W's bounded-store pattern.
**Do NOT start X without the owner's explicit go-ahead.** Do not skip to Y, U
or V.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§5 for W/W-extension/X/Y/U/V, §7 for the
continuation point), then verify: `git status`, `git rev-parse HEAD`,
`git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`.

### 54.8 Checkpoint (2026-09-24, after X) — resume here (supersedes §54.7)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`. Increment
**X** (stateful long-term Nexus) sits on §54.7. Pushed to both remotes (`origin` =
mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc); both verified with
`git ls-remote`. Working tree **clean**. `main` and the annotated tag
`release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**,
and it is an ancestor of HEAD. Suite **3484 passed, 0 failed** (was 3417).
**Not merged, not deployed.**

**What already existed before X (verified, not reimplemented).**

* **Conversation History** — the room window (`group_messages`, one hour) and the
  per-person transcript (`chat_messages`), unchanged.
* **Awareness** — `app/awareness.py` + `app/awareness_context.py`, the
  room-scoped reading and its `Source` registry, unchanged. **X adds one source
  to that registry; it does not touch the layer.**
* **Long-Term User Memory (W)** — `app/memory.py`, `app/memory_extract.py`, the
  `user_memory`/`user_memory_signal` tables, unchanged. **X reuses W's
  bounded-store *pattern* (additive table, background observe, bounded render,
  one `Source`, a `main._*_context` fallback) but not its table, its vocabulary
  or its semantics.**
* **"Nexus state"** (`app/nexus.py`) is the **ONLINE/OFFLINE runtime switch and
  the trigger policy**, not conversational state. It was inspected and left
  alone; X is a new layer beside it, not a change to it.

**What X adds (DONE).**

* **A new module, `app/state.py`,** that owns one narrow thing: the *active task*
  of an interaction. One row per `(chat_id, user_id)`, holding a topic, a goal,
  an unresolved question, a status from a closed vocabulary and the last
  transition's name. It is not a transcript — there is no column a message body
  could fit in — and the row *is* the bound.
* **An explicit lifecycle.** `activate` / `update` / `replace` / `complete` /
  `reset` / `continue`, read deterministically from the message. A completion or
  a reset **clears** the row; a new task **replaces** the old one (one row, never
  a growing set); a continuation marker re-stamps it. An ordinary message — a
  greeting, an acknowledgement, a reaction, a claim of authority, a request for
  an action — matches nothing and changes nothing.
* **A new additive table, `conversation_state`,** keyed by `(chat_id, user_id)`,
  with a `version` for optimistic concurrency and a `message_id` for idempotency.
  Rollback is `DROP TABLE conversation_state`; no existing table is altered.
* **Compare-and-swap and idempotency.** The write names the version it read, so an
  older background worker is **refused** rather than clobbering a newer state; a
  duplicate delivery (same message id) is a **no-op** rather than a second
  transition. Both are counted in the benchmark.
* **One context `Source` (`conversation_state`), tier 0,** right after
  `user_memory`, and deliberately **not** in `CONVERSATION_SKIP` — an addressed
  message is exactly the turn whose continuation state exists to serve. It is
  rendered on the awareness pass and borrowed by the addressed conversation.
* **A fallback, `main._state_context`,** so State survives awareness being off,
  unavailable or failed, exactly as Memory does. The two are separate blocks and
  neither is duplicated.
* **Off the answer path.** `main._schedule_state_observation` schedules
  `state.observe` through the shared `_schedule_background` helper (the memory
  observation now uses the same helper, so the two cannot drift). Nothing about
  state is awaited by a handler.
* **No model seam, and that is the design.** The roadmap scopes X at
  "Gemini: 0 expected" and the request allowance is rationed, so there is **no
  `state` Gemini workload** and no extraction seam. State cannot spend, delay or
  exhaust the request somebody is waiting on — isolation by construction, not by
  a budget. (This is the one place X deliberately does **not** mirror W: W has an
  off-by-default `memory` seam because semantic fact-extraction needed one; X's
  deterministic signals cover the cases that matter and a seam would spend the
  rationed currency.)

**The four context sources, and the boundary (now all four exist).**

* **Conversation History** — what was recently said.
* **Awareness** — what is happening around Nexus now.
* **State** — what the current interaction is trying to accomplish.
* **Long-Term User Memory** — what is worth remembering about this person.

The boundary is one example: a preference for Python is **Memory**; "currently
debugging the Python authentication bug" is **State**. A state transition writes
no memory and a memory writes no state (`tests/test_state.py` asserts both
directions). Each contributes its own bounded block; the composition takes the
minimum relevant combination, and no block is duplicated.

**Measured** (`python3 tools/eval_state.py`, deterministic and offline):

* transition reader precision **1.0** / recall **1.0** (13 pos / 14 neg), **0**
  false positives;
* storage: one row per person, **267.6 bytes/row**, **0.77 MB at 3000 members**
  (budget 200 MB); write **0.132 ms p50 / 0.374 ms p95**;
* lifecycle: a 5-message scripted conversation produces all five transitions and
  **0 rows at the end** (a completion clears the task) — no ground-truth
  mismatch;
* concurrency: the stale write is **refused**, the duplicate is a **no-op**, the
  version is unchanged and one row remains;
* **sync cost** (the only work State adds to a chat turn): read+render
  **0.059 ms p50 / 0.115 ms p95**, against **0.0016 ms** with the feature off;
* retrieval **0.038 ms p50**; block mean/max **170 / 171** chars (budget 300);
* model calls **0**.

**Live probe — NOT RUN, and why.** The brief's probe compares a baseline against
Conversation + Awareness + State + relevant Memory and measures continuation
accuracy, repeated-question reduction, referent resolution, latency and model
calls. It is an **answer-quality** measurement and it needs a provider. The
measurements above are the **server's** contribution — what is read, stored,
rendered and what it costs — and they are **not** evidence that answers improved.
Claiming otherwise would be the overclaiming this project refuses. The probe
belongs to increment **Y**, whose whole purpose is the controlled before/after
evaluation; X supplies the State block it will measure. **Known limitation:** the
deterministic transition rules cover a fixed set of phrasings; a task stated in
an unanticipated wording changes nothing (documented, not inferred).

**Known limitations.** (1) The transition reader is a fixed set of Persian and
English phrasings; a task stated in another wording is not read. (2) Continuation
that arrives more than `NEXUS_STATE_TTL` (72 h) after the last activity is
treated as a new interaction. (3) State is one task per `(chat_id, user_id)`, so
a person genuinely running two interleaved tasks in one room keeps only the most
recent — ambiguity is preserved by *not* guessing rather than by holding both.
(4) The observation is a background task, so on the turn a task is replaced the
answer still sees the previous state; the brief accepts this ("chat continues
with the previous valid State"), and `state.relevant` withholds it when the fresh
message clearly supersedes it.

**Rollback.** X is independently revertable: `git revert <sha>` on this branch,
plus `DROP TABLE conversation_state` (the one new table, additive — no existing
table was altered). W and the W extension are untouched. The whole evolution
reverts by leaving the branch unmerged; `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch.

**Exact next step.** INCREMENT **Y** — "the minimum relevant combination"
(roadmap §5/Y): the addressed path consumes the MINIMUM relevant combination of
Intent + referents + Awareness + Memory + **State** + history (+ search), with a
fast path for simple messages, and a **controlled live probe** of answer quality.
All four sources now exist. **Do NOT start Y without the owner's explicit
go-ahead.** Do not skip to U or V.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§5 for W/W-extension/X/Y/U/V, §7 for the
continuation point), then verify: `git status`, `git rev-parse HEAD`,
`git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`.

### 54.9 Checkpoint (2026-09-24, after Y) — resume here (supersedes §54.8)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`. Increment
**Y** (chat quality / context intelligence) sits on §54.8. Working tree clean;
pushed to both remotes (`origin` = mo3iiibest77-hub, `dashmo3i` =
Dashmo3i-GitAcc). `main` and the annotated tag `release-base/nexus-intel` both
still `00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is
untouched**, and it is an ancestor of HEAD. Suite **3568 passed, 0 failed** (was
3484). **Not merged, not deployed.**

**What Y is, and what it is not.** Y is *not* "give Gemini more context". It is a
**deterministic context-composition layer** that answers one question — *given
this message, what is the minimum relevant combination of the four sources* — and
nothing else. There is deliberately **no** universal context database, no
`ContextMemory`/`UniversalContext`/`NexusContextStore`, **no second model call**
to select context, and **no new table**. The four sources stay four sources.

**What Y adds (DONE).**

* **`app/context_plan.py`** (new, 858 lines) — the selector and the composer.
  `read(text, kind, reply, media) -> Reading` is a pure function of the message's
  own shape; `compose(reading, admin, room, awareness, state, memory, date,
  search, message, ceiling) -> ContextPlan` selects, orders, de-duplicates and
  bounds the rendered blocks. Neither reads a database, calls a model or stores
  anything.
* **The selector** asks the scored readers the project already has
  (`referents.find_expression`, `discourse.read_act`, `state.read`) plus its own
  small patterns (back-reference, opinion request) and two structural facts the
  handler already holds (`reply`, `media`). A closed reason vocabulary (`R_*`)
  makes every decision countable. `wants_awareness` is true only for reasons that
  make the message depend on the **room**; a task starting, continuing or ending
  is the person's own thread and does **not** pull the room in.
* **The fast path** is "no room window" — the expensive source — for a message
  with no dependency signal. **Short is not simple**: «همونو بزن» is four
  characters and needs the room; «قیمت چنده؟» has its own subject and needs none.
  The one short case called dependent is a bare interrogative («چی؟»), which has
  no content word to answer.
* **Precedence is per-conflict, and each rule is a refusal**: a correction drops
  the memory line that shares a word with it; the state reader withholds a stale
  or superseded task and Y does not put it back; the room is not the person (a
  private intent selects no room); a repeated fact is sent once — conservatively,
  so every meaningful word of the lower block must already be present before it
  is dropped.
* **One deterministic order** — admin, room, reading, state, memory, date,
  search — appended to the **system instruction**, so the prompt-injection
  boundary is unchanged.
* **`compose` enforces the reading's own selection.** A block the reading did not
  ask for is dropped even if a caller rendered it, so `ContextPlan.selected()` is
  the decision and not the caller's discipline. The caller still does not *read*
  an unwanted source, which is where "no duplicate retrieval" lives.
* **`NEXUS_CONTEXT_CHARS` (3500)** bounds the four **selectable** sources
  together, by dropping whole sources in reverse precedence (memory → state →
  room). It deliberately **does not count the roster, the date or the web
  findings** — see defect 2 below.
* **`app/awareness_context.blocks(ctx, *, skip, budget)`** gained a `budget`
  parameter (defaulting to the pass-wide cap), so the reading can be bounded by
  its caller. **`app/config.py`** and **`.env.example`** document the new knob.
* **`tools/eval_context.py`** — the deterministic A–U benchmark (535 lines);
  **`tools/bench_context_real.py`** — the same comparison on the *real* addressed
  path, with the model stubbed (the provenance of the real-path figures below);
  **`tests/test_context_eval.py`** (25 tests) holds the floors under both;
  **`tests/test_context_plan.py`** (59 tests) covers the selector, the composer
  and the real path.

**Two defects found and fixed during verification** — both by running the real
path, neither visible from reading the module in isolation:

1. **An opinion request was read as self-contained.** «نکسوس نظرت چیه؟» ("what do
   you think?") has no subject of its own — its subject is the room's recent
   content — but «نظرت» reads as a content word, so the short-message rule called
   it fast and the room was dropped. The pre-existing test
   `test_awareness.py::test_an_addressed_answer_is_given_the_room_context` caught
   it; only a **full-suite run** surfaced it. Added `R_OPINION` and an opinion
   pattern.
2. **The whole-prompt ceiling was self-defeating.** Counting the administrative
   roster against `NEXUS_CONTEXT_CHARS` meant that for an owner (~3700-character
   roster) the limit was already exceeded with nothing left to drop, so the only
   effect was to strip the room out of answers that needed it — the plan logged
   `dropped=awareness:ceiling` and `chars=3797 > 3500`. A ceiling may only bound
   what it can remove, so it now bounds the four selectable sources only.

**Measured** (`python3 tools/eval_context.py`, deterministic and offline):

* **selection**: **27/27** labelled cases (the brief's A–U plus an opinion case) —
  mode, selected sources, omitted sources, reasons and drops all exact; **0**
  isolation leaks; non-vacuity proved (the same shape carries the block for its
  own key).
* **budget (corpus)**: mean context **26.0 → 18.4 chars** (**29.1 %** smaller);
  the fast path is 9 of 27 cases.
* **budget (the real path** — `python3 tools/bench_context_real.py`; a
  20-message room, an owner's roster, state and memory seeded, model stubbed):
  context **40733 → 36127 chars** (**11.3 %** smaller); each fast-path message
  saves the whole room window (**~1100–1200 chars**); over 8 messages the room
  renders **16 → 8**, the reading **8 → 4**, memory **8 → 6**. The char and read
  counts are exact; the assembly latency (DB + composition, model excluded) is
  **p50 ~15–17 → ~10–12 ms**, **p95 ~36–41 → ~21–29 ms** across runs — it moves
  with host load, so it is quoted as a range.
* **the selector's own cost**: `read` **~0.15 ms p50 / ~0.5 ms p95**, `compose`
  **~0.02 ms p50 / ~0.08 ms p95** (host-dependent; the test floor is p95 < 5 ms);
  **0** model calls in the module's source (asserted).
* full suite **3568 passed, 0 failed**.

**Live probe — NOT RUN, and why.** The brief's probe compares a baseline against
Conversation + Awareness + State + relevant Memory and measures answer quality,
referent correctness, ambiguity handling, latency and model calls. It needs a
**chat provider credential** and a live Telegram room; the measurements above are
the **server's** contribution — what is selected, in what order, at what cost —
and are **not** evidence that answers improved. Claiming otherwise would be the
overclaiming this project refuses. The deterministic benchmark cannot score a
model answer; the live probe waits on the owner's go-ahead.

**Known limitations.** (1) The reading is a fixed set of deterministic signals; a
dependency expressed in an unanticipated wording is read as self-contained, and
the cost of that is a missing room block, never a wrong answer from the server.
(2) De-duplication matches on shared **words**, so a fact recorded in one script
and mentioned in another («Python» beside «پایتون») is not recognised as a
duplicate and both are sent. (3) The opinion pattern is a lexicon, not a parse.
(4) `ContextPlan` is not persisted and never rendered to the model — it exists
for the log and the tests.

**Rollback.** Y is independently revertable: `git revert <sha>` on this branch. No
table was added or altered; the only persisted change is the new
`NEXUS_CONTEXT_CHARS` default. W, the W extension and X are untouched. The whole
evolution reverts by leaving the branch unmerged; `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch.

**Exact next step.** INCREMENT **U** — "the room that should go next" (roadmap
§5/U): intelligent/adaptive Awareness scheduling **within the existing 200-request
allowance**, **no new model calls**. It has an open design question to resolve
first (§4.3: the seam that does not violate "`awareness.due` cannot see messages",
whose signature a test asserts). **V** (model routing) stays NOT SCOPED. **Do NOT
start U without the owner's explicit go-ahead.** Do not merge or deploy Y.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§5 for W/W-extension/X/Y/U/V, §7 for the
continuation point), then verify: `git status`, `git rev-parse HEAD`,
`git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`.

### 54.10 Checkpoint (2026-09-24, after U) — resume here (supersedes §54.9)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`. Increment
**U** (adaptive awareness scheduling) sits on §54.9. `main` and the annotated tag
`release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
and is an ancestor of HEAD. Suite **3619 passed, 0 failed** (was 3568).
**Not merged, not deployed.**

**What U is, and what it is not.** U is a **scheduling** increment, not a
semantic one. It answers exactly one question — *given a room that the existing
policy has already said may be read, is this request worth spending now?* — and
nothing else. There is deliberately **no** new reader, no new model call, no new
table, no numeric score, no reordering of the pass loop, and **no change to
`awareness.due`**, whose signature a test asserts. The 200-request allowance is
untouched.

**What U adds (DONE).**

* **`app/awareness_schedule.py`** (new) — the seam and the decision. One word per
  room (`high` / `low` / none), the strongest class seen among the room's
  **unread** messages, stored as `chat_id -> (class, monotonic stamp)`. `read`
  reuses the project's own `context_plan.read` for the single boolean
  `wants_awareness` and returns a word; `note` stores the maximum and refreshes
  the stamp; `priority` reads (dropping an expired entry); `forget` drops one
  room; `defer(chat_id, *, waited, waiting, now)` is the whole increment. Bounded
  by `MAX_ROOMS` (512, evicted oldest-first) and by `_bound()`, which is derived
  from `NEXUS_AWARENESS_RETENTION_SECONDS` — one number, no new knob.
* **`app/main.py`** — three small edits. `_awareness_capture` notes the class
  where the text is already in hand; `_awareness_run_room` consults `defer`
  **after** `due` and **before** the allowance, only on the ordinary path, and
  drops the hint in its `finally` when a pass actually runs.
* **`tools/eval_awareness_schedule.py`** (new) — the deterministic benchmark: ten
  room shapes, 28 rooms, 684 messages, 204 hand-labelled dependent, one simulated
  day at the real 200-request allowance, running the production `awareness.due`,
  the production allowance-gap formula, both scheduling paths, and the production
  `defer`. `--seeds N` aggregates; `--compare-ordering` reproduces the rejected
  mechanism.
* **`tests/test_awareness_schedule.py`** (35 tests) — the seam, the store, the
  decision, and the **failure matrix A–P** (every way the seam is asked a
  question it cannot answer, all resolving to *read*). The structural tests
  assert `defer`'s signature and body cannot see a message, that `awareness` does
  not import the scheduler, that no authorisation module imports it, and that no
  model/HTTP/Telegram client is imported.
* **`tests/test_awareness_schedule_eval.py`** (16 tests) — the floors under the
  benchmark and the **real path**: `main._awareness_capture` →
  `main._awareness_run_room` through the actual seam, with the transport stubbed.
* **`tests/conftest.py`** — `awareness_schedule.reset()` added to the global
  fixture. The hint store is process state keyed by chat id with a one-hour life;
  without this reset, a hint noted by one test deferred the next test's room and
  six `test_awareness_latency.py` tests failed as "awareness stopped reading".

**The mechanism decision — made by measurement, not by argument.** The first
candidate was **ordering** the pending list by class. Measured over eight seeds it
changed the outcome by **exactly zero passes**, and the reason is architectural:
the scheduler is **event-driven per room, not batch-driven** — each room is
offered a pass on its own debounce deadline and admitted or refused by its own
share of the allowance, so at any instant there is about one candidate and nothing
to sort. The second candidate — defer a low room only while another room holds
work that needs the room — is strictly safe but **cross-room** and worth about one
point. The shipped mechanism is the per-room **spend-or-wait** decision, which is
where the leverage actually is. Both rejected mechanisms are recorded in the
benchmark (the ordering is reproducible via `--compare-ordering`).

**Two defects found during verification** — both by running the real path, and
both invisible from the benchmark:

1. **The reader-error fallback was the wrong direction.** `read` fell back to
   `P_LOW` on a reader exception, which *causes* a deferral: a reader broken on
   every message would have deferred every room in the deployment, a
   deployment-wide slowdown wearing a scheduling choice's clothes. The fallback is
   now `P_NONE` (*no evidence*), which `note` refuses to store, so a broken reader
   leaves the room read exactly as before U existed.
2. **A deferred room delayed an owner's admin confirmation.** A confirmation
   («تأیید میکنم») is self-contained, so the classifier is right to call it
   `low` — but the pass is what *consumes* it, so the naive rule postponed the
   owner's already-approved action by up to the retention window. Three
   `test_nexus.py` confirmation tests caught it. `defer` gained `waiting`, the
   caller supplies `db.admin_pending_waiting(chat_id)`, and **a room the server is
   waiting on is never deferred**. This is the one place the mechanism can be
   *wrong* rather than merely slow, and it is closed at the source.

**Measured** (`python3 tools/eval_awareness_schedule.py`; deterministic, offline,
no Telegram, no model). Eight seeds, mean, same arrivals and same allowance:

* **useful passes**: **44.6 % → 67.6 %** (worst seed 41.5 % → 65.0 %); ordering
  alone, on the same workload, is **44.6 %** — i.e. exactly the baseline.
* **requests spent**: **200 → 200** (the allowance is not raised) and **model
  calls == passes** in both runs (no extra call anywhere).
* **fairness improved, not traded**: starved rooms **1.0 → 0.6** (worst 2 → 2);
  dependent messages left unread **105.4 → 59.1**; max wait **5400 s → 2732 s**;
  p95 wait **2107 s → 562 s**.
* **where the passes moved** (default seed): the hog `one_constant` **43 → 14**,
  `busy_independent` **18 → 11**, while `sparse` **20 → 27**, `many_active`
  **73 → 84**, `replies_anaphora` **9 → 16**, `addressed` **10 → 16**.
* **the decision's own cost**: `decide_ms_p95` **< 0.1 ms** (the test floor is
  < 5 ms); the hint store is a dict lookup.

**Live probe — NOT RUN, and why.** The brief's probe is a bounded, self-cleaning
live run against Telegram; there is **no deployed container carrying U** (the
increment is not deployed, and the brief forbids deploying it), so a live probe
would measure the *previous* build. The measurements above are the policy's, not a
model's, and are not evidence that any answer improved — claiming otherwise would
be the overclaiming this project refuses. The live probe waits on the owner's
go-ahead and a deploy.

**Known limitations.** (1) The mechanism trades **timeliness for coverage**: a room
whose batch reads as chatter is not read for up to `NEXUS_AWARENESS_RETENTION_SECONDS`
(1 h). Nothing is lost content-wise — the window still holds the messages, and a
message that changes the class raises the hint and the room is read on its next
deadline — so the exposure is exactly the classifier's false negatives. (2) The
**one false negative the server can know about** (a pending admin confirmation) is
closed. A **residual remains**: a bare answer to the assistant's own question
(«بله») carries no room dependency and no server flag, so it can be postponed like
any other chatter. Closing it needs a "the assistant is awaiting an answer" flag
the server does not currently keep; it is recorded rather than guessed at.
(3) The class is one of two words from a deterministic reader; a dependency worded
in an unanticipated way reads `low`, and the cost of that is a delayed reading,
never a wrong action.

**Rollback.** U is independently revertable: `git revert <sha>` on this branch. No
table was added or altered, no config default changed, and `awareness.due` is
untouched. The whole evolution reverts by leaving the branch unmerged; `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch.

**Exact next step.** **V** (model / Gemini routing) remains **NOT SCOPED**: it
needs an answer-quality measurement that does not exist, and the honest first step
would be to build that evidence base rather than to change routing. **Do NOT start
V, do NOT merge, do NOT deploy U.** Do not raise the 200-request allowance.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§4.3(d) is now RESOLVED, §5 for U/V, §7 for
the continuation point), then verify: `git status`, `git rev-parse HEAD`,
`git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`.

### 54.11 Checkpoint (2026-09-24, after the V evidence base) — resume here (supersedes §54.10)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`. The V
evidence base sits on §54.10, in two commits — `4a84756` (the tool and the
fixture) and `c58fa2a` (the tests). `main` and the annotated tag
`release-base/nexus-intel` both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
and is an ancestor of HEAD. Suite **3640 passed, 0 failed** (was 3619).
**Not merged, not deployed.** Nothing in production changed: no routing, no
config default, no table, no awareness allowance.

**What this is, and what it is not.** It is the **measurement** V was blocked on,
and only the measurement. `docs/intent-awareness-roadmap.txt` §5/V said it
plainly: *"There is currently NO measurement of model quality anywhere in this
repo… This increment cannot be planned until an evidence base exists; the honest
first step would be to BUILD that evidence base, not to change routing."* That is
what was built. **V itself is still NOT SCOPED** — producing a measurement is not
starting V, and no routing decision has been made or implied.

**What was added (DONE).**

* **`tools/eval_chat_quality.py`** (new) — the corpus, the deterministic scorer
  and the live harness, in one script.
  * **The corpus**: 14 scenarios across the six categories the docs name —
    `referent`, `ambiguity`, `grounded`, `correction`, `self_contained`,
    `action_safety`. One grounded scenario is a deliberately **absent fact**
    («پسورد root سرور چیه؟»), so "always confident" cannot score full marks.
  * **The scorer**: each scenario carries machine-checkable rules (`all_of`,
    `any_of`, `none_of`, `asks`, `claims_action`, `admits_unknown`, `max_chars`,
    `room_markers`) matched against the folded text (ZWNJ removed, Arabic-Indic
    digits folded, whitespace collapsed). **No LLM judge.** A `(scenario, arm)`
    pair whose every sample was skipped or errored is **NOT RUN** and leaves
    every denominator — a gap, never a zero.
  * **The harness**: drives the **real** `main._answer_conversationally` and the
    **real** `chat.reply`, with `DB_PATH` forced to `:memory:`, the search and the
    awareness note stubbed, each scenario in its own room and its own person, and
    the whole process restored in a `finally`.
  * **Two arms**: `--arm context` (default) compares the real `context_plan.read`
    against the pre-Y "render everything" reading, installed by swapping
    `context_plan.read` — **that is Y's outstanding probe**. `--arm model` pins
    one model per arm (`config.GEMINI_CHAT_MODEL`, a one-element `models` list on
    the chat pool spec, then `build_pools`, because `Pool.models` is frozen at
    build time and chat **rotates**) and compares them. `--no-pin` trades
    attribution for the pool's ordinary failover.
* **`tests/test_chat_quality_eval.py`** (new, 21 tests) — all offline, zero model
  calls. The floors are the two ways a benchmark lies: **vacuity** (four targeted
  mutations each move exactly their metric while a control category stays 1.0)
  and **reporting a number it did not measure** (the credential gate prints NOT
  RUN and never calls `chat.reply`; the pre-Y arm is proved to render a *larger*
  prompt than the real reading on the real path).
* **`tools/fixtures/chat_quality_transcripts.json`** (new) — a **synthetic**
  transcript for both arms, generated from the corpus's own labelled good answers
  and round-trip asserted, so a corpus edit and the fixture cannot drift.

**The live run: NOT RUN — and the reason is the provider, not the tool.** The
authorised run is:

```
docker run --rm --env-file .env -v "$PWD:/srv" -w /srv guardbot-guardbot \
  python tools/eval_chat_quality.py --arm context --samples 2 --max-calls 60
```

It was attempted three times. Each attempt ended with the chat workload's own
attempt budget spent on `503`/`504` from the provider. A **bare `google-genai`
call, outside this application entirely**, then returned
`503 UNAVAILABLE … This model is currently experiencing high demand` for
`gemini-flash-lite-latest`, `gemini-flash-latest`, `gemini-3.5-flash-lite`,
`gemini-3.1-flash-lite` and `gemini-3.7-flash` — every chat model, on every one
of the four configured accounts. A bounded 2-request probe reported
`answered_rate 0.0`, `not_run 1`. **No numbers are reported from it.** A quality
score computed from an empty transcript would be invented, and making that
impossible is the tool's whole purpose. This is the same provider-side
degradation recorded on 2026-09-23, still in effect. The run needs only a
healthy provider — nothing else is outstanding.

**Two defects found by running the real path** — both invisible from the offline
suite, and both now closed:

1. **A logger level that was never restored.** The tool's `main()` lowered the
   `guardbot` logger to `WARNING` to keep its own output readable, and left it
   there. Four `tests/test_classifier.py` `caplog.at_level("INFO")` assertions
   then failed in a full-suite run — for a reason that was not in those tests.
   The level is now saved and restored around the run.
2. **Cleanup after the connection was closed.** `_bench` rebuilt the pool
   registry *after* dropping the database connection, and `build_pools` reads the
   database once per account, so cleanup raised
   `'NoneType' object has no attribute 'execute'`. It fires **only on a
   deployment that has credentials**: with no key the pool has no accounts, the
   per-account loop never runs, and the offline suite passes. That is exactly why
   a live run is not optional. Fixed by removing the rebuild (the pinning helper
   already restores the registry while the connection is open), with a regression
   test that populates the pool before cleanup.

**Honesty, printed with every report.** It measures **text against authored
rules** — never tone, helpfulness or politeness. `ChatReply.model` is the
**requested** model, not necessarily the serving one; that is recorded as a
limitation and a finding for V, not fixed here. Temperature is 0.8, so a report
carries the sample count and the spread rather than one number as a verdict. The
corpus is small (14 scenarios) and was authored by the same project that answers
it: it is a floor to move on purpose, not a verdict.

**Rollback.** Two new files and one new fixture; no production file is touched, so
the whole increment reverts by `git revert 4a84756 c58fa2a` on this branch, or by
leaving the branch unmerged. `main` at
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` is the production state and is an
**ancestor** of the branch.

**Exact next step.** Run the command above **once the provider answers**, and read
`--arm context` as Y's probe: does the real reading hold up against "render
everything"? Then, and only then, is there a basis to discuss V's routing question
at all — and that discussion needs the owner's go-ahead. **Do NOT start V, do NOT
merge, do NOT deploy U, and do NOT raise the 200-request allowance.** The
`--arm model` comparison is built and deliberately unrun: running it answers V's
own question.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§4.3(e) is now RESOLVED, §5 for U/V, §7 for
the continuation point), then verify: `git status`, `git rev-parse HEAD`,
`git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`.

### 54.12 Checkpoint (2026-09-24, post-audit cleanup) — resume here (supersedes §54.11)

**Where the work is.** Branch `develop/nexus-intelligence-evolution`. The
post-audit cleanup is the commits `50a45ee` (the fix), `62474be`, `ac44f62` and
the freeze clarification, **committed and pushed to both remotes** (`origin` =
mo3iiibest77-hub, `dashmo3i` = Dashmo3i-GitAcc; the branch tip equals local HEAD
on both and is 0/0 ahead/behind — verify with `git ls-remote`, and do **not**
pin a HEAD SHA in prose, because every doc commit moves it). This increment sits
on §54.11 (`06f31a0`, the Token & AI Workload Audit). `main` and
the annotated tag `release-base/nexus-intel` are both still
`00c5d1dd412e033c6ac15599b28bc0fbcb54d709` — **the rollback point is untouched**
and is an ancestor of HEAD. **Not merged, not deployed.** No credential was
added, removed, moved or rotated; no token reallocation was performed. The one
live deployment (`docker ps` → container `guardbot`, image `guardbot-guardbot`,
created 2026-09-23T20:15:33) is main-based and carries **neither** U nor V (its
filesystem has no `app/awareness_schedule.py`, `app/context_plan.py` or
`tools/eval_chat_quality.py`).

**What this increment is.** The post-audit cleanup: the open items the final
audit recorded were each inspected against the implementation and either fixed
(if they were real, bounded, in-repo defects) or documented (if they need a new
secret or an external provider). It is **not** a new roadmap stage and **not** V.

**What was changed (DONE).**

* **U's residual is CLOSED** — the one item the audit listed as needing an
  "awaiting an answer" flag.
  * `app/awareness_schedule.py`: a second, content-free store
    (`chat_id -> monotonic stamp`), `_AWAIT_QUESTION_RE` (a question mark,
    `?`/`؟`, at the end of a string), and `awaiting_note` / `awaiting` /
    `awaiting_size`. `defer` now refuses to postpone a room carrying the stamp;
    `forget` spends it (a pass read the room) and `reset` clears it; the store is
    bounded by `MAX_ROOMS` and expires by `_bound()`, exactly like the hint
    store. **`defer`'s signature is unchanged** — `{chat_id, waited, waiting,
    now}` — so the structural message-blindness test still passes untouched; the
    stamp is consulted from the module's own store, the same way `defer` already
    consulted `priority(chat_id)`.
  * `app/main.py`: `_awareness_note_reply` — the function that records Nexus's
    own outbound reply, called only after a successful send — now also calls
    `awareness_schedule.awaiting_note(chat_id, said)`. The member capture path
    (`_awareness_capture`) does **not** touch the stamp.
  * **Design, and why it is not a second state system.** The flag lives in the
    same module and the same shape as U's hint store, not in `app/state.py`:
    State is about the *user's* task and question, keyed by `(chat_id, user_id)`;
    this is about *Nexus's own* outbound question and is room-scoped for
    scheduling, keyed by `chat_id` alone. It records only *that the server
    asked* — it never reads a member's message, never decides which message
    answers, and never guesses between speakers. The pass reads the whole window
    and does the interpreting with the context it always had. Deterministic, no
    model call, no new request, does not block Chat, grants nothing, no
    cross-room or cross-user leakage, bounded, replay-safe (a duplicate delivery
    re-stamps one entry), and self-limiting (at most **one** undeferred pass per
    question, because the pass spends the stamp).
  * Tests: `tests/test_awareness_schedule.py` (+13 tests: the stamp, both
    question marks, statements and empty text, the deferral refusal, expiry,
    `forget`, `reset`, room isolation, idempotency, malformed id, and a
    structural test that the member capture path cannot set it);
    `tests/test_awareness_schedule_eval.py` (+3 real-path tests through
    `main._awareness_run_room`: a question un-defers the room and the stamp is
    spent, a statement does not, and another room's stamp does not).
* **Roadmap §2.4 (cosmetic duplication) FIXED** — `app/objects.py`'s
  `CLASS_THING` branch no longer interpolates the label "a thing" into a sentence
  that already says "a thing, not a person". Meaning and every asserted substring
  are unchanged and the line is shorter. Sibling branches untouched.
* **Roadmap §2.2 and §2.3 RECONCILED, NOT changed** (recorded in the roadmap).
  §2.2's `objects.Object.source` is **not** a dead field — the harness scores
  `object_source_accuracy == 1.0` (`tests/test_intent_eval.py`) and
  `tests/test_objects.py` asserts it — so removing it would break both. §2.3's
  `requests.render` wording (one hardcoded reason for two routes) would rewrite a
  rendered PROMPT line with an unmeasured corpus effect, so it stays a recorded
  finding; the accurate reason is available in `request.why`.
* **`GEMINI_MEMORY_API_KEY` DOCUMENTED** — `.env.example` gained a memory
  workload section: the credential is memory-only, must never be a chat or
  awareness key, must not be in the shared pool for memory without the explicit
  `GEMINI_MEMORY_ALLOW_SHARED_KEY` opt-in, and is **not needed at all** in the
  shipped deterministic default (setting it alone does not enable the seam;
  `NEXUS_MEMORY_EXTRACT_MODEL` must also be on). No secret was written.

**What was NOT changed, deliberately.** The two credential collisions remain and
are **documented, not fixed**, because fixing either needs a NEW secret (a
genuinely different Google project), which this phase must not invent or move:
  * `fp 24b50725` = `GEMINI_CHAT_API_KEY_5` **==** `GEMINI_AWARENESS_API_KEY_2`.
    In use by two workloads; awareness therefore has only one fully-isolated key.
  * `fp 20ed3899` = `GEMINI_LIVE_API_KEY` **==** `GEMINI_SEARCH_API_KEY`
    (the search Gemini path is dormant; `SEARCH_PROVIDER=tavily`).
  No reallocation was done. The 6/5 Chat/Awareness target remains a **plan only**
  and is **not applied**, pending a trustworthy health probe. `TAVILY_API_KEY`,
  `BOT_TOKEN`, `TELEGRAM_API_ID/HASH`, `VPNBOT_SHARED_SECRET` are untouched;
  TTS still mirrors Chat; Search stays on Tavily; Memory and Live Voice stay at
  zero allocation.

**Provider degradation (external limitation).** When first observed
(2026-09-24 ~13:30) the chat provider returned `503 UNAVAILABLE` for every chat
model on every account (confirmed by a bare `google-genai` call outside this
application); the recheck below shows it is in fact **intermittent**, not a
global outage. Every live probe is therefore **NOT VERIFIED / BLOCKED BY
PROVIDER**, never "failed" and never "healthy": Y's `--arm context`, U's live
probe, V's `--arm model`, the memory model seam, and Voice Live end-to-end. No
fake health result was produced and no number was claimed from a run that did
not complete.

**Provider recheck (2026-09-24, at checkpoint close).** A fresh bare probe
changed the picture in two ways, and the live probe was re-attempted on it:

* The 503 is **intermittent and per-project, not global**. `models.list`
  succeeds on every key; a single `generate_content` on
  `gemini-flash-lite-latest` succeeded on **6 of 9** chat accounts
  (`ad4bfbe4`, `23c19e3b`, `295c2a86`, `24b50725`, `2a52d966`, `4a75741b`) and
  503'd on three (`5148caba`, `599a1072`, `5ff073ee`); the same account returned
  OK and then 503 minutes apart. The awareness key served `gemini-flash-lite-latest`
  too.
* **One credential is dead, not degraded: `GEMINI_API_KEY` (fp `7c707e1b`),
  the `intent` workload's primary (slot 1, `app/config.py:2755`), returns
  `401 UNAUTHENTICATED` on every model** — an invalid/revoked key, not high
  demand. Its only sibling `GEMINI_API_KEY_2` (fp `a3aae772`) is transiently
  503. This is **reported, not fixed** — no token is added, moved, rotated or
  removed without the owner's instruction.
* `tools/eval_chat_quality.py --arm context --samples 2 --max-calls 60
  --time-budget 90` was **re-attempted and is still NOT RUN**: 10 calls made,
  every one 503, the pool reported `usable=0/9`, the chat breaker opened. Both
  arms report `not_run: 14`, `answered_rate 0.0`, `scored_samples: 0` — the
  honest NOT-RUN result, no number claimed. **NOT VERIFIED / BLOCKED BY
  PROVIDER.** Do **not** spend another run on it while the provider is still
  intermittently 503 — the owner has explicitly frozen this probe.

**FREEZE — the current, unambiguous state (read this before acting).** The only
outstanding verification is the live context-quality run
(`tools/eval_chat_quality.py --arm context`, Y's probe), but it is currently
**FROZEN by the owner** because the provider is intermittently returning 503.
It must **NOT** be run until the owner explicitly lifts the freeze. Therefore,
**while the freeze remains active there is no executable next implementation
step from the current audit checkpoint** — the next session must not invent one,
must not re-run the probe, and must not run any new health or provider test to
"check" it. The other two items recorded in this section (the dead `intent`
primary and the credential collisions) are likewise **reported, not actionable**
without the owner's explicit instruction.

**Tests.** Targeted (this increment's files, run in the project image):
`tests/test_objects.py tests/test_requests.py tests/test_intent_eval.py
tests/test_awareness_schedule.py tests/test_awareness_schedule_eval.py` →
**247 passed, 0 failed**. Full suite, run in the same image →
**3658 passed, 0 failed** (baseline before this increment: **3640 passed**; this
increment adds 16 tests — 13 deterministic, 3 real-path — the remaining +2
predate the increment).

**Build & regression verification (2026-09-24, HEAD `7d5ab6b`).** Re-verified
from a clean tree on the frozen branch, using the repository's **non-Docker**
test path (`AgentMD.md` §11: "a plain venv can run the suite"). **The Docker
image was not rebuilt** — the owner's instruction forbids it, and the project's
only official build (`docker compose build` / the `Dockerfile`) is
Docker-dependent, so no non-Docker *build* exists; the existing image
`guardbot-guardbot:latest` and the container `guardbot` were left untouched (an
image built during the previous turn was removed, restoring the environment).
Non-Docker build check: `python -m compileall app tools` → exit 0 (every module
byte-compiles). Tests, in `.venv-test` (Python 3.10.12, pytest 9.1.1):
collection **3658 tests**; targeted set (the same five files) → **247 passed**;
full suite → **3658 passed, 0 failed, 0 skipped, 0 errors** (7 warnings, all a
`google-genai` `DeprecationWarning`). No failure, so no fix was needed. These
match the numbers already recorded above.

**Production latency diagnosis (2026-09-24, read-only trace).** The owner
reported that replies are noticeably slow. Traced from the live container's logs
(`guardbot`, main-based `00c5d1d`, Up 20h — no code change, no restart). One
real reply (user `6931339207`, chat `-1001299527312`, 16:35:42 → 16:36:30):
`prepare_ms=0`, `gemini_ms=40408`, `send_ms=172`, `total_ms=40580` → **≈48.5 s
end-to-end, and 40.4 s of it is the provider call**. Across **151** real chat
replies: p50 **5.3 s**, p75 18.7 s, p90 44.3 s, p95 95 s, p99 135 s, max
**143 s**; **15 % over 30 s, 8 % over 60 s** — the median is healthy, the tail
is not. `prepare_ms` is 0 and `send_ms` ≤ 1.1 s, so neither context assembly nor
the Telegram send is the cost.

*Root cause, in order.* (1) **Provider degradation** — 503/504 on most models
(266 `provider_error`: 183×503, 83×504 in ~20 min); the healthy path is a
first-model answer in 9–12 s, now the walk burns 14–22 attempts. (2) **Free-tier
quota** — 120 `rate_limited` = `generate_content_free_tier` on chat *and*
awareness; the keys are free-tier projects and **quota is the binding
constraint**. (3) **The walk is sequential** (`app/gemini_pool.py` `generate()`:
`for account → for model → for attempt → await _call`, with `await
asyncio.sleep(1.5)` between retries), up to 12 attempts per logical call and up
to 2 logical calls per reply, each failure costing 0.3–2 s (503) or up to 25 s
(a timeout). (4) **Awareness contention** — it runs in its own 15 s sweep, not
inline with chat, but overruns it (**137 passes vs 139 skips**) and **shares a
key with chat** (`iqnA` = fp `24b50725`). (5) **Intent adds a floor** — it burns
its ~21.5 s budget (`2×10 + 1.5`) on the failing provider before falling back to
the rules. (6) **A dead intent key** — `bxvA` = `GEMINI_API_KEY` (fp `7c707e1b`)
= 401 `invalid_credential`, an attempt spent on every intent call.

*What it is NOT.* Not a code regression: the running container is main-based
`00c5d1d` and no code has landed on `main` since. Not the rate limiter or the
circuit breaker — those **skip**, they do not sleep. Not the Telegram send or
the update path. **The new chat key does not help**: the bottleneck is provider
health + **per-project** free-tier quota, so another free-tier key adds one more
exhausted quota, not a faster answer (9 chat accounts still fail over 14+ times).

*Status.* **Diagnosis only — no fix applied** (owner's instruction), no probe
run (still FROZEN), no token moved. This is a candidate for the owner to
prioritise next; it is **not** a checkpoint NEXT STEP.

**Nexus responsiveness fix — the account breaker (2026-09-24, DONE, not
deployed).** The owner asked to make Chat usable again from the diagnosis above.
The change is the **smallest safe one** and is confined to the pool's own
routing; **no token was added, removed, moved or rotated**, and no credential
policy changed.

*Diagnosis, from the live DB and logs (not guessed).* `gemini_accounts` for
`chat` held **4** rows, every one `state=ACTIVE`, `cooldown_until=0` — while
their lifetime failure counters read **1058 / 927 / 400 / 388** and their
`rate_limits` read **625 / 465 / 170 / 175**. `gemini_events` held **no
`account_failover` row at all**; every chat event was `model_failover`. The
container logs show the same account re-tried across models on one message
(`BCIQ`: `3.6-flash` 429 → `flash-lite-latest` 504 → `3.5-flash-lite` 504 →
`3.5-flash-lite` 504) and the same model re-tried on a *different* account
minutes later. **Root cause: the pool benched the *model* and never the
*account*.** `ModelState.note_failure` sets a model cooldown; `Account.trip`
fires only on `SCOPE_ACCOUNT` faults (401 / project quota), which a per-model
429 and a 503/504 are not. So an account out of allowance on every model it
offered stayed `ACTIVE`, `ordered_accounts` returned it every time, and every
message re-walked all four accounts and re-paid for the same failures.

*Health check of every configured Chat credential (read-only, fingerprints
only, one small `generate_content` per key, no DB/pool/key-store write).* All
**9 configured chat keys answered** — `ad4bfbe4` 11.6 s, `5148caba` 18.6 s,
`23c19e3b` 15.0 s, `295c2a86` 15.9 s, `24b50725` 17.6 s, `2a52d966` 21.7 s,
`4a75741b` 13.2 s, `599a1072` 20.3 s, `5ff073ee` 24.4 s. **Healthy 9,
unhealthy 0** — which is itself the finding: three of these keys 503'd in the
13:30 recheck and none does now, confirming the 503 is **intermittent**, and the
11.6–24.4 s cost of a five-token call is the provider being **slow**, not down.
No key was modified.

*Implemented behaviour (`app/gemini_pool.py`, `app/config.py`).* A per-account
consecutive-failure breaker, on top of the existing model cooldowns — the
existing pool is reused, no parallel pool, no ordering change (the pinned
least-recently-succeeded rotation is untouched):
  * `Account.consecutive_failures` — in-memory, incremented by
    `note_failure` **only** for the kinds that mean "the project is unwell"
    (`rate_limited`, `provider_error`, `network_error`, `timeout`,
    `unknown_error`; the `_BENCH_KINDS` tuple), cleared by `note_success` and by
    `trip`. `bad_request` / `unsupported_input` / `unsupported_model` are
    **excluded on purpose**: they fail identically on every account, so counting
    them would bench the whole pool over one malformed message.
  * `Account.should_bench()` — `consecutive_failures >=
    GEMINI_POOL_ACCOUNT_FAILURE_THRESHOLD` (**new env knob, default 3**), read
    from config on each call so a reload is honoured without rebuilding.
  * `_bench_repeated_failures(pool, account, model, now)` — trips the account
    `UNAVAILABLE` for `GEMINI_POOL_TRANSIENT_COOLDOWN` (**60 s**) and records an
    `account_failover` event (`reason=repeated_failures`), so the state is
    observable for the first time. Called from both the timeout path and the
    general failure path in `generate()`, **before** the scope dispatch, so the
    walk moves to the next **account**, not to another model on the same bad
    credential.
  * **Threshold 3, not 1**: a single 503 is a wobble, and it is deliberately
    above the two failures one retried model produces, so a retry that succeeds
    on its second attempt never trips the breaker.
  * **Bounded attempts are unchanged and still bound**: the total is
    `GEMINI_POOL_MAX_ATTEMPTS` (12) shared as `12 // remaining_accounts` per
    account, `attempts_per_model = retries + 1`, and the `intent`/`chat`
    wall-clock ceilings are untouched. The breaker **shortens** the walk — once
    unhealthy accounts are out, `ordered_accounts` returns fewer of them, so the
    fair share and the walk both shrink.
  * **Workload isolation preserved**: each pool owns its own `Account` objects,
    so the same credential benched in `chat` stays `ACTIVE` in `awareness` or
    `intent` — asserted by a test. `awareness`, `intent`, `moderation`,
    `transcribe`, `memory`, `live_voice`, `search` share no failure state with
    `chat`.

*Tests (DONE).* `tests/test_gemini_pool.py` **+9 focused tests** in a new "THE
ACCOUNT BREAKER" section: repeated failures bench the account (and the walk
stops on the third, not the whole list); a benched account is **skipped by the
next request** (the regression that caused this); a free-tier `rate_limited`
counts toward the breaker while still benching the model; repeated **timeouts**
bench the account; a success clears the streak; an account below the threshold
stays in rotation; a **payload fault never benches** the account (threshold 1);
benching is recorded as an `account_failover` event; and the breaker is
**per-workload**. Targeted → **134 passed** (`tests/test_gemini_pool.py`); full
suite, `.venv-test` → **3667 passed, 0 failed, 0 skipped, 0 errors** (baseline
3658; +9). `python -m compileall app tools` → exit 0.

*Remaining limitations, stated honestly.*
  1. **The fix is not deployed.** The running container `guardbot` is
     main-based `00c5d1d` and does **not** contain this change; no rebuild, no
     restart, no recreate, no merge was performed (owner's instruction). Until
     it is deployed the live behaviour is unchanged.
  2. **Only 4 of the 9 configured chat keys are loaded.** The container's env
     was frozen at creation (2026-09-23T20:15:33), when `.env` had
     `GEMINI_CHAT_API_KEY`…`_4`; the five newer keys (`_5`…`_9`) are read at
     startup only, so they are inert. Picking them up needs a container recreate
     — **not done, deliberately**. `build_pools()` re-reads the *runtime key
     store* per reload, so a key added **there** (not in `.env`) would load
     without a restart; that is the owner's lever, not this change's.
  3. **Provider latency and per-project free-tier quota are outside our
     control** — 11.6–24.4 s for a trivial call, and `generate_content_free_tier`
     is a property of the projects. The breaker bounds the *walk*; it cannot
     make the provider fast or give a free-tier project more quota.
  4. **Not fixed, still reported:** the dead `intent` primary
     `GEMINI_API_KEY` (fp `7c707e1b`, 401) and the two credential collisions
     (`24b50725`, `20ed3899`) — all need the owner's instruction.
  5. **No live probe was run and none may be**: the `--arm context` probe stays
     **FROZEN** and this work did not touch it. The breaker is verified by the
     deterministic suite, not by a live end-to-end run — that verification is
     still blocked by the freeze.
  6. **V is unchanged: NOT IMPLEMENTED, NOT ROUTED, NOT ACTIVATED.**

**V — explicitly, as required.**
* **NOT IMPLEMENTED** — no routing change, no model allocation, no config
  default changed, no new production call.
* **NOT ACTIVATED** — `--arm model` is built and deliberately unrun.
* **NOT ROUTED** — V itself remains NOT SCOPED. Its evidence base
  (`tools/eval_chat_quality.py`, the fixture, `tests/test_chat_quality_eval.py`)
  exists and is unchanged.

**Rollback.** Revert this checkpoint's commits on the branch (`git revert`), or
leave the branch unmerged; `main` at `00c5d1dd412e033c6ac15599b28bc0fbcb54d709`
is the production state and is an ancestor of HEAD.

**State at checkpoint close.** The commit and the push are **done**: the branch
tip on both remotes equals local HEAD, working tree clean, `main` still
`00c5d1d…`, full suite **3667 passed / 0 failed** (3658 before the account
breaker, which added 9 tests). Nothing is pending except the live run, which the
owner has **frozen** — see FREEZE above. While the freeze holds, **there is no
executable next implementation step**.

**NEXT STEP (do this first in the next session).**
1. Read this section, `docs/intent-awareness-roadmap.txt` (§2.2–§2.4, §5/U
   RESULT's residual-closed note, §7) and `docs/TOKEN_AI_WORKLOAD_AUDIT.txt`.
2. Verify (should already hold): `git status` clean, `git rev-parse main` =
   `00c5d1d…`, and `git ls-remote origin
   refs/heads/develop/nexus-intelligence-evolution` = local HEAD (do not pin a
   HEAD SHA in prose — every doc commit moves it).
3. **There is no executable next implementation step while the freeze holds.**
   The only outstanding verification is the live context-quality run
   (`--arm context`, Y's probe); it was re-attempted on 2026-09-24 and is still
   **NOT RUN** (503, `usable=0/9`), and it is **FROZEN by the owner**. Do **not**
   run it, do **not** repeat the blocked probe, and do **not** run a new health
   or provider test to check it, until the owner explicitly lifts the freeze.
   Likewise **not to be fixed without instruction**: the `intent` primary
   `GEMINI_API_KEY` (fp `7c707e1b`) is dead (401), and the credential collisions
   remain reported-not-fixed.
4. Do **NOT** start V (its `--arm model` stays unrun until the owner authorises
   V), do **NOT** merge, do **NOT** deploy, do **NOT** raise the 200-request
   allowance, and do **NOT** reallocate tokens.
5. Keep the probe recorded as **NOT VERIFIED / BLOCKED BY PROVIDER** — never
   invent a result from a run that did not complete.

**To resume after any context loss.** Re-read this section and
`docs/intent-awareness-roadmap.txt` (§2.2–§2.4, §5, §7), then verify `git status`,
`git rev-parse HEAD`, `git rev-parse main` (`00c5d1d…`), and
`git ls-remote origin refs/heads/develop/nexus-intelligence-evolution`.

> **Superseded by §54.13** — the branch has since been merged into `main` and
> deployed, so `main` is no longer `00c5d1d`. Read §54.13 for the current state;
> the `00c5d1d` facts above are kept only as the pre-deploy record.

---

### 54.13 Checkpoint (2026-09-24, **DEPLOYED**) — resume here (supersedes §54.12)

**This is the first increment of the Nexus line to reach production.** The owner
instructed "deploy the fix and recreate the container", and chose — when asked,
because the two differ by 63 commits — **"whole branch, via main"**, not
"only the pool fix". So the entire `develop/nexus-intelligence-evolution` line
(Q–R–S–T–W–X–Y–U + the post-audit cleanup + the account breaker `26c65ff`) is
now live. **V is still NOT IMPLEMENTED, NOT ROUTED, NOT ACTIVATED**, and the
`--arm context` probe is still **FROZEN** — the deploy did not touch either.

**What was deployed.**
* **Merge:** `git merge --no-ff develop/nexus-intelligence-evolution` into
  `main` → merge commit **`25ddee81846dcd2da3cb7555c56e96951e591878`**. The tree
  is byte-identical to the branch tip (`git diff <branch> main` is empty).
  Pushed to **both** remotes and verified with `git ls-remote`: `origin` and
  `dashmo3i` both carry `25ddee8` on `main`.
* **Build:** the documented path (`AgentMD.md` §12) — `docker compose build`,
  then `docker compose up -d`. The build took **5 s** because
  `requirements.txt`/`Dockerfile` are unchanged from `main`, so the ffmpeg and
  `pip install` layers stayed cached; only `COPY app` / `COPY tools` were
  rebuilt. New image **`guardbot-guardbot:latest` = `8d4c6fc63293`**.
* **Container:** `guardbot` **recreated** (not just restarted) at
  `2026-09-24T17:06:21Z`, `RestartCount=0`, no tracebacks. It now loads the
  current `.env`, which is why the chat pool went from **4 to 9** accounts.

**Migrations ran on the live DB.** The branch's two schema additions were
applied at startup: **`user_memory`**, **`user_memory_signal`** (W) and
**`conversation_state`** (X) now exist (32 tables total);
`pragma integrity_check` → `ok`. `data/guardbot.db` was backed up **before** the
deploy (`data/guardbot.db.bak-20260924-170535-predeploy-nexus-merge`,
`integrity_check: ok`) along with the runtime key store.

**Rollback, all still in place.**
* Image **`guardbot-guardbot:pre-00c5d1d`** = `1a3ccbe0b7af` — the exact image
  that was running before the deploy.
* Annotated tag **`release-base/nexus-intel`** still points at
  **`00c5d1dd412e033c6ac15599b28bc0fbcb54d709`**, the pre-merge production
  commit. `main`'s first parent is that commit, so `git revert -m 1 25ddee8`
  (or checking out the tag and rebuilding) is a clean path back.
* The pre-deploy DB backup above.

**Pool state after the deploy** (from the startup line and the DB):
`chat 9/9 usable`, `tts 9/9`, `awareness 3/3`, `intent 2 configured / 1 usable`
(the dead key below), `moderation`/`transcribe`/`live_voice` 1 each,
`memory 0` (not enabled), `search 0` (dormant, Tavily is the provider). The five
previously-inert `GEMINI_CHAT_API_KEY_5…_9` are now live.

**The breaker fired in production, on its own.** Within the first 50 s the new
account breaker benched the awareness pool — `gemini_events` 936 / 937 / 939 are
`account_failover` with **`reason=repeated_failures`**, and 938 is `pool_critical`
(`usable=1/3`) then 940 `pool_empty` (`usable=0/3`). It recovered on schedule:
event **942 `account_recovered`**, and all three awareness accounts are
`state=UNAVAILABLE` with the cooldown lapsed, i.e. **usable again** — the
"temporarily" in "temporarily cooled down" is doing real work. An awareness pass
that had burned **46.5 s** and failed (`gemini_ms=46421`) was followed by one
that **completed in 13.4 s** (`gemini_ms=13195`) once the bad accounts were out.

**Live end-to-end probe (self-cleaning).** Three real requests through the
**deployed** container's real `chat.reply` path (not a reimplementation), with
synthetic ids and a `finally` that restores the baseline:
**3/3 answered, every one on the first model (`gemini-flash-lite-latest`), no
failover walk — 9.82 s / 11.35 s / 16.70 s (median 11.35 s)**. The pre-deploy
baseline was p50 5.3 s but **p90 44.3 s, 15 % over 30 s, and 14–22 sequential
attempts**; these three are single-attempt and sit inside the provider's own
11.6–24.4 s range for a trivial call, so the walk overhead is gone. Cleanup
verified: history rows `0 → 6 → 0`, `chat_usage` counters restored. The
**Telegram update→reply leg** was not exercised (that needs a real member's
message; the groups were quiet) — it is unchanged by this work and measured at
`send_ms ≤ 1.1 s` in the latency diagnosis, but it is honestly **not** part of
this probe's evidence.

**Verification incident, recorded because it happened.** The probe's first
cleanup used `delete from gemini_events where id > <row COUNT>`, which is
unsound — event ids are sparse, so the count is not a boundary. It deleted
**three** live rows (ids **940**, **941**, **942**; awareness events created by
the running bot during the probe window). All three were recovered **byte-exactly
from the SQLite WAL** (the deleted rows were still in the un-checkpointed frames)
and re-inserted with their original ids: 940 `pool_empty`/`no usable accounts`/
`usable=0/3`, 941 `model_failover`/`rate_limited`, 942 `account_recovered`.
`gemini_events` is back to `count=941`, `max id=942`, `integrity_check: ok`. The
recovery was validated by re-parsing the WAL and confirming the already-restored
row 940 came back identically. **No further probe run deletes events**; the
boundary is `max(id)`, and the lesson is that a probe must never delete rows it
cannot prove it created while the live process is writing to the same table.

**Remaining limitations.**
1. **The real Telegram reply latency is still unmeasured** for this deploy — no
   addressed message arrived during the window. The probe covers everything
   except the update/send legs.
2. **Awareness is still the weakest workload**: free-tier `generate_content_free_tier`
   429s and provider 503/504 on all three accounts, and `_awareness_deadline_tick`
   is still being skipped ("maximum number of running instances reached") when a
   pass overruns. The breaker shortens the walk; it cannot create quota.
3. **Not fixed:** the dead `intent` primary `GEMINI_API_KEY` (fp `7c707e1b`, 401)
   and the two credential collisions (`24b50725` chat:5==awareness:2,
   `20ed3899` live_voice:1==search:1). Both need the owner's instruction.
4. **Provider latency and per-project free-tier quota** remain outside our
   control.

**State at checkpoint close.** `main` = **`25ddee8`** on both remotes;
`develop/nexus-intelligence-evolution` fast-forwarded to the same content; the
running container is `8d4c6fc63293` (Up, `RestartCount=0`); `release-base/nexus-intel`
still `00c5d1d`; the pre-deploy image and DB backup both retained.

**NEXT STEP (do this first in the next session).**
1. Read this section; then verify `docker ps` shows `guardbot` up on
   `guardbot-guardbot:latest` and `git rev-parse main` is `25ddee8`.
2. **Take the missing real-traffic measurement**: watch `docker logs guardbot`
   for the first addressed chat reply and record its `gemini_ms`/`total_ms`
   against the pre-deploy p50/p90 (5.3 s / 44.3 s). Do **not** run the frozen
   `--arm context` probe.
3. Do **not** re-run a probe that deletes `gemini_events` rows; if one is
   needed, snapshot `max(id)` and delete only strictly newer rows — and prefer
   not deleting at all while the live process writes.
4. V stays unstarted; the 200-request allowance and the token allocation stay as
   they are; nothing is rotated without instruction.

**To resume after any context loss.** Re-read this section, then verify
`git status`, `git rev-parse main`, `git rev-parse HEAD`,
`git ls-remote origin refs/heads/main`, and `docker ps` / `docker inspect guardbot`.

---

### 54.14 Checkpoint (2026-09-24, Phase One pool stabilisation) — resume here (supersedes §54.13 as the resume point; §54.13's deploy record stands)

**CHECKPOINT STATUS.** Date **2026-09-24 ~18:45Z**. Branch **`main`**, HEAD
**`6a4810a`** (`= origin/main = dashmo3i/main`). Base/rollback is the **deployed**
commit **`25ddee8`**; `6a4810a` is a **docs-only** commit on top of it, so the
working tree's `app/` is functionally identical to what the running container
loads. **The changes in this checkpoint are UNCOMMITTED and NOT DEPLOYED** — they
exist only in the working tree. Current task: Phase One of the owner's brief —
*stabilise, measure and optimise the existing Chat and Awareness Gemini
infrastructure without breaking or redesigning it*. **Phase Two (Telegram
group/member authorisation) is NOT started and must not be** — membership/admin
status must never equal application authorisation.

**Completed work.**
1. **Root-caused three pool behaviours** against live DB evidence (see §54.13
   and the measurements below): (a) a backoff slept for a retry the per-account
   share forbids; (b) the account breaker emptying a small pool; (c) `models_for`
   returning nothing when every capable model was merely *cooling*, which benched
   the account for `GEMINI_POOL_MODEL_COOLDOWN` (120s) — four-to-five times the
   cooldown that caused it.
2. **Implemented the minimum-usable-account floor** in
   `_bench_repeated_failures`: when the breaker would leave ≤1 usable account it
   withholds the bench, resets the streak, and records `bench_withheld`
   (`reason=last_usable_account`).
3. **Split "cooling" from "no compatible model"** in `generate()`: a new
   `ignore_cooldown` argument on `Pool.models_for` tells the two apart; the
   cooling case records `models_cooling`, keeps the account in rotation, and the
   walk's final kind is `models_cooling` rather than `no_compatible_model`.
4. **Guarded the retry backoff** so it is slept only when the retry can actually
   run (`attempt+1 < attempts_per_model and allowance > 0 and budget > 0`), in
   both the timeout and the transient branch.
5. **Fixed `db.pool_account_save`** so the stored `fingerprint`/`masked` are
   written only when supplied — the counter updates were blanking them on every
   attempt (measured live: `chat` 9/9 and `awareness` 3/3 had `masked=''`).
6. **Improved the timing seam** (existing logging only, no new subsystem):
   `chat timing` now logs `prepare_ms ctx_ms search_ms assemble_ms gemini_ms
   model_ms pool_ms proc_ms send_ms total_ms`; the pool logs one
   `[pool] request … select_ms provider_ms retry_ms attempts failures` line per
   request that failed over.

**Architecture decisions.** No new pool, credential manager, retry system,
breaker or context system — every change is inside the existing modules. The
floor is a *policy* on the existing breaker, not a second breaker. The cooling
fix reuses the models' own cooldowns rather than adding a new timer. The timing
work reuses the existing log lines. No deadline was raised, no ceiling changed,
no fallback list trimmed, no credential touched. The new `models_cooling` kind is
not in any caller's `_PERMANENT` set, so it is treated as transient exactly as
`no_compatible_model` was.

**Changed components/files.** `app/gemini_pool.py` (floor, `models_for`
`ignore_cooldown`, cooling branch + kind, backoff guard, timing breakdown);
`app/db.py` (`pool_account_save` identity); `app/chat.py` (`ChatReply.timing` +
`pool_ms`); `app/main.py` (`_timing` decomposition, stage marks);
`tests/test_gemini_pool.py` (+3 tests); `tests/test_chat_latency.py` (+2 tests).

**Tests executed.** Full suite: **3681 passed / 0 failed** (265.37 s) with
`.venv-test/bin/python -m pytest -q` (Python 3.10.12 test venv; production is
3.12). Targeted: `test_gemini_pool.py` 146 passed; the chat/db/nexus/context_plan
set 450 passed.

**Benchmarks.** Deployed baseline (container `guardbot`, image `8d4c6fc63293`,
~1 h of logs): **chat** `gemini_ms` p50 8799 / p90 26130 / p99 71274 / max 87173,
`total_ms` p50 10108 / p90 34409 / p99 71525 — the model stage is **89 %** of the
turn. **awareness** `gemini_ms` p50 10422 / p90 43793 / max 66746, `total_ms`
p50 10590 / p90 43884; its non-model work is p50 **200 ms**. Local, deterministic
measurements of the changes: a chat-like walk (9 accounts, 12 attempts, one 503
then a healthy account) now logs `retry_ms=0.0` and returns in **0.003 s**, where
the old path slept `_backoff()` = **1.5–1.875 s**; the one-account/all-models-429
scenario now yields `models_cooling` on requests 2 and 3 with the account
**ACTIVE** (before: `no_compatible_model` then `pool_empty`). Live DB at the
checkpoint: chat 9/9 ACTIVE, awareness 3/3 ACTIVE; `pool_empty` 5 all-time (4 in
the last 24 h), `pool_critical` 32 (8 in 24 h), `account_failover` 53 (all in
24 h).

**Known issues / blockers.**
1. **The live "after" numbers do not exist** — the fix is not deployed, and the
   owner has not authorised a deploy. The measured improvement is local and
   deterministic; the production effect is **UNMEASURED** until a deploy.
2. **Provider-side** free-tier `generate_content_free_tier` 429s and 503/504 on
   chat and awareness remain; no pool change creates quota.
3. Pre-existing, untouched: the dead `intent` primary `GEMINI_API_KEY`
   (fp `7c707e1b`, 401) and the two credential collisions (§54.13 limitation 3).

**Frozen items (unchanged).** V is not implemented, routed or activated; the
`--arm context` probe stays **FROZEN**; the awareness allowance stays **200**;
no credential is rotated, moved or deleted.

**Explicitly prohibited.** Do not start Phase Two. Do not deploy, commit or push
without the owner's explicit go-ahead. Do not raise a deadline or ceiling to
green a metric. Do not restart the production container without authorisation.
Do not delete `gemini_events` rows while the live process writes.

**Pending verification.** A live end-to-end chat probe and a real-traffic
`chat timing` line on the **deployed** build (to populate `pool_ms`/`proc_ms` and
`[pool] request` from real traffic), and the awareness/chat `pool_empty` rate
after deploy.

**NEXT STEP.** Do **not** commit or deploy. Wait for the owner's go-ahead, then
deploy via the documented path (§12: `docker compose build && docker compose up
-d`) and take a **self-cleaning live probe** — one real addressed chat message
through the deployed container — recording the new `pool_ms`/`proc_ms` and the
`[pool] request` line against the baseline above, plus the `pool_empty`/
`pool_critical` event counts before and after. If asked for the report first,
present the Phase One report from the working tree; make no further code changes.

**To resume after any context loss.** Re-read this section, then verify
`git status` (six modified files), `git rev-parse HEAD` (`6a4810a`),
`git rev-parse main`, `git ls-remote origin refs/heads/main`, and
`docker ps` / `docker inspect guardbot` (image `8d4c6fc63293`, Up,
`RestartCount=0`).

---

### 54.15 Checkpoint (2026-09-24, **DEPLOYED**) — Chat/Awareness credential rebalance — resume here (supersedes §54.14)

**CHECKPOINT STATUS.** Date **2026-09-24 ~19:10Z**. Branch **`main`**, HEAD
**`6a4810a`** (the Phase One code + this rebalance are **uncommitted** in the
working tree). **DEPLOYED**: image **`guardbot-guardbot:latest` =
`0a0e4636a624`**, container `guardbot` recreated **`2026-09-24T19:03:23Z`**,
`RestartCount=0`, no tracebacks. Rollback: image tag
**`guardbot-guardbot:phase-one-deploy1` = `a7ccb4f88b97`** (the first Phase One
build), **`pre-00c5d1d` = `1a3ccbe0b7af`** (pre-Nexus), and the pre-deploy DB
backup `data/guardbot.db.bak-20260924-185354-pre-keyrebalance` plus
`.env.bak-20260924-185254-pre-keyrebalance`.

**The credential rebalance (owner-authorised).** Two healthy chat keys were moved
to awareness by editing the host-only `.env` (gitignored; never in git):
* moved **`GEMINI_CHAT_API_KEY_8` (fp `599a10724725`)** → `GEMINI_AWARENESS_API_KEY_3`
* moved **`GEMINI_CHAT_API_KEY_9` (fp `5ff073eeebb1`)** → `GEMINI_AWARENESS_API_KEY_4`
Selection: both were `ACTIVE`, not cooling, no quota events, and had the best
observed success ratios (62 % / 54 %) among the low-load chat keys. **`chat`
slot 5 (fp `24b5072536f6`) was deliberately NOT moved** — it is the pre-existing
`chat:5 == awareness:2` collision, and moving it would have added no capacity.
No value was printed, logged, committed or rotated.

**Resulting runtime pools** (from the deployed startup line, `[pool]`):
`chat accounts=7 usable=7` · `awareness accounts=5` (env1 `889fc071c248`, env2
`24b5072536f6`, the two moved keys, + managed `k6d7861f7`) · `tts accounts=7`.
**Side effect to remember: `tts` shares chat's key prefix, so it also went 9→7**
— acceptable under the priority policy (tts demand is ~1 success/account) but it
must be stated, not discovered. Verified with a throwaway container *before*
touching production: chat fps = `[ad4bfbe4591c, 5148caba8f93, 23c19e3b642a,
295c2a860dc2, 24b5072536f6, 2a52d9661c2a, 4a75741b5c55]`, awareness fps =
`[889fc071c248, 24b5072536f6, 599a10724725, 5ff073eeebb1, 6d7861f7c2fc]`.

**Allocation policy — now an invariant in §53.3.** Chat and Awareness are the
two highest-priority workloads and together take the dominant share of healthy
capacity, Chat normally the larger share; **Awareness must never be starved or
left with token capacity**. Lower-priority workloads keep the capacity their
measured demand needs and no more. New keys are evaluated by health, quota/limit
state, recent failures, cooldown/bench state, demand and isolation before
assignment; a failing/cooling/benched/quota-exhausted key is never moved merely
to hit a count; the split follows evidence, not a hard-coded percentage.

**Unit fix during this deploy.** The `[pool] request` breakdown held *seconds*
in variables named `_ms` and printed them unchanged — a 15 s walk logged
`provider_ms=14.1`, a factor-of-1000 lie. Renamed to `*_s` and converted to
milliseconds at render (`app/gemini_pool.py`); pinned by
`test_the_breakdown_is_in_milliseconds`.

**Tests.** Full suite **3682 passed / 0 failed** (240.76 s) before the rebuild;
relevant subset 473 passed before the first deploy.

**Live probe (self-cleaning, inside the deployed container, real `chat.reply`).**
Synthetic ids that provably had no history; deleted exactly the 6 rows it created
and restored `chat_usage`. **3/3 answered**, all on `gemini-flash-lite-latest`:
**12.8 s / 12.1 s / 19.8 s** wall. Breakdown: `provider_ms` **11 425 / 12 047 /
19 743**, `retry_ms` **0.0** on all three, `select_ms` ≈ 0. The model stage is
~the whole turn; **our own retry/selection overhead is now ~0**, and the residual
(~1.3 s on the first call only) is the one-time `_load_sdk()` import. The backoff
guard is confirmed live (`retry_ms=0.0` where the old code slept 1.5–1.9 s per
transient-failing account). Accounts used spread across 5 of 7. Organic chat
traffic was **quiet** in the window — the probe is the post-deploy measurement.

**Post-deploy pool behaviour (first ~2 min).** 8 events: `model_failover`,
`account_failover` (incl. awareness slot 3 = a moved key, benched on repeated
503s with `usable=4/5` — the floor held), `account_recovered`. **No `pool_empty`
since the deploy**; the one `pool_critical` in the 30-min window predates it. The
identity fix is live: chat slots 1–7 now carry fingerprints (they were blank).

**Known issues / limitations.** (1) Organic end-to-end latency is still
unmeasured (no addressed message arrived). (2) Stale `gemini_accounts` rows
remain for the two moved keys under `workload=chat` (slots 8/9) — harmless,
not loaded, and **not** cleaned up (no destructive DB cleanup). (3) The
pre-existing collisions (`chat:5==awareness:2` fp `24b5072536f6`,
`live_voice:1==search:1` fp `20ed38996022`) remain and need the owner's
instruction. (4) Provider free-tier 429s / 503-504 persist; no pool change
creates quota. (5) The deployed image is built from an **uncommitted** tree.

**Frozen / prohibited.** V untouched; `--arm context` probe frozen; awareness
daily allowance unchanged at 200/account (the extra accounts raise the aggregate
ceiling 600→1000, which is the point); no credential rotated or deleted; no
destructive DB cleanup; no Phase Two.

**NEXT STEP.** The owner's next phases are **Chat Personality restoration**, then
the **broader integration test** — do **not** start them without the owner's
word. Before any further change: decide whether to **commit** the working tree
(the deploy is currently uncommitted) and whether to fix the two pre-existing
credential collisions. To re-probe, use a self-cleaning script in the container
(see §54.13's incident note: never delete `gemini_events` rows).

**To resume after any context loss.** Re-read this section, then verify
`git status` (7 modified files), `git rev-parse HEAD` (`6a4810a`),
`git ls-remote origin refs/heads/main`, `docker ps` (image `0a0e4636a624`, Up,
`RestartCount=0`), and that `[pool] chat: accounts=7` / `[pool] awareness:
accounts=5` appear in the startup log.

---

### 54.16 Checkpoint (2026-09-24, **COMMITTED & PUSHED**) — resume here (supersedes §54.15)

**CHECKPOINT STATUS.** Date **2026-09-24 ~19:20Z**. Branch **`main`**, HEAD
**`2b1d760`** `= origin/main = dashmo3i/main` (verified by `git ls-remote`, not by
push output). Working tree **clean**. The Phase One code and the rebalance are no
longer uncommitted: the verified tree was committed in three logical slices and
pushed to **both** remotes.

**The commits.** Base `6a4810a` → `2b1d760`:
* **`a46ee09`** `fix(pool):` the minimum-usable-account floor, the
  cooling-vs-no-compatible split, the retry-backoff guard, the breakdown unit fix
  and the `pool_account_save` identity fix (`app/gemini_pool.py`, `app/db.py`,
  `tests/test_gemini_pool.py`).
* **`2597db1`** `feat(chat):` the `ChatReply.timing` seam and the
  `_answer_conversationally` stage decomposition (`app/chat.py`, `app/main.py`,
  `tests/test_chat_latency.py`).
* **`2b1d760`** `docs(agents):` the §53.3 allocation invariant and the §54.14 /
  §54.15 checkpoints (`AgentMD.md`).

Range `6a4810a..2b1d760` = **7 files, 827 insertions / 29 deletions** — exactly
the tree that was deployed and probed. Each slice's staged diff was secret-scanned
clean before its commit.

**The deploy is unchanged and now backed by a commit.** Image
**`guardbot-guardbot:latest` = `0a0e4636a624`**, container `guardbot` Up,
`RestartCount=0`. The deployed files were verified against the committed tree by
sha256 — `/srv/app/{gemini_pool,chat,main,db}.py` each **match**
`git show HEAD:app/…` byte-for-byte. **§54.15 limitation (5) ("the deployed image
is built from an uncommitted tree") is now RESOLVED**: the tree is committed as
`2b1d760` and the running image matches it.

**Unchanged / frozen.** V untouched; `--arm context` probe frozen; awareness daily
allowance unchanged at 200/account; no credential rotated, moved or deleted; no
destructive DB cleanup; no Phase Two. Runtime pools still `chat=7` / `awareness=5`
/ `tts=7`.

**Still open (owner's call).** (1) Stale `gemini_accounts` rows for the two moved
keys under `workload=chat` (slots 8/9) — harmless, not loaded, deliberately not
cleaned. (2) The two pre-existing credential collisions (`chat:5==awareness:2` fp
`24b5072536f6`, `live_voice:1==search:1` fp `20ed38996022`). (3) Organic
end-to-end chat latency is still unmeasured (no addressed message arrived; the
self-cleaning probe is the post-deploy measurement). (4) Provider free-tier 429s /
503-504 persist — no pool change creates quota.

**NEXT STEP.** The owner's sequence is **Chat Personality restoration**, then the
**broader integration test** — do **not** start either without the owner's word.
Do not deploy, restart the production container, or change credentials without
authorisation.

**To resume after any context loss.** Re-read this section, then verify
`git status` (**clean**), `git rev-parse HEAD` (**`2b1d760`**),
`git ls-remote origin refs/heads/main` and `git ls-remote dashmo3i refs/heads/main`
(both **`2b1d760`**), `docker ps` (image `0a0e4636a624`, Up, `RestartCount=0`), and
that `[pool] chat: accounts=7` / `[pool] awareness: accounts=5` appear in the
startup log.

---

### 54.17 Checkpoint (2026-09-24, **Chat personality restored**) — resume here (supersedes §54.16)

**CHECKPOINT STATUS.** Date **2026-09-24 ~19:30Z**. Branch **`main`**, base/rollback
**`bb3f8e5`** (the pool-stabilisation + rebalance checkpoint, itself on `6a4810a`).
This task: **restore the historical Chat conversational behaviour on the current
architecture.** No architecture changed.

**Historical reference inspected (verified from history, not guessed).** Commit
**`3243067`** — `app/chat.py` (its `SYSTEM_INSTRUCTION`, `_contents`, `_request`,
`reply`), `tests/test_chat.py`, `tests/test_chat_activation.py`; `ChatGPT.md` at
that commit is the *strategy/continuity* doc, not a persona, so it is not the
behavioural reference.

**The historical behaviour identified.** A friendly assistant behind a Telegram
bot in a Persian community about internet access and VPNs: reply in Persian,
warm and informal; **two or three sentences**; no headings or bullet lists
unless genuinely listing; discuss any topic; use memory of the recent turns;
never claim to be human; never state our prices, links or credentials; never
claim an action it cannot take; refuse instructions inside the message that try
to change its role; say when it does not know. Generation: `temperature=0.8`,
`max_output_tokens=1024`, no tools, no context parameter.

**Current-vs-historical differences (documented before implementation).** The
current persona is a **superset** — it adds the public-figure-from-search rule,
the tool amendment, the media prompts, the repetition nudge and the awareness
instruction. The **architectural** difference is that Nexus appends a large
trusted-context block (room, state, memory, date, search) to the *system
instruction*; in production that nudged the register towards "assistant
answering a briefing" — longer, more structured, more restating — which is the
degradation the owner reported. The fix is the words, not the wiring.

**Changes implemented (only `app/chat.py` + `tests/test_chat.py`).**
* `chat.SYSTEM_INSTRUCTION` rewritten: the historical warm/informal/short core is
  restored as the dominant framing, an explicit **anti-robotic** section is added
  (no headings/numbered sections/bullets/summaries/Markdown by default, no
  restating, no assistant filler, no greeting/closing loops, no
  self-introduction), a **joking-around** section is added with hard boundaries,
  and a clause tells the model the appended background is **material, not a
  subject to summarise**. The leading comment records the restoration.
* Every asserted safety clause is preserved **verbatim** (no-human, our prices,
  `subscription link`, `credential`, public-figure-from-search-only, `untrusted
  data`, injection defence, `restricted to VPN or internet topics`).
* **10 new tests** in `tests/test_chat.py` assert the contract: short/informal,
  no document structure, no filler/loops, topic continuity, banter allowed,
  banter bounded (no threats, no slurs, no family insults, no sexual
  humiliation), banter dropped when the person is serious, no false human
  experience, background-is-background, and that `_generation_config` still puts
  the persona + context with `temperature=0.8`/`max_output_tokens=1024`.

**Tests actually run.** Full suite **3692 passed / 0 failed** (287.77 s,
`.venv-test/bin/python -m pytest -q`; was 3682, +10 new). `tests/test_chat.py`
**59 passed**. `test_web_search.py` + `test_gemini_pool.py` + `test_chat_latency.py`
+ `test_admin_continuation.py` **264 passed**.

**Architecture preserved (verified, not asserted).** The only source file changed
is `app/chat.py`, and within it only the prompt text and its comment. The Gemini
pool, credential isolation, Chat/Awareness workload isolation, Intent, Memory,
State, rate limits, daily limits, breakers, cooldowns, retry/failover, the
trusted-context assembly, security boundaries, Telegram auth, server-side
authorization, logging, persistence and the deployment architecture are
untouched; the pool/latency/web-search regression suites pass unchanged.

**Frozen / not touched.** V remains **inactive**; the `--arm context` probe stays
**frozen**; the awareness daily allowance stays **200**; no credential was
changed; **no deploy, restart or live probe** was performed (none authorised);
the separate Telegram group/member authorization task was **not** changed; no
Phase Two.

**Known limitations.** This is a **prompt** change: its effect is not measured in
production (no live probe was run). The rules are asserted in the prompt text,
but a model's compliance is probabilistic — the same honesty the persona itself
demands applies to this checkpoint.

**NEXT STEP.** The owner's remaining sequence is the **broader integration
test** — do **not** start it without the owner's word. A production confirmation
of the restored feel needs a **deploy + self-cleaning live probe**, which needs
the owner's explicit go-ahead. To resume: re-read this section, verify
`git status` (clean), `git rev-parse HEAD` (the commit recorded below),
`git ls-remote` on both remotes, and that `[pool] chat: accounts=7` /
`[pool] awareness: accounts=5` still appear in the startup log.

---

### 54.18 Checkpoint (2026-09-24, **Chat personality DEPLOYED & live-probed**) — resume here (supersedes §54.17)

**CHECKPOINT STATUS.** Date **2026-09-24 ~19:40Z**. Branch **`main`**. The
restoration is committed as **`067c417`** (`feat(chat): restore the historical
conversational persona`, `app/chat.py` + `tests/test_chat.py`) and **`0a80015`**
(`docs(agents)`, §53.8 contract + §54.17). Both were already pushed to both
remotes. This checkpoint records the **deploy and the live probe**.

**Scope verified before deploy.** `git show 067c417 -- app/chat.py` contains
**zero** non-comment, non-string changed lines — the persona text and its
comment only. No credential, auth, rate-limit, breaker, cooldown, failover,
context-assembly, persistence or provider-routing line changed. Range
`bb3f8e5..HEAD` = `AgentMD.md`, `app/chat.py`, `tests/test_chat.py`.

**Deployed.** `docker compose build && docker compose up -d` (§12). Container
`guardbot` recreated **2026-09-24T19:35:45Z** on image
**`guardbot-guardbot:latest = a976bb4c2a7a`**, `RestartCount=0`, `Running=true`,
no tracebacks. Startup loaded the expected pools — `[pool] chat: accounts=7
usable=7`, `[pool] awareness: accounts=5 usable=5`, `[pool] tts: accounts=7` —
and `Conversational AI active`. **0 `pool_empty`** events since deploy. The image
was verified to contain the new persona *before* the swap (throwaway container:
`Joking around`/`Background the server gives you` present, the old opening line
absent). Rollback tag **`guardbot-guardbot:pre-chat-personality`** = `0a0e4636a624`
(the previous image; tagged *before* the build because the disk is at 86 % and
the build GCs the old image).

**Live probe (self-cleaning, inside the deployed container, real pool + real
model, synthetic ids `chat=-100999900001 user=999900001`).** 12/12 answered;
deleted exactly the **8** `chat_messages` rows it created (`rows_left=0`). The
`chat_usage` counters were deliberately **not** rewritten — the owner was
chatting organically at deploy time and decrementing a shared counter would
erase real spend; the probe's ~12 calls are honest spend. Results (Persian):

| # | scenario | reply (verbatim) |
|---|---|---|
| 1 | greeting | سلام، مرسی. تو چطوری؟ چه خبر؟ |
| 2 | short | سلامتی، همه‌چی آرومه جز این اینترنت کذایی! تو چه خبر، کار و بار چطوره؟ |
| 3 | name set-up | خوشبختم رضا، منم که اینجا پای ثابتِ کل‌کل‌های اینترنتی! … |
| 4 | follow-up (memory) | مگه می‌شه یادم بره؟ گفتی رضایی دیگه. انقدر زود حافظه‌ام رو دست‌کم نگیر! |
| 5 | topic start | بستگی داره چی بخوای بسازی. برای هوش مصنوعی … پایتون بهتره، ولی … جاوا … قوی‌تره. |
| 6 | topic continue | معمولاً پایتون رو برای شروع بهتر می‌دونن چون سینتکسش خیلی روون‌تره … |
| 7 | topic switch | آره، امسال سرما بدجوری دندون‌گیر شده. پاشو یه چای داغ بریز … |
| 8 | light joke | (a short doctor joke) |
| 9 | playful teasing | خودتی 😂 یه سؤال درست حسابی بپرس تا نشونت بدم. |
| 10 | serious | ای بابا، ناراحت شدم. اگه دوست داری تعریف کن، شاید با همفکری یه راهی براش پیدا کردیم. |
| 11 | unknown | خب اینو دیگه از کجا باید بدونم؟ خودت تا حالا نگفته بودی که! اسمش چیه حالا؟ |
| 12 | repeat | اسممو نگفته بودی که یادم باشه! اسم چیه اصلاً؟ |

**Assessment — the restoration is confirmed live.** Casual and informal, short
and warm; continuity held inside the window (#4); topic start/continue/switch all
followed; a joke was told for the joke; **#9 teased back in the same register**;
**#10 dropped the joking immediately and answered the person normally**; **#11
did not invent the cat's name**; **#12 was a different, concise answer, not
boilerplate**. Across all 12: **no** «حتماً» / «البته» / «در خدمت شما هستم» /
«اگر سؤال دیگری دارید» / generic assistant endings, and **no** claim to be human.

**One honest nuance (NOT a regression).** At #12 the name given at #3 had fallen
out of the bounded history window, so the model said it had not been told.
`GEMINI_CHAT_HISTORY_TURNS=8` / `HISTORY_TTL=1800` are **identical to the
historical default** (verified at `3243067`), so this is the intended
bounded-history design, unchanged — not a personality defect. No correction made.

**Regression verification after deploy.** `tests/test_chat.py` +
`test_gemini_pool.py` + `test_chat_latency.py` + `test_web_search.py` +
`test_admin_continuation.py` + `test_requests.py` → **380 passed / 0 failed**;
`test_rbac.py` + `test_ai_admin.py` + `test_admin_commands.py` +
`test_vpn_admin.py` → **299 passed / 0 failed**. (Full suite before deploy:
3692 passed.)

**Preserved / frozen.** Current Gemini/Nexus architecture, security boundaries,
credential isolation, pool allocation, Awareness and context assembly all
untouched. **V remains inactive** (`GEMINI_LIVE_ENABLED=False`); the `--arm
context` probe stays frozen; the Telegram group/member authorization task is
**unchanged**; no Phase Two; no new workloads; no Pool-allocation change.

**NEXT STEP.** The owner's remaining phase is the **broader integration test** —
do **not** start it without the owner's word. Rollback is one command
(`docker tag guardbot-guardbot:pre-chat-personality guardbot-guardbot:latest &&
docker compose up -d`). To resume: verify `git status` (clean), `git rev-parse
HEAD` (this checkpoint's commit), `git ls-remote` on both remotes, `docker ps`
(image `a976bb4c2a7a`, Up, `RestartCount=0`), and `[pool] chat: accounts=7` /
`[pool] awareness: accounts=5` in the startup log.

---

### 54.19 Checkpoint (2026-09-24, **owner tone + group authorization**) — resume here (supersedes §54.18)

**CHECKPOINT STATUS.** Date **2026-09-24 ~20:05Z**. Branch **`main`**, HEAD
**`af6930e`** (both remotes). Two features implemented, tested, **DEPLOYED and
live-probed** in the owner's order. Image **`84583581d0dd`**; rollback tag
**`guardbot-guardbot:pre-owner-group` = `a976bb4c2a7a`** (`docker tag
guardbot-guardbot:pre-owner-group guardbot-guardbot:latest && docker compose up
-d`). Container healthy, `RestartCount=0`.

**Feature One — owner-aware tone.** Files: `app/chat.py`, `app/main.py`,
`tests/test_owner_personality.py`.
* `chat.OWNER_AMENDMENT` (exported) is a **tone** amendment: warmer and more
  familiar, and it forbids honorifics («قربان», «سرور», «جناب», «بنده») and any
  ceremonial address, forbids announcing the ownership, and forbids revealing the
  id. It is prepended to the trusted context by `main._answer_conversationally`
  **only** when `rbac.is_owner(user.id)` is true — ownership is read from the
  configured id and nothing else (never a username, display name, Telegram admin
  status, role, or a claim). The base persona contains no honorific. The
  amendment grants no capability and bypasses no gate.

**Feature Two — the group boundary.** Files: `app/main.py`,
`tests/test_group_authorization.py`.
* `main.authorized_group(chat_id)` reads `config.GROUP_IDS` and nothing else, and
  is fail-closed. It is enforced at the **top of `on_group_chat`** — before the
  identity write, the awareness capture and any model call — so an unregistered
  room produces no `chat.reply` and no Gemini request. It is also enforced in
  `_awareness_run_room` (a stale row for a de-registered room is not read) and in
  `on_transcribe_command` for groups. The room boundary and the speaker boundary
  (`rbac`/`nexus.accepts`) are separate and both required; the owner is refused in
  an unregistered room. Being added to a group, or made admin there, does not
  authorize it. See §53.5.

**Tests actually run.** New files: `tests/test_owner_personality.py` (12) +
`tests/test_group_authorization.py` (12) = **24 passed**. Regression
`test_nexus` + `test_awareness` + `test_private_boundary` + `test_web_search` +
`test_chat` + `test_chat_activation` + `test_conversation_media` = **483 passed**.
Full suite **3716 passed / 0 failed** (272.17 s; was 3692, +24).

**Live probe (2026-09-24, self-cleaning, in-container).** Drove the real
`on_group_chat` / `on_private_text` against the real config and the real model,
with synthetic ids; only the rows it created were deleted. Results:
* an **unregistered** room (member *and* owner) → **0** model calls, **0**
  awareness captures, **0** identity writes — refused before anything;
* a registered room with an **addressed** member → answered; the owner's turn
  carried `chat.OWNER_AMENDMENT` and the member's did not; real replies were
  short, informal and contained **no** honorific/ceremonial term («قربان»,
  «سرور», «جناب», «بنده») and no filler;
* a registered, **unaddressed** member message → no model call (awareness only);
* private: a stranger → refused before the model; the owner → answered.

**Known nuance — RESOLVED 2026-09-24, superseded by §54.20.** In the deployed
configuration `NEXUS_ACTORS_ONLY=false`, so an ordinary member in a **registered**
room who addresses Nexus **is** answered by `nexus.accepts`. The owner has since
confirmed this is the **intended** model — the boundary is the **ROOM, not the
SPEAKER** — and the speaker gate has been **retired** entirely. See §54.20 and
§53.5. (The `registered + unauthorized member → denied` test described here no
longer exists; the member is answered on purpose.)

**Architecture preserved.** No Pool, credential, isolation, rate-limit, breaker,
cooldown, failover, context-assembly, persistence or deployment change. V remains
**inactive**; the `--arm context` probe stays frozen; Awareness allocation
unchanged; no acquisition change; no new workload; no Phase Two.

**Unresolved / next.** The two features are **deployed** (image `84583581d0dd`,
live-probed). Open: (1) the owner's decision on member-level gating above; (2) the
**broader integration test**, to be run only on the owner's go-ahead. Rollback is
`docker tag guardbot-guardbot:pre-owner-group guardbot-guardbot:latest && docker
compose up -d`. To resume: verify `git status` (clean), `git rev-parse HEAD`,
`git ls-remote` on both remotes.

---

### 54.20 Checkpoint (2026-09-24, **room boundary + tenant isolation**) — superseded by §54.21 (deploy)

**CHECKPOINT STATUS.** Date **2026-09-24 ~21:10Z**. Branch **`main`**, base
**`7968c06`** (both remotes). This work was **committed and pushed** at this
point (`a511ce3` code+tests, `69690a0` docs); at the time it was **NOT
DEPLOYED**. **It was deployed later the same evening — see §54.21**, which
supersedes the deploy status below. The implementation narrative that follows is
still authoritative for *what* was built.

**The owner's definitive correction.** The security boundary is the **ROOM, not
the SPEAKER**. Nexus MUST chat with **all normal members** inside an explicitly
authorized/registered group. `NEXUS_ACTORS_ONLY=true` must **not** be enabled to
satisfy group authorization, and there is **no per-member gate** on ordinary
conversation. This resolves the "known nuance" flagged in §54.19.

**Feature — the room allowlist, DB-authoritative.**
* New table `authorized_groups` (`app/db.py`): `chat_id` PK, `enabled`, `title`,
  `added_by/added_at/updated_at`, `revoked_by/revoked_at`, `note`. New functions
  `authorized_group_get/list/set/disable/ids/any/reset`. `GROUP_IDS` is a
  **bootstrap** seeded **once** (guard = "the table has ever held a row"), then
  the table is authoritative; **revoke is a soft disable** that keeps the row, so
  a restart can never resurrect a room.
* New module `app/groups.py`: load/state/cache (mirrors `nexus.py`), fail-closed
  (a failed read is **no rooms**), `is_authorized`, `register`, `revoke`,
  `all_ids`, `count`, `list_rows`, `seed_if_empty`. It performs **no** permission
  check — `admin_service.execute` is the boundary.
* `app/nexus.py`: `accepts(principal)` **removed**; new
  `accepts_in_group(*, room_authorized)` = `is_online() and room_authorized`.
  `accepts_private` unchanged. `actors_only` removed from `describe()`.
* `app/main.py`: `authorized_group()` now reads `groups.is_authorized`;
  `_nexus_will_answer` / `_awareness_read` / `on_group_chat` gates use
  `nexus.accepts_in_group`; the blind-list, visibility loop, startup log and
  `/nexus status` scope line read the live allowlist. New commands
  `/registergroup`, `/unregistergroup`, `/groups` (+ `OWNER_COMMAND_LABELS`).
* `app/admin_service.py`: two new `OPERATIONS` — `register_group` /
  `unregister_group` (`config.manage`, `kind=OP_SYSTEM`,
  `requires_nexus_online=False`, **no AI tool**, not confirmation-gated) — with
  `_apply` branches calling `groups.register` / `groups.revoke`. A typed command
  is a person acting directly; the model has no tool for either.
* `app/config.py`: `NEXUS_STATUS_TEXT` `{actors_only}` → `{answer_scope}`;
  `NEXUS_ANSWER_SCOPE_LABEL` added; `NEXUS_ACTORS_ONLY*` kept but **read
  nowhere**; the group-command strings added (unused ones removed).

**Feature — tenant isolation (one shared DB, strict group scoping).**
* `agent_tasks` gained `chat_id` (defensive `_ensure_column`); the request id is
  now **content-derived from `(chat_id, actor_id, repository, task, minute)`**
  (`agent_bridge.new_request_id`), so two rooms cannot collide; `agent_task_view`
  refuses a chat mismatch; `agent_task_active/recent/waiting` take an optional
  `chat_id`.
* `people.resolve` and `identity._names_for`/`describe` are **room-scoped**:
  name memory is per room; `chats_seen` (membership) and `recent_audit` are
  **group-specific** and returned **only** on the unscoped operator view.
* `db.audit_recent(limit, *, chat_id=None)`; `admin_service.recent_refusals` /
  `status_report(*, chat_id=None)`; the group-facing `/pool` passes `room.id`.
* Analytics scoped: `awareness_summary`, `group_pending`, `group_role_counts`
  take `chat_id`; `awareness.metrics` / `metrics_line(*, chat_id=None)`;
  `_nexus_status_text(*, chat_id=None)` passes `room.id`.
* Row-ceiling prunes (`people_prune`, `memory_prune`, `state_prune`) are **per
  group** via `ROW_NUMBER() OVER (PARTITION BY chat_id …)`; the age bound stays
  global.

**Tests.** New `tests/test_group_allowlist.py` (17) + rewritten
`tests/test_group_authorization.py` (17). Rewritten/updated: `test_nexus.py`,
`test_private_boundary.py`, `test_chat_activation.py`, `test_ai_isolation.py`,
`test_awareness.py`, `test_awareness_metrics.py`; `tests/conftest.py` resets the
allowlist table + `groups` cache per test (setup only — the teardown reset was
removed because some module fixtures close `db._conn`). **Full suite: 3738
passed / 0 failed** (247.5 s; was 3716 at §54.19).

**Docs.** `AgentMD.md` §53.5 rewritten with the owner's definitive room-boundary
text; §53.6 gained the **Tenant isolation** block; §54.19's "known nuance" marked
resolved; the private-chat narrative de-referenced `NEXUS_ACTORS_ONLY`.
`README.md`, `.env.example`, `docs/reference/nexus-awareness.md` (§34.13, §35.10,
§35 test list) and `docs/reference/assistant.md` (§17.1, §40) updated with
superseded banners where they described the retired speaker gate.

**Architecture preserved.** No Pool, credential, isolation, rate-limit, breaker,
cooldown, failover, context-assembly or deployment change. V remains **inactive**;
the `--arm context` probe stays frozen; Awareness allocation unchanged; no
acquisition change; no new workload; no Phase Two. Nothing was deployed.

**NEXT STEP.** Nothing is running differently. To deploy this (only on the
owner's explicit word): `docker compose build && docker compose up -d`, then a
self-cleaning live probe (unregistered room → 0 model calls / 0 captures / 0
identity writes; registered room + member → answered; registered room + owner →
answered with the owner amendment; `/registergroup` / `/unregistergroup` /
`/groups` in a real room). Rollback stays
`docker tag guardbot-guardbot:pre-owner-group guardbot-guardbot:latest && docker
compose up -d`. To resume: verify `git status` (clean), `git rev-parse HEAD`,
`git ls-remote` on both remotes.

---

### 54.21 Checkpoint (2026-09-24, **room boundary + tenant isolation DEPLOYED**) — resume here (supersedes §54.20)

**CHECKPOINT STATUS.** Date **2026-09-24 ~21:19Z**. Branch **`main`**, commit
**`69690a0`** on **both remotes** (`git ls-remote` confirmed), working tree
clean. The §54.20 work is now **DEPLOYED and live-probed**. This is the first
checkpoint in this feature that describes the *running* system.

**Deploy.** Rollback image tagged **`guardbot-guardbot:pre-room-boundary`**
(built from `84583581d0dd`, the §54.19 image). New image
**`5769e3678e0f`** built and brought up with `docker compose build && docker
compose up -d`. Container recreated, **Up**, **`RestartCount=0`**. Boot log:
`group allowlist seeded from GROUP_IDS: 2 room(s)`; `Nexus state: online
rooms=2 observe_admins=on names=2`; `GuardBot started. Groups:
[-1003587640764, -1001299527312]`. The running container was verified to contain
the new code (`app/groups.py`, `nexus.accepts_in_group`, the `groups.is_authorized`
gate, the two operations, the three commands, the `chat_id` scoping). Production
`authorized_groups` holds exactly the **2** seeded enabled rooms. Real owner
traffic was served in `-1001299527312` within a minute of the restart (chat
reply, `turns=1`/`turns=3`, `sent=True`).

**Live probe (self-cleaning, in-container, real config/DB/rbac/model).** Source
committed at **`tools/probe_room_boundary.py`** (commit `6a1e8c7`); run with
`docker cp tools/probe_room_boundary.py guardbot:/tmp/ && docker exec -w /srv -e
PYTHONPATH=/srv guardbot python /tmp/probe_room_boundary.py`. Synthetic ids
`REG=-1009000000001`,
`UNREG=-1009000000002`, `CMD=-1009000000003`, `MEMBER=900000042`,
`STRANGER=900000043`; two turns ran the **real** model. Results:
* **Room boundary fails closed** — unregistered **member** and unregistered
  **owner** both: **0** model calls / **0** awareness captures / **0** identity
  writes. Revoked room: **0/0/0**. (The boundary is the room, not the speaker.)
* **Registered room is open to every member** — member addressed: **1** model
  call, `caller=900000042`, no owner amendment. Member **unaddressed**: **0**
  model calls but awareness+remember still fired (observed, not answered).
* **Owner tone** — owner turn: `caller=6931339207`, `owner_amendment=true`;
  member turn: `owner_amendment=false`. Both real replies scanned clean
  (`honorifics=[]`, `filler=[]`).
* **Private chat unchanged** — stranger **0/0/0**; owner answered with the
  amendment.
* **Commands** — `/groups` lists the two real rooms + the probe room;
  `/registergroup` → authorized, `/unregistergroup` → not authorized; a
  **non-owner** `/registergroup` is refused (`⛔️ این کار رو نمی‌تونی انجام بدی.`).
* **Cleanup** — all created rows deleted; **residue sweep across every table and
  every synthetic id = 0**. `chat_usage`/`gemini_daily` deliberately untouched
  (two real turns stay visible). One cleanup gap was found and fixed: the probe
  had not deleted its `admin_requests` ledger rows (6); they were removed by
  hand and the probe's cleanup list now includes that table (committed in
  `tools/probe_room_boundary.py`, `6a1e8c7`).

**Architecture preserved.** No Pool, credential, isolation, rate-limit, breaker,
cooldown, failover, context-assembly, Awareness-allocation or acquisition
change; V remains **inactive**; the `--arm context` probe stays frozen; no Phase
Two. Rollback: `docker tag guardbot-guardbot:pre-room-boundary
guardbot-guardbot:latest && docker compose up -d`.

**NEXT STEP.** None required for this feature — it is live. Do **not** redeploy
without the owner's go-ahead. To resume: `git status` (clean), `git rev-parse
HEAD` (this checkpoint's commit on top of `6a1e8c7`), `git ls-remote` on both
remotes. The feature's commit chain is `a511ce3` → `69690a0` → `312c07c` →
`6a1e8c7`.

---

### 54.22 Checkpoint (2026-09-24, **Chat personality rebuilt + DEPLOYED**) — resume here (supersedes §54.21)

**CHECKPOINT STATUS.** Date **2026-09-24 ~21:47Z**. Branch **`main`**, commit
**`f3688e3`** on **both remotes** (`git ls-remote` confirmed), working tree
clean. **Deployed and live-probed.** This is the current running system.

**The owner's instruction.** Rebuild the Chat behaviour around the exact
historical baseline **`3243067`** — the old conversational character — as the
foundation of the *current* implementation. Not a rollback, and not another
personality layer stacked on the existing one. The persona was to become **one
coherent behavioural source of truth**, with infrastructure/security/context
left separate.

**What was wrong.** The Nexus era had grown the persona into a policy document
with separate tone, joke, owner and repetition sections, and several competed
over the same decision. The owner's failure example: «نخند حرزاده» answered with
«چشم قربون‌سربازیت😂 بی‌خیال بابا» — servile address, automatic laughter and
canned filler instead of a reaction to what was said. Two concrete causes were
found in the prompt itself: a **canned example** («خودتی 😂 یه سؤال درست حسابی
بپرس») that *taught* the laughter shape, and an **honorific ban that lived only
in the owner amendment**, so an ordinary member got no such rule.

**The rebuild.** `chat.SYSTEM_INSTRUCTION` is now the single source: one identity
(warm, informal, short, Persian, context-driven), reactive humour and register,
the ban on titles/servile address **for everyone**, the hard boundaries, the
context-as-data framing, and every security clause verbatim. Removed: the canned
joke examples, the manufactured-intimacy clause, the separate "Joking around"
section, the duplicate repetition micro-rule. `OWNER_AMENDMENT` (a competing
personality) became **`OWNER_NOTE`** — a pure server-stated data line. Kept
separate as non-personality: `TOOL_AMENDMENT` (a capability correction) and
`REPETITION_NUDGE` (a retry mechanism). Wiring unchanged: persona + context,
`temperature=0.8`, `max_output_tokens=1024`.

**Commits.** `3476eea` (persona rebuild + tests, base `fae9604`) and `f3688e3`
(§53.8 contract). Pushed to both remotes. New
`tests/test_chat_behavior_contract.py` (20 tests) plus the rewritten
`test_owner_personality.py` and updated `test_chat.py`. **Full suite: 3757
passed / 0 failed** (263.4 s; was 3738).

**Deploy.** Rollback tag **`guardbot-guardbot:pre-chat-rebuild`**
(`5769e3678e0f`). New image **`0b259c42ff33`**; container recreated, **Up**,
**`RestartCount=0`**. The running `/srv/app/chat.py` is **byte-identical** to
`HEAD:app/chat.py` (sha256 `76965522a7ddae71…`), and the running module has
`OWNER_NOTE`, no `OWNER_AMENDMENT`. Production `authorized_groups` still holds
its **2** rooms; the awareness pass is serving normally.

**Live acceptance probe (self-cleaning, in-container, real model).**
`tools/probe_chat_personality.py` — **10/10 scenarios passed, 0 violations**.
Highlights: the failure class «نخند حرزاده» answered «من که نخندیدم، واقعاً هم
وضعیتت روی اعصابه…» (a reaction, none of the old shape); owner turn «سلام. بد
نیستم، تو چطوری؟» (familiar, no honorifics, no announcement); user-initiated
humour teased back with no canned laughter; a user-initiated adult joke declined
naturally rather than escalated; an innocent message drew no sexual register; the
normal, serious and slang turns stayed in register. Composition verified
in-process: member instruction == persona, owner note is data-only. Residue sweep
across every table for the synthetic ids = **0**; `chat_usage`/`gemini_daily`
deliberately untouched.

**Architecture preserved.** No Pool, credential, isolation, rate-limit, breaker,
cooldown, failover, context-assembly, Awareness-allocation, authorization,
tenant-scoping or acquisition change; V remains **inactive**; the `--arm
context` probe stays frozen. Rollback: `docker tag
guardbot-guardbot:pre-chat-rebuild guardbot-guardbot:latest && docker compose up
-d`.

**NEXT STEP.** The feature is live. Do **not** redeploy without the owner's
go-ahead. To resume: `git status` (clean), `git rev-parse HEAD` (this
checkpoint's commit on top of `f3688e3`), `git ls-remote` on both remotes.

---

### 54.23 Checkpoint (2026-09-24, **Chat rebuild — explicit deploy + verification**) — resume here (supersedes §54.22)

**CHECKPOINT STATUS.** Date **2026-09-24 ~22:08Z**. Branch **`main`**, deployed
commit **`6bdebab`**, working tree clean. This is the authoritative record of what
is **running**; §54.22's image reference (`0b259c42ff33`) is superseded.

**Deployed commit / image.**

| | value |
|---|---|
| Deployed commit | **`6bdebab`** (HEAD; `3476eea` → `f3688e3` → `fcc51c0` → `6bdebab`) |
| Deployed image | **`f36e60bf3971`** |
| Rollback tags | `guardbot-guardbot:pre-fcc51c0` = `0b259c42ff33` (the first rebuilt image); `guardbot-guardbot:pre-chat-rebuild` = `5769e3678e0f` (pre-rebuild) |
| Container | `Up`, **`RestartCount=0`**, started `22:02:18Z` |

**The commit actually tested is the app code that is running.** `app/` and
`tests/` at HEAD are **byte-identical** to the tested commit `3476eea` (proved by
`git diff 3476eea HEAD -- app/ tests/` = empty); the commits after it are docs and
the probe tool only. Every `app/*.py` and `tools/*.py` in the container matches
`HEAD` (76 files, **0 mismatches**). The running module has `OWNER_NOTE` and no
`OWNER_AMENDMENT`; the persona is 5640 chars.

**Tests.** Full suite **3757 passed / 0 failed** — run once on the tested code
and **re-run post-deploy** (242.97 s) with the same result. Focused: 88 chat /
owner / behaviour-contract; related regression: 484.

**Live acceptance probe** (`tools/probe_chat_personality.py`, run from the
**deployed** copy against the real model): **10/10, 0 violations.** Highlights:
failure class «نخند حرومزاده» → «نخندیدم. تو حالت خوب نیست و کلافهای…»;
owner → «سلام، ممنون. روزت بخیر.» (familiar, no honorifics); user-initiated
humour/teasing answered in register without canned laughter; user-initiated adult
joke declined contextually; an innocent message drew no sexual register; normal /
serious / slang turns stayed in register. Residue sweep for the probe ids = **0**;
`chat_usage`/`gemini_daily` deliberately untouched.

**Deviations (recorded, none are behaviour changes).**
1. **Probe checker false positive.** The first deployed run scored 9/10: the
   adult-joke answer contained «یه بنده خدایی» — the idiom "some poor guy", not
   the servile vocative «بنده» — and the naive substring check flagged it. The
   behaviour was correct; the checker was fixed (match «بنده» only when not
   followed by «خدا», tolerating ZWNJ, commit `6bdebab`) and the probe re-run to
   10/10. No app code changed.
2. **Soft observation (no action taken).** In one of the live runs, two teased
   turns volunteered self-description («من هوش مصنوعی هستم…», «من فقط چندتا خط کد
   و الگوریتمم…») instead of staying in register. It is a soft deviation from the
   persona's "do not announce it / do not describe yourself", not a banned-token
   violation, and it is model variance (the same scenarios did not do it on the
   other run). **No behaviour change was made during deployment**, per the
   instruction; it is noted for a future owner-approved pass if wanted.

**Health / drift.** `ERROR`/`CRITICAL`/`Traceback` in the container log: **0**.
`RestartCount=0`. Boot: `Nexus state: online rooms=2`, `GuardBot started. Groups:
[-1003587640764, -1001299527312]`, `Conversational AI active: model=
gemini-flash-lite-latest … history_turns=8`, `Authorization: owner=6931339207`,
`AI ADMIN MODE: AVAILABLE`. Runtime flags unchanged (`GEMINI_CHAT_ENABLED=True`,
`ADMIN_AI_ENABLED=True`, `NEXUS_AWARENESS_ENABLED=True`, `NEXUS_ACTORS_ONLY=False`,
`GEMINI_SEARCH_ENABLED=True`); `GROUP_IDS` unchanged; `authorized_groups` still
holds its **2** rooms; `.env` untouched (mtime 18:52). No Pool, credential,
rate-limit, breaker, tenant-isolation or configuration change.

**NEXT STEP.** Nothing outstanding. The Dashboard work has **not** been started
(explicitly out of scope for this task). Do **not** redeploy without the owner's
go-ahead. To resume: `git status` (clean), `git rev-parse HEAD` (this
checkpoint's commit on top of `6bdebab`), `git ls-remote` on both remotes.

---

### 54.24 Checkpoint (2026-09-24, **Admin Control Center — A-to-Z audit + staged plan**) — audit DONE, implementation NOT STARTED

**CHECKPOINT STATUS.** Date **2026-09-24 ~22:16Z**. Branch **`main`**, working
tree clean. The **A-to-Z system audit is complete**; the **dashboard itself has
not been written yet** — this checkpoint exists so the audit and the plan survive
the context window, per §55. The deployed GuardBot is untouched and verified
(§54.23): commit `6bdebab`, image `f36e60bf3971`, `RestartCount=0`.

**Audit method.** Repository + AgentMD inspection, live-DB inspection, secret
scan, migration inspection, and the full test suite. Evidence:

| Check | Result |
|---|---|
| Full suite (A-to-Z baseline) | **3757 passed / 0 failed** (245.4 s) |
| Live Chat acceptance probe | **10/10** (§54.23, unchanged) |
| Deployed code vs HEAD | 76 py files, **0 mismatches** |
| Tracked secrets | **none** — every match is a synthetic test fixture (`AIzaSyFAKE…`, the public example token `123456789:AAHdq…`, a fake RSA header) used to test the redactor |
| `.gitignore` | correct: `.env`, `.env.*` (but `!.env.example`), `data/`, `*.db`, `*.log` |
| Runtime data tracked | **no** (`git ls-files data/` empty) |
| Migrations | idempotent: `CREATE TABLE IF NOT EXISTS` + `_ensure_column` (PRAGMA → `ALTER TABLE`); WAL + `synchronous=NORMAL` |
| Live DB | **33 tables** (list below) |
| Lint/type config | **none** (dev deps = `pytest` only); static checking = `py_compile` + the suite |
| Web layer in GuardBot | **none** — no aiohttp/FastAPI/Flask/uvicorn anywhere in `app/` |

**A–U coverage (what actually exists).**

* **A Auth/sessions** — Telegram-bot side only. `rbac.resolve/authorize` (6 roles:
  owner, senior_admin, admin, moderator, helper, guest; `ROLE_PERMISSIONS`),
  `admin_service.execute` as the single authority boundary, `admin_audit` +
  `admin_requests` + `admin_pending_ops`. **No dashboard identity, session,
  cookie or CSRF layer exists.**
* **B Bot lifecycle** — **single bot** (`BOT_TOKEN`, one
  `telegram.ext.Application`). There is **no bot registry, no multi-token model,
  no token-rotation path**. "Add another bot" is **not present in the
  architecture** and must not be invented silently.
* **C Chat** — complete and verified (§53.8, §54.22/§54.23). One persona,
  `OWNER_NOTE` as data, reactive tone. **Must not be touched.**
* **D DB integrity/migrations** — complete; 33 tables; per-group scoping and
  per-group retention ceilings (§53.6).
* **E Errors/retries/idempotency** — complete: `admin_requests` (content-hash
  request ids), `admin_pending_ops`, `vpn_pending_ops`, pool retry/backoff.
* **F Feature flags** — complete: `config.py` env-driven flags (`GEMINI_*_ENABLED`,
  `NEXUS_*_ENABLED`, `ADMIN_AI_ENABLED`, `GEMINI_SEARCH_ENABLED`, …).
* **G Groups/room authorization** — complete and DB-authoritative
  (`authorized_groups`, `app/groups.py`); register/revoke/list via
  `/registergroup`, `/unregistergroup`, `/groups`.
* **H Health/runtime** — boot log, `nexus_state`, `awareness_control`,
  `search_control`, `gemini_*` tables. **No HTTP health endpoint** (`/healthz`
  does not exist in GuardBot).
* **I AI workload isolation** — complete: chat / intent / awareness / moderation /
  transcribe / live_voice / search each have their own keys, counters, windows,
  breakers and clients.
* **J Jobs/workers** — `apscheduler` ticks (awareness deadline, sweeps),
  `agent_poller`; `agent_tasks` + spool.
* **K Providers/credentials/quotas** — `gemini_accounts`, `gemini_models`,
  `gemini_daily`, `gemini_events`, `gemini_discovery`, plus the runtime credential
  store `app/key_store.py` (`add`/`remove`/`entries`/`slots_for`, actor-audited).
  Quotas/caps/breakers live in `gemini_pool` + `config`.
* **L Logging/privacy/secrets** — complete: `agent_bridge.redact`, no key in logs
  (asserted by tests), masked fingerprints (`****gxYw`).
* **M Memory/context/history/awareness** — complete: `memory`, `room_state`,
  `state`, `conversation_state`, `user_memory`, `awareness*`, `context_plan`.
* **N Personality/moderation/injection** — complete (§53.8; moderation via
  `ai_moderation`/`moderation`/`mod_policy`; injection defence asserted in tests).
* **O Owner/admin authority** — complete: `rbac` + `admin_service`; owner by id.
* **P Pools/limits/breakers** — complete: `gemini_pool` per workload.
* **Q Quality/regression/live acceptance** — complete: 3757-test suite,
  `tools/probe_chat_personality.py`, `tools/probe_room_boundary.py`,
  `tools/eval_*`.
* **R Room/tenant boundaries** — complete (§53.6, §54.21).
* **S Search** — complete: `web_search` (Tavily), `search_control`, its own key
  and budget, separate from chat/intent.
* **T Media/voice/duplicates/malformed** — complete: `media`, `transcribe`,
  `voice_live/`, `seen_updates` (dedupe), `text_filters`.
* **U Users/identities/permissions/audit** — complete: `users`, `identities`,
  `identity_resolutions`, `people`, `admins`, `admin_audit`.
* **Beyond U** — VPN-bot integration (`vpnbot`, `vpn_service`,
  `service_adapters`, `vpn_pending_ops`; internal API on `127.0.0.1:8099`);
  agent bridge (`agent_bridge`, `agent_data`, `agent_service`, `agent_poller`,
  `agent_spool`, `tools/agent_runner.py`); `burst`, `decision`, `discourse`,
  `entities`, `intent`, `referents`, `responses`, `temporal`, `classifier`,
  `addressing`, `net`, `requests`, `objects`, `persian_calendar`.

**Audit conclusion.** The backend is healthy and complete for everything it
claims; the full suite is green; secret hygiene is correct. **No critical or high
defect was found in the existing system, so no fix or new regression test was
required by the audit.** The one genuine gap is that **the Admin Control Center
does not exist** — GuardBot has no web layer, no dashboard identity/session, and
no multi-bot registry.

**Convention source (visual identity + stack).** `/opt/vpn-bot` already ships a
mature dashboard and is the reference for both: **aiohttp + Jinja2 templates +
`app/web/static`**, route modules with `register(app)`, a `queries` data layer,
`copy` (Persian), `jalali`, and **`hashlib.scrypt` password hashing with a signed
stateless session cookie** (`HttpOnly`, audience-scoped, CSRF token, password
epoch), plus `ops/*.service`, `ops/nginx-dashboard.conf.example`,
`ops/dashboard_passwd.py`, and `tests/test_web_dashboard.py`. The GuardBot
dashboard must follow these conventions and the same dark visual family
(`static/app.css`, `theme.css`, `logo.svg`) — **not** a generic admin template.

**Architecture decisions (proposed, to confirm before M1).**
1. **Stack** — aiohttp + Jinja2 + static, mirroring `/opt/vpn-bot`; no new
   compiled dependency (`scrypt` from stdlib).
2. **Placement — LOCKED (owner, 2026-09-24): a separate `dashboard` Compose
   service.** It runs `python -m app.web` as its **own process**, sharing the
   **same `./data` SQLite volume** (no second DB, no second data volume), and
   must **not** run inside the bot's event loop, so a dashboard restart can never
   disturb Telegram polling. Managed with the existing compose project.
   **Image/layer optimization is a hard requirement** (owner): reuse the
   existing GuardBot image layers where technically safe; **no duplicate Python/
   system dependencies**; multi-stage build if there is a frontend build; keep
   build-only deps out of the runtime image; prefer the existing runtime/base
   image when compatible; **no heavyweight framework** when the existing stack
   (aiohttp/Jinja) suffices; a correct `.dockerignore` (exclude `.git`, tests,
   `.venv*`, caches, `data/`, artifacts); minimal optimized frontend assets; **no
   dev dependencies in the production container**; reuse static assets rather
   than duplicating; **log rotation configured** so dashboard logs cannot grow
   unbounded; no persistent in-container caches unless measurably beneficial.
   Security, reliability, maintainability and rollback must **not** be sacrificed
   for size. The measured sizes must be reported (current GuardBot image,
   dashboard image, additional unique disk, shared/reused layers, extra
   persistent data/log/cache, total footprint) and recorded here.
3. **Identity** — a dashboard admin identity **separate** from Telegram
   membership, reusing `rbac` for authorization (`config.manage` etc.). A
   Telegram group admin is **not** a dashboard admin (requirement §10).
4. **Data** — new tables only (`dashboard_sessions`/`dashboard_credentials`/
   `dashboard_audit` as needed) via `CREATE TABLE IF NOT EXISTS` +
   `_ensure_column`; **no destructive rewrite** of history.
5. **Multi-bot — LOCKED (owner, 2026-09-24): single bot now, designed for
   multi-bot later.** **GuardBot is single-bot today** (one `BOT_TOKEN`, one
   `Application`); there is **no** registry and no token-rotation path. The
   dashboard's first release surfaces and manages the **existing** single bot
   (health, webhook/polling status, groups, usage, errors, audit, and **safe**
   token rotation) and **must not pretend** a multi-bot capability exists. The
   schema and API are shaped so a bot registry can be added later **without a
   rewrite** (e.g. bot-scoped keys/columns and a `bot_id` seam that currently
   resolves to the single configured bot), but **no multi-bot behaviour is
   implemented** and no existing behaviour changes.
6. **Untouchable** — Chat persona/architecture, credentials, pools, rate limits,
   breakers, context ownership, tenant isolation, workload boundaries
   (requirement §2). The dashboard reads through existing modules; it must not
   bypass `rbac`, `admin_service`, `groups`, or `key_store`.

**Staged plan (each stage = tests → secret-scan → commit → push → verify).**
* **M1 — Foundation.** `app/web/` package: `server.py` (aiohttp app), `auth.py`
  (scrypt + signed cookie + CSRF + password epoch, audience-scoped), `jinja.py`,
  `render.py`, `labels.py`, `jalali.py`, `copy.py`, `static/` + `templates/`
  (dark theme from the VPN-bot family). Login/logout, `/healthz`, session
  expiry/rotation, brute-force rate limit. Tests: auth, session fixation, CSRF,
  cookie flags.
* **M2 — Authorization + audit.** Dashboard identity ↔ `rbac`; every mutation
  server-authorized; `dashboard_audit` append-only; IDOR/cross-group tests.
* **M3 — Overview.** Real metrics only, from existing tables
  (`*_usage`, `gemini_daily`, `gemini_events`, `admin_audit`, `authorized_groups`,
  `nexus_state`, `awareness_control`, `search_control`), with
  today/7d/30d/custom and DB-layer aggregation + pagination.
* **M4 — AI Control Center + providers/credentials.** Per-workload view over
  `gemini_pool`/`gemini_accounts`/`key_store`; add/replace/rotate/disable with
  validate → activate → verify → rollback, secrets masked (fingerprint only).
* **M5 — Groups.** Read-only first (list/detail/usage/audit), then
  register/disable/re-enable through `admin_service`, never cross-group leakage.
* **M6 — Bot management.** The existing single bot: health, polling/webhook
  status, groups, usage, errors, audit. Token rotation done **safely** (validate
  new token via `getMe` → confirm it is the expected bot → persist → audit
  without the secret → activate → verify health → rollback on failure → retire
  the old token only after success).
* **M7 — Usage/analytics + audit log pages.** Filters, charts, pagination,
  export-free (no bulk dumps).
* **M8 — Security & performance pass.** The §18 test list, the §19 measurements,
  responsive/keyboard/accessibility pass, then deploy + post-deploy verification
  (image/code identity, health, dashboard smoke, security smoke, live Chat probe,
  regressions, ERROR/CRITICAL scan, restart count, drift, migrations, audit
  events, secret-leak check, rollback path).

**Known limitations / risks to record now.** (a) No multi-bot support (see
decision 5). (b) No lint/type tooling exists, so "static checks" are limited to
`py_compile` + the suite. (c) The dashboard adds an HTTP surface that must be
firewalled/behind nginx like the VPN bot's; it must not be publicly exposed
without TLS. (d) SQLite is single-writer: analytics must aggregate at the DB
layer and paginate, not pull rows into the app.

**NEXT STEP (exact).** Both open decisions are now **locked** (see decisions 2
and 5 above): a **separate `dashboard` Compose service** sharing `./data`, with
aggressive image/layer optimization; and **single bot now, designed for
multi-bot later**. Implement **M1** (`app/web/` foundation: aiohttp app, scrypt +
signed session cookie + CSRF, Jinja templates + dark static shell, login/logout,
`/healthz`, session expiry/rotation, brute-force rate limit, and tests), plus the
`dashboard` Compose service, `.dockerignore`, log rotation and the **measured
image/layer report**; then commit, push, and continue M2…M8 from the repository.
Do **not** modify Chat, pools, credentials, limits, breakers, tenant isolation or
workload boundaries.

---

### 54.25 Checkpoint (2026-09-24, **M1 — the panel's foundation is built**) — M1 DONE, NOT DEPLOYED, M2 NEXT

**CHECKPOINT STATUS.** Date **2026-09-24 ~22:40Z**. Branch **`main`**. M1 of the
Admin Control Center (§54.24) is **implemented, tested and committed**. The
dashboard service is **defined but not started**: the running bot is untouched
(`guardbot`, image `f36e60bf3971`, `RestartCount=0`, verified before and after
the build). Nothing in Chat, the pools, the credentials, the limits, the
breakers, tenant isolation or the workload boundaries was modified.

**What M1 ships (exactly what the §54.24 plan asked for).**

| Piece | File |
|---|---|
| aiohttp app, middlewares, entry point | `app/web/server.py`, `app/web/__main__.py` |
| identity, scrypt, signed cookie, CSRF, epoch, throttle | `app/web/auth.py` |
| the credential file + the epoch it protects | `app/web/credentials.py` |
| typed request/app keys | `app/web/context.py` |
| Jinja environment + Persian filters | `app/web/jinja.py` |
| shared page context, render, redirect, `safe_next` | `app/web/render.py` |
| Persian digits, Jalali, durations | `app/web/jalali.py` |
| every word the panel shows | `app/web/copy.py` |
| login / logout / `/healthz` | `app/web/routes/auth_routes.py` |
| the landing page | `app/web/routes/home.py` |
| dark shell (RTL, Vazirmatn stack, gold accent, own logo) | `app/web/templates/*`, `app/web/static/{app.css,logo.svg}` |
| password CLI (hash / apply / clear / status) | `ops/dashboard_passwd.py` |
| reverse proxy with TLS + security headers | `ops/nginx-dashboard.conf.example` |
| the panel's two dependencies, in their own layer | `requirements-dashboard.txt` |
| the second process over the same image and volume | `docker-compose.yml` → `dashboard` |

**Decisions taken during M1 (all reversible, none touching the bot).**

* **One image, two processes.** Both compose services build the same tag
  (`guardbot:latest`, declared once via an `x-guardbot-image` anchor). The
  dashboard is a second `command` over the same layers, not a second image.
  This does rename the bot's image from `guardbot-guardbot` to `guardbot:latest`
  on the next `docker compose build`; the running container keeps its image ID,
  so nothing restarts.
* **The dashboard's dependencies are their own Docker layer.**
  `requirements.txt` is copied and installed first (unchanged → cache hit), then
  `requirements-dashboard.txt` in a second layer. `aiohttp` is now declared
  directly as well as being present transitively through `py-tgcalls`, because
  `app/web` imports it directly.
* **The panel never migrates the schema.** The bot owns the tables and applies
  the migrations at boot; a second process running them concurrently would race
  it. M1 does not touch the database at all.
* **Dark-only, no theme toggle, no JavaScript.** The plan said "dark static
  shell", and the panel is one operator on one machine. There is no light
  palette, no `theme.js` and no client-side behaviour, so there is nothing that
  can break with scripting disabled.
* **Session rotation is a sliding re-mint**, at half the session's life, on safe
  methods only, and *before* the handler runs — so the CSRF token on the page
  just rendered is the token the new cookie carries. Rotating after the handler
  would break every POST from that page.
* **`labels.py` was not created.** The plan listed it for M1, but M1 has no
  status to label; an empty mapping module is dead code. It arrives with the
  first page that has a status (M3).
* **The credential is a file, not a table** (`/data/dashboard_credentials.json`,
  mode 600, atomic write), because the SQLite database is backed up and copied
  around and the panel's password should not travel with it.

**Verification (all measured, none assumed).**

| Check | Result |
|---|---|
| Focused suite `tests/test_web_dashboard.py` | **55 passed**, 22 subtests (6.0 s) |
| Full suite | **3812 passed / 0 failed** (233.8 s) — was 3757, so **+55** |
| `py_compile` on every new module | clean |
| Container smoke (throwaway, `--rm`) | `/healthz` → `{"status":"ok"}`; `/login` renders the shell; POST → 303 + `gb_admin` cookie; the compose healthcheck command exits 0 |
| Bot container before/after the build | `guardbot` still `f36e60bf3971`, `RestartCount=0`, no restart |
| Secret scan of the staged diff | clean (27 files, no key, token or `.env` value) |

**Measured image / layer report (the owner's hard requirement).**

| Item | Measured |
|---|---|
| Bot image before | `guardbot-guardbot:latest` = `f36e60bf3971`, **1.15 GB** |
| Shared image after | `guardbot:latest` = `7768f5dc0053`, **1.15 GB** |
| Layers in the new image | 13 total, **8 shared** with the old image, **5 new** |
| Reused unchanged | the 457 MB `ffmpeg` layer and the **259 MB** `pip install -r requirements.txt` layer (cache hits — the build took **13 s**, which is the proof) |
| New layer: `COPY requirements-dashboard.txt` | 12.3 kB |
| New layer: `pip install -r requirements-dashboard.txt` | **1.48 MB** (Jinja2 3.1.6 = 1.11 MB + MarkupSafe 3.0.3 = 81 kB) |
| New layer: `COPY app ./app` | 6.43 MB (grew by the panel's source; `app/web` itself is **126 kB**) |
| New layer: `COPY tools ./tools` | 1.06 MB (a code change, not the panel) |
| New layer: `COPY ops ./ops` | 41 kB |
| **Dashboard-specific unique addition** | **≈ 1.53 MB** (deps + `ops`) + 126 kB of source |
| `aiohttp` | **not added** — 7.66 MB, already in the image (transitively via `py-tgcalls`) |
| Extra persistent data | **none** — the same `./data` volume; M1 writes no database row |
| Extra persistent logs | the same `json-file` ceiling as the bot: `10m` × 3 files |
| Disk on `/` before vs after the build | **2.5 GB free / 88 %** both before and after (no measurable change) |

**Known limitations / risks to carry forward.** (a) The dashboard is **not
deployed**: `docker compose up -d dashboard` has not been run, and doing so is a
deploy that needs the owner's go-ahead (§54.24 decision, and the standing rule
that a deploy means deploy *and* a live probe). (b) `DASHBOARD_SECRET` and a
password are **not set** in the host `.env`, so a started panel would log
everyone out on restart and refuse every login; setting both is part of the M8
deploy. (c) `DASHBOARD_SECURE_COOKIES` is off by default and **must be turned on
with the nginx TLS proxy** — it is the only thing that keeps the session cookie
off plain HTTP. (d) Still no multi-bot support (§54.24 decision 5); the schema
and API seam for it does not exist yet either — it arrives with the bot page in
M6. (e) SQLite is still single-writer: every later analytics page must aggregate
at the DB layer and paginate.

**NEXT STEP (exact).** Implement **M2 — Authorization + audit**: bind the
dashboard identity to `app/rbac.py` (a Telegram group admin is *not* a dashboard
admin), make every mutation server-authorized through the same boundary the bot
uses (`admin_service`), add the append-only `dashboard_audit` table via
`CREATE TABLE IF NOT EXISTS` + `_ensure_column` (no destructive rewrite), and add
the IDOR / cross-group tests. Then M3 (Overview) … M8 (security, performance,
deploy). Do **not** deploy the dashboard without the owner's go-ahead, and do
**not** modify Chat, pools, credentials, limits, breakers, tenant isolation or
workload boundaries.

---

### 54.26 Checkpoint (2026-09-24, **M2 — authorization + audit**) — M2 DONE, NOT DEPLOYED, M3 NEXT

**CHECKPOINT STATUS.** Date **2026-09-24 ~23:05Z**. Branch **`main`**. Base /
rollback commit **`e50ec5c`** (M1 — the panel's foundation). M2 of the Admin
Control Center (§54.24/§54.25) is **implemented, tested and committed**. The
dashboard is still **defined but not started**: the running bot is untouched
(`guardbot`, image `f36e60bf3971`, `RestartCount=0`, verified before and after
the rebuild). Nothing in Chat, the pools, the credentials, the limits, the
breakers, tenant isolation or the workload boundaries was modified.

**What M2 ships.**

| Piece | File |
|---|---|
| the panel's authority: `rbac.resolve` + fail-closed middleware + `@requires` | **`app/web/authz.py`** (new) |
| the panel's own trail: writer, prune, never raises | **`app/web/audit.py`** (new) |
| `dashboard_audit` table, `connect()` split out of `init()`, truncating writer, retention | `app/db.py` |
| the bound operator: `pid` in the session, re-checked on every read, `operator_id()/operator_configured()` | `app/web/auth.py` |
| `DASHBOARD_OPERATOR_ID`, `DASHBOARD_AUDIT_RETENTION_SECONDS` | `app/config.py` |
| login / failed / throttled / logout auditing, the unconfigured-operator refusal | `app/web/routes/auth_routes.py` |
| the permission declaration + the resolved role on the landing page | `app/web/routes/home.py` |
| the 403 sentence that distinguishes "not signed in" from "not allowed" | `app/web/copy.py`, `app/web/server.py`, `app/web/templates/*` |
| `PRINCIPAL` request key | `app/web/context.py` |
| the panel audit counter in the shared test fixture | `tests/conftest.py` |
| **31 new tests**: identity, gate, trail, IDOR, cross-group | **`tests/test_web_dashboard_authz.py`** (new) |

**Decisions taken during M2 (all reversible, none touching the bot).**

* **The permission vocabulary is the bot's, not a second one.** The panel's pages
  require `config.manage` and nothing else, and the panel **does not append to
  `rbac.PERMISSIONS`** — that tuple is also the wire format of the bot's
  promotion-dialog bitmask, so adding to it would add a tick box to the bot's own
  UI. A test asserts `PANEL_PERMISSION in rbac.PERMISSION_SET` and
  `AUTHENTICATED not in rbac.PERMISSION_SET`.
* **One configured operator id, stamped into the session.** `DASHBOARD_OPERATOR_ID`
  (default `OWNER_USER_ID`) is the only identity the panel authorizes; it is
  stamped as `pid` at mint time and re-checked on **every** read, so re-pointing
  the panel at a different id retires the sessions minted under the old one —
  the same shape as the password epoch, for the same reason. The id is never read
  from `admins`, `CONFIG_ADMINS`, or anything a client sends.
* **Declaration + fail-closed middleware, not per-handler checks.** The permission
  is an attribute on the handler (`@authz.requires`); the middleware refuses a
  protected route that declares nothing. A **route-inventory test** walks
  `app.router.routes()` and fails if any non-public route declares no permission
  or declares a name that is not real, so the omission fails in the suite rather
  than in production.
* **A separate `dashboard_audit` table, not rows in `admin_audit`.** The panel's
  actor is a configured operator with a password, not a Telegram user, and its
  events are logins/refusals/logouts — not administrative actions inside a chat.
  Writing them into `admin_audit` would put them in front of the bot's own audit
  view, which would be a change to the bot's behaviour. Proven live: in scenario C
  the panel's trail had the login row while the bot's `admin_audit` had **0 rows**.
* **`db.connect()` was split out of `db.init()`.** The panel opens the same
  database but runs **no** migrations (AgentMD §53.13); the split is what makes
  that possible. The table is new, so `_ensure_column` was **not** needed —
  `CREATE TABLE IF NOT EXISTS` plus an index is the whole migration, and it is
  idempotent from either process.
* **The throttle audits one row per blocked window per address, not one per
  knock.** The brake is on the address, so a blocked caller can keep knocking;
  a row per request would make a brute-force attempt a way to grow the table from
  outside. Bounded by construction at `max_failures + 1` rows per address per
  window, and a test asserts it (6 blocked requests → 1 row).
* **No mutation exists yet, and none is claimed.** M2 makes every route
  server-authorized through one boundary; the panel's only state-changing routes
  today are `/login` and `/logout`. The first real mutation arrives in M3 and will
  go through `admin_service` — the same boundary the bot uses — not through a
  second path. §54.25's NEXT STEP named `admin_service` here; that is deferred,
  not dropped, and this checkpoint says so rather than implying a mutation path
  that does not exist.

**A bug the live probe found and the suite did not.** Container smoke scenario B
(panel bound to a non-owner id, on a database where the bot had never run) returned
**500 on `POST /login`**. Cause: the login audit row asks for the operator's role
→ `authz.principal` → `rbac.resolve` → `db.admin_get` → the `admins` table does
not exist, and `rbac.resolve` (unlike `rbac.resolve_many`) does not tolerate that.
Fixed in `app/web/authz.py`: an authority that cannot be read is **no authority**
(`rbac.guest(pid)`, logged), which is the rule `rbac.resolve_many` already stated
— so the panel refuses with the permission sentence instead of falling over. A
regression test (`test_an_unreadable_authority_is_a_refusal_not_a_crash`) drives
the same path end-to-end. This is the second time a live probe found something the
suite could not (§54.25's lesson, repeated).

**Verification (all measured, none assumed).**

| Check | Result |
|---|---|
| `tests/test_web_dashboard_authz.py` (new) | **31 passed**, 14 subtests |
| Both dashboard suites | **86 passed**, 36 subtests (5.2 s) |
| Full suite | **3843 passed / 0 failed** (244.5 s) — was 3812, so **+31** |
| `py_compile` on every changed module | clean |
| Secret scan of the staged diff | clean (16 files; no key, token or `.env` value) |
| **Smoke A** — operator = the owner (`424242`) | `/healthz` 200; `/` 302 → login; `POST /login` 303 + `gb_admin` cookie; `/` **200**, role **«مالک»**; `POST /logout` 303. Trail: `login\|ok\|424242\|owner`, `logout\|ok\|424242\|owner`. **0 tracebacks** |
| **Smoke B** — operator `555555`, bot's schema absent | `/healthz` 200; `POST /login` **303** (was the 500); `/` **403** with the permission sentence; `POST /logout` 303. Trail: `login\|ok\|555555\|guest`, `authz.refused\|refused\|555555\|guest\|not_admin`, `logout\|ok\|555555\|guest`. The panel created **only** `dashboard_audit` |
| **Smoke C** — operator `555555` granted `senior_admin` in the bot's own table | `/healthz` 200; `POST /login` 303; `/` **200**, role **«مدیر ارشد»**; **0 tracebacks**. Panel trail: `login\|ok\|555555\|senior_admin`. The bot's `admin_audit`: **0 rows** — the two trails are separate |
| Bot container before/after the rebuild | `guardbot` still `f36e60bf3971`, `RestartCount=0`, no restart |

**Measured image / layer report (the owner's hard requirement, continued).**

| Item | Measured |
|---|---|
| Shared image | `guardbot:latest` = **`9fd5fd927dc8`**, **1.15 GB** (unchanged from M1's `7768f5dc0053`) |
| New layers | **none** — M2 adds no dependency, so the `requirements-dashboard.txt` layer (12.3 kB) and the `pip install` layer (1.48 MB) are **cache hits** |
| `COPY app ./app` | **6.43 MB → 6.47 MB = +40 kB** (M2's source only) |
| `COPY app ./app` vs the running bot's image | 6.18 MB → 6.47 MB = **+0.29 MB** (M1 + M2 together) |
| `app/web` on disk | **126 kB → 268 kB** (+142 kB: `authz.py`, `audit.py`, and the edits) |
| Reused unchanged | the 457 MB `ffmpeg` layer and the 259 MB `pip install -r requirements.txt` layer (the build took **4 s**, which is the proof) |
| Extra persistent data | **none** — the same `./data` volume. The panel writes rows to its own table, which the bot never reads |
| Extra persistent logs | the same `json-file` ceiling as the bot: `10m` × 3 files |
| Disk on `/` before vs after | **2.5 GB free / 88 %** both before and after (no measurable change) |

**Known limitations / risks to carry forward.** (a) The dashboard is **not
deployed**: `docker compose up -d dashboard` has not been run, and doing so is a
deploy that needs the owner's go-ahead (a deploy means deploy *and* a live probe).
(b) `DASHBOARD_SECRET` and a password are **not set** in the host `.env`, so a
started panel would log everyone out on restart and refuse every login; setting
both is part of the M8 deploy. (c) `DASHBOARD_SECURE_COOKIES` is off by default
and **must be turned on with the nginx TLS proxy** — it is the only thing that
keeps the session cookie off plain HTTP. (d) A configured operator who is **not**
the owner needs an rbac principal that holds `config.manage` (a `senior_admin`
row, or a `CONFIG_ADMINS` entry); otherwise the panel accepts the login and
refuses every page. That is fail-closed and deliberate, but it is the one
misconfiguration that reads as "the panel is broken" — smoke B is exactly that
state, and the 403 sentence names `DASHBOARD_OPERATOR_ID` to make it diagnosable.
(e) Still no multi-bot support (§54.24 decision 5); the seam arrives with the bot
page in M6. (f) SQLite is still single-writer: every later analytics page must
aggregate at the DB layer and paginate.

**NEXT STEP (exact).** Implement **M3 — Overview**: the panel's first *read*
pages over the existing modules — bot/service status, the groups it serves, the
workload/pool health — with the permission each page needs declared through
`@authz.requires`, every query scoped and paginated at the DB layer, no new
permission added to `rbac.PERMISSIONS`, and no mutation (the first mutation is
M4/M5 and goes through `admin_service`). Add `app/web/labels.py` when the first
page has a status to label (§54.25 deferred it for exactly that reason). Then
M4 … M8. Do **not** deploy the dashboard without the owner's go-ahead, and do
**not** modify Chat, pools, credentials, limits, breakers, tenant isolation or
workload boundaries.

---

## 55. Context Preservation & Session Handoff

**This is a permanent, non-bypassable project rule.** No new session, agent or
context window may cause the workflow, the architecture decisions, the completed
work, the tests, the open problems, the checkpoints or the NEXT STEP to be lost.

**Git and `AgentMD.md` are the source of truth for continuing the work — not a
session's transient memory.**

### 55.1 Never rely on chat context as project memory

Critical project information must never live only in a context window, a chat
history or an agent's transient memory. Everything needed to continue correctly
must be recorded in the repository — in `AgentMD.md` or a durable checkpoint.

"Critical" includes, at minimum:

- the current branch;
- the base commit;
- the most recent commits;
- completed work;
- partial work;
- pending work;
- architecture decisions;
- significant changes;
- affected files and components;
- tests run;
- benchmarks;
- test results;
- known failures;
- provider / infrastructure state;
- credential / token state, without exposing a secret;
- the rollback point;
- current limitations;
- work that must deliberately not be done;
- frozen work;
- the exact NEXT STEP.

Nothing important may remain only in the conversation.

### 55.2 Context usage monitoring

On a long task, stay aware of how much context has been consumed:

- **~70%** — reduce redundant investigation, stop re-scanning the repository,
  keep important discoveries and decisions in `AgentMD.md`, and avoid repeating
  unnecessary output.
- **~80%** — before starting new work, create or update a durable checkpoint:
  record the current state in the repository, commit the checkpoint, and push it
  if the state needs to survive on the remote. Only then continue, and only with
  essential, related work.
- **~90%** — do not start new, broad work. First record and commit the smallest
  safe checkpoint. The repository must be self-contained enough that a new agent
  or session can continue without guessing. Critical project state must not
  remain only in the chat.

If context reaches the point where continuing safely is no longer possible:
checkpoint, commit, push, then stop.

### 55.3 Durable checkpoint format

Every important checkpoint records at least:

- **CHECKPOINT STATUS** — date/time, branch, HEAD commit, base/rollback commit,
  current task;
- **completed work**, **partial work**;
- **changed components/files**;
- **architecture decisions**;
- **tests executed** and **test results**;
- **benchmarks**, if applicable;
- **known issues**, **blockers**;
- **frozen items**;
- **explicitly prohibited actions**;
- **pending verification**;
- the exact **NEXT STEP**.

The most important part is the NEXT STEP: it must be a precise, executable
instruction for the next agent.

- Good: *"Do not modify code. First wait for the provider freeze to be explicitly
  lifted. Then run the existing live context-quality probe with the documented
  command. Do not rotate or move credentials."*
- Bad: *"Continue testing."*

The NEXT STEP must never be vague.

### 55.4 Session handoff protocol

Every new session or agent working on this repository must, before any
implementation, inspect:

1. `git status`
2. the current branch
3. HEAD
4. recent commits
5. `AgentMD.md`
6. `AGENTS.md` / project instructions, if present
7. the latest checkpoint
8. the files and components relevant to the NEXT STEP

Then reconcile the state recorded in `AgentMD.md` with the real repository. Do
not trust a checkpoint blindly:

- if the checkpoint says commit X exists, Git must confirm it really does;
- if the checkpoint says work is completed, verify the relevant
  implementation/tests when needed;
- if the real repository differs from the checkpoint, treat the real repository
  and Git as the source of truth, identify the discrepancy, reconcile
  `AgentMD.md` with reality before continuing, and never re-do implementation
  merely because an old checkpoint says so.

### 55.5 Never restart completed work

A new session must not audit or reimplement the project from scratch. After
reading the checkpoint:

- do not reimplement completed work;
- do not redesign the existing architecture;
- do not replace an existing subsystem with a parallel one;
- do not re-run previously run tests without reason;
- do not perform a full repository scan merely for reassurance.

Re-examine or re-run completed work only when the repository state shows it does
not really exist, the implementation is broken, the checkpoint contradicts Git,
or verification is genuinely required to continue.

### 55.6 Preserve decisions, not just file changes

A checkpoint is not merely a list of changed files. It must preserve the
important architecture decisions and their reasoning — for example:

- why an existing subsystem was reused;
- why a new subsystem was not created;
- why a provider/workload was kept separate;
- why a feature is currently disabled/frozen;
- why a token allocation was deliberately left unchanged;
- why a failure is acceptable or expected;
- what was deliberately left out of scope.

The goal is that the next agent does not have to re-derive the same reasoning
from scratch.

### 55.7 Preserve "do not do" information

Negative information must also be durable. If something must not be done, record
it explicitly. For example, **DO NOT**:

- run the frozen live probe;
- rotate credentials;
- move tokens;
- merge to `main`;
- deploy;
- create a parallel State system;
- modify financial/audit history;
- expose secrets.

These must persist in the checkpoint or `AgentMD.md` so the next session does
not do them by mistake.

### 55.8 Commit checkpoints before context loss

A checkpoint must not remain only in the working tree. When a checkpoint is
preserving critical state:

1. update `AgentMD.md`;
2. review the diff;
3. confirm no secret entered it;
4. create a clear commit;
5. push to the current branch;
6. verify the remote.

The checkpoint must be recoverable on GitHub.

### 55.9 Safe continuation after compaction

If the context was compacted, a new session started, or the agent senses it has
lost part of the previous workflow: **stop implementation temporarily.** First:

1. read `AgentMD.md`;
2. find the latest checkpoint;
3. check `git status`;
4. check the branch;
5. check HEAD and recent commits;
6. compare the recorded state with the real repository;
7. find the NEXT STEP;
8. continue only after verifying all of the above.

Never reconstruct the workflow from guesswork.

### 55.10 Context loss must not change architecture

Context loss must not cause an agent to: build a new architecture, create a
parallel implementation, change a prior decision without investigation,
duplicate an existing subsystem, expand scope, or rebuild completed features.

If an architecture decision is recorded in `AgentMD.md`, a new session continues
that decision unless repository evidence shows it is no longer valid. If the
architecture changes, the reason must be recorded in `AgentMD.md`.

### 55.11 Handoff must be self-contained

At the end of a long task, or before context exhaustion, the agent must be able
to hand a new session at least:

- **CURRENT STATE** — what is true now?
- **WHAT CHANGED** — what was implemented?
- **WHAT WAS VERIFIED** — what tests/checks actually passed?
- **WHAT IS NOT VERIFIED** — what remains uncertain?
- **WHAT IS BLOCKED** — what cannot currently proceed, and why?
- **WHAT MUST NOT HAPPEN** — what actions are explicitly forbidden/frozen?
- **NEXT STEP** — exactly what the next agent should do.

A new agent must be able to continue from the repository without asking the
previous agent: "What did you do?", "What was the plan?", "Where were we?",
"What should I run next?".

### 55.12 AgentMD is durable project memory

`AgentMD.md` is not merely documentation. It is the project's durable
operational memory for architecture, implementation state, decisions,
checkpoints, known limitations, test state, blockers, frozen work and handoff
instructions. Update it when an important project decision or state change
occurs. Do not overload it with transient noise — preserve only durable
information that can materially affect future engineering decisions.

### 55.13 Final rule

**"CONTEXT IS TEMPORARY. THE REPOSITORY IS DURABLE."**

Chat context may disappear. A session may restart. The agent may change. The
context window may compact. The project must still remain understandable and
safely continuable from Git + `AgentMD.md`. **No critical workflow may exist
only in the agent's memory.**

- Before context exhaustion: **CHECKPOINT → COMMIT → PUSH → VERIFY.**
- After a new session: **READ → VERIFY → RECONCILE → CONTINUE.**
- Never: **GUESS → RESTART → DUPLICATE → REIMPLEMENT.**
