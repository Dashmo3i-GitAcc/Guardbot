import asyncio
import logging
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db, detector, moderation
from .decision import Decision, default_engine

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


async def report(ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not config.ADMIN_LOG_CHAT:
        return
    try:
        await ctx.bot.send_message(config.ADMIN_LOG_CHAT, text, parse_mode="HTML")
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


def _explicit_report_text(chat, user, msg, kind, result, note) -> str:
    matched = result.matched
    label = matched.label if matched else "-"
    score = matched.score if matched else 0.0
    username = f"@{user.username}" if getattr(user, "username", None) else "-"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"🚨 <b>حذف محتوای صریح</b>\n\n"
        f"👤 کاربر: {mention(user)}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"🔗 Username: {username}\n"
        f"💬 Chat ID: <code>{chat.id}</code>\n"
        f"📩 Message ID: <code>{msg.message_id}</code>\n\n"
        f"📦 نوع محتوا: <b>{kind}</b> {note}\n"
        f"🔎 تشخیص: <b>{label}</b>\n"
        f"📊 امتیاز: <b>{score:.2f}</b>\n\n"
        f"⛔ دلیل:\n"
        f"محتوای صریح بزرگسالان با نمایش واضح ناحیه تناسلی تشخیص داده شد.\n\n"
        f"✅ اقدام:\n"
        f"پیام از گروه حذف شد.\n\n"
        f"ℹ️ بررسی دستی:\n"
        f"در صورت خطای تشخیص، مدیر می‌تواند این مورد را بررسی کند.\n\n"
        f"🕒 زمان: {now}"
    )


async def _send_explicit_report(ctx, chat, user, msg, kind, analysis, result, note) -> None:
    """Admin report for a confirmed deletion, with a representative frame.

    Only EXPLICIT + DELETE_SUCCESS reaches this function. Evidence is uploaded
    from the per-job temp dir and the file is removed by the caller's finally.
    """
    if not config.ADMIN_LOG_CHAT:
        return
    text = _explicit_report_text(chat, user, msg, kind, result, note)

    evidence = analysis.evidence_frame(result.matched.label) if result.matched else None
    if evidence and os.path.exists(evidence):
        for method, field in (("send_photo", "photo"), ("send_document", "document")):
            try:
                with open(evidence, "rb") as fh:
                    await getattr(ctx.bot, method)(
                        config.ADMIN_LOG_CHAT,
                        **{field: fh},
                        caption=text,
                        parse_mode="HTML",
                    )
                return
            except TelegramError as e:
                log.warning("%s evidence failed: %s", method, e)

    # never leave the admin without the report itself
    await report(ctx, text)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Media pipeline: download -> detect -> decide -> delete -> report -> cleanup."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or chat.id not in config.GROUP_IDS or not user:
        return
    if await is_admin(ctx, chat.id, user.id):
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

        log.info(
            "media chat=%s user=%s kind=%s detector=%s frames=%d decision=%s "
            "class=%s confidence=%.2f detections=%s generic=%s reason=%s",
            chat.id, user.id, kind, config.DETECTOR_BACKEND, analysis.frames_checked,
            result.decision.value,
            result.matched.label if result.matched else "-",
            result.matched.score if result.matched else 0.0,
            analysis.detections_summary(),
            f"{result.generic_nsfw:.2f}" if result.generic_nsfw is not None else "-",
            result.reason,
        )

        if result.decision is Decision.SAFE:
            return

        if result.decision is Decision.REVIEW:
            # borderline: logged only. No delete, no admin message, no punishment.
            return

        # EXPLICIT -> delete the Telegram message. Nothing else: no strike,
        # no mute, no ban, no kick, no restrict.
        outcome = await moderation.enforce(result, delete_media=lambda: msg.delete())

        if not outcome.deleted:
            log.error(
                "DELETE_FAILED chat=%s message=%s class=%s confidence=%.2f error=%s",
                chat.id, getattr(msg, "message_id", "-"),
                result.matched.label if result.matched else "-",
                result.matched.score if result.matched else 0.0,
                outcome.reason,
            )
            return

        log.info(
            "DELETE_SUCCESS chat=%s message=%s class=%s confidence=%.2f",
            chat.id, getattr(msg, "message_id", "-"),
            result.matched.label if result.matched else "-",
            result.matched.score if result.matched else 0.0,
        )
        await _send_explicit_report(ctx, chat, user, msg, kind, analysis, result, note)
    except Exception:
        # fail open: never delete or punish because of an internal error
        log.exception("media pipeline failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ------------------------------------------------------------ wiring
async def post_init(app: Application) -> None:
    app.job_queue.run_repeating(captcha_reaper, interval=10, first=10)
    log.info(
        "GuardBot started. Groups: %s | explicit classes: %s",
        config.GROUP_IDS,
        sorted(config.EXPLICIT_CLASSES),
    )


def main() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    os.makedirs(config.TMP_DIR, exist_ok=True)
    db.init()
    if config.MEDIA_ENABLED:
        detector.load_model()

    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    app.add_handler(
        ChatMemberHandler(on_member_update, ChatMemberHandler.CHAT_MEMBER)
    )
    app.add_handler(CallbackQueryHandler(on_captcha_click, pattern=r"^cap:\d+$"))

    if config.MEDIA_ENABLED:
        media_filter = (
            filters.PHOTO
            | filters.VIDEO
            | filters.ANIMATION
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.Document.IMAGE
            | filters.Document.VIDEO
        ) & filters.ChatType.GROUPS
        app.add_handler(MessageHandler(media_filter, on_media), group=0)

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
