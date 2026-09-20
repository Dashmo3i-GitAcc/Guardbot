import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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

from . import config, db, detector

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("guardbot")

_pool = ThreadPoolExecutor(max_workers=config.MEDIA_WORKERS)
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
        if st.is_animated:  # .tgs (Lottie) can't be decoded by ffmpeg -> check thumb
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


def _analyze_blocking(path: str, is_video: bool) -> detector.Verdict:
    return detector.analyze_video(path) if is_video else detector.analyze_image(path)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    verdict = None
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
        verdict = await loop.run_in_executor(
            _pool, _analyze_blocking, path, target_is_video
        )
    except Exception as e:
        # fail-open: never delete legit content because of a bug
        log.exception("media pipeline failed")
        await report(ctx, f"⚠️ خطا در بررسی مدیا: <code>{e}</code>")
        return
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    if verdict.frames_checked == 0:
        log.warning("no frames checked: %s", verdict.note)
        return

    trusted = db.is_trusted(chat.id, user.id)
    margin = config.TRUSTED_EXTRA_MARGIN if trusted else 0.0
    del_thr = min(config.NSFW_DELETE_THRESHOLD + margin, 0.99)
    ban_thr = min(config.NSFW_BAN_THRESHOLD + margin, 0.99)

    log.info(
        "media chat=%s user=%s kind=%s score=%.3f frames=%d",
        chat.id, user.id, kind, verdict.score, verdict.frames_checked,
    )

    if verdict.score < del_thr:
        return

    # ---- bad content: delete first, ask questions later ----
    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("delete failed: %s", e)

    strikes = db.add_strike(chat.id, user.id)
    hard = strikes >= config.MAX_STRIKES

    action = "حذف شد"
    if hard:
        if config.HIGH_CONF_ACTION == "ban":
            try:
                await ctx.bot.ban_chat_member(chat.id, user.id)
                action = "حذف + بن"
            except TelegramError as e:
                log.warning("ban failed: %s", e)
        else:
            until = datetime.now(timezone.utc) + timedelta(hours=config.MUTE_HOURS)
            try:
                await ctx.bot.restrict_chat_member(
                    chat.id, user.id, permissions=MUTED, until_date=until
                )
                action = f"حذف + میوت {config.MUTE_HOURS} ساعته"
            except TelegramError as e:
                log.warning("mute failed: %s", e)

    await report(
        ctx,
        f"🚫 <b>مدیای نامناسب</b> {note}\n"
        f"کاربر: {mention(user)} (<code>{user.id}</code>)\n"
        f"نوع: {kind} | امتیاز: <b>{verdict.score:.2f}</b> | فریم‌ها: {verdict.frames_checked}\n"
        f"اخطار: {strikes} | اقدام: <b>{action}</b>",
    )


# ------------------------------------------------------------ counting
async def on_any_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Counts messages for the trust system."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not user or chat.id not in config.GROUP_IDS:
        return
    db.bump_messages(chat.id, user.id)


# ------------------------------------------------------------ wiring
async def post_init(app: Application) -> None:
    app.job_queue.run_repeating(captcha_reaper, interval=10, first=10)
    log.info("GuardBot started. Groups: %s", config.GROUP_IDS)


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

    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_any_message),
        group=1,
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
