import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import (
    admin_service,
    admin_tools,
    ai_intent,
    ai_moderation,
    burst,
    chat,
    classifier,
    config,
    db,
    detector,
    gemini_pool,
    media,
    mod_policy,
    moderation,
    net,
    rbac,
    responses,
    transcribe,
    vpnbot,
)
from .decision import Decision, DecisionResult, default_engine

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("guardbot")

_pool = ThreadPoolExecutor(max_workers=config.MEDIA_WORKERS)
_engine = default_engine()
_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
# Instant-flood tracker. Bounded internally; see app/burst.py.
_bursts = burst.BurstTracker(
    window_seconds=config.BURST_WINDOW_SECONDS,
    max_items=config.BURST_MAX_ITEMS,
)

MUTED = ChatPermissions(can_send_messages=False)
FULL = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)

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
    )
    log.info(
        "Bot identity: id=%s username=%s name=%s aliases=%d",
        _bot_identity["id"],
        f"@{_bot_identity['username']}" if _bot_identity["username"] else "-",
        _bot_identity["name"] or "-",
        len(_bot_identity["aliases"]),
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


# Callback-data prefix for the admin-report self-delete button. Short and
# unique so it can never collide with the captcha button ("cap:<id>").
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


# ------------------------------------------------------------ captcha
async def on_member_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when someone joins. Works for public groups and join-requests."""
    cm = update.chat_member
    if cm.chat.id not in config.GROUP_IDS or not config.CAPTCHA_ENABLED:
        return

    old, new = cm.old_chat_member.status, cm.new_chat_member.status
    joined = old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    )
    if not joined:
        return

    user = cm.new_chat_member.user
    if user.is_bot or await is_admin(ctx, cm.chat.id, user.id):
        return

    chat_id = cm.chat.id
    try:
        await ctx.bot.restrict_chat_member(chat_id, user.id, permissions=MUTED)
    except TelegramError as e:
        log.warning("restrict failed for %s: %s", user.id, e)
        return

    text = config.CAPTCHA_TEXT.format(
        name=user.full_name, timeout=config.CAPTCHA_TIMEOUT_SEC
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(config.CAPTCHA_BUTTON, callback_data=f"cap:{user.id}")]]
    )
    msg = await ctx.bot.send_message(chat_id, text, reply_markup=kb)
    db.add_captcha(
        chat_id, user.id, msg.message_id, int(time.time()) + config.CAPTCHA_TIMEOUT_SEC
    )


async def on_captcha_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, target = q.data.split(":")
    if q.from_user.id != int(target):
        await q.answer("این دکمه برای شما نیست.", show_alert=True)
        return

    chat_id = q.message.chat.id
    if not db.get_captcha(chat_id, q.from_user.id):
        await q.answer("مهلت تمام شده.", show_alert=True)
        return

    try:
        await ctx.bot.restrict_chat_member(chat_id, q.from_user.id, permissions=FULL)
    except TelegramError as e:
        log.warning("unrestrict failed: %s", e)
        await q.answer("خطا، دوباره امتحان کن.", show_alert=True)
        return

    db.remove_captcha(chat_id, q.from_user.id)
    await q.answer("✅ تأیید شد")
    try:
        await q.message.delete()
    except TelegramError:
        pass


async def captcha_reaper(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every 10s: kick users who didn't solve the captcha in time."""
    for chat_id, user_id, msg_id in db.expired_captchas(int(time.time())):
        db.remove_captcha(chat_id, user_id)
        try:
            # ban + unban = kick (user can rejoin later)
            await ctx.bot.ban_chat_member(chat_id, user_id)
            await ctx.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except TelegramError as e:
            log.warning("kick failed %s: %s", user_id, e)
        try:
            await ctx.bot.delete_message(chat_id, msg_id)
        except TelegramError:
            pass


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


# ------------------------------------------------------------ media
def _thumb_of(obj):
    return getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)


def _pick_media(msg):
    """Returns (file_obj, kind, is_video_like) or None."""
    if msg.photo:
        return msg.photo[-1], "photo", False
    if msg.video:
        return msg.video, "video", True
    if msg.animation:
        return msg.animation, "gif", True
    if msg.video_note:
        return msg.video_note, "video_note", True
    if msg.sticker:
        st = msg.sticker
        if st.is_animated:
            # .tgs (Lottie) can't be decoded by ffmpeg. Telegram attaches a
            # static preview thumbnail, so analyse that instead of dropping
            # the sticker entirely.
            th = _thumb_of(st)
            if th:
                return th, "animated_sticker", False
            return None
        return st, "sticker", bool(st.is_video)
    if msg.document and msg.document.mime_type:
        mt = msg.document.mime_type
        if mt.startswith("image/"):
            return msg.document, "image_file", False
        if mt.startswith("video/"):
            return msg.document, "video_file", True
    return None


