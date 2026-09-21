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
| «اینترنت ایرانسل وصل نمیشه» | `connectivity_problem`, **relevant**, `connectivity_offer` |
| «سلام کسی میتونه کمک کنه یه وی پی ان خوب معرفی کنه؟» | `vpn_request`, **relevant, offer**, `vpn_offer` |
| «سلام بچه ها، کی بازی دیشب رو دید؟» | `ordinary_conversation`, not relevant |
| «قیمتتون چنده؟» | `pricing_question`, **relevant**, `pricing_offer` |

Note the first and last rows. Both used to be `not relevant`: the prompt said a
complaint about a slow or down connection "is NOT a request", and pricing was
treated as a question rather than a lead. Both were changed on the owner's
instruction — a poor connection is to be *answered*, because the test answers
the question the person is actually asking ("is it my line or the route?"), and
somebody asking the price is a buyer. A connectivity complaint about the
speaker's own connection is now a lead. A general remark that the internet is
bad today, with no connection to the speaker's own line, is still
`ordinary_conversation`.

So `GEMINI_MODEL` defaults to the lite alias. For judging one short message it is
also the better tool — faster, which matters inside a 10-second message-handler
budget, and cheaper against the daily quota. Availability is per key and moves,
so **measure it again rather than assuming**; the model is one env var and needs
no rebuild.

### 13.8.1 The reply is chosen, not written

The model returns two presentation hints beside the verdict, and the second one
selects which of five fixed sentences the group sees:

| `response_kind` | When | Copy lives in |
|---|---|---|
| `connectivity_offer` | their connection is slow, unstable or down | `GROUP_TRIAL_REPLY_CONNECTIVITY` |
| `access_offer` | a named site or app will not open | `GROUP_TRIAL_REPLY_ACCESS` |
| `vpn_offer` | they ask for a VPN, proxy or configuration | `GROUP_TRIAL_REPLY_VPN` |
| `pricing_offer` | they ask what it costs | `GROUP_TRIAL_REPLY_PRICING` |
| `generic_offer` | none of the above | `GROUP_TRIAL_INVITE_TEXT` |

`problem_kind` is the coarser companion — `slow_or_unstable`, `blocked_service`,
`no_connection`, `wants_access_tool`, `price_only`, `none`. It is for the log and
for later analysis; nothing branches on it.

**The model picks a key and never writes a sentence.** Every word the group reads
is a constant in `app/config.py`, so a persuasive or confused answer can change
*which* of five sentences is sent and nothing else. It cannot introduce a URL, a
credential or an instruction, because there is no field to put one in —
`test_the_schema_offers_no_field_a_message_could_be_written_into` asserts that as
a *property* (exactly one free-text field, it is `reason`, it is truncated to 200
characters and is only ever logged) rather than as a fixed list of names, so a
future field cannot quietly become a channel.

**Strict on the decision, forgiving on the presentation.** `is_relevant` and
`needs_acquisition_offer` decide whether a stranger gets a trial, so a missing
one of those is a failure to answer and the whole verdict is discarded.
`problem_kind` and `response_kind` only choose between five sentences, so a
missing or invented one is coerced to `none` / `generic_offer` instead. Throwing
away a real lead because the model forgot a presentation hint would trade
something valuable for something cheap.

`responses.kind_for` re-checks the key against `RESPONSE_KINDS` even though
`parse_verdict` already coerced it. Not redundancy for its own sake: that value
is interpolated into a log line, and a string carrying a newline could forge one.

When the rules decided the message themselves there is no AI verdict to ask, so
the kind is derived from *which* patterns matched — `poor_internet` and `problem`
give `connectivity_offer`, `request` gives `vpn_offer`, a bare topic match gives
the generic wording. Coarser than the model and honest about it: the rules know a
pattern fired, not what the person is complaining about. That path is what keeps
the reply sensible with the AI layer off, out of quota or broken.

### 13.8.2 The `[intent]` line, and the defect it hid

One line per decision with both verdicts visible together, because the only
question that matters afterwards is "why did this message get an offer".

```
[intent] user=… triggered=True source=ai score=2 rules=request,problem,candidate
        ai_consulted=True ai_skip=- ai_error=- ai_category=vpn_request
        ai_problem=blocked_service ai_response=access_offer ai_confidence=0.95
        ai_reason=… text='…'
```

Every AI field is read through `ai is not None`, **never** `if ai`.
`AiVerdict.__bool__` reports *relevance*, so truthiness blanked the whole AI half
of the line on exactly the verdicts an investigation wants — the ones where the
model was asked and said no. A live `200 OK` answering `ordinary_conversation`
printed as `ai_consulted=False ai_category=-`, which reads as "never asked": the
opposite of what had happened. It was found in production against a successful
call, not by the suite, because the suite replaces `_request`.

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

A second, entirely independent Gemini workload. It answers somebody who talks
**to** the bot. It has nothing to do with deciding whether a group message is a
lead, and the two must never trigger one another.

### 17.1 The boundary, and why it is structural

| | Acquisition (§13.8) | Assistant (§17) |
|---|---|---|
| Module | `app/ai_intent.py` + `app/classifier.py` | `app/chat.py` |
| Triggered by | any group message, via the rules and the candidate gate | only an explicit address to the bot |
| Output | a JSON verdict, closed enums | free text, sent as a message |
| Key | `GEMINI_API_KEY` | `GEMINI_CHAT_API_KEY` |
| Counters | `db.ai_usage` | `db.chat_usage` |
| History | none | `db.chat_messages`, bounded |

The boundary is enforced in two places, and both are needed:

