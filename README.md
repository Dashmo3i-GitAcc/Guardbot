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

The NudeNet model (~12 MB) ships inside the `nudenet` wheel. The second-stage
scene classifier (~330 MB) is downloaded once on first start into `./data/hf`
and then reused. Wait for `Detector ready.` and, if enabled,
`Scene classifier ready.`

## Before you start (Telegram side)

1. Make the bot admin with: Delete Messages, Ban Users (Restrict Members)
2. Captcha needs `chat_member` updates, which only work if the bot is an **admin**
3. Turn OFF "Approve New Members" (join requests) if you want the captcha
   to do the gatekeeping automatically, otherwise both run
4. BotFather: Group Privacy may be left **on**. A bot that is an
   **administrator** receives every group message either way, and that is what
   lets the assistant notice an administrator's message without being replied
   to. A bot that is only a *member* receives commands, replies and mentions and
   nothing else — the assistant then works, but cannot observe. The startup log
   and `/nexus status` say which situation applies to each group.

## How media moderation works

```
Telegram media
  -> instant-flood check (GIF/sticker kinds, from metadata, no download)
       -> burst -> restrict sender + delete the burst's messages + warn
  -> per-job temp dir (download, ffmpeg frame extraction for video/GIF)
  -> explicit-content detector (NudeNet, explicit body-region classes)
  -> second-stage scene classifier (local, scene-level NSFW score)
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

### Second-stage scene classifier

NudeNet sees explicit *body regions* only. It cannot see a sexual act when no
genitalia are visible - intimate/sexual interaction, an erotic scene, or an
explicit scene where the anatomical class is simply missed. A local,
image-level NSFW classifier
([Falconsai/nsfw_image_detection](https://huggingface.co/Falconsai/nsfw_image_detection),
a 224px ViT, CPU-only, ~330 MB) scores the scene on top of NudeNet so the
overall sexual nature of the media is recognised, not just body parts.

Its score is graded, so only clearly sexual media deletes:

| Scene score | Decision |
|---|---|
| `< SCENE_REVIEW_THRESHOLD` (0.60) | `SAFE` |
| `>= SCENE_REVIEW_THRESHOLD` | `REVIEW` - logged only, never deletes |
| `>= SCENE_DELETE_THRESHOLD` (0.95) | `EXPLICIT` - deletes the message |

* A scene score at or above the delete threshold deletes **even when NudeNet
  found nothing**. That is the point of the stage.
* The delete threshold is deliberately high; the stage is not meant to turn
  mildly suggestive media into deletions.
* For video/GIF it scores up to `SCENE_MAX_FRAMES` (default 2) of the frames
  that were **already** sampled for NudeNet - no extra ffmpeg work - and keeps
  the highest score, because the sexual nature of a clip can be visible in a
  single frame.
* It fails open: a missing model, a missing dependency, an undecodable frame or
  an inference error leaves the score absent, and an absent score never deletes.
* Measured cost on this 2-core VPS: ~1.7 s per scored frame (the model is
  loaded once at startup), so ~1.7 s per photo and ~3.4 s per video at the
  default frame budget.
* Disable it with `SCENE_ENABLED=false`.

### Decision policy

| Decision | Meaning | Action |
|---|---|---|
| `SAFE` | normal / non-explicit | allow |
| `REVIEW` | explicit class below the delete threshold, or a scene score in the review band | allow + log |
| `EXPLICIT` | an `EXPLICIT_CLASSES` detection at >= `EXPLICIT_DELETE_THRESHOLD`, **or** a scene score at >= `SCENE_DELETE_THRESHOLD` | delete message |

* Only classes in `EXPLICIT_CLASSES` can produce `EXPLICIT` from NudeNet.
* The scene stage can produce `EXPLICIT` on its own, but only at or above
  `SCENE_DELETE_THRESHOLD`.
* Any detector or decoding error fails open (`SAFE`), and an absent scene score
  is not zero - it never deletes.
* Swimsuit, underwear, cleavage, gym, dancing, memes, cartoons and normal
  stickers/GIFs are **not** explicit evidence and are allowed.
* Thresholds are calibrated for NudeNet 320n, whose own detection gate is 0.20
  and NMS threshold 0.25. Confirmed explicit media from live testing scored
  0.50-0.67, which is why the default delete threshold is `0.45`. The scene
  thresholds are conservative starting points to be tuned against real traffic,
  not measured constants.

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
  `MUTE_MINUTES` (default 15). Telegram lifts a timed restriction itself.
* A **failed deletion is never a violation** - it logs `DELETE_FAILED` and
  applies nothing.

### Test account

`TEST_USER_ID` (default `8299811287`) marks one account used to exercise the
pipeline repeatedly in a test group. It is **not exempt from anything** -
detection, deletion, the strike, the warning, the admin report and the real
`restrict_chat_member` call all happen exactly as for any other user. The only
difference is what happens *after* a successful restriction:

1. the restriction is applied normally,
2. the warning and admin report are sent normally,
3. `TEST_USER_UNRESTRICT_SECONDS` (default 2) later the restriction is lifted,
4. the warning message from that restriction cycle is deleted.

So the account can send the next test violation immediately. The cleanup never
touches the admin report or the moderated message. At most one delayed job
exists per (chat, user), so repeated violations cannot pile up background tasks.
A failure in the delayed unrestrict is logged and never crashes the bot. Set
`TEST_USER_ID=0` to disable the exception.

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

## Group acquisition (the VPN test handover)

A member asking for a VPN in one of the moderated groups gets a friendly reply
and **one button** — a personal link into the VPN bot, which is where a test
actually gets provisioned and delivered privately.

GuardBot never holds a VPN credential, never talks to the panel, and never puts
a configuration, a subscription URL or a UUID in a group message. It signs an
HTTP request to the VPN bot and gets back either a deep link or a refusal.

It stays silent until the shared secret is set, so a deployment that has not
been wired up to the VPN bot behaves exactly as before.

```ini
GROUP_TRIAL_ENABLED=1
VPNBOT_API_URL=http://127.0.0.1:8099
VPNBOT_SHARED_SECRET=<must equal SERVICE_SHARED_SECRET in the VPN bot's .env>
```

What counts as a request is data, not code: `app/intent_rules.json`. Persian
phrasing, informal wording and spelling variations are normalised before
matching, and an `ignore` list vetoes competing sellers. A bare mention of "VPN"
is not enough, and neither is a plain "my internet is slow" — see
`app/intent.py` and `AgentMD.md` §13.

The container needs `network_mode: host` for this; the reason is in
`AgentMD.md` §13.6 and in `docker-compose.yml`.

## The assistant (Nexus)

"Nexus" is this project's name for the conversational layer as a **role** —
understanding language and context, working out intent, and orchestrating. It is
not a model: which model answers is decided by the `GEMINI_CHAT_*` settings and
the account pool (`AgentMD.md` §28). Nexus may *ask* for an action; the bot's
execution layer decides whether it happens.

**Who it answers.** Only authorized administrators — the owner and anybody with a
stored role. An ordinary member cannot activate it by replying to it, mentioning
it, or wording a message that looks like an order; their message costs one lookup
and never reaches Gemini as a question aimed at the assistant. (Set
`NEXUS_ACTORS_ONLY=false` to restore the older "answers any member who addresses
it" behaviour.) `/nexus status` prints this setting as its `پاسخ‌دهی به` line, so
you can confirm from inside the group which gate is in force.

**It follows the room, not just the messages aimed at it.** Nexus keeps a
bounded, per-group view of the recent conversation — who said what, and how they
stand in the group, labelled by the server from Telegram ids and never from what
anyone wrote — and reads it with Gemini. That is what lets it understand
«پس همون کاری که گفتی رو بکن» with no moderation word in it, or a complaint that
is plainly a request without being phrased as one. A keyword list cannot do that,
so there is no keyword list in the decision: **whether a conversation concerns
Nexus is the model's judgement, not a pattern match.**

Reading and answering are two separate decisions. Nexus is aware continuously and
speaks rarely: it stays quiet through ordinary conversation, and it does not
answer merely because it was mentioned or stay silent merely because it was not.
It speaks when a reply would genuinely help, or when an action actually ran — an
instruction that was carried out is always acknowledged.

This costs far less than it sounds like. A room where twenty people are talking
costs **one** batched call, because the pass waits for the room to fall quiet
first; a room where nobody is talking to Nexus costs none. The awareness layer
has its own daily allowance and its own circuit breaker, separate from the
allowance a person is waiting on an answer to. It can be switched off entirely
with `NEXUS_AWARENESS_ENABLED=false`, and `/nexus status` shows whether it is on
as its `درک گفتگوی گروه` line.

Nexus reading the room grants nobody anything. Every action is still authorized
separately against the Telegram id of the person who actually spoke last, so an
ordinary member's message being understood does not make it an instruction.

**What it does with an administrator's message.**

* Addressed to it — by reply, `@mention`, a `BOT_ALIASES` word, or one of
  `NEXUS_NAMES` (`نکسوس`) — it answers, and can use the moderation tools the
  person's role holds.
* Not addressed — the room is read as a batch, and it replies **only if the
  model judges a reply would help, or if an action actually ran**. A message that
  merely looked like an instruction produces no reply.
* Not addressed and ordinary ("امروز اینجا خیلی شلوغه") — it is understood as part
  of the room and answered with **silence**.

So an administrator never has to reply to the bot for it to know who they are,
and the bot never talks to the room uninvited.

**Switching it off.** The owner can say «نکسوس خاموش شو» / "Nexus shut down", or
use the command:

```
/nexus off      # the assistant stops answering everybody
/nexus on       # back on — the owner can always do this
/nexus status   # state, who last changed it, and whether each group is observable
```

While it is off, the assistant is silent for everybody — including ordinary
members and their mentions — and nothing is spent on Gemini. It also stops
following the group conversation: off means off, so it does not go on recording
a room it was told to stop listening to. The typed moderation commands (`/ban`,
`/mute`, …) keep working: switching the assistant off must not switch moderation
off with it. Only the owner can change the state.

**Acting on a target.** A reply is understood ("این رو ساکت کن" acts on the
person replied to). A name is understood when it is unambiguous ("میلاد رو بن
کن"), because names of people who speak in the group are remembered against their
Telegram id. If two people share a name, the bot asks rather than guessing — and
a name never grants anything: authority always comes from the Telegram id.

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
* The scene classifier is a general NSFW image classifier: it recognises
  scene-level sexual content, but it is not an activity classifier and its
  accuracy on this bot's traffic has **not** been measured against a labelled
  set. `SCENE_DELETE_THRESHOLD` is a conservative starting point, not a
  calibrated constant.
* A scene score below the delete threshold is never a deletion, so a sexual
  scene the classifier scores in the review band is logged but kept.
* A flood is only detected when the threshold is crossed, so the messages sent
  before it are processed normally and only the burst's own messages are
  removed.
* Telegram does not let a bot restrict a chat administrator, so a flooding
  admin cannot be stopped; that refusal is logged and reported, never claimed
  as a success.
* Nexus's awareness of a group is only as good as what Telegram delivers to it.
  With privacy mode on, a bot that is an *administrator* in the group receives
  every message and sees the whole conversation; a bot that is only a *member*
  receives commands, mentions and replies and nothing else, so it reads a
  partial room. `/nexus status` reports which one applies per group.
* The room window is text only. Photos, videos and stickers are recorded as
  their kind (`[photo]`, `[sticker]`), not analysed, so awareness understands
  *that* something was posted but not *what* it showed. Media understanding is
  the moderation path's job and has its own workload.
* Awareness is a bounded recent view, not a transcript: messages older than
  `NEXUS_AWARENESS_RETENTION_SECONDS` (1 hour by default) leave the window, and
  a very busy room keeps only the last `NEXUS_AWARENESS_MAX_ROWS` messages. A
  reference to something said an hour ago may therefore be missed.

