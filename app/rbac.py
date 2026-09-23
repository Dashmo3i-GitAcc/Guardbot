"""Who is allowed to do what, and on whom.

This module is the application's authority model. It answers one question —
"may *this* Telegram user perform *this* action, on *that* user?" — and it is
deliberately the only place that answers it. Every administrative command in
``app/main.py`` asks here and does what it is told; none of them decides for
itself.

Three ideas, and the rest follows:

**The owner is configuration, not a row.** ``OWNER_USER_ID`` comes from the
environment and is compared, never looked up. There is no function in this
module that can create, modify or remove the primary authority, which is what
makes "you cannot promote yourself to owner" a property of the design rather
than a check somebody has to remember to write. With no owner configured
(``OWNER_USER_ID=0``) every administrative action is refused — the system fails
closed, because the alternative ("the first admin wins", "the whitelist is the
owner") is how the wrong person ends up in charge.

**Permissions are the model; roles are a convenience.** The vocabulary is
``PERMISSIONS`` — a small closed set of things the bot can actually do. A role
is a named bundle of them. Authorisation compares *permissions*, never role
names, so adding a role cannot accidentally widen an existing one, and an
operator can hand out an unusual combination without inventing a new role.

**Telegram is the floor, not the ceiling.** Application permissions can only
*restrict* what an administrator may request. They can never grant a capability
Telegram has not given the bot, and they are not a substitute for Telegram's own
administrator rights — those are checked separately, live, at the moment of the
action (``app/main.py``). This module models the application's own layer and
says so in ``TELEGRAM_RIGHTS`` rather than pretending the two are the same
thing.

What this module deliberately does **not** do: it does not look at usernames,
display names, message text or any other user-controlled string. Authority is
keyed on Telegram user ids, which the Telegram servers assert, and on rows in
the ``admins`` table, which only an authorised actor can write.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config, db

# ── The permission vocabulary ─────────────────────────────────────────────
# Every entry is something the bot can actually do, or actually needs to know.
# Nothing here is aspirational: if a permission has no enforcement point it does
# not belong in the list, because a permission that is never checked reads like
# a control and behaves like a comment.
PERMISSIONS = (
    # Read-only: see the review queue and the audit trail. The floor, and the
    # only permission a purely observational administrator needs.
    "moderation.review",
    # Warn a user, in the group, without touching their ability to post.
    "moderation.warn",
    # Delete a message.
    "moderation.delete",
    # Restrict a user's ability to post for a bounded time.
    "moderation.mute",
    # Ban and unban. Distinct from mute because it is the one action that
    # survives the session and is visible to the whole group.
    "moderation.ban",
    # Create, change and remove other application administrators. Bounded by
    # `grantable_permissions`, so this is not "hand out anything".
    "admins.manage",
    # See and change runtime configuration (the bot's own settings surface).
    "config.manage",
    # Use the bot's commands at all. Every other permission implies this; it is
    # listed so an administrator can be created who may run the read-only
    # commands and nothing else, without a special case in the handler.
    "commands.use",
    # Turn the conversational layer (Nexus) on and off.
    #
    # This is the one permission that is deliberately in **no** role bundle, so
    # it is held by the owner and by nobody else. The reason is the same one
    # that keeps the owner out of the `admins` table: switching Nexus off is a
    # control over the bot's own behaviour rather than an action inside a group,
    # and "an administrator who can silence the assistant" is a capability with
    # no legitimate day-to-day use. Because no role carries it,
    # ``authorize_grant`` refuses to hand it out even to the owner's own
    # promotion dialog — there is no combination of role and permission set that
    # can express it.
    #
    # Appended last on purpose: ``PERMISSIONS`` is the wire format of the
    # promotion dialog's permission bitmask (``main._MASK_PERMISSIONS``), and
    # inserting anywhere else would renumber every existing bit in a dialog that
    # may already be open in somebody's Telegram client.
    "nexus.control",
    # Ask the coding agent to change one of this system's own repositories.
    #
    # Owner-only for the same structural reason as ``nexus.control``, and for
    # one more: the thing on the other end of this permission is a process with
    # a filesystem, a shell and a git remote. Handing it to an administrator
    # would make "an administrator" and "somebody who can edit the code that
    # runs this bot" the same role, and no role bundle here is meant to mean
    # that. Like ``nexus.control`` it is carried by no role, so
    # ``authorize_grant`` cannot express it either.
    #
    # Appended after ``nexus.control`` and not before it: the same bitmask
    # argument applies to both — see the note above.
    "agent.request",
    # Read the VPN project's own records through its signed service API:
    # subscriptions, service status, whether its panel is wired. Owner-only
    # because even after the other side drops the subscription link and the
    # panel client id, what remains describes a customer's account.
    "vpn.read",
    # Change something on the VPN side. Owner-only, permanently, and never
    # carried by a role: three of the six operations behind it move money or
    # bulk-reject orders, and the other three change whether a paying customer
    # has service. That is a different kind of power from moderating a chat.
    #
    # Both are appended last for the bitmask reason above. ``main.py``'s
    # ``_MASK_PERMISSIONS`` is built from this tuple by index, so inserting
    # anywhere but the end would silently re-point every stored mask.
    "vpn.manage",
    # Approve an administrative action the assistant proposed but did not take.
    #
    # Owner-only, and carried by no role, for the reason the whole two-step
    # exists: the operations it releases are the ones where a model's mistake is
    # either invisible or a grant of authority — promoting somebody, or
    # silencing the assistant that would have reported it. If an administrator
    # could confirm their own request, the step would be a formality and would
    # protect nothing, so the confirmer is the owner by construction.
    #
    # Appended last, per the note above.
    "admin.confirm",
)

PERMISSION_SET = frozenset(PERMISSIONS)

# The permissions no role may carry. Asserted against ``ROLE_PERMISSIONS`` in the
# test suite rather than enforced by a filter here, because a filter would make
# the omission silent: this list exists so that "nexus.control is owner-only" is
# a checked property of the tables below rather than a fact somebody has to
# notice while editing them.
OWNER_ONLY_PERMISSIONS = frozenset(
    {"nexus.control", "agent.request", "vpn.read", "vpn.manage", "admin.confirm"}
)

# The permission implied by every other one. Held by every principal, including
# a guest, so a handler never has to special-case it.
BASE_PERMISSION = "commands.use"

# ── Roles ─────────────────────────────────────────────────────────────────
# Named bundles. `level` orders them for the protection rules: an actor may
# never act administratively on a principal whose level is greater than or equal
# to their own (the owner is the exception, and is handled by id, not by level).
ROLE_OWNER = "owner"
ROLE_SENIOR_ADMIN = "senior_admin"
ROLE_ADMIN = "admin"
ROLE_MODERATOR = "moderator"
ROLE_HELPER = "helper"
ROLE_GUEST = "guest"

ROLE_LEVELS = {
    ROLE_OWNER: 100,
    ROLE_SENIOR_ADMIN: 60,
    ROLE_ADMIN: 50,
    ROLE_MODERATOR: 40,
    ROLE_HELPER: 20,
    ROLE_GUEST: 0,
}

# What each assignable role carries. `owner` is absent on purpose: it is not
# assignable, so there is no bundle to look up.
#
# `admin` sits between `moderator` and `senior_admin` and is the role the brief
# calls "Admin": a moderator who may also ban. It deliberately does **not**
# carry `admins.manage` or `config.manage`, so "make this person an admin" is
# not a way to hand out the authority to mint other administrators — that is
# what `senior_admin` is, and only the owner may create one.
ROLE_PERMISSIONS = {
    ROLE_HELPER: frozenset({BASE_PERMISSION, "moderation.review", "moderation.warn"}),
    ROLE_MODERATOR: frozenset(
        {
            BASE_PERMISSION,
            "moderation.review",
            "moderation.warn",
            "moderation.delete",
            "moderation.mute",
        }
    ),
    ROLE_ADMIN: frozenset(
        {
            BASE_PERMISSION,
            "moderation.review",
            "moderation.warn",
            "moderation.delete",
            "moderation.mute",
            "moderation.ban",
        }
    ),
    ROLE_SENIOR_ADMIN: frozenset(
        {
            BASE_PERMISSION,
            "moderation.review",
            "moderation.warn",
            "moderation.delete",
            "moderation.mute",
            "moderation.ban",
            "admins.manage",
            "config.manage",
        }
    ),
    ROLE_GUEST: frozenset(),
}

# Which roles each role may hand out. This is a second, independent bound on top
# of the permission-subset rule below, because they catch different mistakes:
# the subset rule stops "grant something you do not hold", and this stops
# "create a peer". A senior admin can build the moderation team; only the owner
# can build another senior admin — and only the owner can create an `admin`,
# because an admin may ban and a senior admin's own grant list deliberately
# stops one rung lower.
GRANTABLE_ROLES = {
    ROLE_OWNER: (ROLE_HELPER, ROLE_MODERATOR, ROLE_ADMIN, ROLE_SENIOR_ADMIN),
    ROLE_SENIOR_ADMIN: (ROLE_HELPER, ROLE_MODERATOR),
    ROLE_ADMIN: (),
    ROLE_MODERATOR: (),
    ROLE_HELPER: (),
    ROLE_GUEST: (),
}

# ── Persian labels ────────────────────────────────────────────────────────
# These live beside the vocabulary rather than in app/config.py because they are
# not operator copy: they are the names of these exact keys, and a permission
# added here without a label would render as a raw key in the selection UI. The
# module's rule is that the vocabulary and its labels move together.
PERMISSION_LABELS = {
    "moderation.review": "دیدن صف بازبینی",
    "moderation.warn": "اخطار دادن به کاربر",
    "moderation.delete": "حذف پیام",
    "moderation.mute": "محدود کردن موقت کاربر",
    "moderation.ban": "بن کردن کاربر",
    "admins.manage": "مدیریت مدیرها",
    "config.manage": "تغییر تنظیمات ربات",
    "commands.use": "استفاده از دستورهای ربات",
    "nexus.control": "روشن/خاموش کردن نکسوس",
    "agent.request": "درخواست از عامل برنامه‌نویسی",
    "vpn.read": "دیدن اطلاعات سرویس VPN",
    "vpn.manage": "تغییر سرویس‌های VPN",
    "admin.confirm": "تأیید کارهای مدیریتی پیشنهادی دستیار",
}
ROLE_LABELS = {
    ROLE_OWNER: "مالک",
    ROLE_SENIOR_ADMIN: "مدیر ارشد",
    ROLE_ADMIN: "مدیر",
    ROLE_MODERATOR: "ناظر",
    ROLE_HELPER: "کمک‌ناظر",
    ROLE_GUEST: "کاربر",
}

# ── Telegram's own rights ─────────────────────────────────────────────────
# What the bot must hold to perform each application action, expressed with the
# real ``ChatAdministratorRights`` field names. Nothing here is invented: these
# are the flags the Bot API accepts in ``promoteChatMember``.
#
# `None` means the action needs no Telegram administrator right at all (it is
# either read-only or performed with the ordinary message-delete right the bot
# already uses for moderation).
TELEGRAM_RIGHTS_FOR_ACTION = {
    "moderation.delete": "can_delete_messages",
    "moderation.mute": "can_restrict_members",
    "moderation.ban": "can_restrict_members",
    "admins.manage": "can_promote_members",
    "config.manage": "can_manage_chat",
}

# Which application permissions correspond to which Telegram administrator
# rights when an administrator is promoted in Telegram as well as in this
# application. This is the mapping the permission-selection UI uses: a box the
# operator ticks either has a Telegram counterpart or it does not, and the UI
# says which, because "I granted can_delete_messages but nothing changed in
# Telegram" is exactly the confusion this table exists to prevent.
TELEGRAM_RIGHTS = (
    "is_anonymous",
    "can_manage_chat",
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_post_messages",
    "can_edit_messages",
    "can_pin_messages",
    "can_manage_topics",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
)

# Application permission -> the Telegram right it would turn on. Only the
# permissions that have a real counterpart appear; the rest are application-only
# and the UI marks them as such.
PERMISSION_TELEGRAM_RIGHT = {
    "moderation.delete": "can_delete_messages",
    "moderation.mute": "can_restrict_members",
    "moderation.ban": "can_restrict_members",
    "admins.manage": "can_promote_members",
    "config.manage": "can_manage_chat",
    "moderation.warn": None,
    "moderation.review": None,
    "commands.use": None,
    # Controlling the bot's own conversational layer is not a Telegram
    # capability at all. There is no right to turn on, and saying so here keeps
    # the promotion dialog honest: it marks this permission as application-only
    # rather than offering to tick a Telegram box that does not exist.
    "nexus.control": None,
    # Nor is asking a coding agent to edit a repository. It has no Telegram
    # counterpart, and offering to tick a box for it would suggest that
    # promoting somebody in a group could give them the ability to change the
    # code — which is exactly what the owner-only bundle above prevents.
    "agent.request": None,
    # Neither is reaching into the VPN project. These are application powers
    # over a different service, and no Telegram administrator flag in a chat
    # corresponds to any of them.
    "vpn.read": None,
    "vpn.manage": None,
    # Nor is approving an action the assistant proposed. It is a power over this
    # bot's own behaviour, not a capability inside a chat, and no Telegram
    # administrator flag corresponds to it.
    "admin.confirm": None,
}

# Rights the bot must itself hold before it can grant them to somebody else.
# Telegram enforces this too, and refuses with an error this module cannot see —
# but checking first is what turns "the API said no" into a sentence an operator
# can act on, and it is the difference between reporting a refusal and appearing
# to succeed.
BOT_RIGHT_FOR_GRANT = {
    "can_delete_messages": "can_delete_messages",
    "can_restrict_members": "can_restrict_members",
    "can_promote_members": "can_promote_members",
    "can_change_info": "can_change_info",
    "can_invite_users": "can_invite_users",
    "can_pin_messages": "can_pin_messages",
    "can_manage_chat": "can_manage_chat",
    "can_manage_video_chats": "can_manage_video_chats",
    "can_manage_topics": "can_manage_topics",
}


# ── Reasons ───────────────────────────────────────────────────────────────
# Machine keys, never sentences: the handler maps them to copy, so a new reason
# cannot leak an internal string into a group.
REASON_OK = "ok"
REASON_NO_OWNER = "no_owner"
REASON_NOT_ADMIN = "not_admin"
REASON_MISSING_PERMISSION = "missing_permission"
REASON_OWNER_PROTECTED = "owner_protected"
REASON_HIGHER_RANK = "higher_rank"
REASON_CANNOT_GRANT_ROLE = "cannot_grant_role"
REASON_CANNOT_GRANT_PERMISSION = "cannot_grant_permission"
REASON_UNKNOWN_ROLE = "unknown_role"
REASON_SELF_TARGET = "self_target"
REASON_BAD_TARGET = "bad_target"
# Not an authority failure. The actor may hold every permission the action
# needs; the *system* is in a state where the request cannot be honoured. It
# lives in this vocabulary anyway because it is a refusal with a reason, and a
# second place to keep reasons would be a second place for them to drift.
REASON_NEXUS_OFFLINE = "nexus_offline"


@dataclass(frozen=True)
class Principal:
    """One Telegram user, resolved to an application role and permission set."""

    user_id: int
    role: str
    permissions: frozenset[str]
    source: str  # "owner" | "config" | "database" | "none"

    @property
    def level(self) -> int:
        return ROLE_LEVELS.get(self.role, 0)

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def is_admin(self) -> bool:
        return bool(self.permissions)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


@dataclass(frozen=True)
class Decision:
    """The answer, and why. Truthy when allowed, so callers can `if decision:`."""

    allowed: bool
    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def owner_id() -> int:
    """The configured owner, or 0. Read from config every time, never cached."""
    return int(config.OWNER_USER_ID or 0)


def has_owner() -> bool:
    return owner_id() != 0


def guest(user_id: int = 0) -> Principal:
    return Principal(
        user_id=int(user_id), role=ROLE_GUEST, permissions=frozenset(), source="none"
    )


def _config_admins() -> dict[int, str]:
    """``CONFIG_ADMINS`` parsed into ``{user_id: role}``.

    Malformed entries are skipped rather than raising: a typo in a deployment
    variable must not stop the bot booting, and it must not silently become a
    grant either. The startup log lists what was accepted.
    """
    parsed: dict[int, str] = {}
    for entry in config.CONFIG_ADMINS:
        user_id, _, role = entry.partition(":")
        user_id, role = user_id.strip(), role.strip()
        if not user_id.lstrip("-").isdigit():
            continue
        if role not in ROLE_PERMISSIONS:
            continue
        if int(user_id) == owner_id():
            # The owner is not a config admin; that role is a single fact.
            continue
        parsed[int(user_id)] = role
    return parsed


def configured_admin_count() -> int:
    """How many administrators come from the environment. For the startup log."""
    return len(_config_admins())


def resolve(user_id: int) -> Principal:
    """Resolve a Telegram user id to its application principal.

    Order matters and is the whole security story:

    1. **The owner**, by id, from configuration. Nothing can shadow it.
    2. **A configured admin**, from the environment.
    3. **A stored admin**, from the ``admins`` table.
    4. Otherwise a guest with no permissions.

    A stored row can never produce the owner role, and ``resolve`` never returns
    a principal whose permissions came from a user-controlled string: both the
    role and every permission are filtered against the closed sets above, so a
    corrupted or hand-edited row grants nothing rather than everything.
    """
    user_id = int(user_id)
    # The three reads are ordered by cost and stopped at the first that answers,
    # which is the same shape the logic below has. The owner is a configuration
    # read and no query; a configured administrator is the same; only a stranger
    # or a stored administrator costs a database read, and this function runs on
    # every group message.
    if user_id and user_id == owner_id():
        return _principal(user_id, {}, None)
    configured = _config_admins()
    if user_id in configured:
        return _principal(user_id, configured, None)
    return _principal(user_id, configured, db.admin_get(user_id))


def _principal(user_id: int, configured: dict[int, str], row: dict | None) -> Principal:
    """Build one principal from already-read inputs.

    Split out of ``resolve`` so that the bulk form below cannot drift from it.
    The authority rules are stated once, here, and both readers go through them:
    the owner by id, then the environment, then the stored row, then a guest.
    """
    if user_id and user_id == owner_id():
        return Principal(
            user_id=user_id,
            role=ROLE_OWNER,
            permissions=PERMISSION_SET,
            source="owner",
        )

    if user_id in configured:
        role = configured[user_id]
        return Principal(
            user_id=user_id,
            role=role,
            permissions=ROLE_PERMISSIONS[role],
            source="config",
        )

    if row:
        role = row["role"] if row["role"] in ROLE_PERMISSIONS else ROLE_HELPER
        granted = frozenset(p for p in row["permissions"] if p in PERMISSION_SET)
        # A stored row may narrow its role's bundle but never widen it. Without
        # this, editing one text column in the database would be a way to hand
        # out `admins.manage`.
        permissions = granted & ROLE_PERMISSIONS[role]
        if not permissions:
            return guest(user_id)
        return Principal(
            user_id=user_id,
            role=role,
            permissions=permissions,
            source="database",
        )

    return guest(user_id)


def resolve_many(user_ids) -> dict[int, Principal]:
    """Resolve several ids in one pass. Same answers, one round of reads.

    Added for the room transcript, which labels every speaker in a window with
    the role they hold *now*. The point of doing that is that a promotion is
    visible to the very next awareness pass rather than to the next restart —
    and the point of *this* function is that saying so costs one query and one
    config parse instead of forty of each.

    It is a wrapper, not a second implementation: every id goes through
    ``_principal``, which is the same function ``resolve`` calls.
    """
    ids = {int(uid) for uid in user_ids if uid}
    if not ids:
        return {}
    configured = _config_admins()
    stored: dict[int, dict] = {}
    try:
        stored = {int(row["user_id"]): row for row in db.admin_list()}
    except Exception:  # noqa: BLE001 - a missing overlay is a guest, not a crash
        log.exception("could not read the stored administrators")
    return {
        uid: _principal(uid, configured, stored.get(uid))
        for uid in ids
    }


def is_owner(user_id: int) -> bool:
    """Whether this id is the configured owner. The one protected identity."""
    return bool(owner_id()) and int(user_id) == owner_id()


def is_protected(user_id: int) -> bool:
    """Identities no administrative action may target.

    Today that is the owner alone. It is a function rather than a comparison so
    that adding a second protected identity is a change in one place, and so
    that every caller asks the same question.
    """
    return is_owner(user_id)


def grantable_roles(actor: Principal) -> tuple[str, ...]:
    """The roles ``actor`` may assign. Empty for anyone who cannot assign any."""
    if not actor.can("admins.manage"):
        return ()
    return GRANTABLE_ROLES.get(actor.role, ())


def grantable_permissions(actor: Principal) -> frozenset[str]:
    """The permissions ``actor`` may hand out.

    The owner may hand out everything. Everyone else may hand out what they
    themselves hold, minus ``admins.manage``: an administrator who could create
    other administrators could build a peer group, and the hierarchy stops being
    a hierarchy. That single exclusion is why this is not simply
    ``actor.permissions``.
    """
    if actor.is_owner:
        return PERMISSION_SET
    if not actor.can("admins.manage"):
        return frozenset()
    return frozenset(p for p in actor.permissions if p != "admins.manage")


def authorize(
    actor: Principal,
    permission: str,
    *,
    target: Principal | None = None,
    role_change: bool = False,
) -> Decision:
    """Whether ``actor`` may exercise ``permission``, optionally on ``target``.

    Checks, in order, and the order is deliberate — the cheapest and most
    fundamental first, so a refusal is always for the most basic reason that
    applies:

    1. Is an owner configured at all? If not, nothing is authorised.
    2. Does the actor hold the permission?
    3. Is the target protected (the owner)?
    4. Is this a role change aimed at the actor themselves? Refused outright.
    5. Is the target at or above the actor's own level?
    6. Is the actor acting on themselves? Also refused — but by step 5, not by a
       rule of its own.

    Step 4 is the one that exists for its own sake rather than for convenience.
    A self-targeted *role change* was previously refused only as a side effect of
    step 5 — the actor's own level equals the target's, so the hierarchy check
    caught it and reported ``higher_rank``. That is a refusal for the wrong
    reason: it is an accident of the level arithmetic, it names the wrong rule,
    and it would stop protecting the moment those numbers were rearranged. The
    requirement is absolute — nobody promotes or demotes themselves, whatever
    the levels say — so it is now checked as itself, and it reports
    ``REASON_SELF_TARGET``.

    Step 4 sits *after* step 3 on purpose: the owner targeting themselves must
    still answer ``owner_protected``, because "the owner is never a target" is
    the more fundamental rule and the more informative answer.

    Step 6 is stated as a consequence rather than as a branch, because that is
    what it is: an earlier version of this docstring claimed self-targeting was
    *allowed* ("I can mute myself"), and it never was. An actor's own level
    always equals their own level, so step 5 refuses it for every non-owner, and
    step 3 refuses it for the owner. The refusal is kept — self-restriction is
    not a thing anybody needs to do through this path, and allowing it would
    mean carving an exception into the hierarchy rule.

    Everything else is refused by default: an unknown permission, an unknown
    actor and an unhandled case all fall out at step 2.
    """
    if not has_owner():
        return Decision(False, REASON_NO_OWNER)

    if not actor.is_admin:
        return Decision(False, REASON_NOT_ADMIN)

    if permission not in PERMISSION_SET:
        return Decision(False, REASON_MISSING_PERMISSION, "unknown permission")

    if not actor.can(permission):
        return Decision(False, REASON_MISSING_PERMISSION, permission)

    if target is None:
        return Decision(True, REASON_OK)

    if is_protected(target.user_id):
        # The owner is never the target of an administrative action — not by a
        # subordinate, and not by the owner either. Making that unconditional is
        # what removes the whole class of "ban the owner" bugs rather than one
        # instance of it.
        return Decision(False, REASON_OWNER_PROTECTED)

    if role_change and target.user_id == actor.user_id:
        # Never, for anybody, including the owner. A model that could rewrite its
        # own authority would make every other check in this module advisory.
        return Decision(False, REASON_SELF_TARGET)

    if not actor.is_owner and target.level >= actor.level:
        # A senior admin cannot touch another senior admin, and nobody below the
        # owner can touch the owner (already refused above). Equal level is
        # refused too: peers cannot demote each other.
        return Decision(False, REASON_HIGHER_RANK)

    return Decision(True, REASON_OK)


def authorize_grant(
    actor: Principal, role: str, permissions, *, target: Principal | None = None
) -> Decision:
    """Whether ``actor`` may give ``target`` exactly ``role`` + ``permissions``.

    This is the promotion path, and it has three separate bounds because each
    one closes a different door:

    * the role must be one this actor may assign at all (``GRANTABLE_ROLES``);
    * every permission must be one this actor may hand out
      (``grantable_permissions``), which is what stops a senior admin minting
      another senior admin by ticking ``admins.manage``;
    * the target must pass the ordinary hierarchy check, so a lower admin cannot
      rewrite a higher one's permissions.

    Note that the permission set is validated against the *role's* bundle as
    well: a caller cannot ask for ``moderator`` plus ``admins.manage``.

    ``role_change=True`` is what makes the self-target refusal explicit rather
    than incidental — see :func:`authorize`, step 4. Both callers of this
    function (``promote_member`` and ``demote_member``) are role changes, so
    there is no path here that should ever be allowed to aim at the actor.
    """
    base = authorize(actor, "admins.manage", target=target, role_change=True)
    if not base:
        return base

    if role not in ROLE_PERMISSIONS:
        return Decision(False, REASON_UNKNOWN_ROLE, str(role))

    if role not in grantable_roles(actor):
        return Decision(False, REASON_CANNOT_GRANT_ROLE, str(role))

    wanted = frozenset(p for p in (permissions or ()) if p)
    unknown = wanted - PERMISSION_SET
    if unknown:
        return Decision(False, REASON_CANNOT_GRANT_PERMISSION, ",".join(sorted(unknown)))

    # A role's bundle is the ceiling for that role. Asking for something the
    # role does not carry is refused rather than quietly dropped, because
    # silently narrowing a request is how an operator comes to believe they
    # granted something they did not.
    beyond_role = wanted - ROLE_PERMISSIONS[role]
    if beyond_role:
        return Decision(
            False, REASON_CANNOT_GRANT_PERMISSION, ",".join(sorted(beyond_role))
        )

    beyond_actor = wanted - grantable_permissions(actor)
    if beyond_actor:
        return Decision(
            False, REASON_CANNOT_GRANT_PERMISSION, ",".join(sorted(beyond_actor))
        )

    return Decision(True, REASON_OK)


def telegram_rights_for(permissions) -> dict[str, bool]:
    """The ``promoteChatMember`` flags an application permission set implies.

    Only rights that a permission actually maps to are included, and they are
    only ever set to True. There is deliberately no way to express "grant this
    Telegram right but not that permission": the application layer decides what
    the administrator may request, and Telegram's flags are derived from it, so
    the two cannot drift apart into a combination nobody reviewed.
    """
    rights: dict[str, bool] = {}
    for permission in permissions or ():
        right = PERMISSION_TELEGRAM_RIGHT.get(permission)
        if right:
            rights[right] = True
    return rights


def permission_labels(permissions) -> list[str]:
    """Persian labels for a permission set, in vocabulary order."""
    chosen = set(permissions or ())
    return [PERMISSION_LABELS[p] for p in PERMISSIONS if p in chosen and p in PERMISSION_LABELS]


def describe(principal: Principal) -> dict:
    """A safe summary for the log: ids and names, never anything sensitive."""
    return {
        "user_id": principal.user_id,
        "role": principal.role,
        "source": principal.source,
        "permissions": sorted(principal.permissions),
    }


def reset_state() -> None:
    """Kept for symmetry with the other modules' test hooks.

    This module holds no mutable state of its own — the owner comes from config
    and everything else from the database — so there is nothing to clear. The
    function exists so a test that resets every subsystem does not have to know
    which of them are stateless.
    """
    return None
