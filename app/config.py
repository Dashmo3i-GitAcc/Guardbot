"""All settings come from environment variables (.env)."""
import os


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


def _str_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = os.environ["BOT_TOKEN"]

# The group(s) the bot protects, e.g. -1001234567890 (comma separated)
GROUP_IDS = _int_list(os.environ["GROUP_IDS"])

# Private admin log group/chat. Reports are sent here.
ADMIN_LOG_CHAT = int(os.getenv("ADMIN_LOG_CHAT", "0")) or None

# Extra user IDs that are always immune (besides real chat admins)
WHITELIST_USER_IDS = set(_int_list(os.getenv("WHITELIST_USER_IDS", "")))

# ---------------- Captcha ----------------
CAPTCHA_ENABLED = _bool("CAPTCHA_ENABLED", True)
CAPTCHA_TIMEOUT_SEC = _int("CAPTCHA_TIMEOUT_SEC", 120)
CAPTCHA_TEXT = os.getenv(
    "CAPTCHA_TEXT",
    "سلام {name} 👋\nبرای اینکه بتونی پیام بدی، ظرف {timeout} ثانیه دکمه‌ی زیر رو بزن.",
)
CAPTCHA_BUTTON = os.getenv("CAPTCHA_BUTTON", "✅ من ربات نیستم")

# ---------------- Explicit media moderation ----------------
MEDIA_ENABLED = _bool("MEDIA_ENABLED", True)

# Detector backend. NudeNet ships a small ONNX model with explicit
# body-region classes (CPU friendly).
DETECTOR_BACKEND = os.getenv("DETECTOR_BACKEND", "nudenet")

# Body-region classes that count as *explicit evidence*. These are real
# NudeNet classes. Anything not listed here can never trigger a deletion.
EXPLICIT_CLASSES = set(
    _str_list(
        os.getenv(
            "EXPLICIT_CLASSES",
            "FEMALE_GENITALIA_EXPOSED,MALE_GENITALIA_EXPOSED,ANUS_EXPOSED",
        )
    )
)

# >= this confidence for an explicit class -> EXPLICIT (auto-delete).
#
# Calibration note: NudeNet 320n is not a calibrated probability model. Its own
# operating point is a 0.20 detection gate with NMS at 0.25. Live testing with
# confirmed explicit media produced 0.50 / 0.51 / 0.56 / 0.67 for the explicit
# classes, so the previous 0.80 never fired and everything landed in REVIEW.
# 0.45 sits just below the lowest confirmed true positive (0.50) with a small
# margin, while staying ~1.8x above the model's 0.25 noise floor. Explicit
# classes are region-specific, so swimwear/clothing normally produces the
# *_COVERED classes instead and is not affected.
EXPLICIT_DELETE_THRESHOLD = _float("EXPLICIT_DELETE_THRESHOLD", 0.45)
# >= this (but below the delete threshold) -> REVIEW: logged only, never
# deleted and never notified. Set to NudeNet's NMS floor so REVIEW stays a
# meaningful state instead of a degenerate sliver just under the delete value.
EXPLICIT_REVIEW_THRESHOLD = _float("EXPLICIT_REVIEW_THRESHOLD", 0.25)

# Video / GIF / animated sticker: number of frames sampled
VIDEO_FRAMES = _int("VIDEO_FRAMES", 4)

# Bot API download limit is 20 MB. Larger files: only the thumbnail is checked.
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 20)

# How many media items are analyzed in parallel
MEDIA_WORKERS = _int("MEDIA_WORKERS", 2)

# ---------------- Instant media flood (burst) ----------------
# A burst is *more than* BURST_MAX_ITEMS qualifying media messages from the
# same user inside BURST_WINDOW_SECONDS. The window is deliberately very
# short: this rule stops an instant flood, it is not a "sent a lot of media
# today" rule, and it must not flag normal sharing over 20-30 seconds.
#
# Only BURST_MEDIA_KINDS are counted. Ordinary photos are never in this set,
# so sending several photos quickly is not a flood; a photo is still checked
# by the sexual-content detector on its own.
#   gif            = Telegram animation
#   sticker        = static sticker
#   video_sticker  = .webm video sticker
#   animated_sticker = .tgs (preview thumbnail)
#   video_note     = round video message
BURST_ENABLED = _bool("BURST_ENABLED", True)
BURST_WINDOW_SECONDS = _float("BURST_WINDOW_SECONDS", 3.0)
BURST_MAX_ITEMS = _int("BURST_MAX_ITEMS", 5)  # more than this inside the window
BURST_MEDIA_KINDS = set(
    _str_list(
        os.getenv(
            "BURST_MEDIA_KINDS", "gif,sticker,animated_sticker,video_sticker,video_note"
        )
    )
)

