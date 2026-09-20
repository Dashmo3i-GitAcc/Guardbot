# GuardBot

Captcha for new members + conservative explicit-media moderation on every
photo / video / GIF / sticker, plus an instant media-flood rule. Everything
runs on your server. No media leaves it.

The media layer is deliberately **narrow**: it removes clearly explicit adult
genital imagery and allows everything else. It is not a general NSFW,
profanity, text or behaviour moderation system.

Three independent signals, which are never conflated:

* **explicit sexual content** - the detector + decision engine below;
* **instant media flood** - more than `BURST_MAX_ITEMS` GIFs/stickers inside
  `BURST_WINDOW_SECONDS`;
* **repeated violations** - the warn / count / restrict ladder.

## Deploy

```bash
tar xzf guardbot.tar.gz && cd guardbot
cp .env.example .env && nano .env     # BOT_TOKEN, GROUP_IDS, ADMIN_LOG_CHAT
docker compose up -d --build
docker compose logs -f
```

The NudeNet model (~12 MB) ships inside the `nudenet` wheel. The optional
second-stage scene classifier (~340 MB) is downloaded once on first start into
`./data/hf` and then reused. Wait for `Detector ready.` and, if enabled,
`Scene classifier ready.`

## Before you start (Telegram side)

1. BotFather: Group Privacy -> **Turn off**
2. Make the bot admin with: Delete Messages, Ban Users (Restrict Members)
3. Captcha needs `chat_member` updates, which only work if the bot is an **admin**
4. Turn OFF "Approve New Members" (join requests) if you want the captcha
   to do the gatekeeping automatically, otherwise both run

## How media moderation works

```
Telegram media
  -> instant-flood check (GIF/sticker kinds, from metadata, no download)
       -> burst -> restrict sender + delete the burst's messages + warn
  -> per-job temp dir (download, ffmpeg frame extraction for video/GIF)
  -> explicit-content detector (NudeNet, explicit body-region classes)
  -> second-stage scene classifier (optional, one frame, REVIEW-only)
  -> decision engine (SAFE / REVIEW / EXPLICIT)
  -> SAFE     -> allow (log only)
     REVIEW   -> allow (log only)
     EXPLICIT -> delete the Telegram message
                 -> DELETE_SUCCESS: report + evidence frame to admin chat
                                    + one violation: warn, count, restrict at N
                 -> DELETE_FAILED : log only, nothing else happens
  -> temp dir removed (always, in a finally block)
```

### Detector