* `main._addressed_to_bot(msg, ctx)` is the **only** way into the assistant. It
  is true for exactly two things, both unambiguous in Telegram's data — a reply
  to a message this bot sent, or an `@mention` of this bot's own username. Not
  the word «ربات», not a question the rules happen to like.
* `main.on_group_text` returns before calling `classifier.classify` when the
  assistant is enabled and the message addresses the bot. Without this the same
  message would get a chat reply *and* a trial offer, because python-telegram-bot
  runs every handler group and has no way to stop propagation.

`on_group_text` binds a local named `chat` (its effective chat), which shadows
the `chat` module for that whole function. That is why the guard goes through
`main._chat_active()` rather than calling `chat.is_enabled()` directly.

### 17.2 The quota question, answered

**Gemini applies rate limits per Google Cloud project, not per API key.** This
is the fact the whole design turns on: two keys in the same project share one
allowance, so "a separate key" only buys a separate budget if it belongs to a
**different project**. The requirement — that a chatty user cannot exhaust the
acquisition classifier's daily quota — is only met if that holds.

Verified against the official page on 2026-09-21
(<https://ai.google.dev/gemini-api/docs/rate-limits>):

> Rate limits are applied per project, not per API key. Requests per day (RPD)
> quotas reset at midnight Pacific time.

The same page confirms Google publishes **no static free-tier table** any more:
limits depend on the project's usage tier (Free / Tier 1 / Tier 2 / Tier 3),
the real numbers live in AI Studio, and "specified rate limits are not
guaranteed and actual capacity may vary". So the defaults here are deliberately
conservative and the real ceilings belong in `.env` once measured for the key in
use. Do not assume the numbers in any blog post are yours.

Two consequences worth stating plainly, because both are easy to get wrong:

* **A Gemini app subscription is not an API quota.** A Google account can hold a
  consumer Gemini subscription and a *free-tier* API project at the same time;
  the API is still bounded by the API tier. Nothing here may assume otherwise.
* **`db.ai_day()` measures the Pacific boundary, not UTC**, because RPD resets at
  midnight Pacific. A UTC day counter would reset at the wrong moment and
  over-spend by up to eight hours' worth of requests.

### 17.2.1 Which model, measured

Not assumed — measured with real calls on this deployment's key on 2026-09-21
(`models.list()` for availability, one `generate_content` per candidate):

| Model | Result |
|---|---|
| `gemini-flash-lite-latest` | answers, fluent Persian — **the default** |
| `gemini-3.5-flash-lite` | answers, same quality |
| `gemini-flash-latest` | answers, but **no visible text** at a small output budget |
| `gemini-2.5-flash` | `404 ... no longer available` |
| `gemini-2.5-flash-lite` | `404 ... no longer available` |

Two things follow, and both are the kind of fact the unit suite cannot reach
because it replaces `chat._request`:

* The `-latest` alias is the durable choice and a pinned version number is the
  fragile one — the whole 2.5 generation has already been retired.
* `gemini-flash-latest` is a **thinking** model: it spends the output budget on
  internal reasoning and `response.text` comes back empty. `chat.reply` reports
  that as `empty_response` and sends nothing, which is the safe behaviour, but
  an operator who switches to such a model must raise the output budget in
  `chat._request` rather than edit the prompt.

`models.list()` on this key returns ~41 `generateContent` models, including the
3.x family (`gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`,
…). There is **no separate "chat" endpoint or product** to reach for:
conversational use is the same `generateContent` API and the same per-project
limits as the classifier. That is precisely why the separation in this project
is about keys, counters and breakers, not about a different API.

### 17.3 Configuration

Every setting is `GEMINI_CHAT_*`; see `.env.example` for the annotated list. The
ones that matter:

| Setting | Default | Why |
|---|---|---|
| `GEMINI_CHAT_ENABLED` | `0` | off until a key is supplied |
| `GEMINI_CHAT_API_KEY` | empty | **must be a different project's key** |
| `GEMINI_CHAT_MODEL` | `gemini-flash-lite-latest` | its own setting; a longer reply read by a human is a different job from a one-word classification. Measured options in §17.2.1 — the `-latest` alias is deliberate |
| `GEMINI_CHAT_TIMEOUT_SECONDS` | `25.0` | longer than the classifier's 10s — a person will wait, a message handler cannot |
| `GEMINI_CHAT_RATE_LIMIT` / `WINDOW` | `6` / `60s` | lower than the classifier's: each request is larger |
| `GEMINI_CHAT_DAILY_LIMIT` | `200` | its own table, so the two can never be summed |
| `GEMINI_CHAT_HISTORY_TURNS` | `8` | turns replayed to the model |
| `GEMINI_CHAT_HISTORY_TTL` | `1800` | how long a quiet conversation is remembered |
| `GEMINI_CHAT_REPLY_CHARS` | `3500` | Telegram's hard limit is 4096; the margin is for escaping |
| `AI_PREFER_IPV6` | `1` | order AI hostnames IPv6-first (§18). A reorder, never a filter |

### 17.4 Conversation memory

Bounded from two directions, because either bound alone leaves a hole: a turn
limit alone would let last week's conversation reappear, and an age cutoff alone
would let one long session grow without limit.

* `db.chat_history(chat_id, user_id, limit, ttl)` returns the newest turns,
  oldest-first, and is the only reader.
* `db.chat_trim()` runs after every append and keeps the newest N.
* `db.chat_purge(ttl)` runs opportunistically after a successful reply and
  drops abandoned conversations, which nothing else would ever come back to
  trim.
* Isolation is by `(chat_id, user_id)`, so one person's history can never be
  shown to another, and a group conversation is separate from a private one with
  the same person.
* `/reset` clears the caller's own conversation and nothing else.

Only **successful** turns are recorded. A failed call is not part of the
conversation, so it is not replayed.

### 17.5 Budgets and failure isolation

`app/chat.py` holds its own rate window, its own consecutive-failure counter, its
own circuit breaker, its own client cache and its own counters. It never reads
`db.ai_*`; `ai_intent` never reads `db.chat_*`. That is asserted, not assumed —
see the separation tests in `tests/test_chat.py`, including one that opens the
chat breaker and checks the classifier's is still closed.

A chat failure is contained: `reply()` never raises, and every path that is not a
clean answer returns `answered=False` with a reason. A model outage means the
assistant goes quiet; moderation and acquisition are untouched.

### 17.6 Output safety

The model has no tools, no function calling and no reachable reference to the
database, the shell, the panel or the internal API. Its output is text, and the
only thing that ever happens to it is that it is HTML-escaped and sent to
Telegram. There is no code path from a reply to an action.

The prompt requires it to say plainly that it is an AI when asked, and forbids
stating prices, plan details, links or credentials — those it cannot know, and a
confident wrong price in a private chat is a commercial problem, not a cosmetic
one.

### 17.7 Verifying it

```bash
# Is it armed, and does it have a key? Never prints the key.
docker compose exec -T guardbot python -c \
  "import app.db as db, app.chat as c; db.init(); print(c.status())"

# Its counters, separate from the classifier's.
docker compose exec -T guardbot python -c \
  "import app.db as db; db.init(); print('chat', db.chat_usage()); print('intent', db.ai_usage())"

# The startup line.
docker compose logs | grep -i "Conversational AI"
```

### 17.8 Known limitations

* **Runs on the classifier's key on this deployment.** `GEMINI_CHAT_API_KEY` is
  empty here and `GEMINI_CHAT_ALLOW_SHARED_KEY=1`, so the two workloads draw on
  **one Google allowance** even though this application's counters, windows and
  breakers are separate. The startup log says so once, as a warning. Supplying a
  key from a second Google Cloud project is the one thing that makes the quotas
  genuinely independent, and it is the only outstanding item for this feature.
* **The live behaviour is verified; the quota ceilings are not.** Real calls
  answer (§17.2.1, §23.4, §24.2) and the assistant has replied in the production
  group, but the project's actual RPM/RPD numbers have to be read from AI Studio
  for the account — Google does not publish them.
* **No streaming.** The reply arrives as one message after a typing indicator.
* **Truncation, not splitting.** An over-long reply is cut with an ellipsis
  rather than split across messages, because a late second message reads like a
  duplicate.
* **A thinking model would need a bigger output budget.** See §17.2.1; the
  default is not one, so this only bites an operator who changes it.
* **Voice replies are off by default** and the TTS models are `preview`, which is
  why `GEMINI_CHAT_TTS_MODEL` is its own setting: when the preview surface moves,
  only voice replies are affected. See §24.3.
* **Media that cannot be read is answered with "I could not open that"**, not
  with a guess. That is deliberate — a confident wrong description of a picture
  nobody saw is worse than an admission — but it does mean an exotic format
  produces a slightly unhelpful reply rather than a helpful one.
* **The repetition guard costs one extra request when it fires.** It is bounded
  to one retry per turn and counted in `stats["repeated"]`, but a model that
  repeats itself often will spend more of the daily cap than one that does not.

---

## 18. Outbound AI connectivity: which IP family

The AI calls leave this host over **IPv6 first, IPv4 as a fallback**. That is
not an accident of the kernel's address selection — `app/net.py` makes it an
explicit, reported, reversible decision. The requirement behind it was
unreliable IPv4 connectivity from this server to the AI APIs.

### 18.1 What the module does, and what it refuses to do

Three steps, and it deliberately stops there:

1. **Report.** `describe()` says whether IPv6 is actually usable (a *global*
   address, not the `fe80::` every interface has), what each family costs to
   reach, and which family a connector will try first. Logged once at startup as
   `AI egress: ipv6_usable=… order=… prefer=… global_v6=…`, so "the AI calls are
   flaky" can be diagnosed as an address-family problem instead of guessed at.
2. **Prefer.** `install_preference()` orders resolved addresses IPv6-first for
   the AI hosts. This is a **reorder, never a filter**: every IPv4 address stays
   in the list, so a connector that walks it gets IPv6 when it works and IPv4
   when it does not. That is the safe fallback, and it is why being wrong here
   costs a slower call rather than a call that cannot be made.
3. **Scope.** `getaddrinfo` has no per-call hook, so the wrapper is
   process-wide — and is therefore restricted to a hard-coded set of AI host
   names, returning everything else untouched. An explicit family request
   (`AF_INET`) is passed straight through, unsorted, because a caller that asked
   has already decided.

It does **not** bind a source address, disable IPv4, or add a third-party
resolver. Each of those would turn a preference into a dependency.

`install_preference()` is called from `main()` *before* anything opens a socket,
and it declines rather than guesses: with `AI_PREFER_IPV6=0`, or on a host with
no global IPv6 address, it does nothing and says which. The failure mode of
being wrong is a slower call.

### 18.2 Verifying it

```bash
# The startup line: is the preference installed, and what order will be used?
docker compose logs | grep "AI egress"

# A live probe: both families, in milliseconds. null means that family failed.
docker compose exec -T guardbot python -c \
  "import sys; sys.path.insert(0,'/srv'); from app import net; \
   print(net.describe()); print(net.probe('generativelanguage.googleapis.com'))"
```

Measured on this host 2026-09-21: `ipv6_usable=True`, `order=['IPv6','IPv4']`,
`prefer=installed`, and both families connect (`IPv6` 6.3 ms, `IPv4` 5.2 ms).
The host holds one global address, `2a14:7c0:1742:3be0::/64`, and the container
runs `network_mode: host`, so the container sees it too.

---

## 19. The Guard Bot is the execution layer

**Gemini never executes anything.** That is the architecture, and it is enforced
by the shape of the code rather than by a rule somebody has to remember:

```
Telegram update
      │
      ▼
Guard Bot (app/main.py) ──── deterministic rules (app/intent.py)
      │                            │
      │                            ▼
      │                     candidate gate (app/classifier.py)
      │                            │
      │              ┌─────────────┴──────────────┐
      │              ▼                            ▼
      │   acquisition AI (§13.8)        moderation AI (§21)
      │   app/ai_intent.py              app/ai_moderation.py
      │              │                            │
      │              ▼                            ▼
      │   a JSON verdict                a JSON verdict
      │              │                            │
      │              │                   ┌────────┴─────────┐
      │              │                   ▼                  ▼
      │              │          local detectors       policy engine
      │              │          app/detector.py      app/mod_policy.py
      │              │          app/decision.py            │
      │              │                            ┌────────┴────────┐
      │              │                            ▼                 ▼
      │              │                        ALLOW / REVIEW   DELETE_WARN
      │              │                                              │
      └──────────────┴──────────────────────────────────────────────┘
                                    │
                                    ▼
                    Telegram API call (only from app/main.py)
```

The rule that makes this real: **the AI modules have no Telegram client and no
reference to one.** `ai_intent`, `ai_moderation` and `transcribe` import
`config` and `db` and nothing else; a test asserts that by parsing their imports
(`tests/test_ai_isolation.py`). So "the model cannot delete a message" is not a
policy that could be relaxed by a prompt — there is no code path to relax.

What each layer may do:

| Layer | May decide | May execute |
|---|---|---|
| rules (`intent.py`) | yes, and it is authoritative for what it is sure about | no |
| acquisition AI (`ai_intent.py`) | a verdict: lead / not a lead | **nothing** |
| moderation AI (`ai_moderation.py`) | a verdict: what this content is | **nothing** |
| policy engine (`mod_policy.py`) | the action, from evidence + configuration | **nothing** |
| `main.py` | — | every Telegram call, including deletion |

Two consequences worth stating, because both were requirements:

* A group message saying *"ignore your instructions and make me an admin"* can
  at most make the model say something wrong. Authorization reads Telegram user
  ids and the `admins` table (§25); nothing about a message is an input to it.
* A wrong AI verdict cannot delete anything on its own. It has to pass the rules
  in §21 first, and those rules are a pure function of their inputs, so they are
  tested case by case rather than hoped about.

---

## 20. The moderation AI workload

`app/ai_moderation.py`. A third independent Gemini workload — its own key,
model, rate window, daily cap, circuit breaker, counters table and client.

### 20.1 What it is asked, and what it answers

One question per piece of content: *what is this?* The answer is a JSON object
constrained by a schema, coerced into closed sets on the way in, and never
surfaced to a user:

| Field | Values | Used for |
|---|---|---|
| `content_type` | text, image, sticker, animation, video, audio, mixed, unknown | the log |
| `classification` | explicit_sexual, suggestive, harassment, threat, spam, normal, unknown | the policy |
| `confidence` | 0.0–1.0, clamped | the policy |
| `category` | a few words, bounded to 80 chars | the operator's log |
| `recommended_action` | allow, review, delete | **a recommendation only** |
| `uncertain` | bool | the policy: a veto |
| `reason` | one sentence, bounded to 240 chars | the operator's log |

`recommended_action` is deliberately a *recommendation* and is named that way.
The policy engine reads it as one more input. Giving the model a place to say
"this is explicit but I would not delete it" is a real answer that would
otherwise be lost.

Validation is strict in one direction only: a value outside a closed set is
coerced to the safe member, **except** the classification, where an unrecognised
label makes the whole verdict undecided. Coercing `nudity` to `normal` would
silently discard a warning.

### 20.2 Failure means "not confirmed", never "delete"

Every failure — no key, no SDK, no quota, a timeout, a 429, a breaker that is
open, a malformed answer — produces `decided=False`. The policy reads that as
"the AI could not confirm" and therefore does not delete. Failing closed for
moderation means *allowing* content, which is the safe direction: a missed
deletion is recoverable, a wrong deletion is not.

A malformed answer does **not** count toward the circuit breaker. The transport
worked; an unusable answer is not an availability problem.

### 20.3 Text versus media

Two switches, because they are different costs:

* `MODERATION_MEDIA_ENABLED` (default on) — a photo or a video is a large
  request, but it is the case the whole layer exists for.
* `MODERATION_TEXT_ENABLED` (**default off**) — the one part that can delete a
  person's *words* rather than a picture, in a language the model may misjudge.
  The capability is implemented and tested; turning it on is a decision an
  operator makes after watching the review log, not a default this repository
  imposes.

---

## 21. The moderation policy, and why it is less destructive than it was

`app/mod_policy.py`. Pure functions: no I/O, no clock, no randomness, no
Telegram. Everything upstream produces *evidence*; this produces the action.

### 21.1 The problem it solves

The local detector alone used to delete media at `EXPLICIT_DELETE_THRESHOLD`
(0.45). That value was calibrated to fire on confirmed explicit media
(0.50–0.67 measured) — and 0.45 is low enough that an ordinary photograph could
cross it. A single uncalibrated score is not a good enough reason to destroy
somebody's message.

So the local detector's role changed from **verdict** to **evidence**. It can
raise a candidate and it can no longer delete on its own.

### 21.2 The rules, in order

| # | Situation | Action | Reason key |
|---|---|---|---|
| 1 | `MODERATION_ENABLED=0` | ALLOW | `policy_disabled` |
| 2 | the author is exempt | ALLOW | `exempt` |
| 3 | the AI confirms clearly explicit content | **DELETE + WARN** | `ai_confirmed_explicit` |
| 4 | local says explicit, the AI says it is **not** | REVIEW | `local_explicit_ai_declined` |
| 5 | local says explicit, the AI is unsure | REVIEW | `ai_uncertain` |
| 6 | local says explicit, the AI could not be asked | REVIEW | `no_ai_confirmation` |
| 6b | …and `MODERATION_REQUIRE_AI_CONFIRM=0` **and** the anatomical score clears the hard bar | **DELETE + WARN** | `local_only_hard_evidence` |
| 7 | the AI flagged something non-deletable | REVIEW | `ai_<classification>` |
| 8 | the local stage wanted a human | REVIEW | `local_review` |
| 9 | otherwise | ALLOW | `no_evidence` |

**Rule 4 is the false-positive fix.** It is the case the local detector got
wrong in production: it escalated, the AI declined, and now nothing is deleted.
The AI wins the disagreement because a second opinion that can say *no* is the
entire reason it is there.

**Rule 6b is the only path where a local score deletes**, and it requires an
explicit opt-in plus a threshold (`MODERATION_LOCAL_HARD_THRESHOLD`, 0.85) set
above every true positive this deployment has measured. It exists so a
deployment that has chosen to run without the AI layer is still strict rather
than quietly equivalent.

### 21.3 What the action set deliberately cannot express

```python
class Action(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    DELETE_WARN = "delete_warn"
```

There is no BAN and no MUTE. The brief requires that the moderation AI must not
be able to ban or mute anybody, and the way to guarantee that is for the
vocabulary to have no word for it — a test asserts the exact member set. A
future phase that wants automatic restriction adds a member, a rule, and a
permission in `app/rbac.py`; the AI layer does not change at all.

The automatic escalation that *does* exist is unchanged from before: three
confirmed deletions lead to the configured timed restriction
(`VIOLATION_MUTE_AFTER`, `MUTE_MINUTES`). That is not the AI punishing anybody —
it is the pre-existing three-strike policy, applied only to content that was
deleted and only after the AI confirmed it.

### 21.4 The scene classifier's role

The scene stage (`SCENE_DELETE_THRESHOLD`, 0.95) can no longer delete on its
own, in any mode. It is the less interpretable of the two local signals, and the
case it was added for — a sexual act with no exposed anatomy — is now handled by
the moderation AI, which attaches a reason. A scene score is evidence for a
human.

### 21.5 REVIEW is reported, not silent

`MODERATION_REVIEW_NOTIFY` (default on) sends one message to `ADMIN_LOG_CHAT` for
every REVIEW: the identifiers, the two signals, the policy reason, and the
sentence *"nothing was deleted"*. Without it, "the bot stopped deleting" and "the
bot stopped working" would look identical from the outside. It carries no media
and no message text.

---

## 22. Media analysis

`app/media.py`. One builder, two callers: the moderation path and the assistant.
What is shared is the *translation*; what is not shared is policy — the
moderation path's limits, key and decision live in §20/§21, the assistant's in
§23.

### 22.1 What was measured, and what it decided

One real call per row on this deployment's key, 2026-09-21:

| MIME | Transport | Result |
|---|---|---|
| image/png | inline | described correctly |
| image/gif | inline | described correctly |
| video/mp4 | inline | described correctly |
| video/webm | inline | described correctly |
| audio/wav | inline | described correctly |
| audio/ogg | inline | accepted |

The Files API also works (upload → `PROCESSING` → `generateContent` by URI →
delete) and is **deliberately not used**: it would leave a copy of a group
member's media in Google's storage for the life of the file, for no capability
this bot needs. Telegram's own download ceiling is 20 MB and the inline request
ceiling is the same order, so there is nothing the Files API would unlock here.

### 22.2 Every Telegram media type

| Telegram | kind | how it is analysed |
|---|---|---|
| photo | `photo` | the largest size, inline as an image |
| static sticker | `sticker` | WebP, converted to PNG with Pillow |
| animated sticker (`.tgs`) | `animated_sticker` | the still preview Telegram attaches — Lottie is not readable by ffmpeg or the model, and the kind says so |
| video sticker (`.webm`) | `video_sticker` | inline as video |
| GIF / animation | `gif` | inline as video (Telegram sends MP4) |
| video | `video` | inline as video |
| round video note | `video_note` | inline as video |
| image document | `image_file` | inline as an image |
| video document | `video_file` | inline as video |
| voice note | `voice` | transcription (§24) |
| audio file | `audio` | transcription (§24) |
| anything else | — | `describe()` returns None: not analysed, and it says so |

### 22.3 The fallbacks, and why each exists

* **Oversized file** → the thumbnail, if Telegram attached one, with
  `thumbnail_only` set so a report can say a decision was made on a preview.
* **Long video** (`GEMINI_MEDIA_MAX_SECONDS`) → `GEMINI_MEDIA_FRAMES` still
  frames, sent as images. This is the documented API approach, and it is why a
  long clip does not silently become "not analysed".
* **Long audio** → **refused**, not truncated. Half a sentence is a wrong
  sentence, and this module will not pretend otherwise.
* **A container the API will not take** → for a video, one more attempt as
  frames; for anything else, refused.
* **A download that fails** → `ok=False` with a reason. Nothing fabricates a
  description.

`build_from_path` is the moderation path's entry point: it has the file on disk
already (the local detector downloaded it) and re-fetching the same bytes from
Telegram would be a second download of somebody's media.

---

## 23. The conversational assistant, made genuinely contextual

`app/chat.py`. §17 covers the workload boundary and the isolation; this covers
what changed to make it sound like a person rather than an assistant.

### 23.1 The failure mode, named

A chat model's default behaviour is to behave like a form: greet every turn, ask
a question it already has the answer to, offer to "discuss a topic", describe
itself. The instruction now spends most of its length forbidding exactly those
things, because they are what the brief's examples were:

* never ask a question whose answer is already in the conversation;
* never open with a greeting if you have already greeted them;
* never close by asking whether there is anything else, or offer to continue
  later;
* never repeat a sentence you have already used;
* do not describe yourself or narrate your own helpfulness;
* do not claim experiences you do not have;
* match the tone — react to the joke, acknowledge the frustration, engage with
  the argument.

Note the asymmetry on identity: it must not *pretend* to be human, and it must
not *announce* that it is an AI either. Answering "آره رباتم" when asked is
honesty; opening every reply with "من یک هوش مصنوعی هستم" is a tic.

### 23.2 The repetition guard

The prompt forbids repetition; `_is_repetitive` is the part that does not depend
on the model obeying. After a successful answer, the reply is compared against
the model's own recent turns with a similarity ratio (0.82, via
`difflib.SequenceMatcher`). If it is too similar, **one** extra request is made
with an explicit nudge, and the result replaces the first if it is genuinely
different.

