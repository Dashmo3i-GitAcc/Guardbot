"""The one place an administrative action is authorised, and the one place it runs.

Every administrative action in this bot — whether a person typed ``/ban`` or
asked the assistant *"این کاربر رو بن کن"* — arrives here as an
:class:`AdminRequest` and leaves as an :class:`AdminResult`. There is no second
path. That is the whole point of the module: the brief's requirement is that
Gemini and the Python commands "must NOT create two competing implementations of
Telegram actions", and the only way to make that true is to have exactly one
function that performs them.

Three separations make this work, and each one is a boundary rather than a
convention:

**The request carries identity, never authority.** A request says *who* is
acting (a Telegram user id), *what* they want, and *on whom*. It never says
"allowed". Authority is resolved here, from ``app/rbac.py``, on every call — so
a request that was built by a language model, or hand-written by an attacker, or
replayed from an hour ago, has exactly the same standing as one built by a
command handler: none, until :func:`authorize` says otherwise. There is no field
in :class:`AdminRequest` that can express "I am the owner", because a field that
can express it is a field a model can fill in.

**Telegram is reached through a gateway, not directly.** This module never
imports ``telegram``. It asks a :class:`Gateway` to do things, and the gateway
in ``app/main.py`` is the only object that holds a bot instance. Two things fall
out of that: the whole service is testable without a network, and the set of
Telegram operations this bot can ever perform is the gateway's method list —
which is short, explicit and reviewable, rather than "whatever ``ctx.bot``
happens to expose".

**Refusals are values, not exceptions.** Every failure — a missing permission, a
protected target, a bot without ``can_promote_members``, a Telegram error —
comes back as a result with a machine-readable ``outcome``. Callers map that to
a sentence. Nothing raises into a handler, and nothing reports success it did
not get.

What this module deliberately does not do: it does not decide what a role means
(``app/rbac.py`` does), it does not know how a request was phrased (the callers
do), and it does not read message text. It takes a typed request and returns a
typed result.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from . import agent_bridge, config, db, nexus, rbac, vpnbot

log = logging.getLogger("guardbot.admin")

# ── Operations ────────────────────────────────────────────────────────────
# The closed set of things an administrator can ask for, and what each one
# needs. This table is the reason a model cannot invent an operation: an
# operation that is not a key here is refused before any other check runs.
#
# ``right`` is the Telegram administrator right the *bot* must hold, taken from
# the real ``ChatAdministratorRights`` field names. ``None`` means the operation
# needs no administrator right at all.
#
# ``kind`` splits the two shapes a target can take. Most operations act on a
# *user*; ``delete_message`` acts on a message. Keeping them apart is what stops
# "delete message 12345" from ever being read as "delete user 12345".
#
# The third kind is for an operation whose subject is the bot itself rather than
# anybody in a chat. There is no target to validate, and inventing one (the
# actor? the chat?) would mean a state change carried a meaningless id that a
# later reader would have to reason about.
OP_USER = "user"
OP_MESSAGE = "message"
OP_SYSTEM = "system"
# The fourth kind is for an operation whose subject is not anybody in a chat and
# not a message, but an object inside another service: a VPN service, a plan, a
# transaction. There is nothing here for the gateway to validate — a plan id is
# not a Telegram user and checking it against Telegram's member list would be
# meaningless — so the kind exists to *skip* the user and message branches
# rather than to add a third validation. What it does add is a pre-flight: an
# operation against an integration this bot has not been pointed at can never
# succeed, so it is refused here, before anything is recorded, rather than
# discovered as an unreachable host later.
OP_VPN = "vpn"


@dataclass(frozen=True)
class Operation:
    name: str
    permission: str
    right: str | None
    kind: str = OP_USER
    # True when the operation changes the *application* role table as well as
    # Telegram. Only promotions and demotions do.
    changes_role: bool = False
    # True when a missing Telegram right is reported as a note rather than
    # refusing the operation outright.
    #
    # This is not a hole, it is a statement about which layer owns the outcome.
    # For a ban, the ban *is* the Telegram call — if the bot cannot restrict
    # members there is nothing to do, so a missing right refuses. For a
    # promotion, the bot's own role table is the authoritative record and
    # Telegram's administrator flag is a second, separable thing: the brief says
    # so itself, that "a user may be an application Moderator but not currently
    # be a Telegram administrator". Refusing to record the role because Telegram
    # said no would make the two layers impossible to reconcile, which is the
    # opposite of what is wanted.
    soft_right: bool = False
    # The name this operation is written under in ``admin_audit``.
    #
    # Kept as an explicit field rather than derived from ``name``, because the
    # audit table is append-only and already contains years of rows under the
    # older vocabulary. Renaming an action here would not rename those rows — it
    # would leave an operator reading a history in two languages, where the same
    # event appears as ``moderation.ban`` before some date and something else
    # after. One vocabulary, stated once, is worth the extra column.
    audit_action: str = ""
    # Whether this operation requires the conversational layer to be awake.
    #
    # True for everything a conversation can ask for, because "is the system in
    # the state the request assumes" is one of the checks the brief lists for the
    # execution layer. False for the two operations that *are* the state, which
    # would otherwise be impossible to perform precisely when they are needed —
    # turning Nexus back on while it is off.
    requires_nexus_online: bool = True
    # Whether a request that came *from a conversation* is recorded and waits for
    # the owner, instead of being carried out.
    #
    # This is not "is this operation dangerous". Banning is dangerous and is
    # deliberately **not** in this set: moderation has to be immediate or it is
    # not moderation, and every operation the automatic pipeline performs is
    # outside this table entirely. What is in the set is the two kinds of action
    # where a model's mistake is either invisible or a grant of authority:
    #
    #   * the switches — ``nexus_offline`` in particular silences the assistant
    #     that would otherwise have reported it, so the failure is silent by
    #     construction;
    #   * promotions and demotions — the one thing in this table that hands
    #     somebody else power.
    #
    # It applies to the **AI interface only**. A person typing ``/promote`` has
    # stated the intent themselves and is present to see the result; the risk
    # this guards is a model acting on an ambiguous sentence. So the typed
    # commands stay one step, and the change is confined to the path where the
    # model is the one deciding what was asked.
    needs_confirmation: bool = False


def _op(
    name: str,
    permission: str,
    right: str | None,
    audit_action: str,
    **kwargs,
) -> Operation:
    return Operation(
        name, permission, right, audit_action=audit_action, **kwargs
    )


OPERATIONS: dict[str, Operation] = {
    "ban_member": _op(
        "ban_member", "moderation.ban", "can_restrict_members", "moderation.ban"
    ),
    "unban_member": _op(
        "unban_member", "moderation.ban", "can_restrict_members", "moderation.unban"
    ),
    "mute_member": _op(
        "mute_member", "moderation.mute", "can_restrict_members", "moderation.mute"
    ),
    "unmute_member": _op(
        "unmute_member", "moderation.mute", "can_restrict_members", "moderation.unmute"
    ),
    "warn_member": _op("warn_member", "moderation.warn", None, "moderation.warn"),
    "delete_message": _op(
        "delete_message",
        "moderation.delete",
        "can_delete_messages",
        "moderation.delete",
        kind=OP_MESSAGE,
    ),
    "promote_member": _op(
        "promote_member",
        "admins.manage",
        "can_promote_members",
        "admin.promote",
        changes_role=True,
        soft_right=True,
        needs_confirmation=True,
    ),
    "demote_member": _op(
        "demote_member",
        "admins.manage",
        "can_promote_members",
        "admin.demote",
        changes_role=True,
        soft_right=True,
        needs_confirmation=True,
    ),
    # ── The conversational layer's own state ──────────────────────────────
    # Two operations rather than one with a parameter, and that is a security
    # choice rather than a stylistic one. The state is the *name* of the
    # operation, so there is no argument a model could populate and no argument
    # ``parse_write_call`` has to validate: a request either is "turn Nexus off"
    # or it is not. One operation taking a state string would add a free-form
    # field to the request boundary for the sake of saving a tool declaration.
    #
    # ``nexus.control`` is held by the owner and by nobody else, because no role
    # bundle carries it — see ``app/rbac.py``. So "an administrator silences the
    # assistant" is not refused, it is inexpressible.
    #
    # ``needs_confirmation`` on all six switches, and the reason is stated once
    # here rather than six times. The switches are the clearest case for the
    # step: they are the only operations whose *effect* is on the assistant
    # itself, so a model that acted on a misread sentence would be turning off
    # the thing that could have told the owner what it did. ``nexus_offline`` is
    # the sharpest of the six — silence is the outcome, and silence is
    # indistinguishable from "nothing happened".
    #
    # Note what is deliberately not in this set: the moderation operations. A ban
    # is more visible than a switch and is undone by ``unban_member``, and
    # moderation that waited for a second message would not be moderation.
    "nexus_offline": _op(
        "nexus_offline",
        "nexus.control",
        None,
        "nexus.offline",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    "nexus_online": _op(
        "nexus_online",
        "nexus.control",
        None,
        "nexus.online",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    # ── The awareness layer's own switch ──────────────────────────────────
    # Deliberately separate operations rather than a parameter on the two
    # above, because they are separate facts: "the assistant is silent" and
    # "the assistant is answering without reading the room" are different
    # states with different causes, and one audit action that covered both
    # would leave an operator unable to tell which had happened.
    #
    # ``requires_nexus_online=False`` for the same reason the two above carry
    # it: the switch has to be reachable in the state it is most likely to be
    # wanted in, and a request that could only be authorised while the layer
    # was running could never be used to stop it.
    "awareness_offline": _op(
        "awareness_offline",
        "nexus.control",
        None,
        "awareness.offline",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    "awareness_online": _op(
        "awareness_online",
        "nexus.control",
        None,
        "awareness.online",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    # ── The Web Search switch ─────────────────────────────────────────────
    # A third switch beside the two above, and separate for the same reason they
    # are separate from each other: "the assistant may not look anything up" is
    # its own fact with its own cause, and one audit action covering it and the
    # others would leave an operator unable to tell which had happened. Held by
    # ``nexus.control``, which no role bundle carries, so "an administrator
    # switches search off" is not refused — it is inexpressible.
    "search_offline": _op(
        "search_offline",
        "nexus.control",
        None,
        "search.offline",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    "search_online": _op(
        "search_online",
        "nexus.control",
        None,
        "search.online",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    # ── The Voice Context switch ──────────────────────────────────────────
    # A fourth switch beside the three above, and separate for the same reason
    # they are separate from each other: "a voice note is answered in text" is
    # its own fact with its own cause — it changes neither whether the assistant
    # answers nor whether it reads the room nor whether it looks anything up —
    # and one audit action covering it and the others would leave an operator
    # unable to tell which had happened. Held by ``nexus.control``, which no role
    # bundle carries, so "an administrator switches Voice Context" is not
    # refused — it is inexpressible.
    "voice_context_offline": _op(
        "voice_context_offline",
        "nexus.control",
        None,
        "voice_context.offline",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    "voice_context_online": _op(
        "voice_context_online",
        "nexus.control",
        None,
        "voice_context.online",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
        needs_confirmation=True,
    ),
    # ── The room allowlist ────────────────────────────────────────────────
    # Registering or revoking the room a command was typed in. The subject is
    # the *current* room — ``request.chat_id`` — and there is no parameter for a
    # different one, so "register a room" and "be in the room" are the same act
    # and there is no id a model or a person could name to authorize a room they
    # are not in.
    #
    # ``permission="config.manage"`` is held by the owner and by senior admins,
    # which is exactly the owner's "an authorized administrator" — not the
    # owner alone, and not every moderator. ``right=None`` because no Telegram
    # call is made: this is an application-side state change, like the switches
    # above it. ``requires_nexus_online=False`` so a room can be registered or
    # revoked while the assistant is switched off, which is when an operator is
    # most likely to be fixing exactly that.
    #
    # ``needs_confirmation=False`` and there is deliberately **no AI tool** for
    # either: a grant of access is not something a model proposes. The
    # confirmation step exists for operations the *model* can ask for; a typed
    # command is a person acting directly, and a person acting directly is
    # already the authority. Leaving the tools out is what makes "the model
    # cannot register a group" structural rather than checked.
    "register_group": _op(
        "register_group",
        "config.manage",
        None,
        "group.register",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
    ),
    "unregister_group": _op(
        "unregister_group",
        "config.manage",
        None,
        "group.revoke",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
    ),
    # ── The coding agent ──────────────────────────────────────────────────
    # Asking the host's coding agent to work on one of this system's own
    # repositories. It is an operation here, rather than a separate front door,
    # for the same reason everything else is: this is where an actor id becomes
    # an authority, and a second entry point would be a second answer to "who
    # may do this".
    #
    # ``kind=OP_SYSTEM`` because the subject is the system itself and not a
    # member — there is no target to validate and inventing one would put a
    # meaningless id in an audit row. ``permission="agent.request"`` is held by
    # the owner alone, so an administrator asking for it is refused by
    # ``rbac`` rather than by a check anybody has to remember to write.
    #
    # Note what is *not* here: nothing in this table, and nothing in
    # :class:`AdminRequest`, can express "this is approved" or "deploy is
    # allowed". The operation names what is wanted; whether it may run is
    # decided in :mod:`app/agent_service` from the actor's real id.
    "codebuddy_task": _op(
        "codebuddy_task",
        "agent.request",
        None,
        "agent.task",
        kind=OP_SYSTEM,
    ),
    # ── The VPN bot's operational surface ─────────────────────────────────
    # Six writes against another service, and the one operation that releases
    # them. They carry no Telegram right — none of them touches a chat — and the
    # permission behind every one of them is ``vpn.manage``, which no role
    # bundle carries. So "an administrator edits a customer's balance" is not
    # refused, it is inexpressible, exactly as "an administrator silences the
    # assistant" is.
    #
    # Three of the six are marked in ``app/vpn_service.py`` as requiring the
    # owner's explicit confirmation. That is not a property of the operation
    # table, and deliberately so: the table says *who may ask*, and the adapter
    # says *what the ask produces*. A money operation asked for by the owner is
    # still recorded and still waits, because the whole point of the second step
    # is that the same sentence cannot both authorise and execute.
    #
    # ``vpn_confirm`` is an ordinary operation in this table rather than a
    # special case outside it, and that is the security choice: confirming goes
    # through the same seven steps as everything else — shape, state, replay,
    # target, RBAC, rights, call — so the second half of a money operation is
    # authorised by the same code as the first half.
    "vpn_service_enabled": _op(
        "vpn_service_enabled",
        "vpn.manage",
        None,
        "vpn.service.enabled",
        kind=OP_VPN,
    ),
    "vpn_notifications": _op(
        "vpn_notifications",
        "vpn.manage",
        None,
        "vpn.notifications",
        kind=OP_VPN,
    ),
    "vpn_plan_active": _op(
        "vpn_plan_active",
        "vpn.manage",
        None,
        "vpn.plan.active",
        kind=OP_VPN,
    ),
    "vpn_balance": _op(
        "vpn_balance",
        "vpn.manage",
        None,
        "vpn.balance",
        kind=OP_VPN,
    ),
    "vpn_orders_sweep": _op(
        "vpn_orders_sweep",
        "vpn.manage",
        None,
        "vpn.orders.sweep",
        kind=OP_VPN,
    ),
    "vpn_transaction_status": _op(
        "vpn_transaction_status",
        "vpn.manage",
        None,
        "vpn.transaction.status",
        kind=OP_VPN,
    ),
    "vpn_confirm": _op(
        "vpn_confirm",
        "vpn.manage",
        None,
        "vpn.confirm",
        kind=OP_VPN,
    ),
    # ── Releasing an action the assistant proposed ────────────────────────
    # The second half of the ``needs_confirmation`` operations, and an ordinary
    # operation in this table rather than a special case outside it — the same
    # choice ``vpn_confirm`` makes, for the same reason. Confirming goes through
    # all seven steps, so the second half of a privileged action is authorised by
    # the same code as the first half, and ``needs_confirmation`` is **not** set
    # here: an operation that had to be confirmed in order to confirm something
    # could never be performed.
    #
    # ``admin.confirm`` is carried by no role, so the tool is offered to the
    # owner alone. That is exposure, not authority — the confirmer is checked
    # again in ``_confirm_pending`` by ``agent_bridge.resolve_confirmation``,
    # which is the one place the owner rule is written.
    #
    # ``requires_nexus_online=False`` because this is the operation that has to
    # work in every state. ``nexus_offline`` is one of the things it releases,
    # so a confirmation that required the assistant to be awake would be
    # unavailable in exactly the case it exists for — the owner saying "no, do
    # not turn yourself off" after the model proposed it.
    "admin_confirm": _op(
        "admin_confirm",
        "admin.confirm",
        None,
        "admin.confirm",
        kind=OP_SYSTEM,
        requires_nexus_online=False,
    ),
}

# The VPN operations, by name. Kept here rather than imported from
# ``app/vpn_service.py`` because that module imports this one, and a cycle at
# import time would make authorising a ban depend on the VPN adapter being
# importable. ``tests/test_vpn_admin.py`` asserts the two sets agree, so the
# duplication cannot drift silently.
VPN_OPERATIONS = frozenset(
    {
        "vpn_service_enabled",
        "vpn_notifications",
        "vpn_plan_active",
        "vpn_balance",
        "vpn_orders_sweep",
        "vpn_transaction_status",
        "vpn_confirm",
    }
)

# The role names an actor may ask for, mapped to the canonical role. Kept here
# rather than in ``app/main.py`` because both interfaces accept them now, and two
# alias tables would eventually disagree.
ROLE_ALIASES = {
    "helper": rbac.ROLE_HELPER,
    "moderator": rbac.ROLE_MODERATOR,
    "admin": rbac.ROLE_ADMIN,
    "senior": rbac.ROLE_SENIOR_ADMIN,
    "senior_admin": rbac.ROLE_SENIOR_ADMIN,
}

# ── Outcomes ──────────────────────────────────────────────────────────────
# Machine keys. The callers own the Persian; this module owns the vocabulary, so
# a new outcome cannot leak an internal string into a group.
OUTCOME_OK = "ok"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_MALFORMED = "malformed"
OUTCOME_UNKNOWN_OPERATION = "unknown_operation"
OUTCOME_STALE = "stale"
OUTCOME_DENIED = "denied"
OUTCOME_BOT_LACKS_RIGHT = "bot_lacks_right"
OUTCOME_TELEGRAM_ERROR = "telegram_error"
OUTCOME_BAD_TARGET = "bad_target"
OUTCOME_TARGET_IS_BOT = "target_is_bot"
OUTCOME_UNKNOWN_ROLE = "unknown_role"
# The conversational layer is switched off, so a request that came *from* a
# conversation is refused. Distinct from a denial: nobody lacked authority, the
# assistant was not supposed to be talking at all.
OUTCOME_NEXUS_OFFLINE = "nexus_offline"
# A demotion of somebody who held no application role. Distinct from a refusal:
# nothing was forbidden, there was simply nothing to remove.
OUTCOME_NOT_AN_ADMIN = "not_an_admin"
# ── The coding agent ──────────────────────────────────────────────────────
# Four outcomes rather than one, because "the bridge is switched off", "you
# asked for a repository that does not exist", "there is already a task on that
# repository" and "the same request is already in flight" are four different
# things for the owner to do next, and collapsing them into "refused" would
# leave them guessing which.
OUTCOME_AGENT_DISABLED = "agent_disabled"
OUTCOME_AGENT_REJECTED = "agent_rejected"
OUTCOME_AGENT_BUSY = "agent_busy"
OUTCOME_AGENT_DUPLICATE = "agent_duplicate"
OUTCOME_AGENT_WAITING = "agent_waiting"
# ── The VPN bot ───────────────────────────────────────────────────────────
# Four outcomes, and the split is the one that decides what the owner does
# next. "Unavailable" means the integration could not be reached at all — look
# at the wiring. "Refused" means it answered and the answer was no — look at
# the request. "Error" means something on our side of the wire was malformed.
# "Awaiting confirmation" is not a failure: nothing ran, and the next step is
# the owner's own approval.
OUTCOME_VPN_UNAVAILABLE = "vpn_unavailable"
OUTCOME_VPN_REFUSED = "vpn_refused"
OUTCOME_VPN_ERROR = "vpn_error"
OUTCOME_VPN_AWAITING_CONFIRMATION = "vpn_awaiting_confirmation"
# ── Releasing an action the assistant proposed ────────────────────────────
# "Recorded, nothing ran, the next step is the owner's" — the same kind of
# answer as ``OUTCOME_VPN_AWAITING_CONFIRMATION`` and for the same reason: it is
# a state rather than a failure, and counting it as one would send the owner
# looking for a problem that does not exist. It is deliberately **not** in
# ``_REFUSAL_OUTCOMES``.
OUTCOME_ADMIN_AWAITING_CONFIRMATION = "admin_awaiting_confirmation"
OUTCOME_ADMIN_CONFIRM_REFUSED = "admin_confirm_refused"

# Which interfaces can raise a request. Recorded in the audit trail, because
# "was this a person typing or a model proposing?" is the first question after
# an unexpected action.
INTERFACE_AI = "ai"
INTERFACE_PYTHON = "python"


# ── The request ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AdminRequest:
    """One administrative intention, fully typed.

    Note what is *absent*: there is no ``is_owner``, no ``actor_role``, no
    ``allowed``. Those are derived in :func:`authorize` from ``actor_id`` and
    the database, so a request cannot assert its own authority. The brief calls
    this out explicitly — *"The model must never be allowed to declare
    'is_owner=true' by itself"* — and the way to guarantee it is to leave no
    field for it to declare it in.
    """

    operation: str
    chat_id: int
    actor_id: int
    target_id: int = 0
    message_id: int = 0
    role: str = ""
    permissions: tuple[str, ...] = ()
    reason: str = ""
    # ── The coding-agent payload ──────────────────────────────────────────
    # Three data fields, and the distinction from an authority field is worth
    # stating because they are the newest thing here.
    #
    # ``repository`` is a *logical name*, not a path: it is resolved against the
    # allowlist in :mod:`app/agent_bridge`, and a name that is not in that table
    # is refused. A path is accepted only when it is exactly an allowlisted
    # root, and is then converted back to the name. So there is no string a
    # model could produce that becomes a directory this bot will hand to a
    # process with a shell.
    #
    # ``task`` is the owner's instruction in their own words. It is data — it is
    # read by the agent and never by an authorisation check.
    #
    # ``agent_operation`` is the model's *claim* about what kind of work this
    # is. It is validated against a closed table, and it can only ever make a
    # request *more* dangerous (see ``agent_bridge.danger_for``), never less.
    # Like ``role``, it is a value a caller may name and the application
    # interprets.
    repository: str = ""
    task: str = ""
    agent_operation: str = ""
    # How the owner wants a long answer back: ``text``, ``document`` or
    # ``both``. A preference and not a permission — an unknown value falls back
    # to ``text`` in the bridge, and no value here can change what is delivered
    # versus what is withheld.
    reply_mode: str = ""
    # ── The VPN payload ───────────────────────────────────────────────────
    # Ids and values for one operation against the VPN bot. All data, none of
    # it authority: the ids say *which* object, and whether the actor may touch
    # it is decided in ``authorize`` from the permission on the operation.
    #
    # ``enabled`` is a tri-state and that is the point. A service toggle has
    # three meaningful states — "turn it on", "turn it off" and "nobody said" —
    # and collapsing the third into ``False`` would make a model that forgot
    # the argument silently disable something. ``None`` is refused as malformed
    # by ``app/vpn_service.py``.
    service_id: int = 0
    plan_id: int = 0
    transaction_id: int = 0
    days: int = 0
    amount: int = 0
    enabled: bool | None = None
    compensate: bool = False
    status: str = ""
    # The id of a *recorded* VPN operation this request refers to. It is a
    # reference, not an approval: what it names is looked up, and everything the
    # execution needs is re-read from that row. Named ``pending_id`` rather than
    # ``request_id`` because ``request_id`` below is already the replay key, and
    # one name for two different things is how a replay key becomes a token.
    pending_id: str = ""
    request_id: str = ""
    interface: str = INTERFACE_PYTHON
    # When the request was created, as a unix timestamp. Checked against the
    # replay window: a request that has been sitting around is not acted on.
    at: int = 0

    def normalized(self) -> "AdminRequest":
        """Coerce every field to the type the rest of the module assumes.

        The AI interface produces these from JSON, where a user id may arrive as
        ``"123"`` or ``123`` and a role as ``"Moderator"``. Normalising here
        rather than at each use site means there is one answer to "what type is
        target_id", and it is ``int``.
        """
        def _int(value, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _tri_bool(value) -> bool | None:
            """A real boolean, or ``None`` when nobody said.

            ``bool("false")`` is ``True``, so a string is *not* coerced: a
            model that sends the word rather than the value has not expressed a
            state, and treating it as one would be guessing. ``int`` is accepted
            because the typed command path builds these in Python.
            """
            if isinstance(value, bool):
                return value
            if isinstance(value, int):
                return bool(value)
            return None

        def _flag(value) -> bool:
            decided = _tri_bool(value)
            return bool(decided)

        role = str(self.role or "").strip().lower()
        return replace(
            self,
            operation=str(self.operation or "").strip().lower(),
            chat_id=_int(self.chat_id),
            actor_id=_int(self.actor_id),
            target_id=_int(self.target_id),
            message_id=_int(self.message_id),
            role=ROLE_ALIASES.get(role, role),
            permissions=tuple(str(p) for p in (self.permissions or ()) if p),
            reason=str(self.reason or "")[:400],
            repository=str(self.repository or "").strip().lower()[:64],
            # Bounded at the same length the bridge stores. The bound is here as
            # well as there so that a request which never reaches the bridge
            # still cannot carry an unbounded string into an audit row.
            task=str(self.task or "").strip()[: int(db.AGENT_TASK_MAX_CHARS)],
            agent_operation=str(self.agent_operation or "").strip().lower()[:40],
            reply_mode=str(self.reply_mode or "").strip().lower()[:16],
            service_id=_int(self.service_id),
            plan_id=_int(self.plan_id),
            transaction_id=_int(self.transaction_id),
            days=_int(self.days),
            amount=_int(self.amount),
            enabled=_tri_bool(self.enabled),
            compensate=_flag(self.compensate),
            status=str(self.status or "").strip().lower()[:32],
            pending_id=str(self.pending_id or "").strip()[:64],
            request_id=str(self.request_id or "")[:120],
            interface=self.interface if self.interface in (INTERFACE_AI, INTERFACE_PYTHON)
            else INTERFACE_PYTHON,
            at=_int(self.at),
        )


@dataclass(frozen=True)
class AdminResult:
    """What happened, in terms the caller can act on.

    ``outcome`` is the machine key; ``message`` is a Persian sentence that is
    safe to send to a group. ``duplicate`` is separate from ``ok`` so a caller
    can say "already done" rather than "done" — the second is a lie that makes
    an operator think their second attempt had an effect.
    """

    ok: bool
    operation: str
    outcome: str
    reason: str = ""
    detail: str = ""
    actor_id: int = 0
    target_id: int = 0
    chat_id: int = 0
    request_id: str = ""
    duplicate: bool = False
    message: str = ""
    # Filled by the callers that need it (the assistant wants the target's
    # name); never used for authorisation.
    extra: dict = field(default_factory=dict)


# ── The Telegram gateway ──────────────────────────────────────────────────
@runtime_checkable
class Gateway(Protocol):
    """Everything this bot is able to do to Telegram, and nothing else.

    The narrowness is the security property. There is no ``call`` method, no way
    to pass a raw method name, and no access to the underlying bot — so the
    complete set of Telegram side effects reachable from an administrative
    request is the ten methods below, and reviewing that set is reviewing the
    whole attack surface.
    """

    async def bot_right(self, chat_id: int, right: str) -> bool:
        """Whether the bot itself holds an administrator right in a chat."""

    async def promote(self, chat_id: int, user_id: int, rights: dict) -> None:
        """Grant exactly ``rights`` (``promoteChatMember``)."""

    async def demote(self, chat_id: int, user_id: int) -> None:
        """Strip every administrator right the bot could have granted."""

    async def mute(self, chat_id: int, user_id: int) -> None:
        """Restrict posting for the configured mute duration."""

    async def unmute(self, chat_id: int, user_id: int) -> None:
        """Restore the ordinary member permission set."""

    async def ban(self, chat_id: int, user_id: int) -> None: ...

    async def unban(self, chat_id: int, user_id: int) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...

    async def warn(self, chat_id: int, user_id: int, reason: str) -> None:
        """Say the warning in the group, addressed to the user."""

    async def member(self, chat_id: int, user_id: int) -> dict:
        """The target's live Telegram status, as a plain dict. Read-only."""