def _analyze_blocking(path: str, is_video: bool, work_dir: str) -> detector.MediaAnalysis:
    if is_video:
        return detector.analyze_video(path, work_dir)
    return detector.analyze_image(path)


def _explicit_report_text(chat, user, msg, kind, result, note, verdict=None, outcome=None) -> str:
    """Persian admin report for one confirmed deletion.

    The detection line reflects *which* signal fired: the anatomical NudeNet
    class when there is one, otherwise the scene-level classifier. The reason
    sentence matches too, so the report never claims genital evidence that the
    detector did not actually find.

    When the moderation AI confirmed the deletion its own classification and
    confidence are shown as well, because "who decided this" is the first
    question an operator asks about a deletion they disagree with — and with two
    signals in the pipeline the answer is no longer obvious from the score.
    """
    matched = result.matched
    if matched is not None:
        label = matched.label
        score = matched.score
        reason = "محتوای صریح بزرگسالان با نمایش واضح ناحیه تناسلی تشخیص داده شد."
    else:
        # scene-stage deletion: no anatomical class was detected
        label = "SCENE_NSFW"
        score = result.scene_nsfw if result.scene_nsfw is not None else 0.0
        reason = (
            "محتوای جنسی/صریح در صحنه تشخیص داده شد "
            "(بدون شناسایی ناحیه تناسلی)."
        )
    username = f"@{user.username}" if getattr(user, "username", None) else "-"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    ai_line = ""
    if verdict is not None and verdict.decided:
        ai_line = (
            f"🤖 تأیید هوش مصنوعی: <b>{verdict.classification}</b> "
            f"({verdict.confidence:.2f})\n"
            f"   دسته: {verdict.category or '-'}\n"
        )
    policy_line = ""
    if outcome is not None:
        policy_line = f"⚖️ سیاست: <code>{outcome.reason}</code>\n"

    return (
        f"🚨 <b>حذف محتوای صریح</b>\n\n"
        f"👤 کاربر: {mention(user)}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"🔗 Username: {username}\n"
        f"💬 Chat ID: <code>{chat.id}</code>\n"
        f"📩 Message ID: <code>{msg.message_id}</code>\n\n"
        f"📦 نوع محتوا: <b>{kind}</b> {note}\n"
        f"🔎 تشخیص محلی: <b>{label}</b>\n"
        f"📊 امتیاز: <b>{score:.2f}</b>\n"
        f"{ai_line}"
        f"{policy_line}\n"
        f"⛔ دلیل:\n"
        f"{reason}\n\n"
        f"✅ اقدام:\n"
        f"پیام از گروه حذف شد.\n\n"
        f"ℹ️ بررسی دستی:\n"
        f"در صورت خطای تشخیص، مدیر می‌تواند این مورد را بررسی کند.\n\n"
        f"🕒 زمان: {now}"
    )


async def _send_explicit_report(
    ctx, chat, user, msg, kind, analysis, result, note, verdict=None, outcome=None
) -> None:
    """Admin report for a confirmed deletion, with a representative frame.

    Only a DELETE_WARN outcome that actually deleted reaches this function.
    Evidence is uploaded from the per-job temp dir and the file is removed by the
    caller's finally.
    """
    if not config.ADMIN_LOG_CHAT:
        return
    text = _explicit_report_text(chat, user, msg, kind, result, note, verdict, outcome)

    # Evidence frame: the frame behind the matched anatomical detection, or -
    # for a scene-stage deletion, where there is no matched detection - the
    # frame that produced the scene score.
    if result.matched is not None:
        evidence = analysis.evidence_frame(result.matched.label)
    else:
        evidence = analysis.scene_frame
    if evidence and os.path.exists(evidence):
        for method, field in (("send_photo", "photo"), ("send_document", "document")):
            try:
                with open(evidence, "rb") as fh:
                    await getattr(ctx.bot, method)(
                        config.ADMIN_LOG_CHAT,
                        **{field: fh},
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=_report_keyboard(),
                    )
                return
            except TelegramError as e:
                log.warning("%s evidence failed: %s", method, e)

    # never leave the admin without the report itself
    await report(ctx, text)