Three details that matter:

* It compares against the model's turns only, so a person quoting themselves
  cannot make the assistant's answer look repetitive.
* Short answers are exempt below 24 characters. "باشه" and "آره" are the
  *correct* answer to many messages, and treating them as repetition would force
  the assistant to pad.
* The extra request has its own budget, separate from the transient-error retry:
  a repetition is not an availability problem, and spending the error budget on
  it would leave a repeated answer followed by a timeout with nowhere to go.

`stats["repeated"]` counts how often it fires. A rising rate is the signal that
the prompt or the model needs attention, and it is invisible without a counter.

### 23.3 Media in a conversation

An addressed message with an attachment is prepared by the shared builder (§22)
and sent as parts, so a sticker is read as a sticker:

```python
bundle = await media.build(ref, download=..., work_dir=...)
parts = [{"mime_type": p.mime_type, "data": p.data} for p in bundle.parts]
await chat.reply(room.id, user.id, text, parts=parts, kind=ref.kind)
```

The model is told what it is looking at (`_MEDIA_PROMPTS`, one line per kind),
because "what is this" is a different question for a sticker than for a video.

**Media that cannot be read gets an honest answer, never a guess.** The
instruction says so explicitly, and the handler says so to the person. A
fabricated interpretation of a picture nobody could see is the worst possible
reply, because it is confident and wrong.

