# The conversational assistant

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is the original text, moved out of `AgentMD.md` without editing.
Where a claim in it had drifted from the code, the claim has since been corrected
in place — `git log -- docs/reference/` records each correction, and §53 of
`AgentMD.md` is authoritative where the two disagree.

## Contents

- [17. The conversational assistant](#s17)
- [23. The conversational assistant, made genuinely contextual](#s23)
- [24. Voice: transcription, and voice replies](#s24)
- [38. "Him" means him](#s38)
- [40. A private chat is the owner's, and one request gets one reply](#s40)

---

<a id="s17"></a>

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
| `GEMINI_CHAT_DAILY_LIMIT` | `200` | **per account**, not per bot: the pool spends one account's day and fails over to the next (§29.14) |
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
stating our own prices, plan details, links or credentials — those it cannot
know, and a confident wrong price in a private chat is a commercial problem, not
a cosmetic one. A **public** market figure (a cryptocurrency, gold, a currency or
exchange rate, a stock or an index) is different: it may be stated **only** when
this turn's web search results carry it, and never from memory or by estimation.
The search results are untrusted data; the rule that withholds our own commercial
information is not something a page can lift.

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

* **Has its own key on this deployment now.** `GEMINI_CHAT_API_KEY` is set in
  the host `.env`, so the conversation no longer draws on the classifier's
  allowance. The property worth preserving is the general one rather than this
  deployment's current state: if a deployment leaves `GEMINI_CHAT_API_KEY`
  empty and sets `GEMINI_CHAT_ALLOW_SHARED_KEY=1`, the two workloads draw on
  **one Google allowance** even though this application's counters, windows and
  breakers stay separate, and the startup log says so once, as a warning. A key
  from a second Google Cloud project is the only thing that makes the quotas
  genuinely independent.
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

<a id="s23"></a>

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

<a id="s24"></a>

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
does so; `transcribe` is called from exactly three places — the awareness read,
the conversational path and the transcription-only command — and a test asserts
that count. This is what keeps ordinary group voice out of acquisition and
moderation.

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

<a id="s38"></a>

## 38. "Him" means him

### 38.1 The bug was not the model

«این کاربر رو ساکت کن» worked. «درش بیار» answered that it could not be done,
while the assistant held `unmute_member` the whole time. Two defects, neither in
Gemini's understanding of Persian:

1. **The antecedent did not exist.** A tool call and its result live only inside
   the turn that made them: `chat._tool_turn` builds the exchange in a local
   list and returns the final text, and the conversation store can only hold
   `user` and `model` turns. A function turn has no representation in it. So the
   follow-up turn began with a history in which the mute had never happened, and
   "him" had nothing to point at.
2. **The persona forbade it.** `chat.SYSTEM_INSTRUCTION` is written for a turn
   with no tools and said so in as many words: "you cannot change an account,
   place an order, contact anyone, or run any operation". A prompt that says
   both "you cannot do this" and "here is the tool that does this" is answered
   by refusing.

### 38.2 State the server's record, and scope the persona

`admin_tools.recent_actions_block` appends this actor's recent *successful*
actions in this room to the trusted context, read from `audit_recent_actions`.
Three properties make it safe, and all three are asserted:

* it cannot be planted, because the execution layer writes the audit row *after*
  an action succeeded;
* it is scoped to this actor in this room, so one person's actions are not
  another's antecedent and one group's business is not another's;
* it only lists what actually happened, so a failed mute leaves no phantom
  target.

It is context and never authority: the follow-up still becomes a typed request
that `app/admin_service.py` re-authorises against the actor's real id. The block
says so itself, in the prompt, in as many words.

`chat.TOOL_AMENDMENT` is appended after the persona for a turn that actually
holds tools, and the offending persona line is now scoped to the tool-free case.
`TOOL_AMENDMENT` is also in `chat.__all__`, because a prompt fragment that
matters should be nameable.

### 38.3 Tests

`tests/test_admin_continuation.py` (25 tests): the block appears for the owner
and not for a guest; it lists only successes; it is scoped by actor and by room;
a follow-up that resolves to it still goes through the ordinary check, so a
demoted actor gets the same refusal they would have got without it; the persona
no longer claims it cannot run operations when it holds tools; and the
tool-free path is unchanged.

---

<a id="s40"></a>

## 40. A private chat is the owner's, and one request gets one reply

§34 answers who may talk to Nexus *in a room*. This section is about the other
door, and about the two ways the assistant was answering twice.

### 40.1 The requirement, and why it is not a setting

The owner's instruction was unambiguous: in a private chat, Nexus answers the
owner and nobody else. Not "administrators too", not "administrators if
`NEXUS_ACTORS_ONLY` is off". The reasoning is the same reasoning that makes
`NEXUS_ACTORS_ONLY` correct in a group, read the other way round:

* in a **group**, an administrator is answered because the room is already
  public and moderating it is their job. Answering them discloses nothing that
  the other forty people in the room cannot already read;
* in a **private chat**, there is exactly one reader. Every message the bot
  stores, every turn of context it carries and every answer it produces is
  therefore the owner's property, and answering an administrator would hand a
  third party a window into the owner's own channel.

So it is not a permission and not a flag. It is a second gate.

### 40.2 Two gates, not one setting

`app/nexus.py` holds both, and they are separate functions with separate
docstrings because they are separate rules:

```python
def accepts(principal) -> bool:          # a group
    ...
    return principal.is_owner or principal.is_admin   # subject to NEXUS_ACTORS_ONLY

def accepts_private(principal) -> bool:  # a private chat
    if not is_online():
        return False
    if principal is None:
        return False
    return bool(principal.is_owner)
```

Three properties fall out of writing it this way, and each is a test:

| property | why it matters |
|---|---|
| `NEXUS_ACTORS_ONLY` cannot open it | turning the group switch off restores "answer anybody" *in a group*. Reading it as a statement about private messages would silently reopen this door the first time an operator flipped it for an unrelated reason. |
| being an administrator cannot open it | `accepts(admin) is True` and `accepts_private(admin) is False`, asserted together in one test. If they ever agree, the private boundary has been folded back into the group one. |
| OFFLINE binds the owner too | the offline state is the owner's own instruction, so it applies to the owner in their own channel. `accepts_private` checks it first. |

### 40.3 Refused before the model, and before the record

The gate runs in `main.on_private_text` **before** `_answer_conversationally`,
which means a non-owner's message is refused before `chat.reply` is reached. Two
consequences, both asserted against the transport seam rather than inferred from
silence:

* **no model call happens.** The test replaces `chat.reply` with a stub that
  records every call and asserts the list is empty. A refusal the bot prints
  while still calling the API is not a refusal;
* **no row is written.** This is the one that is easy to lose in a refactor,
  because it is invisible in the reply: a bot that answers only the owner but
  stores everybody's messages looks correct from the outside. The test asserts
  `chat_messages` is empty after an administrator's private message, and that
  the owner's history is not readable from another scope.

Refusal is silent, matching the group policy for a non-actor: being ignored is
not announced. One log line records it, and the reason is the point — "the
owner's assistant stayed silent" and "the bot is broken" must not look the same
in a log:

```
private chat refused user=556 role=admin source=config online=True
```

### 40.4 One request, one reply — the duplicate that was already there

Separately from the boundary, the owner reported that Nexus sometimes answered
the same thing twice. There were **two independent defects**, and they needed
different fixes.

**The first: the ambient path re-answering an addressed message.** A message
aimed at Nexus is answered by `_answer_conversationally`. It *also* joins the
room window, because the awareness layer reads the whole room — so the next
awareness pass could read it, decide it was relevant, and answer it again.

The obvious fix is wrong. Advancing the awareness watermark past an addressed
message would stop the re-answer, but it would also mark the messages *before*
it as read, and those would never be read at all. **Losing events to prevent a
duplicate is a worse bug than the duplicate.**

So the response is suppressed and nothing else is:

```python
_nexus_addressed[room.id] = max(_nexus_addressed.get(room.id, 0), message_id)
if not await _answer_conversationally(...):
    _nexus_addressed.pop(room.id, None)
```

The marker is set *before* the answer is awaited, because a model call is a
suspension point and the pass can run during it; and it is cleared if nothing
went out, so a refused or failed answer leaves the room readable rather than
silent. A withheld answer is not a duplicate, and suppressing the ambient reply
for one would turn a rate limit into silence.

There is a second condition, because there are two ways a batch can be answered
and only one of them is visible in the window: `_nexus_addressed` covers the
answer being written *right now*, which the window cannot show yet, and
`awareness.nexus_has_the_last_word` covers the answer written *before this
process started*, which the marker cannot know about.

### 40.5 One request, one reply — the duplicate that was missing entirely

**The second defect: no `update_id` deduplication at all.** Telegram retries a
delivery when it does not receive a 200 promptly, and python-telegram-bot makes
no promise about the order of two deliveries of the same update. Nothing in the
codebase had ever looked at `update_id`.

The fix is a claim table and a guard handler:

```sql
CREATE TABLE seen_updates (update_id INTEGER PRIMARY KEY, at INTEGER NOT NULL)
```

```python
def update_claim(update_id: int) -> bool:
    """True for the first delivery, False for every later one. Atomic."""
    cur = _conn.execute(
        "INSERT OR IGNORE INTO seen_updates (update_id, at) VALUES (?, ?)",
        (int(update_id), int(time.time())),
    )
    return cur.rowcount == 1
```

`INSERT OR IGNORE` plus `rowcount` is the whole of the concurrency story: the
primary key makes it atomic, so two threads racing the same `update_id` produce
exactly one winner without a lock of ours. The guard runs as a `TypeHandler` in
handler group `-1`, which is the only place that is guaranteed to see every
update before any other handler; a duplicate raises `ApplicationHandlerStop` so
nothing else runs.

What it deliberately does *not* do is advance any watermark. A duplicate
delivery is discarded; the first delivery's effects are untouched, and the
claimed id is pruned after `UPDATE_DEDUP_TTL_SECONDS` (24 h) so the table does
not grow without bound. A missing or zero `update_id` is refused rather than
recorded, because a row keyed on zero would suppress every future update that
also failed to carry an id.

### 40.6 Tests

| file | tests | what it covers |
|---|---|---|
| `tests/test_private_boundary.py` | 10 | the owner is answered; an administrator and a member are refused with **zero** model calls and **zero** rows written; the owner's history is not readable from another scope; an administrator claiming ownership in the message text is still refused; `accepts_private` with `NEXUS_ACTORS_ONLY` off, and offline |
| `tests/test_update_dedup.py` | 13 | first and second delivery, distinct updates, zero and missing ids refused, eight threads racing for one claim, the guard passing the first and raising `ApplicationHandlerStop` on a duplicate, the off switch, DB-failure tolerance, the handler group asserted from the source, prune, and the reaper |
| `tests/test_awareness.py` | +9 | an addressed message is not answered a second time; a room that was never answered is still answerable; a write confirmation is never withheld; a silent decline leaves the room readable and a spoken one keeps the marker |
