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
API's real limits, ffmpeg, ONNX/CPU inference, Docker on a small VPS, and
SQLite under concurrent access.

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
   - moderation decisions → `app/decision.py` **and** `app/moderation.py`
     **and** the handler in `app/main.py` that consumes them;
   - detector output → `app/detector.py` (the `MediaAnalysis` shape is the
     interface between detector and policy);
   - configuration → `app/config.py` **and** `.env.example` (they must stay in
     sync);
   - anything media-related → the `finally`/cleanup path in `app/main.py`.
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
  `Pillow`, `nudenet==3.4.2` (bundles a small ONNX model; pulls
  `onnxruntime` + `opencv-python-headless`), and `transformers` + CPU-only
  `torch` for the second-stage scene classifier. Tests need
  `pytest` and the same runtime, so run them inside the image (see §11).
- **Entry point:** `python -m app.main` (`app/main.py:main`). It creates the
  DB dir and temp dir, calls `db.init()`, loads the detector if
  `MEDIA_ENABLED`, builds the `Application`, registers handlers, then
  `run_polling(allowed_updates=[MESSAGE, CALLBACK_QUERY, CHAT_MEMBER],
  drop_pending_updates=True)`.
- **Modules and their single jobs:**

  | File | Responsibility |
  |---|---|
  | `app/config.py` | every setting, from environment variables |
  | `app/db.py` | SQLite: `users` (strikes/violations) and `captchas` |
  | `app/burst.py` | pure, bounded instant-flood tracker (no Telegram, no I/O) |
  | `app/detector.py` | raw detections only; NudeNet + ffmpeg frame sampling + the scene classifier |
  | `app/decision.py` | the policy: `MediaAnalysis` → `SAFE`/`REVIEW`/`EXPLICIT` |
  | `app/moderation.py` | executing a decision (delete); no Telegram import |
  | `app/main.py` | Telegram wiring: captcha handlers + the media/flood pipeline |

  The detector/policy split is deliberate: `detector.py` produces raw
  detections, `decision.py` owns *which* classes and *which* confidence count
  as explicit. Keep them separate — a future stage must be able to extend one
  without touching the other. `burst.py` is pure and bounded for the same
  reason: the flood rule is unit-testable without Telegram or a clock.

- **Three independent signals, never conflated:** explicit sexual content
  (detector + decision engine), instant media flood (burst tracker), and the
  repeated-violation ladder. A flood is a violation on its own and does not
  require sexual content; a photo is never counted toward a flood.
- **Deployment:** `Dockerfile` (`python:3.12-slim` + `ffmpeg` + CPU-only torch
  from the pytorch CPU index, `HF_HOME=/data/hf`, `CMD python -m app.main`) and
  `docker-compose.yml` (service `guardbot`, `restart: always`, `env_file:
  .env`, `./data:/data`, 2 GB memory limit). The VPS runs it from `~/guardbot`
  with `docker compose up -d --build`. `HF_HOME` is why the ~340 MB scene
  model survives rebuilds; do not point it somewhere that is not the volume.
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
| `SAFE` | normal / non-explicit | allow, log only |
| `REVIEW` | an explicit class below the delete threshold, or a scene score in the review band | allow, log only — **never** delete, **never** notify |
| `EXPLICIT` | an `EXPLICIT_CLASSES` detection at ≥ `EXPLICIT_DELETE_THRESHOLD`, **or** a scene score at ≥ `SCENE_DELETE_THRESHOLD` | delete the Telegram message |

- Only classes listed in `EXPLICIT_CLASSES` can ever produce `EXPLICIT` from
  NudeNet.
- `REVIEW` is a log-only state. It sends no admin message and applies no
  punishment. This is intentional, not an omission.
- There are **two** evidence sources and either can produce `EXPLICIT`:
  1. **NudeNet anatomical evidence** — an `EXPLICIT_CLASSES` detection at or
     above `EXPLICIT_DELETE_THRESHOLD`.
  2. **Scene-level sexual content** — the second-stage classifier's NSFW score
     at or above `SCENE_DELETE_THRESHOLD`, even when NudeNet found nothing.
     This is deliberate: it is what catches a sexual act whose anatomical class
     was not detected.
- The scene score is **graded**: below `SCENE_REVIEW_THRESHOLD` it is `SAFE`,
  at/above it the media is `REVIEW`, and at/above `SCENE_DELETE_THRESHOLD` it is
  `EXPLICIT`. `SCENE_DELETE_THRESHOLD` (default 0.95) is deliberately high —
  do not lower it to "catch more" without real evidence.
- `DecisionResult.source` is `"nudenet"`, `"scene"` or `"none"` and records
  which signal decided. Never merge the two signals into one score.
- An **absent** scene score (`None`: stage disabled, model missing, decode or
  inference error) is not zero and **never** deletes. Do not replace `None`
  with a default score.

### 4.2 Fail open