# ------------------------------------------------- AI moderation
async def _assess_media_with_ai(
    path: str, work_dir: str, kind: str, result, note: str
) -> ai_moderation.ModerationVerdict | None:
    """The moderation AI's opinion on this media, or None.

    **When it is asked**, and why that condition is the whole design: only when
    the local stage has something to say — a REVIEW or an EXPLICIT. That is both
    the cheapest rule (an ordinary photo costs nothing) and the one that matters,
    because the AI's value here is that it can *disagree*. Asking it about
    content nobody doubted would spend the quota to confirm the obvious.

    It is also why a local EXPLICIT that the AI declines now ends in REVIEW
    rather than a deletion: this function is the second opinion that can say no.

    The file is already on disk — the local detector downloaded it — so the parts
    are built from the path rather than fetched from Telegram a second time.

    Never raises, and returns None on any failure, which the policy reads as
    "not confirmed" and therefore does not delete.
    """
    if not config.MODERATION_MEDIA_ENABLED:
        return None
    if not ai_moderation.is_enabled():
        return None
    if result.decision is Decision.SAFE and result.scene_nsfw is None:
        return None

    try:
        bundle = media.build_from_path(path, kind, work_dir=work_dir)
    except Exception as e:  # noqa: BLE001 - never let this break the pipeline
        log.warning("media preparation for the moderation AI failed: %s", e)
        return None

    if not bundle.ok:
        # The media could not be prepared. That is a reason to leave the content
        # alone, not a reason to delete it, and the log line says which.
        log.info(
            "moderation AI skipped kind=%s: %s", kind, bundle.note or "not preparable"
        )
        return None

    parts = [{"mime_type": p.mime_type, "data": p.data} for p in bundle.parts]
    try:
        verdict = await ai_moderation.assess_media(parts, kind)
    except Exception as e:  # noqa: BLE001
        log.warning("moderation AI call failed: %s", e)
        return None
    if bundle.reduced_to_frames or bundle.thumbnail_only:
        # The verdict was formed from a reduction of the media, and the report
        # has to say so — an operator deciding whether a deletion was right must
        # know it was made on frames or a preview rather than the file.
        log.info(
            "moderation AI verdict is about a reduction of the media: %s",
            bundle.note,
        )
    return verdict