# ---------------- Repeated violations ----------------
# One confirmed explicit-media deletion counts as one violation. A warning is
# sent each time, and the configured restriction is applied once the count
# reaches VIOLATION_MUTE_AFTER.
#
# Note: this is deliberately NOT named MAX_STRIKES. The live .env still carries
# a stale MAX_STRIKES=5 from the removed first-generation bot; that variable is
# not read anywhere any more. This setting keeps the documented three-strike
# policy regardless of leftover environment.
VIOLATION_MUTE_AFTER = _int("VIOLATION_MUTE_AFTER", 3)
# Restriction length in minutes. Telegram lifts a timed restriction itself when
# the time is up, so no reaper is needed. 0 = no automatic expiry.
#
# Note: this is deliberately NOT named MUTE_HOURS. The live .env still carries a
# stale MUTE_HOURS=24 from the previous 24-hour policy; that variable is not
# read anywhere any more. This setting keeps the documented 15-minute policy
# regardless of leftover environment.
MUTE_MINUTES = _int("MUTE_MINUTES", 15)

# ---------------- Test account ----------------
# One user id used to exercise the moderation pipeline repeatedly in a test
# group. It is NOT exempt from anything: detection, deletion, strikes, the admin
# report and the real Telegram restrict call all run exactly as for anyone else.
# The only difference is that a *successful* restriction is lifted again after
# TEST_USER_UNRESTRICT_SECONDS, so the next test violation can be sent straight
# away without a manual unrestrict. Set TEST_USER_ID=0 to disable the exception.
TEST_USER_ID = _int("TEST_USER_ID", 8299811287)
TEST_USER_UNRESTRICT_SECONDS = _float("TEST_USER_UNRESTRICT_SECONDS", 2.0)

VIOLATION_WARNING_TEXT = os.getenv(
    "VIOLATION_WARNING_TEXT",
    "سلام {name} 🙏\n"
    "پیامت به‌دلیل محتوای نامناسب حذف شد.\n"
    "لطفاً دیگه چنین محتوایی نفرست؛ در صورت تکرار، امکان ارسال پیام برات محدود می‌شه.\n"
    "(تخلف {count} از {max})",
)
FLOOD_WARNING_TEXT = os.getenv(
    "FLOOD_WARNING_TEXT",
    "سلام {name} 🙏\n"
    "چند تا فایل/استیکر رو خیلی سریع پشت سر هم فرستادی و این باعث شلوغی گروه می‌شه.\n"
    "به همین دلیل ارسال پیام برات {minutes} دقیقه محدود شد. لطفاً آرام‌تر بفرست.",
)

# ---------------- Second-stage scene classifier ----------------
# NudeNet only sees explicit *body regions*; it cannot see a sexual act when no
# genitalia are visible (intimate/sexual interaction, erotic scenes, explicit
# scenes where the anatomical class is simply missed). This stage adds a local,
# scene-level NSFW score on top of NudeNet, so the overall sexual nature of the
# media is recognised and not just individual body parts.
#
# Unlike the first version of this stage, a confident scene score CAN now
# produce EXPLICIT and therefore a deletion - that is the whole point of the
# stage. It is graded so that only clearly sexual media deletes:
#
#     score <  SCENE_REVIEW_THRESHOLD  -> SAFE
#     score >= SCENE_REVIEW_THRESHOLD  -> REVIEW   (logged, never deletes)
#     score >= SCENE_DELETE_THRESHOLD  -> EXPLICIT (delete)
#
# It still fails open: a missing model, a missing dependency or an inference
# error leaves the score absent, and an absent score never deletes.
SCENE_ENABLED = _bool("SCENE_ENABLED", True)
SCENE_MODEL = os.getenv("SCENE_MODEL", "Falconsai/nsfw_image_detection")
# Deliberately high: this threshold deletes media, so it is set where only a
# clearly sexual scene reaches it. Like EXPLICIT_DELETE_THRESHOLD it is a
# conservative starting point to be tuned against real traffic, not a measured
# constant. Do not lower it to "catch more" without evidence.
SCENE_DELETE_THRESHOLD = _float("SCENE_DELETE_THRESHOLD", 0.95)
# Lower bound of the REVIEW band: mildly suggestive / ambiguous media.
SCENE_REVIEW_THRESHOLD = _float("SCENE_REVIEW_THRESHOLD", 0.60)
# How many of the already-sampled frames the scene stage scores. 1 reproduces
# the old single-frame behaviour. Measured ~1.7 s per frame on the 2-core VPS,
# so this is the knob that bounds the stage's cost on video/GIF - it is not
# affected by raising VIDEO_FRAMES.
SCENE_MAX_FRAMES = _int("SCENE_MAX_FRAMES", 2)

