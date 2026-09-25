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
NAV_OVERVIEW = "نمای کلی"
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
LOGIN_NO_OPERATOR = (
    "رمز ورود تنظیم شده، ولی پنل نمی‌دونه به اسم کی باید وارد بشی. توی "
    "<code>.env</code> مقدار <code>DASHBOARD_OPERATOR_ID</code> (یا "
    "<code>OWNER_USER_ID</code>) رو با شناسه‌ی عددی تلگرامت پر کن و سرویس "
    "داشبورد رو دوباره راه بنداز. تا اون موقع، هیچ صفحه‌ای باز نمی‌شه — و این "
    "عمدیه: پنل فقط یه هویت رو قبول می‌کنه، و اون هویت از تنظیمات میاد، نه از "
    "عضویت تلگرام."
)

# ── Flash messages ────────────────────────────────────────────────────────
# A flash travels as a *key* in the query string, never as text, so a caller
# cannot put a sentence of their own on the page. The key is looked up here.
LOGOUT_DONE = "خارج شدی. هر وقت خواستی دوباره وارد شو."
SESSION_EXPIRED = "نشستت منقضی شد. یه بار دیگه وارد شو."

# ── Overview ──────────────────────────────────────────────────────────────
OVERVIEW_TITLE = "نمای کلی"
OVERVIEW_INTRO = (
    "یه نگاه سریع به وضعیت ربات: گروه‌ها، حساب‌های هوش مصنوعی و مصرف امروز. "
    "هر عددی که اینجا می‌بینی از همون دیتابیسیه که ربات داره توش می‌نویسه — "
    "هیچی اینجا حدس زده نمی‌شه."
)
# Shown when one or more reads failed. The failed source names are listed under
# it, as code, so the operator can tell which section is short rather than
# trusting a number that happens to be zero.
OVERVIEW_PARTIAL = (
    "یه بخشی از داده‌ها خونده نشد، پس بعضی عددها ناقص‌اند — نه صفر. "
    "منبع‌هایی که خطا دادن:"
)

# The six headline numbers. Labels are short; the hint under each one says what
# the number actually counts, because "accounts" and "requests" can mean more
# than one thing and the panel must not make the operator guess which.
OVERVIEW_ROOMS = "گروه‌های مجاز"
OVERVIEW_ROOMS_HINT = "از بین همه‌ی گروه‌های ثبت‌شده"
OVERVIEW_PEOPLE = "آدم‌ها"
OVERVIEW_PEOPLE_HINT = "کسی که ربات می‌شناسه"
OVERVIEW_ACCOUNTS = "حساب‌های هوش مصنوعی"
OVERVIEW_ACCOUNTS_HINT = "فعال، از کل حساب‌های ثبت‌شده"
OVERVIEW_REQUESTS = "درخواست‌های امروز"
OVERVIEW_REQUESTS_HINT = "جمع چهار بخشِ مصرف"
OVERVIEW_ERRORS = "خطاهای امروز"
OVERVIEW_ERRORS_HINT = "همون چهار بخش"
OVERVIEW_LAST_UPDATE = "آخرین آپدیت"
OVERVIEW_LAST_UPDATE_HINT = "آخرین پیامی که ربات پردازش کرده"

# What to look at first. One sentence per kind, and the number (where there is
# one) is rendered by the template, never written here.
OVERVIEW_ATTENTION_TITLE = "این‌ها رو یه نگاه بنداز"
OVERVIEW_ATTENTION_EMPTY = "هیچ حساب فعالی نداره"
OVERVIEW_ATTENTION_ONE = "به یه حساب فعال رسیده"
OVERVIEW_ATTENTION_INVALID = "کلید نامعتبر داره"
OVERVIEW_ALL_GOOD = "چیزی نیست که بخواد نگرانت کنه. همه‌ی حساب‌ها سر جاشونن."

OVERVIEW_POOLS_TITLE = "حساب‌های هوش مصنوعی"
OVERVIEW_POOLS_HINT = "آخرین وضعیتی که ربات ذخیره کرده"
OVERVIEW_POOLS_EMPTY = (
    "هنوز هیچ حسابی ثبت نشده. ربات هر حساب رو اولین باری که استفاده کنه "
    "ذخیره می‌کنه، پس تا اون موقع چیزی برای نشون دادن نیست."
)

OVERVIEW_USAGE_TITLE = "مصرف امروز"
OVERVIEW_USAGE_HINT = "روزِ خودِ گوگل، نه نیمه‌شب محلی"
OVERVIEW_USAGE_EMPTY = "امروز هنوز هیچ درخواستی از این چهار بخش ثبت نشده."