# ── Time ──────────────────────────────────────────────────────────────────
def _now() -> int:
    return int(time.time())


def new_request_id() -> str:
    """A fresh id for one request. The replay key."""
    return uuid.uuid4().hex


def is_stale(request: AdminRequest, *, now: int | None = None) -> bool:
    """Whether a request is outside the replay window.

    A request with no timestamp is *not* treated as stale: the Python commands
    build requests in-process and stamp them at the moment of use, so a missing
    stamp means "just made", not "from the past". The AI path always stamps, so
    this only ever relaxes the in-process path, which cannot be replayed anyway
    because it has no external representation.
    """
    if not request.at:
        return False
    window = max(0, int(config.ADMIN_REQUEST_REPLAY_WINDOW))
    if window == 0:
        return False
    return (now if now is not None else _now()) - request.at > window


# ── Idempotency ───────────────────────────────────────────────────────────
def _seen(request: AdminRequest) -> dict | None:
    """The stored result for this request id, if it was already carried out."""
    if not request.request_id:
        return None
    return db.admin_request_get(request.request_id)


def _remember(request: AdminRequest, result: AdminResult) -> None:
    """Record that this request id was handled, and how it ended.

    Written for refusals as well as successes. A replayed *refusal* is also a
    replay: answering it twice from a stale row would let a denial be re-run
    after the actor's permissions had changed in either direction.
    """
    if not request.request_id:
        return
    try:
        db.admin_request_put(
            request.request_id,
            actor_id=request.actor_id,
            chat_id=request.chat_id,
            operation=request.operation,
            target_id=request.target_id,
            outcome=result.outcome,
            at=request.at or _now(),
        )
    except Exception:  # noqa: BLE001 - never the reason an action fails
        log.exception("could not record admin request id")