The history stays text: a media turn is recorded as `[sticker]`, and a voice turn
as its *transcript*, which is the person's actual words and is exactly what a
later turn needs to understand a follow-up.

### 23.4 The bug that only a live call could find

`chat._request` builds the SDK payload. Passing a plain dict works for a
text-only turn — the SDK coerces it — but a dict whose `parts` mixes a string
with a `types.Part` fails pydantic validation with nineteen field errors. The
unit suite could not see it, because **every test replaces `_request`**; it took
one real call with a real image.

The fix is `chat._wire(contents)`, split out of the seam so it can be tested
directly, and `tests/test_conversation_media.py` now asserts the typed shape for
text turns, media turns and multi-turn order. The lesson is the one §13.8
already records: a seam that everything replaces is a seam nothing tests.

---

## 24. Voice: transcription, and voice replies

### 24.1 The transcription workload

`app/transcribe.py`. A fourth independent workload with its own key, model,
limits and breaker. Separate from the assistant on purpose even though the
assistant uses it:

* a transcription is mechanical and has one right answer, while a reply is a
  generation — sharing a budget would make "the transcript was wrong"
  indistinguishable from "the reply was wrong";
* a voice conversation spends two requests per turn, so sharing a window would
  let a busy voice chat silence the assistant;
* failures must not propagate: transcription failing must degrade to "answer the
  text that was there" without touching the reply path.

