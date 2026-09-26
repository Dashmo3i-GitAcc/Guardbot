# GuardBot

Moderation for the moderated groups: a text-moderation AI (off by default), a
pattern filter, an instant media-flood rule, and a warn / count / restrict
ladder. Everything runs on your server.

There is **no visual content detection**. The photo / video / GIF / sticker
analysis pipeline — a local NudeNet detector, a scene classifier and a
media-moderation AI stage — was deliberately removed. A photo, video or GIF is
no longer downloaded or inspected for content at all; media is only ever
counted for the flood rule, from message metadata.

Four independent signals, which are never conflated:

* **text moderation** - the moderation AI's verdict on a group message, acted
  on by a deterministic policy (off by default);
* **pattern filter** - local regex rules that need no model;
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

There is no model to download: the image installs only the Python dependencies
and starts. Wait for `Bot ready.`

## Before you start (Telegram side)

1. Make the bot admin with: Delete Messages, Ban Users (Restrict Members)
2. BotFather: Group Privacy may be left **on**. A bot that is an
   **administrator** receives every group message either way, and that is what
   lets the assistant notice an administrator's message without being replied
   to. A bot that is only a *member* receives commands, replies and mentions and
   nothing else — the assistant then works, but cannot observe. The startup log
   and `/nexus status` say which situation applies to each group.

## How moderation works

```
Telegram media (GIF / sticker / video note)
  -> instant-flood check (kinds from metadata, no download)
       -> burst -> restrict sender + delete the burst's messages + warn

Telegram text (group messages)
  -> pattern filter (local rules, no model)
       -> hit -> delete (or log-only, per rule) + report
  -> text-moderation AI (only when MODERATION_TEXT_ENABLED=1)
       -> AI verdict -> deterministic policy (SAFE / REVIEW / EXPLICIT)
       -> SAFE     -> allow
          REVIEW   -> allow (log + optional admin notice)
          EXPLICIT -> delete the message
                      -> DELETE_SUCCESS: report to admin chat
                                         + one violation: warn, count, restrict at N
                      -> DELETE_FAILED : log only, nothing else happens
```

There is no media-content branch. A photo, video, GIF, sticker or document is
never downloaded for analysis and never reaches the moderation AI. The only
thing a media message can trigger is the flood rule, which reads metadata.

### Text moderation

Off by default (`MODERATION_TEXT_ENABLED=0`). When on, a group text message is
sent to the moderation AI, which returns a classification, a confidence and a
category. The decision is made in code, not by the model:

| Decision | Meaning | Action |
|---|---|---|
| `SAFE` | nothing wrong | allow |
| `REVIEW` | worth a human's attention, below the delete bar | allow + log |
| `EXPLICIT` | a confident verdict on a deletable class | delete the message |

* Only classes in `MODERATION_DELETABLE_CLASSES` (default `explicit_sexual`) can
  ever be deleted; every other class is carried, logged and reported.
* A deletion needs an AI confidence of at least `MODERATION_DELETE_CONFIDENCE`
  (default 0.80). Below it the verdict is treated as uncertain and only logged.
* The band down to `MODERATION_REVIEW_CONFIDENCE` (default 0.45) is `REVIEW`:
  allowed, but visible in the log and, with `MODERATION_REVIEW_NOTIFY`, in the
  admin chat.
* The AI never acts. It cannot delete, restrict or reply; its output is data and
  the action is decided by `app/mod_policy.py`. There is no code path from its
  return value to a Telegram call.
* The admin report for a deleted text message carries the classification, the
  confidence and the policy reason — never an excerpt of the message.

### Pattern filter

Local regex rules (`app/text_filters.py`) that need no model, so they keep
working when the moderation AI is off, out of quota or unreachable. A rule can
delete or only log, and every hit is reported. The filter and the AI are
independent: a message that hits a rule is still allowed to be seen by the AI
unless the rule already deleted it.

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

One confirmed deletion — a text message the moderation AI confirmed, or a
confirmed flood — is one violation, recorded in the existing `users.strikes`
column.

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

