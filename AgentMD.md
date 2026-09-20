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
  `onnxruntime` + `opencv-python-headless`). Tests need
  `requirements-dev.txt` (adds `pytest`).
- **Entry point:** `python -m app.main` (`app/main.py:main`). It creates the
  DB dir and temp dir, calls `db.init()`, loads the detector if
  `MEDIA_ENABLED`, builds the `Application`, registers handlers, then
  `run_polling(allowed_updates=[MESSAGE, CALLBACK_QUERY, CHAT_MEMBER],
  drop_pending_updates=True)`.
- **Modules and their single jobs:**

  | File | Responsibility |
  |---|---|
  | `app/config.py` | every setting, from environment variables |
  | `app/db.py` | SQLite: `users` (strikes) and `captchas` |
  | `app/detector.py` | raw detections only; NudeNet + ffmpeg frame sampling |
  | `app/decision.py` | the policy: `MediaAnalysis` → `SAFE`/`REVIEW`/`EXPLICIT` |
  | `app/moderation.py` | executing a decision (delete); no Telegram import |
  | `app/main.py` | Telegram wiring: captcha handlers + the media pipeline |

  The detector/policy split is deliberate: `detector.py` produces raw
  detections, `decision.py` owns *which* classes and *which* confidence count
  as explicit. Keep them separate — a future stage must be able to extend one
  without touching the other.

- **Two features only:** captcha for new members, and conservative
  explicit-media moderation. Nothing else exists, on purpose.
- **Deployment:** `Dockerfile` (`python:3.12-slim` + `ffmpeg`, `CMD python -m
  app.main`) and `docker-compose.yml` (service `guardbot`, `restart: always`,
  `env_file: .env`, `./data:/data`, 2 GB memory limit). The VPS runs it from
  `~/guardbot` with `docker compose up -d --build`.
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
| `REVIEW` | an explicit class below the delete threshold, or ambiguous | allow, log only — **never** delete, **never** notify |
| `EXPLICIT` | an explicit body-region class at ≥ `EXPLICIT_DELETE_THRESHOLD` | delete the Telegram message |

- Only classes listed in `EXPLICIT_CLASSES` can ever produce `EXPLICIT`.
- `REVIEW` is a log-only state. It sends no admin message and applies no
  punishment. This is intentional, not an omission.
- A **generic** NSFW score may only ever raise `REVIEW`, never `EXPLICIT`.
  Note for accuracy: `MediaAnalysis.generic_nsfw` is currently never set by any
  detector, so the generic branch in `decision.py` is dormant plumbing. Do not
  describe it as an active classifier, and do not wire one in without an
  explicit request.

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

- `app/moderation.py:enforce` is the only place that executes an action.
  `EXPLICIT` → attempt delete. Delete succeeded → `deleted=True`. Delete
  raised → `delete_failed`, **no strike, no ban, no notification**.
- `DELETE_SUCCESS` and `DELETE_FAILED` are the two log outcomes. Only
  `DELETE_SUCCESS` reaches the admin report.

### 4.4 Admin reporting

- The admin chat (`ADMIN_LOG_CHAT`) receives a message **only** for
  `EXPLICIT` + `DELETE_SUCCESS`. `SAFE`, `REVIEW`, `DELETE_FAILED` and every
  operational error are container-log only.
- The report carries media type, user, user id, username, chat id, message id,
  detected class, confidence, a reason line and a UTC timestamp, plus a
  representative evidence frame (`MediaAnalysis.evidence_frame` — the frame
  with the highest score for the matched class). The reason line is currently a
  fixed explanatory sentence, not the engine's `result.reason` string; if you
  change that, keep the report readable for a non-technical admin.
- Evidence upload falls back `send_photo` → `send_document` → text-only
  `send_message`. **The report itself must never be lost** because an upload
  failed. Keep that fallback order.
- The report text is Persian and HTML-parse-mode. If you touch it, keep the
  same register and the same fields; do not machine-translate or restructure it.

### 4.5 No punishment

There is **no member punishment in the media path**: no strike, mute, ban,
kick or restrict. The only automatic action is deleting the message.
`db.add_strike` exists but is **intentionally unused** — member punishment is
deferred to its own future task. Do not start it, do not wire it into the media
path, and do not delete it either. (The captcha path is separate and does
kick on timeout; that is existing captcha behaviour, not a media-punishment
hook.)

### 4.6 Thresholds are calibrated evidence, not guesses