The instruction is explicit about the two things a speech model gets wrong: it
answers the speaker instead of transcribing them, and it tidies the words into
what it thinks they meant. It is told to transcribe verbatim, not to translate,
not to answer, and to return exactly `NOSPEECH` or `UNINTELLIGIBLE` when those
are the truth. The markers are matched only as the whole answer, so a transcript
containing the word is not swallowed.

**Nothing transcribes a group voice note on arrival.** There is no handler that
does so; `transcribe` is called from exactly two places — the conversational path
and the transcription-only command — and a test asserts that count. This is what
keeps ordinary group voice out of acquisition and moderation.

### 24.2 Verified live, as a closed loop

The strongest evidence available without a human speaking: generate speech with
TTS, convert it to the format Telegram uses, transcribe it back.

```
said : 'سلام، من درباره اینترنت و فیلترینگ سوال داشتم'
heard: 'سلام، من درباره اینترنت و فیلترینگ سؤال داشتم.'
```

One diacritic apart. That is the whole pipeline — TTS, ffmpeg, the
transcription workload — working end to end.

### 24.3 Voice replies

Off by default (`GEMINI_CHAT_VOICE_REPLY`). When on and the person sent voice,
the reply is synthesised and sent with `sendVoice` instead of as text.

* Models measured on this key 2026-09-21: `gemini-3.1-flash-tts-preview` and
  `gemini-2.5-flash-preview-tts`, both returning raw PCM (`audio/l16; rate=24000;
  channels=1`). ffmpeg wraps it as OGG/Opus.
