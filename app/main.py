import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import (
    admin_service,
    admin_tools,
    agent_bridge,
    agent_poller,
    agent_service,
    agent_spool,
    ai_intent,
    ai_moderation,
    answer_shape,
    awareness,
    awareness_context,
    awareness_schedule,
    burst,
    chat,
    chat_queue,
    classifier,
    config,
    context_plan,
    db,
    decision,
    gemini_keys,
    gemini_pool,
    groups,
    key_store,
    media,
    memory,
    mod_policy,
    moderation,
    net,
    nexus,
    people,
    persian_calendar,
    rbac,
    reply_target,
    responses,
    state,
    subject,
    text_filters,
    transcribe,
    voice_context,
    vpnbot,
    web_search,
)
# Nexus Voice Live: the same assistant, reached through a voice chat. Imported
# as a package because the router needs three things from it and they belong to
# different layers — the phrase vocabulary, the session manager that is the one
# gate on whether a call may start, and the transport that carries it.
from .voice_live import commands as voice_commands
from .voice_live import errors as voice_errors
from .voice_live import session as voice_session
from .voice_live import telegram_voice

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("guardbot")

_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
# Instant-flood tracker. Bounded internally; see app/burst.py.
_bursts = burst.BurstTracker(
    window_seconds=config.BURST_WINDOW_SECONDS,
    max_items=config.BURST_MAX_ITEMS,
)

# The two permission objects a restriction is expressed with.
#
# ``MUTED`` names one field and relies on a Bot API rule: an unspecified field in
# ``ChatPermissions`` means *false*, so a mute that mentions only
# ``can_send_messages`` is a total mute. That is the intent, and it is written
# down here because the rule is invisible in the code — filling in the other
# fields "for completeness" would turn every mute into a mute that still allows
# media.
MUTED = ChatPermissions(can_send_messages=False)
#
# ``FULL`` is the opposite operation and must name *every* field, because the same
# rule cuts the other way: an omitted field is set to false, so an "unmute" built
# from a partial list writes a restriction that keeps those permissions denied.
# This was a live bug. ``FULL`` listed the ten sending permissions and omitted
# ``can_change_info``, ``can_invite_users``, ``can_pin_messages`` and
# ``can_manage_topics``; Telegram then reported every unmuted member as
# ``restricted`` for good, because ``restrict_chat_member`` without ``until_date``
# makes the record permanent. The Bot API documentation for that method is
# explicit — "Pass True for all permissions to lift restrictions from a user" —
# and ``ChatPermissions.all_permissions()`` is exactly that sentence, and cannot
# fall behind the API the way a hand-written list did.
FULL = ChatPermissions.all_permissions()

# ── The bot's own identity ────────────────────────────────────────────────
# Resolved from Telegram at startup with getMe, plus the operator's configured
# aliases. Kept in a dict rather than a module of constants because it is only
# known once the network answers, and because a failed getMe must leave the bot
# running with the identity it can still infer (the token's own username is not
# readable, but `ctx.bot.id`/`username` are populated by the Application).
_bot_identity: dict = {
    "id": 0,
    "username": "",
    "name": "",
    "aliases": (),
    "resolved": False,
    # Telegram's own answer to "may this bot read ordinary group messages
    # without being addressed", from ``getMe``. Recorded because it is a fact
    # about the deployment that changes what the assistant can honestly claim:
    # with privacy mode on and no administrator promotion, it simply does not
    # receive the messages the awareness layer is supposed to be reading, and an
    # assumption about that would be the worst kind of wrong.
    "reads_all_group_messages": False,
}

# Messages this process has just deleted, so the conversational handler does not
# answer a message that no longer exists. Bounded, and in-process only: a
# restart loses it, which is correct because the messages are gone either way.
# The value is a timestamp; entries older than a few minutes are dropped.
_recently_deleted: dict[tuple[int, int], float] = {}
_DELETED_TTL = 120.0


def bot_identity() -> dict:
    """A copy of what we know about ourselves. Never contains a secret."""
    return dict(_bot_identity)


# There is deliberately no pool-notification helper here.
#
# This module used to carry `notify_owner`, which delivered pool failover and
# health notices to ADMIN_LOG_CHAT with the owner's private chat as a fallback,
# and a `_pool_bot` handle so the pool could reach Telegram from inside a request
# no handler was running. Both are gone.
#
# The reason is not that the notices were broken — they worked, and they were
# the reason a group full of members was reading `GEMINI MODEL FAILOVER` and
# `rate_limited` while the pool quietly did its job. Operational detail about
# the AI provider belongs to the operator who asks for it, not to a chat that
# receives it because a retry happened. The pool still records every event; a
# human reads them from `/pool` or the events table.
#
# Nothing replaces this. There is no queue, no digest, no "only the important
# ones" filter — because the requirement is that no automatic pool message
# reaches Telegram at all, and a filter would be a promise about which messages
# matter rather than a guarantee that none are sent.


async def load_identity(app: Application) -> None:
    """Ask Telegram who we are. Never fatal.

    getMe is the authoritative source for the id and the username, and both are
    what reply-to-bot and @mention matching depend on — inferring them from
    configuration would be a second, possibly wrong answer to a question
    Telegram already answers.
    """
    try:
        me = await app.bot.get_me()
    except TelegramError as e:
        log.warning("getMe failed; alias matching will be limited: %s", e)
        return
    _bot_identity.update(
        id=int(me.id),
        username=(me.username or "").lower(),
        name=(me.first_name or "").strip(),
        aliases=tuple(a.strip().lower() for a in config.BOT_ALIASES if a.strip()),
        resolved=True,
        reads_all_group_messages=bool(
            getattr(me, "can_read_all_group_messages", False)
        ),
    )
    log.info(
        "Bot identity: id=%s username=%s name=%s aliases=%d reads_all=%s",
        _bot_identity["id"],
        f"@{_bot_identity['username']}" if _bot_identity["username"] else "-",
        _bot_identity["name"] or "-",
        len(_bot_identity["aliases"]),
        _bot_identity["reads_all_group_messages"],
    )


def mark_deleted(chat_id: int, message_id: int) -> None:
    """Remember that a message was just removed, so nothing tries to answer it."""
    now = time.monotonic()
    _recently_deleted[(int(chat_id), int(message_id))] = now
    if len(_recently_deleted) > 500:
        cutoff = now - _DELETED_TTL
        for key in [k for k, at in _recently_deleted.items() if at < cutoff]:
            _recently_deleted.pop(key, None)


def was_deleted(chat_id: int, message_id: int) -> bool:
    at = _recently_deleted.get((int(chat_id), int(message_id)))
    if at is None:
        return False
    if time.monotonic() - at > _DELETED_TTL:
        _recently_deleted.pop((int(chat_id), int(message_id)), None)
        return False
    return True