DB_PATH = os.getenv("DB_PATH", "/data/guardbot.db")
TMP_DIR = os.getenv("TMP_DIR", "/tmp/guardbot")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


# ---------------- Group acquisition (VPN trial invitations) ----------------
# Someone asking about a VPN in the group is a customer the VPN bot has not met
# yet. This bot notices, asks the VPN bot for a personal invitation link, and
# posts that link in the group. It never sees a credential, a client or a
# subscription link — the VPN bot owns all of that, and the actual test is
# delivered in a private chat.
GROUP_TRIAL_ENABLED = _bool("GROUP_TRIAL_ENABLED", True)

# Where the rules live. Empty means the copy shipped next to app/intent.py,
# which is the normal case; the override exists so the vocabulary can be
# extended on a running deployment without rebuilding the image.
INTENT_RULES_PATH = os.getenv("INTENT_RULES_PATH", "")

# Whether a message must be *about* circumvention before it counts. Left on,
# "اینترنتم ضعیفه" is not an intent. Turning it off makes the bot offer a test
# to anyone complaining about their connection — occasionally useful, usually
# noisy.
INTENT_REQUIRE_TOPIC = _bool("INTENT_REQUIRE_TOPIC", True)

# Shortest message worth looking at, in characters after normalisation.
INTENT_MIN_LENGTH = _int("INTENT_MIN_LENGTH", 4)

# How long a user is left alone after being offered a test, so the same person
# asking three times in a row gets one reply and not three. Persisted in the
# database, so a restart does not reset it.
INTENT_COOLDOWN_SECONDS = _int("INTENT_COOLDOWN_SECONDS", 3600)

# The VPN bot's internal service endpoint, e.g. http://172.21.0.1:8099
# (the Docker bridge gateway of this container's network — see README).
VPNBOT_API_URL = os.getenv("VPNBOT_API_URL", "").strip().rstrip("/")
# Shared secret for signing those requests. Must match SERVICE_SHARED_SECRET in
# the VPN bot's .env. Empty disables the feature rather than sending anything.
VPNBOT_SHARED_SECRET = os.getenv("VPNBOT_SHARED_SECRET", "").strip()
VPNBOT_TIMEOUT_SECONDS = _float("VPNBOT_TIMEOUT_SECONDS", 8.0)