# ── Authorisation ─────────────────────────────────────────────────────────
def _denied(request: AdminRequest, decision: rbac.Decision, outcome: str) -> AdminResult:
    """A refusal, carrying the same sentence every other outcome carries.

    ``message`` is filled here rather than left empty because the field means
    "the Persian sentence for this outcome" wherever it appears. The command
    path happens to re-derive a more specific sentence from ``reason``, but the
    AI path hands this straight to the model, and a model told ``message: ""``
    has been given a sentence-shaped hole where the wording should be.
    """
    return AdminResult(
        ok=False,
        operation=request.operation,
        outcome=outcome,
        reason=decision.reason,
        detail=decision.detail,
        actor_id=request.actor_id,
        target_id=request.target_id,
        chat_id=request.chat_id,
        request_id=request.request_id,
        message=message_for(outcome),
    )


def authorize(
    request: AdminRequest, *, actor: rbac.Principal | None = None
) -> rbac.Decision:
    """Whether this request may proceed, judged only on its own contents.

    The actor is *always* re-resolved from ``actor_id`` unless a caller passes
    one in — and the only caller that does is the Python command path, which has
    already resolved the same id from the same Telegram update. Nothing that
    arrives over the wire, and nothing a model produces, can supply a principal.

    Promotions and demotions take a different road because they change the role
    table: they must satisfy ``authorize_grant``, which bounds *what* may be
    handed out, not merely *who* may act.
    """
    actor = actor if actor is not None else rbac.resolve(request.actor_id)

    operation = OPERATIONS.get(request.operation)
    if operation is None:
        return rbac.Decision(False, rbac.REASON_MISSING_PERMISSION, "unknown operation")

    # Only a *user-targeted* operation has a target the hierarchy rules are
    # about. This distinction is load-bearing, and it was found by a live run
    # rather than by a test: a VPN operation's ``target_id`` is the **subject**
    # of the change — the customer whose balance moves, whose reminders are
    # muted — and resolving it into a principal makes the owner-protection and
    # hierarchy checks fire against that customer. A balance change for the
    # owner would be refused as "the target is the owner", and one for an
    # administrator would be refused as "the target is at your own level".
    # Neither has anything to do with who may change a balance, which
    # ``vpn.manage`` has already decided.
    #
    # ``promote_member`` and ``demote_member`` are ``OP_USER`` and keep their
    # target; the system operations never carried one.
    target = (
        rbac.resolve(request.target_id)
        if request.target_id and operation.kind == OP_USER
        else None
    )

    if operation.changes_role:
        role = request.role or rbac.ROLE_MODERATOR
        if role not in rbac.ROLE_PERMISSIONS:
            return rbac.Decision(False, rbac.REASON_UNKNOWN_ROLE, role)
        # An explicit permission set is honoured when one is supplied, and
        # ``authorize_grant`` still bounds it twice over: nothing beyond the
        # role's own bundle, and nothing beyond what this actor may hand out.
        #
        # Only the Python confirmation UI ever supplies one — the operator
        # toggles individual permissions there. The AI path cannot, because
        # ``promote_member`` has no such parameter, which is how "the model may
        # not specify arbitrary Telegram rights" is enforced structurally
        # rather than by a check somebody has to remember.
        permissions = request.permissions or rbac.ROLE_PERMISSIONS[role]
        return rbac.authorize_grant(actor, role, permissions, target=target)

    return rbac.authorize(actor, operation.permission, target=target)