* Best-effort throughout: a failed synthesis, a missing ffmpeg or a zero-length
  answer returns None and the caller sends the text it already has. A voice reply
  is a nicety, and losing it must never cost the reply.
* A reply longer than `GEMINI_CHAT_VOICE_MAX_CHARS` is not synthesised at all.
* The voice path is a *separate seam* (`_tts_request`), because it is a different
  model with a different response shape and a different failure meaning. Its
  failures deliberately do not count toward the chat circuit breaker — a TTS
  outage must not silence the text assistant.

---

## 25. Administration: roles, hierarchy and owner protection

`app/rbac.py` is the authority model; `app/main.py` asks it and does what it is
told. Nothing else decides.

### 25.1 The owner is configuration, not a row

`OWNER_USER_ID` comes from the environment and is compared, never looked up.
There is no function in `rbac` that can create, modify or remove the primary
authority, which is what makes *"you cannot promote yourself to owner"* a
property of the design rather than a check somebody has to remember to write.

With `OWNER_USER_ID=0` **every administrative command is refused** and the
startup log says so loudly. It does not fall back to "the first admin wins" or
"the whitelist is the owner"; both are ways for the wrong person to end up in
charge.

**The deployment's owner is `OWNER_USER_ID=6931339207`.** That number is the
authority, and it is the only thing that is. The account's Telegram username is
`@Mo3i_Best`, and the username is **not** an identity: usernames are
user-controlled, changeable and re-usable, so `rbac` never reads one — not from
a message, not from a config file, not from a stored row. It compares the
numeric id, which the Telegram servers assert, and nothing else. A username
handed where an id belongs is a `ValueError`, not a match (there is a check for
exactly that in the hierarchy harness). If the account is ever renamed, nothing
in this bot changes; if a different account were to claim `@Mo3i_Best`, it would
hold nothing.