- Any detector error, model-load failure or media-decode failure produces
  `MediaAnalysis(ok=False)`, and `DecisionEngine.decide` returns `SAFE` for it.
  Uncertainty never deletes anything.
- The whole media handler body is wrapped in `try/except Exception` that logs
  `media pipeline failed` and does nothing else. A crash inside the pipeline
  must not delete, notify or punish.
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

- The admin chat (`ADMIN_LOG_CHAT`) receives a message **only** for
  `EXPLICIT` + `DELETE_SUCCESS`, and for a confirmed flood whose restriction
  Telegram refused. `SAFE`, `REVIEW`, `DELETE_FAILED`, a *successful*
  restriction and every operational error are container-log only.
- The report carries media type, user, user id, username, chat id, message id,
  the detection that fired, its score, a reason line and a UTC timestamp, plus
  an evidence frame. The evidence frame is
  `MediaAnalysis.evidence_frame(result.matched.label)` for a NudeNet deletion,
  and `MediaAnalysis.scene_frame` (the frame that produced the scene score) for
  a scene-stage deletion — a scene-only deletion still gets an evidence image.
  The reason sentence matches the source: it must **not** claim genital
  evidence when the deletion came from the scene stage. Keep the report
  readable for a non-technical admin.
- Evidence upload falls back `send_photo` → `send_document` → text-only
  `send_message`. **The report itself must never be lost** because an upload
  failed. Keep that fallback order.
- The report text is Persian and HTML-parse-mode. If you touch it, keep the
  same register and the same fields; do not machine-translate or restructure it.
- Every report carries a single inline button (`REPORT_DELETE_CALLBACK =
  "report_delete"`, label `🗑 حذف گزارش`). It removes **the report message
  itself** — the moderated message is already gone. It is attached to all three
  delivery paths (photo, document, text-only) and never sent as a separate
  message. `app/main.py:on_report_delete` verifies
  `callback_query.message.chat.id == config.ADMIN_LOG_CHAT` first, then that the
  presser is a **current member** of that chat (`_is_chat_member`: MEMBER /
  ADMINISTRATOR / OWNER / RESTRICTED), and only then deletes. Membership is
  checked fresh on every press and fails closed. This path deliberately does
  **not** use `is_admin` and does **not** require Telegram administrator
  status: the report group is a private trusted team group, so any member may
  clean up a report. A callback from any other chat deletes nothing.

### 4.5 Punishment is a timed restriction, never a ban

- The only member action is `restrict_chat_member` with `MUTED` permissions and
  an expiry of `MUTE_MINUTES` (default 15). Telegram lifts a timed restriction
  itself, so there is no reaper. `MUTE_MINUTES=0` means no automatic expiry.
  The unit is **minutes**: the old `MUTE_HOURS` (24) is no longer read, and a
  leftover `MUTE_HOURS` in a live `.env` must stay inert.
- There is **no ban and no permanent punishment**. Do not add one.
- One confirmed explicit deletion is one violation, recorded through the
  existing `db.add_strike` / `users.strikes` (do not add a second violation
  store). Every violation warns the user; at `VIOLATION_MUTE_AFTER` (default 3)
  the timed restriction is applied. Every violation at or after the threshold
  re-applies it, which extends the restriction.
- **A failed deletion, a detector error or a database error never punishes
  anyone.** If recording the violation fails, the deletion still stands and
  `outcome.strike` is `None`, so nothing else happens.
- `_restrict_user` returns True only when Telegram accepted the call. A refusal
  (an administrator target, missing `can_restrict_members`) is logged and
  reported, never claimed as a success.

### 4.5.1 The test account exception

`TEST_USER_ID` (default `8299811287`) is the owner's test account. It exists so
the pipeline can be exercised repeatedly without a manual unrestrict.

- It is **not exempt from anything**: detection, deletion, the strike, the
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
  a flood; each photo is still checked by the content pipeline on its own.
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
- **Telegram admins are not exempt** — not from content moderation and not from
  the flood rule. Do not reintroduce an administrator check in `on_media`.
- The captcha path still uses `is_admin`; that is a different, older rule and is
  unchanged.

### 4.8 Out of scope by default

Text/profanity/username/link moderation, raid detection, bans, a dashboard and
unrelated Telegram features do **not** exist. Do not add any of them unless the
current stage explicitly asks. The project advances one narrow stage at a time;
pre-building a future stage is a defect, not initiative.

### 4.9 Thresholds are calibrated evidence, not guesses

`EXPLICIT_DELETE_THRESHOLD=0.45`, `EXPLICIT_REVIEW_THRESHOLD=0.25`. NudeNet
320n is **not** a calibrated probability model: its own detection gate is 0.20
and its NMS threshold is 0.25, and confirmed explicit media from live testing
scored 0.50–0.67. A previous 0.80 delete threshold never fired and sent
everything to `REVIEW`.