The admin chat receives a report for every deletion (`EXPLICIT` +
`DELETE_SUCCESS` in the text path), every filter hit, every `REVIEW` when
`MODERATION_REVIEW_NOTIFY=1`, and a notice when a confirmed flood could not be
restricted. `SAFE`, `DELETE_FAILED` and operational errors are logged to the
container log only.

A deletion report contains the user, user id, chat id, message id, the AI's
classification and confidence, the policy reason and a timestamp. It carries
**no excerpt of the message and no media** — the content itself is what this
project spends the most effort not copying into a log or an admin chat.

Every report also carries a `🗑 حذف گزارش` inline button that deletes the
**report message itself** (the moderated message is already gone). Any
**current member** of the admin chat may use it — Telegram administrator status
is not required, and it is not limited to the owner. A user who has left or was
removed cannot use it, and a button press coming from any other chat deletes
nothing. If the report was already deleted, the press is answered and ignored.

### What it does NOT do

* No visual / media content moderation of any kind. A photo, video, GIF,
  sticker or document is not inspected for content — only counted for the flood
  rule.
* No ban and no permanent punishment: the only member action is a **timed**
  restriction.
* A failed deletion logs `DELETE_FAILED` and applies nothing.

### Media types

Media is never analysed. This table is only about the flood rule.

| Type | Counts toward a flood |
|---|---|
| Photo | no |
| GIF / animation | yes |
| Video | no |
| Video note | yes |
| Static sticker (.webp) | yes |
| Video sticker (.webm) | yes |
| Animated sticker (.tgs) | yes |
| Image / video document | no |

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

**Who it answers.** The **room**, not the speaker. A Telegram group is served
only if it is on the server-side allowlist (the `authorized_groups` table,
seeded once from `GROUP_IDS` on first boot); once a room is registered, **every
member** of it may talk to Nexus. An unregistered room is refused before any AI
work — no model call, no identity write, no awareness capture. Being added to a
group, or made an administrator in it, does **not** register it: the Owner (or a
server-side administrative workflow — `/registergroup`, `/unregistergroup`,
`/groups`) does. `/nexus status` prints the scope as its `پاسخ‌دهی به` line, so
you can confirm from inside the group.

Being able to *talk* is not being able to *act*: every action a conversation
produces is re-authorised from the actor's Telegram id, and a member is offered
no administrative tool at all.

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

What the pass knows is **staged rather than preloaded**. The room's own name and
the people who were here last time are always there, because they are free — a
name Telegram already gave us, and a list the server already wrote down. The
expensive context is built only when the batch calls for it: recent
administrative actions when an administrator is involved, and one line about
each person the batch actually refers to when somebody has replied to somebody
else. An ordinary member's ordinary message carries neither, so nothing deeper
is looked up for it — the allowance is rationed in *requests*, and context
nobody asked for is paid on every pass. `NEXUS_AWARENESS_CONTEXT_DEEP=false`
removes the whole conditional half.

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

**Voice messages.** A voice note aimed at Nexus is transcribed and answered, and
the answer can come back as a **voice message** that replies to the note — the
same assistant, answering with the same context a typed message would get. This
is **Voice Context**, and it is a layer of its own with its own switch.

It is *not* a separate voice bot. The note is understood first — downloaded,
transcribed, its sender resolved, its reply edge and target read, and the same
room / memory / state / awareness / date / search context a text turn composes —
and only then is that context, together with the person's own voice, handed to
the provider's Live API for one spoken turn. The server decides identity,
memory and authority exactly as before; the model never does, and the spoken
session is given **no tools at all**. A voice note that contains an instruction
is answered with words, like any other message.

Because a voice message carries no caption to put `@guardbot` in, the way to aim
one at Nexus is to send it as a **reply** to one of its messages.

* **Off means the old path.** With the switch off, a voice note is transcribed
  and answered in text, exactly as before. Nothing else changes.
* **Every failure falls back to text.** No credential, a dropped connection, a
  provider that never speaks, audio that cannot be decoded or encoded, a refused
  upload — the words still reach the person. The turn is bounded and always
  closes its session, so nothing is left open.
* **It is owner-only to switch.** «ویس کانتکست خاموش» / «ویس کانتکست باز» (or
  "voice context off" / "voice context on") moves it; an administrator cannot,
  because the permission behind it is held by no role. `/nexus status` shows it
  as the `کانتکست صوتی` line.

