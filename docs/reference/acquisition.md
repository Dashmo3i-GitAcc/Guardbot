# Acquisition: the VPN bot handover

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is verbatim; it was not edited during the move.

## Contents

- [13. Group acquisition — the VPN bot handover](#s13)

---

<a id="s13"></a>

## 13. Group acquisition — the VPN bot handover

Someone asks in one of the moderated groups for a VPN. Instead of ignoring it or
answering with a link, the bot offers them a **personal way into the VPN bot**,
which is where a test actually gets provisioned.

The other half of this feature lives in the VPN bot
(`mo3iiibest77-hub/vpn-bot-private`, `/opt/vpn-bot/AGENTS.md` §13). Read that
side before changing this one; the wire protocol is the contract between them.

### 13.1 The boundary

| | GuardBot | VPN bot |
|---|---|---|
| Detects the intent | ✅ | |
| Signs requests | ✅ | ✅ verifies |
| Holds panel credentials | ❌ **never** | ✅ |
| Decides eligibility | ❌ | ✅ |
| Creates the client | ❌ | ✅ |
| Sends a configuration | ❌ **never** | ✅ |

This bot must never hold a VPN credential, never talk to the 3x-ui panel, and
never put a subscription URL, a UUID, a `pbk` or a panel client name in a group
message. `tests/test_acquisition.py` asserts all of that by reading the outgoing
`send_message` calls. The only thing a group ever sees is a friendly line and
one button.

Nothing is scraped: this bot does not read the VPN bot's Telegram messages. It
calls a signed HTTP endpoint and gets back a deep link or a refusal.

### 13.2 What counts as an intent

The rules are **data**, not code: `app/intent_rules.json`. Topic groups, support
groups, standalone phrases and `ignore` patterns, each a list of regexes with
weights. Adding a phrasing is a JSON edit. `INTENT_RULES_PATH` can point at a
different file to override them on a running deployment without a rebuild.

`app/intent.py` normalises the message *and* the rule patterns before matching —
Arabic yeh/kaf to Persian, alef variants unified, ZWNJ and bidi controls
stripped, harakat stripped, Persian and Arabic-Indic digits to ASCII, Arabic
punctuation to ASCII, whitespace collapsed. Without this, «ویپیان» typed with an
Arabic yeh never matches a Persian pattern, which is most of the traffic.

Scoring: a **topic** hit (weight 2) **and** at least one **support** hit
(request / problem / poor-internet, weight 1) makes an intent. A **standalone**
phrase (weight 3) is enough on its own. An **ignore** pattern is a hard veto.

Two deliberate consequences:

- **A bare mention of "VPN" is not an intent.** With `INTENT_REQUIRE_TOPIC=1`
  (the default), «اینترنتم ضعیفه» is not one either — half the group complains
  about slow internet, and offering all of them a test is noise. Both are
  asserted in `tests/test_intent.py`.
- **The `ignore` list vetoes competing sellers.** Someone advertising their own
  service must not be handed ours.

`INTENT_MIN_LENGTH` (4) exists because a three-character message cannot carry a
request; without it, stray short messages reach the matcher.

### 13.3 The cooldown, and why it is not in memory

`intent_offers(chat_id, user_id, last_offered, last_reason)` in `app/db.py`.
`INTENT_COOLDOWN_SECONDS` (3600) is checked per chat *and* user, and the record
is in SQLite because this container is restarted on every deploy — an
in-memory cooldown would reset and re-offer to the same person.

The VPN bot enforces the real limit (one test per account, §13.5). This cooldown
is only here so a chatty member cannot make the bot look like a spammer.

### 13.4 The handler

`on_group_text` is registered in `group=1` behind `acquisition_message_filter()`,
which is `TEXT & ~COMMAND & ChatType.GROUPS & ~EDITED_MESSAGE`. Edited updates
are not requested from Telegram at all, and the filter excludes them as well, so
editing a message into an intent cannot produce a second reply.

It returns early for bots, for `user.is_bot`, and for group admins — never offer
staff their own product. It only acts in `config.GROUP_IDS`, so the bot can
never advertise in a group the owner did not list.

Replies go through `_reply_in_group`, which retries without
`reply_to_message_id` when the original message is gone: a reply to a deleted
message is an error, and losing the invitation over that would be silly.

### 13.5 The VPN bot client

`app/vpnbot.py` mirrors the VPN bot's signing scheme rather than importing it —
the two projects run different frameworks on different Python versions and
cannot share code. Both repositories assert the **same fixed vector**
(`34bfe93c19f199b1e9d20199845cea664b0f0da554e0f16b170563d4f7176950`). If you
change the signing string, change it in both and update both tests, or
production breaks with a 401 that looks like a wrong secret.

`ERR_NOT_CONFIGURED` / `ERR_UNREACHABLE` / `ERR_BAD_RESPONSE` are this side's
failures; `ERR_REFUSED` carries the VPN bot's decision about the user
(`already_invited`, `in_progress`, `invalid`, `already_used`, `unavailable`),
each with its own reply. A refusal is a 200 with `ok: false` — a decision, not
an error — so "we already sent you a link" is distinguishable from "the VPN bot
is down". An unreachable VPN bot is silent by design: a group must not see the
infrastructure complaining.

### 13.6 Deployment, and the networking trap

```ini
GROUP_TRIAL_ENABLED=1
VPNBOT_API_URL=http://127.0.0.1:8099
VPNBOT_SHARED_SECRET=<must equal SERVICE_SHARED_SECRET in the VPN bot's .env>
```

**The container runs with `network_mode: host`.** The obvious alternative —
staying on a bridge network and using
`extra_hosts: host.docker.internal:host-gateway` — does not work on this host:
the gateway address *is* the host, so the packet lands on the host's `INPUT`
chain where ufw's default-deny drops it. It does not refuse, it **times out**,
which looks exactly like the VPN bot being down. Sharing the host's network
namespace makes the VPN bot reachable on `127.0.0.1:8099`, and the VPN bot binds
loopback to match. Nothing is published either way.

The coupling to remember: if this container goes back to bridge networking, the
VPN bot's `INTERNAL_API_HOST` must go back to `0.0.0.0` **and** the host firewall
must allow the Docker bridge range to reach 8099. Both sides, together.

`docker-compose.yml` carries the same note next to the setting.

### 13.7 Testing

| File | Covers |
|---|---|
| `tests/test_intent.py` | normalisation, a corpus of realistic Persian requests, the negative corpus, the bare-mention and connectivity-complaint guards, the competing-seller veto, the knobs, rule loading and extension |
| `tests/test_vpnbot_client.py` | the signing vector, header shape, nonce uniqueness, cross-path and body-tamper rejection, unconfigured refusal, unreachable transport |
| `tests/test_acquisition.py` | the group handler: the invitation is sent, **nothing else ever reaches the group**, unmatched messages stay silent, the cooldown across a restart, refusal copy, unreachable, unconfigured, deleted original, other groups, bots and admins, the command filter |

`tests/test_acquisition.py` exercises the real `main.acquisition_message_filter()`
rather than a copy of it, so a filter change cannot pass the tests while
changing production behaviour.

The full suite has no model dependency, so a plain venv (`.venv-test/`,
gitignored) runs everything. Run it in the image when you want the most faithful
environment.

### 13.8 The AI second opinion

The rules decide. This section is about the layer that exists because they
cannot decide *everything*, and about the constraints that keep it from becoming
the thing that decides.

**Why it exists.** `app/intent_rules.json` is a list of patterns someone wrote
down. A member can ask for a VPN in a sentence no pattern describes — «یه چیزی
میخوام که بشه باهاش رفت» — and the rules, correctly, stay silent. The layer
closes that gap. It is an addition to the rules, never a replacement: with it
switched off, on a keyless deployment, or with Google unreachable, the bot
behaves exactly as it did before the layer existed.

**The decision order.** `app/classifier.py` is the only place the two meet, and
its four steps are the design, not an optimisation:

1. **A veto is final.** An `ignore` match (a rival seller advertising) is
   decided and the model is never asked. A guard that a persuasive message can
   talk out of a veto is not a guard.
2. **A rule match is a decision.** It is acted on immediately. No call, no
   quota, no latency.
3. **No subject signal means ordinary.** Silence from the rules is only
   ambiguous when the message was *about* circumvention or connectivity.
   `intent.is_candidate(match)` is the gate; everything else stays silent and
   costs nothing.
4. **Only then, the model** — the genuinely uncertain middle.

**The candidate gate.** A message is a candidate when the rules found a
`topic` hit or one of the weight-0 patterns in the `ai_candidates` group of
`app/intent_rules.json` (`"candidate_group": "ai_candidates"`). Weight 0 is
deliberate: the group marks a message as *worth a second look* without moving
the score, so it cannot by itself produce an offer. Pricing words live there
because «قیمتتون چنده» and «قیمت گوشی چنده» are lexically identical and only
the model can tell them apart.

**The contract.** `app/ai_intent.py` asks one bounded, structured question:
`response_mime_type="application/json"` with a `response_json_schema`, and the
schema has fields for `is_relevant`, `intent_category`, `confidence`,
`needs_acquisition_offer`, `reason` and `signals` — and **no field for a
message**. The model is a classifier; it cannot address a user, and `reason` /
`signals` are for the log only. `parse_verdict` validates strictly: a blank
answer is `empty_response`, non-JSON is `malformed_json`, a non-object is
`malformed_shape`, a missing or wrongly-typed field is `malformed_missing` /
`malformed_type`, an unknown category is folded to `other`, and confidence is
clamped. Anything malformed is *unknown*, which is the same as "the rules'
silence stands".

**Failure is contained, in this order.** A timeout (`asyncio.wait_for`) or an
unexpected exception is caught; transient failures get one retry with backoff,
permanent ones (a `400`, an empty answer) do not; a rate window caps how often
we ask; a persisted daily cap stops us at the day boundary; and a run of
consecutive transport failures opens a circuit breaker for five minutes. The
daily counter is keyed to the API's own day — midnight **Pacific**
(`db.ai_day`, `_API_DAY_OFFSET = 8 * 3600`) — and it is pinned so that it can
never roll over *before* Google's does. In summer that means we are an hour
stricter than the API, which is the safe direction: the failure worth avoiding
is believing we have allowance the API still considers spent.
`classify()` never raises, and `app/main.py` never awaits it in a way that can
fail a handler.

**The deadline has a floor of 10 seconds, and you cannot go below it.** This
one cost a live debugging round: with `GEMINI_TIMEOUT_SECONDS=6` the transport
accepted the setting, the request went out, and Google answered *every* call
with

```
400 INVALID_ARGUMENT  Manually set deadline 6s is too short.
                      Minimum allowed deadline is 10s.
```

The layer reported itself `active` and classified nothing — silent, total, and
invisible to a suite that replaces `_request`, because the mock never validates
the deadline. So `ai_intent.MIN_DEADLINE_SECONDS` is 10.0 and
`timeout_seconds()` clamps to it rather than trusting the setting; the default
in `config.py` matches. `tests/test_ai_intent.py` asserts the floor, the clamp,
and that the value actually reaches `HttpOptions` — the last one is what fails
if someone removes the clamp. **A green unit suite is not evidence this
integration works; one real call is.**

Function calling is disabled explicitly (`AutomaticFunctionCallingConfig`). We
give the model no tools, so leaving it on only produces a warning on every
request and advertises a capability this integration never wants.

**What it can and cannot change.** It can promote a message the rules missed
and it can decline a candidate. It cannot overturn a rule match or a veto, and
it cannot change eligibility, provisioning or what is sent: the offer is the
same invitation from §13.1, sent to the same place, through the same VPN bot
endpoint. Nothing about the security model changes — the VPN bot still decides
who gets a test.

**Observability.** One `[intent]` line per decision (§8) with `source=` telling
you which layer decided. `ai_usage` in `app/db.py` keeps per-day counters for
`calls`, `relevant`, `irrelevant`, `malformed`, `errors` and `skipped` — and a
skip (a rate-limit, a circuit, a missing key) deliberately does **not** consume
the day's allowance, because we did not ask.

**Configuration.** The whole block is in `.env.example` under "the AI second
opinion". `GEMINI_ENABLED=false` or an empty `GEMINI_API_KEY` disables it
without touching anything else, and that is the supported way to take it out of
service.

**External limits you cannot code around.** The free tier's requests-per-minute
and requests-per-day are per project, are not guaranteed, and are only visible
in AI Studio — the numbers in `.env.example` are deliberately set *below* them.
Over quota is a `429 RESOURCE_EXHAUSTED`, which this layer treats as a transient
failure and then a skip. None of this can be verified from inside the bot, so
treat the counters as the real ceiling and watch `ai_usage`.

**The model default was chosen by measurement, and the first choice was wrong.**
The SDK documents `gemini-flash-latest` as the stable alias for the current
Flash model, so that is what this shipped with. Against this deployment's key it
answered **0 of 8** calls — `503 UNAVAILABLE ... currently experiencing high
demand`, `504 DEADLINE_EXCEEDED`, sustained over roughly 25 attempts. The layer
reported itself `active` and classified nothing, which is the same silent-total
failure shape as the deadline bug above.

`gemini-flash-lite-latest` answered **8 of 8** with no errors, and classified
every probe message correctly:

| Message | Verdict |
|---|---|
| «اینترنت ایرانسل وصل نمیشه» | `connectivity_problem`, **relevant**, `connectivity_offer` |
| «سلام کسی میتونه کمک کنه یه وی پی ان خوب معرفی کنه؟» | `vpn_request`, **relevant, offer**, `vpn_offer` |
| «سلام بچه ها، کی بازی دیشب رو دید؟» | `ordinary_conversation`, not relevant |
| «قیمتتون چنده؟» | `pricing_question`, **relevant**, `pricing_offer` |

Note the first and last rows. Both used to be `not relevant`: the prompt said a
complaint about a slow or down connection "is NOT a request", and pricing was
treated as a question rather than a lead. Both were changed on the owner's
instruction — a poor connection is to be *answered*, because the test answers
the question the person is actually asking ("is it my line or the route?"), and
somebody asking the price is a buyer. A connectivity complaint about the
speaker's own connection is now a lead. A general remark that the internet is
bad today, with no connection to the speaker's own line, is still
`ordinary_conversation`.

So `GEMINI_MODEL` defaults to the lite alias. For judging one short message it is
also the better tool — faster, which matters inside a 10-second message-handler
budget, and cheaper against the daily quota. Availability is per key and moves,
so **measure it again rather than assuming**; the model is one env var and needs
no rebuild.

### 13.8.1 The reply is chosen, not written

The model returns two presentation hints beside the verdict, and the second one
selects which of five fixed sentences the group sees:

| `response_kind` | When | Copy lives in |
|---|---|---|
| `connectivity_offer` | their connection is slow, unstable or down | `GROUP_TRIAL_REPLY_CONNECTIVITY` |
| `access_offer` | a named site or app will not open | `GROUP_TRIAL_REPLY_ACCESS` |
| `vpn_offer` | they ask for a VPN, proxy or configuration | `GROUP_TRIAL_REPLY_VPN` |
| `pricing_offer` | they ask what it costs | `GROUP_TRIAL_REPLY_PRICING` |
| `generic_offer` | none of the above | `GROUP_TRIAL_INVITE_TEXT` |

`problem_kind` is the coarser companion — `slow_or_unstable`, `blocked_service`,
`no_connection`, `wants_access_tool`, `price_only`, `none`. It is for the log and
for later analysis; nothing branches on it.

**The model picks a key and never writes a sentence.** Every word the group reads
is a constant in `app/config.py`, so a persuasive or confused answer can change
*which* of five sentences is sent and nothing else. It cannot introduce a URL, a
credential or an instruction, because there is no field to put one in —
`test_the_schema_offers_no_field_a_message_could_be_written_into` asserts that as
a *property* (exactly one free-text field, it is `reason`, it is truncated to 200
characters and is only ever logged) rather than as a fixed list of names, so a
future field cannot quietly become a channel.

**Strict on the decision, forgiving on the presentation.** `is_relevant` and
`needs_acquisition_offer` decide whether a stranger gets a trial, so a missing
one of those is a failure to answer and the whole verdict is discarded.
`problem_kind` and `response_kind` only choose between five sentences, so a
missing or invented one is coerced to `none` / `generic_offer` instead. Throwing
away a real lead because the model forgot a presentation hint would trade
something valuable for something cheap.

`responses.kind_for` re-checks the key against `RESPONSE_KINDS` even though
`parse_verdict` already coerced it. Not redundancy for its own sake: that value
is interpolated into a log line, and a string carrying a newline could forge one.

When the rules decided the message themselves there is no AI verdict to ask, so
the kind is derived from *which* patterns matched — `poor_internet` and `problem`
give `connectivity_offer`, `request` gives `vpn_offer`, a bare topic match gives
the generic wording. Coarser than the model and honest about it: the rules know a
pattern fired, not what the person is complaining about. That path is what keeps
the reply sensible with the AI layer off, out of quota or broken.

### 13.8.2 The `[intent]` line, and the defect it hid

One line per decision with both verdicts visible together, because the only
question that matters afterwards is "why did this message get an offer".

```
[intent] user=… triggered=True source=ai score=2 rules=request,problem,candidate
        ai_consulted=True ai_skip=- ai_error=- ai_category=vpn_request
        ai_problem=blocked_service ai_response=access_offer ai_confidence=0.95
        ai_reason=… text='…'
```

Every AI field is read through `ai is not None`, **never** `if ai`.
`AiVerdict.__bool__` reports *relevance*, so truthiness blanked the whole AI half
of the line on exactly the verdicts an investigation wants — the ones where the
model was asked and said no. A live `200 OK` answering `ordinary_conversation`
printed as `ai_consulted=False ai_category=-`, which reads as "never asked": the
opposite of what had happened. It was found in production against a successful
call, not by the suite, because the suite replaces `_request`.