The scene thresholds are a different kind of number. `SCENE_REVIEW_THRESHOLD`
(0.60) and `SCENE_DELETE_THRESHOLD` (0.95) are **conservative starting points,
not measured constants** — the scene classifier's accuracy on this bot's
traffic has not been measured against a labelled set. `SCENE_DELETE_THRESHOLD`
deletes media, so it is set high on purpose. Do not present it as calibrated,
and do not claim a detection-accuracy figure that was not measured.

Therefore: **do not change a threshold to make a test or a scenario pass.**
Thresholds are environment variables and are tuned from real traffic. If a
change genuinely requires a different threshold, say so explicitly in the
report and let the owner decide — do not bake a new number into the code.

---

## 5. Telegram engineering realities

The Bot API and Telegram's media model have hard limits. Design within them.

- **Handlers and filters.** The media handler is registered with a combined
  filter (photo, video, animation, video note, all stickers, image/video
  documents) **and** `filters.ChatType.GROUPS`. A new media type or a changed
  filter changes what is inspected. `chat_member` updates must be requested in
  `allowed_updates` or the captcha silently stops working.
- **Only bot owners are immune.** `WHITELIST_USER_IDS` short-circuits the media
  path. Telegram admins are deliberately **not** exempt. `is_admin` (with a
  300 s `_admin_cache`) still guards the captcha path; do not use it to skip
  media moderation again.
- **Media types.** Photo, GIF/animation, video, video note, static sticker,
  video sticker and image/video documents are analysed. Animated `.tgs`
  (Lottie) stickers cannot be decoded by ffmpeg and are analysed through their
  **static preview thumbnail** only — explicit content that appears only
  mid-animation can be missed. That is a known limit, documented in
  `README.md`; do not claim `.tgs` is fully analysed.
- **Download behaviour.** `get_file(...).download_to_drive(path)` into the
  per-job temp dir. Files larger than `MAX_DOWNLOAD_MB` (20 MB, the Bot API
  limit) fall back to the thumbnail; if there is no thumbnail the media is
  `SKIPPED` and logged, never guessed.
- **Deletion is the only action**, and it can fail (missing delete permission,
  message already gone, rate limit). Failure is handled in `moderation.enforce`
  and never escalated.
- **Async.** Handlers are `async`. CPU-bound work (ONNX inference, ffmpeg
  frames) must run off the event loop — `app/main.py` uses a
  `ThreadPoolExecutor` via `loop.run_in_executor`. Never call blocking
  detector/ffmpeg code directly on the event loop.
- **Rate limits and API failures.** `TelegramError` is the expected failure
  mode for every API call. Catch it narrowly where a call is optional (report,
  evidence upload, captcha delete) and let the media pipeline's outer
  fail-open handler cover the rest. Never let an API failure delete or punish.
- **`drop_pending_updates=True`** is deliberate: on restart the bot does not
  process a backlog of old messages.

---

## 6. Temporary media and disk

This is a small VPS. Disk leaks are production incidents.

- Every media job owns **one** temp directory created with
  `tempfile.mkdtemp(prefix=f"job_{chat.id}_{message_id}_", dir=config.TMP_DIR)`.
- That directory is removed in a `finally` block with
  `shutil.rmtree(work_dir, ignore_errors=True)` — on success, on detector
  error, on Telegram error, on cancellation, on any exception. **Keep the
  cleanup in `finally`.** If you add a new early `return`, it must still be
  inside the `try` that owns the `finally`.
- `detector.analyze_video` writes frames into the caller's `work_dir` and
  **never deletes them** — the caller uses one frame as evidence and then
  removes the whole directory. Keep that ownership rule: the detector does not
  own cleanup.
- The evidence frame is uploaded to the admin chat; GuardBot itself keeps **no
  permanent copy** on the VPS.
- If you add a new file, frame or cache, decide explicitly who deletes it and
  prove it is deleted on every path. The tests assert `TMP_DIR` is empty after
  each run — keep that property.

---

## 7. Concurrency and shared state

- `MEDIA_WORKERS` (default 2) media jobs may run at once via the
  `ThreadPoolExecutor`. Detector state (`detector._detector`) is loaded once at
  startup and read concurrently — treat it as read-only after `load_model()`.
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
  and the media pipeline keeps its outer fail-open `except Exception`.
- Logging is `logging` with the module logger (`guardbot`, `detector`,
  `decision`, `moderation`). `logging.basicConfig` is configured in
  `app/main.py`.
- The per-media decision line is the observability contract. Keep its fields
  and shape when you change the pipeline:

  ```
  media chat=... user=... kind=... detector=... frames=... decision=...
  source=... class=... confidence=... detections=... scene=... scene_frames=... reason=...
  ```

  `detections=` comes from `MediaAnalysis.detections_summary()` and must keep
  distinguishing `n/a` (analysis failed) from `none` (ran, no detections) from
  `CLASS:score,...`.