It shares the Live API credential with the voice call by default
(`VOICE_CONTEXT_API_KEY`, falling back to `GEMINI_LIVE_API_KEY`) but keeps its
own allowance and breaker, so a busy afternoon of voice notes cannot spend the
day a call was waiting on. Set `VOICE_CONTEXT_ENABLED=false` to remove the layer
entirely — a deployment with no live credential is inert by itself, and voice
notes simply take the text path.

### The coding agent (the bridge)

The owner can ask Nexus in the group for a change to this system's own code —
«توی guardbot این باگ رو درست کن» — and Nexus hands the work to a coding agent
running on the host. The answer comes back into the same conversation: a single
message that narrates the run as it works, then the result as its own message,
chunked in order or as a document.

It is **owner-only**, and structurally so: the permission behind it is held by no
role, so an administrator cannot be given it however they are promoted. Nexus
may *ask*; it may not decide. It cannot claim to be the owner, cannot name a
repository that is not on the allowlist, and cannot approve its own request — the
repository is a name that the server resolves to a directory, and the approval is
the owner's.

**Dangerous work waits.** Deploying, migrating, deleting, resetting and changing
credentials are recorded and **not started**. They appear in `/agent`, and they
run only when the owner says so explicitly. With more than one waiting, a bare
«اوکی» gets a question rather than a guess.

`/agent` shows the bridge's state; `/agent confirm <id>` and
`/agent cancel <id>` are the typed interface for when the assistant is the thing
that is broken.

The execution half is a separate host process, `tools/agent_runner.py`, because
the container ships neither Node nor the CodeBuddy CLI. Pointing `AGENT_CLI` at a
working invocation is a deployment step — see `AgentMD.md` §39.

## Test in a private test group first

Set `GROUP_IDS` to a test group, send normal photos / videos / stickers and
check that nothing gets deleted. Watch the decisions:

```bash
docker compose logs -f | grep -E "DELETE_SUCCESS|DELETE_FAILED|SKIPPED"
docker compose logs -f | grep -E "FLOOD|VIOLATION"
docker compose logs -f | grep "text moderation"
```

Tune `MODERATION_DELETE_CONFIDENCE` / `MODERATION_DELETABLE_CLASSES` after
watching real traffic, and `BURST_MAX_ITEMS` / `BURST_WINDOW_SECONDS` after
watching real flood behaviour (all are environment variables, so no rebuild is
needed).

## Tests

The tests need the same environment as the bot (Telegram, Pillow), so run them
in the image:

```bash
docker compose build
docker run --rm -v "$PWD:/srv" -w /srv guardbot-guardbot \
  bash -lc "pip install -q pytest && python -m pytest tests -q"
```

## Known limits

* Text moderation is off by default, and when on it is the one path that can
  delete a person's **words**, in a language the model may misjudge. Turn it on
  after watching the review log for a while.
* The AI cannot be asked for a second opinion on a media file, because no media
  is sent to it: a photo, video or GIF is never inspected for content.
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
  *that* something was posted but not *what* it showed. The assistant can read a
  file that is explicitly sent to it, but nothing analyses group media
  automatically.
* Awareness is a bounded recent view, not a transcript: messages older than
  `NEXUS_AWARENESS_RETENTION_SECONDS` (1 hour by default) leave the window, and
  a very busy room keeps only the last `NEXUS_AWARENESS_MAX_ROWS` messages. A
  reference to something said an hour ago may therefore be missed.
* The coding-agent bridge needs a coding agent on the host, and this container
  has neither Node nor the CodeBuddy CLI. `tools/agent_runner.py` is the other
  half and runs outside the container; which invocation it uses is a deployment
  decision, and on this host the CLI's headless mode needs a credential and a
  free loopback port that the bot cannot supply. Everything on the bot's side of
  the bridge is implemented and tested regardless — see `AgentMD.md` §39.15.
* A coding task's output is whatever the agent printed, redacted and bounded. The
  bot does not verify that a change was made or that a test passed; it relays
  what the agent said and records it. A claim in an agent's summary is a claim.