# ── Execution ─────────────────────────────────────────────────────────────
async def execute(
    request: AdminRequest,
    gateway: Gateway,
    *,
    actor: rbac.Principal | None = None,
    bot_id: int = 0,
) -> AdminResult:
    """Authorise one request, then carry it out. The whole pipeline, in order.

    The order below is the brief's list and it is not negotiable: shape, then
    system state, then replay, then actor, then permission, then target, then
    Telegram's own rights, then the call. Every step that can refuse does so
    *before* anything with a side effect has run, so a refusal never leaves a
    half-finished action behind.

    ``bot_id`` is passed in rather than read from the gateway because the
    gateway's protocol is deliberately about chat operations; the caller knows
    its own bot id.
    """
    request = request.normalized()

    # 1. Shape. An unknown operation, or a request with no chat or no actor, is
    #    refused before anything else — including before the replay lookup, so a
    #    malformed request cannot be used to probe the idempotency table.
    operation = OPERATIONS.get(request.operation)
    if operation is None:
        return _result(
            request, OUTCOME_UNKNOWN_OPERATION, detail=request.operation
        )
    if not request.chat_id or not request.actor_id:
        return _result(request, OUTCOME_MALFORMED, detail="chat_id/actor_id")

    # 2. Is the system in the state this request assumes?
    #
    #    The gate in ``app/main.py`` already refuses to *start* a conversation
    #    while Nexus is offline, so in the ordinary course of events this branch
    #    is never reached. It exists because "in the ordinary course" is not the
    #    same as "always": a tool turn can be in flight when the owner switches
    #    Nexus off, and the request it eventually produces must not be executed
    #    by a layer that has been told to stop. The brief lists "current system
    #    state" among the things the execution layer verifies independently, and
    #    this is where that check lives.
    #
    #    Only the AI interface is refused. The typed commands are the documented
    #    fallback for exactly the situation where the assistant is unavailable,
    #    and switching the assistant off must not switch moderation off with it.
    operation = OPERATIONS.get(request.operation)
    if (
        operation.requires_nexus_online
        and request.interface == INTERFACE_AI
        and not nexus.is_online()
    ):
        decision = rbac.Decision(False, rbac.REASON_NEXUS_OFFLINE)
        result = _denied(request, decision, OUTCOME_NEXUS_OFFLINE)
        _record(request, result, decision=decision)
        return result

    # 3. Replay window, then idempotency.
    if is_stale(request):
        return _result(request, OUTCOME_STALE)
    seen = _seen(request)
    if seen is not None:
        return _result(
            request,
            OUTCOME_DUPLICATE,
            duplicate=True,
            ok=seen.get("outcome") == OUTCOME_OK,
            detail=seen.get("outcome", ""),
        )

    # 4. The target must be real, and must not be the bot itself. Promoting the
    #    bot is a no-op that looks like a success, and banning it is worse.
    #    A system operation has no target — it is about the bot itself — so it
    #    is exempt rather than being made to carry a meaningless id.
    if operation.kind == OP_USER:
        if not request.target_id:
            return _result(request, OUTCOME_BAD_TARGET)
        if bot_id and request.target_id == bot_id:
            return _result(request, OUTCOME_TARGET_IS_BOT)
    elif operation.kind == OP_MESSAGE:
        if not request.message_id:
            return _result(request, OUTCOME_BAD_TARGET, detail="message_id")
    elif operation.kind == OP_VPN:
        # Nothing to validate here. The subject is an object inside another
        # service, and its per-operation requirements are the adapter's own
        # business — ``app/vpn_service.py`` refuses a missing amount by naming
        # the field. What this branch does is the pre-flight the kind exists
        # for: an operation against an integration this bot has not been pointed
        # at cannot succeed, so it is refused *here*, before anything is
        # recorded, rather than discovered later as an unreachable host. Either
        # way it lands in ``admin_audit`` as a refusal, never as a success.
        if not vpnbot.is_configured():
            result = _result(
                request, OUTCOME_VPN_UNAVAILABLE, detail="not_configured"
            )
            _record(request, result)
            return result

    # 5. Authorisation, resolved here, from the id. Nothing the caller said
    #    about itself is trusted, because nothing the caller said about itself
    #    is read.
    decision = authorize(request, actor=actor)
    if not decision:
        result = _denied(request, decision, OUTCOME_DENIED)
        _record(request, result, decision=decision)
        return result

    # 6. Telegram's own permission for this action, checked live. Configuration
    #    saying the bot should have a right is not evidence that it has one.
    #    Skipped for the operations whose outcome the application owns — see
    #    ``Operation.soft_right``.
    if (
        operation.right
        and not operation.soft_right
        and not await gateway.bot_right(request.chat_id, operation.right)
    ):
        result = _result(request, OUTCOME_BOT_LACKS_RIGHT, detail=operation.right or "")
        _record(request, result)
        return result

    # 6.5 Confirmation, for the operations where the assistant proposing is not
    #     the same as the owner asking.
    #
    #     Placed here rather than earlier, and the position is the point: every
    #     check that can refuse has already refused, so a request that would have
    #     been denied for any other reason is answered rather than turned into a
    #     question. Placed here rather than later, because everything below has a
    #     side effect and the whole purpose of this step is that nothing does.
    #
    #     Two conditions, and each excludes a case that must not be gated:
    #
    #     * ``INTERFACE_AI`` — a person typing ``/promote`` has stated the intent
    #       themselves and is present to see the result. Gating that would make
    #       every command two-step to guard against a risk that only exists when a
    #       model is the one reading the sentence.
    #     * ``pending_id`` — a request that names a recorded action is the
    #       *execution* of one, produced by ``_confirm_pending`` below. Without
    #       this, confirming would record a second pending action instead of
    #       carrying the first one out, and no confirmation could ever complete.
    #       It is not settable from outside: ``parse_write_call`` fills a field
    #       only when the tool's schema declares it, and only the confirm tools
    #       declare ``pending_id``.
    if (
        operation.needs_confirmation
        and request.interface == INTERFACE_AI
        and not request.pending_id
    ):
        return _record_pending(request, operation)

    # 7. The call itself. A Telegram failure is reported as a failure, never
    #    smoothed over — the brief is explicit that the caller must not fabricate
    #    success.
    try:
        if operation.name == "admin_confirm":
            note = await _confirm_pending(request, gateway, actor=actor, bot_id=bot_id)
        else:
            note = await _apply(request, operation, gateway)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        log.warning("admin action %s failed: %s", request.operation, exc)
        result = _result(
            request, OUTCOME_TELEGRAM_ERROR, detail=str(exc)[:160]
        )
        _record(request, result)
        return result

    # An operation carried out by another service returns its own result rather
    # than a note. The coding-agent operation is the only one: it makes no
    # Telegram call, so there is no "the application change went through and
    # Telegram refused" state for a note to describe, and it has more to report
    # than a sentence — the task's id, its status, and whether it is waiting for
    # the owner. Returning it whole keeps the pipeline below unchanged instead of
    # adding a second dispatch site in the middle of the authorisation steps.
    if isinstance(note, AdminResult):
        result = note
        _record(request, result)
        log.info(
            "admin action=%s interface=%s actor=%s outcome=%s",
            request.operation,
            request.interface,
            request.actor_id,
            result.outcome,
        )
        return result

    if note == OUTCOME_NOT_AN_ADMIN:
        result = _result(request, OUTCOME_NOT_AN_ADMIN)
        _record(request, result)
        return result

    result = _result(request, OUTCOME_OK, ok=True)
    if operation.changes_role:
        # What was granted, in the audit row. The old command wrote exactly this
        # string and the trail is read by an operator looking for it, so the
        # detail is reproduced rather than reinvented.
        role = request.role or rbac.ROLE_MODERATOR
        permissions = request.permissions or rbac.ROLE_PERMISSIONS[role]
        result = replace(
            result,
            detail=f"role={role} perms={','.join(sorted(permissions))}",
        )
    if note:
        # The application change went through and Telegram did not. The caller
        # shows the note beside the success rather than instead of it, which is
        # the honest description: two layers, one of which refused.
        result = replace(result, extra={"telegram_note": note})
    _record(request, result)
    log.info(
        "admin action=%s interface=%s actor=%s target=%s chat=%s",
        request.operation,
        request.interface,
        request.actor_id,
        request.target_id or request.message_id,
        request.chat_id,
    )
    return result


