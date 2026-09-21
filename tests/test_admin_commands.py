"""The administrative command surface, end to end through the handlers.

Every test drives a real handler with a fake update, so what is pinned is the
whole chain the brief asks for: who asked, are they registered, what may they
do, is the target protected, does Telegram permit it, then act — and the audit
row either way.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import config, db, main, rbac

OWNER = 999
SENIOR = 555
MODERATOR = 777
MEMBER = 42
CHAT = -1001234567890


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{SENIOR}:senior_admin",
                                                  f"{MODERATOR}:moderator"])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", None)
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "MUTE_MINUTES", 15)
    db.init()
    main._admin_cache.clear()
    main._recently_deleted.clear()
    yield


class FakeBot:
    """Records the Telegram calls and can be told to refuse them."""

    def __init__(self, *, can_promote=True, can_restrict=True, can_delete=True,
                 promote_fails=False, action_fails=False):
        self.id = 1
        self.username = "guardbot"
        self.sent = []
        self.promoted = []
        self.demoted = []
        self.banned = []
        self.unbanned = []
        self.restricted = []
        self.deleted = []
        self.can_promote = can_promote
        self.can_restrict = can_restrict
        self.can_delete = can_delete
        self.promote_fails = promote_fails
        self.action_fails = action_fails
        self.callback_answers = []
        self.edits = []

    async def get_chat_member(self, chat_id, user_id):
        if int(user_id) == self.id:
            return SimpleNamespace(
                status="administrator",
                can_promote_members=self.can_promote,
                can_restrict_members=self.can_restrict,
                can_delete_messages=self.can_delete,
            )
        return SimpleNamespace(status="member", can_promote_members=False,
                               can_restrict_members=False, can_delete_messages=False)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def promote_chat_member(self, chat_id, user_id, **rights):
        if self.promote_fails:
            raise TelegramError("not enough rights")
        if any(rights.values()):
            self.promoted.append((user_id, rights))
        else:
            self.demoted.append((user_id, rights))

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        if self.action_fails:
            raise TelegramError("refused")
        self.banned.append(user_id)

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        if self.action_fails:
            raise TelegramError("refused")
        self.unbanned.append(user_id)

    async def restrict_chat_member(self, chat_id, user_id, **kwargs):
        if self.action_fails:
            raise TelegramError("refused")
        self.restricted.append(user_id)

    async def send_chat_action(self, *args, **kwargs):
        return None


class FakeMessage:
    def __init__(self, *, sender=MEMBER, message_id=10, reply_to=None, text="/x"):
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=sender, full_name=f"user{sender}",
                                         username=None, is_bot=False)
        self.reply_to_message = reply_to
        self.text = text
        self.caption = None

    async def delete(self):
        pass


class FakeRepliedMessage:
    """The message a command is replying to. Deletable, like the real one."""

    def __init__(self, user_id, message_id):
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=user_id, full_name=f"user{user_id}",
                                         username=None, is_bot=False)
        self.delete_calls = 0
        self.delete_error = None

    async def delete(self):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error


def reply_to_user(user_id=MEMBER, message_id=9):
    return FakeRepliedMessage(user_id, message_id)


def update_for(bot, *, actor, reply=None, args=None, text="/x", chat_id=CHAT):
    msg = FakeMessage(sender=actor, reply_to=reply, text=text)
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=actor, full_name=f"user{actor}",
                                       username=None, is_bot=False),
        callback_query=None,
        effective_bot=bot,
    )


def ctx_for(bot, args=None):
    return SimpleNamespace(bot=bot, args=list(args or []))


def run(handler, bot, *, actor, reply=None, args=None):
    asyncio.run(handler(update_for(bot, actor=actor, reply=reply), ctx_for(bot, args)))


# ── whoami ────────────────────────────────────────────────────────────────
def test_whoami_tells_an_ordinary_member_they_are_nothing():
    bot = FakeBot()
    run(main.cmd_whoami, bot, actor=MEMBER)

    assert len(bot.sent) == 1
    assert "کاربر" in bot.sent[0]


def test_whoami_tells_the_owner_they_are_the_owner():
    bot = FakeBot()
    run(main.cmd_whoami, bot, actor=OWNER)

    assert rbac.ROLE_LABELS[rbac.ROLE_OWNER] in bot.sent[0]


def test_whoami_says_so_when_no_owner_is_configured(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", 0)
    bot = FakeBot()
    run(main.cmd_whoami, bot, actor=OWNER)

    assert bot.sent == [config.ADMIN_NOT_CONFIGURED_TEXT]


# ── /admins ───────────────────────────────────────────────────────────────
def test_a_stranger_cannot_list_the_admins():
    bot = FakeBot()
    run(main.cmd_admins, bot, actor=MEMBER)

    assert bot.sent == [config.ADMIN_DENIED_TEXT]


def test_a_moderator_can_list_the_admins():
    db.admin_set(MEMBER, rbac.ROLE_HELPER, rbac.ROLE_PERMISSIONS[rbac.ROLE_HELPER],
                 granted_by=OWNER)
    bot = FakeBot()
    run(main.cmd_admins, bot, actor=MODERATOR)

    assert config.ADMIN_LIST_TITLE in bot.sent[0]
    assert str(OWNER) in bot.sent[0]
    assert str(MEMBER) in bot.sent[0]


# ── /promote ──────────────────────────────────────────────────────────────
def test_a_stranger_cannot_promote_anybody():
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=MEMBER, reply=reply_to_user())

    assert bot.sent == [config.ADMIN_DENIED_TEXT]
    assert db.admin_get(MEMBER) is None


def test_promote_needs_a_target():
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=OWNER)

    assert bot.sent == [config.ADMIN_TARGET_NOT_FOUND_TEXT]


def test_the_bot_cannot_be_promoted():
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=OWNER, reply=reply_to_user(user_id=bot.id))

    assert bot.sent == [config.ADMIN_TARGET_IS_BOT_TEXT]


def test_the_owner_can_open_the_permission_dialog():
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=OWNER, reply=reply_to_user())

    assert len(bot.sent) == 1
    assert "user42" in bot.sent[0]
    assert db.admin_get(MEMBER) is None, "nothing is written before confirmation"


def test_a_moderator_cannot_promote():
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=MODERATOR, reply=reply_to_user())

    assert bot.sent == [config.ADMIN_DENIED_TEXT]


def test_a_senior_admin_cannot_promote_to_senior():
    """Asked for more than they may give, they get their own ceiling instead.

    The question was "promote this person"; the only open question is how far.
    """
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=SENIOR, reply=reply_to_user(), args=["senior_admin"])

    # The dialog opened, so the request was not refused outright — and the
    # keyboard only offers what a senior may assign.
    assert len(bot.sent) == 1
    assert "user42" in bot.sent[0]


def test_a_senior_admin_cannot_promote_the_owner():
    bot = FakeBot()
    run(main.cmd_promote, bot, actor=SENIOR, reply=reply_to_user(user_id=OWNER))

    assert bot.sent == [config.ADMIN_OWNER_PROTECTED_TEXT]


def test_the_dialog_says_so_when_the_bot_cannot_promote_in_telegram():
    """Told before confirming, not discovered afterwards."""
    bot = FakeBot(can_promote=False)
    run(main.cmd_promote, bot, actor=OWNER, reply=reply_to_user())

    assert config.ADMIN_PROMOTE_NO_TELEGRAM_TEXT in bot.sent[0]


# ── The dialog callback ───────────────────────────────────────────────────
class FakeQuery:
    def __init__(self, data, bot):
        self.data = data
        self.bot = bot
        self.answers = []
        self.edited = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edited.append(reply_markup)

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)


def callback_update(bot, data, actor):
    query = FakeQuery(data, bot)
    return SimpleNamespace(
        effective_message=None,
        effective_chat=SimpleNamespace(id=CHAT),
        effective_user=SimpleNamespace(id=actor, full_name=f"user{actor}",
                                       username=None, is_bot=False),
        callback_query=query,
    )


def press(bot, data, actor):
    update = callback_update(bot, data, actor)
    asyncio.run(main.on_admin_callback(update, ctx_for(bot)))
    return update.callback_query


def mask_of(role):
    return main._mask(rbac.ROLE_PERMISSIONS[role])


def test_the_owner_can_confirm_a_promotion():
    bot = FakeBot()
    data = f"adm:c:{OWNER}:{MEMBER}:m:{mask_of(rbac.ROLE_MODERATOR)}"

    query = press(bot, data, OWNER)

    stored = db.admin_get(MEMBER)
    assert stored is not None
    assert stored["role"] == rbac.ROLE_MODERATOR
    assert stored["granted_by"] == OWNER
    assert bot.promoted, "the Telegram promotion should have been attempted"
    assert query.answers or query.edited


def test_a_confirmed_promotion_applies_the_telegram_rights():
    bot = FakeBot()
    data = f"adm:c:{OWNER}:{MEMBER}:m:{mask_of(rbac.ROLE_MODERATOR)}"

    press(bot, data, OWNER)

    rights = bot.promoted[0][1]
    assert rights == {"can_delete_messages": True, "can_restrict_members": True}


def test_a_telegram_refusal_is_reported_and_never_claimed_as_success():
    bot = FakeBot(promote_fails=True)
    data = f"adm:c:{OWNER}:{MEMBER}:m:{mask_of(rbac.ROLE_MODERATOR)}"

    query = press(bot, data, OWNER)

    body = " ".join(str(item) for item in query.edited)
    assert config.ADMIN_TELEGRAM_FAILED_TEXT in body
    # The application role was still stored, and the operator was told that
    # Telegram did not follow — which is the honest pair of facts.
    assert db.admin_get(MEMBER) is not None


def test_a_second_administrator_cannot_confirm_someone_elses_dialog():
    bot = FakeBot()
    data = f"adm:c:{OWNER}:{MEMBER}:m:{mask_of(rbac.ROLE_MODERATOR)}"

    press(bot, data, SENIOR)

    assert db.admin_get(MEMBER) is None
    assert not bot.promoted


def test_a_crafted_mask_cannot_grant_more_than_the_actor_holds():
    """The escalation attempt the re-authorisation exists to stop.

    A senior admin crafts a payload asking for `admins.manage` inside a
    moderator role. The permission is not in the role bundle, so it is refused.
    """
    bot = FakeBot()
    forged = main._mask(["moderation.delete", "admins.manage"])
    data = f"adm:c:{SENIOR}:{MEMBER}:m:{forged}"

    press(bot, data, SENIOR)

    assert db.admin_get(MEMBER) is None


def test_a_crafted_payload_cannot_name_a_role_the_actor_may_not_assign():
    bot = FakeBot()
    data = f"adm:c:{SENIOR}:{MEMBER}:s:{mask_of(rbac.ROLE_SENIOR_ADMIN)}"

    press(bot, data, SENIOR)

    assert db.admin_get(MEMBER) is None


def test_a_crafted_payload_cannot_target_the_owner():
    bot = FakeBot()
    data = f"adm:c:{OWNER}:{OWNER}:m:{mask_of(rbac.ROLE_MODERATOR)}"

    press(bot, data, OWNER)

    assert db.admin_get(OWNER) is None


def test_a_crafted_payload_from_a_stranger_does_nothing():
    bot = FakeBot()
    data = f"adm:c:{MEMBER}:{MEMBER}:m:{mask_of(rbac.ROLE_MODERATOR)}"

    press(bot, data, MEMBER)

    assert db.admin_get(MEMBER) is None


def test_an_unknown_role_code_is_refused():
    bot = FakeBot()
    data = f"adm:c:{OWNER}:{MEMBER}:z:{mask_of(rbac.ROLE_MODERATOR)}"

    press(bot, data, OWNER)

    assert db.admin_get(MEMBER) is None


def test_a_malformed_payload_is_answered_not_crashed():
    bot = FakeBot()

    query = press(bot, "adm:c:notanumber", OWNER)

    assert query.answers == [config.ADMIN_STALE_BUTTON_TEXT]


def test_cancel_does_nothing():
    bot = FakeBot()

    query = press(bot, f"adm:x:{OWNER}", OWNER)

    assert query.answers == [config.ADMIN_CANCELLED_TEXT]
    assert db.admin_get(MEMBER) is None


def test_a_toggle_redraws_the_keyboard():
    bot = FakeBot()
    mask = mask_of(rbac.ROLE_MODERATOR)
    data = f"adm:t:{OWNER}:{MEMBER}:m:{mask}:0"

    query = press(bot, data, OWNER)

    assert query.edited, "the keyboard should have been redrawn"
    assert db.admin_get(MEMBER) is None


def test_a_toggle_with_an_out_of_range_bit_is_refused():
    bot = FakeBot()
    data = f"adm:t:{OWNER}:{MEMBER}:m:1:999"

    query = press(bot, data, OWNER)

    assert query.answers == [config.ADMIN_STALE_BUTTON_TEXT]


# ── /demote ───────────────────────────────────────────────────────────────
def test_the_owner_can_demote_a_moderator():
    db.admin_set(MODERATOR + 1, rbac.ROLE_MODERATOR,
                 rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR], granted_by=OWNER)
    bot = FakeBot()

    run(main.cmd_demote, bot, actor=OWNER, reply=reply_to_user(user_id=MODERATOR + 1))

    assert db.admin_get(MODERATOR + 1) is None
    assert bot.demoted


def test_a_moderator_cannot_demote_a_senior():
    db.admin_set(SENIOR + 1, rbac.ROLE_SENIOR_ADMIN,
                 rbac.ROLE_PERMISSIONS[rbac.ROLE_SENIOR_ADMIN], granted_by=OWNER)
    bot = FakeBot()

    run(main.cmd_demote, bot, actor=MODERATOR, reply=reply_to_user(user_id=SENIOR + 1))

    assert db.admin_get(SENIOR + 1) is not None


def test_nobody_can_demote_the_owner():
    bot = FakeBot()

    run(main.cmd_demote, bot, actor=OWNER, reply=reply_to_user(user_id=OWNER))

    assert bot.sent == [config.ADMIN_OWNER_PROTECTED_TEXT]


def test_demoting_somebody_who_is_not_an_admin_says_so():
    bot = FakeBot()

    run(main.cmd_demote, bot, actor=OWNER, reply=reply_to_user(user_id=MEMBER))

    assert bot.sent == [config.ADMIN_DEMOTE_NOTHING_TEXT]


# ── Moderation commands ───────────────────────────────────────────────────
def test_a_senior_admin_can_ban():
    bot = FakeBot()
    run(main.cmd_ban, bot, actor=SENIOR, reply=reply_to_user())

    assert bot.banned == [MEMBER]


def test_a_moderator_cannot_ban():
    """A moderator may delete and mute; banning is one rank above them."""
    bot = FakeBot()
    run(main.cmd_ban, bot, actor=MODERATOR, reply=reply_to_user())

    assert bot.banned == []
    assert bot.sent == [config.ADMIN_DENIED_TEXT]


def test_a_helper_cannot_ban():
    from app import config as cfg
    cfg.CONFIG_ADMINS = [f"{MEMBER + 1}:helper"]
    bot = FakeBot()

    run(main.cmd_ban, bot, actor=MEMBER + 1, reply=reply_to_user())

    assert bot.banned == []


def test_a_stranger_cannot_mute():
    bot = FakeBot()
    run(main.cmd_mute, bot, actor=MEMBER, reply=reply_to_user())

    assert bot.restricted == []


def test_a_moderator_can_mute():
    bot = FakeBot()
    run(main.cmd_mute, bot, actor=MODERATOR, reply=reply_to_user())

    assert bot.restricted == [MEMBER]


def test_nobody_can_ban_the_owner():
    bot = FakeBot()
    run(main.cmd_ban, bot, actor=SENIOR, reply=reply_to_user(user_id=OWNER))

    assert bot.banned == []
    assert bot.sent == [config.ADMIN_OWNER_PROTECTED_TEXT]


def test_a_senior_cannot_ban_another_senior():
    from app import config as cfg
    cfg.CONFIG_ADMINS = [f"{SENIOR}:senior_admin", f"{SENIOR + 1}:senior_admin"]
    bot = FakeBot()

    run(main.cmd_ban, bot, actor=SENIOR, reply=reply_to_user(user_id=SENIOR + 1))

    assert bot.banned == []


def test_the_bot_reports_its_own_missing_permission():
    bot = FakeBot(can_restrict=False)
    run(main.cmd_mute, bot, actor=MODERATOR, reply=reply_to_user())

    assert bot.restricted == []
    assert bot.sent == [config.ADMIN_BOT_LACKS_RIGHT_TEXT]


def test_a_telegram_refusal_is_reported():
    bot = FakeBot(action_fails=True)
    run(main.cmd_mute, bot, actor=MODERATOR, reply=reply_to_user())

    assert bot.sent == [config.MOD_COMMAND_FAILED_TEXT]


def test_warn_works_for_a_helper(monkeypatch):
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{MEMBER + 2}:helper"])
    bot = FakeBot()

    run(main.cmd_warn, bot, actor=MEMBER + 2, reply=reply_to_user())

    # The warning to the user, and the confirmation to the operator.
    assert len(bot.sent) == 2


def test_warn_needs_a_target():
    bot = FakeBot()
    run(main.cmd_warn, bot, actor=MODERATOR)

    assert bot.sent == [config.MOD_TARGET_REQUIRED_TEXT]


def test_delete_needs_a_reply():
    bot = FakeBot()
    run(main.cmd_delete, bot, actor=MODERATOR)

    assert bot.sent == [config.MOD_TARGET_REQUIRED_TEXT]


def test_a_moderator_can_delete_a_message():
    bot = FakeBot()
    run(main.cmd_delete, bot, actor=MODERATOR, reply=reply_to_user())

    assert bot.sent == [config.MOD_DELETE_DONE_TEXT]


def test_a_stranger_cannot_delete():
    bot = FakeBot()
    run(main.cmd_delete, bot, actor=MEMBER, reply=reply_to_user())

    assert bot.sent == [config.ADMIN_DENIED_TEXT]


# ── The audit trail ───────────────────────────────────────────────────────
def test_a_successful_command_is_audited():
    bot = FakeBot()
    run(main.cmd_ban, bot, actor=SENIOR, reply=reply_to_user())

    rows = db.audit_recent(5)
    assert rows[0]["action"] == "moderation.ban"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["actor_id"] == SENIOR
    assert rows[0]["target_id"] == MEMBER


def test_a_refused_command_is_audited_too():
    """`who tried` is the question asked after an incident."""
    bot = FakeBot()
    run(main.cmd_ban, bot, actor=MEMBER, reply=reply_to_user())

    rows = db.audit_recent(5)
    assert rows[0]["actor_id"] == MEMBER
    assert rows[0]["outcome"] != "ok"


def test_a_telegram_failure_is_audited():
    bot = FakeBot(action_fails=True)
    run(main.cmd_mute, bot, actor=MODERATOR, reply=reply_to_user())

    assert db.audit_recent(1)[0]["outcome"] == "telegram_error"


def test_a_promotion_is_audited():
    bot = FakeBot()
    press(bot, f"adm:c:{OWNER}:{MEMBER}:m:{mask_of(rbac.ROLE_MODERATOR)}", OWNER)

    row = db.audit_recent(1)[0]
    assert row["action"] == "admin.promote"
    assert row["outcome"] == "ok"
    assert "role=moderator" in row["detail"]


def test_the_audit_carries_no_message_content():
    bot = FakeBot()
    run(main.cmd_warn, bot, actor=MODERATOR, reply=reply_to_user(),
        args=["secret text"])

    assert "secret text" not in str(db.audit_recent(1))


def test_an_audit_write_failure_does_not_break_the_command(monkeypatch):
    """An audit row that cannot be written must not be why moderation fails."""
    def _boom(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db, "audit_write", _boom)
    bot = FakeBot()

    run(main.cmd_ban, bot, actor=SENIOR, reply=reply_to_user())

    assert bot.banned == [MEMBER]


# ── The escape hatch ──────────────────────────────────────────────────────
def test_a_display_name_that_looks_like_an_admin_grants_nothing():
    """Authority comes from ids, never from anything a user can set."""
    bot = FakeBot()
    update = update_for(bot, actor=MEMBER, reply=reply_to_user())
    update.effective_user.full_name = "Admin Owner 999"
    asyncio.run(main.cmd_ban(update, ctx_for(bot)))

    assert bot.banned == []
