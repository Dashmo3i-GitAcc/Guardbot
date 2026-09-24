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

A private chat belongs to the owner: the second gate, and the two duplicate-reply defects. `accepts_private` checks offline first and then `is_owner`, `NEXUS_ACTORS_ONLY` cannot open it, and the gate runs before `_answer_conversationally`, so a non-owner costs no model call and no row.

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
  nor its caller changes.
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
  a setting**: `accepts_private` checks offline first and then `is_owner`,
  `NEXUS_ACTORS_ONLY` cannot open it, and being an administrator cannot open it.
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
  (`anchor_act`, budget 420 — the longest block the corpus produces is 297), and
  the two lines that contradict a naive reading come **first**: `_clip` keeps
  whole lines from the front, so what survives a tight budget is the warning, not
  the claim it warns about.
* `tools/eval_intent.py` reports **`object_person_offered_for_a_thing`** — the
  residual wrong lead, a thing-object request for which `referents` still lists a
  person — rather than hiding it, and `tests/test_intent_eval.py` **pins** it at 0
  after the resolver guard below drove it from 5. "A thing" there is the labelled
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

## 54. Nexus intelligence evolution — checkpoint (2026-09-24)

A durable checkpoint for continuing the Intent/Awareness evolution. `§53.7` is
the invariant set; this section is the **state**, not a rule, and is meant to be
replaced as the work advances.

**Where the work is.** Branch `develop/nexus-intelligence-evolution`, HEAD
`66a1e94` plus increment P (pushed to both remotes `origin`/`dashmo3i`). `main`
and the rollback base are untouched at `00c5d1d`. Increments A–P are done; the
deterministic benchmark in `tools/eval_intent.py` is clean over 134 cases
(`tests/test_intent_eval.py` holds the floor) and the full suite is
**3236 passed / 0 failed**. **No merge, no deploy** without the owner's explicit
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

**Next increment (Q), in priority order.** The owner's list still stands
(1–5 intent/Awareness understanding, 10–12 scheduling/adaptivity/integration, 13
model routing only if benchmarks prove it, 14 verification only where
measurable). The concrete threads P leaves:

1. The **rendered prose** is now scored for the referent block only. The other
   rendered blocks — the object's "the verb decides", the room-state graph's
   "converged on", the act block's quoted directive — are scored only where they
   move a labelled verdict (§55.5). Extending the same "score the product, not
   the reading" discipline to the **act block's prose** is the next measured step.
2. `role-two-admins` stays ambiguous by design; check whether the corpus should
   carry a case where the room's reply convergence *should* break the tie.

**Constraints carried:** speed-first (no unnecessary Gemini calls; benchmark
latency before/after every change), rollback safety, no merge, no deploy.
