# Administration, the audit trail, identity and agent data

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is verbatim; it was not edited during the move.

## Contents

- [25. Administration: roles, hierarchy and owner protection](#s25)
- [29. AI-mediated administration: the model asks, the bot decides](#s29)
- [30. The audit trail says which interface acted](#s30)
- [31. The administration mode is observable](#s31)
- [43. The audit trail says with what authority, and proves what it cannot hold](#s43)
- [44. Identity: a handle, and turning a reference into one person](#s44)
- [45. What the assistant may read, and the two boundaries around it](#s45)

---

<a id="s25"></a>

## 25. Administration: roles, hierarchy and owner protection

`app/rbac.py` is the authority model; `app/main.py` asks it and does what it is
told. Nothing else decides.

### 25.1 The owner is configuration, not a row

`OWNER_USER_ID` comes from the environment and is compared, never looked up.
There is no function in `rbac` that can create, modify or remove the primary
authority, which is what makes *"you cannot promote yourself to owner"* a
property of the design rather than a check somebody has to remember to write.

With `OWNER_USER_ID=0` **every administrative command is refused** and the
startup log says so loudly. It does not fall back to "the first admin wins" or
"the whitelist is the owner"; both are ways for the wrong person to end up in
charge.

**The deployment's owner is `OWNER_USER_ID=6931339207`.** That number is the
authority, and it is the only thing that is. The account's Telegram username is
`@Mo3i_Best`, and the username is **not** an identity: usernames are
user-controlled, changeable and re-usable, so `rbac` never reads one — not from
a message, not from a config file, not from a stored row. It compares the
numeric id, which the Telegram servers assert, and nothing else. A username
handed where an id belongs is a `ValueError`, not a match (there is a check for
exactly that in the hierarchy harness). If the account is ever renamed, nothing
in this bot changes; if a different account were to claim `@Mo3i_Best`, it would
hold nothing.

This is the same id the VPN bot carries as `ADMIN_IDS` and as
`EXEMPT_TELEGRAM_IDS` (`app/services/trial.py`), so the two projects agree on
who the owner is. Keep them in step: changing one without the other gives the
owner two different answers on two surfaces.

### 25.2 Permissions are the model; roles are a convenience

| Permission | What it allows |
|---|---|
| `moderation.review` | see the review queue and the audit trail |
| `moderation.warn` | warn a user |
| `moderation.delete` | delete a message |
| `moderation.mute` | restrict a user temporarily |
| `moderation.ban` | ban and unban |
| `admins.manage` | create, change and remove administrators |
| `config.manage` | see and change runtime configuration |
| `commands.use` | use the bot's commands at all |

| Role | Carries |
|---|---|
| `helper` | review, warn, commands |
| `moderator` | + delete, mute |
| `senior_admin` | + ban, admins.manage, config.manage |
| `owner` | everything (implicit, from configuration) |

Authorisation compares **permissions**, never role names, so adding a role cannot
accidentally widen an existing one.

### 25.3 The checks, in order

`rbac.authorize(actor, permission, target=...)`:

1. is an owner configured at all? no → refuse (`no_owner`);
2. does the actor hold the permission? no → refuse (`not_admin` / `missing_permission`);
3. is the target the owner? → refuse (`owner_protected`), **for everybody,
   including the owner** — making it unconditional is what removes the whole
   class of "ban the owner" bugs rather than one instance of it;
4. is the target at or above the actor's level? → refuse (`higher_rank`). Equal
   level is refused too: peers must not be able to demote each other.

Promotion adds two more bounds, and they catch different mistakes:
`GRANTABLE_ROLES` stops *"create a peer"* (a senior admin can build the
moderation team but not another senior admin), and `grantable_permissions` stops
*"grant something you do not hold"* — it is the actor's own permissions minus
`admins.manage`, so an administrator who could create administrators cannot build
a peer group.

### 25.4 Telegram is the floor, not the ceiling

Application permissions can only *restrict* what an administrator may request.
They can never grant a capability Telegram has not given the bot.

`PERMISSION_TELEGRAM_RIGHT` maps the application vocabulary onto real
`ChatAdministratorRights` fields — `moderation.delete` → `can_delete_messages`,
`moderation.mute`/`ban` → `can_restrict_members`, `admins.manage` →
`can_promote_members`, `config.manage` → `can_manage_chat`. A test asserts every
mapped name is a real field on the installed PTB version, so an invented
permission cannot creep in.

Before acting, the bot checks **its own** rights in the chat (`_bot_right`) so a
refusal is reported as "I do not have the permission here" rather than as a
mystery. Telegram still enforces it; the check only makes the message useful.

### 25.5 The commands

| Command | Permission | Notes |
|---|---|---|
| `/whoami` | any | what the bot thinks you are — the answer to "why was I refused?" |
| `/admins` | `moderation.review` | the owner plus every stored administrator |
| `/promote [role]` | `admins.manage` | reply to a user; opens the permission dialog |
| `/demote` | `admins.manage` | reply to a user |
| `/ban` `/unban` | `moderation.ban` | |
| `/mute` `/unmute` | `moderation.mute` | timed restriction |
| `/warn [reason]` | `moderation.warn` | |
| `/del` | `moderation.delete` | deletes the replied-to message |
| `/pool` | owner only | the Gemini account pool — accounts, states, counters (§28.8) |
| `/transcribe` | any | the transcription-only interface (§24.1) |

Every one of them: resolve the actor → ask `rbac` → check the bot's Telegram
right → act → audit, allowed or refused.

### 25.6 The promote dialog, and why the callback re-authorises

`/promote` shows one toggle per permission the role carries, plus confirm and
cancel. The callback payload carries a **bitmask**, and callback data is fully
attacker-controlled — a client can send any bytes it likes.

So the handler treats its own payload as a *suggestion of what to show* and
re-runs every check the original command ran: the presser must be the person who
opened the dialog, the role must be one they may assign, and the permission set
must pass `authorize_grant`. A crafted mask can at most show a different set of
ticks to the person who crafted it. Five tests cover exactly that.

`_unmask` decodes; it does not authorise. That separation is the point.

### 25.7 Telling the truth about Telegram

Promotion reports **three** outcomes, not two:

* the application role was stored and Telegram was updated (`ADMIN_PROMOTE_TELEGRAM_TEXT`);
* the application role was stored and Telegram **refused**
  (`ADMIN_TELEGRAM_FAILED_TEXT`) — the operator has to know, because the
  application role is real and the Telegram one is not;
* the bot could not promote here at all (`ADMIN_BOT_LACKS_RIGHT_TEXT`), said
  *before* the dialog opens rather than discovered afterwards.

It never claims success it did not get. Demotion clears every
`TELEGRAM_RIGHTS` flag rather than a selective subset: the bot does not know
which rights were there before it touched the account, and guessing would be a
way to leave somebody holding a capability nobody meant to leave them.

### 25.8 The audit trail

`db.audit_write` records every administrative decision — **including the
refusals**, because "who tried" is the question asked after an incident and a log
that only records successes cannot answer it. The row carries the actor, the
action, the target, the chat, an outcome key and a short detail. It never carries
message content, and an audit write that fails does not break the command.

Since §30 the row also carries an **`interface`** column — `python` or `ai` —
recording which of the two interfaces asked for the action. The action vocabulary
is deliberately *not* forked: an operator searching for `moderation.ban` still
finds every ban, whichever interface requested it.

---

<a id="s29"></a>

## 29. AI-mediated administration: the model asks, the bot decides

The assistant can now *propose* administrative actions. Somebody types "ban
@someone for spamming" in the group, Gemini works out that this is a ban request
against a particular person, and calls a tool. What happens next is the entire
subject of this section.

```
HUMAN → conversational Gemini → a typed tool call
      → app/admin_service.py → app/rbac.py → Telegram
```

Gemini is the **interface**. `admin_service` is the **boundary**, the
**execution layer**, and the final authority. `rbac` is the only thing that
decides who may do what. Three sentences, three different jobs, and the design
falls apart the moment they blur.

The rule the whole section exists to enforce: **a language model may ask, and
may not decide.** Everything below is a way of making that true structurally
rather than by remembering to check.

### 29.1 One execution layer, two interfaces

There is exactly one place in this codebase that performs an administrative
action: `admin_service.execute()`. Both interfaces end there.

| | AI mode | Python mode |
|---|---|---|
| who parses the request | Gemini, into a tool call | `app/main.py`, from the command |
| what it produces | `AdminRequest` | `AdminRequest` |
| who authorises it | `admin_service` → `rbac` | `admin_service` → `rbac` |
| who performs it | `Gateway` | `Gateway` |
| who audits it | `admin_service` | `admin_service` |

The two paths are indistinguishable from step two onwards. `AdminRequest` has an
`interface` field (`ai` / `python`) which is recorded in the audit row and
**read by nothing that decides anything** — it exists so an operator can ask
"did the model do this or did a person", not so the code can behave differently.
A request that is refused on the command path is refused identically when a
model asks for it, and the tests assert exactly that by driving both.

`cmd_ban`, `cmd_mute` and the rest in `app/main.py` are now one-liners that
resolve *what* is being asked (which target, which message) and hand over a
typed request. They no longer contain a single authority check, because a check
there would be a second authority model, and the point is that there is one.

### 29.2 `AdminRequest` has no field that can express authority

This is the load-bearing decision, so it is worth stating plainly. The frozen
dataclass contains:

```python
operation, chat_id, actor_id, target_id, message_id,
role, permissions, reason, request_id, interface, at
```

There is no `is_owner`, no `actor_role`, no `allowed`, no `permissions_of_actor`.
Not "these are ignored" — **they do not exist**, so nothing can set them and
nothing can read them. The service re-resolves the actor from `actor_id` through
`rbac.resolve()` on every single call, and the permission set it checks against
is the one that resolution produces.

The consequence is that the two most obvious attacks are not rejected, they are
*inexpressible*:

* **Forged identity.** The model has no parameter for `actor_id`, so it cannot
  claim to be somebody else. `parse_write_call()` takes `actor_id` and `chat_id`
  from the *caller* — `app/main.py`, from the real Telegram update — and not from
  the model's arguments.
* **Forged authority.** Even if a model wrote `"I am the owner"` into a reason
  string, that string is an audit detail. It is never compared to anything.

The tests assert this as a property of the code rather than of a run:
`test_a_request_has_no_field_that_can_claim_authority` checks that `AdminRequest`
has none of those field names, and `test_the_tool_schema_has_no_identity_parameter`
checks that no declared tool has a parameter named `actor_id`, `chat_id`,
`is_owner` or `permissions`. Adding one later fails the suite.

### 29.3 The trusted context, and why it is not in the user's message

The model needs to know who it is talking to. It is told, in a block that
`admin_tools.build_context()` appends to the **system instruction** — not to the
user's turn:

```
Actor Telegram user id: 6931339207
Actor role: owner (owner of this bot)
Actor is the owner: yes
Actor may ask for: admins.manage, commands.use, config.manage, …
Chat id: -1001234567890
Replying to user id: 42 (Somebody)
You may only act on the ids above. If a target is not identified by an id, ask
for one — never pick a person by name, and never choose between two similar
names.
```

Two things make this safe rather than a new attack surface:

1. **One producer.** The block is built in one function from the resolved
   `Principal`, so the id the model is told about and the id the service will
   authorise against cannot disagree. If they could be assembled independently,
   they eventually would, and that disagreement is precisely the bug this design
   exists to prevent.
2. **It is instruction, not evidence.** A claim to be the owner arriving *in the
   conversation* arrives as text the model has been told to distrust. The
   server-side block says who is asking; anything else is content.

### 29.4 The tool set: eight writes, nine reads

`app/admin_tools.py` declares seventeen tools. The write tools map onto the eight
operations in `admin_service.OPERATIONS`:

| Tool | Permission | Telegram right | Notes |
|---|---|---|---|
| `ban_member` / `unban_member` | `moderation.ban` | `can_restrict_members` | |
| `mute_member` / `unmute_member` | `moderation.mute` | `can_restrict_members` | duration from `MUTE_MINUTES` |
| `warn_member` | `moderation.warn` | — | application-owned |
| `delete_message` | `moderation.delete` | `can_delete_messages` | |
| `promote_member` / `demote_member` | `admins.manage` | `can_promote_members` | *soft right*, see 29.7 |

The read tools — `get_member`, `get_member_status`, `get_admin_status`,
`list_admins`, `get_role`, `get_permissions`, `get_chat_info`,
`resolve_reply_target`, `get_recent_admin_context` — answer from state and touch
no authority at all.

**`promote_member` has no parameter for Telegram rights.** Its schema is
`target_user_id` and a `role` string, nothing else. The role is mapped to
`promoteChatMember` flags by `rbac.telegram_rights_for()`, inside the
application. So "the model may not hand out arbitrary Telegram permissions" is
enforced by the *shape of the tool*, not by a check somebody has to remember to
write — there is no argument a model could populate to express it. The Python
promotion dialog is the only caller that ever supplies an explicit permission
set, because an operator is allowed to tick boxes and a model is not.

### 29.5 Exposure is a courtesy; authority is the thing

`tool_names_for(principal)` decides which tools are *offered*, and it is worth
being clear about what that does and does not buy. It buys a better conversation:
a helper is not told about `ban_member`, so it does not offer to ban anybody and
then have to explain a refusal. It buys nothing else. Every call the model
actually makes comes back through `on_tool` and is authorised again in
`admin_service` against the same id, whether or not it was offered.

That asymmetry is deliberate. Exposure is the layer that can be wrong without
consequence; authorisation is the layer that cannot.

By default a guest — an ordinary member — is offered **nothing**, because
`ADMIN_TOOL_GUEST_TOOLS` is off. The read tools would let any member enumerate
the administrator roster, which is not a secret inside a group but is also not
something an ordinary conversation needs. It is off for a second reason too:
offering tools at all switches the turn onto the tool-aware transport, which
sends every declaration with every message, and for a member that is the price
of nothing. Even with the setting on, no write tool is ever offered to a
principal with no permissions — that is a loop in the code, not a setting.

### 29.6 A request is typed, stamped, and remembered

Four separate refusals guard the request boundary. They exist because a tool call
is the first administrative request in this codebase that has ever existed
*somewhere other than the call stack* — it is produced as model output, and
anything that can be produced can be produced again.

**Shape.** `parse_write_call()` returns `None` for anything malformed: an
unknown tool, an argument the schema does not declare, a missing required
argument, an id that is not a positive integer. It refuses rather than repairs.
The temptation is to coerce — a missing id becomes `0`, a missing role becomes
the default — and every coercion is a way for an action to run that the model did
not correctly ask for. An undeclared argument is refused rather than ignored,
because a model inventing parameters is not describing the call it thinks it is
describing.

**Replay.** The request is stamped with `time.time()` when the call is read, and
`execute()` refuses one older than `ADMIN_REQUEST_REPLAY_WINDOW` (120s) as
`stale`. A request that has been sitting somewhere is the shape of a replay, not
of a live request. This check only means something on the AI path — the Python
path has no representation outside the call stack and cannot be replayed — but
it is applied uniformly so there is no second code path to reason about.

**Idempotency.** Every request carries a `request_id` (`uuid4().hex`) and
`admin_requests` is keyed on it with `INSERT OR IGNORE`. First write wins: if two
requests with the same id race, the first one recorded is the one that happened,
and rewriting the row would let the loser claim it did something else. A
duplicate returns the stored outcome with `duplicate=True` and a sentence saying
so, rather than performing the action twice.

**Target.** An operation that acts on a user needs a `target_id`; one that acts
on a message needs a `message_id`; and a target equal to the bot's own id is
refused as `target_is_bot`, because promoting the bot is a no-op that looks like
success and banning it is worse. Resolution never guesses by display name — the
model is told to ask, and `resolve_reply_target` answers with the replied-to user
or with "there is no reply", never with a best guess.

### 29.7 Telegram is the floor, not the ceiling

After `rbac` has allowed something, the service checks whether the *bot* holds
the Telegram right the action needs, live, before attempting it. Configuration
saying the bot should have a right is not evidence that it has one.

For six of the eight operations that check is fatal: if the bot cannot restrict
members it cannot ban, and the request fails with `bot_lacks_right`.

`promote_member` and `demote_member` are marked `soft_right`, and the difference
matters. The application role and the Telegram administrator flag are two
separable layers — §35 of the brief, and true of Telegram generally: somebody can
hold `moderator` here without being a Telegram admin there. So a promotion writes
the application role **first**, and a Telegram refusal is reported as a note
attached to the success rather than as a failure. Reporting it as a failure would
be a lie in the other direction: the role really was granted.

`demote_member` returns `not_an_admin` when there was no stored role to remove,
which the caller turns into "there was nothing to do". A demotion that removed
nothing has not happened, and saying "done" would be wrong.

### 29.8 The gateway is the whole attack surface

`admin_service` never imports `telegram`. It reaches Telegram only through a
`Gateway` `Protocol` whose complete method list is:

```
bot_right, promote, demote, mute, unmute, ban, unban, delete, warn, member
```

Ten methods. No `call`, no raw method name, no access to the underlying `Bot`
object. So the set of Telegram side effects reachable from an administrative
request is those ten methods, and reviewing them is reviewing the whole surface —
which is a great deal easier than reviewing a `Bot` object with four hundred
methods on it. `TelegramGateway` in `app/main.py` is the only implementation, and
the tests replace it with a fake that records calls, which is how "nothing
reached Telegram" becomes an assertion rather than a hope.

### 29.9 Two modes, and what a Gemini outage actually does

`admin_service.mode_status()` reports one of three states:

| mode | meaning |
|---|---|
| `ai` | AI administration is up |
| `degraded` | configured but unavailable — the commands are the way |
| `python` | AI administration is switched off by configuration |

Three states rather than two, because "AI is off" and "AI is broken" are
different facts and an operator needs to be able to tell them apart. The mode is
surfaced to the operator rather than being a silent property of the deployment.

The fallback is the whole reason the commands were kept. `ADMIN_AI_ENABLED` off,
or Gemini unreachable, or every account in the pool cooling down — in all three
cases `/ban`, `/mute`, `/promote` keep working, because they were never routed
through the model. A provider outage degrades the group to commands. It does not
degrade it to no administration at all.

### 29.10 Isolation: the other three workloads cannot reach here

The conversational assistant is the only workload with administrative tools. The
acquisition classifier, the moderation AI and the transcription workload have no
route to `admin_tools` at all, and this is asserted rather than assumed:

* `test_only_the_conversational_workload_has_administrative_tools`
* `test_the_chat_module_does_not_execute_tools_itself` — `app/chat.py` transports
  tool calls and never decides anything; the runner is passed in.
* `test_the_service_never_imports_telegram` — the boundary is real, not
  stylistic.
* `test_a_moderation_verdict_cannot_become_a_ban` — a moderation decision is a
  decision about content, and it has no path to an administrative action.

That last one is worth its own sentence. A moderation verdict and an
administrative ban are produced by different systems for different reasons, and
the only thing that turns one into the other is a human or a model *asking* for a
ban. There is no code path where a classifier's output becomes an action.

### 29.11 The audit trail keeps its one vocabulary

Refusals are audited as well as successes. "Who did this" is the question asked
after an incident; "who tried" is the question asked *during* one, and a trail
that only records successes cannot answer it. Both interfaces write the same
rows.

The action names are the ones the trail already used — `moderation.ban`,
`admin.promote` and so on — preserved through an `Operation.audit_action` field
so that adding the AI path did not fork the vocabulary. An operator searching the
audit for `moderation.ban` finds every ban, whichever interface asked for it.

What the trail *does* distinguish is **who asked**. The `interface` column holds
`python` for the direct commands and `ai` for the model-mediated path, so "did a
person do this, or did the assistant?" is a `WHERE` clause rather than an
inference from the actor id. That column was added to a table that already
existed in production, which is why `db.py` grew an idempotent
`_ensure_column` — see §30.

The detail column holds ids, keys and short machine strings. It never holds a
message body, and the tests assert that: `test_the_audit_row_never_contains_a_message_body`.

Retention is enforced on the administrative path — `audit_prune()` and
`admin_request_prune()` — because this process has no scheduler, and a retention
rule that only runs when somebody remembers is not a retention rule.
`ADMIN_IDEMPOTENCY_RETENTION` is floored at `ADMIN_REQUEST_REPLAY_WINDOW` in
`config.py` rather than trusted to the operator, because a request forgotten
while it is still replayable fails silently.

### 29.12 Configuration

| Setting | Default | Meaning |
|---|---|---|
| `ADMIN_AI_ENABLED` | `true` | offer the model administrative tools at all |
| `ADMIN_PYTHON_ENABLED` | `true` | the direct commands keep working |
| `ADMIN_REQUEST_REPLAY_WINDOW` | `120` | seconds; older requests are refused as stale |
| `ADMIN_IDEMPOTENCY_RETENTION` | `86400` | floored at the replay window |
| `ADMIN_ACTIVITY_RETENTION` | `7776000` | 90 days of audit trail |
| `ADMIN_CONTEXT_LIMIT` | `12` | recent events shown to the model |
| `ADMIN_CONTEXT_WINDOW` | `21600` | and over what window |
| `ADMIN_TOOL_MAX_CALLS` | `4` | tool calls per turn before the loop stops |
| `ADMIN_TOOL_GUEST_TOOLS` | `false` | offer the read-only tools to members |

None of these widen anybody's authority. They decide what the model is offered
and how long records are kept; every action still requires the same permission
from the same table.

`ADMIN_TOOL_MAX_CALLS` is a bound rather than a timeout because the failure mode
is a model that keeps asking, and a turn that never ends is worse than one that
ends with "I could not finish". The loop is application-owned: the last request
is made without tools, so the model has to answer in words.

### 29.13 What this section does not claim

1. **The model's judgement is not a security control, and is not treated as
   one.** It decides *what was asked for*. It has no influence on whether the
   asker may have it. A perfectly-prompted model and a jailbroken one produce
   requests that go through the identical pipeline.
2. **Prompt injection is not solved; it is defanged.** Somebody can still put
   text in a message that persuades the model to call `ban_member` on somebody.
   What they cannot do is make that call succeed for an actor without
   `moderation.ban`, or against the owner, or against a peer, or twice. The blast
   radius of a fully compromised model is "the set of actions the person who
   triggered it could have performed anyway by typing the command".
3. **A refusal is only a refusal if nothing reached Telegram.** Every test in
   `tests/test_ai_admin.py` that asserts a refusal also asserts that the fake
   gateway recorded no calls. A refusal the bot prints while still calling the
   API is not a refusal, and that is the one failure mode this suite is built to
   make impossible.

### 29.14 The conversational daily allowance belongs to an account

The chat workload has a daily allowance, and it is now **per account**. With two
chat keys and `GEMINI_CHAT_DAILY_LIMIT=200`, the bot can serve 400 conversations
a day, and it says "سهم امروز چت تموم شده" only when *both* accounts have spent
their own 200.

#### The incident this fixes

The allowance used to be one counter for the whole deployment, in
`chat_usage`, keyed by day alone:

```sql
-- the columns, before
day, calls, replies, malformed, errors, skipped
```

There is no account column there, and that was the bug. A second configured key
with a completely fresh day's allowance bought nothing, because the gate was
`db.chat_calls_today() >= GEMINI_CHAT_DAILY_LIMIT` — a question about the
*deployment* — and it was asked before the pool was ever consulted. So the
group was told its quota was gone while half the pool sat idle. Observed live on
2026-09-21: 500 calls against a cap of 500, both chat accounts `ACTIVE` with zero
cooldowns and zero quota events, and the credential answering a real request in
four seconds.

#### How it works now

A new table, `gemini_daily(workload, slot, day, calls)`, records how many
provider requests each account has spent on the current day. It is a separate
table rather than extra columns on `gemini_accounts` for two reasons: the
lifetime counters there are the account's health record and must not be reset by
a day boundary, and `CREATE TABLE IF NOT EXISTS` needs no migration where an
added column would.

`Account.usable()` now also requires the account to be under its allowance, so
the pool stops offering a spent account and moves to the next — the same
mechanism it already uses for a 429. `Pool.daily_exhausted()` is the *only*
question that may produce the "quota is used up" message, and it is asked of the
accounts:

```python
if not self.daily_budget or not self.accounts:
    return False
return all(a.daily_exhausted(now) for a in self.accounts)
```

Two properties of that line are deliberate:

* **It is `all`, over accounts.** One counter reaching zero was the bug; the
  message is now only produced when there is genuinely nothing left to try.
* **Cooldowns are ignored.** An account that has allowance but is briefly
  cooling down is a transient failure, not an exhausted quota. Conflating them
  would send somebody away for a day over a minute.

Only the chat workload sets `daily_budget`; it is 0 — unlimited — for
acquisition, moderation, transcription and TTS, so their provider use is
unchanged. `spec.get("daily_budget", 0)` in `build_pools()` is what keeps that
true.

#### The two clocks, and the bug that found itself

A day is a calendar fact; a cooldown is an interval. The rest of `app/chat.py`
measures intervals against `time.monotonic()`, and the first version of this
change passed that monotonic reading into the allowance check. `db.ai_day()` on
a monotonic timestamp lands in 1970, so the allowance was written under one day
and read under another and the cap silently never fired.

The fix is structural rather than a corrected argument: `daily_calls`,
`daily_exhausted`, `daily_remaining` and `Pool.daily_exhausted` take **no clock
at all** and read `time.time()` themselves. `Account.usable()` calls
`self.daily_exhausted()` with no argument rather than forwarding the `now` it
was given. The calendar is now behind a boundary that cannot be handed the wrong
clock, which is a stronger guarantee than remembering not to. The test that
caught it is `test_the_user_is_told_only_once_every_account_is_out`, which drives
`chat.reply` — the one path that had the monotonic clock in scope.

#### The fallback is unchanged

A deployment with no pool has no account to attribute an allowance to, so one
counter really is the whole truth and the old behaviour stands exactly as it was,
including its floor of one. `_daily_allowance_left()` decides between the two:

```python
pool = gemini_pool.pool_for("chat")
if pool is not None and pool.enabled:
    return not pool.daily_exhausted()
return db.chat_calls_today() < max(1, int(config.GEMINI_CHAT_DAILY_LIMIT))
```

#### What the model-level failover was already doing

The other half of the request — "use all compatible models, fail over when one
is limited" — needed no change. The chat pool is configured with eight
compatible models and, when a model answers 429, that *model* is benched on that
account and the next compatible one is tried before the account is abandoned at
all. That distinction is §28.2, and it was verified live rather than inferred:
`tests/test_gemini_pool.py` covers it with a scripted provider, and
`test_a_spent_account_is_skipped_and_the_request_is_served_by_the_next` in
`tests/test_chat_daily_budget.py` covers the new allowance composing with it
through `generate()` rather than around it.

#### Verifying it

```bash
# The allowance suite: per-account spending, the `all` question, the rollover,
# the fallback, and that the other workloads have no allowance at all.
.venv-test/bin/python -m pytest tests/test_chat_daily_budget.py -q

# What is left across the whole chat pool right now.
docker exec -w /srv guardbot python -c "
from app import db, gemini_pool; db.init()
p = gemini_pool.pool_for('chat')
print(p.daily_remaining(), 'of', p.daily_budget * len(p.accounts))"
```

### 29.15 The allowance is spent by requests the provider *served*

The per-account allowance above fixed *whose* day was spent. It did not fix
*what* spends it, and that was the second half of the same message to the group.

#### The incident this fixes

Measured live on 2026-09-22, with two chat accounts and
`GEMINI_CHAT_DAILY_LIMIT=200` — 400 requests of allowance:

| counter | value |
| --- | --- |
| `chat_usage.calls` | 415 |
| `chat_usage.replies` | 394 |
| `gemini_daily` `chat` slot `1` | 503 |
| `gemini_daily` `chat` slot `2` | 504 |
| `quota_events` (both accounts) | **0** |

Four hundred and fifteen real calls spent a thousand charges, both accounts hit
their ceiling, `usable` went to zero, and the group was told «سهم امروز چت تموم
شده» for the next fourteen hours. `quota_events = 0` is the provider saying, in
its own record, that it had never once refused the quota.

The arithmetic is the diagnosis: `note_request` charged the day *before* the
call, and nothing ever gave the charge back. So every free-tier 429, every retry
across the eight compatible models, and every 503 was charged as though the
provider had produced a completion. A logical call that walked seven models
before the eighth answered cost **eight** — which is why 415 calls consumed 1007.

#### The rule

A charge is given back when the provider *refused* the request, because a
refusal consumed no quota. This is not "refund failures"; it is "refund the ones
that provably reached nothing", and the three that may have reached the model
stay charged:

```python
_MAY_HAVE_BEEN_SERVED = ("timeout", "network_error", "unknown_error")
_DEADLINE_DETAILS = ("DEADLINE_EXCEEDED", "504")

def _may_have_been_served(failure: Failure) -> bool:
    if failure.kind in _MAY_HAVE_BEEN_SERVED:
        return True
    if failure.kind == "provider_error":
        return failure.detail in _DEADLINE_DETAILS
    return False
```

* `timeout` — *our* deadline expired. The provider had the request; we stopped
  waiting. Reading that as "not served" would make a slow afternoon look free.
* `network_error` — the response was lost on the way back, so the answer may
  have been generated and billed.
* `unknown_error` — unclassified, and the safe reading of "I do not know" is
  that it may have cost something.
* `provider_error` is split by `detail`, because the same `kind` and the same
  `scope` cover opposite facts: a `503`/`UNAVAILABLE` never reached the model,
  while a `504`/`DEADLINE_EXCEEDED` means the model accepted the request and ran
  out of its own time. Only the first is refunded — the same reasoning as our own
  `timeout`, and the reason `detail` is logged at all (§28).

`Account.note_failure` is the single place that applies it, so every failure path
in `generate` — the transient retry loop, the account trip, the model failover —
gets the same treatment without any call site having to remember:

```python
if self.daily_budget and not _may_have_been_served(failure):
    self.refund_daily()
```

The charge stays where it was, in `note_request`, and is *refunded* rather than
never made. That ordering is deliberate: the charge-before-the-call is what
bounds a runaway retry loop inside a single logical request, and moving the
charge to after the call would remove that bound.

`db.daily_refund` uses `MAX(calls - 1, 0)` rather than a plain subtraction. A
refund without a matching charge is reachable — a retry after a restart, or a row
written by a build that did not refund — and it must not drive the counter
negative and hand out allowance that was never configured.

#### What is unchanged

`0` still means unlimited, and a workload without an allowance is not given a
counter by a refund: `refund_daily` returns early on `not self.daily_budget`, and
`db.daily_for("moderation", ...)` stays `{}`. The `gemini_daily` table is
untouched in shape, so this needs no migration — the same reason it was a
separate table in the first place.

#### Verifying it

```bash
# The refund rule: refusal vs. served, the retry-across-models shape, the three
# kinds that stay charged, the deadline split, the zero floor, and the incident
# end to end through chat.reply.
.venv-test/bin/python -m pytest tests/test_chat_daily_budget.py -q
```

#### Repairing the rows the old rule wrote

The fix stops the over-charging from here on; it does not un-spend what was
already charged. On 2026-09-22 both chat accounts sat at 503/504 against a cap of
500, so the group stayed silent until the 08:00 UTC rollover unless the day's
rows were corrected — which is what "unblock today" required.

The correct value is the provider-served floor, and the only per-day record of
that is `chat_usage.replies`: every reply needed at least one served provider
call, so it is a floor on today's real spend. The split *between* accounts is not
recoverable from any table, so it is even — which is also the shape the
over-charging itself took (503/504), and the shape `ordered_accounts` produces.

```python
# run in the container, after the fix is deployed and before the restart below
day = db.ai_day()
replies = db._conn.execute(
    "SELECT replies FROM chat_usage WHERE day=?", (day,)
).fetchone()[0]
for slot in ("1", "2"):
    db._conn.execute(
        "UPDATE gemini_daily SET calls=? WHERE workload='chat' AND slot=? AND day=?",
        (replies // 2, slot, day),
    )
db._conn.commit()
```

Two things about doing this on a running deployment:

* **The cache makes a restart necessary.** `Account.daily_calls` caches the
  counter per day and only re-reads when the *day* changes, so the live process
  keeps returning 503/504 until it is restarted. A restart rebuilds it from the
  table; the startup log then shows the corrected figure.
* **This is a one-off for rows written by the old rule.** From this build on the
  counter is right by construction, and no scheduled job or migration is needed.

The repair was verified the way the incident was found — by measuring, not by
asserting:

| check | after |
| --- | --- |
| `gemini_daily` `chat` | `{'1': 200, '2': 197}` |
| `pool.daily_remaining()` | 603 of 1000 |
| `pool.daily_exhausted()` | `False` |
| `_daily_allowance_left()` | `True` |
| startup log | `daily_remaining=603`, `[pool] chat: accounts=2 usable=2` |

And the refund rule was confirmed on the live path rather than in the unit tests
alone: one logical request that met a `503` and a free-tier `429` before an
answer came back cost **one** charge (606 → 605), where the old rule would have
cost three. A real question from the owner was answered at 17:56:39 UTC with
`sent=True`, which is the only proof that matters.

---

<a id="s30"></a>

## 30. The audit trail says which interface acted

`admin_audit` records every administrative decision from both interfaces. The
action vocabulary is shared on purpose (§29.11) — an operator searching for
`moderation.ban` finds every ban — but the two interfaces are *not* the same
thing, and an incident review needs to tell them apart: "an administrator ran
`/ban`" and "the assistant was talked into banning someone" are different
findings with the same action name.

So the row carries an `interface` column: `python` for the direct commands, `ai`
for the model-mediated path. It is written by `admin_service._record()` from the
request's own `interface` field, and by `main._audit()` as `python`, because the
command path is the python path. The two constants are `INTERFACE_AI` and
`INTERFACE_PYTHON` in `app/admin_service.py`; nothing else may invent a value, and
`_record()` coerces anything unrecognised back to `python` so a bad caller cannot
write a third interface into the trail.

### 30.1 The column had to be added to a table that already existed

This codebase had **no migration pattern** — every table is `CREATE TABLE IF NOT
EXISTS`, which is fine for a new table and useless for a new column on a table
that is already in production. `admin_audit` in the live database was created
before this column existed, and `CREATE TABLE IF NOT EXISTS` would have left it
alone.

Rather than a one-off script, `db.py` grew one small primitive:

```python
def _ensure_column(table: str, column: str, declaration: str) -> None:
    cols = {row[1] for row in _conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
```

It is idempotent by construction: it reads `PRAGMA table_info`, returns early if
the column is present, and only then issues the `ALTER TABLE`. It is called for
`admin_audit.interface` immediately after the schema is created, so a fresh
database gets the column from the `CREATE` and a live one gets it from the
`ALTER`, and neither path is special-cased. The declaration carries
`NOT NULL DEFAULT ''`, so the rows that predate the column read back as empty
rather than as a value nobody wrote.

`tests/test_db_migration.py` is the test that makes this a guarantee rather than
a hope: it builds an `admin_audit` table with the **old** schema, runs `db.init()`
over it, and asserts the column appears, that old rows are still readable, that
running init twice changes nothing, and that new rows carry the interface.

### 30.2 Reading it back

`db.audit_recent()` and `db.audit_since()` return rows through a single
`_audit_row()` projection built from `_AUDIT_COLS`, so the `interface` key is
present on every row the application reads. `admin_tools.recent_admin_context()`
passes it through to the model's trusted context, which is how the assistant can
answer "what did *I* do" as distinct from "what was done".

---

<a id="s31"></a>

## 31. The administration mode is observable

`admin_service.mode_line()` reports which of the two interfaces is currently
able to act — `AVAILABLE`, `DEGRADED — PYTHON FALLBACK ACTIVE`, or `OFF —
PYTHON COMMANDS ONLY`. The function existed; it was never called. A mode line
nobody reads is not observability, so it is now wired in two places:

* `post_init` logs it once at startup, next to the pool and workload lines. This
  is the line that tells an operator, before anything is asked of the bot,
  whether the assistant will answer at all.
* `cmd_pool` appends an **AI administration** section to the pool report. `/pool`
  is where an operator already goes when the AI is misbehaving, so the mode and
  the pool's health arrive together.

That section is `admin_service.status_report()`, and it answers two questions
that look identical from inside a group and are completely different problems:

1. **The mode.** "The assistant never answers" is a configuration or provider
   problem.
2. **The recent refusals.** "The assistant answers and is refused every time" is
   a permissions problem. `recent_refusals()` reads them from the audit table —
   not from an in-memory counter, because the question is usually asked after a
   restart and a counter that resets would answer it with a confident zero. The
   scan is bounded: `db.audit_recent(limit * 6)` and the refusals inside that
   window.

A **duplicate** is deliberately not a refusal (`_REFUSAL_OUTCOMES` omits
`OUTCOME_DUPLICATE`): the desired state does hold, it was simply reached earlier,
and listing it would send an operator chasing a problem that does not exist. Each
refusal line ends with `via=<interface>`, so the report itself demonstrates §30.

---

<a id="s43"></a>

## 43. The audit trail says with what authority, and proves what it cannot hold

### 43.1 The two fields that were missing

The brief lists nine things an audit row has to carry. Seven of them were there.
The two that were not were the two nothing was asking for:

| field | what it answers | why it was missing |
|---|---|---|
| `role` | *with what authority* did this happen? | the trail recorded **who** acted and never with what standing. That stops being answerable the moment a role changes: an administrator who is later demoted leaves a trail saying they acted, and not whether they were entitled to |
| `request_id` | which request produced this row? | the outcome a person saw and the row that recorded it were linked only by matching actor, chat, operation and target by hand |

Both are additive columns on a table already in production, so both go through
`db._ensure_column`, and `tests/test_db_migration.py` covers the migration and
the reading of rows that predate it.

The role is resolved from `rbac` **at write time** and never taken from the
request, and that direction is the point: the request is the thing being
audited, so a request that named its own authority would be writing its own
alibi. It is the same rule as `AdminRequest` having no `is_owner` field — here
the claim *is* expressible, because `role` exists on the request, so the test is
that it is ignored. And because it is stamped rather than recomputed, a test
ages a row across a demotion and asserts it still reads `helper`.

The typed-command path stamps the same fields from `main._audit`. It has no
request id, because a typed command is not a request from the assistant and has
none, so the column stays empty rather than being filled with something that
only looks like an identifier.

### 43.2 What must never be recorded, asserted two ways

Either half alone is weak: a shape test can pass while a leak happens through a
different door, and a sentinel test can pass while a different secret leaks. So
`tests/test_audit_hygiene.py` asserts both.

**Structurally** — the schema has no column wide enough for a conversation, and
`detail` is truncated to 300 characters on write, so no caller can use it as a
text column even by accident. The column list itself is asserted, so adding a
`body TEXT` column later is a deliberate act that fails a test rather than
something a reviewer has to notice.

**Behaviourally** — a sentinel credential placed in the configuration appears in
no audit row and no log line, including the boot report, which is the one place
a pool is described. That test was vacuous on its first run: every pool reported
`accounts=0`, because `GEMINI_POOLS` is built from the environment at import
time and patching the individual setting changed nothing. It now asserts that
the boot report really does describe the sentinel by its masked tail, so the
"no leak" assertion cannot pass by looking at an empty pool. A passing test that
proves nothing is worse than a failing one.

### 43.3 The permission model, end to end

For reference, the whole path an administrative action takes, with the file that
owns each step:

| step | owner | what it enforces |
|---|---|---|
| 1. identity | `rbac.resolve` | the role comes from the owner id in configuration, the config admins, or the `admins` table — never from the message |
| 2. the actor may talk to Nexus at all | `nexus.accepts` (room) / `nexus.accepts_private` (direct) | §34 and §40 |
| 3. the message is addressed to Nexus | `main._nexus_directed` | a reply to the bot, an `@mention`, an alias, or a configured name |
| 4. shape | `admin_service.execute` step 1 | a closed `OPERATIONS` vocabulary; no chat or no actor is refused before the replay lookup, so a malformed request cannot probe the idempotency table |
| 5. system state | `execute` step 2 | an AI request is refused while Nexus is offline; the typed commands are the documented fallback and are not |
| 6. replay and idempotency | `execute` step 3 | `admin_requests`, keyed by request id |
| 7. authority | `execute` step 4 → `rbac.authorize` | the operation's permission against the resolved principal, plus owner protection and hierarchy |
| 8. target | `execute` step 5 | a real target, not the bot, not higher-ranked |
| 9. Telegram's own rights | `execute` step 6 → `Gateway.bot_right` | the bot must hold the right it is about to use |
| 10. the call | `execute` step 7 → `_apply` | the only place a Telegram mutation happens |
| 11. the record | `admin_service._record` | actor, role, action, target, chat, outcome, timestamp, request id, interface, and the failure reason |

`nexus.control` and `agent.request` are held by **no role bundle**, so no
promotion dialog can express them and an administrator promoted to every role
still does not hold them. That is what makes "only the owner" a property of the
tables rather than a check somebody has to remember.

---

<a id="s44"></a>

## 44. Identity: a handle, and turning a reference into one person

The brief asks for two things that are easy to conflate and must not be: an
**internal UUID** for each person, and **deterministic identity resolution**.

### 44.1 The handle is a name, not a credential

`identities(user_id PRIMARY KEY, uuid UNIQUE, created_at, last_seen)` in
`app/db.py`, minted once on first sight by `db.identity_ensure`, which is called
from `people.remember` — the one path that already runs for every message a
person sends. `app/identity.py` wraps it.

Three properties, and each was chosen against an alternative:

* **Not derived from the Telegram id.** A derived value would be reversible,
  which defeats the point of an opaque handle. `test_identity.py` asserts the
  Telegram id does not appear in it.
* **Global, not per-chat.** A Telegram user id is global; a per-chat handle
  would make the same person two people the moment they spoke in a second
  group. The name rows stay per-chat; the handle does not.
* **Not authority.** Nothing reads a uuid to decide anything. `app/rbac.py`
  remains keyed on Telegram ids, and the uuid is only ever a *second name* for
  the same person — for correlation in logs and for the assistant to refer to
  somebody without repeating their number.

A person who has not spoken since this shipped has no handle yet and
`identity.describe` reports `uuid: ""`. That is deliberate: minting on a read
path would make a lookup a write.

### 44.2 Resolution is exact, and ambiguity is a question

`identity.resolve(query, chat_id=...)` accepts a numeric Telegram id, an internal
uuid, an `@username`, a display name or an alias, and answers with one of four
statuses: `ok`, `ambiguous`, `unknown`, `invalid`.

The load-bearing rule is the third line of `app/people.py`'s docstring, kept
here: **it never guesses.** Two people matching one name returns `ambiguous`
with the candidates and *no* `identity` field, so there is nothing for a model
to pick from. The consequence of being wrong is an action on the wrong person,
which is the worst failure this subsystem could have.

The name matching itself is unchanged from `app/people.py` — an exact,
normalised comparison that folds the Arabic/Persian letter variants, the
diacritics and the zero-width joiner, so «ميلاد» and «میلاد» are one person.
`identity.resolve` adds the id, uuid and username keys and delegates the name
case to it rather than growing a second, weaker matcher.

### 44.3 Where it plugs in

* `people.remember` mints the handle (never fatal; a failure leaves the
  Telegram id, which is authoritative anyway).
* `admin_tools.build_context` states the actor's handle in the trusted block.
* The `get_identity` tool returns `identity.describe`, and `resolve_person`
  now delegates to `identity.resolve`, so a name, a `@username`, an id and a
  uuid are all resolvable by the assistant through one path.
* `agent_data.agent_task_view` reports `actor_uuid` alongside `actor_id`.

### 44.4 How resolution has been ending, as a rate

The brief asks for identity resolution success and ambiguity to be visible, and
that is the one awareness-side signal nothing else stores: a name matching two
people leaves no other trace. `identity_resolutions` is a counter table —
`outcome` and a count — written from `identity.resolve`, and rendered by
`identity.resolution_line()` on the Nexus status report.

Two deliberate choices. The bump lives in the public `resolve` wrapper around
`_resolve`, not at each of the eight returns inside it, so the tally is complete
by construction: a new branch cannot be added without being counted. And it is
guarded at both the call site and inside `db.identity_resolution_bump`, because
a metric is never worth a wrong answer — `test_identity.py` proves a counter
that raises still leaves the lookup correct.

This is not a write on the message path. `resolve` is reached when a person asks
*about* a person, through `resolve_person`; the hot path calls `identity.ensure`,
which is a different function.

---

<a id="s45"></a>

## 45. What the assistant may read, and the two boundaries around it

`app/agent_data.py` is the operational data layer. The brief asks for extensive
read access to logs and structured events *and* for no secret ever reaching the
model; those are in tension exactly once, and this module is where it is
resolved.

### 45.1 No generic query, and no row copied through

There is no `execute_sql`, and no parameter anywhere becomes SQL text. Each
function knows the one question it answers, and each answer is a dict built
field by field. A column added to a table later cannot appear in an answer by
default, because nothing here does `SELECT *` into a return value. That is what
makes "secret-bearing columns are structurally excluded" a property rather than
a promise.

### 45.2 Redaction at the boundary

Every string that leaves passes through `redact`, which delegates to
`agent_bridge.redact` — one pattern list, not two that could drift. It is the
second line of defence: the allowlist above is the first, and this catches a
token that ended up somewhere it was never meant to be (an error string, a task
result). `test_agent_data.py` plants real bot tokens in audit details, in an
awareness summary and in a task error, and asserts they do not survive.

### 45.3 The sources

`search_events` correlates five sources into one shape, filtered by the ids the
server already uses — actor, target, room, time — and never by message content,
because this bot does not keep message content for a search to find:

| source | table | what it answers |
|---|---|---|
| `admin` | `admin_audit` | who did what, and what was refused |
| `model` | `gemini_events` | rate limits, failures, pool state |
| `agent` | `agent_tasks` | coding-agent task lifecycle |
| `awareness` | `awareness_state` | what Nexus currently understands about a room |
| `moderation` | `moderation_usage` | today's moderation counters |

`nexus_diagnostics(chat_id)` answers «چرا نکسوس جواب نداد؟» from the state that
decided it — the switch, the awareness layer, the pending batch, recent
refusals, recent model events — and states a reason in words rather than leaving
the model to infer one.

### 45.4 The tools, and why they carry no `chat_id`

Four read-only tools are exposed (`get_identity`, `search_events`,
`get_nexus_diagnostics`, `get_service_status`), gated on `moderation.review` —
the observational floor — so a member is never given a window into operational
history while a helper or moderator may use them to explain what happened.

None of them has a `chat_id`, `actor_id`, `target_id` or `permissions`
parameter. The room and the actor come from the server, exactly as they do for a
write tool, so a forged one is not rejected — it is *inexpressible*. The test
suite asserts this for every tool in the registry, not only the new ones.

The prompt cost is real and was measured: the four tools add about 6.3 KB of
declarations, and `tests/test_awareness_latency.py` carries a ceiling that was
raised deliberately, with the reason recorded in the test itself. The cost is
bounded in practice because the full set is only attached when the last human
speaker in a room is an administrator — a member's message still costs no
declarations at all.

### 45.5 Exposure is a courtesy; the dispatch is the boundary

A declaration tells the model what it may ask for. It does not stop the model
from asking for something else, and a hallucinated tool name is not a
hypothetical — it is what a model does when it is unsure. So `run_read_tool`
refuses any tool that `tool_names_for` would not have offered the same
principal, before it looks at the arguments. A guest who was offered nothing
therefore gets `not permitted`, not the integrations list; a moderator is
refused `get_agent_status`, which is the owner's.

The two rules are one function on purpose. When exposure and enforcement are
computed separately they eventually disagree, and the disagreement is invisible
until it is a leak. `test_ops_tools.py` asserts the pair for every
permission-gated tool in the registry, not only the four new ones.