`EXPLICIT_DELETE_THRESHOLD=0.45`, `EXPLICIT_REVIEW_THRESHOLD=0.25`. NudeNet
320n is **not** a calibrated probability model: its own detection gate is 0.20
and its NMS threshold is 0.25, and confirmed explicit media from live testing
scored 0.50–0.67. A previous 0.80 delete threshold never fired and sent
everything to `REVIEW`.

Therefore: **do not change a threshold to make a test or a scenario pass.**
Thresholds are environment variables and are tuned from real traffic. If a
change genuinely requires a different threshold, say so explicitly in the
report and let the owner decide — do not bake a new number into the code.

### 4.7 Out of scope by default

Text/profanity/username/link moderation, raid detection, member punishment,
a dashboard and additional detectors do **not** exist. Do not add any of them
unless the current stage explicitly asks. The project advances one narrow stage
at a time; pre-building a future stage is a defect, not initiative.

---

## 5. Telegram engineering realities

The Bot API and Telegram's media model have hard limits. Design within them.

- **Handlers and filters.** The media handler is registered with a combined
  filter (photo, video, animation, video note, all stickers, image/video
  documents) **and** `filters.ChatType.GROUPS`. A new media type or a changed
  filter changes what is inspected. `chat_member` updates must be requested in
  `allowed_updates` or the captcha silently stops working.
- **Admins and the whitelist are immune.** `is_admin` (with a 300 s
  `_admin_cache`) and `WHITELIST_USER_IDS` short-circuit the media path. Do not
  remove or reorder that check.
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
  class=... confidence=... detections=... generic=... reason=...
  ```

  `detections=` comes from `MediaAnalysis.detections_summary()` and must keep
  distinguishing `n/a` (analysis failed) from `none` (ran, no detections) from
  `CLASS:score,...`.
- The outcome lines are `DELETE_SUCCESS`, `DELETE_FAILED`, `media SKIPPED`.
  Do not rename or remove them; operators grep them.
- **Never log media content, file bytes, tokens or the bot token.** Class names
  and scores are fine; the media is not.
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

- Tests are `pytest`, run with:

  ```bash
  pip install -r requirements-dev.txt
  python -m pytest tests -q
  ```

- `tests/conftest.py` sets safe defaults (`BOT_TOKEN`, `GROUP_IDS`, in-memory
  `DB_PATH`, a temp `TMP_DIR`) so tests import the app without a real `.env`.
- The existing tests pin the safety contracts and must keep passing:
  - `tests/test_decision.py` — the policy table, fail-open, generic-never-explicit.
  - `tests/test_detector.py` — parsing, fail-open on decode error, media rules.
  - `tests/test_moderation.py` — delete success/failure, no-punishment rules.
  - `tests/test_media_pipeline.py` — the real `on_media` handler end to end with
    a fake Telegram layer and a stubbed detector: delete, no-delete, fail-open,
    `DELETE_FAILED` applies nothing, evidence fallbacks, temp cleanup.
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

## 13. Git discipline

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

## 14. Documentation updates

- If a change alters the decision table, the admin-report rule, a known limit,
  a setting or the architecture, update `README.md` **and** the relevant
  section of this file in the same commit. `ChatGPT.md`'s current-state section
  is maintained by the strategy agent, not by you — but if you know the state
  it records is now wrong, say so in your report.
- Keep documentation honest. Do not describe a class, a behaviour or a
  capability the code does not have. If a limit exists, document it as a limit.

---

## 15. Final implementation report

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

## 16. Gotchas learned the hard way

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
6. **`generic_nsfw` is always `None` today.** The generic branch in
   `decision.py` is dormant plumbing, not an active classifier.
7. **`db.add_strike` is intentionally unused.** No punishment exists in the
   media path; member punishment is a separate future task.
8. **Admins and whitelisted users are immune.** Keep that check in front of the
   media pipeline.
9. **The bot needs delete-message permission and privacy mode off.** If
   deletion silently fails, check the Telegram-side setup before the code.
10. **`drop_pending_updates=True` means restarts skip the backlog** by design —
    do not "fix" it into processing old messages.
11. **`data/` and `.env` are gitignored and must stay out of Git.** The model
    cache, the SQLite DB and the token never belong in a commit.
12. **A documentation change is not a code change.** Do not let a docs commit
    carry source edits, and do not let a code commit quietly rewrite the
    decision table.