OVERVIEW_SWITCH_TITLE = "کلیدهای اصلی"
OVERVIEW_SWITCH_NEXUS = "نکسوس"
OVERVIEW_SWITCH_AWARENESS = "آگاهی از گروه"
OVERVIEW_SWITCH_SEARCH = "جست‌وجوی وب"

OVERVIEW_EVENTS_TITLE = "رویدادهای اخیر حساب‌ها"
OVERVIEW_EVENTS_EMPTY = (
    "هنوز رویدادی ثبت نشده. رویداد وقتی نوشته می‌شه که یه حساب یا مدل "
    "وضعیتش عوض بشه — عوض شدن مدل، کول‌داون، برگشتن به چرخه."
)

OVERVIEW_COL_WORKLOAD = "بخش"
OVERVIEW_COL_ACCOUNTS = "حساب"
OVERVIEW_COL_ACTIVE = "فعال"
OVERVIEW_COL_LIMITED = "محدود"
OVERVIEW_COL_EXHAUSTED = "سهمیه تموم"
OVERVIEW_COL_INVALID = "نامعتبر"
OVERVIEW_COL_REQUESTS = "درخواست"
OVERVIEW_COL_FAILURES = "خطا"
OVERVIEW_COL_CALLS = "درخواست"
OVERVIEW_COL_ERRORS = "خطا"
OVERVIEW_COL_SKIPPED = "رد شده"
OVERVIEW_COL_RESULT = "نتیجه"
OVERVIEW_COL_WHEN = "کِی"
OVERVIEW_COL_EVENT = "رویداد"
OVERVIEW_COL_DETAIL = "جزئیات"

# The honest footnote. Every clause here names a thing the panel genuinely
# cannot show, and why — an operator who knows what is missing stops looking
# for it in the wrong place.
OVERVIEW_NOT_SHOWN_TITLE = "چیزی که این صفحه نشون نمی‌ده"
OVERVIEW_NOT_SHOWN_BODY = (
    "تأخیر پاسخ‌ها جایی ذخیره نمی‌شه، پس اینجا هم نیست — ربات زمان‌ها رو فقط "
    "توی لحظه اندازه می‌گیره. لاگ خطایی هم به‌صورت فایل وجود نداره؛ خطاها "
    "فقط توی شمارنده‌ی همون روز شمرده می‌شن. و ربات ضربان جدا نمی‌فرسته، پس "
    "«آخرین آپدیت» بالا نزدیک‌ترین چیز به «زنده بودن»‌ه — اگه ربات مدتی هیچ "
    "پیامی نگیره، این عدد هم قدیمی می‌مونه."
)

# ── The panel's own state ─────────────────────────────────────────────────
# Not about the bot: whether *this panel* is configured and whether its sessions
# survive a restart. Shown on the Overview because an operator who cannot tell
# these apart from a bot fault will debug the wrong process.
PANEL_TITLE = "خود پنل"
PANEL_UPTIME = "از وقتی بالا اومده"
PANEL_ROLE = "نقش پنل"
PANEL_PERMISSIONS = "دسترسی"
PANEL_SESSION = "این نشست تا"
PANEL_SECRET_TITLE = "نشست‌ها بعد از ری‌استارت"
PANEL_NOT_CONFIGURED_TITLE = "ورود تنظیم نشده"
PANEL_SECRET_WARNING = (
    "<code>DASHBOARD_SECRET</code> تنظیم نشده. با هر بار ری‌استارت، همه از "
    "پنل بیرون می‌افتن. برای اینکه نشست‌ها بمونن، یه مقدار ثابت توی "
    "<code>.env</code> بذار."
)
PANEL_NOT_CONFIGURED_WARNING = (
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
# A signed-in operator whose rbac principal does not hold what the page needs.
# Distinct from FORBIDDEN_BODY, which is about not being signed in at all: the
# two need different next steps, so they get different sentences.
FORBIDDEN_PERMISSION = (
    "وارد شدی، ولی این صفحه کاری رو می‌خواد که حسابت اجازه‌ش رو نداره. "
    "دسترسی پنل با دسترسی مدیریت گروه یکیه نیست: پنل فقط هویتی رو قبول می‌کنه "
    "که توی تنظیمات مشخص شده. اگه فکر می‌کنی باید دسترسی داشته باشی، "
    "<code>DASHBOARD_OPERATOR_ID</code> رو توی <code>.env</code> چک کن."
)

_FLASH = {
    "logged_out": (LOGOUT_DONE, False),
    "session_expired": (SESSION_EXPIRED, True),
    "csrf": (CSRF_EXPIRED, True),
    "forbidden": (FORBIDDEN_BODY, True),
    "forbidden_permission": (FORBIDDEN_PERMISSION, True),
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
