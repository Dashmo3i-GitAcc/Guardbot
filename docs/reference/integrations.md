# Integrations: the capability registry, the VPN surface, the key control plane

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is verbatim; it was not edited during the move.

## Contents

- [46. Integrations: what exists, what does not, and saying so](#s46)
- [49. The VPN operational surface: powerful, and under the owner's hand](#s49)
- [50. The owner's credential control plane](#s50)

---

<a id="s46"></a>

## 46. Integrations: what exists, what does not, and saying so

`app/service_adapters.py` is a **capability registry**, not an integration. It
exists because the failure mode of the alternative is worse than not having the
feature: an assistant that *claims* it can build a configuration, on a
deployment whose upstream API has no such endpoint, will promise an operation in
front of a customer and then fail.

The three states, and they are different:

* **available** — configured, and the operation is implemented.
* **unconfigured** — implemented, but this deployment has not pointed the bot at
  a backend.
* **absent** — there is no implementation at all.

What is actually true on this deployment:

| integration | state | operations |
|---|---|---|
| VPN bot (`app/vpnbot.py`) | available when `VPNBOT_API_URL` and `VPNBOT_SHARED_SECRET` are set | `health`, `status`, `acquisition.invite`, `subscription.lookup`, `service.status`, and six owner-only administrative writes — see §48 |
| OpenVPN | **absent** | none — no integration exists |
| TQI panel | **absent** | none — this bot holds no panel credentials |
| coding agent (`app/agent_bridge.py`) | available when `AGENT_ENABLED` and a repository allowlist are set | `task.submit/status/confirm/cancel` |

The VPN bot's internal API exposes a health probe, an acquisition invite, two
lookups and six administrative writes. It does **not** expose user records or
configuration generation to this bot, so those are listed as `unsupported` and
the assistant is told to explain the gap rather than improvise around it. No
endpoint was invented: the brief's own rule — inspect the real API, do not
invent one — is the rule this module follows. The list is asserted against the
paths `app/vpnbot.py` actually implements, so a name in the report with no code
behind it fails the suite.

**The acquisition flow being switched off no longer reports the whole
integration as dead.** It used to, and that stopped being true once the reads
and the writes existed: they do not go through the acquisition path and they
work either way. The integration is now reported for what it is, with the
individual operation named in `disabled_operations` beside it — "nothing here
works" and "this one thing is off" are different answers, and an operator acting
on the first when the second is true goes looking for a fault that does not
exist.

The shared secret is read only to decide *whether* the client is configured — a
boolean — and `test_service_adapters.py` asserts it cannot appear in the report.

---

<a id="s49"></a>

## 49. The VPN operational surface: powerful, and under the owner's hand

### 49.1 What was asked, and the four pieces it became

The owner's decision was explicit: Nexus should have **real operational reach
over the VPN project** — full administrative capability where the project needs
it — while every execution stays under server-side authorisation, and only the
Owner can issue a sensitive command. Four independent pieces:

1. **VPN reads** — subscription, service and integration status.
2. **VPN writes** — six operations, three of which move money or bulk-reject
   orders and sit behind an explicit owner confirmation.
3. **A closed escalation path** — §48.
4. **Awareness gets its own credential** — §35, and it no longer falls back to
   the chat pool.

Plus the standing requirements: one central gateway, RBAC, an operation
allowlist, an audit log, and fail-closed authorisation.

### 49.2 There is no second gateway

`app/admin_service.py` **is** the gateway. The brief forbids a parallel
architecture, so nothing here adds one. The precedent is `codebuddy_task` →
`agent_service.submit(request)`, which returns an `AdminResult` from inside the
one pipeline; the VPN operations follow it exactly. `app/vpn_service.py` holds
no authority of its own, is never called by a Telegram handler, and cannot be
reached except through `admin_service.execute` — which is where an actor id
becomes an authority and where the audit row is written.

The seven new operations (`vpn_service_enabled`, `vpn_notifications`,
`vpn_plan_active`, `vpn_balance`, `vpn_orders_sweep`, `vpn_transaction_status`,
`vpn_confirm`) are ordinary entries in the one `OPERATIONS` table, with a new
`OP_VPN` kind. That kind is not a validation branch — a plan id is not a
Telegram member and checking it against the member list would be meaningless —
it is the branch that *skips* the user and message checks and adds the one
pre-flight that is meaningful: **an operation against an integration this bot
has not been pointed at is refused before anything is recorded**, rather than
discovered later as an unreachable host. Either way it lands in `admin_audit` as
a refusal.

### 49.3 Owner-only, permanently, and structurally

`vpn.read` and `vpn.manage` are appended to `PERMISSIONS` — last, because
`main.py` uses that tuple as a **positional bitmask** and inserting in the middle
would silently renumber every stored permission — and to
`OWNER_ONLY_PERMISSIONS`. Neither appears in any `ROLE_PERMISSIONS` bundle.

The consequence is worth stating plainly: "an administrator edits a customer's
balance" is not refused, it is **inexpressible**. There is no role an
administrator can be promoted to that carries the permission, and §48's test
proves no bundle carries an owner-only permission. So the answer to "could a
sufficiently senior administrator do this?" is no at the level of the role
table, not no at the level of a check somebody has to remember to write.

### 49.4 The two-step write, and why the second step is a reference

`vpn_balance`, `vpn_orders_sweep` and `vpn_transaction_status` are recorded and
**not executed**. `app/vpn_service.py` writes a row into `vpn_pending_ops` and
returns `OUTCOME_VPN_AWAITING_CONFIRMATION`, which is deliberately *not* a
success and deliberately not in `_REFUSAL_OUTCOMES` either — it is a state, and
counting it as a failure would send the owner hunting for a problem that does
not exist.

The property that makes the second step worth having is that **the confirmation
is a reference, not an approval**. Everything the execution needs is re-read
from the row written the first time, by the gateway, after authorisation:

* the model supplies `pending_id` and nothing else;
* `AdminRequest.pending_id` is named differently from `request_id` on purpose,
  because `request_id` is already the replay key and one name for two things is
  how a replay key becomes a token;
* `app/vpn_service.py` rebuilds the operation from the stored JSON payload, so a
  confirmation carrying a different amount, a different user or a different
  transaction cannot smuggle any of them in. `test_the_stored_payload_is_what_runs_not_anything_the_confirmer_supplies`
  builds exactly that forged request and asserts the stored values are what
  reach the VPN bot.

Confirmation is an ordinary operation in the table (`vpn_confirm`), not a
special case outside it. That is the security choice: the second half of a money
operation is authorised by the same seven steps as the first half — shape,
system state, replay, target, RBAC, rights, call. It is also a separate *tool*,
and `vpn_admin` refuses `vpn_confirm` as a value for its `operation` parameter,
so a single tool call can never both ask for a money operation and approve it.

The four confirmation rules are **not reimplemented**. They live in
`agent_bridge.resolve_confirmation` — only the owner confirms, there must be
something pending, a named reference must really be waiting, and a bare
confirmation resolves only when exactly one thing is — and a second copy of that
reasoning would be a second answer to "who may approve". The two flows therefore
cannot drift. The waiting list is scoped to the room the operation was asked in,
which is the fail-closed direction: the wrong answer is "nothing is waiting",
never "here, the other group's operation".

The claim is a compare-and-swap on one row, like the update-dedup claim and for
the same reason: two confirmations arriving together must not both execute. It
is taken *before* the call and **released again only when the failure was a
transport one** — a refusal from the VPN bot is a decision, and re-asking would
produce the same answer.

`vpn_pending_ops` is bounded by `db.vpn_pending_prune`, applied from this path on
`VPN_PENDING_RETENTION_SECONDS` (see `admin-and-audit.md` §29.11). The rule is
narrow on purpose: it drops a finished receipt past the window, and an operation
whose own `expires_at` has passed — which `vpn_pending_claim` already refuses — and
it never touches a row that is still confirmable. The window is measured from the
operation's expiry rather than from its creation, so a rule about disk space
cannot delete a money operation the owner is in the middle of approving.

### 49.5 Fail-closed, in four outcomes rather than one

`OUTCOME_VPN_UNAVAILABLE`, `OUTCOME_VPN_REFUSED`, `OUTCOME_VPN_ERROR` and
`OUTCOME_VPN_AWAITING_CONFIRMATION`, because they are four different next steps
for the owner:

* **unavailable** — the integration could not be reached at all (not configured,
  unreachable, or its own write switch is off). Look at the wiring.
* **refused** — it answered and the answer was no. Look at the request.
* **error** — something on our side of the wire was malformed. Look at this bot.
* **awaiting confirmation** — nothing ran, and the next step is the owner's own
  approval.

The rule the brief cares about is that **an unconfigured or unreachable VPN bot
is recorded as a refusal in `admin_audit`, never reported as done.** Two tests
assert the audit row, one for the immediate path and one for the two-step path —
the second is the one that matters, because it is the path where "it went
through" would be most plausible and most damaging.

### 49.6 The redactor is local, and that is the point

A VPN service is described by a *connection string*, and that string is the
credential: `vless://…` and its siblings carry the client id in the fragment,
and a subscription link carries it in the path. So the VPN reads need patterns
the generic redactor does not have — and they are **not** added to
`agent_bridge._SECRET_PATTERNS`.

The reason is that a bare 32-hex rule is right for a panel client id and wrong
for this bot's own identity handle, which is 32 lowercase hex characters and is
*deliberately* a non-secret — it is how a person is addressed (§44). A global
rule would quietly rewrite it everywhere and break the thing the identity layer
exists to provide. `agent_data.redact_vpn` composes the generic redactor with the
VPN patterns, and `test_the_vpn_redactor_is_not_the_global_one` asserts both
halves: the VPN redactor removes such a handle, and `identity_view` still returns
it.

Redaction is the *second* line of defence. The first is that the views copy an
allowlist field by field and never `**raw`, so a field that is never copied
cannot be leaked by a redactor that misses it. The VPN bot narrows the same
object on its own side before serialising, including on **write** responses —
a write response that embeds a subscription link leaks exactly as much as a read
does — so the two narrowings are independent and either one alone would hold.

### 49.7 The tool set: five declarations, and the cost of them

Three reads (`vpn_subscription_lookup`, `vpn_service_status`, `get_vpn_status`),
one write tool (`vpn_admin`) and one confirmation (`confirm_vpn_operation`).

`vpn_admin` carries an `operation` parameter naming which of the six changes is
wanted, rather than being six near-identical tools. The vocabulary is closed in
`_enum_for` *and* checked again in `parse_write_call` *and* checked a third time
by `execute` — the same belt-and-braces the `role` parameter gets. Six separate
tools were measured as more expensive, and a set of terse descriptions that omit
the argument-to-operation mapping was rejected: the rule here is to record the
measurement and the reason rather than to trim descriptions to letters.

The cost was measured. Tool declarations went from 42794 characters to **54141**,
so the ceiling in `tests/test_awareness_latency.py` was raised from 48000 to
62000 deliberately, with the measurement and the reasoning written into the
test. In practice these five are the cheapest kind of growth: `vpn.read` and
`vpn.manage` are carried by no role bundle, so they are attached for the owner
and for nobody else, and no administrator's or member's message pays for them.

None of the new tools declares a parameter in the forbidden set (`actor_id`,
`chat_id`, `is_owner`, `permissions`, `owner`), so the every-tool invariant test
still passes — and `test_no_vpn_tool_can_name_an_actor_a_room_or_a_permission`
asserts it for these five specifically. The actor and the room come from the
caller, never from the arguments.

### 49.8 The other side of the wire

The VPN bot (`/opt/vpn-bot`, a separate repository on its own branch) gained
eleven internal endpoints: the health probe and acquisition invite it already
had, a config-only status read, two lookups, and six administrative writes. They
inherit its existing four-gate `guard_middleware` — CIDR, rate limit, HMAC,
handler — and reuse its `service_auth`.

One constraint shaped every signature: the HMAC covers
`method\npath\ntimestamp\nnonce\nSHA256(body)` and the verifier uses
`request.path`, which **excludes the query string**. A parameter sent as
`?telegram_id=` would therefore sit outside the signature and a captured request
could be replayed with a different id, so every parameterised endpoint is POST
with a signed JSON body. There are no query parameters anywhere.

Application outcomes are 200 with `ok: false` and a machine `code`
(`not_found`, `panel_error`, `trial_locked`, `invalid_amount`, …); only endpoint
problems are non-200. So a refusal reaches guardbot as a precise outcome rather
than as a generic "unreachable", and `admin_disabled` — the VPN bot's own write
kill switch, `INTERNAL_API_ADMIN_ENABLED`, default off — is reported as
*unavailable* rather than *refused*, because the next step is to look at the
VPN bot's configuration rather than at the request.

Every write records the acting operator in the VPN bot's **own** audit table as
`actor=f"guardbot:{operator_id}"`, the parallel of the dashboard's existing
`actor=f"dashboard:{user}"`. A service response that embeds the affected service
is narrowed through the same allowlist a read uses, because a write response
leaks exactly as much as a read.

### 49.9 The honest limitation

`operator_id` is **asserted** by guardbot and not independently verified by the
VPN bot. The HMAC proves which *service* asked; the VPN bot trusts guardbot's
RBAC for which *person* was allowed to. No new configuration surface was added
to pretend otherwise, and this is written down rather than papered over.

The second honest limitation is the shape of the risk itself: the shared secret's
power has expanded from "may ask whether to invite somebody" to "may write". The
mitigations are the owner-only RBAC, the confirmation step on the three
unrecoverable operations, the VPN bot's independent kill switch, and narrowing
`INTERNAL_API_ALLOW_CIDRS` to `127.0.0.1/32` — guardbot runs with
`network_mode: host`, so it does not need the Docker bridge range.

### 49.10 Configuration

| variable | default | what it does |
|---|---|---|
| `VPNBOT_API_URL` | *(none)* | the VPN bot's internal API. Empty disables every VPN operation |
| `VPNBOT_SHARED_SECRET` | *(none)* | the HMAC secret; must match the VPN bot's `SERVICE_SHARED_SECRET` |
| `VPNBOT_TIMEOUT_SECONDS` | `8` | per-request timeout |
| `VPN_CONFIRMATION_TTL_SECONDS` | `900` | how long a recorded operation stays confirmable |
| `INTERNAL_API_ADMIN_ENABLED` | `0` | **on the VPN bot** — closes its write surface independently |

Rollback needs no code change on either side: `INTERNAL_API_ADMIN_ENABLED=0`
closes the write surface, and unsetting `VPNBOT_API_URL` closes all of it.

### 49.11 Tests

`tests/test_vpn_admin.py` (57) covers authority — every one of the seven
operations refused for an administrator, with the VPN bot never reached — the
fail-closed audit rows, the two-step write, the reference-not-payload property,
the ambiguity rule, expiry, single-use confirmation, and the request boundary
(undeclared arguments, unknown operations, a string where a boolean belongs, the
operation tables agreeing).

`tests/test_vpn_tools.py` (27) covers the reads: owner-only exposure *and*
server-side refusal, the connection string absent from every answer, the
redactor's locality, and the capability report.

The VPN bot's own `tests/test_internal_api.py` enumerates every route and asserts
each one is behind the signature check, that a lookup returns no `sub_url` and no
`vless://`, that an unknown user is a 200 decision rather than an error, that a
write is audited as `guardbot:<id>` under the right action, and that the write
surface is closed when its kill switch is off.

---

<a id="s50"></a>

## 50. The owner's credential control plane

Until now every Gemini credential came from `.env`. That is a good default — a
secret in a file the process reads at boot is the easiest thing in the world to
audit — but it made one operation impossible from where the owner actually is:
giving a workload a new key meant editing `.env` on the host and restarting the
container. `/keys` is the smallest thing that fixes that, and it is a control
plane rather than a prettier `/pool` because it can *write*.

### 50.1 What it is not

It is not a second pool. There is one pool (`app/gemini_pool.py`), one registry,
one set of counters, one events table. If a number on a dashboard screen
disagrees with `/pool`, that is a bug in the dashboard and nothing else.

It is not a web dashboard. It is Telegram inline keyboards, because that is where
the owner is, and because a second HTTP surface with its own authentication is a
much larger thing to get right than a callback handler behind the authority model
that already exists.

It is not a menu bolted onto `/pool`. `/pool` is a dump for somebody who already
knows what they are looking at; these are the questions the owner actually asks,
one at a time.

### 50.2 Where a credential added from Telegram lives

One file: `GEMINI_KEY_STORE_PATH`, default `/data/gemini_keys.json`, mode `0600`,
inside the data volume so it survives a container rebuild.

**It is plaintext on disk, and that is stated rather than dressed up.** The brief
asked for no plaintext secrets, and the honest reading of that is: not in the
database, not in the audit trail, not in a log line, not in a Telegram message,
not in a rendered screen. Those are all true and all tested. Encryption at rest
was the alternative and it was declined, deliberately:

* SQLite cannot hold a value the process cannot read back, so "encrypted in the
  database" means the decryption key is also in the environment — a lock with the
  key taped to it;
* the project has no existing at-rest secret mechanism, and the brief says not to
  invent an encryption scheme casually;
* the boundary that actually protects the credential is the file mode plus the
  container, and that boundary is real and is asserted by a test.

The database deliberately does not hold it because the database is the thing
operators copy, back up and attach to support tickets. `gemini_accounts` keeps
the `fingerprint` and the `masked` tail, exactly as it already did.

### 50.3 The pool stays the single source of truth

`build_pools()` reads the environment's key list and then appends whatever
`key_store` has for that workload:

```python
workload = spec["workload"]
keys = list(spec["keys"])
keys.extend(key_store.slots_for(workload))
```

Environment slots come first, so a credential written down at deployment time
stays the primary one and is not demoted by something added later from a phone.

`gemini_pool.reload()` is what makes the dashboard a control plane: it drops the
cached SDK clients and rebuilds the registry. Every workload asks for its pool
through `pool_for()` on each request and reads that registry, so the next message
that needs an answer already sees the new account list. No restart is involved,
and there is no second copy of the account list to keep in step.

A request already in flight holds a reference to the old pool and finishes
against it. That is intended: it is one answer computed with the credentials that
were valid when it started.

### 50.4 Three workloads are writable, and the rest are not

```python
GEMINI_KEY_MANAGED_WORKLOADS = frozenset({"chat", "awareness", "intent"})
```

`moderation`, `transcribe` and `tts` appear in the dashboard read-only. They are
visible so nothing is hidden; they are not writable because the owner is rotating
three keys, not six, and a write surface that is larger than the job is a
liability rather than a feature.

This is a closed set rather than an environment variable on purpose: a typo in an
env var could widen the write surface, and the set *is* the write surface.
`key_store.is_managed` refuses every write for a workload outside it, and
`key_store.slots_for` returns nothing for one — so even a hand-edited store file
containing a `moderation` row cannot widen moderation's pool. Both directions are
tested.

### 50.5 The entry flow, and the isolation guarantee

Adding a key is the one operation that cannot be a callback, because a callback
payload cannot carry a secret. The flow is:

1. the owner presses **➕ افزودن کلید** on a workload screen;
2. in a **private chat** the prompt is armed (`gemini_keys.begin_add`); in a group
   it is refused, because a key typed into a group has already been published;
3. the owner sends the key as a plain message;
4. `on_key_message` deletes that message, answers, and hands the verification to a
   task;
5. the task verifies, stores, calls `gemini_pool.reload()`, and edits the notice
   with the result.

The handler is registered in **group 0**, ahead of the assistant's private-chat
handler in group 2, and it raises `ApplicationHandlerStop` once it has taken the
message. That is the isolation requirement and it is structural rather than a
convention: `app/main.py`'s `on_private_text` would otherwise hand a pasted key to
the conversational model, and the only way to prevent that reliably is to stop the
update before that group runs. `tests/test_gemini_keys.py` asserts the stop, not
just the deletion.

The verification is **not awaited in the handler**. `models.list` is a network
round trip; a handler that blocked for fifteen seconds would stop every other
update in the bot. The handler returns immediately and the work runs as its own
task.

Three guards keep the handler from interfering with ordinary private chat:

* it returns unless the sender is the owner;
* it returns unless a prompt is armed for that owner, and a prompt expires after
  `GEMINI_KEY_ADD_TTL_SECONDS`;
* a message that is plainly conversation — anything with a space in it, or under
  twenty characters — is left alone *and the prompt stays armed*. A prompt that
  hijacked the next thing the owner typed would be worse than one that expired.

A single long token that is not a credential is answered with
`TEXT_ADD_BAD_SHAPE` and is not consumed, because otherwise a mistyped key looks
exactly like nothing happening.

### 50.6 Authority is re-decided on every press

`cmd_keys` and `on_key_callback` both resolve the actor with `rbac.resolve` and
refuse unless `is_owner`, before the payload is parsed. `key_store` then refuses
the workload as well. A crafted payload can therefore choose *which screen opens*
— a workload name and a slot that must already exist — and nothing else. A
non-owner's press is audited as `keys.view` with a refusal outcome, exactly like
every other refused administrative action.

### 50.7 A credential is verified before it is stored

`gemini_pool.probe_credential` calls `models.list` — the same call model discovery
already makes. It authenticates the credential and consumes no generation quota,
which matters because the whole point of adding a key is that the existing ones
are running out.

The result splits two ways, and the caller says which:

| provider said | stored? | what the owner is told |
|---|---|---|
| the key is not valid (`invalid_credential`) | no | سرویسدهنده این کلید را نامعتبر میداند |
| no usable model (`unsupported_model`) | no | این کلید به هیچ مدل قابل استفادهای دسترسی ندارد |
| rate-limited, quota, 5xx, timeout, network | no | the reason, in words, and "try again" |

Nothing is stored on a failure. That is fail-closed, and it is the direction the
rest of this project already fails in: an unverifiable credential is not a
credential. The client is built with `build_client` rather than `client_for`, so a
rejected key's client is never left in the process-lifetime cache — asserted by a
test.

### 50.8 What the numbers on the screens mean

The usage screen states this outright rather than leaving it to be inferred:

* every number is a **provider request**, not a user message. One logical request
  may be tried on several models and several accounts and each attempt is counted,
  so "requests" can exceed "answers". This is the same distinction that produced
  the "75% failure" misreading of the intent workload — see §28.13.
* **token usage is not tracked**, and no number is invented for it. The provider
  does not publish token counts for these keys, and a number that is not a
  measurement is worse than a stated absence.
* **remaining quota and reset times** are shown only when an error response
  actually carried them, which is the rule the pool has always followed.

### 50.9 What never appears anywhere

* the database — asserted by dumping the audit rows and the account rows after a
  real add;
* `admin_audit` — the row carries `workload/slot` and the masked tail;
* any log line — `Entry.key` is `repr=False` so a future `log.info("%s", entry)`
  cannot leak it, and the store logs only `slot` and `masked`;
* any screen — asserted for every screen with a credential in the pool;
* the probe's error detail — redacted through `gemini_pool.redact`, because a
  provider body is the one place a credential could plausibly be echoed back.

### 50.10 Settings

| variable | default | what it does |
|---|---|---|
| `GEMINI_KEY_STORE_PATH` | `/data/gemini_keys.json` | the credential file. Must be inside the data volume or a container rebuild loses it |
| `GEMINI_KEY_ADD_TTL_SECONDS` | `300` | how long a "send me the key" prompt stays armed |
| `GEMINI_KEY_PROBE_TIMEOUT_SECONDS` | `15` | the deadline on the one verification call |
| `GEMINI_KEY_MAX_PER_WORKLOAD` | `10` | ceiling on runtime credentials per workload |
| `GEMINI_KEYS_COMMAND` | `keys` | the command name |

`GEMINI_KEY_MANAGED_WORKLOADS` is deliberately **not** a setting — see §50.4.

Rollback needs no code change: deleting the store file returns every workload to
its environment-only pool, which is exactly the behaviour before this section
existed.

### 50.11 Tests

`tests/test_gemini_keys.py` (108) covers the store (shape, `0600`, atomicity,
idempotence, the unmanaged-workload refusal, refusing to overwrite an unreadable
file, concurrent writers, `repr` not rendering the credential), the pool wiring
(runtime credentials joining and leaving the live pool, environment keys staying
first, a store row unable to widen an unmanaged workload, a broken store not
stopping `build_pools`, counters surviving a reload), the probe (valid, invalid,
unreachable, no usable model, redaction, no cached client for a rejected key),
every screen (rendering, length, no credential, the remove button only for
runtime credentials, the empty-workload warning), the payload parser, the prompt
lifecycle, discoverability (§50.12), and the handlers end to end — including the
three isolation properties: the stop, the deletion, and that a non-key message is
left to the dispatcher.

`tests/test_gemini_pool.py` gained the `reload`/`probe` seams; the whole suite is
2070 passing.

### 50.12 Discoverability: the menu, and the button

The control plane shipped working and unfindable. The owner reported "no button
has been set up in the bot" and cleared their chat history looking for it. The
diagnosis was not a rendering problem in this code at all: **the bot had never
called `setMyCommands`**, so Telegram's command list for it was empty
(`getMyCommands` returned `[]`), and Telegram therefore rendered no menu button
and no command list. Every command — `/keys` included — was reachable only by
somebody who already knew it existed. Clearing a chat's history does not change
that: the menu is served from Telegram's own state, not from the chat.

Two things were added, and they are deliberately not the same thing:

* **`_publish_command_menu(app)`**, called from `post_init`, tells Telegram the
  command list for **two scopes**. Everybody gets the commands anybody may use;
  the owner's chat additionally gets the administrative ones. `/ban`,
  `/promote` and `/keys` are *not* published to every chat — advertising the
  moderation surface to the people it is aimed at, and telling a stranger the bot
  has a credential dashboard, are both the wrong thing to do. The owner's list
  is scoped with `BotCommandScopeChat(chat_id=owner_id)`, so it lands in the
  owner's chat and nowhere else.
* **A button on `/start` and `/whoami`**, because a menu has to be *noticed*
  before it can be used, and those are the two screens a person actually lands
  on. The button carries `gk:home` — the same screen `/keys` opens — so it is an
  entry point, not a second implementation. It is built by
  `_owner_menu_keyboard(actor)`, which returns `None` for anybody who is not the
  owner, so the two callers do not each have to remember the check.

**The menu cannot drift from the registrations.** `main()` no longer contains a
literal list of commands. It registers from `admin_command_handlers()`,
`chat_command_handlers()` and `transcribe_command_handlers()`, and the published
menu is derived from those same three functions. A menu written out separately
would eventually name a command that does not exist, and a dead menu entry is
worse than no menu at all — tapping it does nothing, and "the bot is broken" is
the only reasonable conclusion. Two consequences fall out of this for free:
`/start` and `/reset` disappear from the menu when `GEMINI_CHAT_ENABLED` is off,
and the voice command follows `TRANSCRIBE_COMMAND`, because in both cases the
menu asks the function that registers the handler rather than assuming.

Publishing is **best effort and never fatal**. It runs inside `post_init`, so
anything escaping it would stop the bot from starting; a cosmetic menu is not
worth an outage. The catch is therefore deliberately broad rather than
`TelegramError`, and it logs a traceback so a real bug is still visible.

One filter change belongs to this section rather than to §50.5: the credential
entry handler now uses `key_entry_filter()`, which excludes `filters.COMMAND`
(alongside the private-chat and not-an-edit conditions). A command is a command
even with a prompt armed — `/keys` re-opens the dashboard rather than being
weighed as candidate key material. Today the store's shape check would reject a
command anyway; the point is that "the owner's own commands are never read as a
credential" should not depend on that check staying strict. The filter is a
named function for the same reason the other handlers' filters are: the test
asserts the registered filter, not a copy of it.
