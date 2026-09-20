# GuardBot

Captcha for new members + conservative explicit-media moderation on every
photo / video / GIF / sticker. Everything runs on your server. No media leaves
it.

The media layer is deliberately **narrow**: it removes clearly explicit adult
genital imagery and allows everything else. It is not a general NSFW,
profanity, text or behaviour moderation system.

## Deploy

```bash
tar xzf guardbot.tar.gz && cd guardbot
cp .env.example .env && nano .env     # BOT_TOKEN, GROUP_IDS, ADMIN_LOG_CHAT
docker compose up -d --build
docker compose logs -f
```

The detector model (~12 MB) ships inside the `nudenet` wheel, so there is no
large download on first start. Wait for `Detector ready.`

## Before you start (Telegram side)

1. BotFather: Group Privacy -> **Turn off**
2. Make the bot admin with: Delete Messages, Ban Users (Restrict Members)
3. Captcha needs `chat_member` updates, which only work if the bot is an **admin**
4. Turn OFF "Approve New Members" (join requests) if you want the captcha
   to do the gatekeeping automatically, otherwise both run

## How media moderation works

```
Telegram media
  -> per-job temp dir (download, ffmpeg frame extraction for video/GIF)
  -> explicit-content detector (NudeNet, explicit body-region classes)
  -> decision engine (SAFE / REVIEW / EXPLICIT)
  -> SAFE     -> allow (log only)
     REVIEW   -> allow (log only)
     EXPLICIT -> delete the Telegram message
                 -> DELETE_SUCCESS: report + evidence frame to admin chat
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

### Decision policy

| Decision | Meaning | Action |
|---|---|---|
| `SAFE` | normal / non-explicit | allow |
| `REVIEW` | explicit class detected below the delete threshold | allow + log |
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

### Admin chat

The admin chat receives a message **only** for `EXPLICIT` + `DELETE_SUCCESS`.
`SAFE`, `REVIEW`, `DELETE_FAILED` and operational errors are logged to the
container log only. The report contains media type, user, user id, username,
chat id, message id, detected class, confidence, reason and timestamp, plus a
representative frame as evidence (photo, with document and text-only fallbacks
if Telegram rejects the upload).

### What it does NOT do

* No text/profanity/username/link moderation.
* No raid detection.
* **No member punishment at all** - no strike, mute, ban, kick or restrict. The
  only automatic action is deleting the message.
* A failed deletion logs `DELETE_FAILED` and applies nothing.

### Media types

| Type | Analysed |
|---|---|
| Photo | yes |
| GIF / animation | yes (ffmpeg frames) |
| Video | yes (ffmpeg frames) |
| Video note | yes (ffmpeg frames) |
| Static sticker (.webp) | yes |
| Video sticker (.webm) | yes (ffmpeg frames) |
| Animated sticker (.tgs) | static preview thumbnail only |
| Image / video document | yes |

## Test in a private test group first

Set `GROUP_IDS` to a test group, send normal photos / videos / stickers and
check that nothing gets deleted. Watch the decisions:

```bash
docker compose logs -f | grep "decision="
docker compose logs -f | grep -E "DELETE_SUCCESS|DELETE_FAILED|SKIPPED"
```

Tune `EXPLICIT_DELETE_THRESHOLD` / `EXPLICIT_CLASSES` after watching real
traffic (both are environment variables, so no rebuild is needed).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

## Known limits

* Media is visible for 1-3 seconds before it is deleted.
* Files > 20 MB: only the thumbnail is checked (Bot API limit).
* Animated `.tgs` (Lottie) stickers are analysed through their static preview
  thumbnail, so explicit content that only appears mid-animation can be missed.
* Evidence frames are uploaded to the admin chat, which means Telegram stores
  them there. GuardBot itself never keeps a permanent copy on the VPS.
* The detector cannot determine age, so no member-level punishment is ever
  applied automatically - only the message is removed.