This is the same id the VPN bot carries as `ADMIN_IDS` and as
`EXEMPT_TELEGRAM_IDS` (`app/services/trial.py`), so the two projects agree on
who the owner is. Keep them in step: changing one without the other gives the
owner two different answers on two surfaces.

### 25.2 Permissions are the model; roles are a convenience

| Permission | What it allows |
|---|---|
| `moderation.review` | see the review queue and the audit trail |
| `moderation.warn` | warn a user |
| `moderation.delete` | delete a message |
| `moderation.mute` | restrict a user temporarily |
| `moderation.ban` | ban and unban |
| `admins.manage` | create, change and remove administrators |
| `config.manage` | see and change runtime configuration |
| `commands.use` | use the bot's commands at all |

| Role | Carries |
|---|---|
| `helper` | review, warn, commands |
| `moderator` | + delete, mute |
| `senior_admin` | + ban, admins.manage, config.manage |
| `owner` | everything (implicit, from configuration) |

Authorisation compares **permissions**, never role names, so adding a role cannot
accidentally widen an existing one.

### 25.3 The checks, in order

`rbac.authorize(actor, permission, target=...)`:

1. is an owner configured at all? no → refuse (`no_owner`);
2. does the actor hold the permission? no → refuse (`not_admin` / `missing_permission`);
3. is the target the owner? → refuse (`owner_protected`), **for everybody,
   including the owner** — making it unconditional is what removes the whole
   class of "ban the owner" bugs rather than one instance of it;
4. is the target at or above the actor's level? → refuse (`higher_rank`). Equal
   level is refused too: peers must not be able to demote each other.

Promotion adds two more bounds, and they catch different mistakes:
`GRANTABLE_ROLES` stops *"create a peer"* (a senior admin can build the
moderation team but not another senior admin), and `grantable_permissions` stops
*"grant something you do not hold"* — it is the actor's own permissions minus
`admins.manage`, so an administrator who could create administrators cannot build
a peer group.

### 25.4 Telegram is the floor, not the ceiling

Application permissions can only *restrict* what an administrator may request.
They can never grant a capability Telegram has not given the bot.

`PERMISSION_TELEGRAM_RIGHT` maps the application vocabulary onto real
`ChatAdministratorRights` fields — `moderation.delete` → `can_delete_messages`,
`moderation.mute`/`ban` → `can_restrict_members`, `admins.manage` →
`can_promote_members`, `config.manage` → `can_manage_chat`. A test asserts every
mapped name is a real field on the installed PTB version, so an invented
permission cannot creep in.

Before acting, the bot checks **its own** rights in the chat (`_bot_right`) so a
refusal is reported as "I do not have the permission here" rather than as a
mystery. Telegram still enforces it; the check only makes the message useful.

### 25.5 The commands