- The outcome lines are `DELETE_SUCCESS`, `DELETE_FAILED`, `media SKIPPED`,
  and for the new signals `FLOOD`, `FLOOD_RESTRICT`, `FLOOD_CLEARED`,
  `FLOOD_DELETE_FAILED`, `VIOLATION`, `VIOLATION_RESTRICT`, plus the test-account
  pair `TEST_UNRESTRICT_SCHEDULED` / `TEST_UNRESTRICT` and their failure lines
  `TEST_UNRESTRICT_FAILED` / `TEST_UNRESTRICT_NOTICE_KEPT`. Do not rename or
  remove them; operators grep them.
- `source=` is how an operator sees **which detector** caused the decision
  (`nudenet` / `scene` / `none`). `scene=` is the scene score (`-` when
  absent), `scene_frames=` how many frames the scene stage actually scored.
- **Never log media content, file bytes, tokens or the bot token.** Class names
  and scores are fine; the media is not.
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
- Do not change moderation behaviour, thresholds, detector classes or the
  decision table unless the stage is explicitly about them.
- Prefer the smallest diff that works. Three similar lines beat a premature
  helper.
- If you believe the requested change is wrong or unsafe, say so in the report
  and implement the safe version — do not silently expand the scope.

---

## 11. Testing

- Tests are `pytest` and they import the app, which imports
  `python-telegram-bot`, `Pillow`, `nudenet` and `transformers`. Run them in
  the image, not on a bare host:

  ```bash
  docker compose build
  docker run --rm -v "$PWD:/srv" -w /srv guardbot-guardbot \
    bash -lc "pip install -q pytest && python -m pytest tests -q"
  ```

- `tests/conftest.py` sets safe defaults (`BOT_TOKEN`, `GROUP_IDS`, in-memory
  `DB_PATH`, a temp `TMP_DIR`) so tests import the app without a real `.env`.
- `google-genai` is imported lazily inside `app/ai_intent.py`, so a test
  environment without it still runs the whole suite: the AI layer reports
  `sdk_missing` and the rules carry on. Never move that import to module scope —
  it would make an optional dependency mandatory at import time.
- The existing tests pin the safety contracts and must keep passing:
  - `tests/test_decision.py` — the policy table, fail-open, the scene bands,
    and that a borderline NudeNet hit with a safe scene score stays `REVIEW`.
  - `tests/test_detector.py` — parsing, fail-open on decode error, media rules.
  - `tests/test_moderation.py` — delete success/failure, no-punishment-on-failure.
  - `tests/test_media_pipeline.py` — the real `on_media` handler end to end with
    a fake Telegram layer and a stubbed detector: delete, no-delete, fail-open,
    `DELETE_FAILED` applies nothing, evidence fallbacks, temp cleanup.
  - `tests/test_burst.py` — the pure flood tracker: threshold, window, separate
    bursts, photo exclusion, per-user isolation, bounding.
  - `tests/test_flood_pipeline.py` — the flood rule through the handler:
    restrict, only-the-burst deletion, admin not exempt, owner exempt, fail-open.
  - `tests/test_violations.py` — the violation ladder: warn, count, restrict at
    the threshold, and everything that must not count.
  - `tests/test_test_user.py` — the test-account exception and the normal
    restriction duration: 15-minute default, the restrict still happening, the
    delayed unrestrict, the warning cleanup, the untouched admin report, and
    that other users get none of it.
  - `tests/test_scene_stage.py` — the graded scene stage: `EXPLICIT` at high
    confidence, `REVIEW` in the band, bounded frame sampling, fail-open, and
    that a scene-stage load failure cannot stop startup.
  - `tests/test_ai_intent.py` — the Gemini layer on its own: config on/off/no
    key, the structured contract (malformed, missing field, wrong type,
    invented category, clamped confidence), the prompt's own guarantees, the
    timeout, the retry policy, the rate window, the persisted daily cap, the
    circuit breaker, truncation, and that the key never reaches a log line.
  - `tests/test_classifier.py` — the policy between the two layers: a rule
    match is free, the model cannot overturn a match or a veto, ordinary
    chatter is never escalated, the model can promote and can decline, and
    every failure mode degrades to the rules.
  - `tests/test_acquisition.py` (extended) — the AI layer end to end through
    the real group handler: the model's yes becomes the usual invitation, its
    no leaves the group alone, a Gemini outage does not break the handler, and
    with no key the handler behaves exactly as it did before.
- **The AI tests never touch Google.** `app/ai_intent.py` has exactly one
  network seam, `_request`, and the tests replace it. If you add a code path
  that talks to the API outside `_request`, the tests will silently stop
  covering it — keep the seam.
- **Do not weaken or delete a test to make a change pass.** If a contract
  genuinely changes, update the contract text here and in `README.md` and the
  test in the same commit.
- A new regression test is only worth having if it **fails against the bug**.
  When you fix a defect, add the test that would have caught it and confirm it
  fails before the fix and passes after.