# ------------------------------------------------------------ helpers
async def is_admin(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if user_id in config.WHITELIST_USER_IDS:
        return True
    key = (chat_id, user_id)
    cached = _admin_cache.get(key)
    if cached and time.time() - cached[1] < 300:
        return cached[0]
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
        ok = m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        ok = False
    _admin_cache[key] = (ok, time.time())
    return ok


# Callback-data prefix for the admin-report self-delete button. Short, and
# matched exactly by the handler that reads it.
REPORT_DELETE_CALLBACK = "report_delete"


def _report_keyboard() -> InlineKeyboardMarkup:
    """The self-delete button attached to every admin report.

    The original moderated message has already been deleted by the time a
    report is sent, so this button only ever removes the report message itself.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 حذف گزارش", callback_data=REPORT_DELETE_CALLBACK)]]
    )


async def report(ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not config.ADMIN_LOG_CHAT:
        return
    try:
        await ctx.bot.send_message(
            config.ADMIN_LOG_CHAT,
            text,
            parse_mode="HTML",
            reply_markup=_report_keyboard(),
        )
    except TelegramError as e:
        log.warning("report failed: %s", e)


def mention(user) -> str:
    name = (user.full_name or str(user.id)).replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def on_any_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Claim every update, and stop the ones that have already been handled.

    This runs in the lowest handler group, so it sees an update before any
    handler does, and it is the only place in the bot that decides whether an
    update is new. ``ApplicationHandlerStop`` is what makes "stop" mean stop: the
    dispatcher runs groups in order, so raising it here means no handler in any
    later group sees the update at all.

    The claim is written *before* the handlers run rather than after. That is the
    deliberate half of the trade: an update that is being handled when the
    process dies is lost, and Telegram will not send it again either way — the
    offset moves when the update is fetched, not when it is answered — so writing
    the claim first costs nothing that was not already lost, while writing it
    last would leave a window in which a crash after handling produces a second
    reply on the next start.

    Nothing here is fatal. A database failure must not stop the bot from
    answering: if the claim cannot be written, the update is handled, which is
    the behaviour that existed before this guard.
    """
    if not config.UPDATE_DEDUP_ENABLED:
        return
    update_id = int(getattr(update, "update_id", 0) or 0)
    if update_id <= 0:
        # Not something Telegram does. Refusing to guess is the safe direction:
        # treating a missing id as claimable would collide every such update on
        # one row and start dropping real ones.
        return
    try:
        first_time = db.update_claim(update_id)
    except Exception:  # noqa: BLE001 - a dedup failure must not silence the bot
        log.exception("could not claim update_id=%s; handling it anyway", update_id)
        return
    if first_time:
        return
    log.info("duplicate update dropped update_id=%s", update_id)
    raise ApplicationHandlerStop


# ------------------------------------------------------ admin report button
# The admin-report group is a private trusted team group. ANY current member
# may delete a report, so Telegram administrator status is deliberately NOT
# required and ``is_admin`` is deliberately NOT used here.
_MEMBER_STATUSES = frozenset(
    {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.RESTRICTED,
    }
)


async def _is_chat_member(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Is ``user_id`` currently a member of ``chat_id``?

    Fails closed: if membership cannot be verified, the caller must not delete
    anything. This is a fresh check on every press - a user who left or was
    removed loses the ability immediately.
    """
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        log.warning("membership check failed chat=%s user=%s: %s", chat_id, user_id, e)
        return False
    return member.status in _MEMBER_STATUSES


async def on_report_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete an admin report when a member of the report group presses the button.

    The original moderated message is already gone; only the report message is
    removed. Anything that is not a member of the configured admin-report chat
    is refused, and a callback from any other chat can never delete anything.
    """
    q = update.callback_query
    chat = q.message.chat if q.message else None

    # 1. the callback must belong to the configured admin-report chat
    if not config.ADMIN_LOG_CHAT or chat is None or chat.id != config.ADMIN_LOG_CHAT:
        log.warning(
            "report delete refused chat=%s user=%s reason=wrong_chat",
            getattr(chat, "id", None), q.from_user.id,
        )
        await q.answer("⛔ شما عضو این گپ نیستید.", show_alert=True)
        return

    # 2. the clicking user must currently be a member of that chat
    if not await _is_chat_member(ctx, chat.id, q.from_user.id):
        log.warning(
            "report delete refused chat=%s user=%s reason=not_a_member",
            chat.id, q.from_user.id,
        )
        await q.answer("⛔ شما عضو این گپ نیستید.", show_alert=True)
        return

    # 3. delete the report message itself (never the moderated message)
    message_id = getattr(q.message, "message_id", "-")
    try:
        await q.message.delete()
    except TelegramError as e:
        # already deleted, or the bot lost the right: log and stay alive
        log.warning(
            "report delete failed chat=%s message=%s user=%s: %s",
            chat.id, message_id, q.from_user.id, e,
        )
        await q.answer("گزارش قبلاً حذف شده است.")
        return

    log.info(
        "REPORT_DELETED chat=%s message=%s user=%s", chat.id, message_id, q.from_user.id
    )
    await q.answer("🗑 گزارش حذف شد.")


async def _notify_review(ctx, chat, user, msg, verdict, outcome) -> None:
    """Tell the operator about something the policy declined to act on.

    REVIEW is where every uncertain case lands, so this is the channel that
    makes the policy observable: without it, "the bot stopped deleting" and "the
    bot stopped working" would look the same from the outside. It carries no
    message text — an identifier, the AI's verdict, and the reason.
    """
    if not config.MODERATION_REVIEW_NOTIFY or not config.ADMIN_LOG_CHAT:
        return
    ai_line = (
        f"🤖 هوش مصنوعی: <b>{verdict.classification}</b> ({verdict.confidence:.2f})"
        f"{' ⚠️ نامطمئن' if verdict.uncertain else ''}\n"
        if verdict is not None and verdict.decided
        else "🤖 هوش مصنوعی: پاسخی نداد\n"
    )
    text = (
        f"👀 <b>نیازمند بررسی دستی</b>\n\n"
        f"👤 {mention(user)} (<code>{user.id}</code>)\n"
        f"💬 Chat ID: <code>{chat.id}</code>\n"
        f"📩 Message ID: <code>{getattr(msg, 'message_id', '-')}</code>\n\n"
        f"{ai_line}"
        f"⚖️ سیاست: <code>{outcome.reason}</code>\n"
        f"✅ اقدام: هیچ‌چیز حذف نشد.\n"
    )
    try:
        await report(ctx, text)
    except TelegramError as e:
        log.warning("review notice failed: %s", e)


# ------------------------------------------------- flood / violations
def _burst_kind(msg) -> str | None:
    """Burst-rule kind of a message, independent of whether it is decodable.

    Ordinary photos are never counted, so sending several photos quickly is not
    a flood. The names returned here are the values used in
    ``BURST_MEDIA_KINDS``.
    """
    if msg.animation:
        return "gif"
    if msg.sticker:
        st = msg.sticker
        if getattr(st, "is_video", False):
            return "video_sticker"
        if getattr(st, "is_animated", False):
            return "animated_sticker"
        return "sticker"
    if msg.video_note:
        return "video_note"
    return None


def _format_notice(template: str, **kwargs) -> str | None:
    """Format a configurable notice. Never raises."""
    try:
        return template.format(**kwargs)
    except Exception:
        log.exception("notice template formatting failed")
        return None


async def _restrict_user(ctx, chat_id: int, user_id: int) -> bool:
    """Apply the configured timed restriction.

    Returns True only when Telegram accepted it. A refusal (for example the
    target is a chat administrator, or the bot lacks can_restrict_members) is
    logged and returns False - it is never reported as a success.
    """
    until = None
    if config.MUTE_MINUTES > 0:
        until = datetime.now(timezone.utc) + timedelta(minutes=config.MUTE_MINUTES)
    try:
        await ctx.bot.restrict_chat_member(
            chat_id, user_id, permissions=MUTED, until_date=until
        )
        return True
    except TelegramError as e:
        log.warning("restrict failed chat=%s user=%s: %s", chat_id, user_id, e)
        return False


async def _send_user_notice(ctx, chat_id: int, text: str | None) -> int | None:
    """Post a warning in the group. Fail-open: a rejected message changes nothing.

    Returns the sent message id (or None) so the caller can clean the warning up
    later - the test account uses this so it does not accumulate warnings.
    """
    if not text:
        return None
    try:
        msg = await ctx.bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramError as e:
        log.warning("user notice failed: %s", e)
        return None
    return getattr(msg, "message_id", None)


# ------------------------------------------------- test account unrestrict
# The test account is moderated exactly like everyone else; only the *cleanup*
# after a successful restriction differs. These two maps keep that bounded: at
# most one pending unrestrict job per (chat, user), and at most one list of
# warning message ids waiting to be removed.
_test_unrestrict_jobs: dict[tuple[int, int], object] = {}
_test_unrestrict_notices: dict[tuple[int, int], list[int]] = {}


def _schedule_test_unrestrict(
    ctx, chat_id: int, user_id: int, notice_id: int | None
) -> None:
    """Test account only: lift a successful restriction again after a delay.

    This is the *only* special behaviour for ``TEST_USER_ID``. Detection,
    deletion, the strike, the admin report and the real ``restrict_chat_member``
    call all happen normally first; the restriction is simply undone a moment
    later so the next test violation can be sent without a manual unrestrict.

    A no-op for every other user. At most one delayed job exists per
    (chat, user): a new restriction cycle cancels the pending one instead of
    stacking background tasks, and warnings from replaced cycles are still
    cleaned up.
    """
    if not config.TEST_USER_ID or user_id != config.TEST_USER_ID:
        return

    job_queue = getattr(ctx, "job_queue", None)
    if job_queue is None:  # pragma: no cover - the bot always builds one
        log.warning("TEST_UNRESTRICT_SKIPPED chat=%s user=%s reason=no_job_queue",
                    chat_id, user_id)
        return

    key = (chat_id, user_id)
    previous = _test_unrestrict_jobs.pop(key, None)
    if previous is not None:
        try:
            previous.schedule_removal()
        except Exception:  # pragma: no cover - depends on the job queue
            log.warning("could not cancel the pending test unrestrict for user=%s", user_id)
    if notice_id is not None:
        _test_unrestrict_notices.setdefault(key, []).append(notice_id)

    delay = config.TEST_USER_UNRESTRICT_SECONDS
    _test_unrestrict_jobs[key] = job_queue.run_once(
        _test_unrestrict_job,
        when=delay,
        data={"chat_id": chat_id, "user_id": user_id},
        name=f"test-unrestrict:{chat_id}:{user_id}",
    )
    log.info(
        "TEST_UNRESTRICT_SCHEDULED chat=%s user=%s in=%ss", chat_id, user_id, delay
    )


async def _apply_strike_ladder(
    ctx, chat_id: int, user, *, strike: int, source: str
) -> None:
    """Warn the person, and restrict them once the configured count is reached.

    One implementation, because there is one ladder. The media pipeline and the
    text pipeline used to carry a copy each, and two copies of a punishment rule
    is how a group ends up punishing the same behaviour two different ways
    depending on whether the violation arrived as a photo or as a sentence.

    The order matters and is deliberate: restrict first, then say so. The
    restriction is the fact and the notice is the explanation, and a notice that
    arrives before the restriction is a promise the bot might then fail to keep.

    ``strike`` is passed in rather than read here, so the caller records it —
    which is what keeps "a strike is only ever recorded for content that was
    actually removed" a property of the caller that did the deleting.

    A restriction failure is not an error: it is logged, the warning still goes
    out, and nothing else changes. ``_schedule_test_unrestrict`` is reached only
    when the restriction actually applied.
    """
    restricted = False
    if strike >= config.VIOLATION_MUTE_AFTER:
        restricted = await _restrict_user(ctx, chat_id, user.id)
        log.info(
            "VIOLATION_RESTRICT chat=%s user=%s count=%d minutes=%s applied=%s "
            "source=%s",
            chat_id, user.id, strike, config.MUTE_MINUTES, restricted, source,
        )

    notice_id = await _send_user_notice(
        ctx,
        chat_id,
        _format_notice(
            config.VIOLATION_WARNING_TEXT,
            name=mention(user),
            count=strike,
            max=config.VIOLATION_MUTE_AFTER,
        ),
    )
    # The test account is restricted for real above; only then is the unrestrict
    # scheduled, and only for that one user id.
    if restricted:
        _schedule_test_unrestrict(ctx, chat_id, user.id, notice_id)


async def _test_unrestrict_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Lift the test account's restriction and clean up its warning message.

    Never raises: a failure here must not take the bot down, must not affect any
    other user, and must not leave a broken job behind.
    """
    job = getattr(ctx, "job", None)
    data = getattr(job, "data", None) or {}
    chat_id, user_id = data.get("chat_id"), data.get("user_id")
    if chat_id is None or user_id is None:  # pragma: no cover - defensive
        return
    key = (chat_id, user_id)
    if job is not None and _test_unrestrict_jobs.get(key) is not job:
        # A newer restriction cycle replaced this job. It owns the unrestrict
        # and the cleanup, so a stale job must not duplicate either.
        log.info("TEST_UNRESTRICT_SUPERSEDED chat=%s user=%s", chat_id, user_id)
        return
    _test_unrestrict_jobs.pop(key, None)

    try:
        await ctx.bot.restrict_chat_member(chat_id, user_id, permissions=FULL)
        log.info("TEST_UNRESTRICT chat=%s user=%s", chat_id, user_id)
    except TelegramError as e:
        log.warning("TEST_UNRESTRICT_FAILED chat=%s user=%s: %s", chat_id, user_id, e)
    except Exception:
        log.exception("TEST_UNRESTRICT_FAILED chat=%s user=%s", chat_id, user_id)

    # The warning belonged to the restriction cycle that just ended. Cleaning it
    # up (even when the unrestrict above failed) is what stops the test account
    # from accumulating warnings; the failure is logged either way.
    for message_id in _test_unrestrict_notices.pop(key, []):
        try:
            await ctx.bot.delete_message(chat_id, message_id)
        except TelegramError as e:
            log.warning(
                "TEST_UNRESTRICT_NOTICE_KEPT chat=%s message=%s: %s",
                chat_id, message_id, e,
            )


async def _enforce_burst(ctx, chat, user, decision: burst.BurstDecision) -> None:
    """A confirmed instant flood: stop the user, remove the burst, warn.

    Only the messages recorded as belonging to this burst are touched - never
    other history from the same user. Every Telegram call fails open.
    """
    log.warning(
        "FLOOD chat=%s user=%s count=%d window=%ss messages=%s",
        chat.id, user.id, decision.count, config.BURST_WINDOW_SECONDS,
        decision.message_ids,
    )

    restricted = await _restrict_user(ctx, chat.id, user.id)
    log.info(
        "FLOOD_RESTRICT chat=%s user=%s minutes=%s applied=%s",
        chat.id, user.id, config.MUTE_MINUTES, restricted,
    )

    deleted = 0
    failed = 0
    for mid in decision.message_ids:
        try:
            await ctx.bot.delete_message(chat.id, mid)
            deleted += 1
        except TelegramError as e:
            failed += 1
            log.warning("FLOOD_DELETE_FAILED chat=%s message=%s error=%s", chat.id, mid, e)
    log.info(
        "FLOOD_CLEARED chat=%s user=%s deleted=%d failed=%d",
        chat.id, user.id, deleted, failed,
    )

    if restricted:
        notice_id = await _send_user_notice(
            ctx,
            chat.id,
            _format_notice(
                config.FLOOD_WARNING_TEXT,
                name=mention(user),
                minutes=config.MUTE_MINUTES,
            ),
        )
        _schedule_test_unrestrict(ctx, chat.id, user.id, notice_id)
    else:
        # The restriction was refused (for example an administrator). Say so
        # rather than pretending it worked.
        await report(
            ctx,
            "⚠️ <b>سیل مدیا</b>\n"
            f"👤 کاربر: {mention(user)}\n"
            f"🆔 <code>{user.id}</code>\n"
            f"📊 تعداد: <b>{decision.count}</b>\n"
            "❌ محدودسازی اعمال نشد (تلگرام اجازه نداد).",
        )


async def on_media_flood(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The instant media-flood rule, and nothing else.

    This handler used to be the whole media pipeline: download the file, run the
    local sexual-content detector, ask the moderation AI, delete and report. That
    pipeline was removed (see ``AgentMD.md``), so what is left is the one part of
    it that was never about content: a burst of the same kind of media in a few
    seconds is a flood, and the rule that stops it is decided from message
    metadata alone — no download, no model, no file on disk.

    Registered for the media kinds the burst rule counts, and independently of
    any content setting: the flood rule is abuse protection, so it keeps working
    whatever else is switched off. Ordinary photos are not in
    ``BURST_MEDIA_KINDS``, so several photos in a row are not a flood.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if not authorized_group(chat.id):
        return
    # Owner exemption only (WHITELIST_USER_IDS). Telegram admins are NOT exempt:
    # the anti-flood rule applies to them too. Telegram itself decides whether
    # the bot may act on an admin's message, and every failure is handled
    # fail-open below.
    if user.id in config.WHITELIST_USER_IDS:
        return

    bkind = _burst_kind(msg)
    if not config.BURST_ENABLED or bkind is None:
        return
    decision = _bursts.record(
        chat.id, user.id, getattr(msg, "message_id", 0), bkind,
        kinds=config.BURST_MEDIA_KINDS,
    )
    if not decision.is_burst:
        return
    try:
        await _enforce_burst(ctx, chat, user, decision)
    except Exception:
        # fail open: a flood-handling error never escalates
        log.exception("burst enforcement failed")


# ------------------------------------------------------------ acquisition
def acquisition_message_filter():
    """Ordinary group text, and nothing else.

    No captions (the media pipeline owns those), no commands, and no edited
    messages — the last is belt and braces, since edited updates are not
    requested from Telegram at all, so editing a message into an intent can
    never produce a second invitation.
    """
    return (
        filters.TEXT
        & ~filters.COMMAND
        & filters.ChatType.GROUPS
        & ~filters.UpdateType.EDITED_MESSAGE
    )


def _intent_reply_keyboard(deep_link: str | None) -> InlineKeyboardMarkup | None:
    """The invitation button, or nothing.

    Only ever a link *into the VPN bot*. The group never sees a configuration,
    a subscription link or a credential — those are delivered in a private chat
    by the bot that owns them.
    """
    if not deep_link:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(config.GROUP_TRIAL_BUTTON, url=deep_link)]]
    )


async def _reply_in_group(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    reply_to: int | None = None,
) -> None:
    """Reply in the group, falling back if the original message is gone.

    The user may have deleted the message between sending it and our reply, and
    a reply to a deleted message is an error — losing the invitation over that
    would be silly, so it is retried as a plain message.

    ``keyboard`` defaults to None so the administrative replies, which mostly
    carry no markup, do not have to pass one explicitly.
    """
    try:
        await ctx.bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )
    except TelegramError:
        try:
            await ctx.bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            log.warning("acquisition reply failed: %s", e)


# ------------------------------------------------- conversational assistant
# A policy entirely separate from group acquisition, and the separation is the
# requirement rather than a nicety: an ordinary group message must never start a
# chat, and a chat must never produce a trial offer.
#
# It is enforced structurally. The assistant is reachable only from
# `_addressed_to_bot` — a reply to a message this bot sent, or an @mention of
# this bot — and `on_group_text` returns before classifying when that is true.
# Neither path can therefore be entered by the other's traffic.
#
# ── Nexus ─────────────────────────────────────────────────────────────────
# "Nexus" is the name this project gives the conversational layer as a *role*:
# language understanding, context, intent and orchestration. It is not a model —
# which model answers is the pool's decision, and nothing here names one.
#
# The routing below is the gate the brief asks for, in the order it asks for it,
# and every step before the last one is a dictionary lookup:
#
#   1. who sent it (Telegram's own id, never a name)
#   2. what role they resolve to (`app/rbac.py`)
#   3. whether Nexus is awake (`app/nexus.py`)
#   4. whether the message is aimed at Nexus, or looks like an instruction
#   5. only then, the model
#
# An unauthorized message is refused at step 2 and never reaches Gemini. An
# authorized message that is not aimed at Nexus is *observed* at step 4 —
# recorded into that administrator's own bounded context and answered with
# silence — which is the "watch without replying" requirement, and it costs no
# AI call either.
def _addressed_to_bot(msg, ctx) -> bool:
    """Whether this message is aimed at the bot rather than at the room.

    Telegram gives exactly two unambiguous signals, and they are the first two
    checked:

    * a reply to a message this bot sent, and
    * an @mention of this bot's own username.

    The third is the operator's own list of aliases (``BOT_ALIASES``), which
    exists because a group often calls the bot something other than its
    username. It is empty by default and matched as a whole word, because
    matching a bare word is a heuristic — and a heuristic that decides whether
    the bot speaks is a decision the operator should make explicitly rather than
    one this file should assume.

    Nothing else counts. A message that merely contains the word «ربات», or a
    question the rules happen to like, is ordinary conversation and stays in the
    acquisition pipeline. The brief is explicit that seeing a group message is
    not an invitation to start chatting.

    A caption counts as text here as well as in the handlers: somebody who
    addresses the bot while sending a photo has addressed the bot.
    """
    replied = getattr(msg, "reply_to_message", None)
    if replied is not None:
        author = getattr(replied, "from_user", None)
        if author is not None and getattr(author, "id", None) == ctx.bot.id:
            return True

    text = _message_text(msg).lower()
    if not text:
        return False

    username = (getattr(ctx.bot, "username", "") or "").strip().lower()
    if username and f"@{username}" in text:
        return True

    for alias in _bot_identity.get("aliases", ()):
        if _mentions_alias(text, alias):
            return True
    return False


def _message_text(msg) -> str:
    """The text of a message, whether it is a body or a caption.

    One helper rather than ``msg.text or msg.caption`` at every call site: a
    media message carries its words in ``caption``, and forgetting that is how a
    photo with "سلام ربات" on it silently stops being addressed to the bot.
    """
    return (getattr(msg, "text", None) or getattr(msg, "caption", None) or "")


def _mentions_alias(text: str, alias: str) -> bool:
    """Whole-word, case-insensitive match of a configured alias.

    Escaped before it becomes a pattern even though the alias comes from
    configuration rather than from a user: an operator typing ``.`` should get a
    literal dot, not a wildcard, and getting that wrong is a silent widening of
    what the bot answers to.
    """
    if not alias:
        return False
    try:
        return re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text) is not None
    except re.error:
        return False


# ------------------------------------------------- Nexus: routing
# Whether the bot can actually *see* every message in each group. Filled in at
# startup by asking Telegram for the bot's own membership status, because the
# answer decides whether silent observation works at all — and an assumption
# about it would be the worst kind of wrong, since the failure is invisible.
#
# The rule, verified against the live deployment on 2026-09-22: this bot has
# Telegram privacy mode **enabled** (``getMe`` reports
# ``can_read_all_group_messages=false``) and still receives every ordinary group
# message, because it is an **administrator** in both groups. A bot promoted to
# group administrator receives all messages regardless of the privacy setting;
# a bot that is only a member receives commands, replies to its own messages,
# mentions, and nothing else. So observation is a capability the deployment
# holds rather than a property of the code, and the startup line says which.
_nexus_visibility: dict[int, str] = {}


def authorized_group(chat_id: int) -> bool:
    """Whether this chat is a group this deployment is registered to serve.

    The source of truth is the shared database — the ``authorized_groups``
    allowlist (``app/groups.py``), seeded once from ``GROUP_IDS`` on the first
    boot and thereafter authoritative. Nothing else is read here, and nothing
    else may be used to decide it: not the bot's Telegram admin status, not the
    bot merely being a member, not the group's title or username, not a member's
    display name, and not anything a member claims. Being *added* to a group —
    even promoted to administrator in it — is a Telegram fact, and a Telegram
    fact is **not** application authorization.

    It is fail-closed: with no registered groups, nothing is authorized. This is
    the *room* boundary, and it is the **only** thing that decides whether a
    group is served. Member and administrator status do **not** decide
    conversational eligibility — an authorized room is open to every member, and
    the speaker is not consulted here or anywhere else on this path.
    """
    return groups.is_authorized(chat_id)


def _nexus_can_observe(chat_id: int) -> bool:
    """Whether the bot is an administrator here, and so sees every message.

    ``creator`` counts as well as ``administrator``, and the two are the same
    fact for this purpose: Telegram delivers every group message to a bot that
    holds either status, whatever the privacy setting says. Keeping the pair
    together here means the status report and the visibility log cannot disagree
    about whether a group is observable.
    """
    return _nexus_visibility.get(int(chat_id)) in ("administrator", "creator")


def _nexus_directed(msg, ctx) -> bool:
    """Whether this message is aimed at Nexus.

    Two independent signals, both server-checked: the Telegram-native ones (a
    reply to this bot, an @mention of it, a configured ``BOT_ALIASES`` word) and
    the Nexus names (``NEXUS_NAMES``), which exist because a group calls the
    assistant by its role rather than by a username nobody can mention.
    """
    if _addressed_to_bot(msg, ctx):
        return True
    return nexus.is_named(_message_text(msg))


def _nexus_will_answer(msg, ctx) -> bool:
    """Whether the conversational layer will actually answer this message.

    The acquisition pipeline has to know this in order to stand down, and it is
    stated once so both handlers ask the same question. They did not: the
    acquisition guard tested ``_addressed_to_bot`` while the chat handler tested
    ``_nexus_directed``, and the difference is not academic — ``_nexus_directed``
    also accepts the ways a group writes the assistant's name («نکسی»,
    «نکسوسو»). A message calling Nexus by name therefore fell through the
    boundary the guard exists to draw: answered by the assistant *and* offered a
    trial by acquisition, which is the double-handling that comment forbids.

    The three conditions are the chat handler's own gates rather than a second
    reading of them — it must be able to answer, the room must be authorized, and
    the message must be aimed at it. The room is what decides eligibility, not
    the sender: every member of a registered group is answered, so a member
    naming the assistant must stand the acquisition offer down exactly as an
    administrator does, or the one message would get both a reply and a trial.
    """
    if not _chat_active() or not _nexus_directed(msg, ctx):
        return False
    chat_id = int(getattr(msg, "chat_id", 0) or 0)
    return nexus.accepts_in_group(room_authorized=authorized_group(chat_id))


def _nexus_observe(room, user, msg, text: str) -> bool:
    """Record an unaddressed administrator message as context. Never replies.

    The reply marker is the important part. "این کاربر خیلی مزاحم شده" followed
    by "بنش کن" only resolves if the first message carried *who* it was about —
    and the only place that information exists is the reply it was sent as. The
    id is written into the stored turn because the model is later shown that
    turn and needs the id, not a name it would have to look up again.
    """
    if not nexus.observation_enabled():
        return False
    kind = ""
    ref = media.describe(msg)
    if ref is not None:
        kind = getattr(ref, "kind", "") or ""
    reply_user_id, reply_name, _ = _reply_context(msg)
    return nexus.observe(
        room.id,
        user.id,
        text,
        kind=kind,
        reply_user_id=reply_user_id,
        reply_name=reply_name,
    )


# ── Nexus Awareness: reading the room ─────────────────────────────────────
# The observation layer. Capture is unconditional and free; the model call is
# batched, debounced and budgeted. See ``app/awareness.py`` for the policy and
# ``AgentMD.md`` §35 for the architecture.
#
# Five pieces of state, all in this process and all bounded:
#   * which rooms are being analysed right now, so two ticks cannot overlap on
#     one room and produce two replies to the same conversation;
#   * when each room was last analysed, which is the minimum-interval brake;
#   * which rooms asked to be read promptly, which is the only thing
#     ``nexus.looks_actionable`` still does — a timing hint, never a verdict;
#   * whether a sweep is already running, so a slow pass cannot pile up behind
#     the job queue's next tick;
#   * when each room's debounce expires, which is what stops a quiet room from
#     waiting for the next sweep.
_awareness_inflight: set[int] = set()
_awareness_last_pass: dict[int, float] = {}
_awareness_urgent: set[int] = set()
_awareness_sweeping = False

# The debounce, as a per-room deadline rather than as something a 15-second tick
# happens to notice.
#
# This is the whole of the latency fix, and it is worth stating in the terms of
# what it removes. The policy is "read a room once it has been quiet for
# ``NEXUS_AWARENESS_DEBOUNCE_SECONDS``", and the sweeper is a coarse way to ask
# whether that has happened: it wakes every ``TICK_SECONDS`` and checks. So a
# message that made the room due at 8 seconds was read at the next multiple of
# 15 — 8 to 23 seconds of waiting, median 15.5, of which the policy accounts for
# 8 and the rest is the tick. That was the reported slowness: not the model, and
# not the database, but a room that had already gone quiet waiting for a timer
# that was not about it.
#
# A deadline is set when a message is captured and pushed out by each later one,
# which is the debounce, coalesced by assignment — a burst of twenty messages
# sets the same key twenty times and costs one wake-up. A fast tick then reads
# rooms whose deadline has passed and does nothing else. It performs no query
# and no work while every room is still talking, so the interval can be short
# without making the bot busy.
#
# The sweeper stays, unchanged and still on its 15-second interval, as the
# ceiling: it is what reads a room that never falls quiet (past
# ``NEXUS_AWARENESS_MAX_WAIT_SECONDS``), what picks up a room after a restart
# has emptied this dict, and what makes a lost deadline a delay rather than a
# silence. The deadline can only make a pass *earlier*; it cannot make one
# happen that the policy would have refused.
_awareness_ready_at: dict[int, float] = {}

# The highest message id in each room that the *addressed* path is answering, or
# has answered. One integer per room, and it exists to close the one race the
# window cannot: ``awareness.nexus_has_the_last_word`` is read from the stored
# window, and the assistant's turn only lands in that window once the reply has
# been sent — which is seconds after the message arrived, because a model call
# is in between. A pass that runs in that gap would see an unanswered question
# and answer it, and the room would get two replies after all.
#
# So the marker is set *before* the answer is awaited and cleared only if no
# answer went out. A message that was never answered leaves no marker behind, so
# the ambient path is free to try — which is what keeps this from turning a rate
# limit into silence.
_nexus_addressed: dict[int, int] = {}
# The deadline tick's own interval. One second is below the resolution anybody
# can perceive as "late" and costs a dictionary scan.
AWARENESS_DEADLINE_TICK_SECONDS = 1.0


async def _awareness_capture(
    ctx, room, user, msg, text: str, principal, *, directed: bool = False
) -> bool:
    """Record one received group message into the room window. No AI call.

    Called for every message the bot can actually receive — including ordinary
    members who will never be answered. That is the point of the feature: the
    assistant understands the room rather than only the messages aimed at it.
    It grants nothing. A captured message can still not produce an action,
    because every tool call is authorised separately against the actor's id.

    Media is recorded as its *kind* and never as bytes, on the same rule the
    moderation path follows: the window is text, and it has to stay text or one
    photograph becomes a row every later prompt has to carry. **Speech is the one
    exception**, and it is not really an exception: a voice note from an actor is
    transcribed, so what the room records is the words that were spoken rather
    than the string «[voice]». An administrator who says an instruction out loud
    instead of typing it is giving an instruction, and a window that shows only
    that something was said would make the assistant deaf to exactly the people
    it is supposed to be listening to.

    Only an **actor's** speech is transcribed. A member's voice note is recorded
    as its kind, because the room is read to understand the people who can act,
    and transcribing every voice note in a busy group would spend the speech
    quota on the messages that can never become an instruction. A note that
    cannot be read — too long, no speech, a failed download — keeps the kind
    marker, so the window degrades rather than losing the message.

    A **directed** voice note is not transcribed here. The conversational path
    transcribes it a moment later to answer it, and doing it twice would be two
    speech calls for one sentence; the answered turn and its reply are both in
    the window, so the context is not lost.

    The reply edge is written as columns rather than folded into the body, which
    is the change that made «این رو سکوت کن» resolvable. It used to be a
    bracketed sentence appended to the text — «[در پاسخ به X (123)] ...» — which
    put the one fact an instruction needs inside a string the model had to parse
    and then contradicted it in the trusted context by saying there was no
    referent. As a column it is structure, the renderer draws it as an edge, and
    ``awareness.instruction_block`` can state it as the answer to "who".

    ``directed`` and ``actor`` are recorded because they are what
    ``awareness.anchor`` uses to decide which message a pass is *about*. They are
    hints for attribution and nothing else; the authority for anything the turn
    does is resolved again from the id.

    Nothing is captured while Nexus is switched off. Recording a group's
    conversation is part of what the assistant does, so "off" has to mean off:
    an owner who silenced the assistant must not find that it went on reading
    the room. This matches the pre-existing observation path, which is only
    reached through ``nexus.accepts``.

    The room's own name and type are cached here, before any gate, and that is
    not part of the capture: the handler already holds ``effective_chat``, so
    the one place in the bot that knows what the room is called is the one place
    that should write it down. Doing it here rather than on the pass is what
    keeps a pass free of a ``get_chat`` network call for a fact Telegram has
    already told us.
    """
    awareness_context.note_room(
        room.id,
        getattr(room, "title", "") or "",
        getattr(room, "type", "") or "",
    )
    if not awareness.capture_enabled() or not nexus.is_online():
        return False
    actor = nexus.is_actor(principal)
    body = (text or "").strip()
    kind = ""
    ref = media.describe(msg)
    if ref is not None:
        kind = getattr(ref, "kind", "") or "media"
        spoken = ""
        if actor and not directed and getattr(ref, "is_transcribable", False):
            spoken = await _transcribe_for_awareness(ctx, ref)
        body = spoken or (f"[{kind}] {body}".strip() if body else f"[{kind}]")
    if not body:
        return False
    reply_user_id, reply_name, reply_message_id = _reply_context(msg)
    name = getattr(user, "full_name", "") or getattr(user, "first_name", "") or ""
    recorded = awareness.capture(
        room.id,
        user.id,
        awareness.role_of(principal),
        name,
        body,
        message_id=int(getattr(msg, "message_id", 0) or 0),
        reply_user_id=reply_user_id,
        reply_name=reply_name,
        reply_message_id=reply_message_id,
        directed=directed,
        actor=actor,
        kind=kind,
        username=str(getattr(user, "username", "") or ""),
    )
    if recorded:
        # Increment U: classify the message for the *scheduler*, once, here,
        # where the text is already in hand and a row is already being written.
        # It is the same reading the chat path takes of a message it answers —
        # "does this need the room" — reduced to one word and stored keyed by
        # the room, so the pass loop can decide whether the next request is
        # worth spending without ever letting the words reach ``awareness.due``
        # (whose signature a test asserts). A failed classification is not a
        # failed capture: ``read`` never raises, and the caller ignores the
        # result either way.
        awareness_schedule.note(
            room.id,
            awareness_schedule.read(
                body,
                kind=kind,
                reply=bool(reply_user_id),
                media=bool(kind),
                directed=directed,
            ),
        )
    return recorded


async def _transcribe_for_awareness(ctx, ref) -> str:
    """The words in an actor's voice note, or ``""`` if they cannot be read.

    Never raises and never reports a problem to the room: this is a read of a
    message for the assistant's own understanding, and a note nobody could
    transcribe must not become a reason the room stops being read or the sender
    is told anything. An empty answer means "keep the kind marker", which is the
    same row the capture would have written before this existed.
    """
    try:
        result = await transcribe.transcribe_ref(
            ref, download=lambda fid: _download_file(ctx, fid)
        )
    except Exception:  # noqa: BLE001 - a room read is never worth a failure
        log.exception("could not transcribe a voice note for the room window")
        return ""
    if not result.ok:
        log.info(
            "awareness: voice note not transcribed (%s)",
            result.error or result.skipped,
        )
        return ""
    return (result.text or "").strip()


def _awareness_note_reply(chat_id: int, text: str) -> None:
    """Record what Nexus itself said, so the next pass reads a whole exchange.

    Without this the assistant would see questions and never its own answers,
    and would cheerfully answer the same thing twice. A failed send is not
    recorded — ``_send_chat`` returns whether it went out — because a reply that
    nobody saw is not part of the conversation.

    Increment U's residual is closed in the same place. If what Nexus said was a
    *question*, the room is mid-exchange: the reply to it («بله») is
    self-contained by every rule the project has, so the scheduler would rightly
    read it ``LOW`` and postpone the very pass that is supposed to read the
    answer. The stamp records only that the server asked — it never reads a
    member's message and never decides which message answers — and ``defer``
    refuses to postpone a room carrying it. See ``awareness_schedule.awaiting``.
    """
    if not awareness.capture_enabled() or not (text or "").strip():
        return
    said = text.strip()
    awareness.capture(chat_id, 0, awareness.ROLE_NEXUS, "", said)
    awareness_schedule.awaiting_note(chat_id, said)


def _awareness_context(
    chat_id: int,
    *,
    messages: list[dict] | None = None,
    anchor: dict | None = None,
) -> str:
    """The system-instruction context for a pass: roster, memory, digest, room.

    The transcript is *not* in here. For the awareness pass the transcript is
    the user turn — the thing to be read — and duplicating it would double the
    prompt for no gain.

    The **digest** is: ``awareness.activity`` is the server's own count of the
    whole three-day window — who spoke, how much, what each said last, and who
    said nothing. It is here rather than in the transcript because three days
    cannot fit in a prompt and the transcript is trimmed to the newest
    conversation; without it, widening the window from 150 messages to three days
    would change nothing about what the model knows. It makes no provider call,
    so it costs nothing in the currency that is rationed.

    The last part is staged, and that is the point: ``awareness_context``
    renders the cheap context always (the room's name, and who was here last
    time) and the deeper context — recent administrative actions, and who the
    batch is about — only when a predicate over the batch says the conversation
    calls for it. See ``app/awareness_context.py`` for why: the awareness
    allowance is rationed in *requests*, so context nobody asked for is paid on
    every pass.

    ``messages`` and ``anchor`` are the ones the pass already read. Handing them
    in is what keeps this from becoming a second read of the room per pass.
    """
    context = awareness_context.build_ctx(
        chat_id,
        messages=messages,
        anchor=anchor,
        nexus_id=int(_bot_identity.get("id") or 0),
    )
    return (
        awareness.roster()
        + awareness.memory_block(chat_id)
        + awareness.activity(chat_id)
        + awareness_context.blocks(context)
    )


async def _awareness_pass(ctx, chat_id: int, row: dict) -> None:
    """One batched read of one room: understand it, and maybe speak.

    The order is the requirement. The room is read first and recorded whatever
    the model decides, because understanding is the point and speaking is the
    exception. Only then is the response decision applied, and the two
    conditions on it are the existing security boundary rather than anything new:

    * the room must be one Nexus serves and the layer must be awake
      (``nexus.accepts_in_group``) — the speaker does not decide eligibility,
      because an authorized room is open to every member; and
    * if a write tool ran, the confirmation is sent regardless of what the model
      decided to say — an action that happened and was never acknowledged is the
      failure the addressed path already goes out of its way to avoid.

    Everything here is wrapped: a pass runs in the job queue, and an exception
    escaping it would be a traceback in the log and a stalled room, not a
    failed pass. Failures degrade to "say nothing and try again later".

    The trace is taken here rather than inside ``_awareness_read`` so that it
    covers the whole pass — including the paths that return early because there
    was nothing to read. ``waited_ms`` is measured from the capture of the
    newest unread message, which is the number the owner was complaining about;
    the rest are the stages after the wait.
    """
    trace = awareness.PassTrace(
        chat_id=int(chat_id), trigger_at=awareness.trigger_at(chat_id)
    )
    trace.mark("batch")
    try:
        await _awareness_read(ctx, chat_id, row, trace)
    finally:
        trace.mark("end")
        # Durations only. See ``awareness.PassTrace``: the transcript, the
        # decision and the reply are other people's words and are not logged.
        log.info("awareness timing %s", trace.summary())


async def _awareness_read(
    ctx, chat_id: int, row: dict, trace: awareness.PassTrace
) -> None:
    """The body of one pass. See ``_awareness_pass`` for the contract.

    The room is read **once** and everything is derived from that one read:
    the anchor, the transcript, and the roles. It used to be read three times —
    once for the speaker, once for the transcript, once for the participants —
    which was three queries per pass for one answer, and the seam between them
    was a real bug rather than only a cost: the speaker could be a different
    message than the one the transcript ended with.
    """
    max_id = int(row.get("max_id") or 0)
    messages = awareness.window(chat_id)
    speaker = awareness.anchor(chat_id, messages=messages)
    if speaker is None:
        # Nothing but the assistant's own words; there is no conversation to
        # read and no one to answer.
        awareness.skip(chat_id, seen_message_id=max_id)
        return

    actor_id = int(speaker.get("user_id") or 0)
    counters: dict = {"writes": 0}
    tools, context, on_tool = await _awareness_turn(
        ctx, chat_id, actor_id, speaker=speaker, counters=counters
    )
    # The seam between building what the model is handed and reading the room.
    # See ``PassTrace.summary``: without it a large room and a slow pass are the
    # same number, and only one of them is worth optimising.
    trace.mark("context")
    transcript = awareness.render(chat_id, messages=messages)
    trace.mark("window")
    if not transcript:
        awareness.skip(chat_id, seen_message_id=max_id)
        return

    # Everything the model is handed is assembled *before* the request mark, so
    # it lands in ``batch_ms`` — the pre-request stage — rather than in
    # ``gemini_ms``. That matters for the staged context more than it looks: it
    # is the one part of this pass whose cost is new, and a promise that it is
    # bounded is only checkable if it is not counted as model time. (The
    # ``ctx_ms``/``window_ms`` split covers the tool declarations and the
    # transcript; what is left of ``batch_ms`` is this assembly.)
    prompt_context = (
        _awareness_context(chat_id, messages=messages, anchor=speaker)
        + awareness.instruction_block(chat_id, messages=messages)
        + (context or "")
    )
    trace.mark("request")
    result = await chat.awareness(
        transcript,
        prompt_context,
        tools=tools,
        on_tool=on_tool,
    )
    trace.mark("response")
    if not result.answered:
        # A skipped or failed pass leaves the understanding alone and only moves
        # the watermark. The messages stay in the window, so the next pass that
        # completes re-reads them and nothing is lost but time.
        log.info(
            "awareness pass did not complete chat=%s error=%s skipped=%s",
            chat_id,
            result.error or "-",
            result.skipped or "-",
        )
        awareness.skip(chat_id, seen_message_id=max_id)
        return

    decision = awareness.parse_decision(result.text)
    trace.mark("decision")
    if decision is None:
        # Unreadable answer. Deliberately not "send the raw text": the one
        # outcome worth losing a pass over is the assistant speaking into a room
        # on the strength of an answer nobody could read.
        log.warning(
            "awareness answer could not be read chat=%s chars=%d",
            chat_id,
            len(result.text or ""),
        )
        awareness.skip(chat_id, seen_message_id=max_id)
        return

    # The model's claim about *who* the batch concerns is checked against the
    # window before it is stored: a model that names somebody the room never
    # mentioned has not understood the room, and recording its guess would let
    # the next pass inherit the mistake. This is validation, not authority —
    # nothing acts on the result.
    decision["about"] = awareness.about_in_window(
        decision.get("about") or 0, messages
    )

    # The server's own reading of the conversation's subject — what the room is
    # talking about, and whether it is talking about Nexus. Computed once here,
    # before the record, so the row the next pass inherits is the reading that
    # produced this pass's decision rather than a second one taken later.
    reading = awareness.subject_of(
        chat_id,
        messages=messages,
        bot_id=int(_bot_identity.get("id") or 0),
        bot_username=str(_bot_identity.get("username") or ""),
    )

    awareness.record(
        chat_id, seen_message_id=max_id, decision=decision, reading=reading
    )
    log.info(
        "awareness chat=%s relevant=%s respond=%s writes=%d intent=%s about=%s "
        "subject=%s/%d participation=%d topic=%r",
        chat_id,
        decision.get("relevant"),
        decision.get("respond"),
        counters.get("writes", 0),
        decision.get("intent") or "-",
        decision.get("about") or 0,
        reading.kind,
        reading.confidence,
        int(decision.get("participation") or 0),
        (decision.get("topic") or "")[:60],
    )

    # The response decision. ``writes`` forces a reply because the action has
    # already happened; otherwise the model's own judgement decides, and it is
    # then filtered by whether this speaker is one Nexus answers at all.
    #
    # The one thing that can override the model's judgement in the *silent*
    # direction is the duplicate guard: if this batch has already been answered,
    # answering again is the bug the owner reported as "one request, two
    # replies". The batch has still been read and recorded above — only the
    # reply is withheld — so nothing is lost and no watermark is moved.
    #
    # Two conditions, because there are two ways a batch can be answered and
    # only one of them is visible in the window:
    #
    #   * ``_nexus_addressed`` covers the answer that is *being* written right
    #     now, which the window cannot show yet;
    #   * ``nexus_has_the_last_word`` covers the answer that was written before
    #     this process started, which the marker cannot know about.
    #
    # A write tool's confirmation is never suppressed: the action has already
    # happened, and an unacknowledged change is worse than a repeated sentence.
    already_answered = int(max_id) <= _nexus_addressed.get(chat_id, 0)
    if not already_answered:
        already_answered = awareness.nexus_has_the_last_word(chat_id)
    if already_answered:
        log.info(
            "awareness withheld a reply: the batch was already answered chat=%s "
            "max_id=%s responded=%s",
            chat_id,
            max_id,
            bool(decision.get("respond")),
        )
    # The participation floor, and the answer to "do not reply to every
    # conversation". ``respond`` is the model's claim that a reply is called for;
    # the server weighs that claim against its own reading of the subject and
    # against the model's own stated confidence, and speaks only when one of them
    # stands behind it. The two are combined as a maximum because they are
    # independent readings of the same room — the server's is deterministic and
    # the model's is semantic — and either being sure is enough, while neither
    # being sure is exactly the weak case that must stay silent.
    #
    # A write tool's confirmation is never filtered by this: the action has
    # already happened, and an unacknowledged change is worse than a sentence
    # nobody needed.
    confidence = max(
        reading.confidence if reading.is_nexus else 0,
        int(decision.get("participation") or 0),
    )
    floor = max(0, int(config.NEXUS_AWARENESS_PARTICIPATION_FLOOR))
    speaking = bool(decision.get("respond")) and not already_answered
    if speaking and confidence < floor:
        log.info(
            "awareness stayed silent: participation below the floor chat=%s "
            "subject=%s/%d participation=%d floor=%d",
            chat_id,
            reading.kind,
            reading.confidence,
            int(decision.get("participation") or 0),
            floor,
        )
        speaking = False
    wants_to_speak = bool(counters.get("writes")) or speaking
    if not wants_to_speak:
        return
    if not nexus.accepts_in_group(room_authorized=authorized_group(chat_id)):
        # The room boundary, plus "the layer is awake". Awareness observes a
        # room; it does not widen who may talk to Nexus, and it does not narrow
        # it either — an authorized room is open to every member, so this only
        # ever refuses when Nexus is off or the room is not ours.
        log.info(
            "awareness stayed silent: room not served or Nexus offline chat=%s "
            "actor=%s",
            chat_id,
            actor_id,
        )
        return

    message = decision.get("message")
    if not message and counters.get("writes"):
        # The action ran but the model gave no wording for it. Fall back to the
        # service's own sentence rather than leaving a change unacknowledged.
        message = config.NEXUS_AWARENESS_ACTION_TEXT
    if not message:
        return
    # Which message the ambient reply quotes, resolved server-side from the
    # stored window. The subject reading names the conversational turn it is
    # about, and the id can only be one already on a row — the model has no part
    # in choosing it. 0 means "no destination", which stays a plain group
    # message exactly as before.
    destination = subject.destination(
        reading, messages, bot_id=int(_bot_identity.get("id") or 0)
    )
    if await _send_chat(ctx, chat_id, message, destination or None):
        _awareness_note_reply(chat_id, message)
    trace.mark("send")


def _awareness_allowance_gap(now: float | None = None) -> float:
    """The minimum gap between two passes in one room, given the day's allowance.

    ``NEXUS_AWARENESS_MIN_INTERVAL_SECONDS`` (20) and
    ``NEXUS_AWARENESS_DAILY_LIMIT`` (200) are two numbers about the same thing,
    and they disagreed by a factor of twenty-one: a room that is at all busy
    reaches the interval once every twenty seconds, so the allowance was gone in
    about an hour and every later pass failed with ``pool_empty`` until the API
    day rolled over. Measured on this deployment before the change — 203
    requests spent, then 141 consecutive failed passes, each of which had
    already rendered the transcript and built 26 KB of tool declarations.

    So the gap is not a constant. It is the configured floor, or whatever
    spacing would make what is left of the allowance last until the end of the
    API day, whichever is longer. Early in the day with a full allowance that is
    still the floor, so nothing slows down while there is budget to spend; as
    the allowance is consumed the gap widens smoothly rather than awareness
    switching off at a cliff.

    A pool with no daily budget is not paced at all. There is no allowance to
    spread, and inventing a rate limit for a workload nobody capped would be
    this function deciding a policy that belongs in configuration.
    """
    floor = max(1.0, float(config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS))
    pool = gemini_pool.pool_for("awareness")
    if pool is None or not pool.daily_budget:
        return floor
    remaining = pool.daily_remaining(now)
    if remaining <= 0:
        # Nothing left today. Wait for the rollover rather than retrying against
        # a pool that cannot answer: those retries cost nothing at the provider
        # but they are not free here, and they fill the log with a failure that
        # is already known and cannot be acted on.
        return max(floor, db.ai_day_seconds_left(now))
    return max(floor, db.ai_day_seconds_left(now) / remaining)


def _awareness_affordable(chat_id: int, now: float | None = None) -> bool:
    """Whether the day can still afford another pass in this room.

    Deliberately per room rather than global. The allowance is shared, but
    starving one room because another happened to be read first is a fairness
    bug rather than a saving, and ``NEXUS_AWARENESS_MAX_CHATS_PER_TICK`` is
    where the per-tick bound belongs.

    Checked before the transcript is rendered, so a pass the pool cannot serve
    costs a dictionary lookup and a cached counter read instead of a prompt.
    """
    last = _awareness_last_pass.get(int(chat_id), 0.0)
    if not last:
        # Never read. The floor is not a debt to be paid before the first pass.
        return True
    moment = time.time() if now is None else float(now)
    return (moment - last) >= _awareness_allowance_gap(moment)


async def _awareness_run_room(ctx, row: dict, *, urgent: bool = False) -> bool:
    """Read one room if it is due. Returns whether a pass actually ran.

    The single place a pass is started, shared by the sweeper and by the
    urgency hint, so the two cannot drift apart in their guards. Every refusal
    here is about cost or safety, never about meaning:

    * a room already in flight is left alone, so two callers cannot produce two
      replies to one conversation;
    * a switched-off assistant reads nothing at all, so "off" costs no allowance
      and sends no message — the execution layer would refuse any action anyway
      (``OUTCOME_NEXUS_OFFLINE``), and paying for a pass that can only be denied
      is waste rather than safety;
    * a room Telegram is not delivering ordinary messages for is not read at all
      — there is nothing in the window but commands and mentions;
    * the room must be *due*, which is a timing question (see
      ``awareness.due``), and ``urgent`` only relaxes the wait-for-quiet clause;
    * and the day must still be able to afford it, which is the second timing
      question and a different one — see ``_awareness_allowance_gap``.

    Increment U adds one more, and it is a *selection* question rather than a
    safety one: a room whose unread batch carries no evidence that it needs the
    room is **postponed** (``awareness_schedule.defer``) so the rationed request
    goes to a room that does. It is asked only on the ordinary path — the urgent
    path never consults it, so a hint can delay a routine reading and never a
    prompted one — and it can only postpone a room, never admit one, because
    every gate above it has already run.
    """
    chat_id = int(row.get("chat_id") or 0)
    if not chat_id or chat_id in _awareness_inflight:
        return False
    if not authorized_group(chat_id):
        # A room this deployment is not registered to serve is not read, even if
        # a stale awareness row survives from a configuration that once listed
        # it. The group boundary is the same one the handlers apply, and it is
        # applied here too so no path can read a room the handlers would refuse.
        return False
    if not nexus.is_online():
        return False
    if not _nexus_can_observe(chat_id):
        # Telegram is not delivering this room's ordinary messages, so there is
        # no conversation to read. Advancing the watermark keeps a room we
        # cannot see from being retried forever.
        awareness.skip(chat_id, seen_message_id=int(row.get("max_id") or 0))
        return False
    now = time.time()
    verdict = awareness.due(
        row,
        now=now,
        last_pass_at=_awareness_last_pass.get(chat_id, 0.0),
        urgent=urgent,
    )
    if not verdict:
        return False
    if not urgent:
        oldest = int(row.get("oldest_at") or 0)
        if oldest and awareness_schedule.defer(
            chat_id,
            waited=now - oldest,
            # A room holding an unapproved action is one the pass is *for*:
            # the confirmation arrives as a self-contained message that the
            # classifier rightly reads as not needing the room, so without this
            # the owner's approval would be postponed by up to the retention
            # window. The server knows it is waiting, so it says so.
            waiting=bool(db.admin_pending_waiting(chat_id=chat_id)),
        ):
            # Eligible, but not worth a request yet: the room's batch is idle
            # chatter and it has not been waiting long enough to be read
            # anyway. This is the whole of increment U — the decision that the
            # 200 requests are better spent elsewhere — and it can only ever
            # *delay* a pass, because the bound on the delay is the window's
            # own retention and the room is re-offered on its next deadline.
            return False
    if not _awareness_affordable(chat_id):
        # The room is ready but the day is not rich enough to read it yet. This
        # is the allowance brake rather than the quiescence one, and it is
        # checked before the transcript is rendered and before the tool
        # declarations are built, so a pass the pool cannot serve costs a
        # dictionary lookup instead of 26 KB of prompt.
        return False
    _awareness_urgent.discard(chat_id)
    _awareness_inflight.add(chat_id)
    try:
        await _awareness_pass(ctx, chat_id, row)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a pass must never take the caller down
        log.exception("awareness pass failed chat=%s", chat_id)
        awareness.skip(chat_id, seen_message_id=int(row.get("max_id") or 0))
    finally:
        _awareness_inflight.discard(chat_id)
        _awareness_last_pass[chat_id] = time.time()
        # The batch has been read, so the hint it produced is spent. Dropped
        # here rather than in ``_awareness_pass`` because this is the one place
        # that knows a pass actually ran: a room refused above keeps its hint,
        # which is what stops a room the allowance cannot serve from being
        # demoted for a reason that had nothing to do with its content.
        awareness_schedule.forget(chat_id)
        # This room has just been read, so it has no deadline left to meet.
        # Dropping it here is what stops the deadline tick from waking up for a
        # room that the sweeper or the urgency hint already handled.
        _awareness_ready_at.pop(chat_id, None)
    return True


def _awareness_pending_for(chat_id: int) -> dict:
    """The pending summary for one room, or an empty one."""
    for row in awareness.pending():
        if int(row.get("chat_id") or 0) == int(chat_id):
            return row
    return {}


async def _awareness_promptly(ctx, chat_id: int) -> None:
    """Read a room now because something in it looked like an instruction.

    This is the whole of what ``nexus.looks_actionable`` still does. It is
    deliberately *not* a separate model call: the room goes through exactly the
    same pass it would have gone through a few seconds later, so there is one
    semantic decision rather than a keyword verdict racing a model verdict. The
    hint can only make a room earlier, never more relevant, and the
    minimum-interval brake still applies — which is what stops a burst of
    actionable-sounding messages from becoming a burst of passes.
    """
    if not awareness.enabled() or not _nexus_can_observe(chat_id):
        return
    row = _awareness_pending_for(chat_id)
    if not row:
        return
    await _awareness_run_room(ctx, row, urgent=True)


def _awareness_schedule(chat_id: int) -> None:
    """Ask for this room to be read once it has been quiet for the debounce.

    One assignment, and the assignment *is* the coalescing: a burst sets the
    same key repeatedly, so twenty messages produce one deadline and one
    wake-up. Nothing is queued, so there is no queue to grow and no job to
    cancel, and the room can only be read once because the pass itself refuses
    to run twice at a time (``_awareness_inflight``).

    The ceiling is deliberately not enforced here. A room that never goes quiet
    must still be read, and that is the sweeper's job — pushing this deadline
    out for ever would be the one way this could turn a slow reply into no
    reply.
    """
    if not awareness.enabled() or not _nexus_can_observe(int(chat_id)):
        # A room Telegram is not delivering ordinary messages for will be
        # refused by ``_awareness_run_room`` anyway, so arming a deadline for it
        # would only be a wake-up that is guaranteed to do nothing.
        return
    _awareness_ready_at[int(chat_id)] = (
        time.monotonic()
        + max(0.0, float(config.NEXUS_AWARENESS_DEBOUNCE_SECONDS))
    )


def _awareness_deadline_passed(now: float) -> list[int]:
    """Rooms whose debounce has expired, oldest deadline first."""
    return sorted(
        (chat_id for chat_id, at in _awareness_ready_at.items() if at <= now),
        key=lambda chat_id: _awareness_ready_at.get(chat_id, 0.0),
    )


async def _awareness_deadline_tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read the rooms whose debounce has expired. The sweeper's fast path.

    A tick that finds nothing does one dictionary iteration and stops, which is
    why this can run every second while the sweeper runs every fifteen. It does
    no query of its own: ``_awareness_pending_for`` is only reached for a room
    whose deadline has actually passed.

    A room that is not read — because a pass is already in flight, or because
    the minimum interval has not elapsed — has its deadline re-armed at the
    brake's own deadline rather than being left to the sweeper. That is a
    bounded wait, not a spin: the pass will run when the interval is up, and the
    room cannot be re-armed more than once per interval.
    """
    if not awareness.enabled():
        return
    now = time.monotonic()
    for chat_id in _awareness_deadline_passed(now):
        if not nexus.is_online():
            # Nothing is read while the assistant is off, so nothing needs a
            # deadline. Dropping them all is the "OFF means off" rule applied to
            # this timer: no query, no pass, no allowance.
            _awareness_ready_at.clear()
            return
        row = _awareness_pending_for(chat_id)
        if not row:
            # Already read, by the urgent path or by the sweeper.
            _awareness_ready_at.pop(chat_id, None)
            continue
        if await _awareness_run_room(ctx, row):
            _awareness_ready_at.pop(chat_id, None)
            continue
        _awareness_ready_at[chat_id] = now + max(
            1.0, float(config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS)
        )


async def awareness_sweep(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The job-queue tick: find rooms with something new, and read a few.

    A tick that finds nothing pending costs one indexed query per configured
    group and no API call, which is what makes a 15-second interval affordable.
    Bounded to ``NEXUS_AWARENESS_MAX_CHATS_PER_TICK`` rooms so a slow pass cannot
    starve the rest of the bot, and guarded against overlapping ticks because
    the job queue makes no promise that a callback has finished before the next
    one starts.
    """
    global _awareness_sweeping
    if not awareness.enabled() or _awareness_sweeping:
        return
    _awareness_sweeping = True
    try:
        budget = max(1, int(config.NEXUS_AWARENESS_MAX_CHATS_PER_TICK))
        done = 0
        for row in awareness.pending():
            if done >= budget:
                break
            chat_id = int(row.get("chat_id") or 0)
            if await _awareness_run_room(ctx, row, urgent=chat_id in _awareness_urgent):
                done += 1
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - and a sweep must never kill the bot
        log.exception("awareness sweep failed")
    finally:
        _awareness_sweeping = False


def _nexus_state_request(operation: str, actor: rbac.Principal, chat_id: int):
    """One typed state-change request, stamped the way every other one is."""
    return admin_service.AdminRequest(
        operation=operation,
        chat_id=chat_id,
        actor_id=actor.user_id,
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_PYTHON,
        at=int(time.time()),
    )


def _group_request(operation: str, actor: rbac.Principal, chat_id: int):
    """One typed allowlist request for the room the command was typed in.

    The subject is the *current room*, never an id parsed out of the message:
    there is no argument a person could name a different group with, so
    registering a room and being in it are the same act. Authority is decided in
    ``app/admin_service.py`` and not here.
    """
    return admin_service.AdminRequest(
        operation=operation,
        chat_id=chat_id,
        actor_id=actor.user_id,
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_PYTHON,
        at=int(time.time()),
    )


async def _owner_state_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    actor: rbac.Principal,
    text: str,
) -> bool:
    """Handle an owner's spoken state command. Returns True when it was one.

    This path exists because it is the only one that keeps working when it is
    most needed. Switching Nexus back on cannot depend on the model — Nexus is
    off, so the model is not being consulted at all, and a Gemini outage must not
    leave the assistant permanently silent either. A fixed phrase list is the
    right design here for exactly that reason, and it is why this is the one
    place in the Nexus layer where the wording is matched rather than understood.

    **Several switches can be spoken about, and they are not the same switch.**
    Nexus off is "the assistant is silent"; awareness off is "the assistant is
    answering, and not reading the room"; search off is "the assistant answers
    without looking anything up"; Voice Context off is "a voice note is
    transcribed and answered as text, exactly as before". They share the verbs —
    «خاموش», «روشن», «باز» — so a router that read only the verb would move the
    wrong one: silence the assistant when the owner meant to stop it reading the
    room, or stop it reading the room when the owner meant to answer a voice note
    in text. Which switch is meant is decided by the layer's own ``named``
    function, and each layer's name wins over the assistant's own name.

    Three conditions, all required, and the third is what stops the group's
    conversation from toggling either switch:

    * the speaker must be the owner, resolved from their Telegram id — never
      from what they wrote about themselves;
    * the message must be aimed at one of the two, by name or by reply or by
      mention. Naming the awareness layer counts as aiming, because that is how
      the owner actually addresses it;
    * and the words must ask for exactly one direction — ``command_from``
      refuses a contradiction or a negation rather than guessing.

    The transition itself is not performed here. It becomes a typed request and
    goes to ``app/admin_service.py``, which re-authorises it against
    ``nexus.control`` and audits it, exactly like every other action. So this
    function decides *what was asked for*, and the service decides whether the
    person asking may have it.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return False
    if not actor.is_owner:
        return False
    about_awareness = awareness.named(text)
    about_search = web_search.named(text)
    about_voice_context = voice_context.named(text)
    names_a_layer = (
        nexus.is_named(text)
        or about_awareness
        or about_search
        or about_voice_context
    )
    if not (names_a_layer or _addressed_to_bot(msg, ctx)):
        return False
    # ``names_a_layer`` is passed rather than recomputed because it is exactly
    # the fact that decides whether the ambiguous half of the vocabulary applies:
    # «بیا پایین» and «راه بنداز» are commands about the layer when the layer is
    # named and ordinary speech otherwise.
    if about_voice_context:
        # The layer's own pair — «باز»/«بسته» — which is unambiguous *because*
        # the layer is named, plus the shared switch words through
        # ``voice_context.command_from``'s own fallback. Kept apart from the
        # ``nexus.command_from`` call below because «باز» is one of the
        # commonest words in Persian and must never move the assistant's own
        # switch.
        direction = voice_context.command_from(text)
        wanted = (
            nexus.ONLINE
            if direction == voice_context.ON
            else nexus.OFFLINE
            if direction == voice_context.OFF
            else None
        )
    else:
        wanted = nexus.command_from(text, names_layer=names_a_layer)
    if wanted is None:
        return False

    # Which switch the words are about. The order is the safety property rather
    # than a preference: each layer is more specific than the assistant itself,
    # and getting it wrong moves the wrong switch — silencing the assistant when
    # the owner meant to stop it reading the room, or moving the assistant when
    # they meant the voice layer. Resolved once here and used for the operation,
    # the sentences and the "already" label below, so the three can never
    # disagree about which switch was spoken about.
    wants_on = wanted == nexus.ONLINE
    if about_voice_context:
        layer = "voice_context"
        operation = (
            "voice_context_online" if wants_on else "voice_context_offline"
        )
        done = (
            config.VOICE_CONTEXT_ON_DONE_TEXT
            if wants_on
            else config.VOICE_CONTEXT_OFF_DONE_TEXT
        )
        already = config.VOICE_CONTEXT_ALREADY_TEXT
        was_on = voice_context.running()
    elif about_search:
        layer = "search"
        operation = "search_online" if wants_on else "search_offline"
        done = (
            config.NEXUS_SEARCH_ON_DONE_TEXT
            if wants_on
            else config.NEXUS_SEARCH_OFF_DONE_TEXT
        )
        already = config.NEXUS_SEARCH_ALREADY_TEXT
        was_on = web_search.running()
    elif about_awareness:
        layer = "awareness"
        operation = "awareness_online" if wants_on else "awareness_offline"
        done = (
            config.NEXUS_AWARENESS_ON_DONE_TEXT
            if wants_on
            else config.NEXUS_AWARENESS_OFF_DONE_TEXT
        )
        already = config.NEXUS_AWARENESS_ALREADY_TEXT
        was_on = awareness.running()
    else:
        layer = "nexus"
        operation = "nexus_online" if wants_on else "nexus_offline"
        done = (
            config.NEXUS_ONLINE_DONE_TEXT if wants_on else config.NEXUS_OFFLINE_DONE_TEXT
        )
        already = config.NEXUS_ALREADY_TEXT
        was_on = nexus.is_online()

    result = await admin_service.execute(
        _nexus_state_request(operation, actor, room.id),
        TelegramGateway(ctx),
        actor=actor,
        bot_id=getattr(ctx.bot, "id", 0),
    )
    if not result.ok:
        log.info(
            "owner state command refused actor=%s operation=%s outcome=%s reason=%s",
            actor.user_id,
            operation,
            result.outcome,
            result.reason,
        )
        await _reply_in_group(
            ctx, room.id, _refusal_text(result), reply_to=msg.message_id
        )
        return True

    log.info(
        "owner state command actor=%s operation=%s", actor.user_id, operation
    )
    # The stored switch and the *effective* state are two halves of one answer,
    # and «آگاهی روشن» can only move one of them. When the deployment has the
    # layer off, the row is stored and nothing runs — so this is checked before
    # the no-op branch below, because "already on" would be just as wrong as
    # "turned on": either sentence reports a half that is not the whole.
    #
    # One branch, chosen by the layer resolved above, rather than three
    # independent tests: with three, a message that named two layers could be
    # acted on as one and answered as another.
    config_off = (
        (layer == "voice_context" and not voice_context.configured())
        or (layer == "search" and not web_search.configured())
        or (layer == "awareness" and not awareness.configured())
    )
    if wants_on and config_off:
        await _reply_in_group(
            ctx,
            room.id,
            {
                "voice_context": config.VOICE_CONTEXT_CONFIG_OFF_TEXT,
                "search": config.NEXUS_SEARCH_CONFIG_OFF_TEXT,
                "awareness": config.NEXUS_AWARENESS_CONFIG_OFF_TEXT,
            }[layer],
            reply_to=msg.message_id,
        )
        return True
    # "Nothing changed" and "it changed" are different facts and the owner
    # acted in order to change something. A bare confirmation for a no-op is the
    # sentence that makes an owner say "it doesn't work" about a switch that is
    # already in the state they asked for.
    if was_on == wants_on:
        # ``already`` is the right sentence for whichever switch was spoken
        # about, and all of them take a ``{state}`` placeholder. The label is
        # read from the same source the live gate reads, so the sentence cannot
        # report a state the switch is not actually in: ``nexus.state_label()``
        # describes the persisted Nexus state, and the other labels are the
        # pairs the status line prints.
        if layer == "voice_context":
            state_label = (
                config.VOICE_CONTEXT_ON_LABEL
                if wants_on
                else config.VOICE_CONTEXT_OFF_LABEL
            )
        elif layer == "search":
            state_label = (
                config.NEXUS_SEARCH_ON_LABEL
                if wants_on
                else config.NEXUS_SEARCH_OFF_LABEL
            )
        elif layer == "awareness":
            state_label = (
                config.NEXUS_AWARENESS_ON_LABEL
                if wants_on
                else config.NEXUS_AWARENESS_OFF_LABEL
            )
        else:
            state_label = nexus.state_label()
        await _reply_in_group(
            ctx,
            room.id,
            already.format(state=state_label),
            reply_to=msg.message_id,
        )
        return True
    await _reply_in_group(ctx, room.id, done, reply_to=msg.message_id)
    return True


# ── Nexus Voice Live: the owner's spoken commands ─────────────────────────
def _voice_refusal_text(reason: str) -> str:
    """The sentence for a refusal from the manager, chosen by its reason.

    The *gate* is ``VoiceLiveManager.refusal`` and it is asked once, in
    ``_owner_voice_command``. This function only picks which sentence to say, so
    that the four ways a call cannot start are answered with four different
    sentences — they need four different fixes, and a single "could not" sends
    the operator looking in the wrong place.
    """
    if reason == voice_errors.REASON_DISABLED:
        # ``disabled`` deliberately covers two facts, because from the gate's
        # point of view they are the same fact: no call starts here. They are
        # not the same fact to the owner, though — one is a decision they made
        # about the feature and the other about the assistant — so the sentence
        # distinguishes them while the gate does not.
        return (
            config.GEMINI_LIVE_OFF_TEXT
            if not config.GEMINI_LIVE_ENABLED
            else config.GEMINI_LIVE_NEXUS_OFF_TEXT
        )
    if reason == voice_errors.REASON_BUSY:
        return config.GEMINI_LIVE_BUSY_TEXT
    return config.GEMINI_LIVE_UNAVAILABLE_TEXT


async def _owner_voice_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    actor: rbac.Principal,
    text: str,
) -> bool:
    """Handle the owner's spoken voice-live commands. True when it was one.

    Model-free, like the assistant's own on/off switch and for the same reason:
    opening a call cannot depend on the model, because the model is what a call
    is *for*. A fixed phrase list is the right design here, not a shortcut, and
    this is the second place in the Nexus layer where the wording is matched
    rather than understood.

    **This runs before the assistant's switch, and it stands down for it.** The
    two vocabularies share a verb — «نکسوس بیا» turns the assistant on, «نکسوس
    بیا بیرون» leaves a call — so a message that reads as a switch is left to
    ``_owner_state_command`` rather than claimed here. Without that, a phrase an
    operator added to the join list could silently stop the owner from turning
    the assistant back on, which is the one command that has to keep working.

    Two conditions beyond that, and both are required:

    * the speaker must be the owner, resolved from their Telegram id — never
      from what they wrote about themselves;
    * the words must ask for exactly one direction. ``commands.action_for``
      refuses a contradiction rather than guessing, because the guess would open
      or close a voice channel.

    Starting a call is not done here either. It goes to
    ``VoiceLiveManager.start``, which is the one place that answers "may a call
    start here", and that answer is checked under a lock so two commands
    arriving together cannot both win.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return False
    if not actor.is_owner:
        return False
    action = voice_commands.action_for(text)
    if not action:
        return False
    # A phrase that also reads as the assistant's switch *is* the switch.
    # ``names_layer=True`` makes the reading deliberately wider than the
    # router's own, which is the safe direction for a guard: the more messages
    # count as a switch, the fewer this router can shadow.
    if nexus.command_from(text, names_layer=True) is not None:
        return False

    manager = voice_session.manager()
    if action == voice_commands.LEAVE:
        stopped = await manager.stop(room.id, reason="owner")
        log.info("owner voice command leave chat=%s stopped=%s", room.id, stopped)
        await _reply_in_group(
            ctx,
            room.id,
            config.GEMINI_LIVE_LEFT_TEXT
            if stopped
            else config.GEMINI_LIVE_NOT_IN_CALL_TEXT,
            reply_to=msg.message_id,
        )
        return True

    # Join. The transport is built here rather than held, because a transport is
    # per call: it is a login and a socket, and a process-wide one would be one
    # call's credentials quietly reused by the next.
    transport = telegram_voice.build()
    reason = manager.refusal(room.id, transport=transport)
    if reason:
        log.info(
            "owner voice command join refused chat=%s actor=%s reason=%s",
            room.id,
            actor.user_id,
            reason,
        )
        await _reply_in_group(
            ctx, room.id, _voice_refusal_text(reason), reply_to=msg.message_id
        )
        return True
    try:
        await manager.start(
            room.id,
            transport=transport,
            gateway=TelegramGateway(ctx),
            bot_id=getattr(ctx.bot, "id", 0),
        )
    except voice_errors.SessionConflict:
        # ``refusal`` cannot see this one: it is a race, not a state, and the
        # manager's lock is what makes it impossible for two commands to both
        # pass the check and both start.
        await _reply_in_group(
            ctx, room.id, config.GEMINI_LIVE_BUSY_TEXT, reply_to=msg.message_id
        )
        return True
    except voice_errors.VoiceLiveError as exc:
        # Every failure between the gate and a live session lands here — a
        # refused join, a provider that will not open, a spent allowance. The
        # reason goes to the log and never to the group: it can name a provider
        # decision, and the room is not the audience for that.
        log.warning(
            "owner voice command join failed chat=%s actor=%s reason=%s",
            room.id,
            actor.user_id,
            exc.reason,
        )
        await _reply_in_group(
            ctx, room.id, config.GEMINI_LIVE_FAILED_TEXT, reply_to=msg.message_id
        )
        return True
    log.info(
        "owner voice command join chat=%s actor=%s model=%s",
        room.id,
        actor.user_id,
        config.GEMINI_LIVE_MODEL,
    )
    await _reply_in_group(
        ctx, room.id, config.GEMINI_LIVE_JOINED_TEXT, reply_to=msg.message_id
    )
    return True


def _nexus_status_text(*, chat_id: int | None = None) -> str:
    """The operator's view of Nexus: the state, who changed it, and the mode.

    The mode and switch lines describe the whole deployment — Nexus is either
    online or not — but the awareness line is scoped to ``chat_id`` when given,
    because the report is rendered inside a room and one room's activity must
    not be readable from another.
    """
    described = nexus.describe()
    changed_at = described["changed_at"]
    changed = (
        datetime.fromtimestamp(changed_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if changed_at
        else config.NEXUS_NEVER_CHANGED_TEXT
    )
    changed_by = str(described["changed_by"]) if described["changed_by"] else config.NEXUS_NEVER_CHANGED_TEXT
    lines = [
        config.NEXUS_STATUS_TEXT.format(
            state=nexus.state_label(),
            changed=changed,
            changed_by=changed_by,
            observe=(
                config.NEXUS_OBSERVE_ON_LABEL
                if described["observe_admins"]
                else config.NEXUS_OBSERVE_OFF_LABEL
            ),
            # Read from the live room allowlist rather than from a cached line,
            # so the status can never claim a scope the gate does not hold: the
            # boundary is the room, and every member of a registered room is
            # answered.
            answer_scope=(
                f"{config.NEXUS_ANSWER_SCOPE_LABEL} ({groups.count()})"
            ),
            awareness=(
                config.NEXUS_AWARENESS_ON_LABEL
                if awareness.enabled()
                else config.NEXUS_AWARENESS_OFF_LABEL
            ),
            # Read from the live gate, so the line and the workload can never
            # disagree: "Nexus did not look that up" and "Nexus is not allowed to
            # search at all" look identical from inside a group.
            search=web_search.state_label(),
            # The same rule as the two above: read from the live gate, so the
            # line and the workload can never disagree about whether a voice
            # note is answered in text or in speech.
            voice_context=voice_context.state_label(),
            mode=admin_service.mode_line(),
        )
    ]
    # Where the deployment cannot actually see the room. Reported rather than
    # hidden, because "Nexus ignored what I said" and "Nexus never received what
    # I said" look identical from inside a group and only one of them is a bug.
    blind = [str(c) for c in groups.all_ids() if not _nexus_can_observe(c)]
    if blind:
        lines.append(
            config.NEXUS_VISIBILITY_WARNING.format(chat_id="، ".join(blind))
        )
    # What the awareness layer has actually done, and which integrations exist.
    # Both are derived from live state rather than from a counter that has to be
    # kept in step with it, so the line cannot drift from the behaviour it
    # describes. Neither can fail the command: a status report that raises is a
    # status report an operator cannot use.
    try:
        lines.append(awareness.metrics_line(chat_id=chat_id))
    except Exception:  # noqa: BLE001 - a status line is never worth a crash
        log.exception("could not build the awareness metrics line")
    try:
        from . import service_adapters

        lines.append(service_adapters.summary_line())
    except Exception:  # noqa: BLE001
        log.exception("could not build the integrations line")
    try:
        from . import identity

        lines.append(identity.resolution_line())
    except Exception:  # noqa: BLE001
        log.exception("could not build the identity resolution line")
    # Nexus Voice Live: whether it is switched on, on what, and how many calls
    # are up. A machine-key line like the two above it, so an operator can
    # answer "why did it not join" without reading a log — and so that the
    # default-off flag is visible rather than inferred.
    try:
        lines.append(voice_session.status_line())
    except Exception:  # noqa: BLE001 - a status line is never worth a crash
        log.exception("could not build the voice live line")
    return "\n".join(lines)


async def _nexus_visibility_report(app) -> None:
    """Ask Telegram what the bot can see in each group. Never fatal.

    This is the honest answer to the brief's "do not assume Telegram delivers
    every group message". Whether observation works is a property of the
    deployment — a bot that is only a member of a group receives commands,
    replies and mentions and nothing else — so it is measured at startup and
    logged, and a group where it does not hold gets a warning rather than a
    silent degradation.
    """
    for chat_id in groups.all_ids():
        status = "unknown"
        try:
            me = await app.bot.get_chat_member(chat_id, app.bot.id)
            raw = str(getattr(me, "status", "") or "")
            status = raw.split(".")[-1].lower() or "unknown"
        except TelegramError as e:
            log.warning("could not read my own status in %s: %s", chat_id, e)
        _nexus_visibility[int(chat_id)] = status
        if status in ("administrator", "creator"):
            log.info(
                "Nexus observation: chat=%s status=%s can_read_all=true",
                chat_id,
                status,
            )
        else:
            log.warning(
                "Nexus observation: chat=%s status=%s can_read_all=false — "
                "unaddressed administrator messages will NOT reach the bot. "
                "Promote the bot to administrator in that group to enable it.",
                chat_id,
                status,
            )


# Media the assistant will look at when somebody addresses it with an
# attachment. Deliberately the same set ``app/media.py`` can describe, minus
# nothing: a sticker the moderator can see is a sticker the assistant can see.
#
# Note what this filter does *not* do: it does not make the assistant answer
# unattended media. `on_group_chat` still requires `_addressed_to_bot`, so an
# ordinary photo in the group goes through moderation and nothing else.
def conversation_media_filter():
    return (
        filters.PHOTO
        | filters.VIDEO
        | filters.ANIMATION
        | filters.VIDEO_NOTE
        | filters.VOICE
        | filters.AUDIO
        | filters.Sticker.ALL
        | filters.Document.IMAGE
        | filters.Document.VIDEO
        | filters.Document.AUDIO
    )


def group_chat_filter():
    """Group text or media, for the assistant's handler."""
    return (
        (filters.TEXT | conversation_media_filter())
        & ~filters.COMMAND
        & filters.ChatType.GROUPS
        & ~filters.UpdateType.EDITED_MESSAGE
    )


def private_chat_filter():
    """Private text or media, for the assistant's handler."""
    return (
        (filters.TEXT | conversation_media_filter())
        & ~filters.COMMAND
        & filters.ChatType.PRIVATE
        & ~filters.UpdateType.EDITED_MESSAGE
    )


def key_entry_filter():
    """Private *plain text*, for the credential-entry handler.

    Narrower than `private_chat_filter` on purpose: media has no business here,
    an edited message would mean the credential had already been delivered once,
    and a command is a command even with a prompt armed — `/keys` re-opens the
    dashboard rather than being weighed as candidate key material. Today the
    store's shape check would reject a command anyway; the point is that "the
    owner's own commands are never read as a credential" should not depend on
    that check staying strict.
    """
    return (
        filters.TEXT
        & ~filters.COMMAND
        & filters.ChatType.PRIVATE
        & ~filters.UpdateType.EDITED_MESSAGE
    )


async def _send_chat(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    mention: tuple[int, str] | None = None,
) -> bool:
    """Send one conversational answer. Returns whether it went out.

    Escaped, because the body is model output and Telegram is asked to parse
    HTML: an unescaped angle bracket would be a parse error at best. The typing
    action is best-effort — it is a courtesy, and failing to show it must not
    cost the reply.

    An answer longer than one Telegram message is **split** into several, at
    natural seams, and each is sent in order. This is the other half of the
    length policy: ``chat`` no longer cuts a reply to a house length, and the
    per-message limit is Telegram's, so a request to explain fully or to bring
    the news arrives whole. Only the first message carries the reply target, the
    keyboard and the mention — a reply chain of four messages should not be four
    replies to the same person.

    The return value exists for the awareness window: what the assistant said
    out loud is part of the conversation the next pass has to understand, and a
    reply that failed to send must not appear there as though it had. It reports
    whether the answer *began* to go out; a failure on a later message is logged
    and does not turn a partially-delivered answer into silence.

    ``keyboard`` is appended last and defaults to None so the existing callers,
    which pass ``reply_to`` positionally, keep working untouched.

    ``mention`` is a server-controlled ``(user_id, name)`` for a person the
    message asked Nexus to *bring in* («فلانی رو تگ کن»). It is injected as
    Telegram's own ``tg://user`` anchor, prepended on its own line, so the person
    is actually notified. It is never read from the message text or from model
    output — the id comes from ``people.resolve`` through ``reply_target`` — and
    the name is HTML-escaped like any other value.
    """
    chunks = _split_for_telegram(text, config.GEMINI_CHAT_REPLY_CHARS)
    sent_any = False
    for index, chunk in enumerate(chunks):
        safe = html.escape(chunk)
        if index == 0 and mention and mention[0]:
            anchor = (
                f'<a href="tg://user?id={int(mention[0])}">'
                f"{html.escape(str(mention[1] or '?'))}</a>"
            )
            safe = f"{anchor}\n{safe}"
        try:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass
        try:
            await ctx.bot.send_message(
                chat_id,
                safe,
                parse_mode="HTML",
                reply_to_message_id=reply_to if index == 0 else None,
                reply_markup=keyboard if index == 0 else None,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            if index == 0:
                log.warning("chat reply failed: %s", exc)
                return False
            # The answer is already partly delivered; losing the tail is a
            # smaller fault than reporting the whole turn as unanswered.
            log.warning("chat reply part %d/%d failed: %s", index + 1, len(chunks), exc)
            return sent_any
        sent_any = True
    return sent_any


# Where a long answer is allowed to be cut, best first. A blank line is a real
# seam between thoughts; a single newline is a list item or a line of dialogue;
# sentence punctuation is the last place that still reads as a boundary. A space
# is the fallback, and a hard cut at the limit is the floor — a single word or a
# URL longer than the limit still has to go somewhere.
_SENTENCE_ENDS = ("\n", ".", "!", "?", "؟", "!", "؛", "،", ":", "…")


def _split_for_telegram(text: str, limit: int) -> list[str]:
    """Split one answer into messages Telegram will accept, at natural seams.

    Never raises and never invents text: every character of the input appears in
    exactly one output chunk, apart from the whitespace at the seams that a
    person would not keep either. A short answer comes back as a single chunk,
    which is what keeps the ordinary case a single message.
    """
    limit = max(1, int(limit))
    body = (text or "").strip()
    if not body:
        return [""]
    if len(body) <= limit:
        return [body]

    chunks: list[str] = []
    rest = body
    while len(rest) > limit:
        cut = _seam(rest, limit)
        head = rest[:cut].rstrip()
        if not head:  # a run of whitespace longer than the limit
            head = rest[:limit]
            cut = limit
        chunks.append(head)
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks or [body]


def _seam(text: str, limit: int) -> int:
    """The best cut index at or before ``limit``, or ``limit`` itself."""
    window = text[:limit]
    floor = limit // 2
    # A paragraph break is the strongest seam; keep both newlines out of the
    # next message by cutting after them.
    for needle, offset, threshold in (
        ("\n\n", 2, limit // 4),
        ("\n", 1, limit // 4),
    ):
        at = window.rfind(needle)
        if at >= threshold:
            return at + offset
    best = -1
    for mark in _SENTENCE_ENDS:
        at = window.rfind(mark)
        if at > best:
            best = at
    if best >= floor:
        return best + 1
    space = window.rfind(" ")
    if space >= floor:
        return space + 1
    return limit


async def _download_file(ctx: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    """Fetch one Telegram file into memory.

    In memory rather than to disk because everything that uses it is about to
    be sent straight to the API as inline bytes, and a file that is never
    written is a file that cannot be left behind. The bound is Telegram's own
    download ceiling and the media builder's size check, both applied before
    this is called.
    """
    f = await ctx.bot.get_file(file_id)
    return bytes(await f.download_as_bytearray())


# The reasons `_prepare_conversation_media` can come back with nothing to send.
# They are separate strings rather than a boolean because they are separate
# sentences to the person waiting: "I heard nothing in that" and "I could not
# open that" are not the same thing to say, and collapsing them is how a bot
# ends up telling somebody their voice note was unreadable when it was silent.
PREPARE_OK = ""
PREPARE_NO_SPEECH = "no_speech"
PREPARE_UNREADABLE = "unreadable"


async def _prepare_conversation_media(
    ctx: ContextTypes.DEFAULT_TYPE, msg, work_dir: str
) -> tuple[list | None, str, str, bool, str, bytes]:
    """Turn this message's attachment into (parts, kind, text, want_voice, why, audio).

    The outcomes, and they are genuinely different to the person waiting:

    * **Voice** is transcribed. The transcript becomes the turn's text, so the
      conversation carries on as if they had typed it — which is the whole point
      of the brief's "preserve the same conversational context as text
      messages". ``want_voice`` is set so the reply can come back in kind.
    * **Anything visual** is prepared by the shared media builder and sent as
      parts, so a sticker is read as a sticker rather than acknowledged as a
      MIME type.
    * **Anything else** — a format the API cannot take, a file too large, a
      download that failed — returns no parts and a reason, and the caller says
      which. Nothing here ever guesses what an unreadable attachment was.

    The sixth element is the attachment's own bytes when it is a voice note, and
    ``b""`` otherwise. It exists so that Voice Context can hear the person's
    actual voice without a second download: the bytes were fetched here to be
    transcribed, and the alternative — asking Telegram for the same file again
    on the voice path — would pay for one file twice. It is held in memory only,
    and this turn's caller deletes the work directory when it is done.

    Never raises.
    """
    ref = media.describe(msg)
    if ref is None:
        return None, "", _message_text(msg), False, PREPARE_OK, b""

    if ref.is_transcribable:
        # The download is captured on its way past, so the voice path gets the
        # same bytes the transcript was made from rather than fetching them
        # again. ``transcribe_ref`` still owns the bounds and the refusal, and
        # the holder is filled before it is asked.
        held: dict = {}

        async def _keep(fid: str) -> bytes:
            data = await _download_file(ctx, fid)
            held["data"] = data
            return data

        transcript = await transcribe.transcribe_ref(ref, download=_keep)
        audio = held.get("data", b"")
        if transcript.ok:
            return None, ref.kind, transcript.text, True, PREPARE_OK, audio
        if transcript.no_speech:
            return None, ref.kind, "", False, PREPARE_NO_SPEECH, audio
        log.info(
            "conversation: could not transcribe (%s)",
            transcript.error or transcript.skipped,
        )
        return None, ref.kind, "", False, PREPARE_UNREADABLE, audio

    if not ref.is_visual:
        # A document we can neither read nor transcribe. Not a failure of ours,
        # but there is nothing to send either.
        return None, ref.kind, _message_text(msg), False, PREPARE_UNREADABLE, b""

    try:
        bundle = await media.build(
            ref,
            download=lambda fid: _download_file(ctx, fid),
            work_dir=work_dir,
            max_parts=int(config.GEMINI_CHAT_MEDIA_MAX_PARTS),
        )
    except Exception as e:  # noqa: BLE001 - never break the handler
        log.warning("conversation media preparation failed: %s", e)
        return None, ref.kind, _message_text(msg), False, PREPARE_UNREADABLE, b""

    if not bundle.ok:
        log.info("conversation media unreadable kind=%s: %s", ref.kind, bundle.note)
        return None, ref.kind, _message_text(msg), False, PREPARE_UNREADABLE, b""

    parts = [{"mime_type": p.mime_type, "data": p.data} for p in bundle.parts]
    return parts, ref.kind, _message_text(msg), False, PREPARE_OK, b""


def _reply_context(msg) -> tuple[int, str, int]:
    """Who the current message is replying to, if anyone.

    Returned as plain ids and a display name rather than the message object, so
    nothing downstream can be tempted to read the replied-to *content* here. The
    assistant is told who was replied to; it is not handed the message, because
    the only thing an administrative request needs is the id. The reading itself
    lives in ``app/reply_target.py``, which is also the reader the conversational
    path uses when it *does* need the content — one reader of
    ``reply_to_message``, so the id an action carries and the content the model
    reads can never disagree about which message the parent is.
    """
    return reply_target.replied_identity(msg)


async def _admin_turn_core(
    *,
    actor_id: int,
    chat_id: int,
    chat_title: str = "",
    chat_type: str = "",
    message_id: int = 0,
    reply_user_id: int = 0,
    reply_name: str = "",
    reply_message_id: int = 0,
    bot_username: str = "",
    bot_id: int = 0,
    gateway,
    counters: dict | None = None,
    ambient: bool = False,
):
    """The tools, trusted context and tool-runner for one administrative turn.

    The single implementation behind both ways a turn reaches the model: the
    addressed conversation (``_ai_admin_turn``) and the awareness layer's read of
    the room (``_awareness_turn``). They are the same turn as far as authority is
    concerned — one actor id, one tool surface, one service that decides — and
    having two builders would mean two places for the trusted context to drift
    away from what the service authorises against.

    Returns ``(None, "", None)`` — the ordinary, tool-free conversation — in
    every case where administration is not on the table: the feature is switched
    off, the actor resolves to no permissions at all, or the declarations could
    not be built. Failing to the tool-free path is deliberate. An error while
    assembling the administrative half must degrade the assistant to what it was
    before this feature existed, never leave it in a state where it holds tools
    it was not supposed to have.

    Tool *exposure* is decided here. Tool *authority* is not: every call the
    model makes comes back through ``on_tool`` and is authorised again in
    ``app/admin_service.py`` against the same id. That asymmetry is the design —
    exposure is a courtesy that keeps the model from offering things it cannot
    do, and it is never the thing that stops it.

    ``counters`` is the caller's tally of what this turn actually did, and it
    has exactly one consumer: an unaddressed or ambient turn gets a visible
    reply only if a write tool was called. See ``_answer_conversationally`` and
    ``_awareness_pass``.
    """
    if not config.ADMIN_AI_ENABLED:
        return None, "", None

    try:
        principal = rbac.resolve(actor_id)
        if not principal.is_admin and not config.ADMIN_TOOL_GUEST_TOOLS:
            return None, "", None
        names = admin_tools.tool_names_for(principal)
        if not names:
            return None, "", None

        # The bot's own rights, so that "I do not have that permission" is a
        # fact the model read rather than one it assumed. Best-effort and
        # outside the try's failure contract below: a lookup that fails yields
        # no block, which degrades the context rather than removing the tools.
        try:
            rights = await _bot_rights(gateway.ctx, chat_id)
        except Exception:  # noqa: BLE001 - context, never worth the tool surface
            log.exception("could not read the bot's rights for the context block")
            rights = None

        context = admin_tools.build_context(
            principal=principal,
            chat_id=chat_id,
            chat_title=chat_title,
            chat_type=chat_type,
            message_id=message_id,
            reply_user_id=reply_user_id,
            reply_name=reply_name,
            reply_message_id=reply_message_id,
            bot_username=bot_username,
            bot_rights=rights,
            ambient=ambient,
        )
        tools = admin_tools.declarations_for(principal)
    except Exception:  # noqa: BLE001 - degrade to an ordinary conversation
        log.exception("could not build the administrative tool set")
        return None, "", None

    async def on_tool(name: str, args: dict) -> dict:
        """Run one tool call the model asked for. Authorisation is not here.

        This function's only job is to route: read tools answer from state, write
        tools become a typed request and go to the service, which decides. It
        deliberately contains no ``if actor is allowed`` branch — a check here
        would be a second authority model, and the whole point is that there is
        one.
        """
        spec = admin_tools.TOOLS.get(name)
        if spec is None:
            return {"error": f"unknown tool {name}"}

        if spec.kind == admin_tools.KIND_READ:
            return await admin_tools.run_read_tool(
                name,
                args,
                principal=principal,
                chat_id=chat_id,
                reply_user_id=reply_user_id,
                reply_name=reply_name,
                bot_id=bot_id,
                gateway=gateway,
            )

        if spec.kind == admin_tools.KIND_AGENT:
            # The bridge's two non-request calls. They are routed here rather
            # than through ``admin_service`` because they carry no target and no
            # role — there is no ``AdminRequest`` shape for "confirm the thing
            # waiting" — and ``app/agent_service.py`` re-derives the actor's
            # authority itself. The permission on the spec is what kept the tool
            # out of anybody else's hands; this is the second check.
            if not config.AGENT_ENABLED:
                return {"ok": False, "error": "the coding-agent bridge is switched off"}
            if not rbac.is_owner(principal.user_id):
                return {"ok": False, "error": "only the owner may do that"}
            if name == "confirm_agent_task":
                result = agent_service.confirm(
                    actor_id=principal.user_id,
                    chat_id=chat_id,
                    request_id=str((args or {}).get("request_id", "") or ""),
                    message_id=message_id,
                )
            elif name == "answer_agent_task":
                result = agent_service.resume(
                    actor_id=principal.user_id,
                    chat_id=chat_id,
                    request_id=str((args or {}).get("request_id", "") or ""),
                    text=str((args or {}).get("answer", "") or ""),
                )
            else:
                result = agent_service.cancel(
                    actor_id=principal.user_id,
                    request_id=str((args or {}).get("request_id", "") or ""),
                )
            log.info(
                "ai agent tool=%s actor=%s outcome=%s ok=%s",
                name, principal.user_id, result.outcome, result.ok,
            )
            return {
                "ok": result.ok,
                "operation": result.operation,
                "outcome": result.outcome,
                "message": result.message,
                "explanation": admin_service.explain(result),
                **({"task": result.extra["task"]} if result.extra.get("task") else {}),
                **(
                    {"candidates": result.extra["candidates"]}
                    if result.extra.get("candidates")
                    else {}
                ),
            }

        if not config.ADMIN_AI_ENABLED:
            return {"error": "AI administration is switched off"}

        request = admin_tools.parse_write_call(
            name,
            args,
            actor_id=principal.user_id,
            chat_id=chat_id,
            message_id=message_id,
            request_id=admin_service.new_request_id(),
        )
        if request is None:
            log.info("refused malformed tool call %s args=%s", name, sorted(args or {}))
            return {
                "ok": False,
                "error": "the request was malformed, so nothing was executed",
            }

        # Counted *before* the service runs, and counted whether or not it is
        # allowed. The question this answers is "did the model try to change
        # something", which is what decides whether an unaddressed message
        # deserves an answer — a refusal is an answer to a real instruction, and
        # swallowing it would leave the administrator believing the request was
        # never seen.
        if counters is not None:
            counters["writes"] = counters.get("writes", 0) + 1

        result = await admin_service.execute(
            request,
            gateway,
            actor=principal,
            bot_id=bot_id,
        )
        log.info(
            "ai admin tool=%s actor=%s outcome=%s ok=%s",
            name, principal.user_id, result.outcome, result.ok,
        )
        return {
            "ok": result.ok,
            "operation": result.operation,
            "outcome": result.outcome,
            "target_user_id": result.target_id,
            # Who it happened to, in the words an answer can use: the name, the
            # @username, and a handle that is always writable. Empty for an
            # operation whose subject is not a person. See
            # ``admin_tools.target_identity`` — the announcement must name the
            # target, and this is where the name comes from rather than from a
            # second lookup the model would have to remember to make.
            "target": admin_tools.target_identity(result, chat_id=chat_id),
            # The Persian sentence and the English gloss: the first is what to
            # convey, the second is why, and the model is expected to write its
            # own words around them rather than repeat either.
            "message": result.message,
            "explanation": admin_service.explain(result),
            # Only meaningful for the two state operations, and harmless
            # otherwise: it tells the model the state that now holds rather than
            # leaving it to assume the transition happened.
            "nexus_state": nexus.state(),
        }

    return tools, context, on_tool


async def _ai_admin_turn(update, ctx, msg, room, user, counters: dict | None = None):
    """The administrative half of one addressed conversational turn."""
    reply_user_id, reply_name, reply_message_id = _reply_context(msg)
    return await _admin_turn_core(
        actor_id=int(user.id),
        chat_id=room.id,
        chat_title=getattr(room, "title", "") or "",
        chat_type=str(getattr(room, "type", "") or ""),
        message_id=int(getattr(msg, "message_id", 0) or 0),
        reply_user_id=reply_user_id,
        reply_name=reply_name,
        reply_message_id=reply_message_id,
        bot_username=getattr(ctx.bot, "username", "") or "",
        bot_id=getattr(ctx.bot, "id", 0),
        gateway=TelegramGateway(ctx),
        counters=counters,
    )


async def _awareness_turn(
    ctx,
    chat_id: int,
    actor_id: int,
    *,
    speaker: dict | None = None,
    counters: dict | None = None,
):
    """The administrative half of one awareness pass.

    Same core, same authority, different framing: there is no single message
    being answered, so the trusted context says so and the model is told to find
    its target in the transcript by id. The tool surface is the anchor's — see
    ``awareness.anchor`` for why that is the safe attribution, and note that it
    is the *newest instruction* rather than the newest message.

    ``speaker`` is the anchor, and its reply edge is passed through as the real
    reply context. That is what turns «این رو سکوت کن» from a sentence with no
    referent into an instruction with a target: the same fields the addressed
    path has always had, filled in from the message the instruction was sent as
    a reply to.
    """
    return await _admin_turn_core(
        actor_id=int(actor_id),
        chat_id=int(chat_id),
        chat_title="",
        chat_type="",
        message_id=int((speaker or {}).get("message_id") or 0),
        reply_user_id=int((speaker or {}).get("reply_user_id") or 0),
        reply_name=str((speaker or {}).get("reply_name") or ""),
        reply_message_id=int((speaker or {}).get("reply_message_id") or 0),
        bot_username=getattr(ctx.bot, "username", "") or "",
        bot_id=getattr(ctx.bot, "id", 0),
        gateway=TelegramGateway(ctx),
        counters=counters,
        ambient=True,
    )


def _today_block(now: float) -> str:
    """The server's date, for the conversational system instruction.

    The room window and the web findings are full of dates other people and
    other pages wrote, and a model with no date of its own treats the most recent
    claim it read as today — which is how a live search came back dated a year
    early and how "امروز چندمه" was answered with a date from its training. The
    awareness layer has always stated this for its own pass
    (``awareness_context``); the conversation never did, so the same failure it
    was written to prevent was happening on the direct answer path.

    Stated as the server's own reading, in both calendars, and explicitly above
    any date in a message or a result. It goes in the trusted context, never the
    user turn, because the transcript is text people typed.
    """
    moment = persian_calendar.tehran_moment(int(now))
    return (
        "\nThe current date, from the server's own clock in Tehran — the "
        "server's reading and not anyone's claim. Trust it over any date in a "
        "message or a web result, and never state a date you remember.\n"
        f"- Gregorian: {persian_calendar.gregorian_text(moment)}\n"
        f"- Persian (Jalali): {persian_calendar.jalali_text(moment)}\n"
    )


def _room_reading(
    chat_id: int,
    *,
    message_id: int,
    messages: list[dict],
    skip: frozenset | None = None,
    budget: int = 0,
) -> str:
    """The server's reading of the message being answered, for the conversation.

    The awareness pass has always been handed this — ``_awareness_context``
    renders ``awareness_context.blocks`` for the anchor. The addressed path was
    handed the transcript and nothing else, so a message that *is* a reading
    problem («همون رو بن کن», «اینو محدود کن») reached the model as raw text with
    no resolution, while the same room read by a pass got the resolver's
    candidates, the act, the reply graph and the thread. This hands the
    conversation the same reading of *its own* message.

    It is derived from the window the caller already read — the anchor is the
    row for the message being answered, and the reading is pure Python over that
    window and the roles — so it adds no database query beyond the one the roles
    need, and no model call. ``skip`` defaults to ``CONVERSATION_SKIP``, which
    drops the blocks the conversation already carries (the date, the room) and
    the database-backed room memory it does not pay for; increment Y adds the
    two personal sources it renders itself. ``budget`` is the ceiling for this
    caller, defaulting to the pass-wide one.

    Returns ``""`` when the message is not in the window (nothing to read it
    against), when the layer is off, or on any failure: a reading is context,
    and context is never worth failing an answer over.
    """
    if not awareness.enabled() or not message_id:
        return ""
    anchor = next(
        (row for row in messages if int(row.get("message_id") or 0) == int(message_id)),
        None,
    )
    if anchor is None:
        return ""
    try:
        ctx = awareness_context.build_ctx(
            chat_id,
            messages=messages,
            anchor=anchor,
            nexus_id=int(_bot_identity.get("id") or 0),
        )
        return awareness_context.blocks(
            ctx,
            skip=(
                awareness_context.CONVERSATION_SKIP if skip is None else skip
            ),
            budget=budget,
        )
    except Exception:  # noqa: BLE001 - context is never worth a failed answer
        log.exception("could not render the room reading for an addressed reply")
        return ""


def _people_context(chat_id: int, text: str) -> str:
    """The room's name memory for the people this message mentions.

    The transcript already names whoever spoke in the window. This closes the
    other half of the brief's "knows everybody's name": a message about somebody
    who has not spoken recently still reaches their name, username and id, so
    Nexus can tell people apart and refer to them correctly.

    It is bounded twice (``NEXUS_PEOPLE_CONTEXT_ITEMS`` rows and
    ``NEXUS_PEOPLE_CONTEXT_CHARS`` characters) and filtered by the reader to the
    names the message actually contains, so it is never a member dump. The ids
    are Telegram's, read through ``people`` — the block is *data* about who is in
    the room, and it grants nothing: which message an answer is sent under is
    still decided by ``reply_target`` from the message and Telegram's metadata.

    A failure returns ``""``: a context block is never worth failing an answer
    over.
    """
    if not config.NEXUS_PEOPLE_ENABLED or not chat_id:
        return ""
    try:
        rows = people.roster(
            int(chat_id),
            text=str(text or ""),
            limit=int(config.NEXUS_PEOPLE_CONTEXT_ITEMS),
        )
        if not rows:
            return ""
        return _render_roster(rows, budget=int(config.NEXUS_PEOPLE_CONTEXT_CHARS))
    except Exception:  # noqa: BLE001 - context is never worth a failed answer
        log.exception("could not render the room's name memory")
        return ""


def _render_roster(rows: list[dict], *, budget: int) -> str:
    """The mentioned people as one bounded block, or ``""``.

    Each line carries what tells one person from another: the name as Telegram
    has it, the ``@username`` when there is one, and the numeric id. It is framed
    as the server's name memory rather than as the message's own content, so the
    model reads it as data about the room and not as something a person said.
    """
    cap = int(budget or config.NEXUS_PEOPLE_CONTEXT_CHARS)
    if cap <= 0:
        return ""
    header = "People in this room the message mentions (from Telegram, not guessed):\n"
    if len(header) >= cap:
        header = "Room names:\n"
    if len(header) >= cap:
        return ""
    lines: list[str] = []
    used = len(header)
    for row in rows or []:
        name = " ".join(str(row.get("name") or "").split()) or "?"
        username = str(row.get("username") or "").strip()
        user_id = int(row.get("user_id") or 0)
        if not user_id:
            continue
        handle = f" (@{username})" if username else ""
        line = f"- {name}{handle} — id {user_id}\n"
        if used + len(line) > cap:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return header + "".join(lines)


def _memory_context(chat_id: int, user_id: int, text: str) -> str:
    """The person's own long-term memory, as one bounded block.

    A source of its own, deliberately not reached through the awareness layer:
    the brief's requirement is that a chat answer survives awareness being off,
    unavailable or failed, and the fallback it falls back *to* includes this.
    Retrieval is relevance-first — the message being answered is the topic — so
    an unrelated memory is not shown, and the block is bounded twice (rows and
    characters).

    It is a read that grants nothing. A failure returns ``""``: memory is an
    enhancement, and a context block is never worth failing an answer over.
    """
    if not config.NEXUS_MEMORY_ENABLED or not chat_id or not user_id:
        return ""
    try:
        rows = memory.about(
            int(chat_id),
            int(user_id),
            limit=int(config.NEXUS_MEMORY_ITEMS),
            topic=str(text or ""),
        )
        if not rows:
            return ""
        return memory.render(rows, budget=int(config.NEXUS_MEMORY_CHARS))
    except Exception:  # noqa: BLE001 - context is never worth a failed answer
        log.exception("could not render the person's long-term memory")
        return ""


def _relationship_context(chat_id: int, user_id: int) -> str:
    """How this person has treated Nexus, as one bounded server-stated block.

    The behavioural half of the persona's rudeness rule. The persona may answer
    rudeness in kind only when there is a real history — «فحش … رو به کسی بگه که
    باهاش کانتکست بد داره» — and this is where that history comes from: the
    server's own count of messages *aimed at the assistant*, never the model's
    guess, never a username, never anything the speaker wrote. It grants
    nothing; every authority gate has already run by the time it is added, and
    it changes tone and nothing else.

    It is deliberately not part of the selector: the room reading chooses
    between retrieval sources, and this is a fact about the person being
    answered, like the owner note. It is bounded by ``NEXUS_RELATIONSHIP_CHARS``
    and an absent block is the warm default — the safe direction.

    A failure returns ``""``: a context block is never worth failing an answer
    over.
    """
    if not config.NEXUS_RELATIONSHIP_ENABLED or not chat_id or not user_id:
        return ""
    try:
        return memory.relationship(int(chat_id), int(user_id))
    except Exception:  # noqa: BLE001 - context is never worth a failed answer
        log.exception("could not render the person's relationship")
        return ""


def _state_context(chat_id: int, user_id: int, text: str) -> str:
    """What this interaction is trying to accomplish, as one bounded block.

    Increment X's fallback, and it sits beside ``_memory_context`` for the same
    reason: the four sources are independent, so State must survive awareness
    being off, unavailable or failed. It is a *different* layer from memory —
    the active task, not a durable fact about the person — and it is bounded
    twice (one row, and ``NEXUS_STATE_CHARS``).

    It is a read that grants nothing and it is relevance- and freshness-gated:
    a stale task, or one the message explicitly supersedes, renders nothing. A
    failure returns ``""`` — state is an enhancement, and a context block is
    never worth failing an answer over.
    """
    if not config.NEXUS_STATE_ENABLED or not chat_id or not user_id:
        return ""
    try:
        row = state.current(int(chat_id), int(user_id), text=str(text or ""))
        if not row:
            return ""
        return state.render(row, budget=int(config.NEXUS_STATE_CHARS))
    except Exception:  # noqa: BLE001 - context is never worth a failed answer
        log.exception("could not render the conversation state")
        return ""


def _ambiguous_target_text(target, window) -> str:
    """The question asked when a spoken name matches several people.

    Built from the server's own candidates — never from the model, which does not
    have them — so each line carries what tells them apart: the ``@username``
    when Telegram gave one, and whether they are the person who spoke last. The
    server refuses to pick between them (``people.resolve``'s rule) and asks
    instead, which is the behaviour the owner asked for by name.
    """
    last_speaker = 0
    for row in reversed(list(window or ())):
        try:
            user_id = int(row.get("user_id") or 0)
        except Exception:  # noqa: BLE001 - a malformed row is not a failure
            continue
        if user_id and str(row.get("role") or "") != awareness.ROLE_NEXUS:
            last_speaker = user_id
            break
    lines = []
    for candidate in target.ambiguous:
        username = f" (@{candidate.username})" if candidate.username else ""
        hint = (
            " — همین الان داشت حرف می‌زد"
            if candidate.user_id == last_speaker
            else ""
        )
        lines.append(
            config.NEXUS_TARGET_CANDIDATE_LINE.format(
                name=candidate.name or "?", username=username, hint=hint
            )
        )
    return config.NEXUS_TARGET_AMBIGUOUS_TEXT.format(
        name=target.ambiguous_query or "?", options="\n".join(lines)
    )


async def _answer_conversationally(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    reply_to: int | None = None,
) -> bool:
    """The whole conversational policy for a message that was **aimed** at Nexus.

    This path is now reached by exactly one thing: a message that addressed the
    assistant — by reply, by `@mention`, by a `BOT_ALIASES` word, or by one of
    `NEXUS_NAMES`. A message that merely *looked* like an instruction no longer
    comes here. It joins the room window and the awareness layer reads it
    (`_awareness_pass`), because deciding whether an unaddressed message
    concerns Nexus is a semantic question and the answer belongs to the model
    rather than to a keyword list.

    That split is what makes the two paths mean different things: an addressed
    message is a conversation and always gets an answer, and an unaddressed one
    is part of the room and gets a reply only when the model judges that one
    would help — or when an action actually ran.

    Text and media take the same road once the message has been prepared: the
    only difference is that an attachment contributes parts and, for voice, a
    transcript instead of a body.

    Returns whether a reply actually went out. The caller needs that answer for
    one reason: a group message that was aimed here must stop the awareness pass
    from answering the same thing again, but only if this path really did answer
    it. A withheld answer is not a duplicate, and suppressing the ambient reply
    for it would turn a rate limit into silence.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user or user.is_bot:
        return False

    # A message the moderator just deleted must not be answered. Without this
    # an explicit photo addressed to the bot would be removed and then replied
    # to, which is both confusing and a reference to content that is gone.
    if was_deleted(room.id, getattr(msg, "message_id", 0)):
        log.info("chat skipped: the message was just deleted by moderation")
        return False

    # The clock for this turn, in the same shape as ``awareness timing`` and for
    # the same reason: "it feels slow" is not a measurement, and every stage
    # here is a place the time could be going. Durations only — never the
    # message, never the answer.
    started = time.monotonic()
    prepare_done = started
    # The marks that decompose the model stage. Each is advanced at the end of
    # the work it names and left at ``started`` when a turn never reaches it, so
    # a stage that did not happen reads zero instead of borrowing another's time.
    #   ctx_done      — context acquisition: the room window/reading, state, memory
    #   search_done   — the live web lookup, when one ran
    #   assemble_done — context composition (the plan)
    #   gemini_done   — the whole model stage, including the pool's own walk
    ctx_done = started
    search_done = started
    assemble_done = started
    gemini_done = started
    # The network seam's share of the model stage, reported by ``chat.reply``.
    pool_ms = 0.0

    parts: list | None = None
    kind = ""
    text = _message_text(msg)
    want_voice = False
    problem = PREPARE_OK
    # The voice note's own bytes, when this message is one. They are captured by
    # the media preparation below so Voice Context can hear the person's voice
    # without a second download, and they are empty for everything else.
    voice_audio = b""

    def _timing(sent: bool) -> bool:
        """Log this turn's stages once, then hand back the caller's answer.

        The boundaries are the things that can actually be slow: getting the
        message ready (download, transcribe), acquiring the context, composing
        it, asking the model, and the send. A turn that never reaches the model
        reports a zero model stage rather than borrowing somebody else's
        duration, and the total makes whatever is left — this function's own
        bookkeeping — visible if it ever grows into a problem.

        ``gemini_ms`` keeps its original meaning — everything from the end of
        preparation to the end of the model call — so existing readers are
        unchanged; ``ctx_ms``/``search_ms``/``assemble_ms``/``model_ms``
        decompose it, and ``pool_ms``/``proc_ms`` split ``model_ms`` into the
        network seam and this function's own processing. ``pool_ms`` is the
        pool's own time (selection + provider + retry), reported by ``chat.reply``
        and zero when there was no model call to report.
        """
        now = time.monotonic()

        def stage(first: float, second: float) -> float:
            # A stage can never be negative. A mark that was never reached
            # leaves its stage at zero rather than subtracting from the next.
            return max(0.0, (second - first) * 1000.0)

        prepare_ms = stage(started, prepare_done)
        ctx_ms = stage(prepare_done, ctx_done)
        search_ms = stage(ctx_done, search_done)
        assemble_ms = stage(search_done, assemble_done)
        model_ms = stage(assemble_done, gemini_done)
        gemini_ms = stage(prepare_done, gemini_done)
        log.info(
            "chat timing user=%s chat=%s prepare_ms=%.0f ctx_ms=%.0f "
            "search_ms=%.0f assemble_ms=%.0f gemini_ms=%.0f model_ms=%.0f "
            "pool_ms=%.0f proc_ms=%.0f send_ms=%.0f total_ms=%.0f "
            "sent=%s kind=%s",
            user.id,
            room.id,
            prepare_ms,
            ctx_ms,
            search_ms,
            assemble_ms,
            gemini_ms,
            model_ms,
            pool_ms,
            max(0.0, model_ms - pool_ms),
            stage(gemini_done, now),
            stage(started, now),
            sent,
            kind or "text",
        )
        return sent

    if media.describe(msg) is not None:
        work_dir = tempfile.mkdtemp(
            prefix=f"chat_{room.id}_{getattr(msg, 'message_id', 0)}_",
            dir=config.TMP_DIR,
        )
        try:
            (
                parts,
                kind,
                text,
                want_voice,
                problem,
                voice_audio,
            ) = await _prepare_conversation_media(ctx, msg, work_dir)
        finally:
            # The bytes that matter are already in memory. Nothing on disk
            # outlives this turn, which is the same rule the moderation path
            # follows.
            shutil.rmtree(work_dir, ignore_errors=True)
        if parts is None and not text and problem:
            # Nothing readable, and nothing to say about it either. The two
            # reasons get different sentences: a silent clip is not an
            # unreadable file, and telling somebody their voice note could not
            # be opened when it was simply silent is a small lie that costs
            # them a second attempt. This path is only reached for a message
            # that was *aimed* at Nexus, so the honest sentence is always owed.
            await _send_chat(
                ctx,
                room.id,
                config.TRANSCRIBE_EMPTY_TEXT
                if problem == PREPARE_NO_SPEECH
                else config.GEMINI_CHAT_UNREADABLE_TEXT,
                reply_to,
            )
            # Something was said, so this turn is answered even though no model
            # was consulted. The person asked and got an honest sentence back.
            return _timing(True)
    prepare_done = time.monotonic()

    # The administrative half of this turn. Built from server-side values only,
    # and empty for a room where the person asking is not an administrator —
    # which is the normal case, and costs one dictionary lookup.
    #
    # No write-counter is passed: this turn was asked for something, so it is
    # answered either way. The counter exists for the ambient path, where a
    # reply has to be earned by an action having actually run.
    tools, context, on_tool = await _ai_admin_turn(update, ctx, msg, room, user)

    # ── Increment Y: the minimum relevant combination ─────────────────────
    #
    # The four sources are independent and are selected, not preloaded. The
    # message's own shape decides: a self-contained question pays for no room
    # context at all, while a reply, an anaphor, an instruction or a
    # continuation is evidence that the message depends on something outside
    # itself and gets the room. The decision is deterministic (no second model
    # call), it is a pure function of the message, and it never fails the turn:
    # every reader it consults is already fail-soft.
    #
    # The room goes in the system instruction, never the user turn, because the
    # transcript is full of text people typed and text people typed must not be
    # presented to the model as a statement the server is making.
    plan_reading = context_plan.read(
        text,
        kind=kind,
        reply=getattr(msg, "reply_to_message", None) is not None,
        media=parts is not None,
    )

    # The inbound relationship, read once from Telegram's own fields. This is
    # the only place the replied-to *content* is available — the update carries
    # the whole parent message — so it is read here, before any renderer can drop
    # it. It costs no query and no model: the reply id, its author and its words
    # are facts Telegram delivered, and the mention entities are the same.
    incoming = reply_target.read_incoming(msg)

    # The window is read **once** and handed to both the transcript and the
    # reading beside it. The reading is the server's understanding of the
    # message being answered — the same ``awareness_context.blocks`` a pass
    # renders — so an addressed «همون رو بن کن» is resolved by the resolver
    # rather than left to the model over raw text.
    #
    # It is also read when the target reader needs it and the reading did not ask
    # for it: a reply directive that names a person («میلاد رو جواب بده») has to
    # find that person's newest message, and that is a window lookup even on a
    # turn the selector judged self-contained.
    room_text = ""
    reading = ""
    room_window: list[dict] = []
    wants_room = plan_reading.wants_awareness and awareness.enabled()
    if wants_room or reply_target.needs_window(incoming, text):
        room_window = awareness.window(
            room.id, limit=max(1, int(config.NEXUS_AWARENESS_CONTEXT_MESSAGES))
        )
    if wants_room:
        reading = _room_reading(
            room.id,
            message_id=int(getattr(msg, "message_id", 0) or 0),
            messages=room_window,
            skip=awareness_context.CONVERSATION_SKIP | context_plan.reading_skip(),
            budget=int(config.NEXUS_AWARENESS_CONTEXT_CHARS),
        )
        # The window is bounded by what is left of the *selectable* budget once
        # the reading and the two personal blocks have their share. It is the
        # largest, least bounded source, so it is the one that yields. The
        # administrative roster is not part of that budget: it is never dropped,
        # so reserving for it would only shrink the room.
        room_text = awareness.room_block(
            room.id,
            limit=max(1, int(config.NEXUS_AWARENESS_CONTEXT_MESSAGES)),
            messages=room_window,
            budget=context_plan.room_budget(),
        )

    # What the user means, and which message the answer should quote. Two
    # readings of one message, kept apart on purpose: the semantic target is who
    # or what the message is about, and the destination is which message id
    # Telegram should attach the answer to. The default destination is the
    # message being answered (``reply_to``); it moves to the target only when the
    # message actually asks for that, and only when the target resolves to a
    # message the server already holds. The reader never invents an id, and no
    # model output reaches it — the ids here are Telegram's and the window's.
    target = reply_target.resolve(
        text=text,
        incoming=incoming,
        chat_id=room.id,
        window=room_window,
        current_message_id=int(getattr(msg, "message_id", 0) or 0),
        bot_id=int(getattr(ctx.bot, "id", 0) or 0),
        bot_username=str(getattr(ctx.bot, "username", "") or ""),
    )
    destination = target.destination(reply_to)
    log.info(
        "chat target user=%s chat=%s person=%s message=%s reply_to=%s explicit=%s "
        "ambiguous=%s no_reply=%s",
        user.id,
        room.id,
        target.person_id or "-",
        target.message_id or "-",
        destination or "-",
        target.explicit,
        len(target.ambiguous) or "-",
        target.no_reply,
    )

    # A spoken name matched more than one person in this room. The server refuses
    # to pick between them — that is ``people.resolve``'s rule and it is kept —
    # so it asks, with the candidates it already holds, instead of spending a
    # model call on a guess. The question is deterministic and costs nothing.
    if target.ambiguous:
        await _send_chat(
            ctx, room.id, _ambiguous_target_text(target, room_window), destination
        )
        return _timing(True)

    # The person a tag directive asked Nexus to bring in, when one resolved. The
    # id is the server's (``people.resolve`` through ``reply_target``), never the
    # model's, and the mention is injected by ``_send_chat`` as Telegram's own
    # anchor so the person is actually notified.
    mention = (
        (target.person_id, target.person_name)
        if target.wants_mention and target.person_id
        else None
    )

    # Long-term memory and conversational state are their own sources and never
    # a dependency of the awareness layer. They are rendered here — never inside
    # the reading, which is told to skip them — so the plan can gate them and
    # de-duplicate them against the room it selected. Each is a bounded read and
    # its failure is empty text, never an error.
    state_block = (
        _state_context(room.id, user.id, text) if plan_reading.wants_state else ""
    )
    memory_block = (
        _memory_context(room.id, user.id, text) if plan_reading.wants_memory else ""
    )
    # The room's name memory: who this message mentions, whether or not they have
    # spoken in the window. It is *not* gated on the room being wanted — a
    # self-contained message that names somebody still gets the name — and it is
    # its own slot rather than part of the room block, so a person's name in it
    # can never make the plan treat a memory line as a duplicate of the room.
    people_block = _people_context(room.id, text)
    # End of context acquisition: the room window/reading, the personal state
    # and the personal memory. Everything above this line reads the database;
    # nothing below it does until the answer is written.
    ctx_done = time.monotonic()

    # The live web, as its own workload, and only when it is actually wanted.
    # Three outcomes, not two: an explicit request or an explicitly *current*
    # question is searched; a question about a live subject that did not ask for
    # a lookup is **offered**, not performed, and the stored topic is what runs
    # the search when the person agrees. A knowledge question is answered from
    # the model's own knowledge and nothing is spent. When the switch is off the
    # whole block is skipped, so no request is made and no credit is spent.
    #
    # Only the question itself is sent — not the room window. The window is other
    # people's conversation, and handing it to a search provider would be a
    # disclosure this feature has no need to make.
    #
    # What comes back is *untrusted reference material*, appended to the same
    # system-instruction context the room block uses, or — when the search was
    # attempted and did not land — a server-authored note telling the model the
    # web could not be checked, so it says so instead of inventing a live fact.
    # A restraint (the search's own rate limit, or its allowance) adds nothing:
    # that is our choice to make and nobody needs to hear about it.
    #
    # The sources stay internal. Nothing here ever sends a link or a footer to
    # the group: the findings ground the reply, and the reply is the only thing
    # the person sees.
    finding = None
    answer_text = text
    search_block = ""
    if chat.is_enabled() and web_search.enabled():
        now = time.time()
        pending = web_search.pending_offer(room.id, user.id, now=now)
        if pending:
            # The bot asked about a topic on the previous turn and this message
            # is the answer. The stored topic is taken rather than re-guessed,
            # so one question spends at most one search.
            if web_search.is_affirmative(text) or web_search.should_search(
                text, kind=kind
            ).wanted:
                topic = web_search.take_offer(room.id, user.id, now=now) or pending
                finding = await web_search.research(topic, now=now)
                # The reply is an answer to the original question, so the model
                # is given that question rather than the bare «آره».
                answer_text = topic
            else:
                # A decline — or anything that is not an answer — clears the
                # offer and is handled normally below. The bot does not keep
                # asking.
                web_search.clear_offer(room.id, user.id)
        if finding is None:
            decision = web_search.should_search(text, kind=kind)
            if decision.wanted:
                finding = await web_search.research(text, now=now)
            elif decision.ask:
                web_search.offer(room.id, user.id, text, now=now)
                web_search.note_asked()
                log.info(
                    "search offer user=%s chat=%s reason=%s",
                    user.id,
                    room.id,
                    decision.reason,
                )
                await _send_chat(
                    ctx, room.id, config.NEXUS_SEARCH_CONFIRM_TEXT, destination
                )
                return _timing(True)
        search_block = ""
        if finding is not None:
            if finding.usable:
                search_block = web_search.untrusted_block(finding)
            elif finding.attempted:
                search_block = web_search.failure_block()

    # End of context acquisition proper — the web lookup is the only acquisition
    # step that can leave the process, so it is named separately from the
    # database reads above. Set unconditionally so the stages after it align
    # whether or not a search was wanted.
    search_done = time.monotonic()

    # The composition, in one deterministic order and under one hard ceiling.
    # The plan is an application-side decision object: it is logged (names,
    # reasons and sizes only — never a word of the message) and then discarded.
    plan = context_plan.compose(
        plan_reading,
        admin=context or "",
        target=reply_target.render(target),
        people=people_block,
        room=room_text,
        awareness=reading,
        state=state_block,
        memory=memory_block,
        date=_today_block(time.time()),
        search=search_block,
        message=text,
    )
    context = plan.text
    # How much answer was asked for, read deterministically from the message and
    # rendered as a server reading beside the date. It makes the persona's
    # "length follows the request, never a house rule" a fact for this turn — a
    # request to explain fully or to bring the news stops being answered with a
    # summary. It changes only how long the answer is; it can add no content and
    # remove none.
    shape = answer_shape.render(answer_shape.read(text))
    if shape:
        context = shape + context
    # The owner is recognised by the server, from the configured id, and the
    # note is prepended to the trusted context here. It is never inferred by the
    # model, and never from a username, a display name, a Telegram admin status
    # or anything the speaker wrote — `rbac.is_owner` compares the id and nothing
    # else. It is *data* (who is speaking), not a personality: how to talk to
    # somebody you know lives once in `chat.SYSTEM_INSTRUCTION`, and this grants
    # no capability. Every authority gate above this line has already run.
    if rbac.is_owner(user.id):
        context = chat.OWNER_NOTE + context
    # How this person has treated Nexus, counted by the server, prepended beside
    # the owner note for the same reason: it is a fact about the person being
    # answered, not a retrieval the selector chooses between. It is what makes
    # the persona's rudeness rule conditional on a real history instead of being
    # the state every ordinary message starts in — «فحش … رو به کسی بگه که باهاش
    # کانتکست بد داره». Absent when nothing has been promoted, which is the warm
    # default. It grants nothing and it changes tone and nothing else.
    relationship_block = _relationship_context(room.id, user.id)
    if relationship_block:
        context = relationship_block + context
    log.info("chat context user=%s chat=%s %s", user.id, room.id, plan.summary())
    # End of context assembly: the plan is built and the system instruction is
    # final. What follows is the model call.
    assemble_done = time.monotonic()

    # ── Voice Context ─────────────────────────────────────────────────────
    #
    # A voice note that reached this point has already been *understood*: its
    # bytes were downloaded and transcribed by ``_prepare_conversation_media``,
    # its sender is resolved, its reply edge and target are read, and the
    # context above is the same one a typed message would have been given —
    # memory, awareness, state, people, the date and any web finding. When the
    # layer is on, that context and the person's own voice are handed to one
    # spoken turn and the answer comes back as a voice message replying to
    # theirs. This decides only *how the answer is delivered*; nothing above it
    # changes, and nothing here re-decides identity, memory or authority.
    #
    # Three conditions, and each closes a real hole:
    #
    #   * it must be a voice note that was transcribed (``want_voice`` and the
    #     bytes), because that is the only input this path can carry;
    #   * there must be a transcript to ground the words, because the provider's
    #     own speech recognition mishears names — measured, «نکسوس» came back as
    #     «نیکسوس» — and the server's reading is what fixes the spelling;
    #   * and the layer must be switched on with a credential. Otherwise the
    #     message takes the exact path it took before this existed.
    #
    # A tag directive (``mention``) is not served by a voice reply — a voice
    # message cannot carry Telegram's mention anchor — so that one keeps the
    # text path, which is what actually notifies the person it asked for.
    #
    # The turn runs under the conversation's own lock, so a voice turn and a
    # text turn in the same room are ordered rather than racing. The lock is
    # released before the fallback below, because ``chat_queue.reply`` takes it
    # again and it is deliberately not reentrant.
    if (
        want_voice
        and voice_audio
        and text
        and not mention
        and voice_context.available()
    ):
        # What the model is told it heard. With the audio attached, that is the
        # server's transcript of what was actually spoken. Without it the turn
        # is text-only and the block itself is the message, so it carries the
        # question being answered — which is the stored topic when this voice
        # note was the «آره» that ran a search.
        heard_words = text if config.VOICE_CONTEXT_SEND_AUDIO else answer_text
        async with chat_queue.serialized(room.id, user.id):
            spoken_answer = await voice_context.answer(
                context=context,
                transcript=heard_words,
                audio=voice_audio,
                chat_id=room.id,
                user_id=user.id,
            )
        gemini_done = time.monotonic()
        pool_ms = 0.0
        # A turn that produced an answer delivers it here and never re-asks the
        # text model: the words already exist, and asking again would spend a
        # second request to answer the same message a second time. Only a turn
        # that produced *nothing* falls through to the text path below.
        if spoken_answer.ok and (spoken_answer.voice or spoken_answer.said):
            if spoken_answer.voice and await _send_voice(
                ctx, room.id, spoken_answer.voice, destination
            ):
                if spoken_answer.said:
                    # Nexus's own answer is part of the conversation the next
                    # awareness pass reads, exactly as a text reply is.
                    _awareness_note_reply(room.id, spoken_answer.said)
                log.info(
                    "chat voice context to %s in %s %s",
                    user.id,
                    room.id,
                    spoken_answer.describe(),
                )
                return _timing(True)
            # Telegram refused the upload, or the speech could not be packaged.
            # The words are the answer either way, so they go out as text rather
            # than being lost — and without a second model call, because the
            # answer is already in hand.
            log.warning("voice context could not send its voice; sending the words")
            if spoken_answer.said and await _send_chat(
                ctx, room.id, spoken_answer.said, destination, mention=mention
            ):
                _awareness_note_reply(room.id, spoken_answer.said)
                return _timing(True)
        else:
            log.info(
                "voice context produced no reply for %s in %s reason=%s; "
                "falling back to text",
                user.id,
                room.id,
                spoken_answer.reason or "no_voice",
            )

    # The turn goes through the queue rather than straight to the model. The
    # queue owns three things `chat.reply` deliberately does not: it serialises
    # one conversation's turns so two messages sent together cannot read the same
    # history and answer each other's context, it bounds how many calls run at
    # once, and it **waits** for our own rate window instead of dropping the turn
    # and telling the person off. A turn the window never frees is left silent —
    # `chat._MESSAGES` has no sentence for `rate_limit` any more. The provider's
    # rate limits stay the pool's business; the queue never sees one.
    result = await chat_queue.reply(
        room.id,
        user.id,
        answer_text,
        parts=parts,
        kind=kind,
        want_voice=want_voice,
        tools=tools,
        context=context,
        on_tool=on_tool,
    )
    gemini_done = time.monotonic()
    # The network seam's share of the model stage, when the turn reported it.
    pool_ms = float((getattr(result, "timing", None) or {}).get("pool_ms") or 0.0)

    if result:
        log.info(
            "chat reply to %s in %s turns=%d chars=%d truncated=%s repeated=%s "
            "voice=%s kind=%s",
            user.id,
            room.id,
            result.turns,
            len(result.text),
            result.truncated,
            result.repeated,
            bool(result.voice),
            kind or "text",
        )
        if result.voice:
            if await _send_voice(ctx, room.id, result.voice, destination):
                _awareness_note_reply(room.id, result.text)
                return _timing(True)
            # The upload failed; the text is still the answer and is sent below.
            log.warning("voice reply failed; falling back to text")
        if await _send_chat(ctx, room.id, result.text, destination, mention=mention):
            # What the assistant said is part of the conversation the next
            # awareness pass has to understand — otherwise it reads questions
            # and never its own answers, and repeats itself.
            _awareness_note_reply(room.id, result.text)
            return _timing(True)
        # Telegram refused the send. Nothing was said, so the room is still
        # unanswered and the ambient path is free to try.
        return _timing(False)

    log.info(
        "chat declined for %s in %s reason=%s",
        user.id,
        room.id,
        result.error or result.skipped,
    )
    # Silent for the reasons that are nobody's business — a switched-off feature
    # should not announce itself every time somebody says hello.
    if result.message:
        return _timing(bool(await _send_chat(ctx, room.id, result.message, destination)))
    return _timing(False)


async def _send_voice(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, ogg: bytes, reply_to: int | None
) -> bool:
    """Send a synthesised voice note. False if Telegram refused it.

    The caller falls back to text on False, so this never raises and never
    reports success it did not get.
    """
    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
    except TelegramError:
        pass
    try:
        await ctx.bot.send_voice(
            chat_id,
            voice=ogg,
            reply_to_message_id=reply_to,
        )
        return True
    except TelegramError as exc:
        log.warning("send_voice failed: %s", exc)
        return False


def _schedule_background(ctx, coro) -> None:
    """Run one fire-and-forget coroutine off the answer path, and never wait.

    The one place a background task is scheduled, shared by the memory and state
    observations so the two cannot drift: both are enhancements that must never
    add a millisecond to the reply somebody is waiting for, and both are written
    never to raise, so there is no result to await and no failure to report. A
    host whose context has no scheduler simply learns nothing this message, which
    is the same outcome as a message that held nothing to learn.
    """
    try:
        schedule = getattr(getattr(ctx, "application", None), "create_task", None)
        if schedule is None:
            schedule = asyncio.get_running_loop().create_task
        schedule(coro)
    except Exception:  # noqa: BLE001 - a scheduler is never worth a handler
        coro.close()
        log.exception("could not schedule a background observation")


def _schedule_memory_observation(
    ctx, user, chat_id: int, text: str, *, directed: bool = False
) -> None:
    """Learn from one message **off** the answer path, and never wait for it.

    Long-term memory is an enhancement, so it is deliberately not awaited.
    Everything the automatic path does — the deterministic rules, the behavioural
    counters, the bounded write, and the gated provider call an operator may
    enable — runs in a background task, so none of it can add a millisecond to
    the reply somebody is waiting for, and a slow or unavailable memory workload
    cannot delay chat at all.

    ``directed`` is carried so the relationship counter can tell a message aimed
    at Nexus from one aimed at somebody else; it is the flag the handler already
    computed, passed rather than derived a second time.
    """
    try:
        coro = memory.observe(user, chat_id, text, directed=bool(directed))
    except Exception:  # noqa: BLE001 - building the coroutine must not fail
        log.exception("could not prepare memory observation")
        return
    _schedule_background(ctx, coro)


def _schedule_state_observation(
    ctx, user, chat_id: int, text: str, message_id: int = 0
) -> None:
    """Read the interaction's state from one message, off the answer path.

    The same shape as the memory observation and for the same reason: State is
    an enhancement, so nothing about it is awaited by the handler. The write is a
    bounded, indexed upsert with no provider call at all, so this task is cheap —
    but it is still scheduled rather than awaited, because "the answer path never
    waits for a state write" is a property that should hold by construction and
    not by a measurement that happens to be small today. ``message_id`` is
    carried so a retried or duplicated message is a no-op rather than a second
    transition.
    """
    try:
        coro = state.observe(user, chat_id, text, message_id=message_id)
    except Exception:  # noqa: BLE001 - building the coroutine must not fail
        log.exception("could not prepare state observation")
        return
    _schedule_background(ctx, coro)


async def on_group_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The assistant, in a group. The Nexus gate, in order.

    Every step before the model is a lookup, and the order is the requirement
    rather than a preference: the room boundary, then identity, state, relevance,
    and only then an AI call. The boundary is the **room**: an unregistered group
    is refused at the first step and its messages never reach Gemini, while an
    ordinary member of a registered group is answered like anybody else — the
    speaker does not gate ordinary conversation. A member who is not talking to
    Nexus is left to the awareness layer and costs nothing.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user or user.is_bot:
        return
    # The group boundary, first and before anything else. A room without an
    # enabled row in the server-side allowlist is not served at all — no identity
    # write, no awareness capture, no model call. Being added to a group, or made
    # an administrator in it, does not register it; only an explicit
    # registration (the Owner, or a server-side administrative workflow) does.
    # This gate is about the *room*; once it passes, every member of the room is
    # eligible for ordinary conversation.
    if not authorized_group(room.id):
        return
    if was_deleted(room.id, getattr(msg, "message_id", 0)):
        return

    # Identity memory, before any gate. It is metadata about who speaks —
    # a name, a username, a timestamp — recorded for every member so that a
    # later "میلاد رو بن کن" has something to resolve. It grants nothing and it
    # stores no message.
    people.remember(user, room.id)

    # 1. Who, resolved from Telegram's own id and this bot's own tables.
    principal = rbac.resolve(user.id)
    text = _message_text(msg)

    # 1a. Is this aimed at Nexus? Computed once and used three times — by the
    #     capture, which records it as a hint for choosing the pass's anchor; by
    #     the memory observation, where it decides whether the message counts as
    #     evidence of how this person treats the assistant; and by the routing
    #     below, which answers it. Asking twice would be two chances for the two
    #     answers to differ, and it is a regex pass over the message. It sits
    #     above the observation for exactly that reason.
    directed = _nexus_directed(msg, ctx)

    # Long-term user memory, also before every gate and also free — and, unlike
    # everything above it, **off this thread**. The learning is scheduled as a
    # background task rather than awaited, so neither the regex pass, nor a
    # memory write, nor the gated provider call the automatic path may make can
    # add a millisecond to the reply somebody is waiting for. It grants nothing:
    # a memory is a sentence for the model to read, and authority stays in
    # ``rbac`` above. Scoped to the group path because that is where the
    # observation already happens; the key is ``(chat_id, user_id)``, so a
    # private chat's memory could never render here in any case. ``directed`` is
    # the flag above: only a message aimed at Nexus is evidence about Nexus.
    _schedule_memory_observation(ctx, user, room.id, text, directed=directed)

    # Conversational state (increment X), the same shape and the same reason: the
    # transition one message states about the interaction is learned in a
    # background task, so a state read or write can never add to the reply's
    # latency. It is a different layer from the memory above — the active task,
    # not a durable fact about the person — and it is keyed by
    # ``(chat_id, user_id)``, so one person's task is never another's and a
    # private task could never render here. The message id is carried so a
    # retried or duplicated delivery is idempotent. It grants nothing: authority
    # stays in ``rbac`` above, resolved from the Telegram id.
    _schedule_state_observation(
        ctx, user, room.id, text, getattr(msg, "message_id", 0)
    )

    # 1b. Awareness. Captured for **everybody**, before every gate, and at no
    #     AI cost: understanding the room is the feature, and a member whose
    #     message is understood is still a member who cannot act. The role label
    #     written here comes from `rbac.resolve` above and from nothing the
    #     sender wrote.
    if await _awareness_capture(
        ctx, room, user, msg, text, principal, directed=directed
    ):
        # The room now has something unread, so give it a deadline at the
        # debounce rather than leaving it to the sweeper's next tick. Coalesced
        # by assignment: each later message pushes the same deadline out, which
        # is exactly the debounce, and the sweeper remains the ceiling for a
        # room that never falls quiet.
        _awareness_schedule(room.id)

    # 2. The owner's spoken commands, before the model and before anything
    #    else. Voice Live is checked first so that its phrases are decided by
    #    their own vocabulary, and it stands down for anything that reads as the
    #    assistant's switch — see ``_owner_voice_command`` for why that ordering
    #    is the safe one. The switch is second because it is the one thing that
    #    must work when Nexus is already off: the model is not consulted in that
    #    state at all, so it is the only way back.
    if await _owner_voice_command(update, ctx, principal, text):
        return
    if await _owner_state_command(update, ctx, principal, text):
        return

    # 3. Authorized, and awake. The room was gated at the top, so what remains
    #    here is only "is the layer awake" — the speaker does not enter into it.
    #    Both refusals are silent: an ordinary member is not told they were
    #    ignored, and a switched-off assistant does not announce itself every
    #    time somebody speaks.
    if not nexus.accepts_in_group(room_authorized=True):
        return

    # 4. Aimed at Nexus, or left to the room. Only the first is understood
    #    here; everything else is the awareness layer's job.
    if not directed:
        # Watch without replying: the message joins this administrator's own
        # bounded context, and nothing is sent and nothing is spent.
        if nexus.is_actor(principal):
            _nexus_observe(room, user, msg, text)
        # `looks_actionable` is a **timing hint and nothing more** since Group
        # Awareness. It used to be the relevance gate — an unaddressed message
        # containing a moderation verb was sent to the model on the spot — and
        # that made a keyword list the thing that decided relevance, which is
        # exactly what the awareness layer exists to replace. Now it only says
        # "read this room now instead of waiting for it to go quiet", and the
        # model still makes the single semantic decision. It cannot make a
        # message relevant, and it cannot make one be acted on.
        if nexus.looks_actionable(text):
            _awareness_urgent.add(room.id)
            await _awareness_promptly(ctx, room.id)
        return

    # This message is being answered here, so it must not also be answered by the
    # awareness pass — that is the "one request, two replies" defect. The marker
    # is set before the answer is awaited, because a model call is a suspension
    # point and the pass can run during it; it is cleared if nothing went out, so
    # a refused or failed answer leaves the room readable rather than silent.
    _nexus_addressed[room.id] = max(
        _nexus_addressed.get(room.id, 0), int(getattr(msg, "message_id", 0) or 0)
    )
    if not await _answer_conversationally(update, ctx, reply_to=msg.message_id):
        _nexus_addressed.pop(room.id, None)


async def on_private_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A private message to the bot. The owner's own channel, and only theirs.

    The private boundary is a *different* gate from the group one, not a
    stricter setting of it. In a group the boundary is the *room*: once it is
    registered, every member is answered, because the room is already public and
    the speaker is not what decides it. A private chat has exactly one reader, so
    the only defensible rule is that it belongs to the owner.
    ``nexus.accepts_private`` is that rule, and it is deliberately not reachable
    by an administrator role, by the group room allowlist, or by anything written
    in the message.

    Two consequences, both intended and both stated here so they are not
    rediscovered as bugs:

    * an unauthorized sender is refused **silently**, before any model call and
      before any history is written. That matches the group policy for a
      non-actor — being ignored is not announced — and it means a stranger
      cannot spend this deployment's model allowance by talking to the bot in
      private;
    * because the refusal happens before ``chat.reply``, a non-owner's private
      message never enters ``chat_messages`` at all. There is therefore nothing
      for a later turn — theirs or anybody else's — to read.

    ``/start`` and ``/reset`` are registered separately and stay available to
    everyone: they touch only the caller's own ``(chat_id, user_id)`` history,
    which is the one thing a private chat can safely own.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user or user.is_bot:
        return

    # Identity memory is deliberately *not* recorded here. It exists to resolve
    # a name somebody said out loud *in a group*, and the brief scopes it to
    # people who appear in the group — recording a private conversation's
    # participants would grow the table with names that no group ever needed to
    # look up.
    principal = rbac.resolve(user.id)

    # The owner's spoken state command still works here, and it is checked
    # first: it is the only way back when Nexus is already off, and it reaches
    # the transition through ``admin_service``, which re-authorises it against
    # ``nexus.control``. It grants nothing by itself.
    if await _owner_state_command(update, ctx, principal, _message_text(msg)):
        return

    if not nexus.accepts_private(principal):
        # One line, and the reason is the point: "the owner's assistant stayed
        # silent" and "the bot is broken" must not look the same in a log.
        log.info(
            "private chat refused user=%s role=%s source=%s online=%s",
            user.id,
            principal.role,
            principal.source,
            nexus.is_online(),
        )
        return
    await _answer_conversationally(update, ctx)


async def on_transcribe_command(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """The transcription-only interface: the words, and nothing else.

    This exists so the speech pipeline can be used and verified on its own,
    without the assistant being involved — the brief asks for a dedicated
    voice-to-text interface separate from the conversational one, and this is
    it. It never replies conversationally and never reaches acquisition.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    # The transcription interface is AI functionality, so in a group it stands
    # behind the same group boundary as the conversation: an unregistered room
    # is not served. A private chat is the owner's own and is unaffected.
    if getattr(room, "type", "") != "private" and not authorized_group(room.id):
        return
    ref = media.describe(msg)
    if ref is None or not ref.is_transcribable:
        await _send_chat(ctx, room.id, config.TRANSCRIBE_NEED_AUDIO_TEXT, msg.message_id)
        return
    if not transcribe.is_enabled():
        await _send_chat(
            ctx, room.id, config.TRANSCRIBE_UNAVAILABLE_TEXT, msg.message_id
        )
        return
    result = await transcribe.transcribe_ref(
        ref, download=lambda fid: _download_file(ctx, fid)
    )
    if result.ok:
        # Escaped by _send_chat, like every other model output.
        await _send_chat(ctx, room.id, result.text, msg.message_id)
    elif result.no_speech:
        await _send_chat(ctx, room.id, config.TRANSCRIBE_EMPTY_TEXT, msg.message_id)
    else:
        await _send_chat(
            ctx, room.id, config.TRANSCRIBE_UNAVAILABLE_TEXT, msg.message_id
        )


async def on_chat_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset — forget this conversation.

    The user-facing way to clear the bounded history. Scoped to (chat, user), so
    it can only ever clear the caller's own conversation, never anybody else's.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user:
        return

    removed = db.chat_clear(room.id, user.id)
    log.info("chat reset for %s in %s removed=%d", user.id, room.id, removed)

    private = getattr(room, "type", "") == "private"
    await _send_chat(
        ctx,
        room.id,
        config.GEMINI_CHAT_RESET_TEXT,
        None if private else msg.message_id,
    )


async def on_chat_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start in a private chat — Telegram's Start button, answered.

    Without this a person who presses Start gets silence, because /start is a
    command and the conversational handler deliberately ignores commands. Private
    chats only: in a group, /start is noise and answering it would be the bot
    talking to the room without being addressed.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user:
        return
    if getattr(room, "type", "") != "private":
        return
    # A plain first name rather than `mention()`: `_send_chat` escapes what it
    # sends, so an HTML mention would arrive as visible markup.
    name = (user.first_name or "").strip() or "دوست عزیز"
    await _send_chat(
        ctx,
        room.id,
        config.GEMINI_CHAT_START_TEXT.format(name=name),
        keyboard=_owner_menu_keyboard(_actor(update)),
    )


# ------------------------------------------------- administration
# The application-level administration surface: who may do what, and on whom.
#
# Every handler here follows the same three steps, in this order, and the order
# is the security property:
#
#   1. resolve the actor with rbac.resolve() — from the owner id in the
#      environment, then the configured admins, then the database;
#   2. ask rbac whether the action is allowed, passing the target's principal
#      when there is one;
#   3. only then do anything, and audit whatever happened either way.
#
# Nothing here trusts a display name, a username, a message body or a callback
# payload. Callback data in particular is fully attacker-controlled — a client
# can send any bytes it likes — so the promote dialog re-authorises from scratch
# on every press and treats its own payload as a *suggestion* of what to show,
# never as a grant.
ADMIN_CALLBACK_PREFIX = "adm:"
_ROLE_CODES = {
    "h": rbac.ROLE_HELPER,
    "m": rbac.ROLE_MODERATOR,
    "a": rbac.ROLE_ADMIN,
    "s": rbac.ROLE_SENIOR_ADMIN,
}
_CODE_ROLES = {role: code for code, role in _ROLE_CODES.items()}
# A stable, ordered list for the bitmask in the callback payload. The order is
# the vocabulary order in rbac and must not be reordered casually: it is the
# wire format of a dialog that may be open across a deploy.
_MASK_PERMISSIONS = tuple(rbac.PERMISSIONS)

_ROLE_ALIASES = {
    "helper": rbac.ROLE_HELPER,
    "moderator": rbac.ROLE_MODERATOR,
    "admin": rbac.ROLE_ADMIN,
    "senior": rbac.ROLE_SENIOR_ADMIN,
    "senior_admin": rbac.ROLE_SENIOR_ADMIN,
}


def _mask(permissions) -> int:
    chosen = set(permissions or ())
    value = 0
    for index, permission in enumerate(_MASK_PERMISSIONS):
        if permission in chosen:
            value |= 1 << index
    return value


def _unmask(value: int) -> frozenset[str]:
    """Decode a bitmask into permissions, dropping anything out of range.

    A mask is attacker-supplied, so an out-of-range bit is discarded rather than
    indexed — and the result is still only ever fed to ``authorize_grant``,
    which checks it against the role bundle and the actor's own authority. This
    function decodes; it does not authorise.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(
        permission
        for index, permission in enumerate(_MASK_PERMISSIONS)
        if value & (1 << index)
    )


def _actor(update: Update) -> rbac.Principal:
    user = update.effective_user
    return rbac.resolve(getattr(user, "id", 0) or 0)


def _target_from(update: Update, ctx, args) -> tuple[int, str] | None:
    """Who the command is about: a replied-to user, or a numeric id.

    A reply is the primary mechanism because it is unambiguous — the person the
    operator pointed at. A bare id is accepted as a fallback for a user who is
    not in the room, and only if it is actually a number: a name is never
    resolved, because resolving names is how an impersonator gets promoted.
    """
    msg = update.effective_message
    replied = getattr(msg, "reply_to_message", None)
    if replied is not None:
        author = getattr(replied, "from_user", None)
        if author is not None:
            return int(author.id), (getattr(author, "full_name", "") or str(author.id))
    for arg in args or ():
        candidate = str(arg).lstrip("+")
        if candidate.lstrip("-").isdigit():
            return int(candidate), candidate
    return None


def _audit(
    actor_id: int,
    action: str,
    outcome: str,
    *,
    target_id: int | None = None,
    chat_id: int | None = None,
    detail: str = "",
) -> None:
    """Write one audit row for the command path. Never raises into the handler.

    An audit row that cannot be written must not be the reason a moderation
    action fails, and it must not be the reason one *succeeds* either — so this
    logs the failure and returns.

    Everything written here came through a typed command, so the interface is
    ``python`` by construction. The assistant's requests are audited by
    ``app/admin_service.py`` instead, which stamps ``ai``. Between the two, every
    administrative row says which front door it came through.

    The role is resolved here for the same reason the service resolves it: the
    record has to say what authority the action was taken with, and it must not
    be the actor's own claim. There is no request id — a typed command is not a
    request from the assistant and has none — so the column stays empty rather
    than being filled with something that only looks like an identifier.
    """
    try:
        db.audit_write(
            actor_id,
            action,
            outcome=outcome,
            target_id=target_id,
            chat_id=chat_id,
            detail=detail,
            interface=admin_service.INTERFACE_PYTHON,
            role=rbac.resolve(actor_id).role,
        )
    except Exception:  # noqa: BLE001
        log.exception("audit write failed action=%s outcome=%s", action, outcome)


def _deny_text(decision: rbac.Decision) -> str:
    """The Persian sentence for a refusal. One key, one sentence."""
    return {
        rbac.REASON_NO_OWNER: config.ADMIN_NOT_CONFIGURED_TEXT,
        rbac.REASON_OWNER_PROTECTED: config.ADMIN_OWNER_PROTECTED_TEXT,
        rbac.REASON_HIGHER_RANK: config.ADMIN_HIGHER_RANK_TEXT,
        rbac.REASON_SELF_TARGET: config.ADMIN_SELF_TARGET_TEXT,
    }.get(decision.reason, config.ADMIN_DENIED_TEXT)


async def _bot_right(ctx, chat_id: int, right: str) -> bool:
    """Whether the bot itself holds a Telegram administrator right in a chat.

    Checked before attempting an operation the API will refuse, so a refusal can
    be reported as "I do not have the permission here" rather than as a generic
    failure. Telegram still enforces it; this only makes the message useful.

    A **negative** answer is never served from the cache. The cache exists so
    that the common case — the bot has the right, as it does in a group it
    administers — costs no API call, and so that the trusted context can state
    the bot's capabilities without a round trip per turn. A cached "no" would be
    the opposite trade: it would make the bot refuse something it can do for as
    long as the entry lived, which is precisely the defect the owner reported as
    «می‌گه تلگرام اجازه نداده» and then doing it after a fresh look.
    """
    rights = (await _bot_rights(ctx, chat_id)).get("rights") or {}
    if rights.get(right):
        return True
    fresh = (await _bot_rights(ctx, chat_id, fresh=True)).get("rights") or {}
    return bool(fresh.get(right))


# The administrator flags this bot can hold, by the names the Bot API uses in
# ``ChatMemberAdministrator``. Nothing here is invented: a name that is not a
# real flag would always read False and would turn into a false "the bot cannot
# do that" in the context block, which is the failure this exists to prevent.
BOT_RIGHT_FIELDS = (
    "can_manage_chat",
    "can_delete_messages",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_video_chats",
    "can_post_messages",
    "can_edit_messages",
    "can_delete_stories",
    "is_anonymous",
)

# The bot's own rights in a chat, with a short TTL. See ``_bot_right`` for why a
# negative is never served from here, and ``_bot_rights_block`` in
# ``app/admin_tools.py`` for the other consumer: the model is told what the bot
# can actually do here, so that "I do not have that permission" is a fact it read
# rather than a fact it assumed.
_bot_rights_cache: dict[int, tuple[float, dict]] = {}


async def _bot_rights(ctx, chat_id: int, *, fresh: bool = False) -> dict:
    """What the bot may do in this chat, from Telegram's own record.

    ``{"status": ..., "rights": {...}, "error": ...}``, and never raises. A
    lookup that fails answers with an empty right set and an ``error``, which the
    context block renders as "unknown" rather than as "no" — an unknown must not
    read as a refusal, because the whole point is to stop the assistant
    inventing one.
    """
    chat_id = int(chat_id or 0)
    if not chat_id:
        return {"status": "", "rights": {}, "error": "no chat id"}
    now = time.monotonic()
    ttl = max(0.0, float(config.BOT_RIGHTS_TTL_SECONDS))
    cached = _bot_rights_cache.get(chat_id)
    if not fresh and cached and ttl and (now - cached[0]) < ttl:
        return cached[1]
    try:
        member = await ctx.bot.get_chat_member(chat_id, ctx.bot.id)
    except TelegramError as e:
        log.warning("could not read my own chat member status: %s", e)
        # Deliberately not cached: a transient Telegram failure must not become a
        # minute of the bot believing it has no permissions.
        return {"status": "", "rights": {}, "error": "telegram lookup failed"}
    status = str(getattr(member, "status", "") or "")
    answer = {
        "status": status,
        "rights": {
            field: bool(getattr(member, field, False)) for field in BOT_RIGHT_FIELDS
        },
        "error": "",
    }
    _bot_rights_cache[chat_id] = (now, answer)
    return answer


class TelegramGateway:
    """The only object in this bot that performs Telegram administration.

    ``app/admin_service.py`` knows the ten operations below and nothing else. It
    never imports ``telegram``, never sees a ``Context``, and cannot call a
    method that is not on this class — which means the complete set of Telegram
    side effects reachable from *either* interface, the AI's tools or the
    operator's commands, is these ten methods. That is what "do not create a
    second Telegram action engine" means in practice: there is nowhere else for
    one to live.

    The two permission objects are defined here rather than in the service
    because they are Telegram's types, not the application's. The service says
    "mute this person"; what a mute *is* in the Bot API is this file's business.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.bot = ctx.bot

    async def bot_right(self, chat_id: int, right: str) -> bool:
        return await _bot_right(self.ctx, chat_id, right)

    async def bot_rights(self, chat_id: int) -> dict:
        """Every right this bot holds here, for the read tool and the context."""
        return await _bot_rights(self.ctx, chat_id)

    async def promote(self, chat_id: int, user_id: int, rights: dict) -> None:
        await self.bot.promote_chat_member(chat_id, user_id, **rights)

    async def demote(self, chat_id: int, user_id: int) -> None:
        """Strip every right, not a selection.

        The bot does not know which rights were there before it touched the
        account, and guessing would be a way to leave somebody holding a
        capability nobody meant to leave them.
        """
        await self.bot.promote_chat_member(
            chat_id, user_id, **{right: False for right in rbac.TELEGRAM_RIGHTS}
        )

    async def mute(self, chat_id: int, user_id: int) -> None:
        until = datetime.now(timezone.utc) + timedelta(
            minutes=max(1, int(config.MUTE_MINUTES))
        )
        await self.bot.restrict_chat_member(
            chat_id, user_id, permissions=MUTED, until_date=until
        )

    async def unmute(self, chat_id: int, user_id: int) -> None:
        await self.bot.restrict_chat_member(chat_id, user_id, permissions=FULL)

    async def ban(self, chat_id: int, user_id: int) -> None:
        await self.bot.ban_chat_member(chat_id, user_id)

    async def unban(self, chat_id: int, user_id: int) -> None:
        await self.bot.unban_chat_member(chat_id, user_id)

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self.bot.delete_message(chat_id, message_id)

    async def warn(self, chat_id: int, user_id: int, reason: str) -> None:
        """Say the warning in the group, addressed to the person warned.

        This is the one operation whose effect is a message rather than a
        permission change, and it is the only place ``_send_user_notice`` is
        reached from the service. It deliberately takes no ``ctx`` of its own —
        the gateway already holds one.
        """
        await _send_user_notice(
            self.ctx,
            chat_id,
            config.MOD_WARN_USER_TEXT.format(
                name=f"<a href=\"tg://user?id={int(user_id)}\">کاربر</a>",
                reason=html.escape(reason or "لطفاً قوانین گروه رو رعایت کن."),
            ),
        )

    async def member(self, chat_id: int, user_id: int) -> dict:
        """The target's live Telegram status, narrowed to a safe dict.

        Read-only, and narrowed on purpose: the model is told what it needs in
        order to explain a situation, not handed the whole ``ChatMember``
        object, which carries fields it has no use for and should not be
        encouraged to reason about.

        ``is_muted`` exists because ``telegram_status`` alone was ambiguous and
        the ambiguity had a cost. Telegram keeps a member in ``restricted``
        status for as long as *any* per-member permission is denied — including
        ones an ordinary person never notices, like ``can_pin_messages``. A model
        told only "restricted" concludes the person is still silenced, and this
        one did: it reported them as restricted and called ``unmute_member``
        again, twice, for a member who could already talk. ``is_muted`` answers
        the question the tool actually advertises — can this person speak —
        instead of leaving it to be inferred from a permission field.
        """
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except TelegramError as e:
            log.info("get_chat_member failed for %s: %s", user_id, e)
            return {"error": "not a member of this chat, or not readable"}

        status = getattr(member, "status", "")
        name = str(status).split(".")[-1].lower() if status else ""
        rights = {
            right: bool(getattr(member, right, False))
            for right in rbac.TELEGRAM_RIGHTS
            if hasattr(member, right)
        }
        # A member with no `can_send_messages` attribute is not restricted at
        # all; one whose attribute is False is silenced. `kicked` has no such
        # attribute and cannot speak either, which is why it is named here.
        can_send = getattr(
            member, "can_send_messages", name not in ("restricted", "kicked")
        )
        return {
            "user_id": int(user_id),
            "telegram_status": name or str(status),
            "is_telegram_admin": name in ("administrator", "creator"),
            "custom_title": getattr(member, "custom_title", "") or "",
            "is_member": bool(getattr(member, "is_member", name != "left")),
            "can_send_messages": can_send,
            "is_muted": (not can_send) if name in ("restricted", "kicked") else False,
            "telegram_rights": sorted(k for k, v in rights.items() if v),
        }


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """What this bot thinks you are. The answer to "why was I refused?"."""
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    if not rbac.has_owner():
        await _reply_in_group(ctx, room.id, config.ADMIN_NOT_CONFIGURED_TEXT,
                              reply_to=msg.message_id)
        return
    actor = _actor(update)
    perms = "، ".join(rbac.permission_labels(actor.permissions)) or "—"
    await _reply_in_group(
        ctx,
        room.id,
        config.ADMIN_WHOAMI_TEXT.format(
            user_id=actor.user_id, role=actor.label, perms=perms
        ),
        keyboard=_owner_menu_keyboard(actor),
        reply_to=msg.message_id,
    )


async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List the application administrators."""
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    decision = rbac.authorize(actor, "moderation.review")
    if not decision:
        _audit(actor.user_id, "admin.list", decision.reason, chat_id=room.id)
        await _reply_in_group(ctx, room.id, _deny_text(decision),
                              reply_to=msg.message_id)
        return
    lines = [config.ADMIN_LIST_TITLE]
    if rbac.has_owner():
        lines.append(
            config.ADMIN_LIST_LINE.format(
                name=f"<code>{rbac.owner_id()}</code>",
                role=rbac.ROLE_LABELS[rbac.ROLE_OWNER],
            )
        )
    for row in db.admin_list():
        lines.append(
            config.ADMIN_LIST_LINE.format(
                name=f"<code>{row['user_id']}</code>",
                role=rbac.ROLE_LABELS.get(row["role"], row["role"]),
            )
        )
    if len(lines) == 1:
        lines.append(config.ADMIN_LIST_EMPTY)
    _audit(actor.user_id, "admin.list", "ok", chat_id=room.id)
    await _reply_in_group(ctx, room.id, "\n".join(lines), reply_to=msg.message_id)


async def cmd_nexus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/nexus [on|off|status]` — the typed interface to the layer's own state.

    The deterministic counterpart to the spoken command, and the one that always
    works: no model, no key, no allowance. It exists for the same reason the
    moderation commands do — an AI feature that can only be controlled by the AI
    is a feature that can strand itself.

    Reading the state needs only the floor permission, so any administrator can
    ask what is going on. Changing it needs ``nexus.control``, which is held by
    the owner alone, and the change goes through ``app/admin_service.py`` where
    that is decided rather than here.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    arg = (ctx.args[0].lower() if ctx.args else "").strip()

    if arg in ("on", "off"):
        operation = "nexus_online" if arg == "on" else "nexus_offline"
        result = await admin_service.execute(
            _nexus_state_request(operation, actor, room.id),
            TelegramGateway(ctx),
            actor=actor,
            bot_id=getattr(ctx.bot, "id", 0),
        )
        if not result.ok:
            log.info(
                "nexus command refused actor=%s arg=%s outcome=%s reason=%s",
                actor.user_id, arg, result.outcome, result.reason,
            )
            await _reply_in_group(
                ctx, room.id, _refusal_text(result), reply_to=msg.message_id
            )
            return
        await _reply_in_group(
            ctx,
            room.id,
            config.NEXUS_ONLINE_DONE_TEXT
            if arg == "on"
            else config.NEXUS_OFFLINE_DONE_TEXT,
            reply_to=msg.message_id,
        )
        return

    if arg and arg != "status":
        await _reply_in_group(
            ctx, room.id, config.NEXUS_STATUS_HINT, reply_to=msg.message_id
        )
        return

    decision = rbac.authorize(actor, "moderation.review")
    if not decision:
        _audit(actor.user_id, "nexus.status", decision.reason, chat_id=room.id)
        await _reply_in_group(ctx, room.id, _deny_text(decision),
                              reply_to=msg.message_id)
        return
    await _reply_in_group(
        ctx,
        room.id,
        f"{config.NEXUS_STATUS_TITLE}\n{_nexus_status_text(chat_id=room.id)}",
        reply_to=msg.message_id,
    )


async def cmd_register_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/registergroup` — authorize the room this command was typed in.

    The typed interface to the room allowlist, and it is deliberately the only
    interface: registering a group is a grant of access, so it needs a person
    acting directly rather than a model proposing it, and a typed command is not
    gated because a person typing is already the authority. Authority is decided
    in ``app/admin_service.py`` (``config.manage`` — the owner and senior
    admins), and the subject is always the current room.

    It works inside a group that is not yet registered, which is the entire
    point: the command handlers are not room-gated, so this is how a new room
    becomes one.
    """
    await _group_command(update, ctx, "register_group")


async def cmd_unregister_group(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/unregistergroup` — soft-revoke the room this command was typed in.

    The row is kept and only disabled, so the room is fail-closed from the next
    message on and can be registered again without losing the record of who
    turned it off.
    """
    await _group_command(update, ctx, "unregister_group")


async def _group_command(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, operation: str
) -> None:
    """The shared body of the two allowlist commands."""
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    if getattr(room, "type", "") not in ("group", "supergroup"):
        await _reply_in_group(
            ctx, room.id, config.GROUP_REGISTER_NOT_A_GROUP_TEXT, reply_to=msg.message_id
        )
        return
    actor = _actor(update)
    result = await admin_service.execute(
        _group_request(operation, actor, room.id),
        TelegramGateway(ctx),
        actor=actor,
        bot_id=getattr(ctx.bot, "id", 0),
    )
    if not result.ok:
        log.info(
            "group command refused actor=%s op=%s outcome=%s reason=%s",
            actor.user_id, operation, result.outcome, result.reason,
        )
        await _reply_in_group(
            ctx, room.id, _refusal_text(result), reply_to=msg.message_id
        )
        return
    await _reply_in_group(
        ctx,
        room.id,
        config.GROUP_REGISTER_DONE_TEXT
        if operation == "register_group"
        else config.GROUP_REVOKE_DONE_TEXT,
        reply_to=msg.message_id,
    )


async def cmd_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/groups` — list the registered rooms and their state.

    Read-only, and gated on ``config.manage`` like the writes: which rooms the
    deployment serves is operational detail, not something a stranger needs the
    bot to recite.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    decision = rbac.authorize(actor, "config.manage")
    if not decision:
        _audit(actor.user_id, "group.list", decision.reason, chat_id=room.id)
        await _reply_in_group(ctx, room.id, _deny_text(decision), reply_to=msg.message_id)
        return
    rows = groups.list_rows()
    if not rows:
        await _reply_in_group(ctx, room.id, config.GROUP_LIST_EMPTY, reply_to=msg.message_id)
        return
    lines = [config.GROUP_LIST_TITLE]
    for row in rows:
        lines.append(
            config.GROUP_LIST_LINE.format(
                chat_id=row["chat_id"],
                state=(
                    config.GROUP_STATUS_ENABLED_LABEL
                    if row["enabled"]
                    else config.GROUP_STATUS_DISABLED_LABEL
                ),
                added_by=row["added_by"] or "-",
            )
        )
    await _reply_in_group(
        ctx, room.id, "\n".join(lines), reply_to=msg.message_id
    )


async def cmd_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/agent [status|confirm <id>|cancel <id>]` — the typed bridge interface.

    Owner-only, and for the same reason the tool is: the thing on the other end
    is a process with a shell and a git remote. It exists as a command for the
    same reason ``/nexus`` does — a feature that can only be driven through the
    assistant is a feature that strands itself when the assistant is the thing
    that is broken, and "did my request go anywhere" is exactly the question
    somebody asks when they suspect it is.

    The confirmation path is the important one. The brief's rule is that vague
    language is approval only with a specific pending dangerous operation, and
    this command is the deterministic version of that: ``/agent confirm <id>``
    names the task, and the server still refuses it if the caller is not the
    owner or nothing is waiting.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    if not actor.is_owner:
        _audit(actor.user_id, "agent.status", rbac.REASON_NOT_ADMIN, chat_id=room.id)
        await _reply_in_group(
            ctx, room.id, config.AGENT_CONFIRM_OWNER_ONLY_TEXT, reply_to=msg.message_id
        )
        return

    args = [str(a).strip() for a in (ctx.args or [])]
    sub = (args[0].lower() if args else "") or "status"

    if sub in ("confirm", "cancel"):
        request_id = args[1] if len(args) > 1 else ""
        if not request_id:
            await _reply_in_group(
                ctx,
                room.id,
                f"شناسهٔ کار را بده: /agent {sub} <request_id>",
                reply_to=msg.message_id,
            )
            return
        result = (
            agent_service.confirm(
                actor_id=actor.user_id, chat_id=room.id, request_id=request_id
            )
            if sub == "confirm"
            else agent_service.cancel(actor_id=actor.user_id, request_id=request_id)
        )
        _audit(
            actor.user_id,
            f"agent.{sub}",
            result.outcome,
            target_id=None,
            chat_id=room.id,
            detail=request_id,
        )
        await _reply_in_group(
            ctx, room.id, result.message, reply_to=msg.message_id
        )
        return

    if sub != "status":
        await _reply_in_group(
            ctx,
            room.id,
            "کاربرد: /agent [status | confirm <id> | cancel <id>]",
            reply_to=msg.message_id,
        )
        return

    _audit(actor.user_id, "agent.status", admin_service.OUTCOME_OK, chat_id=room.id)
    await _reply_in_group(
        ctx,
        room.id,
        agent_service.status_text(actor_id=actor.user_id),
        reply_to=msg.message_id,
    )


async def cmd_pool(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The Gemini account pool. Owner-only.

    Owner-only rather than visible to every admin, because the report is about
    the operator's own Google projects: how many independent accounts the
    deployment has, which one is answering, and how close each is to its limit.
    That is operational information about somebody's billing relationship with
    Google, and it is not what a helper needs to moderate a group.

    It never contains a credential. Accounts are named by slot and by a masked
    tail, and the pool module has no code path that could render a key.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    if not actor.is_owner:
        _audit(actor.user_id, "pool.status", rbac.REASON_NOT_ADMIN,
               chat_id=room.id)
        await _reply_in_group(ctx, room.id, config.ADMIN_DENIED_TEXT,
                              reply_to=msg.message_id)
        return
    _audit(actor.user_id, "pool.status", "ok", chat_id=room.id)
    await _reply_in_group(
        ctx,
        room.id,
        gemini_pool.status_report()
        + "\n\n"
        + "— AI administration —\n"
        + admin_service.status_report(chat_id=room.id),
        reply_to=msg.message_id,
    )


# ── The owner's Gemini key dashboard ──────────────────────────────────────
#
# A real control plane rather than a prettier `/pool`, and the difference is
# that it can *write*. Three things keep that write surface small enough to
# reason about:
#
#   1. it is owner-only, re-checked from the actor's Telegram id on every single
#      press — never from the payload, which is fully attacker-controlled;
#   2. it can only add or remove credentials for the three workloads
#      `config.GEMINI_KEY_MANAGED_WORKLOADS` names, whatever a crafted callback
#      asks for, because `key_store` refuses the rest;
#   3. the only way to supply a credential is a private message to the owner,
#      which is deleted on arrival and never reaches the assistant.
#
# Everything the screens show is a read of the pool that already exists. There
# is no second source of truth for counters, states or events.
_KEY_SCREENS = {
    "home": lambda _first, _second: gemini_keys.overview(),
    "w": lambda first, _second: gemini_keys.workload_view(first),
    "a": lambda first, second: gemini_keys.account_view(first, second),
    "m": lambda first, second: gemini_keys.models_view(first, second),
    "u": lambda first, _second: gemini_keys.usage_view(first),
    "e": lambda first, _second: gemini_keys.events_view(first),
    "h": lambda first, _second: gemini_keys.daily_view(first),
    "-": lambda first, second: gemini_keys.remove_prompt(first, second),
    "i": lambda first, second: (
        gemini_keys.TEXT_ENV_KEY_INFO,
        [[("⬅️ بازگشت", f"{gemini_keys.PREFIX}a:{first}:{second}")]],
    ),
}


def _owner_menu_keyboard(actor: rbac.Principal) -> InlineKeyboardMarkup | None:
    """The owner's one-tap way into the key dashboard, or None for anybody else.

    Telegram's command menu lists `/keys` once it is published, but a menu has to
    be noticed before it can be used — and the owner's report was precisely that
    nothing was visible. This puts the same entry point on the two screens a
    person actually lands on.
    """
    if not actor.is_owner:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    gemini_keys.TEXT_BUTTON_OPEN,
                    callback_data=f"{gemini_keys.PREFIX}home",
                )
            ]
        ]
    )


def admin_command_handlers() -> tuple[tuple[str, object], ...]:
    """Every typed command this bot registers, as ``(name, handler)``.

    One definition read twice: ``main()`` registers from it, and the command menu
    Telegram is told about is derived from it. A menu written out separately
    would eventually name a command that does not exist, and a dead menu entry is
    worse than no menu at all — tapping it does nothing, and "the bot is broken"
    is the only reasonable conclusion.
    """
    return (
        ("whoami", cmd_whoami),
        ("admins", cmd_admins),
        ("pool", cmd_pool),
        (config.GEMINI_KEYS_COMMAND, cmd_keys),
        ("nexus", cmd_nexus),
        ("groups", cmd_groups),
        ("registergroup", cmd_register_group),
        ("unregistergroup", cmd_unregister_group),
        ("agent", cmd_agent),
        ("promote", cmd_promote),
        ("demote", cmd_demote),
        ("ban", cmd_ban),
        ("unban", cmd_unban),
        ("mute", cmd_mute),
        ("unmute", cmd_unmute),
        ("warn", cmd_warn),
        ("del", cmd_delete),
    )


def chat_command_handlers() -> tuple[tuple[str, object], ...]:
    """The conversational commands — empty when the assistant is switched off.

    A function rather than a constant because the registration and the published
    command menu both have to ask the same question. `/start` is not registered
    when the assistant is off, so a menu that advertised it would offer an entry
    that does nothing when tapped, which reads as a broken bot.
    """
    if not config.GEMINI_CHAT_ENABLED:
        return ()
    return (("start", on_chat_start), ("reset", on_chat_reset))


# What the public half of the menu calls each conversational command. Kept
# beside `chat_command_handlers` so the two cannot disagree about the names.
CHAT_COMMAND_LABELS = {"start": "شروع", "reset": "پاک کردن گفتگو"}


def transcribe_command_handlers() -> tuple[tuple[str, object], ...]:
    """The voice-to-text command, or nothing when it is not configured.

    Same reasoning as `chat_command_handlers`: the name is a setting, so the
    menu has to ask the function that registers it rather than assume it.
    """
    if not config.TRANSCRIBE_COMMAND:
        return ()
    return ((config.TRANSCRIBE_COMMAND, on_transcribe_command),)


# Descriptions for the administrative commands, in the order the menu shows
# them. Names only — the list is filtered against what is actually registered.
OWNER_COMMAND_LABELS = (
    (config.GEMINI_KEYS_COMMAND, "کلیدهای Gemini — افزودن، حذف و مصرف"),
    ("pool", "وضعیت استخر و حساب‌های Gemini"),
    ("nexus", "روشن یا خاموش کردن دستیار"),
    ("groups", "لیست گروه‌های ثبت‌شده"),
    ("registergroup", "ثبت این گروه برای پاسخ‌دهی نکسوس"),
    ("unregistergroup", "لغو ثبت این گروه"),
    ("agent", "درخواست تغییر کد"),
    ("admins", "لیست مدیران"),
    ("promote", "ارتقای یک نفر به مدیر"),
    ("demote", "گرفتن نقش مدیر"),
    ("ban", "بن کردن"),
    ("unban", "رفع بن"),
    ("mute", "سکوت"),
    ("unmute", "رفع سکوت"),
    ("warn", "اخطار"),
    ("del", "حذف پیام"),
)


def command_menu() -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """``(public, owner)`` — the two command lists Telegram is told about.

    Two scopes because the two audiences are different, and the difference is
    the project's usual rule about where operational detail belongs. Everybody
    gets the commands anybody may use; the owner additionally gets the
    administrative ones. Publishing `/ban`, `/promote` and `/keys` to every chat
    would advertise the moderation surface to the people it is aimed at, and
    would tell a stranger the bot has a credential dashboard.

    Every entry is derived from the same functions that register the handlers —
    `chat_command_handlers`, `transcribe_command_handlers` and
    `admin_command_handlers` — so a name can only appear in the menu if that
    command actually exists. A menu entry with nothing behind it is worse than a
    missing one: tapping it does nothing, and "the bot is broken" is the only
    reasonable conclusion.
    """
    admin = dict(admin_command_handlers())
    public: list[tuple[str, str]] = [
        (name, CHAT_COMMAND_LABELS[name]) for name, _handler in chat_command_handlers()
    ]
    if "whoami" in admin:
        public.append(("whoami", "نقش من"))
    for name, _handler in transcribe_command_handlers():
        public.append((name, "تبدیل صدا به متن"))
    owner = list(public)
    seen = {name for name, _label in owner}
    for name, label in OWNER_COMMAND_LABELS:
        if name in admin and name not in seen:
            owner.append((name, label))
            seen.add(name)
    return tuple(public), tuple(owner)


async def _publish_command_menu(app: Application) -> None:
    """Tell Telegram which commands this bot has, for each scope.

    Why this exists at all: without it the bot's command list is *empty*, so
    Telegram renders no menu button and no command list. Every command this bot
    has — `/keys` included — was reachable only by typing it from memory, which
    is not a user interface. The owner reported exactly that.

    Best effort, and never fatal. A bot that cannot reach Telegram at boot has a
    larger problem than a missing menu, and failing to start over one would turn
    a cosmetic failure into an outage. The catch is deliberately broad rather
    than ``TelegramError`` for that reason: this runs inside ``post_init``, so
    *anything* that escapes here stops the bot from starting — and no possible
    failure of a cosmetic menu is worth an outage. It is logged with a traceback,
    so a real bug is still visible rather than silent.
    """
    try:
        public, owner = command_menu()
        await app.bot.set_my_commands(
            [BotCommand(name, label) for name, label in public]
        )
        if rbac.has_owner():
            await app.bot.set_my_commands(
                [BotCommand(name, label) for name, label in owner],
                scope=BotCommandScopeChat(chat_id=rbac.owner_id()),
            )
    except Exception:  # noqa: BLE001 - see above: a menu must not stop the bot
        log.exception("could not publish the command menu")
        return
    log.info(
        "command menu published: public=%d owner=%d",
        len(public),
        len(owner),
    )


def _keys_keyboard(rows) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


async def _keys_edit(query, text: str, rows) -> None:
    """Replace the screen in place, and always clear the loading spinner.

    ``edit_message_text`` raises for a message that has not changed, which is the
    normal outcome of pressing refresh twice. That must not leave the spinner
    running, so the answer is sent either way.
    """
    try:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=_keys_keyboard(rows),
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        log.info("key screen edit failed: %s", exc)
    try:
        await query.answer()
    except TelegramError:
        pass


async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/keys` — the owner's Gemini credential and usage control plane.

    Owner-only for the same reason `/pool` is: the report describes the
    operator's own Google projects. This one additionally writes, so the check is
    repeated on every callback rather than trusted from the command that opened
    the screen.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    if not actor.is_owner:
        _audit(actor.user_id, "keys.view", rbac.REASON_NOT_ADMIN, chat_id=room.id)
        await _reply_in_group(
            ctx, room.id, gemini_keys.TEXT_DENIED, reply_to=msg.message_id
        )
        return
    _audit(actor.user_id, "keys.view", "ok", chat_id=room.id)
    text, rows = gemini_keys.overview()
    await _reply_in_group(
        ctx, room.id, text, keyboard=_keys_keyboard(rows), reply_to=msg.message_id
    )


async def on_key_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A press on the key dashboard.

    Authorisation is decided here, from the presser's Telegram id, before the
    payload is even parsed — and then again by ``key_store`` on the workload the
    payload named. The payload can therefore choose *which* screen to open, and
    nothing else.
    """
    query = update.callback_query
    if query is None or not query.data or not query.data.startswith(
        gemini_keys.PREFIX
    ):
        return
    room = update.effective_chat
    chat_id = getattr(room, "id", None)
    actor = _actor(update)
    if not actor.is_owner:
        _audit(actor.user_id, "keys.view", rbac.REASON_NOT_ADMIN, chat_id=chat_id)
        try:
            await query.answer(gemini_keys.TEXT_DENIED)
        except TelegramError:
            pass
        return

    parsed = gemini_keys.parse(query.data)
    if parsed is None:
        try:
            await query.answer(gemini_keys.TEXT_STALE)
        except TelegramError:
            pass
        return
    verb, first, second = parsed
    private = str(getattr(room, "type", "") or "") == "private"

    if verb == "x":
        try:
            await query.answer()
            await query.delete_message()
        except TelegramError:
            pass
        return

    if verb == "+":
        await _keys_begin_add(query, actor, first, chat_id, private)
        return

    if verb == "!":
        await _keys_remove(query, actor, first, second, chat_id)
        return

    render = _KEY_SCREENS.get(verb)
    if render is None:
        try:
            await query.answer(gemini_keys.TEXT_STALE)
        except TelegramError:
            pass
        return
    try:
        text, rows = render(first, second)
    except Exception:  # noqa: BLE001 - a broken screen must not kill the bot
        log.exception("key screen failed verb=%s workload=%s", verb, first)
        try:
            await query.answer(gemini_keys.TEXT_STALE)
        except TelegramError:
            pass
        return
    await _keys_edit(query, text, rows)


async def _keys_begin_add(query, actor, workload: str, chat_id, private: bool) -> None:
    """Arm the key prompt, or explain why it cannot be armed here."""
    if not key_store.is_managed(workload):
        _audit(actor.user_id, "keys.add", "not_managed", chat_id=chat_id,
               detail=workload)
        try:
            await query.answer(gemini_keys.TEXT_NOT_MANAGED)
        except TelegramError:
            pass
        return
    text, rows = gemini_keys.add_prompt(workload, private=private)
    if private:
        gemini_keys.begin_add(actor.user_id, workload)
    _audit(actor.user_id, "keys.add", "prompt" if private else "not_private",
           chat_id=chat_id, detail=workload)
    await _keys_edit(query, text, rows)


async def _keys_remove(query, actor, workload: str, slot: str, chat_id) -> None:
    """Perform the confirmed removal, then rebuild the pool."""
    if not key_store.is_managed(workload):
        try:
            await query.answer(gemini_keys.TEXT_NOT_MANAGED)
        except TelegramError:
            pass
        return
    try:
        gone = key_store.remove(workload, slot, actor_id=actor.user_id)
    except key_store.StoreError as exc:
        _audit(actor.user_id, "keys.remove", exc.reason, chat_id=chat_id,
               detail=f"{workload}/{slot}")
        text = (
            gemini_keys.TEXT_STORE_BROKEN
            if exc.reason == "corrupt"
            else gemini_keys.TEXT_ADD_FAILED.format(reason=html.escape(exc.reason))
        )
        await _keys_edit(
            query, text, [[("⬅️ بازگشت", f"{gemini_keys.PREFIX}w:{workload}")]]
        )
        return
    if gone is None:
        # Not a runtime credential — most often an environment slot, which this
        # screen has no way to remove and should not pretend to.
        _audit(actor.user_id, "keys.remove", "missing", chat_id=chat_id,
               detail=f"{workload}/{slot}")
        await _keys_edit(
            query,
            gemini_keys.TEXT_REMOVE_MISSING,
            [[("⬅️ بازگشت", f"{gemini_keys.PREFIX}w:{workload}")]],
        )
        return
    # The pool is rebuilt before the owner is told it happened, so the screen
    # they land on cannot describe a pool that no longer exists.
    gemini_pool.reload()
    _audit(actor.user_id, "keys.remove", "ok", chat_id=chat_id,
           detail=f"{workload}/{slot} {gone.masked}")
    log.info(
        "owner removed a credential workload=%s slot=%s by=%s",
        workload,
        slot,
        actor.user_id,
    )
    await _keys_edit(
        query,
        gemini_keys.TEXT_REMOVE_OK.format(
            slot=html.escape(slot), label=html.escape(gemini_keys.label(workload))
        ),
        [[("⬅️ بازگشت", f"{gemini_keys.PREFIX}w:{workload}")]],
    )


async def on_key_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The one place a credential is typed into this bot.

    Registered in group 0, ahead of the assistant's private-chat handler, and it
    raises ``ApplicationHandlerStop`` once it has taken the message. That is the
    isolation requirement and it is structural rather than a convention: a key
    pasted into the owner's private chat must not reach the conversational layer,
    and the only way to guarantee that is to stop the update before the group
    that would send it runs.

    It returns silently in every other case, so nothing here changes how the
    owner's ordinary private messages behave. In particular a message that is
    plainly conversation — more than one word — is left alone and the prompt
    stays armed, because a prompt that hijacked the next thing the owner typed
    would be worse than one that expired.

    The verification is a network call, so it is *not* awaited here. The handler
    deletes the message, answers, and hands the work to a task; a handler that
    blocked for fifteen seconds would stop every other update in the bot.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user or user.is_bot:
        return
    if str(getattr(room, "type", "") or "") != "private":
        return
    text = msg.text or ""
    if not text:
        return
    actor = rbac.resolve(int(user.id))
    if not actor.is_owner:
        return
    workload = gemini_keys.pending_for(actor.user_id)
    if not workload:
        return

    stripped = text.strip()
    if not key_store.looks_like_key(text):
        if any(char.isspace() for char in stripped) or len(stripped) < 20:
            return
        # A single long token that is not a credential. Worth saying, because
        # otherwise a mistyped key looks exactly like nothing happening.
        await _reply_in_group(ctx, room.id, gemini_keys.TEXT_ADD_BAD_SHAPE)
        return

    # From here the message is a credential, and it has been consumed.
    gemini_keys.clear_pending(actor.user_id)
    chat_id = int(room.id)
    deleted = True
    try:
        await ctx.bot.delete_message(chat_id, msg.message_id)
    except TelegramError as exc:
        deleted = False
        log.warning("could not delete the key message: %s", exc)
    _audit(actor.user_id, "keys.add", "received", chat_id=chat_id, detail=workload)
    try:
        notice = await ctx.bot.send_message(chat_id, gemini_keys.TEXT_ADD_VERIFYING)
    except TelegramError as exc:
        log.warning("could not open the key result notice: %s", exc)
        notice = None
    ctx.application.create_task(
        _keys_store(
            ctx.bot,
            actor.user_id,
            workload,
            text,
            chat_id,
            getattr(notice, "message_id", 0),
            deleted,
        )
    )
    raise ApplicationHandlerStop


async def _keys_store(
    bot,
    actor_id: int,
    workload: str,
    key: str,
    chat_id: int,
    notice_id: int,
    deleted: bool,
) -> None:
    """Verify one credential and store it. Runs off the dispatcher's path.

    The credential is never logged, never echoed and never written to the
    database: ``key_store`` puts it in a root-only file, and ``admin_audit``
    gets the slot and the masked tail.
    """
    try:
        ok, kind, detail = await gemini_pool.probe_credential(key)
    except Exception:  # noqa: BLE001 - a failed probe is a refusal, not a crash
        log.exception("credential probe failed workload=%s", workload)
        ok, kind, detail = False, "unknown_error", ""
    if not ok:
        _audit(actor_id, "keys.add", kind or "probe_failed", chat_id=chat_id,
               detail=workload)
        await _keys_notice(
            bot, chat_id, notice_id,
            gemini_keys.probe_reason(kind, detail) + _deleted_note(deleted),
        )
        return
    try:
        entry, created = key_store.add(workload, key, actor_id=actor_id)
    except key_store.StoreError as exc:
        _audit(actor_id, "keys.add", exc.reason, chat_id=chat_id, detail=workload)
        text = (
            gemini_keys.TEXT_STORE_BROKEN
            if exc.reason == "corrupt"
            else gemini_keys.TEXT_ADD_FAILED.format(reason=html.escape(exc.reason))
        )
        await _keys_notice(bot, chat_id, notice_id, text + _deleted_note(deleted))
        return
    # Rebuilt before the confirmation is sent, so the next request that needs an
    # answer already sees the new account.
    gemini_pool.reload()
    _audit(actor_id, "keys.add", "ok", chat_id=chat_id,
           detail=f"{workload}/{entry.slot} {entry.masked}")
    log.info(
        "owner added a credential workload=%s slot=%s created=%s by=%s",
        workload,
        entry.slot,
        created,
        actor_id,
    )
    if not created:
        text = gemini_keys.TEXT_ADD_DUPLICATE.format(
            label=html.escape(gemini_keys.label(workload))
        )
    else:
        text = gemini_keys.TEXT_ADD_OK.format(
            label=html.escape(gemini_keys.label(workload)),
            slot=html.escape(entry.slot),
            masked=html.escape(entry.masked),
            detail=html.escape(detail),
        )
    await _keys_notice(bot, chat_id, notice_id, text + _deleted_note(deleted))


def _deleted_note(deleted: bool) -> str:
    """The line appended when the key message could not be removed.

    Silence here would be the wrong kind of tidy: the credential is in the chat
    history, and the owner is the only person who can delete it.
    """
    if deleted:
        return ""
    return (
        "\n\n⚠️ پیام کلید حذف نشد. لطفاً خودت آن را از این چت پاک کن."
    )


async def _keys_notice(bot, chat_id: int, message_id: int, text: str) -> None:
    """Put the result on the notice message, or on a new one if it is gone."""
    if message_id:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, parse_mode="HTML"
            )
            return
        except TelegramError as exc:
            log.info("key notice edit failed: %s", exc)
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramError as exc:
        log.warning("could not report the key result: %s", exc)


def _promote_keyboard(actor_id: int, target_id: int, role: str, mask: int):
    """The permission-selection keyboard.

    One button per permission the role carries, each showing its own state, then
    confirm and cancel. The buttons carry a *mask*, and the handler re-authorises
    from scratch on every press — so the worst a crafted payload can do is show
    a different set of ticks to the person who crafted it.
    """
    code = _CODE_ROLES.get(role, "m")
    bundle = rbac.ROLE_PERMISSIONS.get(role, frozenset())
    rows = []
    for index, permission in enumerate(_MASK_PERMISSIONS):
        if permission not in bundle:
            continue
        on = bool(mask & (1 << index))
        label = rbac.PERMISSION_LABELS.get(permission, permission)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if on else '⬜️'} {label}",
                    callback_data=(
                        f"{ADMIN_CALLBACK_PREFIX}t:{actor_id}:{target_id}:"
                        f"{code}:{mask}:{index}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                config.ADMIN_PROMOTE_CONFIRM_BUTTON,
                callback_data=(
                    f"{ADMIN_CALLBACK_PREFIX}c:{actor_id}:{target_id}:{code}:{mask}"
                ),
            ),
            InlineKeyboardButton(
                config.ADMIN_PROMOTE_CANCEL_BUTTON,
                callback_data=f"{ADMIN_CALLBACK_PREFIX}x:{actor_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def cmd_promote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/promote [role]` — reply to somebody to make them an administrator.

    The whole flow, and every step is a check rather than an assumption:

    1. the actor must hold ``admins.manage`` *and* be allowed to act on the
       target, which ``authorize`` decides (owner protection and rank included);
    2. the requested role must be one the actor may assign at all;
    3. the bot must itself hold ``can_promote_members`` in this chat before it
       offers to change anything in Telegram;
    4. the keyboard is shown, and nothing is written until it is confirmed.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    found = _target_from(update, ctx, ctx.args)
    if found is None:
        await _reply_in_group(ctx, room.id, config.ADMIN_TARGET_NOT_FOUND_TEXT,
                              reply_to=msg.message_id)
        return
    target_id, target_name = found
    if target_id == getattr(ctx.bot, "id", 0):
        _audit(actor.user_id, "admin.promote", "bot_target", target_id=target_id,
               chat_id=room.id)
        await _reply_in_group(ctx, room.id, config.ADMIN_TARGET_IS_BOT_TEXT,
                              reply_to=msg.message_id)
        return

    target = rbac.resolve(target_id)
    args = list(ctx.args or ())
    named = args[0].lower() if args and args[0].lower() in _ROLE_ALIASES else ""
    requested = _ROLE_ALIASES.get(named, rbac.ROLE_MODERATOR)

    grantable = rbac.grantable_roles(actor)
    if requested not in grantable:
        if not grantable:
            # Nothing this actor may assign at all. Refused with the permission
            # reason rather than a role reason: the missing thing is the
            # authority, not the choice of role.
            decision = rbac.Decision(
                False, rbac.REASON_MISSING_PERMISSION, "admins.manage"
            )
            _audit(actor.user_id, "admin.promote", decision.reason,
                   target_id=target_id, chat_id=room.id)
            await _reply_in_group(ctx, room.id, _deny_text(decision),
                                  reply_to=msg.message_id)
            return
        # Asked for more than they may give. The question was "promote this
        # person", and the only open question is how far, so the highest role
        # they may assign is used rather than the command being refused.
        requested = grantable[-1]

    decision = rbac.authorize_grant(
        actor, requested, rbac.ROLE_PERMISSIONS[requested], target=target
    )
    if not decision:
        _audit(actor.user_id, "admin.promote", decision.reason, target_id=target_id,
               chat_id=room.id, detail=decision.detail)
        await _reply_in_group(ctx, room.id, _deny_text(decision),
                              reply_to=msg.message_id)
        return

    mask = _mask(rbac.ROLE_PERMISSIONS[requested])
    # Told, not assumed: if the bot cannot promote in Telegram the operator is
    # about to grant an application role with no Telegram effect, and that has
    # to be visible before they confirm rather than discovered afterwards.
    can_telegram = await _bot_right(ctx, room.id, "can_promote_members")
    note = "" if can_telegram else f"\n{config.ADMIN_PROMOTE_NO_TELEGRAM_TEXT}"
    await _reply_in_group(
        ctx,
        room.id,
        config.ADMIN_PROMOTE_TITLE.format(name=html.escape(target_name)) + note,
        keyboard=_promote_keyboard(actor.user_id, target_id, requested, mask),
        reply_to=msg.message_id,
    )


async def on_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a press on the promote dialog.

    Re-authorises everything from scratch. The callback payload is fully
    attacker-controlled, so it is parsed for *intent* and then run through the
    same checks the original command ran — a lower administrator cannot confirm
    a promotion they could not have requested, and a crafted mask cannot grant
    a permission the actor does not hold.
    """
    q = update.callback_query
    if q is None or not q.data or not q.data.startswith(ADMIN_CALLBACK_PREFIX):
        return
    parts = q.data.split(":")
    kind = parts[1] if len(parts) > 1 else ""
    presser = _actor(update)
    room = update.effective_chat
    chat_id = getattr(room, "id", None)

    if kind == "x":
        await q.answer(config.ADMIN_CANCELLED_TEXT)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    try:
        actor_id = int(parts[2])
    except (IndexError, ValueError):
        await q.answer(config.ADMIN_STALE_BUTTON_TEXT)
        return

    if presser.user_id != actor_id:
        # The dialog belongs to whoever opened it. Another administrator can run
        # the command themselves; they cannot confirm somebody else's.
        _audit(presser.user_id, "admin.promote", "not_the_requester", chat_id=chat_id)
        await q.answer(config.ADMIN_DENIED_TEXT)
        return

    try:
        target_id = int(parts[3])
        role = _ROLE_CODES.get(parts[4], "")
        mask = int(parts[5])
    except (IndexError, ValueError):
        await q.answer(config.ADMIN_STALE_BUTTON_TEXT)
        return

    target = rbac.resolve(target_id)

    if kind == "t":
        # A toggle changes only what is displayed. It still has to be a change
        # this actor is allowed to make, so a crafted payload cannot even show
        # them a set they could not confirm.
        try:
            bit = int(parts[6])
        except (IndexError, ValueError):
            await q.answer(config.ADMIN_STALE_BUTTON_TEXT)
            return
        if bit < 0 or bit >= len(_MASK_PERMISSIONS):
            await q.answer(config.ADMIN_STALE_BUTTON_TEXT)
            return
        new_mask = mask ^ (1 << bit)
        decision = rbac.authorize_grant(
            presser, role, _unmask(new_mask), target=target
        )
        if not decision:
            _audit(presser.user_id, "admin.promote", decision.reason,
                   target_id=target_id, chat_id=chat_id)
            await q.answer(_deny_text(decision))
            return
        await q.answer()
        try:
            await q.edit_message_reply_markup(
                reply_markup=_promote_keyboard(actor_id, target_id, role, new_mask)
            )
        except TelegramError:
            pass
        return

    if kind != "c":
        await q.answer(config.ADMIN_STALE_BUTTON_TEXT)
        return

    permissions = _unmask(mask)
    decision = rbac.authorize_grant(presser, role, permissions, target=target)
    if not decision:
        _audit(presser.user_id, "admin.promote", decision.reason, target_id=target_id,
               chat_id=chat_id, detail=decision.detail)
        await q.answer(_deny_text(decision))
        return

    # ── Everything is authorised. Now hand it to the service, which authorises
    #    it again on its own terms and performs both layers in the right order.
    #
    #    The double check is not redundant: the check above is what makes the
    #    button honest about what it is offering, and the service's check is what
    #    actually decides. Only the second one is the security boundary, and it
    #    does not know or care that a keyboard was involved.
    result = await admin_service.execute(
        admin_service.AdminRequest(
            operation="promote_member",
            chat_id=int(chat_id or 0),
            actor_id=presser.user_id,
            target_id=target_id,
            role=role,
            # The operator's toggled set. The AI path never sets this field —
            # its tool has no such parameter — so "the model may not choose
            # individual Telegram rights" is a property of the tool, not a rule
            # this line has to remember to enforce.
            permissions=tuple(sorted(permissions)),
            request_id=admin_service.new_request_id(),
            interface=admin_service.INTERFACE_PYTHON,
            at=int(time.time()),
        ),
        TelegramGateway(ctx),
        actor=presser,
        bot_id=getattr(ctx.bot, "id", 0),
    )
    if not result.ok:
        _audit(presser.user_id, "admin.promote", result.outcome, target_id=target_id,
               chat_id=chat_id, detail=result.reason or result.detail)
        try:
            await q.answer(_refusal_text(result)[:180])
        except TelegramError:
            pass
        return

    telegram_note = result.extra.get("telegram_note", "")
    label = rbac.ROLE_LABELS.get(role, role)
    perms_text = "، ".join(rbac.permission_labels(permissions)) or "—"
    body = (
        config.ADMIN_PROMOTE_DONE_TEXT.format(
            name=f"<code>{target_id}</code>", perms=perms_text
        )
        + f"\n({label})"
    )
    if telegram_note:
        body += f"\n{telegram_note}"
    try:
        await q.edit_message_text(body, parse_mode="HTML")
    except TelegramError:
        try:
            await q.answer(body[:180])
        except TelegramError:
            pass


async def cmd_demote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/demote` — reply to an administrator to revoke the application role."""
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)
    found = _target_from(update, ctx, ctx.args)
    if found is None:
        await _reply_in_group(ctx, room.id, config.ADMIN_TARGET_NOT_FOUND_TEXT,
                              reply_to=msg.message_id)
        return
    target_id, _name = found
    result = await admin_service.execute(
        admin_service.AdminRequest(
            operation="demote_member",
            chat_id=room.id,
            actor_id=actor.user_id,
            target_id=target_id,
            request_id=admin_service.new_request_id(),
            interface=admin_service.INTERFACE_PYTHON,
            at=int(time.time()),
        ),
        TelegramGateway(ctx),
        actor=actor,
        bot_id=getattr(ctx.bot, "id", 0),
    )
    if not result.ok:
        await _reply_in_group(ctx, room.id, _refusal_text(result),
                              reply_to=msg.message_id)
        return

    body = config.ADMIN_DEMOTE_DONE_TEXT.format(name=f"<code>{target_id}</code>")
    note = result.extra.get("telegram_note", "")
    if note:
        body += f"\n{note}"
    await _reply_in_group(ctx, room.id, body, reply_to=msg.message_id)




def _command_done_text(operation: str, name: str) -> str:
    """The sentence for a completed operation, keyed on the operation name.

    Keyed on ``admin_service``'s operation names rather than on the RBAC
    permission keys the commands used to pass, because the operation is now what
    the service returns — and a sentence table keyed on anything else would be a
    second vocabulary for the same set of actions.
    """
    safe = html.escape(name)
    if operation == "ban_member":
        return config.MOD_BAN_DONE_TEXT.format(name=safe)
    if operation == "unban_member":
        return config.MOD_UNBAN_DONE_TEXT.format(name=safe)
    if operation == "mute_member":
        return config.MOD_MUTE_DONE_TEXT.format(name=safe, minutes=config.MUTE_MINUTES)
    if operation == "unmute_member":
        return config.MOD_UNMUTE_DONE_TEXT.format(name=safe)
    if operation == "warn_member":
        return config.MOD_WARN_DONE_TEXT.format(name=safe)
    if operation == "delete_message":
        return config.MOD_DELETE_DONE_TEXT
    return config.ADMIN_DONE_TEXT


def _refusal_text(result: admin_service.AdminResult) -> str:
    """The Persian sentence for a refused result.

    A refusal that came from the authority model keeps its specific wording —
    "you cannot do that" and "that person outranks you" are different facts and
    an operator acts differently on each. Everything else uses the outcome's own
    sentence.
    """
    if result.outcome == admin_service.OUTCOME_DENIED and result.reason:
        return _deny_text(rbac.Decision(False, result.reason, result.detail))
    return admin_service.message_for(result.outcome)


async def _admin_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    operation: str,
) -> admin_service.AdminResult | None:
    """The shared body of every moderation command.

    One function rather than six, and it no longer decides anything: it resolves
    *what* is being asked (which target, which message) and hands a typed request
    to ``app/admin_service.py``, which authorises and executes it. The checks
    that must not be duplicated — is the actor real, may they do this, is the
    target protected, does Telegram permit it — now live in exactly one place,
    the same place the assistant's tool calls go through.

    The split of responsibility is deliberate. This function knows about
    ``Update`` objects and Persian sentences; the service knows about authority
    and Telegram. Neither knows about the other's world, which is what makes it
    possible to test the security rules without a Telegram client and to add a
    second interface without touching them.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    actor = _actor(update)

    if operation == "delete_message":
        replied = getattr(msg, "reply_to_message", None)
        if replied is None:
            await _reply_in_group(ctx, room.id, config.MOD_TARGET_REQUIRED_TEXT,
                                  reply_to=msg.message_id)
            return
        request = admin_service.AdminRequest(
            operation=operation,
            chat_id=room.id,
            actor_id=actor.user_id,
            message_id=int(getattr(replied, "message_id", 0) or 0),
            request_id=admin_service.new_request_id(),
            interface=admin_service.INTERFACE_PYTHON,
            at=int(time.time()),
        )
        target_name = str(getattr(replied, "message_id", ""))
    else:
        found = _target_from(update, ctx, ctx.args)
        if found is None:
            await _reply_in_group(ctx, room.id, config.MOD_TARGET_REQUIRED_TEXT,
                                  reply_to=msg.message_id)
            return
        target_id, target_name = found
        request = admin_service.AdminRequest(
            operation=operation,
            chat_id=room.id,
            actor_id=actor.user_id,
            target_id=target_id,
            reason=" ".join(ctx.args or ()) if operation == "warn_member" else "",
            request_id=admin_service.new_request_id(),
            interface=admin_service.INTERFACE_PYTHON,
            at=int(time.time()),
        )

    result = await admin_service.execute(
        request, TelegramGateway(ctx), actor=actor, bot_id=getattr(ctx.bot, "id", 0)
    )

    if not result.ok:
        log.info(
            "admin refused operation=%s actor=%s outcome=%s reason=%s",
            operation, actor.user_id, result.outcome, result.reason,
        )
        await _reply_in_group(ctx, room.id, _refusal_text(result),
                              reply_to=msg.message_id)
        return result

    await _reply_in_group(
        ctx, room.id, _command_done_text(operation, target_name),
        reply_to=msg.message_id,
    )
    return result


# Each command is now one line of policy — which operation it is — and nothing
# else. Everything they used to do individually (resolve the actor, resolve the
# target, ask RBAC, check the bot's Telegram rights, call the API, audit, report)
# happens once, in ``app/admin_service.py``, on the same path the assistant's
# tool calls take. The verbs that used to be lambdas closing over ``ctx.bot``
# are methods on ``TelegramGateway``.
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_command(update, ctx, operation="ban_member")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_command(update, ctx, operation="unban_member")


async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_command(update, ctx, operation="mute_member")


async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _admin_command(update, ctx, operation="unmute_member")


async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Warn a user. The only command that addresses the person being actioned."""
    await _admin_command(update, ctx, operation="warn_member")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the replied-to message.

    The one command with an extra step afterwards: the message id is remembered
    as deleted so the assistant does not reply to something that is gone. That
    is presentation, not authorisation, so it stays here rather than in the
    service.
    """
    msg = update.effective_message
    room = update.effective_chat
    if not msg or not room:
        return
    replied = getattr(msg, "reply_to_message", None)
    result = await _admin_command(update, ctx, operation="delete_message")
    # Only when it actually went. Remembering a deletion that was refused would
    # silence the assistant about a message that is still there.
    if result is not None and result.ok and replied is not None:
        mark_deleted(room.id, getattr(replied, "message_id", 0))


def _chat_active() -> bool:
    """Whether the conversational assistant can answer at all.

    A wrapper rather than a direct ``chat.is_enabled()`` call because
    ``on_group_text`` binds a local named ``chat`` (its effective chat), which
    shadows the module for the whole of that function. This keeps one source of
    truth for the policy without a rename that would touch unrelated handlers.
    """
    return chat.is_enabled()


# ------------------------------------------------- text moderation
# This is the only content-moderation path left. It is off by default (see
# MODERATION_TEXT_ENABLED) and it is deliberately the last handler group: it
# must never run before acquisition or conversation have had their say, because
# it is the only path that can delete a person's words.
#
# The only signal it has is the moderation AI's verdict. The media path used to
# carry a local detector alongside it; that whole pipeline was removed, so a
# deletion now requires a confident AI verdict and nothing else.


async def on_group_text_moderation(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """The moderation AI's opinion on one group message, and the policy on it."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot:
        return
    if not authorized_group(chat.id):
        return
    if user.id in config.WHITELIST_USER_IDS:
        return
    # Staff are not moderated by their own bot. Checked here rather than left to
    # the AI, because a moderation system that argues with its operators is one
    # they will turn off.
    if await is_admin(ctx, chat.id, user.id):
        return
    if was_deleted(chat.id, msg.message_id):
        return

    text = _message_text(msg)
    if len(text) < max(1, int(config.MODERATION_TEXT_MIN_CHARS)):
        return

    verdict = await ai_moderation.assess_text(text)
    outcome = mod_policy.decide(mod_policy.PolicyInput(ai=verdict))
    log.info(
        "text moderation chat=%s user=%s policy=%s",
        chat.id, user.id, mod_policy.describe(outcome),
    )

    if outcome.allows:
        return
    if outcome.reviews:
        await _notify_review(ctx, chat, user, msg, verdict, outcome)
        return

    enforced = mod_policy.enforce_result(outcome)
    result = await moderation.enforce(
        enforced,
        delete_media=lambda: msg.delete(),
        record_confirmed=lambda: db.add_strike(chat.id, user.id),
    )
    if not result.deleted:
        log.error(
            "TEXT_DELETE_FAILED chat=%s message=%s error=%s",
            chat.id, getattr(msg, "message_id", "-"), result.reason,
        )
        return
    mark_deleted(chat.id, getattr(msg, "message_id", 0))
    log.info(
        "TEXT_DELETE_SUCCESS chat=%s message=%s policy=%s",
        chat.id, getattr(msg, "message_id", "-"), outcome.reason,
    )
    await _send_text_deletion_report(ctx, chat, user, msg, verdict, outcome)
    if result.strike is not None:
        # The same ladder the media path uses, through the same function. It is
        # *not* an automatic ban and not an automatic long mute: it is the
        # configured, documented, timed restriction, applied only to a message
        # that was actually deleted — and only because the AI confirmed it.
        await _apply_strike_ladder(
            ctx, chat.id, user, strike=result.strike, source="text"
        )


async def _send_text_deletion_report(ctx, chat, user, msg, verdict, outcome) -> None:
    """The admin report for a deleted text message.

    Carries no excerpt of the message. The classification, the confidence and
    the policy reason are what an operator needs to judge the decision; the
    message itself is what this project spends the most effort not copying into
    a log.
    """
    if not config.ADMIN_LOG_CHAT:
        return
    ai_line = (
        f"🤖 <b>{verdict.classification}</b> ({verdict.confidence:.2f})"
        f"{' ⚠️ نامطمئن' if verdict.uncertain else ''}\n"
        f"   دسته: {verdict.category or '-'}\n"
        if verdict is not None and verdict.decided
        else "🤖 پاسخی نداد\n"
    )
    text = (
        f"🚨 <b>حذف پیام متنی</b>\n\n"
        f"👤 {mention(user)} (<code>{user.id}</code>)\n"
        f"💬 Chat ID: <code>{chat.id}</code>\n"
        f"📩 Message ID: <code>{getattr(msg, 'message_id', '-')}</code>\n\n"
        f"{ai_line}"
        f"⚖️ سیاست: <code>{outcome.reason}</code>\n"
        f"✅ اقدام: پیام حذف شد.\n"
    )
    try:
        await report(ctx, text)
    except TelegramError as e:
        log.warning("text deletion report failed: %s", e)


async def _send_filter_report(
    ctx, chat, user, msg, hit, *, deleted: bool
) -> None:
    """The admin report for a pattern-filter hit. Carries no excerpt of the message.

    The rule and the action are what an operator needs in order to judge the
    decision — and, when the filter is new, to decide whether the rule should
    exist at all. The message itself is what this project spends the most effort
    not copying into a log, so it is not here either.
    """
    if not config.ADMIN_LOG_CHAT:
        return
    action = "پیام حذف شد." if deleted else "فقط برای بازبینی ثبت شد."
    text = (
        f"🧹 <b>فیلتر پیام</b>\n\n"
        f"👤 {mention(user)} (<code>{user.id}</code>)\n"
        f"💬 Chat ID: <code>{chat.id}</code>\n"
        f"📩 Message ID: <code>{getattr(msg, 'message_id', '-')}</code>\n\n"
        f"🔎 قاعده: <code>{hit.kind}/{hit.label}</code>\n"
        f"✅ اقدام: {action}\n"
    )
    try:
        await report(ctx, text)
    except TelegramError as e:
        log.warning("filter report failed: %s", e)


async def on_group_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The pattern rules: links, banned words, and the shapes a scam takes.

    Runs *before* any model is consulted, and consults none. A rule that can be
    a pattern should not be a request against a shared Gemini quota, and these
    are the rules that can be.

    The enforcement is not reimplemented here. A hit becomes a
    ``DecisionResult`` and goes to ``app/moderation.py`` — the same executor, and
    the same strike ladder through ``_apply_strike_ladder``, that every other
    violation goes through. The only thing this function decides is *whether*
    there was a violation, which is what a filter is for.

    Off unless ``FILTER_ENABLED`` is set. That is deliberate: these rules delete
    somebody's message and a false positive cannot be undone, so switching them
    on is a decision an operator makes after reading the review log, not one
    this code makes for them.
    """
    if not config.FILTER_ENABLED:
        return
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot:
        return
    if not authorized_group(chat.id):
        return
    if user.id in config.WHITELIST_USER_IDS:
        return
    if was_deleted(chat.id, msg.message_id):
        return

    # Staff are exempt by default, for the same reason the moderation layer
    # skips them: a filter that argues with its own operators is one they turn
    # off. The exemption is configuration, and it is checked before the rules.
    staff = await is_admin(ctx, chat.id, user.id)
    hit = text_filters.inspect(_message_text(msg), is_admin=staff)
    if hit is None:
        return

    log.info(
        "filter chat=%s user=%s %s", chat.id, user.id, text_filters.describe(hit)
    )

    if hit.reviews:
        await _send_filter_report(ctx, chat, user, msg, hit, deleted=False)
        return

    enforced = decision.DecisionResult(
        decision.Decision.EXPLICIT, reason=f"filter:{hit.label}"
    )
    result = await moderation.enforce(
        enforced,
        delete_media=lambda: msg.delete(),
        # A filter hit only counts as a violation when the operator says so. A
        # deleted link and a deleted explicit image are not the same offence,
        # and conflating them would mute somebody for posting a URL once.
        record_confirmed=(
            (lambda: db.add_strike(chat.id, user.id))
            if config.FILTER_COUNTS_AS_VIOLATION
            else None
        ),
    )
    if not result.deleted:
        log.error(
            "FILTER_DELETE_FAILED chat=%s message=%s rule=%s error=%s",
            chat.id, getattr(msg, "message_id", "-"), hit.label, result.reason,
        )
        return

    mark_deleted(chat.id, getattr(msg, "message_id", 0))
    log.info(
        "FILTER_DELETE_SUCCESS chat=%s message=%s rule=%s",
        chat.id, getattr(msg, "message_id", "-"), hit.label,
    )
    await _send_filter_report(ctx, chat, user, msg, hit, deleted=True)
    if result.strike is not None:
        await _apply_strike_ladder(
            ctx, chat.id, user, strike=result.strike, source="filter"
        )


async def on_group_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Offer a VPN test to someone who has just asked for one.

    Runs on ordinary group text only. Edited messages are not requested from
    Telegram at all and are filtered out below as well, so editing a message
    into an intent cannot produce a second reply.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not msg.text:
        return
    if user.is_bot or not authorized_group(chat.id):
        return

    # A message aimed at the bot is a conversation, not an intent. This is the
    # boundary between the two AI policies: the assistant has its own handler in
    # its own group, and returning here is what stops one message from getting
    # both a chat reply and a trial offer.
    #
    # Two tests, because the two signals are not equally strong. The
    # Telegram-native ones stand down unconditionally, exactly as they always
    # have. The assistant's *names* are wider and stand down only when the
    # conversational layer will actually answer — see ``_nexus_will_answer`` for
    # the messages that used to fall through this line and get both.
    if _chat_active() and (
        _addressed_to_bot(msg, ctx) or _nexus_will_answer(msg, ctx)
    ):
        return

    # The layered decision: the rule engine first, and Gemini only for the
    # messages the rules were not sure about. `match` is truthy exactly when the
    # message is a lead, so nothing below this line changed when the second
    # layer was added — including the cooldown, the invitation and the replies.
    match = await classifier.classify(msg.text, user_id=user.id)
    if not match:
        return

    # Never offer anything to the group's own staff.
    if await is_admin(ctx, chat.id, user.id):
        return

    # Persisted, not in memory: a restart must not reset this and hand the same
    # person a second invitation.
    elapsed = db.seconds_since_offer(chat.id, user.id)
    if elapsed is not None and elapsed < config.INTENT_COOLDOWN_SECONDS:
        log.info(
            "intent from %s suppressed (%ss into a %ss cooldown)",
            user.id,
            elapsed,
            config.INTENT_COOLDOWN_SECONDS,
        )
        return

    log.info(
        "intent detected from %s: %s | %r",
        user.id,
        ",".join(match.reasons),
        match.normalised[:120],
    )

    name = mention(user)
    try:
        result = await vpnbot.request_invite(
            user.id, chat_id=chat.id, message_id=msg.message_id
        )
    except vpnbot.VpnBotError as exc:
        if exc.code == vpnbot.ERR_NOT_CONFIGURED:
            # The integration is switched off. Stay silent: there is nothing
            # useful to say to the group, and saying it repeatedly is worse.
            log.warning("acquisition is not configured: %s", exc)
            return
        log.error("invite request failed: %s", exc)
        db.mark_offered(chat.id, user.id, "unavailable")
        await _reply_in_group(
            ctx,
            chat.id,
            config.GROUP_TRIAL_UNAVAILABLE_TEXT,
            None,
            reply_to=msg.message_id,
        )
        return

    if result.get("ok") and result.get("deep_link"):
        db.mark_offered(chat.id, user.id, "invited")
        # The words are chosen from what the message was about, but every one of
        # them comes from app/config.py — the model selected a key, not a
        # sentence, and it never sees or writes the link.
        kind, text = responses.reply_for(match, name)
        log.info(
            "acquisition reply for %s kind=%s source=%s category=%s problem=%s",
            user.id,
            kind,
            match.source,
            (match.ai.category if match.ai else "") or "-",
            (match.ai.problem_kind if match.ai else "") or "-",
        )
        await _reply_in_group(
            ctx,
            chat.id,
            text,
            _intent_reply_keyboard(result["deep_link"]),
            reply_to=msg.message_id,
        )
        return

    reason = result.get("reason")
    if reason == "already_used":
        # They have had their one free trial. Point at the shop instead of
        # leaving them with nothing.
        db.mark_offered(chat.id, user.id, "used")
        await _reply_in_group(
            ctx,
            chat.id,
            config.GROUP_TRIAL_USED_TEXT.format(name=name),
            None,
            reply_to=msg.message_id,
        )
    elif reason == "already_invited":
        db.mark_offered(chat.id, user.id, "already_invited")
        await _reply_in_group(
            ctx,
            chat.id,
            config.GROUP_TRIAL_ALREADY_TEXT.format(name=name),
            None,
            reply_to=msg.message_id,
        )
    else:
        # not_configured, disabled, or something new. Log it and stay quiet:
        # an unexplained apology in the group is worse than silence.
        log.warning("invite refused for %s: %s", user.id, reason)


# ------------------------------------------------------------ wiring
async def update_dedup_reaper(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget the update ids Telegram will never send again.

    One indexed DELETE, and it is on its own timer rather than folded into
    another job's: a failure in one must not stop the other. Nothing is lost by
    pruning — an id older than the window will not be re-delivered, and if it
    somehow were, the update would simply be handled, which is the behaviour
    before this table existed.
    """
    if not config.UPDATE_DEDUP_ENABLED:
        return
    try:
        dropped = db.seen_updates_prune(int(config.UPDATE_DEDUP_TTL_SECONDS))
    except Exception:  # noqa: BLE001 - housekeeping is never worth a crash
        log.exception("could not prune the claimed update ids")
        return
    if dropped:
        log.info("update dedup: forgot %d expired update id(s)", dropped)


async def post_init(app: Application) -> None:
    if config.UPDATE_DEDUP_ENABLED:
        interval = max(60.0, float(config.UPDATE_DEDUP_PRUNE_INTERVAL_SECONDS))
        app.job_queue.run_repeating(update_dedup_reaper, interval=interval, first=interval)
        log.info(
            "Update dedup: on, ttl=%ds prune_every=%.0fs",
            int(config.UPDATE_DEDUP_TTL_SECONDS),
            interval,
        )
    else:
        log.warning(
            "UPDATE_DEDUP_ENABLED is off: a re-delivered update will be handled "
            "again, which means a duplicate reply after a network failure or a "
            "restart."
        )
    # The awareness sweeper. It is a *poll* rather than a subscription: a tick
    # with nothing pending costs one indexed query per configured group, and a
    # tick with something pending performs at most
    # `NEXUS_AWARENESS_MAX_CHATS_PER_TICK` batched reads. Registered here rather
    # than driven from the message handler so that a burst of messages produces
    # one pass instead of one call per message — see `app/awareness.py`.
    if config.NEXUS_AWARENESS_ENABLED:
        tick = max(5.0, float(config.NEXUS_AWARENESS_TICK_SECONDS))
        app.job_queue.run_repeating(awareness_sweep, interval=tick, first=tick)
        # The fast path. A room whose debounce has expired is read here, one
        # second after it expired, instead of waiting for the sweeper's next
        # 15-second tick — which is where most of the assistant's apparent
        # slowness was: a room that had already gone quiet, waiting for a timer
        # that was not about it. It does no query and no work while every room
        # is still talking, so the interval costs a dictionary scan.
        app.job_queue.run_repeating(
            _awareness_deadline_tick,
            interval=AWARENESS_DEADLINE_TICK_SECONDS,
            first=AWARENESS_DEADLINE_TICK_SECONDS,
        )
        log.info(
            "Nexus awareness: tick=%.0fs deadline_tick=%.1fs debounce=%.0fs "
            "max_wait=%.0fs min_interval=%.0fs window=%d msgs/%d chars "
            "context=%d chars deep=%s owner=%s",
            tick,
            AWARENESS_DEADLINE_TICK_SECONDS,
            float(config.NEXUS_AWARENESS_DEBOUNCE_SECONDS),
            float(config.NEXUS_AWARENESS_MAX_WAIT_SECONDS),
            float(config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS),
            int(config.NEXUS_AWARENESS_WINDOW_MESSAGES),
            int(config.NEXUS_AWARENESS_WINDOW_CHARS),
            # The staged context, reported for the same reason the window is:
            # an operator debugging "the assistant seems to know nothing about
            # this room" needs to see whether the deeper tier is switched on and
            # how much room the whole thing has, without reading the config.
            int(config.NEXUS_AWARENESS_CONTEXT_CHARS),
            "on" if config.NEXUS_AWARENESS_CONTEXT_DEEP else "off",
            # The owner's own switch, read from the persisted row. The jobs are
            # registered on the deploy-time setting either way — that is what
            # lets the owner switch the layer back on without a restart — so
            # this field is the difference between "registered" and "reading
            # the room", and an operator reading the log needs both.
            "on" if awareness.running() else "off",
        )
        if not awareness.running():
            log.info(
                "Nexus awareness: the owner has switched the layer off; the "
                "sweeper is registered and idle, and will read the room again "
                "when the owner says «آگاهی روشن»."
            )
        if not (
            config.GEMINI_AWARENESS_API_KEY
            or gemini_pool.has_accounts("awareness")
        ):
            log.warning(
                "NEXUS_AWARENESS_ENABLED is on but the awareness workload has "
                "no credential; the room will be captured and never read. Set "
                "GEMINI_AWARENESS_API_KEY."
            )
    else:
        log.info("Nexus awareness: off")
    # Web search. Its own workload, so this reports whether it is *armed* rather
    # than whether the assistant works: with the switch on and no credential the
    # assistant answers exactly as it did before this existed, and that is worth
    # one line at boot rather than being discovered as "it never searches".
    if not config.GEMINI_SEARCH_ENABLED:
        log.info("Web search: off (configuration)")
    elif not web_search.running():
        log.info("Web search: off (owner switch)")
    elif web_search.is_enabled():
        prov = web_search.provider()
        log.info(
            "Web search: on provider=%s model=%s results=%d daily=%d/account",
            prov,
            config.GEMINI_SEARCH_MODEL if prov == "gemini" else "-",
            int(config.GEMINI_SEARCH_MAX_RESULTS),
            int(config.GEMINI_SEARCH_DAILY_LIMIT),
        )
    else:
        prov = web_search.provider()
        log.info(
            "Web search: on provider=%s but no credential; the assistant is "
            "unchanged. Set %s.",
            prov,
            "GEMINI_SEARCH_API_KEY" if prov == "gemini" else "TAVILY_API_KEY",
        )
    # The coding-agent bridge. Registered here, and not in the awareness block,
    # because it is deliberately *not* part of the awareness workload: a coding
    # task spends the owner's CodeBuddy credential and must not touch the
    # assistant's daily allowance. One tick lists a directory and queries the
    # tasks that are actually active, which is nothing at all on a normal day.
    if config.AGENT_ENABLED:
        agent_spool.ensure()
        published = agent_poller.recover()
        app.job_queue.run_repeating(
            agent_poller.tick,
            interval=max(1.0, float(config.AGENT_POLL_SECONDS)),
            first=1.0,
        )
        log.info(
            "Coding agent: poll=%.0fs repositories=%s max_active=%d "
            "max_per_repository=%d cli=%s (republished %d queued task(s))",
            float(config.AGENT_POLL_SECONDS),
            ",".join(agent_bridge.repository_names()),
            int(config.AGENT_MAX_ACTIVE),
            int(config.AGENT_MAX_PER_REPOSITORY),
            config.AGENT_CLI or "-",
            published,
        )
        if not config.AGENT_CLI:
            log.warning(
                "AGENT_ENABLED is on but AGENT_CLI is empty, so the runner has "
                "nothing to execute. See AgentMD.md."
            )
    else:
        log.info("Coding agent: off")
    # Who we are, from Telegram rather than from configuration. Done first,
    # because the alias matching that decides whether the assistant answers
    # depends on it and a failed getMe must be visible in the log rather than
    # discovered as "the bot stopped responding to mentions".
    await load_identity(app)
    # The command menu, published to Telegram. Without this call the bot's
    # command list is empty: Telegram has nothing to render, so there is no menu
    # button and no command list, and every command is reachable only by someone
    # who already knows it exists. That was a live defect, reported by the owner
    # as "no button has been set up in the bot".
    await _publish_command_menu(app)
    # What we can see, asked of Telegram rather than assumed. This is the answer
    # to the brief's "do not assume Telegram delivers every group message": the
    # bot's ability to observe unaddressed administrator messages depends on
    # being a group administrator, and a deployment that has lost that would
    # otherwise degrade in silence.
    await _nexus_visibility_report(app)
    # The state, and what it means for who gets answered. One line, because
    # "the assistant is silent" has four different causes and this is the one
    # that says which. Who is answered is decided by the room allowlist, not by
    # an actor gate, so the line reports the registered rooms.
    log.info(
        "Nexus state: %s rooms=%d observe_admins=%s names=%d",
        nexus.state(),
        groups.count(),
        "on" if config.NEXUS_OBSERVE_ADMINS else "off",
        len(nexus.names()),
    )
    log.info("GuardBot started. Groups: %s", list(groups.all_ids()))
    # One line that makes the egress path a fact rather than an assumption. If
    # the AI ever starts timing out, this is what says whether an address family
    # was involved, instead of leaving it to be guessed at from a support report.
    egress = net.describe()
    log.info(
        "AI egress: ipv6_usable=%s order=%s prefer=%s global_v6=%d",
        egress["ipv6_usable"],
        "->".join(egress["order"]) or "unresolved",
        "installed" if egress["preferred"] else "not_installed",
        len(egress["local_ipv6"]),
    )
    if config.GROUP_TRIAL_ENABLED:
        if vpnbot.is_configured():
            log.info("Group acquisition enabled, VPN bot at %s", config.VPNBOT_API_URL)
        else:
            log.warning(
                "GROUP_TRIAL_ENABLED is on but VPNBOT_API_URL / "
                "VPNBOT_SHARED_SECRET are not set; no invitations will be sent."
            )
        # The AI layer is optional and degrades to the rule engine, so this is
        # reported rather than fatal. `status()` never contains the key itself.
        state = ai_intent.status()
        if state["active"]:
            log.info(
                "Intent AI layer active: model=%s daily_limit=%d used_today=%d",
                state["model"],
                state["daily_limit"],
                state["used_today"],
            )
        elif state["enabled"]:
            log.warning(
                "GEMINI_ENABLED is on but GEMINI_API_KEY is not set; ambiguous "
                "messages will fall back to the rule engine alone."
            )
        else:
            log.info("Intent AI layer disabled (GEMINI_ENABLED=0); rules only.")

    # The conversational assistant, reported separately and *outside* the block
    # above: it has its own switch and its own key, and it works whether or not
    # group acquisition is on. `status()` never contains the key.
    chat_state = chat.status()
    if chat_state["active"]:
        # The daily figure is per account, so `used_today` — which is the
        # deployment-wide count — is reported beside the number that is actually
        # spendable rather than against the per-account limit. Reporting
        # `daily_limit=500 used_today=500` read as "exhausted" while a second
        # account with a full day sat unused, which is the confusion this line
        # exists to prevent.
        log.info(
            "Conversational AI active: model=%s daily_limit=%d_per_account "
            "daily_remaining=%d used_today=%d history_turns=%d history_ttl=%ds",
            chat_state["model"],
            chat_state["daily_limit"],
            chat_state["daily_remaining"],
            chat_state["used_today"],
            chat_state["history_turns"],
            chat_state["history_ttl"],
        )
        if chat_state["shares_google_project"]:
            # Not a failure, but the one fact that explains a 429 on the
            # classifier that appears the first time the group is busy: the two
            # workloads keep separate counters here, but Google's own limit is
            # per project, so they draw on one allowance. Said once at startup
            # rather than discovered from a support question.
            log.warning(
                "Conversational AI is using the classifier's key "
                "(GEMINI_CHAT_ALLOW_SHARED_KEY=1). Our counters are separate, "
                "but Google's rate limit is per project, so a busy chat can "
                "push the classifier into a 429. Set GEMINI_CHAT_API_KEY from "
                "a different Google Cloud project for an independent quota."
            )
    elif chat_state["enabled"]:
        log.warning(
            "GEMINI_CHAT_ENABLED is on but no chat key is available; set "
            "GEMINI_CHAT_API_KEY, or GEMINI_CHAT_ALLOW_SHARED_KEY=1 to reuse "
            "the classifier's key. The assistant will not answer until then."
        )
    else:
        log.info("Conversational AI disabled (GEMINI_CHAT_ENABLED=0).")

    # The moderation layer, reported on its own for the same reason. What an
    # operator most needs from this line is whether text moderation is switched
    # on, because that is what decides whether anything is deleted at all.
    mod_state = ai_moderation.status()
    if mod_state["active"]:
        log.info(
            "Moderation AI active: model=%s daily_limit=%d used_today=%d "
            "delete_confidence=%.2f review_confidence=%.2f text=%s",
            mod_state["model"],
            mod_state["daily_limit"],
            mod_state["used_today"],
            mod_state["delete_confidence"],
            mod_state["review_confidence"],
            "on" if mod_state["text_enabled"] else "off",
        )
        if mod_state["shares_google_project"]:
            log.warning(
                "Moderation AI is using the classifier's key "
                "(GEMINI_MOD_ALLOW_SHARED_KEY=1). A busy group can push "
                "acquisition and chat into a 429. Set GEMINI_MOD_API_KEY from a "
                "different Google Cloud project for an independent quota."
            )
    else:
        log.info(
            "Moderation AI is not active (GEMINI_MOD_ENABLED=%s, key=%s). "
            "Text moderation requires an AI confirmation, so nothing is deleted "
            "automatically until this is configured — content that would have "
            "been deleted is reported for review instead.",
            "on" if mod_state["enabled"] else "off",
            "set" if mod_state["configured"] else "missing",
        )

    # The speech pipeline, its own line again.
    tr_state = transcribe.status()
    if tr_state["active"]:
        log.info(
            "Transcription active: model=%s daily_limit=%d used_today=%d "
            "command=/%s max_seconds=%d",
            tr_state["model"],
            tr_state["daily_limit"],
            tr_state["used_today"],
            config.TRANSCRIBE_COMMAND or "-",
            int(tr_state["max_seconds"]),
        )
    elif tr_state["enabled"]:
        log.warning(
            "TRANSCRIBE_ENABLED is on but no transcription key is available; "
            "voice messages addressed to the assistant will be answered with "
            "'I could not listen to that'."
        )
    else:
        log.info(
            "Transcription disabled (TRANSCRIBE_ENABLED=0); voice messages "
            "addressed to the assistant are reported as unreadable."
        )

    # ── The Gemini account pool ───────────────────────────────────────────
    #
    # Built here, after each workload above already asked for its pool while
    # reporting itself, so the operator gets one line per workload saying how
    # deep the pool is. Nothing is registered and nothing is handed over: the
    # pool has no way to reach Telegram, which is the point. The lines never
    # contain a credential — accounts are named by slot and by a masked tail —
    # which is what makes them safe to leave in a log, and the log is the only
    # place they go.
    gemini_pool.build_pools()
    for line in gemini_pool.startup_lines():
        log.info(line)
    # One key reaching two workloads is one Google project and therefore one
    # allowance, however separately the two workloads count their own calls.
    # Said out loud once at boot, because the alternative is discovering it as
    # an unexplained 429 on whichever workload happens to be busy.
    for masked, workloads in gemini_pool.shared_credentials():
        log.warning(
            "Gemini credential %s is used by more than one workload (%s). "
            "Google applies limits per project, so these share one allowance "
            "even though each workload keeps its own counters. Use a key from "
            "a different project for each workload to keep them independent.",
            masked,
            ", ".join(workloads),
        )
    for pool in gemini_pool.pools():
        health = pool.health()
        if not health["accounts"]:
            continue
        if health["empty"]:
            log.warning(
                "Gemini pool '%s' has no usable account (%d configured); this "
                "workload is on its safe fallback until one recovers.",
                pool.workload,
                health["accounts"],
            )
        elif health["degraded"]:
            log.warning(
                "Gemini pool '%s' has degraded to one usable account of %d.",
                pool.workload,
                health["accounts"],
            )
        elif health["accounts"] == 1:
            # Not a warning. A pool of one is the pool the operator configured,
            # and calling it critical on every boot is how a real degradation
            # later gets mistaken for the usual noise.
            log.info(
                "Gemini pool '%s' has a single account; there is no failover "
                "capacity behind it. Add <PREFIX>_2 to give it one.",
                pool.workload,
            )

    # The authorization model. The owner line is the one that matters: with no
    # owner every administrative command is refused, which is the correct
    # fail-closed behaviour and looks exactly like a broken feature unless it is
    # said out loud.
    if rbac.has_owner():
        log.info(
            "Authorization: owner=%s configured_admins=%d stored_admins=%d",
            rbac.owner_id(),
            rbac.configured_admin_count(),
            len(db.admin_list()),
        )
    else:
        log.warning(
            "OWNER_USER_ID is not set. Every administrative command "
            "(/promote, /demote, /ban, /mute, /warn, /del) will be refused. "
            "Set it to your own Telegram user id to enable them."
        )

    # Which of the two front doors is live. Said out loud at startup because the
    # failure it describes is silent by nature: with the AI down, administration
    # still works perfectly through the commands, and nothing about that looks
    # wrong until somebody tries to talk to it and gets no answer. "DEGRADED" is
    # the word that makes the difference visible.
    log.info("%s", admin_service.mode_line())


def main() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    os.makedirs(config.TMP_DIR, exist_ok=True)
    db.init()
    # Read the persisted Nexus state before anything can answer. Doing it here
    # rather than lazily on the first message is deliberate: a deployment that
    # was switched off must come back up switched off, and a lazy read would let
    # the first message arrive while the state was still unknown.
    nexus.load()
    # Read the room allowlist before anything can answer. This is also where a
    # first boot against an empty table seeds it from ``GROUP_IDS``, so an
    # existing deployment keeps serving its rooms with no downtime — and where a
    # revocation survives a restart, because the seed is guarded by "the table
    # has ever held a row" and not by "it is currently empty".
    groups.load()
    # Before anything opens a socket, so every later AI call — classifier and
    # conversation alike — resolves through the IPv6-first ordering. Best
    # effort: on a host without global IPv6 it declines and the bot runs
    # exactly as it did before.
    net.install_preference()

    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    # The update guard, and the reason it is a handler rather than a decorator
    # around the dispatcher: it has to run before *every* other handler, and the
    # only thing that is guaranteed to run before every handler is a handler in a
    # lower group. Group -1 is that group, and no other handler uses it.
    if config.UPDATE_DEDUP_ENABLED:
        app.add_handler(TypeHandler(Update, on_any_update), group=-1)

    app.add_handler(
        CallbackQueryHandler(on_report_delete, pattern=r"^report_delete$")
    )

    # The instant media-flood rule. Registered independently of every content
    # setting on purpose: it is abuse protection, decided from message metadata
    # alone, so it must keep working when text moderation, the filter or the
    # assistant are switched off. Its own group, before the content handlers, so
    # a burst is stopped before anything else looks at the messages.
    #
    # The filter lists exactly the kinds ``_burst_kind`` can name, and no more:
    # the handler would return immediately for a photo or a video, so waking it
    # for them would be a handler call that can never do anything. Named
    # `flood_media_filter`, not `media_filter`: a local called `media` would
    # shadow the app.media module for the whole of main().
    flood_media_filter = (
        filters.ANIMATION | filters.VIDEO_NOTE | filters.Sticker.ALL
    ) & filters.ChatType.GROUPS
    app.add_handler(MessageHandler(flood_media_filter, on_media_flood), group=0)

    # The credential-entry path. Group 0 — ahead of acquisition and, crucially,
    # ahead of the assistant's private-chat handler in group 2 — because it
    # raises ``ApplicationHandlerStop`` when it consumes a message and that is
    # the whole of the isolation guarantee: a key typed into the owner's private
    # chat is never handed to a model. The handler itself returns immediately for
    # anyone but the owner and for any message sent while no prompt is armed.
    app.add_handler(
        MessageHandler(key_entry_filter(), on_key_message),
        group=0,
    )

    if config.GROUP_TRIAL_ENABLED:
        # Its own group so it can never be skipped because a media handler in
        # group 0 happened to match first.
        app.add_handler(
            MessageHandler(acquisition_message_filter(), on_group_text), group=1
        )

    if config.GEMINI_CHAT_ENABLED:
        # The conversational assistant. Registered even when the key is missing
        # so that /reset keeps working, and in groups of its own so it cannot
        # be starved by, or starve, the handlers above. `on_group_chat` returns
        # immediately unless the message explicitly addresses the bot.
        #
        # `block=False` is what keeps a conversation from stalling the bot. A
        # reply can take up to GEMINI_CHAT_TIMEOUT_SECONDS, and the dispatcher
        # processes updates one at a time by default — so without this, one
        # person chatting would pause media moderation for the whole group.
        # Non-blocking runs the callback as its own task, so the rest of the bot
        # keeps working while a reply is being written.
        # From `chat_command_handlers`, the same function the published command
        # menu reads, so the two cannot drift apart.
        for command, handler in chat_command_handlers():
            app.add_handler(CommandHandler(command, handler))
        app.add_handler(
            MessageHandler(group_chat_filter(), on_group_chat, block=False), group=2
        )
        app.add_handler(
            MessageHandler(private_chat_filter(), on_private_text, block=False),
            group=2,
        )

    # ── Administration. Its own groups, after everything else.
    #
    # Group 3 holds the commands. Group 4 holds text moderation, which is
    # non-blocking and last on purpose: it is the only path that can delete a
    # person's words, so it must never be reached before acquisition and
    # conversation have had their say.
    app.add_handler(
        CallbackQueryHandler(on_admin_callback, pattern=r"^adm:")
    )
    # The key dashboard's own namespace, so the two dialogs can never be
    # confused for one another however a payload is crafted.
    app.add_handler(
        CallbackQueryHandler(on_key_callback, pattern=r"^gk:")
    )
    # The list comes from admin_command_handlers() rather than being written out
    # here, because the command menu published to Telegram is derived from that
    # same function. Two lists would drift, and the drift is invisible: a menu
    # entry whose command was never registered looks exactly like a broken bot.
    for command, handler in admin_command_handlers():
        app.add_handler(CommandHandler(command, handler), group=3)

    for command, handler in transcribe_command_handlers():
        app.add_handler(CommandHandler(command, handler), group=3)

    if config.MODERATION_ENABLED and config.MODERATION_TEXT_ENABLED:
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
                on_group_text_moderation,
                block=False,
            ),
            group=4,
        )

    # The pattern rules. Registered independently of the moderation layer on
    # purpose: they need no model, so they must keep working when the text
    # moderation AI is switched off, out of quota, or unreachable. Group 4, the
    # last group, so that a message already handled — or already deleted — by an
    # earlier handler is not acted on twice.
    if config.FILTER_ENABLED:
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
                on_group_filter,
                block=False,
            ),
            group=4,
        )

    # Only the update kinds something is actually registered for. Telegram
    # delivers everything else to nobody, and asking for it would be a queue of
    # updates with no handler.
    app.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.CALLBACK_QUERY,
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
