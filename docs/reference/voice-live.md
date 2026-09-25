# Nexus Voice Live

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is verbatim; it was not edited during the move.

## Contents

- [51. Nexus Voice Live: the same assistant, with a microphone](#s51)

---

<a id="s51"></a>

## 51. Nexus Voice Live: the same assistant, with a microphone

Nexus joins a Telegram voice chat and holds a realtime conversation in Persian.
It is **not** a second assistant that only speaks: it is the same Nexus — the
same awareness, the same authority model, the same audit trail — reached through
a voice interface instead of a text one. That sentence is the design, and every
decision below is downstream of it.

The feature is a **new capability alongside** the text assistant, the awareness
layer and the coding-agent bridge. Nothing it adds replaces or removes anything
that existed before, and the two things it is forbidden to do are the two things
it would have been easiest to do: build a second awareness, and give the model a
way to act on its own.

### 51.1 What it is, in one paragraph

Somebody brings Nexus into a voice chat with a spoken command. From then on the
participants talk to it, and it answers out loud, in Persian, in about a second.
It knows which room it is in because it asks the *existing* awareness layer. It
can be asked to ban, mute or warn somebody, and it will — by calling a function
whose result is a structured request that goes through
`app/admin_service.py`, which re-resolves the speaker from their Telegram id and
applies the same `app/rbac.py` rules that every other administrative act goes
through. The model's opinion about who is speaking is never read, and its
opinion about what it is allowed to do is never read either.

### 51.2 The measurement that chose the model

The model was not chosen by reputation. On real Persian speech synthesised by
this project's own TTS, the time from end of utterance to the first audio byte
was measured:

| model | latency | Persian |
|---|---|---|
| `gemini-3.8-live` | **1.12 s** | `fa-IR` accepted |
| `gemini-3.1-flash-live-preview` | **1.12 s** | `fa-IR` accepted |
| `gemini-2.5-flash-native-audio` | **2.21 s** | auto-detect only |

The purpose-built native-audio model is the obvious choice and it is the wrong
one twice over: it is twice as slow here, and it rejects every explicit Persian
language code (`1007 Unsupported language code 'fa-IR'`), so it can only be run
on auto-detect. The general live model is first for that reason, and the flash
preview is the fallback because it measured identically.

Two other refusals were measured and are the reason the failure taxonomy has a
`setup_rejected` reason that is deliberately **not** retried:

* `gemini-3.5-transcribe-live` refuses the session outright —
  `1007 The requested combination of response modalities (AUDIO) is not supported`;
* explicit `activity_start`/`activity_end` is rejected —
  `1007 Explicit activity control is not supported when automatic activity
  detection is enabled`.

### 51.3 The transport, and an honest correction

Joining a Telegram voice chat is an **MTProto** operation. The Bot API has no
method for it at all — checked against all 277 public `Bot` methods — so this
feature cannot be built on the bot's own token and must not pretend to be.

`py-tgcalls` (2.3.3) over `ntgcalls` (2.2.5) is the transport, and it is
sufficient: it joins, hands over incoming PCM tagged with an `ssrc`, accepts
outgoing PCM, and lists participants with the `user_id` each `ssrc` belongs to —
which is the whole of what this feature needs, speaker identity included.

**A correction, recorded because the wrong version sent the fix in the wrong
direction.** An earlier note in this project said `ntgcalls` published no wheel
for Python 3.12 and that the transport was therefore impossible on this
deployment. That was wrong. `ntgcalls 2.2.5` publishes
`cp312-manylinux_2_28_x86_64` wheels, and `py-tgcalls` installs cleanly on the
container's interpreter. The real blocker is a **credential**: an
`api_id`/`api_hash` pair from my.telegram.org and a logged-in user session.

So the adapter is written against the library's real signatures, read from the
installed package rather than from memory, and it reports
`not_configured` with a reason that distinguishes "the library is missing" from
"the credentials are missing" — because those need two different fixes. The
interface and the double are real and exercised by the suite; everything above
them is tested against the double. **The credential now exists and a real call
has been held with it** — §51.18 records the live join, the two defects it
exposed, and the discovery defect that had been reported as one misleading
sentence.

### 51.4 The audio path, and the finding the feature was built around

Three sample rates meet in one call and none is negotiable: Telegram hands over
**48 kHz**, the provider's realtime input is **16 kHz**, and the provider answers
in **24 kHz**. The ratios are integers — 3 and 2 — which is why `audio.py`
converts with an average over three samples and a midpoint between two, and why
it *refuses* a mixed ratio rather than approximating it. `24000 → 16000` reduces
to 2/3 and is not a conversion the call performs; a caller that needs it composes
`24000 → 48000 → 16000`, which is exact.

The finding that matters, and it was found by running it rather than by reasoning
about it:

> **The feed to the provider must never stop.** The provider's voice-activity
> detector finds the end of an utterance in the *trailing silence*. A real
> transport delivers frames only while somebody is speaking, so a session that
> forwarded only what arrived would hand the provider a sentence and then
> nothing — and the provider would wait for ever while the caller heard silence.
> This happened twice before the cause was found.

The fix is a silence pump (`_silence_loop`): whenever no real audio has been
forwarded for one frame interval, one frame of silence is sent. It is the single
most important loop in the file, and it exists because the obvious
implementation does not work.

Two smaller ones, both about artefacts a person can hear:

* the resampler carries **both** a whole-sample remainder and a single odd byte
  between chunks. `to_samples` is a pure function and drops a trailing half
  sample, which is right for it — but a *stream* that dropped one byte per odd
  chunk would quietly degrade the audio rather than fail;
* `to_bytes` **clamps** rather than wraps. A wrapped sample is a loud click, and
  a loud click is the worst artefact to introduce into a voice call because it
  sounds like a hardware fault.

### 51.5 Speaker identity comes from Telegram, never from the model

A voice conversation has no message to attribute. In a group chat every action
carries an `actor_id` that came from an `Update` the Telegram servers signed; in
a voice chat there is no update per utterance, only a stream of audio frames each
tagged with an `ssrc`, which identifies a *stream* and not a person.

The mapping from `ssrc` to a Telegram user id comes from the transport's own
participant list, which comes from Telegram. That is the only path by which an
identity enters this subsystem, and the model cannot influence it: it is told who
is speaking for the sake of the conversation, and its opinion about it is never
read back as an identity.

Three rules follow, and they are `app/voice_live/speakers.py`:

1. **An unattributed utterance has no actor.** If the current stream is not in
   the participant map the speaker is `0` — not "probably the last person", not
   "the only person here". `0` fails closed at every downstream check, because
   `admin_service` refuses a request with no actor as malformed.
2. **A claim is not an identity.** There is deliberately no method that accepts a
   name, a username or an id *from the model*. Adding one would make "Nexus
   thinks this is the owner" a thing this code could express.
3. **Stale is unknown.** A speaker who stopped sending frames two seconds ago is
   no longer the current speaker, because by then the audio arriving is not
   theirs.

### 51.6 The security boundary: the model asks, the application decides

The model may never perform an administrative act. What it produces is a
`VoiceActionRequest` — `action`, `actor_id`, `chat_id`, `target_id`, `reason`,
`resolution`, `request_id`, `at` — and that record has **no authority field**.
There is nothing in it a model could populate that would grant anything.

The vocabulary is a strict subset of the real one: `ban_member`, `unban_member`,
`mute_member`, `unmute_member`, `warn_member`. The owner-only switches,
promotions, the coding agent and the VPN are **not reachable from a voice chat at
all** — a model that invents `vpn_admin` gets a refusal, and the refusal happens
in `actions.py` before anything is submitted.

Everything else is the existing machinery, unchanged:

| layer | what it does | what it does not do |
|---|---|---|
| `actions.py` | closed vocabulary, argument shape, a local rate limit | it never authorises |
| `admin_service.execute` | re-resolves the actor, applies `rbac`, audits the act | it never trusts the request's claim |
| `rbac.py` | the same roles and permissions as every other interface | it does not know voice exists |
| `TelegramGateway` | the same ten Telegram operations | there is no second action engine |

The actor is read from the speaker map **at the moment the tool call arrives**,
and never from the tool call. A tool call with nobody attributable produces a
refusal, not an exception and not a guess.

One detail is deliberate: the refusals `actions.py` produces locally are
*public* `AdminResult`s built through `admin_service.message_for`, and they are
**not** written to `admin_audit`. A malformed request is not an administrative
act and must not appear in the trail as one.

### 51.7 The awareness bridge: the same awareness, read-only

This is the requirement that shaped the package. Voice Live must not grow a
second awareness; it must ask the one that already exists, and it must ask it
read-only.

So there is exactly one path, and it is two calls:

```
ctx  = awareness_context.build_ctx(chat_id)
text = awareness_context.blocks(ctx)
```

Both are the existing module's public surface. Nothing in
`awareness_bridge.py` decides what context *is*; it decides *when to ask*, which
is a different question.

What the bridge will not do, and each of these was a decision:

* **It will not write.** Not to the database, not to the room cache, not to the
  awareness switch. `note_room` is *not* called here even though it would make
  the room's name available — the message handler already calls it for every
  message, and the join command is a message. A read-only bridge that writes
  "just one cache" is not read-only.
* **It will not cross rooms.** One bridge is one `chat_id`, fixed at construction
  and carried into every call it makes. A bridge with no room is refused rather
  than defaulted, because `build_ctx(0)` would read an empty window and answer
  confidently about nowhere.
* **It will not pass a secret through.** Every snapshot is scanned for the
  credential shapes this deployment uses and any hit is redacted before the text
  can reach a prompt. That is a second line, not the first: the first is that
  nothing sensitive is put in the block.

It is a **cache with a refresh policy**, not a function, because a live call is
continuous and an awareness pass is not: rebuilding the context for every
utterance would run a query and rebuild an identical string per sentence. A
snapshot is reused for `GEMINI_LIVE_CONTEXT_TTL_SECONDS` and rebuilt when it goes
stale, when the session reconnects, or at the end of a turn — and a refresh
reports whether anything actually *changed*, so an unchanged block is not
re-sent. The block is wrapped in a sentence that says what it is — the server's
own record — and what it is not: an instruction. Without that, a room named
"ignore your instructions" is a room that has instructed the model.

### 51.8 The session: five loops, and the bugs that only exist between them

`session.py` is where the timing lives, and almost every difficult thing about it
comes from the interaction between four concurrent activities rather than from
any one of them:

* **in** — incoming audio, attributed, resampled, forwarded;
* **out** — the provider's speech, resampled, framed, played, paced;
* **provider events** — turns, barge-ins, tool calls, disconnects;
* **housekeeping** — the idle timer, the session ceiling, awareness refreshes.

Four behaviours were found by running it, and each is now a named thing:

**The feed never stops** — §51.4.

**A barge-in must flush, not pause.** When somebody talks over Nexus the queued
audio is no longer wanted. Pausing playback would resume it after the
interruption, and the room would hear the tail of an answer to a question nobody
is asking any more. So the queue is emptied, the transport is silenced, and the
state moves to `INTERRUPTED` — which is *not* `CONNECTED`, because the person who
interrupted is still talking and their audio is already arriving.

**A reconnect resumes; it does not restart.** The provider hands back a session
handle, and reopening with it keeps the conversation instead of replaying it. The
first version closed the provider *before* reading that handle, so the handle was
always empty and every reconnect silently began a fresh conversation — Nexus
forgetting the last minute of a call for reasons nobody in the room could see.
The test that catches it asserts the second connection was opened *with* the
handle the first one was issued.

**A session that ends on its own timer must leave the voice chat.** The timer
runs inside the housekeeping task, and teardown cancels the tasks — so a teardown
that cancelled its own caller had `CancelledError` thrown into it partway
through. The provider was closed, but the voice chat was never left and the
session never reached a resting state. `_cancel_tasks` now skips the current
task, and every loop checks `_stop_requested` instead.

Two more leaks were found the same way and are worth naming because both would
have looked like something else entirely:

* **the transport was never closed.** `leave` steps out of the voice chat;
  `close` disconnects the MTProto client the adapter opened to do it. Calling
  only the first leaks a live socket and an authorised session for every call the
  process ever holds — which eventually reads as "Telegram started rate-limiting
  us for no reason", long after the call that caused it;
* **a failed session stayed in the call.** Giving up on reconnecting, or losing
  the incoming stream, left the session in `FAILED` with the voice channel still
  joined and no task left to let go of it. Both paths now tear the session down.

The state machine was also wrong in a way only the session could reveal. The
table permitted `connected → listening` but not `connected → speaking`, so the
first answer of every call attempted an illegal move: the machine refused it
while the audio played anyway, and the state disagreed with the call it
described. `THINKING` was in the vocabulary and unreachable. The table now has
the edges the session actually uses, `THINKING` is entered when the transcript
says the utterance ended, and the deliberate exclusion — a barge-in while Nexus
is silent is not a barge-in — is unchanged and still asserted.

### 51.9 A seventh pool workload, gated

`live_voice` is a new workload in `app/gemini_pool.py`, with its own credential,
its own daily allowance and its own model preference. The separation is the same
one `awareness` has: a call holds a stream for minutes and must not be able to
spend the allowance a text conversation is waiting on.

Admitting live models needed a change to the pool, because they are streaming
models and `{audio_in}` is a subset of their capability set — so the old filter
would have offered a live model to the transcription workload. The fix is an
explicit `LIVE` capability and an explicit gate in `Pool.models_for`. The gate is
asserted rather than assumed: a test removes it, watches a live model appear in
the `intent` workload's offered list, and then requires it back.

`capabilities_of` also distinguishes a *bidirectional* live model from a live
**transcription** model, which has no `audio_out` and must never be offered as
something to talk through.

### 51.10 The commands, and the verb they share

Bringing Nexus in and taking it out are **commands, not requests**. They must
work with no model, no network and no allowance — the same argument that makes
the assistant's own on/off switch a phrase list — so they are matched as fixed
phrases, before any conversational path is reached, and they are owner-only.

The hazard is that the two vocabularies share a verb. «نکسوس بیا» turns the
assistant on; «نکسوس بیا بیرون» leaves a call. A router that read only the verb
would silence the assistant when the owner meant to leave a call, or refuse to
start one because it thought it had been asked to shut down.

Two structural answers:

* **the voice router stands down for anything that reads as a switch.** A message
  that `nexus.command_from` claims is left to the switch router. The check is
  deliberately made with the *wider* reading (`names_layer=True`), because for a
  guard the safe direction is the one where more messages count as a switch;
* **the order is asserted against the source.** A test reads `on_group_chat` and
  requires the voice router to come before both the switch and the model.

A contradiction is refused rather than guessed at — «برو ویسکال، بعد بیا بیرون»
is not a request — and a negation cancels the whole message through the same
`nexus.negated` the switch uses, because «برو ویسکال نکن» contains the join
phrase and asks for the opposite of joining.

Four outcomes get four sentences, because they need four different fixes: the
feature is switched off (a decision), the assistant is switched off (a different
decision), the transport is not available here (a configuration), the group
already has a call (a state). The sentence is chosen from the *manager's* reason,
so the two cannot drift.

### 51.11 Failure behaviour, and the direction it fails in

Every failure has a machine reason and a retryability, decided in `errors.py`.
The default for an unknown reason is **not retryable**, which is the safe
direction: an unknown failure retried is a loop, while an unknown failure
abandoned is a call that ends and says so.

| failure | retried? | why |
|---|---|---|
| `connection_lost`, `connect_failed`, `timeout`, `go_away` | yes | the weather |
| `setup_rejected` | **no** | a refused configuration does not improve on a second attempt |
| `quota_exhausted` | **no** | no credential is a decision, not a hiccup |
| `disabled`, `not_owner`, `busy`, `limit_reached` | **no** | not failures of the provider at all |

Starting fails **closed**. A call is a stream, not a request, so there is no
partial answer and no mid-sentence failover: the only honest outcomes are "start,
on a working credential" and "do not start". If the provider cannot be opened the
session leaves the voice chat rather than sitting in it answering nobody.

### 51.12 Privacy: what is never kept

* **No raw audio is persisted.** Nothing is written to disk. A buffer is at most
  one 20 ms frame, and a resampler's remainder is the residue of one arithmetic
  operation — at most six bytes.
* **No transcript is logged or stored.** Metrics are integers and timestamps;
  `Metrics` has no field that could hold a string, which is why "no sensitive
  logging" is easier to keep here than to remember.
* **No credential reaches a prompt, a log or a context block.** The context block
  is scrubbed for the two credential shapes as a backstop, and the system
  instruction contains no secret and no room context.
* **Context does not cross rooms.** One bridge is one `chat_id`, fixed at
  construction.

### 51.13 Tests

`tests/test_voice_live.py` (81) covers the pure parts: the state table, the
failure taxonomy, the audio maths (including ragged chunking being byte-identical
to one-shot), the speaker map, the awareness bridge, the action bridge, and the
pool gate — plus the whole spoken-action path end to end through the **real**
`admin_service.execute`, including the refusals: a stranger cannot ban, nobody
can ban the owner, an admin cannot mute a peer, a helper without the permission
cannot ban, a replay is a duplicate with one mutation, and the audit row records
the actor.

`tests/test_voice_live_session.py` (45) covers the timing: the silence pump, real
audio being resampled and attributed, barge-in flushing the queue, audio after a
barge-in not being played, the state settling when a turn completes, reconnect
resuming with the handle, reconnect giving up and leaving, a non-retryable
rejection not being retried, both timers, the context being sent once and
refreshed between turns, the transport being released, a call that ends on the
far side leaving, and the manager's refusals — including that two simultaneous
starts cannot both win.

`tests/test_voice_live_commands.py` (25) covers the vocabulary and the routing
order, the shared verb, the contradiction, the negation, the non-owner, and each
of the four refusal sentences.

`tests/test_voice_live_discovery.py` (34) covers finding the call and the
adapter's join path: a live call found and returned, no call, a scheduled call, a
visibility error, a flood wait, an unresolvable id, a basic group, a slug, the
outcome→reason mapping, the library being started before it plays, the discovered
call being seeded into the library's cache, a seed that fails not failing the
join, each refusal reason, a second join being a no-op, `close` leaving every
joined call by id, and — against the source — that neither the transport nor the
resolver imports another AI workload. The resolver tests skip where Telethon is
absent; the adapter tests do not, because the resolver is stubbed for them.

`tests/test_gemini_pool.py` gained four tests for the `LIVE` gate. Two existing
guard tests were widened rather than deleted: the workload-vocabulary test now
names `live_voice` as a second deliberate addition, and the daily-allowance test
records why a live call is rationed in *calls per day* rather than in minutes.

The whole suite is **3897 passing**.

### 51.14 Configuration

| variable | default | what it does |
|---|---|---|
| `GEMINI_LIVE_ENABLED` | **`false`** | the feature gate. A deployment that has not opted in cannot reach any of it |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | first choice, by measurement (§51.2) |
| `GEMINI_LIVE_FALLBACK_MODELS` | `gemini-3.1-flash-live-preview` | measured identically |
| `GEMINI_LIVE_API_KEY` | `""` | its own credential; the shared pool is opt-in via `GEMINI_LIVE_ALLOW_SHARED_KEY` |
| `GEMINI_LIVE_DAILY_LIMIT` | `60` | **calls** per account per API day — one call is one request |
| `GEMINI_LIVE_MAX_SESSIONS` | `1` | concurrent calls |
| `GEMINI_LIVE_MAX_SECONDS` | `3600` | the session ceiling |
| `GEMINI_LIVE_IDLE_SECONDS` | `180` | the quiet ceiling |
| `GEMINI_LIVE_TIMEOUT_SECONDS` | `30` | the connect deadline |
| `GEMINI_LIVE_LANGUAGE` | `fa-IR` | Persian, first-class |
| `GEMINI_LIVE_VOICE` | `Puck` | a prebuilt voice name |
| `GEMINI_LIVE_BARGE_IN` | `true` | whether being talked over stops Nexus |
| `GEMINI_LIVE_RECONNECT_ATTEMPTS` | `3` | with backoff, the call stays joined throughout |
| `GEMINI_LIVE_CONTEXT_TTL_SECONDS` | `45` | how long an awareness snapshot is reused |
| `GEMINI_LIVE_MAX_ACTIONS` | `20` | spoken actions per session |
| `GEMINI_LIVE_ACTION_COOLDOWN_SECONDS` | `3` | the gap between them |
| `GEMINI_LIVE_JOIN_PHRASES` | see config | how the owner says "come in" |
| `GEMINI_LIVE_LEAVE_PHRASES` | see config | how the owner says "come out" |
| `GEMINI_LIVE_TRANSPORT` | `auto` | `auto`, `pytgcalls` or `fake` (tests only) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | `0` / `""` | the MTProto credential the real transport needs |
| `GEMINI_LIVE_SESSION_PATH` | `/data/voice_live.session` | the MTProto session file, inside the data volume |

Turning the feature off needs no code change and no restart of anything else:
`GEMINI_LIVE_ENABLED=false` means no call can start, and any call in progress is
ended by the same gate that refuses the next one.

### 51.15 What has not been verified, and why

Stated plainly, because the difference between a measured fact and a documented
assumption is the difference between an engineer and a brochure:

* **A live Telegram voice call has been held** (§51.18). The adapter joined a
  group whose voice chat was active, read back its own participant entry — `ssrc`
  included, so the identity path was exercised — left, and closed with the
  account no longer in the call. The session is a real logged-in user session,
  not a bot: `BOT=False`.
* **The outgoing microphone frame format** — 16-bit PCM, 48 kHz, mono, 20 ms — is
  what `ntgcalls` documents and what `audio.py` implements, but it has not been
  confirmed against a live call's *audio*: the join above was a control-plane
  join, and no microphone frame was carried through the provider on it. It is one
  constant in `audio.py` if it turns out to be wrong, and saying so is better
  than presenting an unverified number as a measured one.
* **The provider path itself was exercised live**, on this project's own keys:
  Persian TTS → continuous feed → transcript → answer → audio, at 1.12–1.21 s;
  and a spoken ban end to end, from the utterance through the tool call, the
  action bridge and `admin_service.execute` to `ok=True`.

### 51.16 The dependency, and how the image gets it

`requirements.txt` declares three packages for the real transport:

| package | pin | why |
|---|---|---|
| `py-tgcalls` | `>=2.3,<3` | the transport itself. Pure Python, `Requires-Python: >=3.10` |
| `telethon` | `>=1.45,<2` | **declared directly**, see below |
| `ntgcalls` | `>=2.2.5,<3` | transitive, pinned because it is the one native component |

The Telethon line is the one worth reading twice. `py-tgcalls`'s metadata lists
it as `telethon>=1.24.0; extra == "telethon"` — an *extra*, not a base
dependency. So the obvious declaration, `pip install py-tgcalls`, produces an
image in which `telegram_voice.py`'s own `import telethon` fails. It would fail
at the first join rather than at build time, which is the worst possible moment
for it, and it is exactly the kind of gap that a "the library is installed"
check misses. Declaring it directly is the fix; `test_voice_live_transport.py`
asserts both that it is declared and that the extra is not being relied on.

`ntgcalls` is pinned explicitly even though `py-tgcalls` already constrains it
(`>=2.2.4,<3.0.0`), because it is the only native wheel here and its tag is what
decides whether this image can hold a call at all. The version verified for this
deployment is 2.2.5, whose `cp312-cp312-manylinux_2_28_x86_64` wheel is what
makes Python 3.12 viable — the fact §51.3 corrects. Leaving it floating would let
a resolver choose a build this interpreter cannot load.

They are installed **unconditionally**, not behind a build arg. The point of
adding them is that the image *can* hold a call; what keeps the feature off is
`GEMINI_LIVE_ENABLED`, not the absence of a library. The graceful degradation
survives: with the packages removed the transport reports `library_missing`, the
bot boots, and nothing else changes — and that path is asserted, not assumed.

The `Dockerfile` copies `tools/` as well as `app/`, because the bootstrap below
has to run *inside* the container: it writes to `/data`, which is the mounted
volume. Only source is copied. The session file is never in the image — it is
created at runtime under `/data`, and both `.gitignore` and the Dockerfile's
explicit `COPY` paths keep it out. A credential baked into a layer would be
readable by anyone who can pull the image and would survive every rotation.

The build also **proves** the transport is loadable instead of assuming it.
Declaring the packages is not the same as the image being able to run them: the
native half (`ntgcalls`) can fail for a reason no Python-level declaration
catches — a wheel built for the wrong ABI, or linked against a `libstdc++` the
base image does not carry — and `telegram_voice.py` imports it lazily, so that
failure would surface at the first join rather than at build time. The
`Dockerfile` therefore imports all three in a `RUN` step immediately after the
install, so a broken wheel fails the build. This does not weaken the runtime's
graceful degradation — the transport still reports `library_missing` and the bot
still boots if the packages are ever absent from an environment — it only
guarantees that *this* image, built from this file, is one that can hold a call.

### 51.17 Creating the MTProto session (once, by hand)

The last thing between this feature and a real call is a logged-in user session.
It cannot be created by the bot: Telegram sends a code to a phone, and a bot
process that stopped to ask for one would be a bot process that had stopped
moderating. So it is created once, by hand, with a tool that does nothing else:

```
docker compose run --rm guardbot python -m tools.voice_live_session
```

It asks three questions — the phone number, the code Telegram sends, and, only
when the account has two-step verification, the password. The code and the
password are read with `getpass`, so they are not echoed and do not reach the
shell history.

**What it writes.** One file: `/data/voice_live.session`
(`GEMINI_LIVE_SESSION_PATH`), mode `0600`, inside the mounted data volume, with
its directory at `0700`. The permissions are set after the file exists rather
than left to the process umask, because a umask is a property of whoever ran the
command and not of what the file is.

**What it refuses.** It will not replace a session without being told to. If one
is already there and already authorised it says so and exits without touching
it; if one is there but does not work it asks first, and `--force` is the
non-interactive way to answer yes. With no terminal it refuses outright rather
than assuming an answer. A failed attempt removes the file it created, so no
half-made credential is left looking like a session — but it never removes one
it did not create.

**What it never prints.** Not the phone number, not the code, not the password,
not the `api_hash`, and not any part of the session. A failure is reported by the
exception's *type*, never its message, because the message is where the phone
number ends up. Telethon's own logging is turned down for the same reason: it
logs connection detail at INFO and, on some paths, the number it is sending a
code to.

**Required environment.** `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from
my.telegram.org, plus the bot's own `BOT_TOKEN` and `GROUP_IDS`, which is why the
documented invocation goes through `docker compose run` — that loads `.env` for
you. A missing variable is reported by name and never by value.

The session file is a credential and is treated as one everywhere: it is in
`data/`, which is ignored by Git; it is not in the image; and it must never
appear in a document, a log, a test fixture or a Telegram message. Rotating it
means running the command again with `--force`, or deleting the file and
re-running — the account's own Telegram session list can revoke it.

**Where this stands (2026-09-25).** The `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
pair is provisioned in the host's `.env` (mode `0600`, gitignored) — never in
Git, never in the image. **The session now exists and is authorised**:
`/data/voice_live.session` (host `data/voice_live.session`, mode `0600`,
`root:root`), belonging to a real user account with `BOT=False`. It has been used
to hold a real call (§51.18). `GEMINI_LIVE_ENABLED` remains `false`: the session
being ready is not the same as the feature being switched on, and the flip is a
deploy that needs a fresh go-ahead. The flip itself is setting
`GEMINI_LIVE_ENABLED=true` and recreating the container (`env_file` is read at
start), not rebuilding.

### 51.18 Finding the call: what the library swallows, and the live join

Holding a call is two steps — find the active call, then join it — and the
failure recorded here was in the first one, not the second. A join against a
group whose voice chat was believed to be active returned
`JoinRejected: NoActiveGroupCall`. The account was a member and the creator, the
session was authorised, and the library imported and started. Nothing in that
message said what had actually gone wrong.

**What the library does.** `py-tgcalls` finds a call through
`ClientCache.get_input_call`, which reads its own `InputGroupCall` cache and, on a
miss, falls back to `ChannelFull.call` / `ChatFull.call`. That fallback is wrapped
in

    except Exception:
        pass

so every discovery failure — not a member, forbidden, a flood wait, a network
error, an id the client cannot resolve — becomes `None`, which `play()` then
reports as `NoActiveGroupCall`. One sentence for four problems, and it is the
wrong sentence for three of them.

**What Telegram actually exposes.** The active call of a channel or supergroup is
`ChannelFull.call`; of a basic group, `ChatFull.call`. There is no third
mechanism. A Telegram client that connected *after* a call began never receives
the `UpdateGroupCall` that announced it — which is why a raw-update listener can
watch a group with a live call for a minute and see nothing. Discovery must ask
for the full chat; it must not wait for an update, and the absence of an update
proves nothing.

**The fix.** `app/voice_live/call_discovery.py` does the asking, in the open, and
returns a *kind* rather than a `None`: `active`, `scheduled`, `none`, `no_access`,
`unsupported` or `error`. `PytgcallsTransport.join` turns the kind into a machine
reason and — when it found a call — hands the call to the library by seeding the
cache, so the library's own lookup, and the swallowed exception inside it, is not
consulted at all. The seed is private API, so it is one guarded seam that is
allowed to fail: if a release moves it, the join still works and the library
looks the call up itself.

The refusal now names the fix:

| what Telegram said | reason | what it means |
|---|---|---|
| `call` is `None` | `no_active_call` | there is no call here. Start one |
| a call with `schedule_date` set | `scheduled_call` | it is scheduled, not live |
| `ChannelPrivateError`, `ValueError`, … | `call_not_visible` | this account cannot see it |
| a flood wait, or an unexpected reply | `discovery_failed` | asking failed; retry later |

**The live join, and the two defects it exposed.** With the real session, joining
a group that *does* have an active call succeeds: the adapter reports its own
`ssrc` from the participant list, leaves, and closes with the account out of the
call. Two things had to be fixed for that to be true at all:

* `PyTgCalls.play` is wrapped in `@mtproto_required`, which raises
  `ClientNotStarted` until `start()` has run. The adapter now starts the library
  before it plays — without it, the first join failed before Telegram was asked;
* `close()` called `leave_call()` with no argument. `leave_call` requires a chat
  id, so it raised `TypeError` into a handler that logged at debug and moved on:
  shutting the adapter down left the account sitting in the voice chat until the
  socket happened to die. It now leaves every call it holds, by id.

**What the live run also settled.** `ChannelFull.call` was checked across every
group the session is in, in one run with one session: four returned a live call
and the rest returned `None`. The mechanism is therefore not the problem, and a
`None` is Telegram's answer about *that* group rather than a failure to ask. The
target group of the original report returned `None`, had no call service message
in its history, and produced no `UpdateGroupCall` while watched — so the
resolver now reports it as `no_active_call`, which is a fact, instead of a
`NoActiveGroupCall` that could have meant four different things.
