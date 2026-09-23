# The Gemini account pool

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is the original text, moved out of `AgentMD.md` without editing.
Where a claim in it had drifted from the code, the claim has since been corrected
in place — `git log -- docs/reference/` records each correction, and §53 of
`AgentMD.md` is authoritative where the two disagree.

## Contents

- [28. The Gemini account pool: many keys, one AI service](#s28)

---

<a id="s28"></a>

## 28. The Gemini account pool: many keys, one AI service

Until this section, each of the four AI workloads had exactly one credential, and
a credential whose quota ran out was the end of that workload until an operator
edited `.env` and restarted the container. `app/gemini_pool.py` replaces that
with a pool: several credentials per workload, each tracked as its own account,
and a request that survives one of them running out.

The Telegram layer does not know any of this happened. `ai_intent._request`,
`chat._request`, `ai_moderation._request` and `transcribe._request` each ask for
one answer and get one, exactly as before. Failover lives in the provider layer
because that is the only place it can live without being repeated four times and
getting it wrong in one of them.

### 28.1 Every key is a separate account — the rule that shapes the design

The brief is explicit, and the implementation takes it literally: **each
configured key is a separate Google account and a separate project, with its own
quota.** Two keys are not one bigger allowance.

Three things follow, and each is load-bearing:

1. `gemini_accounts` is keyed by `(workload, slot)`. The same key serving two
   workloads is two rows with two counters, two cooldowns and two failure states.
   Workload isolation is then a property of the schema rather than a promise
   about how the code happens to call things.
2. A key written into two slots of one workload is collapsed to **one** account,
   by truncated-SHA-256 fingerprint. Counting it twice would invent a quota that
   does not exist.
3. Where the provider does *not* prove two credentials are independent, the pool
   does not claim they are. Google publishes no API that maps a key to its
   project (verified — see §28.5), so "are these the same project?" is answered
   by what *can* be observed: two slots holding the identical key are one
   project, and that is detected, recorded, and reported at boot by
   `gemini_pool.shared_credentials()`.

`chat` and `tts` share a credential on purpose — speech synthesis is a mode of
the conversation feature, not a peer of it — so that pairing is excluded from the
warning. Crying wolf about a deliberate configuration is how an operator learns
to ignore the warning that matters.

### 28.2 The two levels of failover, and why conflating them is the bug

Google enforces limits at more than one granularity. Treating them as one thing
is the mistake this module is written to avoid:

| | level 1 — model | level 2 — account/project |
|---|---|---|
| example | `gemini-flash-lite-latest` is rate-limited | the project's quota is gone, or the key is revoked |
| right response | another compatible model, **same account** | the next account |
| wrong response | abandon the account | try another model |
| cost of the wrong response | a healthy account benched for the cooldown | quota spent on calls that cannot succeed |

So a model failure never disables an account, and an account failure is never
treated as a model problem. `Account` and `ModelState` are separate objects with
separate states and separate persisted rows.

`classify_error()` reads the failure from the **response body** rather than the
exception class, because the SDK's exception types have moved between versions
and the JSON has not. The shapes below were captured from the live API, not
guessed:

* an unknown model answers `404 NOT_FOUND` — *"is not found for API version
  v1beta, or is not supported for generateContent"*
* a bad key answers `401 UNAUTHENTICATED`
* a per-model limit names the model in `quotaId`; a project-wide one names the
  project or the free tier

When the provider names neither, the conservative reading is a **model** limit:
it costs one wasted call on a sibling model, where the opposite mistake costs the
whole account for the cooldown.

### 28.3 Capability, not just availability

"Use all models of an API until it is limited" means all *compatible* models.
The distinction matters because the failure modes are asymmetric:

* A **text-only** model handed an audio clip does not error. It invents a
  transcript, and an invented transcript is indistinguishable from a real one
  downstream. That is the worst available failure for the transcription
  workload.
* A **text-only** model handed an image in moderation answers about the caption
  instead of the picture. A moderation decision made on different evidence than
  the operator believes is a safety problem, not a quality problem.
* An **image-generation** model, or a video/music/embedding model, would either
  fail outright or answer a different question than the one asked.

So every workload declares what it needs a model to be able to do, and the pool
never offers a model that does not satisfy it *in full*:

| workload | needs |
|---|---|
| intent | `text` |
| chat | `text` |
| moderation | `text` |
| transcribe | `audio_in` |
| tts | `audio_out` |

Two mechanisms answer two different questions:

* **Availability** comes from discovery: `models.list` against the credential,
  filtered to models that advertise `generateContent`. A model the provider does
  not list for this key is never tried.
* **Capability** comes from a curated table in the module, because the provider
  publishes no modality metadata at all. That table is deliberately
  conservative: an unrecognised name returns `None` rather than being
  optimistically assumed multimodal.

Discovery failing means *do not filter*, never *no models*. Losing an
optimisation must not lose the request. The result is cached per credential
fingerprint for `GEMINI_MODEL_DISCOVERY_TTL` seconds, in the database, so a
restart does not re-ask.

### 28.4 The state machines

Accounts:

```
ACTIVE ──429 (project)──► QUOTA_EXHAUSTED ──reset passes──► ACTIVE
ACTIVE ──401/403────────► INVALID            (terminal: never retried)
ACTIVE ──5xx/network────► UNAVAILABLE ──cooldown──► ACTIVE
ACTIVE ──success────────► ACTIVE  (a recovery is announced to the owner)
```

Models, independently:

```
ACTIVE ──429 (model)────► RATE_LIMITED ──cooldown──► ACTIVE
ACTIVE ──404────────────► DISABLED        (terminal: it will not start existing)
ACTIVE ──5xx/network────► ACTIVE + short cooldown
```

The last line is deliberate. A brief provider wobble says nothing about the
model, so it is *not* benched for the model cooldown — a short one keeps the next
request trying it, instead of the model being wrongly written off for minutes.

A state of `RECOVERING` becomes `ACTIVE` on load: the process that was going to
prove recovery is gone, and the next request is itself the proof.

### 28.5 What the provider does not tell us

Verified against the live API rather than assumed:

* `models.list` returns `name`, `version`, `displayName`, `description`,
  `inputTokenLimit`, `outputTokenLimit` and `supportedGenerationMethods` — and
  nothing else. In particular it does **not** report input or output modalities.
* No response header or body exposes the Google Cloud project behind a key.
* Google does not publish remaining quota or reset times for these keys.

Three consequences, and they are deliberate rather than gaps:

* Capability is a curated table (§28.3).
* The project behind a key is inferred only where inference is sound (§28.1).
* **There is no "requests remaining" figure anywhere in this codebase.** The
  status report prints `Not exposed by provider` and reports the bot's own
  observed counters separately, labelled as observations. A reset time is shown
  only when an error response actually carried a `retryDelay`.

### 28.6 Retries, cooldowns and the attempt budget

Retries are bounded on three axes, because any one of them alone can be
circumvented by a large enough pool:

* `retries + 1` attempts per model;
* `GEMINI_POOL_MAX_ATTEMPTS` provider calls for one logical request — the hard
  ceiling that stops a pathological pool spending a minute on one message;
* exponential backoff with jitter (`_backoff`). The jitter is not politeness: the
  four workloads share one process, and without it a rate-limited provider gets
  every workload's retries in lockstep.

When the pool is in use it **owns** the retry policy, and the per-workload retry
loops run exactly once (`attempts = 1 if pooled else ...`). Two loops would
multiply the two budgets and re-walk a pool that had already given up, spending
real quota to learn what the first pass already knew.

A `SCOPE_REQUEST` failure — a 400 that is not a capability mismatch — stops
immediately without trying another account: the payload is wrong, every account
would answer identically, and the rest of the pool would be pure waste.

### 28.7 Selection: least-recently-successful, not "first until it dies"

`ordered_accounts()` sorts by `last_success` ascending. Staying on API #1 until
it dies is the policy that leaves four configured accounts unused, which is the
opposite of the point. Spreading the load is what makes the pool's *total*
capacity available rather than only its first account's.

Within an account, the configured preference order is preserved: the primary
model is tried first and fallbacks only when it is unavailable. Nothing rotates
randomly.

### 28.8 Pool events — recorded, never announced

Meaningful transitions are written to `gemini_events` as structured rows,
deduplicated on `(workload, kind, slot, model)` against
`GEMINI_POOL_EVENT_COOLDOWN`, so a hundred consecutive 429s are one row.

| event | when |
|---|---|
| `model_failover` | a model failed and a sibling is being tried |
| `account_failover` | an account left the pool |
| `account_recovered` | an account came back |
| `pool_critical` | the pool shrank to exactly one usable account |
| `pool_empty` | no usable account remains |

**These events do not reach Telegram, and there is no path by which they could.**
The pool module does not import `telegram`, `Pool.record()` is synchronous — so
there is no `await` in it and nothing it could call that would touch a network —
and there is no notifier to register: `set_notifier`, the module-level
`_notifier` slot, and `main.notify_owner` were all removed, along with the
`_pool_bot` handle that let the pool reach a chat from inside a request no
handler was running.

This was a deliberate reversal. The pool used to deliver failover and health
notices to `ADMIN_LOG_CHAT`, falling back to the owner's private chat, and in
production that put `GEMINI MODEL FAILOVER`, `rate_limited` and
`unsupported_input` in front of a group of members every time a retry happened.
Operational detail about the AI provider belongs to the operator who asks for
it. Nothing replaces the notices — no queue, no digest, no "only the important
ones" filter — because a filter would be a promise about which messages matter,
and the requirement is that none are sent.

The deduplication is kept even though nothing is delivered, because it is what
makes this table a record of *transitions*. The quantitative history — requests,
successes, failures, rate limits, quota events — already lives on the account
and model rows; a table that repeated it would be larger and less readable.

`/pool`, owner-only, renders the live report: per-workload account counts by
state, the active account and model, and per-account requests, successes,
failures, rate limits, quota events, cooldown, and the honest `Not exposed by
provider` for remaining quota and reset. It is owner-only because it describes
the operator's own Google projects; it is audited either way. **It is also the
only way pool state reaches a human** — on request, never on a timer.

### 28.9 Configuration

```dotenv
# shared accounts, drawn on by any workload whose opt-in allows it
GEMINI_KEY_1=...          # ... up to GEMINI_KEY_20
GEMINI_POOL_KEYS=...      # or one comma-separated list

# extra accounts for one workload only
GEMINI_MOD_API_KEY_2=...  # ..._2 through ..._20

GEMINI_MODEL_DISCOVERY_ENABLED=true
GEMINI_POOL_MODEL_COOLDOWN=120
GEMINI_POOL_QUOTA_COOLDOWN=900
GEMINI_POOL_TRANSIENT_COOLDOWN=15
GEMINI_POOL_EVENT_COOLDOWN=900
GEMINI_POOL_MAX_ATTEMPTS=12
```

The number of accounts is not hard-coded: 1, 5, 20 or more need no redesign.
A shared-pool key is used only when the workload's existing opt-in is on
(`GEMINI_CHAT_ALLOW_SHARED_KEY` and friends) — that flag is the operator saying
"these workloads may share one Google allowance", and the pool must not make that
decision for them.

### 28.10 What did not change

* **Four workloads, four budgets.** The pool adds accounts behind each workload;
  it does not merge them. `tests/test_ai_isolation.py` still asserts the
  structural separation, and `tests/test_gemini_pool.py` asserts the new rows are
  per workload.

  *Later additions.* §24's voice reply and §35's Group Awareness each became a
  pool workload of their own — `tts` and `awareness` — and both are deliberate
  *modes of the conversation feature* rather than independent capabilities, so
  `shared_credentials()` excludes them from the shared-pool rule and both may
  legitimately run on the chat credential. §51's voice interface added
  `live_voice`, which is **not** a mode and is reported like any other workload,
  and §52's web search added `search`. What is separate where it matters is
  unchanged: each has its own allowance, breaker, counters and model. The four
  budgets of §26 are still four; the pool now carries **eight** entries —
  `intent`, `chat`, `moderation`, `transcribe`, `tts`, `awareness`, `live_voice`
  and `search`. The authoritative list is `config.GEMINI_POOLS`; this note names
  the count so it cannot drift silently.
* **Voice-to-text is still not a conversation.** Transcription reaches the pool
  through `transcribe._request` only, with `audio_in` required of every model.
* **Moderation is still fail-safe.** A pool failure produces `decided=False`,
  which the policy engine reads as "not confirmed" — the direction that deletes
  nothing. No failover can produce a ban, a mute or a deletion.
* **Gemini still executes nothing.** The pool returns text, or PCM. It has no
  tools, no function calling, no database handle and no Telegram client.
* **The single-key path still works.** A deployment with one credential per
  workload is a pool of one and behaves exactly as it did.

### 28.11 Verifying it

```bash
# What the pool looks like from inside the container. Never contains a key.
docker exec guardbot python -c "
from app import db, gemini_pool; db.init(); gemini_pool.build_pools()
print('\n'.join(gemini_pool.startup_lines()))"

# The same report the owner sees, in Telegram.
/pool

# The pool's own suite, with a scripted provider and no network.
.venv-test/bin/python -m pytest tests/test_gemini_pool.py -q
```

### 28.12 Known limitations

1. **Discovery answers availability, not usability — and they are not the same
   thing.** Measured on 2026-09-21: `models.list` still lists
   `gemini-2.5-flash`, `gemini-2.5-flash-lite` and `gemini-2.5-pro`, and calling
   any of them answers `404 NOT_FOUND — "This model … is no longer available to
   new users."` The 404 is classified correctly and the model is disabled for
   that account permanently, so the pool converges on a working set either way —
   but the convergence costs one wasted call per account per retired name. That
   is why the default preference list no longer contains the 2.5 family, and why
   it is worth re-probing the list after a Google model deprecation rather than
   trusting `models.list` alone.
   Related: a listed-and-capable model can still reject a specific payload; that
   arrives as a 400, which `classify_error` reads as `unsupported_input` and
   treats as a model problem, so the next model is tried.
2. **`Transcript.model` names the workload's configured model**, not the model
   that actually answered after a failover. The per-model counters in the pool
   are the authoritative record of what served what. Making the field exact would
   mean threading the served model back through `_request`, whose two-argument
   signature the existing suite replaces.
3. **The pool does not schedule a recovery probe.** An account returns to
   rotation when its cooldown expires and a real request tries it, which is
   deliberate — a health-check loop is a second source of provider calls that
   nobody asked for.
4. **Concurrency is bounded by one process.** Counters use the database's own
   lock and atomic `UPDATE ... SET x = x + 1`, so concurrent requests inside the
   container cannot corrupt them; two containers sharing one SQLite file is not a
   configuration this deployment has and is not supported.

### 28.13 The intent workload's failure count, and what it was not

On 2026-09-22 the acquisition/intent pool's account row read `requests=445`,
`successes=110`, `failures=335` — 75% of provider attempts failed, which reads
as a broken workload. It is not, and the gap between the two readings is the
first thing to get right.

#### The number is per attempt, not per request

The `gemini_accounts` and `gemini_models` counters count **provider calls**, and
the pool's whole job is to make many of them for one logical request. When the
primary model fails, the walk continues across every compatible model on the
account and every account, `retries + 1` times each. One logical failure
therefore becomes several provider failures.

The workload's own table is the other reading, and it counts **logical
requests**:

| | value |
| --- | --- |
| `ai_usage.calls` (2026-09-22) | 90 |
| decided (`relevant` + `irrelevant`) | 69 |
| `malformed` | 8 |
| `errors` | 13 |
| pool `requests` / `successes` / `failures` | 445 / 110 / 335 |

So the logical failure rate was **23%**, not 75%. Both numbers are correct; they
answer different questions. `/pool` reports attempts because that is what
failover costs; `ai_usage` reports requests because that is what the daily cap
counts.

#### The taxonomy, from the log and the rows

`[pool] error workload=intent …` over the incident window:

| kind | detail | meaning |
| --- | --- | --- |
| `provider_error` | `504` | the provider honoured our deadline and aborted a call that had hung |
| `provider_error` | `503` | the backend was briefly unavailable |
| `rate_limited` | `generate_content_free_tier` | a free-tier 429, per model |

The intent account carried **0 rate limits** on its primary model and 83
failures, so its failures were provider-side slowness and unavailability, not
quota. The fallback models were worse on this credential — `gemini-3.5-flash-lite`
answered 1 of 73, `gemini-3.5-flash` 0 of 35, `gemini-3.7-flash` 0 of 20,
`gemini-pro-latest` 0 of 7 — so failover across models rarely recovered.

#### The false lead: the deadline

The natural reading of a `504` is "the model needed longer". It was measured and
rejected. On the very account whose row shows the 504s, with the same model and
prompt, varying only the deadline:

```
gemini-flash-lite-latest @ 10s   12 of 12 answered, 0.8-1.8s
gemini-flash-lite-latest @ 25s   12 of 12 answered, 0.8-1.8s
```

a burst of six concurrent calls included at each deadline. This classification
answers in about a second, so ten seconds is ten-fold headroom, and the 504s were
episodes of a hung provider rather than a systematically short bound. Raising the
deadline would not have made those calls finish; it would only have made the
group handler wait longer for the same non-answer. **`GEMINI_TIMEOUT_SECONDS`
therefore stays at the API's 10s floor**, and
`test_the_shipped_intent_deadline_stays_at_the_api_floor` records why, so a
future session brings a measurement rather than a hunch.

#### The real defects, and the fix

1. **Nothing bounded the wall clock of one logical request.** Twelve attempts at
   ten seconds is two minutes, and `on_group_text` *awaits* `classifier.classify`,
   so one ambiguous message could block the trial-offer handler for minutes. The
   attempt count bounded the spend; nothing bounded the time. This is what
   `Pool.time_budget` and `GEMINI_INTENT_TIME_BUDGET_SECONDS` fix: an opt-in
   ceiling, checked *before* each attempt, so it covers the whole failover walk.
   It raises `PoolUnavailable("time_budget", …)` with the last real failure folded
   into the detail and records a `time_budget` event. Every other workload keeps
   `0`, which is "no ceiling" — the behaviour it always had.
2. **The workload had one account.** Every 429 was terminal and every hung
   project was hit by every request. Four more credentials were added as
   `GEMINI_API_KEY_2..5`, giving the intent pool five independent projects; the
   boot line reads `[pool] intent: accounts=5 usable=5`.
3. **The fallback list looks dead on this credential, and was still left
   alone.** The rows are damning at face value — `gemini-3.5-flash-lite` answered
   1 of 73, `gemini-3.5-flash` 0 of 35, `gemini-3.7-flash` 0 of 20,
   `gemini-pro-latest` 0 of 7 — but they are *conditional*: a fallback is only
   tried after the primary has already failed, so its record is measured during
   exactly the provider-wide bad periods that caused the primary to fail. That is
   a selection effect, not a verdict on the models, and trimming the list on it
   would delete models that are fine on an ordinary afternoon. The primary model
   answers the great majority of calls, and discovery plus the per-model
   cooldowns already bench what the provider actually rejects. Revisit this only
   with data from a healthy window.

#### Why intent does not have the chat allowance bug

The chat incident (§29.15) was a per-account daily allowance charged for
provider attempts that were *refused*. Intent has no per-account allowance at
all — `daily_budget` is set for `chat`, `awareness`, `live_voice` and `search`,
and not for `intent` — so `refund_daily` is a no-op for it and there is nothing
to over-charge. Intent's own daily cap counts **logical requests**
(`ai_usage.calls`, incremented once per `classify`), so a request that walks
twelve provider calls still costs one. Both properties are asserted in
`tests/test_ai_intent.py`.

#### Verifying it

```bash
.venv-test/bin/python -m pytest tests/test_gemini_pool.py -q -k time_budget
.venv-test/bin/python -m pytest tests/test_ai_intent.py -q
# live: the ceiling, the account count, and a real classification
docker exec -i guardbot python -c "
import asyncio; from app import ai_intent, db, gemini_pool; db.init()
p = gemini_pool.pool_for('intent')
print('budget', p.time_budget, 'accounts', len(p.accounts))
print(asyncio.run(ai_intent.classify('vpn میخوام')))"
```