# ── Recording a proposed action, and releasing it ─────────────────────────
# The two halves of the ``needs_confirmation`` step. The shape is the one the
# VPN adapter already uses, and deliberately so: a second pattern for the same
# idea would be a second set of bugs in the part of the system that decides
# whether something runs.
#
# What the confirming request contributes is its own identity and the id of the
# recorded action. Everything the execution needs is re-read from the row
# written the first time — so a model that confirms "the thing I recorded"
# cannot smuggle a different target, a different role or a different operation
# into the call, and cannot ask for one action and have another run.
def _pending_subject(request: AdminRequest, operation: Operation) -> str:
    """A short, human-readable description of what is being proposed.

    Shown to the owner and written in the audit detail. Ids and a role name,
    never a message body and never anything the model wrote freehand — the same
    rule the audit row follows, for the same reason.
    """
    if operation.changes_role:
        role = request.role or rbac.ROLE_MODERATOR
        return f"target={request.target_id} role={role}"
    return operation.name


def _record_pending(request: AdminRequest, operation: Operation) -> AdminResult:
    """Write the proposed action down and ask. Executes nothing."""
    pending_id = new_request_id()
    now = _now()
    if not db.admin_pending_add(
        pending_id,
        actor_id=request.actor_id,
        chat_id=request.chat_id,
        operation=request.operation,
        subject=_pending_subject(request, operation),
        payload=json.dumps(
            {
                "target_id": int(request.target_id or 0),
                "message_id": int(request.message_id or 0),
                "role": str(request.role or ""),
                "reason": str(request.reason or ""),
            },
            separators=(",", ":"),
        ),
        expires_at=now + max(60, int(config.ADMIN_CONFIRMATION_TTL_SECONDS)),
        now=now,
    ):
        # The id was taken, which is not something the caller can cause but is
        # not worth pretending about either. Reporting a question about an action
        # that was never recorded would leave the owner approving nothing.
        return _result(request, OUTCOME_TELEGRAM_ERROR, detail="could_not_record")

    log.info(
        "admin action %s recorded as pending %s for actor %s (interface=%s)",
        request.operation,
        pending_id,
        request.actor_id,
        request.interface,
    )
    return replace(
        _result(request, OUTCOME_ADMIN_AWAITING_CONFIRMATION),
        extra={
            "pending": {
                "pending_id": pending_id,
                "operation": request.operation,
                "subject": _pending_subject(request, operation),
                "expires_at": now + max(60, int(config.ADMIN_CONFIRMATION_TTL_SECONDS)),
            }
        },
    )


def _request_from_pending(row: dict, confirming: AdminRequest) -> AdminRequest:
    """Rebuild the proposed action from the row that was recorded.

    This is the whole reason the confirmation is a reference and not an
    approval. The confirming request supplies its own ``request_id`` — so the
    execution is recorded against the request that caused it — and its
    ``pending_id``, which is also what keeps ``execute`` from recording a second
    proposal instead of carrying this one out.
    """
    try:
        payload = json.loads(str(row.get("payload") or "{}"))
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return AdminRequest(
        operation=str(row.get("operation") or ""),
        chat_id=int(row.get("chat_id") or 0),
        # The actor is the *confirming* one, re-derived from their own id, and
        # ``authorize`` will check it again from scratch. The owner may confirm
        # an action somebody else proposed — that is what the owner rule means —
        # so taking the actor from the row would execute the action as the wrong
        # person.
        actor_id=int(confirming.actor_id or 0),
        target_id=int(payload.get("target_id") or 0),
        message_id=int(payload.get("message_id") or 0),
        role=str(payload.get("role") or ""),
        reason=str(payload.get("reason") or ""),
        request_id=str(confirming.request_id or ""),
        pending_id=str(row.get("request_id") or ""),
        # The interface of the *original* request, kept so the audit row answers
        # "did a person do this, or did the assistant?" with the truth: the
        # assistant proposed it, and the owner released it. The pending id above
        # is what stops this from being gated a second time.
        interface=str(confirming.interface or INTERFACE_PYTHON),
        at=_now(),
    ).normalized()