# The in-group reply. The group only ever sees this and the button — never a
# configuration, a subscription link or a credential.
GROUP_TRIAL_INVITE_TEXT = os.getenv(
    "GROUP_TRIAL_INVITE_TEXT",
    "سلام {name} 👋\n"
    "برای تست رایگان VPN یه لینک اختصاصی برات ساختیم.\n"
    "روی دکمه زیر بزن تا توی ربات برات فعالش کنیم. 🎁",
)
GROUP_TRIAL_BUTTON = os.getenv("GROUP_TRIAL_BUTTON", "🎁 دریافت تست رایگان")
# Sent when the user was already offered a link recently. Deliberately quiet:
# no second button, so the group does not fill up with invitations.
GROUP_TRIAL_ALREADY_TEXT = os.getenv(
    "GROUP_TRIAL_ALREADY_TEXT",
    "{name} عزیز، لینک تستت رو قبلاً برات فرستادیم 👆\n"
    "اگه پیداش نکردی، توی ربات دکمه «🎁 تست رایگان» رو بزن.",
)
# Sent when the user has already used their one free trial.
GROUP_TRIAL_USED_TEXT = os.getenv(
    "GROUP_TRIAL_USED_TEXT",
    "{name} عزیز، تست رایگانت قبلاً فعال شده و هر کاربر یه‌بار می‌تونه "
    "استفاده کنه. 🙏\n"
    "برای ادامه، توی ربات از «🛒 خرید اشتراک» یه پلن انتخاب کن.",
)
# Sent when the VPN bot cannot be reached. Logged loudly as well, because it
# means the integration is down rather than that the user did something wrong.
GROUP_TRIAL_UNAVAILABLE_TEXT = os.getenv(
    "GROUP_TRIAL_UNAVAILABLE_TEXT",
    "الان نمی‌تونم لینک تست رو بسازم. 🙏\n"
    "لطفاً چند دقیقه بعد دوباره امتحان کن.",
)
# Adds a small «قوانین» hint under the invitation, so the terms gate the user
# meets inside the bot is not a surprise.
GROUP_TRIAL_HINT = os.getenv(
    "GROUP_TRIAL_HINT",
    "تست ۵۰۰ مگابایت و ۱ روزه‌ست و فقط یک‌بار به هر کاربر داده می‌شه.",
)

# ── The lead-in, chosen by what the message was actually about ────────────────
# One generic sentence for every lead reads like a macro, and it answers a
# complaint about a slow connection with the same words as a question about
# price. The AI layer returns a *key* (see app/ai_intent.py RESPONSE_KINDS) and
# these are the words for each key — so the reply can acknowledge what the
# person actually said while every word the group sees still comes from here.
#
# `{name}` is always available. The button and the hint below are appended by
# app/responses.py, so none of these repeats them.
GROUP_TRIAL_REPLY_CONNECTIVITY = os.getenv(
    "GROUP_TRIAL_REPLY_CONNECTIVITY",
    "سلام {name} 👋\n"
    "آره، وقتی اینترنت این‌طور ضعیف یا ناپایدار می‌شه معمولاً مشکل از مسیره.\n"
    "اگه می‌خوای ببینی مشکل از مسیر اتصالت هست یا نه، می‌تونی تست رایگان رو "
    "امتحان کنی. 👇",
)
GROUP_TRIAL_REPLY_ACCESS = os.getenv(
    "GROUP_TRIAL_REPLY_ACCESS",
    "سلام {name} 👋\n"
    "برای باز کردن سرویس‌هایی که فیلتر شدن، یه مسیر جایگزین لازمه.\n"
    "اگه می‌خوای ببینی مشکل از مسیر اتصالت هست یا نه، می‌تونی تست رایگان رو "
    "امتحان کنی. 👇",
)
GROUP_TRIAL_REPLY_VPN = os.getenv(
    "GROUP_TRIAL_REPLY_VPN",
    "سلام {name} 👋\n"
    "اگه دنبال یه وی‌پی‌ان خوبی، ما یه تست رایگان داریم که بدون هزینه "
    "می‌تونی امتحانش کنی. 👇",
)
GROUP_TRIAL_REPLY_PRICING = os.getenv(
    "GROUP_TRIAL_REPLY_PRICING",
    "سلام {name} 👋\n"
    "قیمت بسته به حجم و مدت‌ش فرق می‌کنه.\n"
    "ولی اول می‌تونی با تست رایگان ببینی برات جواب می‌ده یا نه. 👇",
)
# The generic wording is GROUP_TRIAL_INVITE_TEXT above — the same sentence the
# flow used before it could tell the cases apart, reused rather than duplicated
# so there is exactly one place to edit it.


# ---------------- Gemini: the second opinion on an ambiguous message ----------
# The rule engine in app/intent.py is fast, free, offline and explainable, and it
# stays the first and last word on anything it is sure about. What it cannot do
# is recognise a phrasing nobody wrote a pattern for. This layer covers that
# gap: when — and only when — the rules come back unsure, the message is sent to
# Gemini for a structured yes/no.
#
# Four things it deliberately is not:
#
#   * It is not a replacement. A rule match is a decision, not a suggestion, and
#     the rules' `ignore` veto is final — an LLM must not be arguable out of the
#     guard that stops us advertising at a competitor.
#   * It is not an author. The model returns a classification and nothing else.
#     Every word the group sees comes from GROUP_TRIAL_* above, owned by this
#     application. Model output is logged, never sent.
#   * It is not authoritative. Anything it cannot do — no key, no quota, no
#     network, a malformed answer — resolves to "not a lead", which is exactly
#     how the bot behaved before this layer existed.
#   * It is not free. The free tier is rate-limited per project and its exact
#     RPM/RPD are not published and not guaranteed, so the quota is bounded on
#     our side too (rate window + a persisted daily cap) rather than discovered
#     by getting a 429 in the middle of a busy group.
GEMINI_ENABLED = _bool("GEMINI_ENABLED", True)

