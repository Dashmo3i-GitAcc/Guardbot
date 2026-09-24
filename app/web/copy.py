"""Every word the Admin Control Center shows, in one place.

This is the panel's counterpart to ``app/chat.py``'s persona: the bot talks to
the room, this talks to the operator, and both should sound like the same
project. The rules, in order of importance:

1. **Say what happened, not that something happened.** «رمز عوض شد و همه‌ی
   نشست‌های قبلی بسته شدند» — never «عملیات با موفقیت انجام شد».
2. **Empty is a sentence, not a label.** «هنوز چیزی اینجا نیست» — never
   «اطلاعاتی موجود نیست».
3. **Ask the real question.** A confirmation restates the consequence, so the
   operator does not have to remember what the button did.
4. **Be honest about what is missing.** If something is not wired up, say so and
   say what to do instead. Nothing here claims a capability the panel does not
   have.
5. **Emoji earn their place.** One per heading at most, and only where it helps
   scanning. Never to decorate a sentence.

Numbers and dates never appear in this file: they are formatted by
``app/web/jalali.py`` through the Jinja filters in ``app/web/jinja.py``.

Strings here are trusted, code-owned copy and may contain a little inline HTML
(``<code>``), exactly like the bot's own copy. Everything that comes from the
database is escaped by Jinja's autoescaping.
"""

BRAND = "GuardBot"

# ── Shell ─────────────────────────────────────────────────────────────────
APP_TITLE = f"مرکز کنترل {BRAND}"
APP_TAGLINE = "همون ربات، از پشت مرورگر"
NAV_HOME = "خانه"
NAV_LOGOUT = "خروج"
FOOTER_NOTE = (
    "این پنل فقط داده‌های همون ربات رو نشون می‌ده. هر تغییری اینجا بدی، "
    "بلافاصله توی ربات هم دیده می‌شه."
)

# ── Theme ─────────────────────────────────────────────────────────────────
# There is no theme toggle: the panel ships one dark identity, not a preference.
# The strings will arrive with the toggle if it is ever added.

# ── Login ─────────────────────────────────────────────────────────────────
LOGIN_TITLE = "ورود"
LOGIN_INTRO = (
    "این پنل همون رباته، فقط از پشت مرورگر. برای دیدن وضعیت ربات و گروه‌ها "
    "اول وارد شو."
)
LOGIN_USERNAME = "نام کاربری"
LOGIN_PASSWORD = "رمز عبور"
LOGIN_SUBMIT = "بزن بریم"
LOGIN_FAILED = "نام کاربری یا رمز درست نبود. یه بار دیگه امتحان کن."
LOGIN_RATE_LIMITED = (
    "چند بار پشت‌سرهم اشتباه شد. برای اینکه کسی نتونه رمز رو حدس بزنه، "
    "چند دقیقه‌ای صبر کن و بعد دوباره امتحان کن."
)
LOGIN_NOT_CONFIGURED = (
    "هنوز رمز ورود تنظیم نشده، پس کسی نمی‌تونه وارد بشه. توی فایل "
    "<code>.env</code> مقدار <code>DASHBOARD_PASSWORD</code> یا "
    "<code>DASHBOARD_PASSWORD_HASH</code> رو پر کن و سرویس داشبورد رو "
    "دوباره راه بنداز."
)

# ── Flash messages ────────────────────────────────────────────────────────
# A flash travels as a *key* in the query string, never as text, so a caller
# cannot put a sentence of their own on the page. The key is looked up here.
LOGOUT_DONE = "خارج شدی. هر وقت خواستی دوباره وارد شو."
SESSION_EXPIRED = "نشستت منقضی شد. یه بار دیگه وارد شو."

# ── Home ──────────────────────────────────────────────────────────────────
HOME_TITLE = "خانه"
HOME_READY_TITLE = "پنل بالاست"
HOME_READY_BODY = (
    "این صفحه‌ی ورود و پوسته‌ی پنل راه افتاده. از اینجا به بعد، هر بخشی که "
    "اضافه می‌شه توی همین قالب می‌شینه."
)
HOME_UPTIME = "از وقتی بالا اومده"
HOME_SESSION = "این نشست تا"
HOME_NEXT_TITLE = "الان چیزی از اینجا مدیریت نمی‌شه"
HOME_NEXT_BODY = (
    "این نسخه فقط پایه‌ی پنل رو می‌سازه: ورود، نشست و پوسته. نمای کلی، "
    "گروه‌ها، مرکز هوش مصنوعی، مدیریت ربات و گزارش رویدادها مرحله‌به‌مرحله "
    "اضافه می‌شن. تا اون موقع، هیچ دکمه‌ای اینجا کاری روی ربات انجام نمی‌ده — "
    "و این عمدیه."
)
HOME_SECRET_WARNING = (
    "<code>DASHBOARD_SECRET</code> تنظیم نشده. با هر بار ری‌استارت، همه از "
    "پنل بیرون می‌افتن. برای اینکه نشست‌ها بمونن، یه مقدار ثابت توی "
    "<code>.env</code> بذار."
)
HOME_NOT_CONFIGURED_WARNING = (
    "هنوز رمز ورود تنظیم نشده، پس کسی نمی‌تونه وارد بشه. توی <code>.env</code> "
    "مقدار <code>DASHBOARD_PASSWORD</code> یا <code>DASHBOARD_PASSWORD_HASH</code> "
    "رو پر کن."
)

# ── Errors ────────────────────────────────────────────────────────────────
FORBIDDEN_TITLE = "این صفحه برای تو نیست"
FORBIDDEN_BODY = (
    "برای دیدن این صفحه باید وارد شده باشی، یا حسابت دسترسی لازم رو نداشته. "
    "از صفحه‌ی ورود دوباره امتحان کن."
)
NOT_FOUND_TITLE = "این صفحه وجود نداره"
NOT_FOUND_BODY = (
    "آدرسی که زدی به هیچ صفحه‌ای نمی‌خوره. شاید لینک قدیمی شده یا غلط تایپ شده."
)
ERROR_TITLE = "یه چیزی خراب شد"
ERROR_BODY = (
    "این خطا از طرف پنل بود، نه از طرف ربات — ربات داره کارش رو می‌کنه. "
    "یه بار دیگه امتحان کن؛ اگه بازم شد، لاگ سرویس داشبورد رو ببین."
)
CSRF_EXPIRED = (
    "این فرم قدیمی شده بود و برای امنیت قبول نشد. صفحه رو دوباره باز کن و "
    "از نو امتحان کن."
)

_FLASH = {
    "logged_out": (LOGOUT_DONE, False),
    "session_expired": (SESSION_EXPIRED, True),
    "csrf": (CSRF_EXPIRED, True),
    "forbidden": (FORBIDDEN_BODY, True),
    "not_found": (NOT_FOUND_BODY, True),
    "error": (ERROR_BODY, True),
}


def flash_text(key: str | None) -> str:
    """The sentence for a flash key, or ``''`` for an unknown/absent key."""
    if not key:
        return ""
    entry = _FLASH.get(key)
    return entry[0] if entry else ""


def flash_is_error(key: str | None) -> bool:
    """Whether a flash key should be shown as a warning rather than a success."""
    entry = _FLASH.get(key or "")
    return bool(entry and entry[1])


__all__ = [
    "BRAND",
    "flash_is_error",
    "flash_text",
]