[NudeNet](https://github.com/notAI-tech/NudeNet) - a small YOLOv8-based ONNX
model (320px, CPU-only) that reports explicit **body-region** classes, e.g.
`FEMALE_GENITALIA_EXPOSED`, `MALE_GENITALIA_EXPOSED`, `ANUS_EXPOSED`.

For video/GIF/animated video stickers several frames are sampled and the
strongest detection per class is kept, so explicit content that only appears in
one frame is still caught. Per-frame results are retained so the frame the
decision was based on can be attached to the admin report.

### Second-stage scene classifier (optional)

NudeNet sees explicit *body regions* only. It cannot see a sexual act when no
genitalia are visible, so an optional local scene-level NSFW classifier scores
the media on top of it and its score is used **only** to raise `REVIEW`.

* It can **never** produce `EXPLICIT`, so it cannot cause a deletion by itself.
* One frame is scored per media item, not one per frame.
* It fails open: a missing model, a missing dependency or an inference error
  simply leaves the score absent.
* Measured cost on a 2-core VPS is roughly 1-2 s per media item.
* Disable it with `GENERIC_NSFW_ENABLED=false`.

### Decision policy

| Decision | Meaning | Action |
|---|---|---|
| `SAFE` | normal / non-explicit | allow |
| `REVIEW` | explicit class detected below the delete threshold, or a high scene score with no body-region evidence | allow + log |
| `EXPLICIT` | explicit body-region class at >= `EXPLICIT_DELETE_THRESHOLD` | delete message |

* Only classes in `EXPLICIT_CLASSES` can ever produce `EXPLICIT`.
* A generic NSFW score can only raise `REVIEW`, never `EXPLICIT`.
* Any detector or decoding error fails open (`SAFE`) - uncertainty never
  deletes anything.
* Swimsuit, underwear, cleavage, gym, dancing, memes, cartoons and normal
  stickers/GIFs are **not** explicit evidence and are allowed.
* Thresholds are calibrated for NudeNet 320n, whose own detection gate is 0.20
  and NMS threshold 0.25. Confirmed explicit media from live testing scored
  0.50-0.67, which is why the default delete threshold is `0.45`.

### Instant media flood

A user who sends **more than `BURST_MAX_ITEMS`** qualifying media messages
inside **`BURST_WINDOW_SECONDS`** is flooding. Defaults: more than 5 within 3
seconds.

* Only `BURST_MEDIA_KINDS` count (`gif`, `sticker`, `animated_sticker`,
  `video_sticker`, `video_note`). **Ordinary photos are never counted**, so
  sending several photos quickly is not a flood.
* The decision is made from message metadata - a flood costs no download, no
  ffmpeg and no inference.
* The rule applies to everyone except bot owners (`WHITELIST_USER_IDS`).
  Telegram admins are **not** exempt.
* On a confirmed flood: restrict the sender, delete **only** the messages that
  belong to that burst, and warn. Unrelated older messages from the same user
  are never touched.
* If Telegram refuses the restriction (for example the target is an
  administrator), nothing is claimed as done: the failure is logged and
  reported to the admin chat.

### Repeated violations

One confirmed explicit-media deletion is one violation, recorded in the
existing `users.strikes` column.

* Every violation sends the user a warning.
* At `VIOLATION_MUTE_AFTER` (default 3) the user is restricted for
  `MUTE_HOURS` (default 24). Telegram lifts a timed restriction itself.
* A **failed deletion is never a violation** - it logs `DELETE_FAILED` and
  applies nothing.

### Admin chat

The admin chat receives a message **only** for `EXPLICIT` + `DELETE_SUCCESS`,
plus a notice when a confirmed flood could not be restricted. `SAFE`, `REVIEW`,
`DELETE_FAILED` and operational errors are logged to the container log only.
The report contains media type, user, user id, username, chat id, message id,
detected class, confidence, reason and timestamp, plus a representative frame
as evidence (photo, with document and text-only fallbacks if Telegram rejects
the upload).

Every report also carries a `🗑 حذف گزارش` inline button that deletes the
**report message itself** (the moderated message is already gone). Any
**current member** of the admin chat may use it — Telegram administrator status
is not required, and it is not limited to the owner. A user who has left or was
removed cannot use it, and a button press coming from any other chat deletes
nothing. If the report was already deleted, the press is answered and ignored.

### What it does NOT do

* No text/profanity/username/link moderation.
* No raid detection.
* No ban and no permanent punishment: the only member action is a **timed**
  restriction.
* A failed deletion logs `DELETE_FAILED` and applies nothing.

### Media types

| Type | Analysed | Counts toward a flood |
|---|---|---|
| Photo | yes | no |
| GIF / animation | yes (ffmpeg frames) | yes |
| Video | yes (ffmpeg frames) | no |
| Video note | yes (ffmpeg frames) | yes |
| Static sticker (.webp) | yes | yes |
| Video sticker (.webm) | yes (ffmpeg frames) | yes |
| Animated sticker (.tgs) | static preview thumbnail only | yes |
| Image / video document | yes | no |

## Test in a private test group first

Set `GROUP_IDS` to a test group, send normal photos / videos / stickers and
check that nothing gets deleted. Watch the decisions:

```bash
docker compose logs -f | grep "decision="
docker compose logs -f | grep -E "DELETE_SUCCESS|DELETE_FAILED|SKIPPED"
docker compose logs -f | grep -E "FLOOD|VIOLATION"
```

Tune `EXPLICIT_DELETE_THRESHOLD` / `EXPLICIT_CLASSES` after watching real
traffic, and `BURST_MAX_ITEMS` / `BURST_WINDOW_SECONDS` after watching real
flood behaviour (all are environment variables, so no rebuild is needed).

## Tests

The tests need the same environment as the bot (Telegram, Pillow, NudeNet,
transformers), so run them in the image:

```bash
docker compose build
docker run --rm -v "$PWD:/srv" -w /srv guardbot-guardbot \
  bash -lc "pip install -q pytest && python -m pytest tests -q"
```

## Known limits

* Media is visible for 1-3 seconds before it is deleted.
* Files > 20 MB: only the thumbnail is checked (Bot API limit).
* Animated `.tgs` (Lottie) stickers are analysed through their static preview
  thumbnail, so explicit content that only appears mid-animation can be missed.
* Evidence frames are uploaded to the admin chat, which means Telegram stores
  them there. GuardBot itself never keeps a permanent copy on the VPS.
* The detector cannot determine age, so no age-based action is ever taken; the
  only member action is a timed restriction.
* The scene classifier is REVIEW-only, so a sexual act with no visible
  genitalia is logged but is not deleted.
* A flood is only detected when the threshold is crossed, so the messages sent
  before it are processed normally and only the burst's own messages are
  removed.
* Telegram does not let a bot restrict a chat administrator, so a flooding
  admin cannot be stopped; that refusal is logged and reported, never claimed
  as a success.