async def _notify_review(
    ctx, chat, user, msg, kind, result, verdict, outcome
) -> None:
    """Tell the operator about something the policy declined to act on.

    REVIEW is where every disputed and every uncertain case now lands, so this
    is the channel that makes the new policy observable: without it, "the bot
    stopped deleting" and "the bot stopped working" would look the same from the
    outside. It carries no media and no message text — an identifier, the two
    signals, and the reason.
    """
    if not config.MODERATION_REVIEW_NOTIFY or not config.ADMIN_LOG_CHAT:
        return
    ai_line = (
        f"🤖 هوش مصنوعی: <b>{verdict.classification}</b> ({verdict.confidence:.2f})"
        f"{' ⚠️ نامطمئن' if verdict.uncertain else ''}\n"
        if verdict is not None and verdict.decided
        else "🤖 هوش مصنوعی: پاسخی نداد\n"
    )
    local_line = (
        f"🔎 محلی: <b>{result.matched.label}</b> ({result.matched.score:.2f})\n"
        if result.matched is not None
        else f"🔎 محلی: صحنه ({result.scene_nsfw:.2f})\n"
        if result.scene_nsfw is not None
        else "🔎 محلی: -\n"
    )
    text = (
        f"👀 <b>نیازمند بررسی دستی</b>\n\n"
        f"👤 {mention(user)} (<code>{user.id}</code>)\n"
        f"💬 Chat ID: <code>{chat.id}</code>\n"
        f"📩 Message ID: <code>{getattr(msg, 'message_id', '-')}</code>\n"
        f"📦 نوع: <b>{kind}</b>\n"
        f"{local_line}"
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
    a flood; a photo is still checked by the sexual-content detector on its own.
    The names returned here are the values used in ``BURST_MEDIA_KINDS``.
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


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Media pipeline: download -> detect -> decide -> delete -> report -> cleanup."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or chat.id not in config.GROUP_IDS or not user:
        return
    # Owner exemption only (WHITELIST_USER_IDS). Telegram admins are NOT
    # exempt: both the sexual-content moderation and the anti-flood rule apply
    # to them. Telegram itself decides whether the bot may act on an admin's
    # message, and every failure is handled fail-open below.
    if user.id in config.WHITELIST_USER_IDS:
        return

    # ---- instant media flood: decided from message metadata, no download ----
    bkind = _burst_kind(msg)
    if config.BURST_ENABLED and bkind is not None:
        decision = _bursts.record(
            chat.id, user.id, getattr(msg, "message_id", 0), bkind,
            kinds=config.BURST_MEDIA_KINDS,
        )
        if decision.is_burst:
            try:
                await _enforce_burst(ctx, chat, user, decision)
            except Exception:
                # fail open: a flood-handling error never escalates
                log.exception("burst enforcement failed")
            return

    picked = _pick_media(msg)
    if not picked:
        log.info(
            "media SKIPPED chat=%s message=%s reason=unsupported_or_undecodable",
            chat.id, getattr(msg, "message_id", "-"),
        )
        return
    obj, kind, is_video = picked

    os.makedirs(config.TMP_DIR, exist_ok=True)
    # Every job owns one temp dir; the finally below guarantees its removal on
    # success, detector error, decode error, Telegram error, cancellation or
    # any other exception.
    work_dir = tempfile.mkdtemp(
        prefix=f"job_{chat.id}_{getattr(msg, 'message_id', 0)}_", dir=config.TMP_DIR
    )
    note = ""

    try:
        size_mb = (getattr(obj, "file_size", 0) or 0) / 1024 / 1024
        target = obj
        target_is_video = is_video
        if kind == "animated_sticker":
            note = "(فقط پیش‌نمایش استیکر متحرک بررسی شد)"
        if size_mb > config.MAX_DOWNLOAD_MB:
            # too big for Bot API: fall back to the thumbnail
            th = _thumb_of(obj)
            if not th:
                log.warning(
                    "media SKIPPED chat=%s message=%s reason=oversized_without_thumbnail",
                    chat.id, getattr(msg, "message_id", "-"),
                )
                return
            target, target_is_video = th, False
            note = "(فقط تامبنیل بررسی شد)"

        f = await ctx.bot.get_file(target.file_id)
        path = os.path.join(work_dir, "media")
        await f.download_to_drive(path)

        loop = asyncio.get_running_loop()
        analysis = await loop.run_in_executor(
            _pool, _analyze_blocking, path, target_is_video, work_dir
        )

        result = _engine.decide(analysis)

        # The moderation AI's second opinion, when there is something for it to
        # have an opinion about. `_assess_media_with_ai` explains the condition;
        # the short version is that it is asked exactly when the local stage has
        # something to say, which is both the cheapest rule and the one that
        # matters — it is the *disagreement* that stops a false positive.
        verdict = await _assess_media_with_ai(
            path, work_dir, kind, result, note
        )

        outcome = mod_policy.decide(
            mod_policy.PolicyInput(
                local=result,
                ai=verdict,
                media_kind=kind,
                is_media=True,
            )
        )

        log.info(
            "media chat=%s user=%s kind=%s detector=%s frames=%d decision=%s "
            "source=%s class=%s confidence=%.2f detections=%s scene=%s "
            "scene_frames=%d reason=%s | policy=%s",
            chat.id, user.id, kind, config.DETECTOR_BACKEND, analysis.frames_checked,
            result.decision.value,
            result.source,
            result.matched.label if result.matched else "-",
            result.matched.score if result.matched else 0.0,
            analysis.detections_summary(),
            f"{result.scene_nsfw:.2f}" if result.scene_nsfw is not None else "-",
            analysis.scene_frames,
            result.reason,
            mod_policy.describe(outcome),
        )

        if outcome.allows:
            return

        if outcome.reviews:
            # Ambiguous, disputed, or unconfirmed: logged and reported, never
            # acted on. This is where every false positive now lands — the
            # content stays, a human can look, and nobody is punished for a
            # score crossing a line.
            await _notify_review(ctx, chat, user, msg, kind, result, verdict, outcome)
            return

        # The only branch that destroys anything. `enforce_result` is what keeps
        # the executor's safety contract — a failed delete applies no strike and
        # no restriction — and the policy outcome is the only thing that can
        # reach it.
        enforced = mod_policy.enforce_result(outcome, result)
        result = enforced

        outcome_enforced = await moderation.enforce(
            result,
            delete_media=lambda: msg.delete(),
            record_confirmed=lambda: db.add_strike(chat.id, user.id),
        )

        if not outcome_enforced.deleted:
            log.error(
                "DELETE_FAILED chat=%s message=%s class=%s confidence=%.2f error=%s",
                chat.id, getattr(msg, "message_id", "-"),
                result.matched.label if result.matched else "-",
                result.matched.score if result.matched else 0.0,
                outcome_enforced.reason,
            )
            return

        mark_deleted(chat.id, getattr(msg, "message_id", 0))
        log.info(
            "DELETE_SUCCESS chat=%s message=%s class=%s confidence=%.2f policy=%s",
            chat.id, getattr(msg, "message_id", "-"),
            result.matched.label if result.matched else "-",
            result.matched.score if result.matched else 0.0,
            outcome.reason,
        )
        await _send_explicit_report(
            ctx, chat, user, msg, kind, analysis, result, note, verdict, outcome
        )

        # A failed deletion returns above, so a strike is only ever recorded for
        # content that was actually removed. A database error leaves
        # `strike` as None and changes nothing else. Note `outcome_enforced` is
        # the *executor's* result, not the policy's — the policy decided to
        # delete, and this is what actually happened when it tried.
        if outcome_enforced.strike is not None:
            log.info(
                "VIOLATION chat=%s user=%s count=%d",
                chat.id, user.id, outcome_enforced.strike,
            )
            restricted = False
            if outcome_enforced.strike >= config.VIOLATION_MUTE_AFTER:
                restricted = await _restrict_user(ctx, chat.id, user.id)
                log.info(
                    "VIOLATION_RESTRICT chat=%s user=%s count=%d minutes=%s applied=%s",
                    chat.id, user.id, outcome_enforced.strike, config.MUTE_MINUTES,
                    restricted,
                )
            notice_id = await _send_user_notice(
                ctx,
                chat.id,
                _format_notice(
                    config.VIOLATION_WARNING_TEXT,
                    name=mention(user),
                    count=outcome_enforced.strike,
                    max=config.VIOLATION_MUTE_AFTER,
                ),
            )
            # The test account is restricted for real above; only then is the
            # unrestrict scheduled, and only for that one user id.
            if restricted:
                _schedule_test_unrestrict(ctx, chat.id, user.id, notice_id)
    except Exception:
        # fail open: never delete or punish because of an internal error
        log.exception("media pipeline failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


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


async def _send_chat(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
) -> None:
    """Send one conversational message.

    Escaped, because the body is model output and Telegram is asked to parse
    HTML: an unescaped angle bracket would be a parse error at best. The typing
    action is best-effort — it is a courtesy, and failing to show it must not
    cost the reply.
    """
    safe = html.escape(text)
    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except TelegramError:
        pass
    try:
        await ctx.bot.send_message(
            chat_id,
            safe,
            parse_mode="HTML",
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        log.warning("chat reply failed: %s", exc)


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
) -> tuple[list | None, str, str, bool, str]:
    """Turn this message's attachment into (parts, kind, text, want_voice, why).

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

    Never raises.
    """
    ref = media.describe(msg)
    if ref is None:
        return None, "", _message_text(msg), False, PREPARE_OK

    if ref.is_transcribable:
        transcript = await transcribe.transcribe_ref(
            ref, download=lambda fid: _download_file(ctx, fid)
        )
        if transcript.ok:
            return None, ref.kind, transcript.text, True, PREPARE_OK
        if transcript.no_speech:
            return None, ref.kind, "", False, PREPARE_NO_SPEECH
        log.info(
            "conversation: could not transcribe (%s)",
            transcript.error or transcript.skipped,
        )
        return None, ref.kind, "", False, PREPARE_UNREADABLE

    if not ref.is_visual:
        # A document we can neither read nor transcribe. Not a failure of ours,
        # but there is nothing to send either.
        return None, ref.kind, _message_text(msg), False, PREPARE_UNREADABLE

    try:
        bundle = await media.build(
            ref,
            download=lambda fid: _download_file(ctx, fid),
            work_dir=work_dir,
            max_parts=int(config.GEMINI_CHAT_MEDIA_MAX_PARTS),
        )
    except Exception as e:  # noqa: BLE001 - never break the handler
        log.warning("conversation media preparation failed: %s", e)
        return None, ref.kind, _message_text(msg), False, PREPARE_UNREADABLE

    if not bundle.ok:
        log.info("conversation media unreadable kind=%s: %s", ref.kind, bundle.note)
        return None, ref.kind, _message_text(msg), False, PREPARE_UNREADABLE

    parts = [{"mime_type": p.mime_type, "data": p.data} for p in bundle.parts]
    return parts, ref.kind, _message_text(msg), False, PREPARE_OK


def _reply_context(msg) -> tuple[int, str, int]:
    """Who the current message is replying to, if anyone.

    Returned as plain ids and a display name rather than the message object, so
    nothing downstream can be tempted to read the replied-to *content*. The
    assistant is told who was replied to; it is not handed the message, because
    the only thing an administrative request needs is the id.
    """
    replied = getattr(msg, "reply_to_message", None)
    if replied is None:
        return 0, "", 0
    author = getattr(replied, "from_user", None)
    if author is None:
        return 0, "", int(getattr(replied, "message_id", 0) or 0)
    return (
        int(getattr(author, "id", 0) or 0),
        getattr(author, "full_name", "") or "",
        int(getattr(replied, "message_id", 0) or 0),
    )


def _ai_admin_turn(update, ctx, msg, room, user):
    """The tools, trusted context and tool-runner for one conversational turn.

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
    """
    if not config.ADMIN_AI_ENABLED:
        return None, "", None

    try:
        principal = rbac.resolve(user.id)
        if not principal.is_admin and not config.ADMIN_TOOL_GUEST_TOOLS:
            return None, "", None
        names = admin_tools.tool_names_for(principal)
        if not names:
            return None, "", None

        reply_user_id, reply_name, reply_message_id = _reply_context(msg)
        context = admin_tools.build_context(
            principal=principal,
            chat_id=room.id,
            chat_title=getattr(room, "title", "") or "",
            chat_type=str(getattr(room, "type", "") or ""),
            message_id=int(getattr(msg, "message_id", 0) or 0),
            reply_user_id=reply_user_id,
            reply_name=reply_name,
            reply_message_id=reply_message_id,
            bot_username=getattr(ctx.bot, "username", "") or "",
        )
        tools = admin_tools.declarations_for(principal)
    except Exception:  # noqa: BLE001 - degrade to an ordinary conversation
        log.exception("could not build the administrative tool set")
        return None, "", None

    gateway = TelegramGateway(ctx)

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
                chat_id=room.id,
                reply_user_id=reply_user_id,
                reply_name=reply_name,
                bot_id=getattr(ctx.bot, "id", 0),
                gateway=gateway,
            )

        if not config.ADMIN_AI_ENABLED:
            return {"error": "AI administration is switched off"}

        request = admin_tools.parse_write_call(
            name,
            args,
            actor_id=user.id,
            chat_id=room.id,
            message_id=int(getattr(msg, "message_id", 0) or 0),
            request_id=admin_service.new_request_id(),
        )
        if request is None:
            log.info("refused malformed tool call %s args=%s", name, sorted(args or {}))
            return {
                "ok": False,
                "error": "the request was malformed, so nothing was executed",
            }

        result = await admin_service.execute(
            request,
            gateway,
            actor=principal,
            bot_id=getattr(ctx.bot, "id", 0),
        )
        log.info(
            "ai admin tool=%s actor=%s outcome=%s ok=%s",
            name, user.id, result.outcome, result.ok,
        )
        return {
            "ok": result.ok,
            "operation": result.operation,
            "outcome": result.outcome,
            "target_user_id": result.target_id,
            # The Persian sentence and the English gloss: the first is what to
            # convey, the second is why, and the model is expected to write its
            # own words around them rather than repeat either.
            "message": result.message,
            "explanation": admin_service.explain(result),
        }

    return tools, context, on_tool


async def _answer_conversationally(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, reply_to: int | None = None
) -> None:
    """The whole conversational policy, in one place.

    Text and media take the same road once the message has been prepared: the
    only difference is that an attachment contributes parts and, for voice, a
    transcript instead of a body.
    """
    msg = update.effective_message
    room = update.effective_chat
    user = update.effective_user
    if not msg or not room or not user or user.is_bot:
        return

    # A message the moderator just deleted must not be answered. Without this
    # an explicit photo addressed to the bot would be removed and then replied
    # to, which is both confusing and a reference to content that is gone.
    if was_deleted(room.id, getattr(msg, "message_id", 0)):
        log.info("chat skipped: the message was just deleted by moderation")
        return

    parts: list | None = None
    kind = ""
    text = _message_text(msg)
    want_voice = False
    problem = PREPARE_OK

    if media.describe(msg) is not None:
        work_dir = tempfile.mkdtemp(
            prefix=f"chat_{room.id}_{getattr(msg, 'message_id', 0)}_",
            dir=config.TMP_DIR,
        )
        try:
            parts, kind, text, want_voice, problem = await _prepare_conversation_media(
                ctx, msg, work_dir
            )
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
            # them a second attempt.
            await _send_chat(
                ctx,
                room.id,
                config.TRANSCRIBE_EMPTY_TEXT
                if problem == PREPARE_NO_SPEECH
                else config.GEMINI_CHAT_UNREADABLE_TEXT,
                reply_to,
            )
            return

    # The administrative half of this turn. Built from server-side values only,
    # and empty for a room where the person asking is not an administrator —
    # which is the normal case, and costs one dictionary lookup.
    tools, context, on_tool = _ai_admin_turn(update, ctx, msg, room, user)

    result = await chat.reply(
        room.id,
        user.id,
        text,
        parts=parts,
        kind=kind,
        want_voice=want_voice,
        tools=tools,
        context=context,
        on_tool=on_tool,
    )
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
            if await _send_voice(ctx, room.id, result.voice, reply_to):
                return
            # The upload failed; the text is still the answer and is sent below.
            log.warning("voice reply failed; falling back to text")
        await _send_chat(ctx, room.id, result.text, reply_to)
        return

    log.info(
        "chat declined for %s in %s reason=%s",
        user.id,
        room.id,
        result.error or result.skipped,
    )
    # Silent for the reasons that are nobody's business — a switched-off feature
    # should not announce itself every time somebody says hello.
    if result.message:
        await _send_chat(ctx, room.id, result.message, reply_to)


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


async def on_group_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The assistant, in a group. Reached only by an explicit address."""
    msg = update.effective_message
    if not msg or not _addressed_to_bot(msg, ctx):
        return
    await _answer_conversationally(update, ctx, reply_to=msg.message_id)


async def on_private_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A private message to the bot is a conversation, by definition."""
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
    await _send_chat(ctx, room.id, config.GEMINI_CHAT_START_TEXT.format(name=name))


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
    """Write one audit row. Never raises into the handler.

    An audit row that cannot be written must not be the reason a moderation
    action fails, and it must not be the reason one *succeeds* either — so this
    logs the failure and returns.
    """
    try:
        db.audit_write(
            actor_id,
            action,
            outcome=outcome,
            target_id=target_id,
            chat_id=chat_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        log.exception("audit write failed action=%s outcome=%s", action, outcome)


def _deny_text(decision: rbac.Decision) -> str:
    """The Persian sentence for a refusal. One key, one sentence."""
    return {
        rbac.REASON_NO_OWNER: config.ADMIN_NOT_CONFIGURED_TEXT,
        rbac.REASON_OWNER_PROTECTED: config.ADMIN_OWNER_PROTECTED_TEXT,
        rbac.REASON_HIGHER_RANK: config.ADMIN_HIGHER_RANK_TEXT,
    }.get(decision.reason, config.ADMIN_DENIED_TEXT)


async def _bot_right(ctx, chat_id: int, right: str) -> bool:
    """Whether the bot itself holds a Telegram administrator right in a chat.

    Checked before attempting an operation the API will refuse, so a refusal can
    be reported as "I do not have the permission here" rather than as a generic
    failure. Telegram still enforces it; this only makes the message useful.
    """
    try:
        me = await ctx.bot.get_chat_member(chat_id, ctx.bot.id)
    except TelegramError as e:
        log.warning("could not read my own chat member status: %s", e)
        return False
    return bool(getattr(me, right, False))


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
        return {
            "user_id": int(user_id),
            "telegram_status": name or str(status),
            "is_telegram_admin": name in ("administrator", "creator"),
            "custom_title": getattr(member, "custom_title", "") or "",
            "is_member": bool(getattr(member, "is_member", name != "left")),
            "can_send_messages": getattr(
                member, "can_send_messages", name not in ("restricted", "kicked")
            ),
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
    await _reply_in_group(ctx, room.id, gemini_pool.status_report(),
                          reply_to=msg.message_id)


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
# The local detectors see images only, so without this there is no content
# moderation for text at all. It is off by default (see
# MODERATION_TEXT_ENABLED) and it is deliberately the last handler group: it
# must never run before acquisition or conversation have had their say, because
# it is the only path that can delete a person's words.
#
# There is no local signal for text, so the policy's `local` input is None. The
# rule that follows from that is worth stating: with no local evidence, the only
# thing that can produce a deletion is a confident AI verdict, which is exactly
# the same bar media has to clear when the AI confirms it.
_NO_LOCAL = DecisionResult(Decision.SAFE, "no local stage for text")


async def on_group_text_moderation(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """The moderation AI's opinion on one group message, and the policy on it."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot:
        return
    if chat.id not in config.GROUP_IDS:
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
    outcome = mod_policy.decide(
        mod_policy.PolicyInput(local=None, ai=verdict, is_media=False)
    )
    log.info(
        "text moderation chat=%s user=%s policy=%s",
        chat.id, user.id, mod_policy.describe(outcome),
    )

    if outcome.allows:
        return
    if outcome.reviews:
        await _notify_review(ctx, chat, user, msg, "text", _NO_LOCAL, verdict, outcome)
        return

    enforced = mod_policy.enforce_result(outcome, None)
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
    notice_id = await _send_user_notice(
        ctx,
        chat.id,
        _format_notice(
            config.VIOLATION_WARNING_TEXT,
            name=mention(user),
            count=db.get_strikes(chat.id, user.id),
            max=config.VIOLATION_MUTE_AFTER,
        ),
    )
    if result.strike is not None and result.strike >= config.VIOLATION_MUTE_AFTER:
        # The same escalation the media path uses. It is *not* an automatic ban
        # and not an automatic long mute: it is the configured, documented,
        # timed restriction, applied only to a message that was actually
        # deleted — and only because the AI confirmed it.
        restricted = await _restrict_user(ctx, chat.id, user.id)
        if restricted:
            _schedule_test_unrestrict(ctx, chat.id, user.id, notice_id)


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
    if user.is_bot or chat.id not in config.GROUP_IDS:
        return

    # A message aimed at the bot is a conversation, not an intent. This is the
    # boundary between the two AI policies: the assistant has its own handler in
    # its own group, and returning here is what stops one message from getting
    # both a chat reply and a trial offer.
    if _chat_active() and _addressed_to_bot(msg, ctx):
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
async def post_init(app: Application) -> None:
    app.job_queue.run_repeating(captcha_reaper, interval=10, first=10)
    # Who we are, from Telegram rather than from configuration. Done first,
    # because the alias matching that decides whether the assistant answers
    # depends on it and a failed getMe must be visible in the log rather than
    # discovered as "the bot stopped responding to mentions".
    await load_identity(app)
    log.info(
        "GuardBot started. Groups: %s | explicit classes: %s",
        config.GROUP_IDS,
        sorted(config.EXPLICIT_CLASSES),
    )
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
    # operator most needs from this line is the *policy mode*, because that is
    # what decides whether anything is deleted at all.
    mod_state = ai_moderation.status()
    if mod_state["active"]:
        log.info(
            "Moderation AI active: model=%s daily_limit=%d used_today=%d "
            "delete_confidence=%.2f review_confidence=%.2f text=%s media=%s",
            mod_state["model"],
            mod_state["daily_limit"],
            mod_state["used_today"],
            mod_state["delete_confidence"],
            mod_state["review_confidence"],
            "on" if mod_state["text_enabled"] else "off",
            "on" if mod_state["media_enabled"] else "off",
        )
        if mod_state["shares_google_project"]:
            log.warning(
                "Moderation AI is using the classifier's key "
                "(GEMINI_MOD_ALLOW_SHARED_KEY=1). This is the heaviest of the "
                "four workloads, so a busy group can push acquisition and chat "
                "into a 429. Set GEMINI_MOD_API_KEY from a different Google "
                "Cloud project for an independent quota."
            )
    else:
        log.info(
            "Moderation AI is not active (GEMINI_MOD_ENABLED=%s, key=%s). "
            "Media deletions require AI confirmation, so nothing is deleted "
            "automatically until this is configured — content that would have "
            "been deleted is reported for review instead.",
            "on" if mod_state["enabled"] else "off",
            "set" if mod_state["configured"] else "missing",
        )

    if not config.MODERATION_REQUIRE_AI_CONFIRM:
        # A loud line, because this is the one configuration in which an
        # uncalibrated local score can still destroy somebody's message.
        log.warning(
            "MODERATION_REQUIRE_AI_CONFIRM is OFF: the local detector may "
            "delete on its own at MODERATION_LOCAL_HARD_THRESHOLD=%.2f. This is "
            "the configuration that produced the false positives; the default "
            "requires the moderation AI to agree.",
            float(config.MODERATION_LOCAL_HARD_THRESHOLD),
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


def main() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    os.makedirs(config.TMP_DIR, exist_ok=True)
    db.init()
    # Before anything opens a socket, so every later AI call — classifier and
    # conversation alike — resolves through the IPv6-first ordering. Best
    # effort: on a host without global IPv6 it declines and the bot runs
    # exactly as it did before.
    net.install_preference()
    if config.MEDIA_ENABLED:
        detector.load_model()

    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    app.add_handler(
        ChatMemberHandler(on_member_update, ChatMemberHandler.CHAT_MEMBER)
    )
    app.add_handler(CallbackQueryHandler(on_captcha_click, pattern=r"^cap:\d+$"))
    app.add_handler(
        CallbackQueryHandler(on_report_delete, pattern=r"^report_delete$")
    )

    if config.MEDIA_ENABLED:
        # Named `visual_media_filter`, not `media_filter`: a local called
        # `media` would shadow the app.media module for the whole of main().
        visual_media_filter = (
            filters.PHOTO
            | filters.VIDEO
            | filters.ANIMATION
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.Document.IMAGE
            | filters.Document.VIDEO
        ) & filters.ChatType.GROUPS
        app.add_handler(MessageHandler(visual_media_filter, on_media), group=0)

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
        # person chatting would pause captchas and media moderation for the
        # whole group. Non-blocking runs the callback as its own task, so the
        # rest of the bot keeps working while a reply is being written.
        app.add_handler(CommandHandler("start", on_chat_start))
        app.add_handler(CommandHandler("reset", on_chat_reset))
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
    for command, handler in (
        ("whoami", cmd_whoami),
        ("admins", cmd_admins),
        ("pool", cmd_pool),
        ("promote", cmd_promote),
        ("demote", cmd_demote),
        ("ban", cmd_ban),
        ("unban", cmd_unban),
        ("mute", cmd_mute),
        ("unmute", cmd_unmute),
        ("warn", cmd_warn),
        ("del", cmd_delete),
    ):
        app.add_handler(CommandHandler(command, handler), group=3)

    if config.TRANSCRIBE_COMMAND:
        app.add_handler(
            CommandHandler(config.TRANSCRIBE_COMMAND, on_transcribe_command),
            group=3,
        )

    if config.MODERATION_ENABLED and config.MODERATION_TEXT_ENABLED:
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
                on_group_text_moderation,
                block=False,
            ),
            group=4,
        )

    # chat_member updates must be requested explicitly
    app.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.CALLBACK_QUERY,
            Update.CHAT_MEMBER,
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