async def _confirm_pending(
    request: AdminRequest,
    gateway: Gateway,
    *,
    actor: rbac.Principal | None = None,
    bot_id: int = 0,
) -> AdminResult:
    """Release one proposed action the owner has approved.

    Four refusals before anything runs, and each is a different next step for the
    owner: this is not yours to confirm, nothing is waiting, that reference is
    not waiting (the candidates come back so the next message can name one), or
    the reference is ambiguous because more than one action is.

    The four are decided by ``agent_bridge.resolve_confirmation``, which is the
    single place the owner rule is written — the same function the VPN adapter
    and the coding-agent bridge use. Reimplementing it here would be a second
    answer to "who may approve", and the two would eventually disagree.

    The claim is a compare-and-swap on one row, so two confirmations arriving
    together cannot both promote somebody.
    """
    actor_id = int(request.actor_id or 0)
    waiting = db.admin_pending_waiting(chat_id=int(request.chat_id or 0))
    decision = agent_bridge.resolve_confirmation(
        actor_id=actor_id,
        is_owner=rbac.is_owner(actor_id),
        named_request_id=str(request.pending_id or ""),
        waiting=waiting,
    )
    candidates = ", ".join(decision.candidates)

    if decision.answer is agent_bridge.Confirm.NOT_OWNER:
        return _result(
            request,
            OUTCOME_DENIED,
            detail=rbac.REASON_NOT_ADMIN,
            reason=rbac.REASON_NOT_ADMIN,
        )
    if decision.answer is agent_bridge.Confirm.NOTHING_PENDING:
        # Nothing live is waiting — and that is also what an action whose window
        # closed looks like from here, so a named reference is looked up to tell
        # the two apart. The lookup is *after* the resolver, never before it:
        # reaching this line means the owner check already passed, and moving the
        # lookup earlier would put a second copy of that rule here.
        detail = "expired" if _pending_expired(request.pending_id) else "nothing_pending"
        return _result(request, OUTCOME_ADMIN_CONFIRM_REFUSED, detail=detail)
    if decision.answer is agent_bridge.Confirm.NOT_WAITING:
        if _pending_expired(request.pending_id):
            return _result(request, OUTCOME_ADMIN_CONFIRM_REFUSED, detail="expired")
        return _result(
            request,
            OUTCOME_ADMIN_CONFIRM_REFUSED,
            detail=f"not_waiting {candidates}".strip(),
        )
    if decision.answer is agent_bridge.Confirm.AMBIGUOUS:
        # More than one action is waiting, so a bare «اوکی» is a question rather
        # than an approval. The ids go back so the next message can name one, and
        # the model is told never to pick.
        return replace(
            _result(request, OUTCOME_ADMIN_CONFIRM_REFUSED, detail="ambiguous"),
            extra={"candidates": list(decision.candidates)},
        )

    row = db.admin_pending_get(decision.request_id)
    if not row:
        return _result(request, OUTCOME_ADMIN_CONFIRM_REFUSED, detail="unknown_pending")
    if not db.admin_pending_claim(decision.request_id, actor_id=actor_id):
        # Lost the compare-and-swap: another confirmation took it between the
        # read above and this line, or the window closed in the same moment.
        # Either way nothing ran and the next step is to look again.
        return _result(
            request, OUTCOME_ADMIN_CONFIRM_REFUSED, detail="already_claimed"
        )

    # The recorded action, re-authorised from scratch by the ordinary pipeline:
    # all seven steps, against the confirming actor's real id. A confirmation
    # that bypassed authorisation would be a way to perform an action by first
    # proposing it, which is the opposite of what this step is for.
    result = await execute(
        _request_from_pending(row, request), gateway, actor=actor, bot_id=bot_id
    )
    if result.outcome == OUTCOME_TELEGRAM_ERROR:
        # Nothing was decided by a human or by a policy — the call failed — so
        # the action goes back and can be confirmed again. A refusal is not put
        # back: it is an answer, and re-asking would produce it again.
        db.admin_pending_release(decision.request_id)
    else:
        db.admin_pending_finish(
            decision.request_id, outcome=result.outcome, detail=result.detail
        )
    log.info(
        "admin pending %s confirmed by %s -> %s",
        decision.request_id,
        actor_id,
        result.outcome,
    )
    return result


def _pending_expired(pending_id: str) -> bool:
    """Whether the named action is one whose window has closed.

    Only a row that is *still pending* and past its deadline counts. A row whose
    status is ``confirmed`` or ``done`` is a different thing — it ran — and
    calling that "expired" would tell the owner their action was dropped when in
    fact it went through.
    """
    named = str(pending_id or "")
    if not named:
        return False
    row = db.admin_pending_get(named)
    if not row or row.get("status") != "pending":
        return False
    return int(row.get("expires_at") or 0) <= _now()


async def _apply(
    request: AdminRequest, operation: Operation, gateway: Gateway
) -> str | AdminResult:
    """Make the one call this operation names.

    Returns a note to show beside the success, an empty string, or — for the one
    operation that is carried out by another service rather than by Telegram — a
    complete :class:`AdminResult`.

    Only the two role-changing operations produce a note, and only when the
    application layer succeeded and Telegram did not.
    """
    chat_id = request.chat_id
    if request.operation == "ban_member":
        await gateway.ban(chat_id, request.target_id)
    elif request.operation == "unban_member":
        await gateway.unban(chat_id, request.target_id)
    elif request.operation == "mute_member":
        await gateway.mute(chat_id, request.target_id)
    elif request.operation == "unmute_member":
        await gateway.unmute(chat_id, request.target_id)
    elif request.operation == "warn_member":
        await gateway.warn(chat_id, request.target_id, request.reason)
    elif request.operation == "delete_message":
        await gateway.delete(chat_id, request.message_id)
    elif request.operation == "promote_member":
        return await _promote(request, gateway)
    elif request.operation == "demote_member":
        return await _demote(request, gateway)
    elif request.operation == "nexus_offline":
        nexus.set_state(nexus.OFFLINE, actor_id=request.actor_id, reason=request.interface)
    elif request.operation == "nexus_online":
        nexus.set_state(nexus.ONLINE, actor_id=request.actor_id, reason=request.interface)
    elif request.operation == "awareness_offline":
        # Imported here rather than at module scope for the same reason the two
        # branches below are: ``app/awareness.py`` is a peer that reads this
        # module's ``AdminResult``, and a module-scope import would make
        # "authorise a ban" depend on the awareness layer being importable.
        from . import awareness

        awareness.set_running(False, actor_id=request.actor_id, reason=request.interface)
    elif request.operation == "awareness_online":
        from . import awareness

        awareness.set_running(True, actor_id=request.actor_id, reason=request.interface)
    elif request.operation == "search_offline":
        # Imported here for the same reason the awareness branches above are:
        # ``app/web_search.py`` is a peer, and a module-scope import would make
        # "authorise a ban" depend on the search workload being importable.
        from . import web_search

        web_search.set_running(False, actor_id=request.actor_id, reason=request.interface)
    elif request.operation == "search_online":
        from . import web_search

        web_search.set_running(True, actor_id=request.actor_id, reason=request.interface)
    elif request.operation == "voice_context_offline":
        # Imported here for the same reason the awareness and search branches
        # above are: ``app/voice_context.py`` is a peer, and a module-scope
        # import would make "authorise a ban" depend on the voice workload being
        # importable.
        from . import voice_context

        voice_context.set_running(
            False, actor_id=request.actor_id, reason=request.interface
        )
    elif request.operation == "voice_context_online":
        from . import voice_context

        voice_context.set_running(
            True, actor_id=request.actor_id, reason=request.interface
        )
    elif request.operation == "register_group":
        # Imported here rather than at module scope, for the same reason the
        # awareness/search branches above are: ``app/groups.py`` is a peer, and a
        # module-scope import would make "authorise a ban" depend on the room
        # allowlist being importable.
        from . import groups

        groups.register(
            request.chat_id, actor_id=request.actor_id, interface=request.interface
        )
    elif request.operation == "unregister_group":
        from . import groups

        groups.revoke(
            request.chat_id, actor_id=request.actor_id, interface=request.interface
        )
    elif request.operation == "codebuddy_task":
        # Imported here rather than at module scope: the bridge imports this
        # module's peers, and a cycle at import time would make ``admin_service``
        # depend on the agent being importable in order to authorise a ban.
        from . import agent_service

        return await agent_service.submit(request)
    elif request.operation in VPN_OPERATIONS:
        # The same shape as the line above, for the same reason and one more:
        # ``app/vpn_service.py`` imports this module for its ``AdminResult``, so
        # a module-scope import here would be a cycle. Importing it at the point
        # of use keeps "authorise a ban" independent of whether the VPN adapter
        # can be imported at all.
        from . import vpn_service

        return await vpn_service.submit(request)
    else:  # pragma: no cover - OPERATIONS and this branch move together
        raise ValueError(f"unhandled operation {request.operation}")
    return ""


async def _promote(request: AdminRequest, gateway: Gateway) -> str:
    """Record the application role, then try to mirror it in Telegram.

    The order and the tolerance are both deliberate, and both are inherited from
    the command this replaces. The bot's own role table is the authoritative
    record of who may use this bot's commands; Telegram's administrator flag is
    a separate capability that the bot may or may not be able to grant, and the
    brief's own §35 describes the state where the two disagree. Writing the role
    first means a Telegram refusal cannot lose the operator's decision, and
    returning the refusal as a *note* means it is reported rather than hidden.

    The role's permissions decide the Telegram flags — never the caller. The
    brief forbids the model specifying ``can_delete_messages`` and friends, and
    the way that is enforced is that :class:`AdminRequest` has no field for
    them: the only thing a caller can name is a role, and ``rbac`` maps it.
    """
    role = request.role or rbac.ROLE_MODERATOR
    permissions = request.permissions or rbac.ROLE_PERMISSIONS[role]
    db.admin_set(
        request.target_id,
        role,
        permissions,
        granted_by=request.actor_id,
        note=f"via {request.interface}",
    )
    return await _mirror_in_telegram(
        gateway, request.chat_id, request.target_id, permissions
    )


async def _mirror_in_telegram(
    gateway: Gateway, chat_id: int, user_id: int, permissions
) -> str:
    """Grant the role's Telegram rights. Returns "" or a sentence to append.

    Four outcomes rather than two, and the distinction is the point: "the API
    refused" and "we chose not to ask" are different facts, and an operator
    acting on the first would chase a problem that does not exist. A role whose
    permissions map to no Telegram right at all is a legitimate state — the
    helper role is application-only — and it is not a failure.
    """
    rights = rbac.telegram_rights_for(permissions)
    if not rights:
        return config.ADMIN_PROMOTE_NO_TELEGRAM_TEXT
    if not await gateway.bot_right(chat_id, "can_promote_members"):
        log.warning("cannot promote in %s: the bot lacks can_promote_members", chat_id)
        return config.ADMIN_BOT_LACKS_RIGHT_TEXT
    try:
        await gateway.promote(chat_id, user_id, rights)
    except Exception as exc:  # noqa: BLE001 - reported as a note, never raised
        log.warning("promote_chat_member failed: %s", exc)
        return config.ADMIN_TELEGRAM_FAILED_TEXT
    return config.ADMIN_PROMOTE_TELEGRAM_TEXT