- When you add media types, thresholds or a new decision path, extend the
  pipeline tests — especially the temp-cleanup assertion (`TMP_DIR` empty after
  every run).

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
  docker compose logs -f | grep -E "decision=|DELETE_SUCCESS|DELETE_FAILED|SKIPPED"
  ```

- Remember the Dockerfile installs `ffmpeg`; any new binary dependency must be
  added there or frame extraction breaks only in production.
- Report what you actually ran. "Tests pass" and "image builds" are separate
  claims; if you did not run one of them, say so.

---

## 13. Group acquisition — the VPN bot handover

Someone asks in one of the moderated groups for a VPN. Instead of ignoring it or
answering with a link, the bot offers them a **personal way into the VPN bot**,
which is where a test actually gets provisioned.

The other half of this feature lives in the VPN bot
(`mo3iiibest77-hub/vpn-bot-private`, `/opt/vpn-bot/AGENTS.md` §13). Read that
side before changing this one; the wire protocol is the contract between them.

### 13.1 The boundary

| | GuardBot | VPN bot |
|---|---|---|
| Detects the intent | ✅ | |
| Signs requests | ✅ | ✅ verifies |
| Holds panel credentials | ❌ **never** | ✅ |
| Decides eligibility | ❌ | ✅ |
| Creates the client | ❌ | ✅ |
| Sends a configuration | ❌ **never** | ✅ |

This bot must never hold a VPN credential, never talk to the 3x-ui panel, and
never put a subscription URL, a UUID, a `pbk` or a panel client name in a group
message. `tests/test_acquisition.py` asserts all of that by reading the outgoing
`send_message` calls. The only thing a group ever sees is a friendly line and
one button.

Nothing is scraped: this bot does not read the VPN bot's Telegram messages. It
calls a signed HTTP endpoint and gets back a deep link or a refusal.

### 13.2 What counts as an intent

The rules are **data**, not code: `app/intent_rules.json`. Topic groups, support
groups, standalone phrases and `ignore` patterns, each a list of regexes with
weights. Adding a phrasing is a JSON edit. `INTENT_RULES_PATH` can point at a
different file to override them on a running deployment without a rebuild.

`app/intent.py` normalises the message *and* the rule patterns before matching —
Arabic yeh/kaf to Persian, alef variants unified, ZWNJ and bidi controls
stripped, harakat stripped, Persian and Arabic-Indic digits to ASCII, Arabic
punctuation to ASCII, whitespace collapsed. Without this, «ویپیان» typed with an
Arabic yeh never matches a Persian pattern, which is most of the traffic.

Scoring: a **topic** hit (weight 2) **and** at least one **support** hit
(request / problem / poor-internet, weight 1) makes an intent. A **standalone**
phrase (weight 3) is enough on its own. An **ignore** pattern is a hard veto.

Two deliberate consequences:

- **A bare mention of "VPN" is not an intent.** With `INTENT_REQUIRE_TOPIC=1`
  (the default), «اینترنتم ضعیفه» is not one either — half the group complains
  about slow internet, and offering all of them a test is noise. Both are
  asserted in `tests/test_intent.py`.
- **The `ignore` list vetoes competing sellers.** Someone advertising their own
  service must not be handed ours.

`INTENT_MIN_LENGTH` (4) exists because a three-character message cannot carry a
request; without it, stray short messages reach the matcher.

### 13.3 The cooldown, and why it is not in memory

`intent_offers(chat_id, user_id, last_offered, last_reason)` in `app/db.py`.
`INTENT_COOLDOWN_SECONDS` (3600) is checked per chat *and* user, and the record
is in SQLite because this container is restarted on every deploy — an
in-memory cooldown would reset and re-offer to the same person.

The VPN bot enforces the real limit (one test per account, §13.5). This cooldown
is only here so a chatty member cannot make the bot look like a spammer.

### 13.4 The handler

`on_group_text` is registered in `group=1` behind `acquisition_message_filter()`,
which is `TEXT & ~COMMAND & ChatType.GROUPS & ~EDITED_MESSAGE`. Edited updates
are not requested from Telegram at all, and the filter excludes them as well, so
editing a message into an intent cannot produce a second reply.

It returns early for bots, for `user.is_bot`, and for group admins — never offer
staff their own product. It only acts in `config.GROUP_IDS`, so the bot can
never advertise in a group the owner did not list.

Replies go through `_reply_in_group`, which retries without
`reply_to_message_id` when the original message is gone: a reply to a deleted
message is an error, and losing the invitation over that would be silly.

### 13.5 The VPN bot client

`app/vpnbot.py` mirrors the VPN bot's signing scheme rather than importing it —
the two projects run different frameworks on different Python versions and
cannot share code. Both repositories assert the **same fixed vector**
(`34bfe93c19f199b1e9d20199845cea664b0f0da554e0f16b170563d4f7176950`). If you
change the signing string, change it in both and update both tests, or
production breaks with a 401 that looks like a wrong secret.

`ERR_NOT_CONFIGURED` / `ERR_UNREACHABLE` / `ERR_BAD_RESPONSE` are this side's
failures; `ERR_REFUSED` carries the VPN bot's decision about the user
(`already_invited`, `in_progress`, `invalid`, `already_used`, `unavailable`),
each with its own reply. A refusal is a 200 with `ok: false` — a decision, not
an error — so "we already sent you a link" is distinguishable from "the VPN bot
is down". An unreachable VPN bot is silent by design: a group must not see the
infrastructure complaining.

### 13.6 Deployment, and the networking trap

```ini
GROUP_TRIAL_ENABLED=1
VPNBOT_API_URL=http://127.0.0.1:8099
VPNBOT_SHARED_SECRET=<must equal SERVICE_SHARED_SECRET in the VPN bot's .env>
```

**The container runs with `network_mode: host`.** The obvious alternative —
staying on a bridge network and using
`extra_hosts: host.docker.internal:host-gateway` — does not work on this host:
the gateway address *is* the host, so the packet lands on the host's `INPUT`
chain where ufw's default-deny drops it. It does not refuse, it **times out**,
which looks exactly like the VPN bot being down. Sharing the host's network
namespace makes the VPN bot reachable on `127.0.0.1:8099`, and the VPN bot binds
loopback to match. Nothing is published either way.

The coupling to remember: if this container goes back to bridge networking, the
VPN bot's `INTERNAL_API_HOST` must go back to `0.0.0.0` **and** the host firewall
must allow the Docker bridge range to reach 8099. Both sides, together.

`docker-compose.yml` carries the same note next to the setting.

### 13.7 Testing

| File | Covers |
|---|---|
| `tests/test_intent.py` | normalisation, a corpus of realistic Persian requests, the negative corpus, the bare-mention and connectivity-complaint guards, the competing-seller veto, the knobs, rule loading and extension |
| `tests/test_vpnbot_client.py` | the signing vector, header shape, nonce uniqueness, cross-path and body-tamper rejection, unconfigured refusal, unreachable transport |
| `tests/test_acquisition.py` | the group handler: the invitation is sent, **nothing else ever reaches the group**, unmatched messages stay silent, the cooldown across a restart, refusal copy, unreachable, unconfigured, deleted original, other groups, bots and admins, the command filter |

`tests/test_acquisition.py` exercises the real `main.acquisition_message_filter()`
rather than a copy of it, so a filter change cannot pass the tests while
changing production behaviour.

The full suite needs `nudenet` and `torch`; a light venv (`.venv-test/`,
gitignored) runs everything except the media stages, where one test fails for the
missing module. That failure is environmental — verify it is the *same* failure
before calling it unrelated.

### 13.8 The AI second opinion

The rules decide. This section is about the layer that exists because they
cannot decide *everything*, and about the constraints that keep it from becoming
the thing that decides.

**Why it exists.** `app/intent_rules.json` is a list of patterns someone wrote
down. A member can ask for a VPN in a sentence no pattern describes — «یه چیزی
میخوام که بشه باهاش رفت» — and the rules, correctly, stay silent. The layer
closes that gap. It is an addition to the rules, never a replacement: with it
switched off, on a keyless deployment, or with Google unreachable, the bot
behaves exactly as it did before the layer existed.

**The decision order.** `app/classifier.py` is the only place the two meet, and
its four steps are the design, not an optimisation:

1. **A veto is final.** An `ignore` match (a rival seller advertising) is
   decided and the model is never asked. A guard that a persuasive message can
   talk out of a veto is not a guard.
2. **A rule match is a decision.** It is acted on immediately. No call, no
   quota, no latency.
3. **No subject signal means ordinary.** Silence from the rules is only
   ambiguous when the message was *about* circumvention or connectivity.
   `intent.is_candidate(match)` is the gate; everything else stays silent and
   costs nothing.
4. **Only then, the model** — the genuinely uncertain middle.

**The candidate gate.** A message is a candidate when the rules found a
`topic` hit or one of the weight-0 patterns in the `ai_candidates` group of
`app/intent_rules.json` (`"candidate_group": "ai_candidates"`). Weight 0 is
deliberate: the group marks a message as *worth a second look* without moving
the score, so it cannot by itself produce an offer. Pricing words live there
because «قیمتتون چنده» and «قیمت گوشی چنده» are lexically identical and only
the model can tell them apart.

**The contract.** `app/ai_intent.py` asks one bounded, structured question:
`response_mime_type="application/json"` with a `response_json_schema`, and the
schema has fields for `is_relevant`, `intent_category`, `confidence`,
`needs_acquisition_offer`, `reason` and `signals` — and **no field for a
message**. The model is a classifier; it cannot address a user, and `reason` /
`signals` are for the log only. `parse_verdict` validates strictly: a blank
answer is `empty_response`, non-JSON is `malformed_json`, a non-object is
`malformed_shape`, a missing or wrongly-typed field is `malformed_missing` /
`malformed_type`, an unknown category is folded to `other`, and confidence is
clamped. Anything malformed is *unknown*, which is the same as "the rules'
silence stands".

**Failure is contained, in this order.** A timeout (`asyncio.wait_for`) or an
unexpected exception is caught; transient failures get one retry with backoff,
permanent ones (a `400`, an empty answer) do not; a rate window caps how often
we ask; a persisted daily cap stops us at the day boundary; and a run of
consecutive transport failures opens a circuit breaker for five minutes. The
daily counter is keyed to the API's own day — midnight **Pacific**
(`db.ai_day`, `_API_DAY_OFFSET = 8 * 3600`) — and it is pinned so that it can
never roll over *before* Google's does. In summer that means we are an hour
stricter than the API, which is the safe direction: the failure worth avoiding
is believing we have allowance the API still considers spent.
`classify()` never raises, and `app/main.py` never awaits it in a way that can
fail a handler.

**The deadline has a floor of 10 seconds, and you cannot go below it.** This
one cost a live debugging round: with `GEMINI_TIMEOUT_SECONDS=6` the transport
accepted the setting, the request went out, and Google answered *every* call
with

```
400 INVALID_ARGUMENT  Manually set deadline 6s is too short.
                      Minimum allowed deadline is 10s.
