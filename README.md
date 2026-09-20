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
  -> media normalization (download, ffmpeg frame extraction for video/GIF)
  -> explicit-content detector (NudeNet, explicit body-region classes)
  -> decision engine (SAFE / REVIEW / EXPLICIT)
  -> SAFE     -> allow
     REVIEW   -> allow + log (never delete, never punish)
     EXPLICIT -> delete media
```

### Detector

[NudeNet](https://github.com/notAI-tech/NudeNet) - a small YOLOv8-based ONNX
model (320px, CPU-only) that reports explicit **body-region** classes, e.g.
`FEMALE_GENITALIA_EXPOSED`, `MALE_GENITALIA_EXPOSED`, `ANUS_EXPOSED`.

For video/GIF/animated video stickers several frames are sampled and the
strongest detection per class is kept, so explicit content that only appears in
one frame is still caught.

### Decision policy

| Decision | Meaning | Action |
|---|---|---|
| `SAFE` | normal / non-explicit | allow |
| `REVIEW` | ambiguous / borderline / uncertain | allow + log |
| `EXPLICIT` | explicit body-region class at >= `EXPLICIT_DELETE_THRESHOLD` | delete media |

* Only classes in `EXPLICIT_CLASSES` can ever produce `EXPLICIT`.
* A generic NSFW score can only raise `REVIEW`, never `EXPLICIT`.
* Any detector or decoding error fails open (`SAFE`) - uncertainty never
  deletes anything.
* Swimsuit, underwear, cleavage, gym, dancing, memes, cartoons and normal
  stickers/GIFs are **not** explicit evidence and are allowed.

### What it does NOT do

* No text/profanity/username/link moderation.
* No raid detection.
* No automatic ban or mute: the only automatic action is deleting the media.
* A strike is recorded **only** after a deletion that actually succeeded; a
  failed deletion logs the failure and applies no strike/ban.

## Test in a private test group first

Set `GROUP_IDS` to a test group, send normal photos / videos / stickers and
check that nothing gets deleted. Watch the decisions:

```bash
docker compose logs -f | grep "decision="
```

Tune `EXPLICIT_DELETE_THRESHOLD` / `EXPLICIT_CLASSES` after watching real
traffic.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

## Known limits

* Media is visible for 1-3 seconds before it is deleted.
* Files > 20 MB: only the thumbnail is checked (Bot API limit).
* Animated `.tgs` (Lottie) stickers cannot be decoded and are skipped.
* The detector cannot determine age, so no member-level punishment is ever
  applied automatically in this stage - only the media is removed.