async def _demote(request: AdminRequest, gateway: Gateway) -> str:
    """Remove the application role, then strip the Telegram rights.

    Returns ``OUTCOME_NOT_AN_ADMIN`` when there was no application role to
    remove — the caller turns that into "there was nothing to do" rather than a
    success, because a demotion that removed nothing has not happened.
    """
    removed = db.admin_remove(request.target_id)
    if not removed:
        return OUTCOME_NOT_AN_ADMIN

    if not await gateway.bot_right(request.chat_id, "can_promote_members"):
        return config.ADMIN_PROMOTE_NO_TELEGRAM_TEXT
    try:
        await gateway.demote(request.chat_id, request.target_id)
    except Exception as exc:  # noqa: BLE001 - reported as a note, never raised
        log.warning("demotion in Telegram failed: %s", exc)
        return config.ADMIN_TELEGRAM_FAILED_TEXT
    return ""


# ── Result plumbing ───────────────────────────────────────────────────────
def _result(
    request: AdminRequest,
    outcome: str,
    *,
    ok: bool = False,
    detail: str = "",
    duplicate: bool = False,
) -> AdminResult:
    return AdminResult(
        ok=ok,
        operation=request.operation,
        outcome=outcome,
        detail=detail,
        actor_id=request.actor_id,
        target_id=request.target_id or request.message_id,
        chat_id=request.chat_id,
        request_id=request.request_id,
        duplicate=duplicate,
        message=message_for(outcome),
    )


def _record(
    request: AdminRequest,
    result: AdminResult,
    *,
    decision: rbac.Decision | None = None,
) -> None:
    """Write the audit row and remember the request id.

    The audit row is written for refusals too. "Who tried" is the question asked
    after an incident, and a trail that only records successes cannot answer it.

    The role is resolved from ``rbac`` here rather than taken from the request,
    and that direction is the point: the request is the thing being audited, so
    a request that named its own authority would be writing its own alibi. It is
    resolved at write time, which is the moment the action happened — a later
    promotion or demotion must not rewrite what a past action was taken with.
    """
    operation = OPERATIONS.get(request.operation)
    try:
        db.audit_write(
            request.actor_id,
            operation.audit_action if operation else request.operation,
            outcome=result.outcome,
            target_id=request.target_id or request.message_id or None,
            chat_id=request.chat_id or None,
            detail=(
                (decision.detail or decision.reason)
                if decision is not None and not decision.allowed
                else result.detail
            ),
            interface=request.interface,
            role=rbac.resolve(request.actor_id).role,
            request_id=request.request_id,
        )
    except Exception:  # noqa: BLE001
        log.exception("audit write failed action=%s", request.operation)
    _remember(request, result)
    _maybe_prune()
    # The same action, recorded in the observation archive beside the turns it
    # affected. It is deliberately a *second* record and not a replacement for
    # the audit row: the audit is the accountability trail the panel reads, and
    # this is the investigation trail that lets "the behaviour changed at 14:02"
    # be tied to the command that changed it. Never raises.
    try:
        from . import observe

        if observe.started():
            observe.emit(
                observe.schema.KIND_ADMIN,
                ok=bool(result.ok),
                event=result.operation or request.operation,
                error="" if result.ok else (result.outcome or "refused"),
                chat_id=int(request.chat_id or 0),
                user_id=int(request.actor_id or 0),
                data={
                    "outcome": result.outcome,
                    "reason": result.reason,
                    "detail": (result.detail or "")[:300],
                    "target_id": int(request.target_id or 0),
                    "interface": request.interface,
                    "role": rbac.resolve(request.actor_id).role,
                    "request_id": request.request_id,
                    "duplicate": bool(result.duplicate),
                    "allowed": None if decision is None else bool(decision.allowed),
                },
            )
    except Exception:  # noqa: BLE001 — a record must never break an action
        pass


# ── Retention ─────────────────────────────────────────────────────────────
# The two tables below are written here, so they are bounded here. Both windows
# already existed as configuration and neither was ever applied: ``prune`` was
# written, documented as "called from the administrative path", and had no
# caller — so the only thing bounding the audit trail was an operator
# remembering to ask, which is the same as no bound at all.
#
# Neither table is ever dropped wholesale. The brief is explicit that
# accountability survives, so what this does is apply a *window* — 90 days of
# activity, one day of idempotency — and never a truncation.
PRUNE_EVERY = 200
_since_prune = 0


def _maybe_prune() -> None:
    global _since_prune
    _since_prune += 1
    if _since_prune < PRUNE_EVERY:
        return
    _since_prune = 0
    prune()


def prune() -> None:
    """Apply the administrative retention windows. Best effort; never raises.

    Called from ``_record``, which is the one place an administrative request
    reaches regardless of outcome — a refusal is written down too, and a trail
    that only bounded itself on success would grow fastest on the requests that
    were denied.

    ``admin_pending_ops`` is bounded here rather than from a counter of its own.
    It is written by this module and read by this module, and ``_record`` runs on
    every administrative request including the ones that propose an action — so
    the hook that already exists is the right one, and a second counter would
    only be a second thing to keep in step.
    """
    try:
        db.audit_prune(int(config.ADMIN_ACTIVITY_RETENTION))
        db.admin_request_prune(int(config.ADMIN_IDEMPOTENCY_RETENTION))
        db.admin_pending_prune(int(config.ADMIN_PENDING_RETENTION_SECONDS))
    except Exception:  # noqa: BLE001
        log.exception("admin retention prune failed")


def prune_reset() -> None:
    """Forget the prune counter. For tests."""
    global _since_prune
    _since_prune = 0


# ── Sentences ─────────────────────────────────────────────────────────────
# One key, one sentence, in the same place — so the AI and the command paths
# cannot describe the same outcome two different ways.
def message_for(outcome: str) -> str:
    return {
        OUTCOME_OK: config.ADMIN_DONE_TEXT,
        OUTCOME_DUPLICATE: config.ADMIN_DUPLICATE_TEXT,
        OUTCOME_MALFORMED: config.ADMIN_DENIED_TEXT,
        OUTCOME_UNKNOWN_OPERATION: config.ADMIN_DENIED_TEXT,
        OUTCOME_STALE: config.ADMIN_STALE_TEXT,
        OUTCOME_DENIED: config.ADMIN_DENIED_TEXT,
        OUTCOME_BOT_LACKS_RIGHT: config.ADMIN_BOT_LACKS_RIGHT_TEXT,
        OUTCOME_TELEGRAM_ERROR: config.MOD_COMMAND_FAILED_TEXT,
        OUTCOME_BAD_TARGET: config.MOD_TARGET_REQUIRED_TEXT,
        OUTCOME_TARGET_IS_BOT: config.ADMIN_TARGET_IS_BOT_TEXT,
        OUTCOME_UNKNOWN_ROLE: config.ADMIN_DENIED_TEXT,
        OUTCOME_NOT_AN_ADMIN: config.ADMIN_DEMOTE_NOTHING_TEXT,
        OUTCOME_NEXUS_OFFLINE: config.NEXUS_OFFLINE_DENIED_TEXT,
        OUTCOME_AGENT_DISABLED: config.AGENT_DISABLED_TEXT,
        OUTCOME_AGENT_REJECTED: config.AGENT_REJECTED_TEXT,
        OUTCOME_AGENT_BUSY: config.AGENT_BUSY_TEXT,
        OUTCOME_AGENT_DUPLICATE: config.AGENT_DUPLICATE_TEXT,
        OUTCOME_AGENT_WAITING: config.AGENT_WAITING_TEXT,
        OUTCOME_VPN_UNAVAILABLE: config.VPN_UNAVAILABLE_TEXT,
        OUTCOME_VPN_REFUSED: config.VPN_REFUSED_TEXT,
        OUTCOME_VPN_ERROR: config.VPN_FAILED_TEXT,
        OUTCOME_VPN_AWAITING_CONFIRMATION: config.VPN_AWAITING_CONFIRMATION_TEXT,
        OUTCOME_ADMIN_AWAITING_CONFIRMATION: config.ADMIN_AWAITING_CONFIRMATION_TEXT,
        OUTCOME_ADMIN_CONFIRM_REFUSED: config.ADMIN_CONFIRM_REFUSED_TEXT,
    }.get(outcome, config.ADMIN_DENIED_TEXT)


# English glosses for the machine keys. They exist for exactly one audience: the
# model, which has to explain a refusal to a person in Persian and cannot do
# that from the token ``higher_rank``. They are never shown to a user directly —
# the Persian sentence is — so this is not a second copy of the copy.
REASON_GLOSS = {
    rbac.REASON_NO_OWNER: "no owner is configured for this bot, so no administrative action is authorised at all",
    rbac.REASON_NOT_ADMIN: "the person asking is not an administrator of this bot",
    rbac.REASON_MISSING_PERMISSION: "the person asking does not hold the permission this action requires",
    rbac.REASON_OWNER_PROTECTED: "the target is the bot's owner, who can never be the target of an administrative action",
    rbac.REASON_HIGHER_RANK: "the target is at or above the asker's own level in the hierarchy",
    rbac.REASON_CANNOT_GRANT_ROLE: "the asker is not allowed to hand out that role",
    rbac.REASON_CANNOT_GRANT_PERMISSION: "the asker is not allowed to hand out those permissions",
    rbac.REASON_UNKNOWN_ROLE: "that role does not exist",
    rbac.REASON_SELF_TARGET: "the target is the person asking",
    rbac.REASON_BAD_TARGET: "the target is not usable",
    rbac.REASON_NEXUS_OFFLINE: "the assistant is switched off, so it is not acting on anything",
}