```

The layer reported itself `active` and classified nothing — silent, total, and
invisible to a suite that replaces `_request`, because the mock never validates
the deadline. So `ai_intent.MIN_DEADLINE_SECONDS` is 10.0 and
`timeout_seconds()` clamps to it rather than trusting the setting; the default
in `config.py` matches. `tests/test_ai_intent.py` asserts the floor, the clamp,
and that the value actually reaches `HttpOptions` — the last one is what fails
if someone removes the clamp. **A green unit suite is not evidence this
integration works; one real call is.**

Function calling is disabled explicitly (`AutomaticFunctionCallingConfig`). We
give the model no tools, so leaving it on only produces a warning on every
request and advertises a capability this integration never wants.

**What it can and cannot change.** It can promote a message the rules missed
and it can decline a candidate. It cannot overturn a rule match or a veto, and
it cannot change eligibility, provisioning or what is sent: the offer is the
same invitation from §13.1, sent to the same place, through the same VPN bot
endpoint. Nothing about the security model changes — the VPN bot still decides
who gets a test.

**Observability.** One `[intent]` line per decision (§8) with `source=` telling
you which layer decided. `ai_usage` in `app/db.py` keeps per-day counters for
`calls`, `relevant`, `irrelevant`, `malformed`, `errors` and `skipped` — and a
skip (a rate-limit, a circuit, a missing key) deliberately does **not** consume
the day's allowance, because we did not ask.

**Configuration.** The whole block is in `.env.example` under "the AI second
opinion". `GEMINI_ENABLED=false` or an empty `GEMINI_API_KEY` disables it
without touching anything else, and that is the supported way to take it out of
service.

**External limits you cannot code around.** The free tier's requests-per-minute
and requests-per-day are per project, are not guaranteed, and are only visible
in AI Studio — the numbers in `.env.example` are deliberately set *below* them.
Over quota is a `429 RESOURCE_EXHAUSTED`, which this layer treats as a transient
failure and then a skip. None of this can be verified from inside the bot, so
treat the counters as the real ceiling and watch `ai_usage`.

**The model default was chosen by measurement, and the first choice was wrong.**
The SDK documents `gemini-flash-latest` as the stable alias for the current
Flash model, so that is what this shipped with. Against this deployment's key it
answered **0 of 8** calls — `503 UNAVAILABLE ... currently experiencing high
demand`, `504 DEADLINE_EXCEEDED`, sustained over roughly 25 attempts. The layer
reported itself `active` and classified nothing, which is the same silent-total
failure shape as the deadline bug above.

`gemini-flash-lite-latest` answered **8 of 8** with no errors, and classified
every probe message correctly:

| Message | Verdict |
|---|---|
| «اینترنت ایرانسل وصل نمیشه» | `connectivity_problem`, not relevant, no offer |
| «سلام کسی میتونه کمک کنه یه وی پی ان خوب معرفی کنه؟» | `vpn_request`, **relevant, offer** |
| «سلام بچه ها، کی بازی دیشب رو دید؟» | `ordinary_conversation`, not relevant |
| «قیمتتون چنده؟» | `pricing_question`, not relevant |

So `GEMINI_MODEL` defaults to the lite alias. For judging one short message it is
also the better tool — faster, which matters inside a 10-second message-handler
budget, and cheaper against the daily quota. Availability is per key and moves,
so **measure it again rather than assuming**; the model is one env var and needs
no rebuild.

---

## 14. Git discipline

- Work on `main` (this repository has no long-lived feature branches). Keep the
  tree clean and commit only the files the change is about.
- Commit in **logical slices**, not one giant commit, and not unrelated changes
  bundled together.
- Conventional commits, with the area as scope where it helps:

  ```
  fix(media): delete the message after a confirmed explicit detection
  feat(media): add a conservative explicit-media moderation stage
  docs(agents): add the GuardBot agent workflow
  ```

  Types in use: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
- Before committing: `git status --short` and `git diff` — confirm **only** the
  intended files changed. Never commit `.env`, `data/` or a database.
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

## 17. Gotchas learned the hard way

1. **A threshold that looks safe can mean the feature never fires.** The first
   delete threshold was 0.80; confirmed explicit media scored 0.50–0.67, so
   every true positive landed in `REVIEW` and nothing was ever deleted. Watch
   the `decision=` lines on real traffic before concluding a stage works.
2. **Fail-open is a feature, not laziness.** `MediaAnalysis(ok=False)` → `SAFE`
   is the contract. Never let an exception path produce `EXPLICIT`.
3. **`REVIEW` must stay silent.** No admin message, no deletion, no punishment.
   It is a log-only state; adding a notification to it changes the product.
4. **The detector never owns cleanup.** Frames are written into the caller's
   `work_dir`; the handler removes the whole directory in `finally`.
5. **`.tgs` stickers are preview-only.** Do not claim animated stickers are
   fully analysed.
6. **The scene stage is a real, graded signal — not auxiliary plumbing.** When
   `SCENE_ENABLED` is on, `MediaAnalysis.scene_nsfw` fills it and a score at or
   above `SCENE_DELETE_THRESHOLD` deletes on its own (no NudeNet evidence
   needed). Below `SCENE_REVIEW_THRESHOLD` it is ignored. `None` means "no
   score" (disabled / failed) and must never be treated as `0.0` or as
   evidence. Turning the stage off or breaking it must leave the bot working.
7. **`db.add_strike` is now used for the violation ladder.** Do not add a second
   violation store, and remember it is only ever called after a *successful*
   deletion.
8. **Only bot owners (`WHITELIST_USER_IDS`) are immune.** Telegram admins are
   moderated like anyone else — do not add an admin bypass to `on_media`.
9. **A flood fires the moment the threshold is crossed, not after the window
   closes.** The first `BURST_MAX_ITEMS` messages are processed normally; only
   the burst's own messages are deleted, and the window is then cleared so the
   next message starts a fresh burst. Do not "fix" this into waiting for the
   window to expire.
10. **The scene classifier costs ~1.7 s per scored frame on a 2-core VPS.** The
    frame budget is `SCENE_MAX_FRAMES` (default 2), which is what bounds the
    stage's cost on video/GIF — it is deliberately independent of
    `VIDEO_FRAMES`. Raising it improves recall at ~1.7 s per extra frame; do
    not score every frame "just in case".
11. **The live `.env` still contains first-generation leftovers** (`MAX_STRIKES`,
    `NSFW_DELETE_THRESHOLD`, `NSFW_BAN_THRESHOLD`, `HIGH_CONF_ACTION`,
    `TRUST_AFTER_MESSAGES`, `TRUSTED_EXTRA_MARGIN`). Nothing reads them. The
    violation threshold is `VIOLATION_MUTE_AFTER` (default 3) precisely so the
    stale `MAX_STRIKES=5` cannot change the documented three-strike policy. Do
    not start reading the old names.
12. **The bot needs delete-message permission and privacy mode off.** If
    deletion silently fails, check the Telegram-side setup before the code.
13. **`drop_pending_updates=True` means restarts skip the backlog** by design —
    do not "fix" it into processing old messages.
14. **`data/` and `.env` are gitignored and must stay out of Git.** The model
    cache, the SQLite DB and the token never belong in a commit.
15. **A documentation change is not a code change.** Do not let a docs commit
    carry source edits, and do not let a code commit quietly rewrite the
    decision table.
16. **A container reaching a host service through the bridge gateway can hang
    instead of failing.** `host.docker.internal:host-gateway` points at the host
    itself, so the packet hits the host's `INPUT` chain, where ufw's default-deny
    drops it. The symptom is a *timeout*, which is indistinguishable from the
    service being down — do not spend an hour debugging the VPN bot. The
    acquisition flow uses `network_mode: host` and `127.0.0.1` for exactly this
    reason (§13.6). Test a new host dependency with a raw
    `socket.create_connection()` from inside the container before wiring it into
    application code.
17. **`host-gateway` resolves to the *default bridge* gateway, not the compose
    network's.** With `network_mode: host` this stops mattering; if you ever go
    back to a bridge network, remember that `getent hosts host.docker.internal`
    inside the container is the only way to know which address it picked.
18. **A fake that returns the shape you wish for hides real bugs.** The
    acquisition tests originally faked the panel with flat `total` / `up` /
    `down` keys; the real panel sends `totalGB` and nests the counters under
    `traffic`, so the sweep's exhaustion check passed while being unable to fire
    in production. When faking an external system, copy its *actual* response —
    see `/opt/vpn-bot/AGENTS.md` §5.6.