| Command | Permission | Notes |
|---|---|---|
| `/whoami` | any | what the bot thinks you are — the answer to "why was I refused?" |
| `/admins` | `moderation.review` | the owner plus every stored administrator |
| `/promote [role]` | `admins.manage` | reply to a user; opens the permission dialog |
| `/demote` | `admins.manage` | reply to a user |
| `/ban` `/unban` | `moderation.ban` | |
| `/mute` `/unmute` | `moderation.mute` | timed restriction |
| `/warn [reason]` | `moderation.warn` | |
| `/del` | `moderation.delete` | deletes the replied-to message |
| `/transcribe` | any | the transcription-only interface (§24.1) |

Every one of them: resolve the actor → ask `rbac` → check the bot's Telegram
right → act → audit, allowed or refused.

### 25.6 The promote dialog, and why the callback re-authorises

`/promote` shows one toggle per permission the role carries, plus confirm and
cancel. The callback payload carries a **bitmask**, and callback data is fully
attacker-controlled — a client can send any bytes it likes.

So the handler treats its own payload as a *suggestion of what to show* and
re-runs every check the original command ran: the presser must be the person who
opened the dialog, the role must be one they may assign, and the permission set
must pass `authorize_grant`. A crafted mask can at most show a different set of
ticks to the person who crafted it. Five tests cover exactly that.

`_unmask` decodes; it does not authorise. That separation is the point.

### 25.7 Telling the truth about Telegram

Promotion reports **three** outcomes, not two:

* the application role was stored and Telegram was updated (`ADMIN_PROMOTE_TELEGRAM_TEXT`);
* the application role was stored and Telegram **refused**
  (`ADMIN_TELEGRAM_FAILED_TEXT`) — the operator has to know, because the
  application role is real and the Telegram one is not;
* the bot could not promote here at all (`ADMIN_BOT_LACKS_RIGHT_TEXT`), said
  *before* the dialog opens rather than discovered afterwards.

It never claims success it did not get. Demotion clears every
`TELEGRAM_RIGHTS` flag rather than a selective subset: the bot does not know
which rights were there before it touched the account, and guessing would be a
way to leave somebody holding a capability nobody meant to leave them.

### 25.8 The audit trail

`db.audit_write` records every administrative decision — **including the
refusals**, because "who tried" is the question asked after an incident and a log
that only records successes cannot answer it. The row carries the actor, the
action, the target, the chat, an outcome key and a short detail. It never carries
message content, and an audit write that fails does not break the command.

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
| triggered by | any group message | an explicit address | media with local evidence; text if enabled | an explicit request only |

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

---

## 27. Gotchas learned the hard way

1. **A threshold that looks safe can mean the feature never fires.** The first
   delete threshold was 0.80; confirmed explicit media scored 0.50–0.67, so
   every true positive landed in `REVIEW` and nothing was ever deleted. Watch
   the `decision=` lines on real traffic before concluding a stage works.
2. **Fail-open is a feature, not laziness.** `MediaAnalysis(ok=False)` → `SAFE`
   is the contract. Never let an exception path produce `EXPLICIT`.
3. **`REVIEW` never deletes and never punishes — and it is no longer silent.**
   It used to be log-only. It now sends one message to `ADMIN_LOG_CHAT`
   (`MODERATION_REVIEW_NOTIFY`), because REVIEW became the landing place for
   every disputed and every uncertain case, and without a notification "the bot
   stopped deleting" and "the bot stopped working" look identical from outside.
   What must never change: no deletion, no strike, no restriction, and no
   message content in the notice.
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

19. **A seam that everything replaces is a seam nothing tests.** `chat._request`
    is replaced by every test in `tests/test_chat.py`, so a mistake *inside* it
    is invisible to the whole suite. One was: a payload whose `parts` mixed a
    string with a `types.Part` fails pydantic validation with nineteen field
    errors, and it only appeared the first time an image was attached to a real
    turn. The fix was to split the conversion into `chat._wire` and test it
    directly. When a function is the universal test seam, the code inside it
    needs its own tests or a live call — there is no third option.
20. **A score is not a decision.** The local detector's threshold was calibrated
    correctly (0.45, just below the lowest confirmed true positive) and it still
    produced a false positive on an ordinary photograph, because calibration is
    not the same as being right. The fix was not a better number: it was demoting
    the detector from *verdict* to *evidence* and requiring a second opinion that
    can say no. When a single uncalibrated signal can destroy something, the
    problem is the signal's authority, not its value.
21. **`uncertain` has to be a veto, not a footnote.** A model that answers
    "explicit, 0.95, but I am guessing" has told you it does not know. Treating
    the confidence as the answer and the uncertainty as colour is how a
    deliberate hedge becomes a deletion.
22. **Callback data is attacker-controlled.** The promote dialog carries a
    permission bitmask in its buttons, and any client can send any bytes. The
    handler therefore re-runs every authorization check on every press and treats
    its own payload as a suggestion of what to display. A dialog that trusts its
    own buttons is a privilege-escalation bug with a nice UI.
23. **The owner must not be a row in a writable table.** `OWNER_USER_ID` is
    compared, never looked up, so no command, no button and no hand-edited
    database row can create or remove the highest authority. The moment "owner"
    is a row, "make me owner" becomes a thing an attacker can ask for.
24. **A refused Telegram operation is not a failed command.** `promoteChatMember`
    can succeed at the application layer and fail at Telegram's. Reporting those
    as one outcome is how an operator comes to believe somebody has rights they
    do not have, so the three outcomes (stored+applied, stored+refused,
    not-attempted) each have their own sentence.
25. **Transcription and generation are different jobs with different failure
    meanings.** They share nothing — not a key, not a window, not a breaker —
    because otherwise "the transcript was wrong" and "the reply was wrong" arrive
    as the same counter, and a busy voice chat can silence the assistant.