OUTCOME_GLOSS = {
    OUTCOME_DUPLICATE: "this exact request had already been carried out, so it was not repeated",
    OUTCOME_MALFORMED: "the request was incomplete and was not executed",
    OUTCOME_UNKNOWN_OPERATION: "the requested operation does not exist",
    OUTCOME_STALE: "the request was too old to act on and was not executed",
    OUTCOME_BOT_LACKS_RIGHT: "the bot itself does not have the required Telegram permission in this group",
    OUTCOME_TELEGRAM_ERROR: "Telegram refused the operation",
    OUTCOME_BAD_TARGET: "no usable target was identified",
    OUTCOME_TARGET_IS_BOT: "the target was this bot itself",
    OUTCOME_UNKNOWN_ROLE: "that role does not exist",
    OUTCOME_NOT_AN_ADMIN: "that person holds no administrative role here",
    OUTCOME_NEXUS_OFFLINE: "the assistant is switched off and did not act",
    OUTCOME_AGENT_DISABLED: "the coding-agent bridge is switched off",
    OUTCOME_AGENT_REJECTED: (
        "the request was not accepted — an unknown repository, an unknown "
        "operation, or an empty task"
    ),
    OUTCOME_AGENT_BUSY: (
        "the bridge is already running as many tasks as it is allowed to, or "
        "one of them is already working on that repository"
    ),
    OUTCOME_AGENT_DUPLICATE: "this exact request has already been made and is still active",
    OUTCOME_AGENT_WAITING: (
        "the task is recorded but has not started: it is a dangerous operation "
        "and is waiting for the owner to confirm it explicitly"
    ),
    OUTCOME_VPN_UNAVAILABLE: (
        "the VPN service could not be reached at all, so nothing was changed"
    ),
    OUTCOME_VPN_REFUSED: (
        "the VPN service answered and refused: an unknown id, a panel error, or "
        "a value it will not accept"
    ),
    OUTCOME_VPN_ERROR: (
        "the operation did not complete because of a problem on this side of "
        "the wire, not a decision by the VPN service"
    ),
    OUTCOME_VPN_AWAITING_CONFIRMATION: (
        "the operation was recorded but has not run: it moves money or rejects "
        "orders, and is waiting for the owner to confirm it explicitly"
    ),
    OUTCOME_ADMIN_AWAITING_CONFIRMATION: (
        "the action was recorded but has not run: it would silence the "
        "assistant or change somebody's role, and is waiting for the owner to "
        "confirm it explicitly. Tell the owner what you recorded and ask them "
        "to confirm; do not say it is done"
    ),
    OUTCOME_ADMIN_CONFIRM_REFUSED: (
        "the confirmation was not accepted: the person asking is not the owner, "
        "nothing was waiting, or the reference did not name a waiting action"
    ),
}


def explain(result: AdminResult) -> str:
    """One English sentence for the model to build a Persian answer on.

    Deliberately a gloss and not a sentence to repeat: the brief asks for
    natural conversation, and a model that echoes a canned string has stopped
    being conversational. This tells it *why*, and leaves the wording to it.
    """
    if result.outcome == OUTCOME_DENIED and result.reason:
        return REASON_GLOSS.get(result.reason, "the action was refused")
    return OUTCOME_GLOSS.get(result.outcome, "the action did not go through")


def describe(result: AdminResult) -> dict:
    """A log-safe summary. Ids and keys only — never a credential, never text."""
    return {
        "operation": result.operation,
        "outcome": result.outcome,
        "ok": result.ok,
        "duplicate": result.duplicate,
        "actor_id": result.actor_id,
        "target_id": result.target_id,
        "chat_id": result.chat_id,
        "reason": result.reason,
    }


# ── Mode status ───────────────────────────────────────────────────────────
def ai_available() -> bool:
    """Whether AI-mediated administration can run right now.

    Three conditions, and all must hold: the operator must have switched it on,
    there must be a conversational credential to talk to, and the conversational
    layer must be awake. A switched-on feature with no key is not available, and
    reporting it as available would be the "silently pretend Gemini succeeded"
    failure the brief names. Nexus being offline is the third case, and it is
    the one an operator is most likely to be confused by — everything looks
    configured, and nothing answers.
    """
    if not config.ADMIN_AI_ENABLED:
        return False
    if not nexus.is_online():
        return False
    from . import chat  # imported late: chat imports this module's config peers

    return chat.is_enabled()


def python_fallback_available() -> bool:
    """Whether the direct commands still work. They always do, unless disabled."""
    return bool(config.ADMIN_PYTHON_ENABLED)


def mode_status() -> dict:
    """The operational line: which mode is live, and what is behind it.

    Four states rather than three, because "AI is off", "AI is broken" and "the
    owner switched Nexus off" are different facts and an operator needs to tell
    them apart. The fourth is separate from the other three on purpose: it is
    the only one that is *deliberate*, and an operator looking at a silent bot
    needs to know whether they are looking at a fault or at their own decision.

    * ``ai``       — AI administration is up.
    * ``offline``  — Nexus is switched off by the owner.
    * ``degraded`` — AI is configured but unavailable; the commands are the way.
    * ``python``   — AI administration is switched off by configuration.
    """
    online = nexus.is_online()
    ai = ai_available()
    fallback = python_fallback_available()
    if ai:
        mode = "ai"
    elif not online:
        mode = "offline"
    elif config.ADMIN_AI_ENABLED and fallback:
        mode = "degraded"
    else:
        mode = "python"
    return {
        "mode": mode,
        "ai_available": ai,
        "python_fallback": fallback,
        "ai_enabled": bool(config.ADMIN_AI_ENABLED),
        "nexus_online": online,
    }


def mode_line() -> str:
    """The one-line status for the startup log and the owner's report."""
    state = mode_status()
    if state["mode"] == "ai":
        return "AI ADMIN MODE: AVAILABLE"
    if state["mode"] == "offline":
        return "AI ADMIN MODE: OFF — NEXUS IS SWITCHED OFF"
    if state["mode"] == "degraded":
        return "AI ADMIN MODE: DEGRADED — PYTHON FALLBACK ACTIVE"
    return "AI ADMIN MODE: OFF — PYTHON COMMANDS ONLY"


# Outcomes that mean "nothing happened, and here is why". A duplicate is
# deliberately absent: the desired state does hold, it was simply reached
# earlier, and counting it as a failure would make an operator chase a problem
# that does not exist.
_REFUSAL_OUTCOMES = frozenset({
    OUTCOME_DENIED,
    OUTCOME_BOT_LACKS_RIGHT,
    OUTCOME_TELEGRAM_ERROR,
    OUTCOME_MALFORMED,
    OUTCOME_STALE,
    OUTCOME_BAD_TARGET,
    OUTCOME_TARGET_IS_BOT,
    OUTCOME_UNKNOWN_ROLE,
    OUTCOME_UNKNOWN_OPERATION,
    OUTCOME_NEXUS_OFFLINE,
    OUTCOME_AGENT_DISABLED,
    OUTCOME_AGENT_REJECTED,
    OUTCOME_AGENT_BUSY,
    OUTCOME_VPN_UNAVAILABLE,
    OUTCOME_VPN_REFUSED,
    OUTCOME_VPN_ERROR,
    # A confirmation that was refused — the wrong person asked, nothing was
    # waiting, or the reference did not resolve. That is a refusal.
    #
    # ``OUTCOME_ADMIN_AWAITING_CONFIRMATION`` is deliberately absent: nothing
    # failed, nothing ran, and the next step is the owner's own approval. See the
    # note where it is defined.
    OUTCOME_ADMIN_CONFIRM_REFUSED,
})


def recent_refusals(limit: int = 5, *, chat_id: int | None = None) -> list[dict]:
    """Recent administrative requests that did not happen, newest first.

    Read from the audit table rather than kept in memory, because the question
    this answers — "why has nothing been working?" — is usually asked after a
    restart, and a counter that resets with the process would answer it with a
    confident zero.

    The scan is bounded: the audit table is ordered by id, so a window of recent
    rows is enough, and it is the refusals inside that window that matter.

    ``chat_id`` scopes the scan to one room, so a report shown inside a group
    can only ever describe that group's own refusals and never another tenant's.
    """
    window = max(1, int(limit))
    rows = db.audit_recent(window * 6, chat_id=chat_id)
    return [r for r in rows if r.get("outcome") in _REFUSAL_OUTCOMES][:window]


def status_report(*, chat_id: int | None = None) -> str:
    """The operator's view of AI administration: the mode, and what was refused.

    Both halves are needed to tell the two failure shapes apart. "The assistant
    never answers" and "the assistant answers and is refused every time" look
    identical from inside a group and are completely different problems; the
    first is this mode line, the second is the refusal list.

    The mode is deployment-wide (Nexus is either online or not), but the refusal
    list is scoped to ``chat_id`` when given: the report is rendered inside a
    room, and one room's administrative history must not appear in another's.
    """
    state = mode_status()
    if state["mode"] == "ai":
        headline = "Conversational administration is answering."
    elif state["mode"] == "offline":
        headline = (
            "Nexus is switched off, so the assistant is not answering anybody. "
            "The commands still work; /nexus on wakes it."
        )
    elif state["mode"] == "degraded":
        headline = (
            "Gemini is configured but not answering, so the commands are the "
            "way in. They work; nothing here is broken."
        )
    else:
        headline = "AI administration is switched off. Use the commands."

    lines = [mode_line(), headline]

    refusals = recent_refusals(chat_id=chat_id)
    lines.append("")
    if not refusals:
        lines.append("No administrative refusals on record.")
        return "\n".join(lines)

    lines.append(f"Recent refusals ({len(refusals)}):")
    for row in refusals:
        via = row.get("interface") or "-"
        lines.append(
            f"  • {row['action']} actor={row['actor_id']} "
            f"outcome={row['outcome']} via={via}"
        )
    return "\n".join(lines)


def reset_state() -> None:
    """No module-level mutable state; kept for the test-reset convention."""
    return None
