# GuardBot

Captcha for new members + real-time NSFW check on every photo/video/GIF/sticker.
Everything runs on your server. No image leaves it.

## Deploy

```bash
# on the Netherlands server
tar xzf guardbot.tar.gz && cd guardbot
cp .env.example .env && nano .env     # BOT_TOKEN, GROUP_IDS, ADMIN_LOG_CHAT
docker compose up -d --build
docker compose logs -f
```

First start downloads the model (~350MB) into ./data. Wait for "Model ready."

## Before you start (Telegram side)

1. BotFather: Group Privacy -> **Turn off**
2. Make the bot admin with: Delete Messages, Ban Users (Restrict Members)
3. Captcha needs `chat_member` updates, which only work if the bot is an **admin**
4. Turn OFF "Approve New Members" (join requests) if you want the captcha
   to do the gatekeeping automatically, otherwise both run

## Test in a private test group first

Set GROUP_IDS to a test group, send a few normal photos + videos, check that
nothing gets deleted. Then look at the score logged for each media:

    docker compose logs -f | grep "media chat"

Tune NSFW_DELETE_THRESHOLD after you see real scores.

## What it does

| Event | Action |
|---|---|
| Someone joins | muted + button. No click in 120s -> kicked (can rejoin) |
| Photo/video/GIF/sticker/video-note | downloaded, scored, deleted if >= threshold |
| score >= 0.90 or 2nd strike | also banned (or muted, HIGH_CONF_ACTION=mute) |
| Any error in the pipeline | message is NOT deleted, error goes to admin log |
| Admins / whitelist | never checked |
| 30+ clean messages | user is "trusted": thresholds +0.10 |

## Known limits

- Media is visible for 1-3 seconds before it is deleted.
- Files > 20MB: only the thumbnail is checked (Bot API limit).
- Animated stickers (.tgs) are not analyzed.
- Model errors on borderline art/cartoons happen: watch the admin log first days.
