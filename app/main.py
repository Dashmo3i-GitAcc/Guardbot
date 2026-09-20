import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

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
        if st.is_animated:  # .tgs (Lottie) can't be decoded by ffmpeg -> skip
            return None
        return st, "sticker", bool(st.is_video)
    if msg.document and msg.document.mime_type:
        mt = msg.document.mime_type
        if mt.startswith("image/"):
            return msg.document, "image_file", False
        if mt.startswith("video/"):
            return msg.document, "video_file", True
    return None


def _thumb_of(obj):
    return getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)


def _analyze_blocking(path: str, is_video: bool) -> detector.MediaAnalysis:
    return detector.analyze_video(path) if is_video else detector.analyze_image(path)


def _describe(result) -> str:
    matched = result.matched
    cls = matched.label if matched else "-"
    score = matched.score if matched else 0.0
    return f"class=<b>{cls}</b> score=<b>{score:.2f}</b>"


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Media pipeline: normalize -> detect -> decide -> enforce."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or chat.id not in config.GROUP_IDS or not user:
        return
    if await is_admin(ctx, chat.id, user.id):
        return

    picked = _pick_media(msg)
    if not picked:
        return
    obj, kind, is_video = picked

    os.makedirs(config.TMP_DIR, exist_ok=True)
    path = None
    analysis = None
    note = ""

    try:
        size_mb = (getattr(obj, "file_size", 0) or 0) / 1024 / 1024
        target = obj
        target_is_video = is_video
        if size_mb > config.MAX_DOWNLOAD_MB:
            # too big for Bot API: fall back to the thumbnail
            th = _thumb_of(obj)
            if not th:
                await report(
                    ctx,
                    f"⚠️ فایل بزرگ بدون تامبنیل، بررسی نشد\nاز: {mention(user)}",
                )
                return
            target, target_is_video = th, False
            note = "(فقط تامبنیل بررسی شد)"

        f = await ctx.bot.get_file(target.file_id)
        path = os.path.join(config.TMP_DIR, f"{chat.id}_{msg.message_id}")
        await f.download_to_drive(path)

        loop = asyncio.get_running_loop()
        analysis = await loop.run_in_executor(
            _pool, _analyze_blocking, path, target_is_video
        )
    except Exception as e:
        # fail-open: never delete legit content because of a bug
        log.exception("media pipeline failed")
        await report(ctx, f"⚠️ خطا در بررسی مدیا: <code>{e}</code>")
        return
    finally:
        # media is only kept for the duration of inference
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    result = _engine.decide(analysis)

    log.info(
        "media chat=%s user=%s kind=%s detector=%s frames=%d decision=%s "
        "class=%s confidence=%.2f generic=%s reason=%s",
        chat.id, user.id, kind, config.DETECTOR_BACKEND, analysis.frames_checked,
        result.decision.value,
        result.matched.label if result.matched else "-",
        result.matched.score if result.matched else 0.0,
        f"{result.generic_nsfw:.2f}" if result.generic_nsfw is not None else "-",
        result.reason,
    )

    if result.decision is Decision.SAFE:
        return

    if result.decision is Decision.REVIEW:
        # borderline: allowed, never deleted, never punished
        await report(
            ctx,
            f"🔎 <b>REVIEW</b> (بررسی دستی، حذف نشد) {note}\n"
            f"کاربر: {mention(user)} (<code>{user.id}</code>)\n"
            f"نوع: {kind} | {_describe(result)} | فریم‌ها: {analysis.frames_checked}\n"
            f"دلیل: <code>{result.reason}</code>",
        )
        return

    # EXPLICIT -> delete only. No ban/mute in this stage.
    outcome = await moderation.enforce(
        result,
        delete_media=lambda: msg.delete(),
        record_confirmed=lambda: db.add_strike(chat.id, user.id),
    )

    if outcome.action == "delete_failed":
        await report(
            ctx,
            f"⚠️ <b>حذف ناموفق</b> (هیچ اخطار/بنی اعمال نشد) {note}\n"
            f"کاربر: {mention(user)} (<code>{user.id}</code>)\n"
            f"نوع: {kind} | {_describe(result)}\n"
            f"خطا: <code>{outcome.reason}</code>",
        )
        return

    await report(
        ctx,
        f"🚫 <b>مدیای صریح حذف شد</b> {note}\n"
        f"کاربر: {mention(user)} (<code>{user.id}</code>)\n"
        f"نوع: {kind} | {_describe(result)} | فریم‌ها: {analysis.frames_checked}\n"
        f"اخطار ثبت‌شده: {outcome.strike} | اقدام: <b>حذف مدیا</b>",
    )


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