# The API key. Read from the environment only: never logged, never echoed in an
# error, never written to the database. Empty means the layer is inert and the
# bot behaves exactly as it did before it existed.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# The model, chosen from measurement rather than from documentation.
#
# `gemini-flash-latest` is the SDK's documented stable alias for the current
# Flash model, and it was the first choice. Against this deployment's free-tier
# key it answered 0 times out of 8 with `503 UNAVAILABLE ... currently
# experiencing high demand` and `504 DEADLINE_EXCEEDED`, sustained over ~25
# attempts — so the layer reported itself active and classified nothing.
#
# `gemini-flash-lite-latest` answered 8 of 8 with no errors and classified every
# probe message correctly. For a binary judgement about one short message it is
# also the right tool: faster (which matters inside a 10-second message-handler
# budget) and cheaper against the daily quota.
#
# This is one env var, so it can be changed on a running deployment without a
# rebuild. If availability shifts, measure again — do not assume.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip()

# The authoritative bound on one call. `asyncio.wait_for` enforces it, so a
# stalled socket can never hold up a group message handler.
#
# 10 is a floor, not a preference: the API rejects a manually-set deadline below
# 10 seconds outright ("400 INVALID_ARGUMENT ... Minimum allowed deadline is
# 10s"), so anything smaller makes every call fail. `app/ai_intent.py` clamps to
# that floor rather than trusting this value, because a silently dead
# integration is far worse than a slightly longer timeout.
GEMINI_TIMEOUT_SECONDS = _float("GEMINI_TIMEOUT_SECONDS", 10.0)

# One retry, with exponential backoff, and only for transient failures. A 429 or
# a 5xx is worth one more try; a malformed answer is not (it will be malformed
# again, and it is already counted).
GEMINI_MAX_RETRIES = _int("GEMINI_MAX_RETRIES", 1)
GEMINI_BACKOFF_SECONDS = _float("GEMINI_BACKOFF_SECONDS", 1.5)

# Our own ceiling on how often we are willing to ask: at most GEMINI_RATE_LIMIT
# calls in any GEMINI_RATE_WINDOW seconds. Deliberately below the published free
# tier so a burst of group chatter degrades to the rule engine instead of
# earning a 429 that would also break the calls we actually wanted.
GEMINI_RATE_LIMIT = _int("GEMINI_RATE_LIMIT", 10)
GEMINI_RATE_WINDOW = _float("GEMINI_RATE_WINDOW", 60.0)

# A hard daily ceiling, counted against the API's own day (see app/db.ai_day)
# and persisted, so a restart cannot hand us a fresh allowance. The free tier's
# RPD resets at midnight Pacific; ours resets no later than that.
GEMINI_DAILY_LIMIT = _int("GEMINI_DAILY_LIMIT", 400)

# How many consecutive transport failures open the circuit, and for how long.
# A group does not need an offer every minute, so going quiet for five minutes
# after a run of timeouts is cheaper than hammering a service that is down.
GEMINI_CIRCUIT_FAILURES = _int("GEMINI_CIRCUIT_FAILURES", 5)
GEMINI_CIRCUIT_SECONDS = _float("GEMINI_CIRCUIT_SECONDS", 300.0)

# The confidence below which the model's own "yes" is not acted on. The prompt
# asks for a number; this is where we decide what it has to be worth.
GEMINI_MIN_CONFIDENCE = _float("GEMINI_MIN_CONFIDENCE", 0.55)

# The message is truncated to this many characters before it is sent. A group
# message is short; a pasted wall of text is not worth the tokens, and this is
# also the bound on what leaves the server.
GEMINI_MAX_CHARS = _int("GEMINI_MAX_CHARS", 600)

