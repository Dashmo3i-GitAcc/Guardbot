"""All settings come from environment variables (.env)."""
import os


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


def _str_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _kv_list(value: str) -> dict[str, str]:
    """``name=value`` pairs, comma separated. Malformed entries are dropped.

    Dropped rather than raising, on the same reasoning as ``CONFIG_ADMINS``: a
    typo in a deployment variable must not stop the bot booting. And a dropped
    entry *narrows* the result, which is the safe direction — the allowlist this
    builds is only ever consulted to permit something.
    """
    out: dict[str, str] = {}
    for chunk in value.split(","):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, _, target = item.partition("=")
        name, target = name.strip().lower(), target.strip()
        if name and target:
            out[name] = target
    return out


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

# How long a member has to retry after the unmute call fails on a click that was
# otherwise valid. The challenge row is claimed (deleted) before the network
# call, so a failure has to put it back or the member is left muted with no row
# and no way to verify themselves. Short, because it exists to cover a transient
# Telegram error, not to extend the challenge.
CAPTCHA_RETRY_GRACE_SEC = _int("CAPTCHA_RETRY_GRACE_SEC", 15)

# What happens when a challenge runs out of time without being solved.
#
# This is configuration because the right answer is a policy decision and not a
# fact about the code. The three modes, and what each means for the member:
#
#   * ``kick``    — remove them from the group (ban then immediate unban, so
#                   they may rejoin and try again). This is the long-standing
#                   behaviour and remains the default, so an existing deployment
#                   is unchanged by this setting's introduction.
#   * ``restrict``— keep them unable to post and refresh the challenge, giving
#                   them a fresh deadline and a fresh button. They stay in the
#                   group and stay unverified; nothing removes them.
#   * ``none``    — take no member action at all. The challenge row is dropped
#                   and the member is left as Telegram has them. Only appropriate
#                   where an operator has some other verification in place.
#
# What none of these is: a permanent ban on a timer. ``kick`` is not a ban —
# the unban is immediate and the person may rejoin — and the two alternatives
# are milder still. An unrecognised value falls back to ``kick`` so that a typo
# cannot silently turn verification off.
CAPTCHA_ON_EXPIRE = os.getenv("CAPTCHA_ON_EXPIRE", "kick").strip().lower()
CAPTCHA_EXPIRE_MODES = ("kick", "restrict", "none")
if CAPTCHA_ON_EXPIRE not in CAPTCHA_EXPIRE_MODES:
    CAPTCHA_ON_EXPIRE = "kick"

# The copy used in ``restrict`` mode when the challenge is refreshed. ``{name}``
# and ``{timeout}`` are filled the same way ``CAPTCHA_TEXT`` is.
CAPTCHA_RETRY_TEXT = os.getenv(
    "CAPTCHA_RETRY_TEXT",
    "سلام {name} 👋\nهنوز تأیید نشدی. برای اینکه بتونی پیام بدی، ظرف {timeout} "
    "ثانیه دکمه‌ی زیر رو بزن.",
)

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

# ---------------- Outbound AI connectivity: which IP family ------------------
# This host has a global IPv6 address and the AI endpoint publishes AAAA
# records, so the kernel already prefers IPv6 (the RFC 6724 default). This
# setting makes that explicit rather than incidental: with it on, addresses for
# the AI hosts are ordered IPv6-first so the preference does not depend on the
# kernel's address-selection heuristics staying the way they are today.
#
# It is a *reorder*, never a filter — every IPv4 address stays in the list, and
# a connector that walks it falls back to IPv4 when IPv6 does not work. So the
# failure mode of being wrong here is a slower call, not a call that cannot be
# made. See app/net.py.
AI_PREFER_IPV6 = _bool("AI_PREFER_IPV6", True)


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


# ── The VPN bot's operational surface ─────────────────────────────────────
# The reads and writes the assistant may perform against the VPN bot, reached
# through signed requests to its internal API. Two things are worth stating here
# because the wording below depends on them:
#
# * Every one of these operations is gated on ``vpn.read`` / ``vpn.manage``,
#   which no role bundle carries — so they are the owner's, permanently and
#   structurally, not by a setting somebody could flip.
# * The three that move money or bulk-reject orders are not executed when they
#   are asked for. They are *recorded* and the owner is asked to confirm, in the
#   same shape the coding-agent bridge uses for its dangerous tasks.
#
# The sentences below are the four different next steps an operator can be
# given, so they are four sentences and not one "refused".

# How long a recorded VPN operation stays confirmable. Long enough that the
# owner can read a message and reply, short enough that a forgotten operation
# does not stay armed for a day.
VPN_CONFIRMATION_TTL_SECONDS = _int("VPN_CONFIRMATION_TTL_SECONDS", 900)

# Asked when a money operation has been recorded and is waiting. It names the
# operation and its subject, because the owner is being asked to approve a
# specific thing and «اوکی» to an unnamed request is not an approval.
VPN_CONFIRM_REQUIRED_TEXT = os.getenv(
    "VPN_CONFIRM_REQUIRED_TEXT",
    "🔐 این عملیات روی سرویس VPN پول یا سفارش‌ها رو تغییر می‌ده، پس هنوز "
    "اجرا نشده:\n"
    "• {operation}\n"
    "• {subject}\n\n"
    "اگه مطمئنی، صریح تأییدش کن.",
)
# The same state without the detail, for the outcome-to-sentence table. The
# sentence above carries ``{operation}`` and ``{subject}``, so it is only ever
# used where both are known; this is what a caller that has no operation to name
# gets, so a placeholder can never reach a chat.
VPN_AWAITING_CONFIRMATION_TEXT = os.getenv(
    "VPN_AWAITING_CONFIRMATION_TEXT",
    "🔐 این عملیات VPN نیاز به تأیید صریح تو داره، پس هنوز اجرا نشده.",
)
VPN_CONFIRM_NOTHING_TEXT = os.getenv(
    "VPN_CONFIRM_NOTHING_TEXT",
    "الان هیچ عملیات VPNی منتظر تأیید نیست.",
)
VPN_CONFIRM_AMBIGUOUS_TEXT = os.getenv(
    "VPN_CONFIRM_AMBIGUOUS_TEXT",
    "چند عملیات VPN منتظر تأیید هستن؛ کدوم رو تأیید می‌کنی؟",
)
VPN_CONFIRM_NOT_WAITING_TEXT = os.getenv(
    "VPN_CONFIRM_NOT_WAITING_TEXT",
    "اون عملیات در انتظار تأیید نیست.",
)
VPN_CONFIRM_EXPIRED_TEXT = os.getenv(
    "VPN_CONFIRM_EXPIRED_TEXT",
    "⌛️ مهلت تأیید این عملیات گذشته، پس اجرا نشد. دوباره درخواستش کن.",
)
VPN_CONFIRM_OWNER_ONLY_TEXT = os.getenv(
    "VPN_CONFIRM_OWNER_ONLY_TEXT",
    "⛔️ تأیید عملیات VPN فقط کار مالکه.",
)
VPN_CONFIRMED_TEXT = os.getenv(
    "VPN_CONFIRMED_TEXT",
    "✅ تأیید شد؛ عملیات VPN اجرا شد.",
)
# The VPN bot could not be asked at all — not configured, unreachable, or
# answering something that is not JSON. Distinct from a refusal, because the
# next step is to look at the integration rather than at the request.
VPN_UNAVAILABLE_TEXT = os.getenv(
    "VPN_UNAVAILABLE_TEXT",
    "⛔️ الان به سرویس VPN دسترسی ندارم، پس هیچ تغییری اعمال نشد. "
    "لطفاً بعداً دوباره امتحان کن.",
)
# The VPN bot answered, and the answer was no: an unknown id, a panel error, a
# trial plan that may not be switched off, an amount beyond the fat-finger
# limit. The reason is in the detail; this is the sentence.
VPN_REFUSED_TEXT = os.getenv(
    "VPN_REFUSED_TEXT",
    "⛔️ سرویس VPN این درخواست رو رد کرد، پس چیزی تغییر نکرد.",
)
# A failure on our side of the wire that is not the other service's decision —
# a malformed operation, or a pending record that could not be written.
VPN_FAILED_TEXT = os.getenv(
    "VPN_FAILED_TEXT",
    "⚠️ این عملیات VPN کامل نشد. جزئیات در گزارش ثبت شد.",
)


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

# ---------------- Gemini: the conversational assistant ------------------------
# A second, entirely independent Gemini workload. It answers somebody who talks
# to the bot directly; it has nothing to do with deciding whether a group
# message is a lead.
#
# Why it is separate, and why it needs its own key:
#
#   * Gemini rate limits are applied **per Google Cloud project**, not per API
#     key. Two keys in the same project share one allowance. So "a separate key"
#     only gives a separate budget if it belongs to a different project — and
#     that is the whole point of the exercise, because a chatty user must not be
#     able to exhaust the acquisition classifier's daily quota.
#     Verified against the official docs on 2026-09-21:
#     https://ai.google.dev/gemini-api/docs/rate-limits
#     "Rate limits are applied per project, not per API key. Requests per day
#     (RPD) quotas reset at midnight Pacific time." — the same page also
#     confirms that no static free-tier table is published any more, which is
#     why this file carries conservative defaults and `db.ai_day()` measures the
#     Pacific boundary rather than UTC.
#   * The workloads have opposite shapes. Acquisition is many short, cheap,
#     high-stakes classifications. Chat is fewer, longer, multi-turn requests
#     whose output a person is waiting for. One quota tuned for either is wrong
#     for the other.
#   * Failures must not propagate. Chat being down must leave acquisition
#     working, and vice versa. Separate state (rate window, circuit breaker,
#     counters, client) is what makes that true rather than hoped for.
#
# A consumer Gemini app subscription is *not* a developer quota: the same Google
# account can have a Pro/Ultra Gemini subscription and a free-tier API project,
# and the API is still bounded by the API tier. Nothing here may assume
# otherwise.
#
# Google publishes no static free-tier table — limits are per project and must
# be read from AI Studio for the account in question. The defaults below are
# therefore deliberately conservative, and the real ceilings belong in .env
# once measured for the key you supply.
GEMINI_CHAT_ENABLED = _bool("GEMINI_CHAT_ENABLED", False)

# Must belong to a different Google Cloud project from GEMINI_API_KEY, or the
# separation above is nominal. Never logged, never in status().
GEMINI_CHAT_API_KEY = os.getenv("GEMINI_CHAT_API_KEY", "").strip()

# Whether the assistant may fall back to GEMINI_API_KEY when no chat key is set.
#
# Off by default, and it is an explicit opt-in rather than an automatic fallback
# because of what it costs: Google applies rate limits **per project**, so a
# shared key means a shared Google allowance even though this application keeps
# separate counters. A busy conversation can then push the classifier into a
# 429, at which point it degrades to the rule engine — safe, but a real
# behaviour change that an operator should choose knowingly rather than
# discover.
#
# What is *not* shared, either way: the counters, the daily cap, the rate window
# and the circuit breaker in this application. Chat can never spend the
# classifier's 200-call allowance or trip its breaker, and that is asserted in
# tests/test_chat.py. Turn this on to make the assistant work on a deployment
# with one key; leave it off, and set GEMINI_CHAT_API_KEY from a second project,
# for genuinely independent quotas.
GEMINI_CHAT_ALLOW_SHARED_KEY = _bool("GEMINI_CHAT_ALLOW_SHARED_KEY", False)

# The conversational model. This is a different job from classification: the
# reply is longer, is read by a human, and benefits from a stronger model. It is
# its own setting precisely so the two can be tuned apart.
#
# What was measured on this key on 2026-09-21 (one real call each, `models.list`
# for availability) — re-measure before changing any of it:
#
#   * `gemini-flash-lite-latest`  answers, in fluent Persian. The default.
#   * `gemini-3.5-flash-lite`     answers, same quality.
#   * `gemini-flash-latest`       answers, but returned *no visible text* at a
#                                 small output budget. It is a thinking model:
#                                 the budget is spent on internal reasoning and
#                                 `response.text` comes back empty, which
#                                 `chat.reply` correctly reports as
#                                 `empty_response` rather than sending a blank
#                                 message. Raising GEMINI_CHAT_MAX_TOKENS in
#                                 `_request` is what such a model needs — not a
#                                 prompt change.
#   * `gemini-2.5-flash` and `gemini-2.5-flash-lite` are **gone**: the API
#                                 answers `404 ... no longer available`. The
#                                 2.5 generation was retired, so pinning a
#                                 version number is the fragile choice here and
#                                 the `-latest` alias is the durable one.
#
# `models.list()` on this key returns ~41 generateContent models including the
# 3.x family (gemini-3.8-flash, gemini-3.5-flash-lite, gemini-3.1-flash-lite…),
# so the choice is real. There is no separate "chat" endpoint or product to
# reach for: conversational use is the same `generateContent` API and the same
# per-project limits as the classifier — which is why the separation in this
# file is about keys, counters and breakers, not about a different API.
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-flash-lite-latest").strip()

# Longer than the classifier's 10s. A person waiting for a considered reply will
# wait; a group message being classified is on a message-handler budget and
# cannot. Still bounded, because a Telegram handler must never wait forever.
GEMINI_CHAT_TIMEOUT_SECONDS = _float("GEMINI_CHAT_TIMEOUT_SECONDS", 25.0)
GEMINI_CHAT_MAX_RETRIES = _int("GEMINI_CHAT_MAX_RETRIES", 1)
GEMINI_CHAT_BACKOFF_SECONDS = _float("GEMINI_CHAT_BACKOFF_SECONDS", 1.5)

# Its own brake. Lower than the classifier's because each request is larger and
# a person can only read so fast, and because a chatty user is exactly the load
# this is meant to absorb without touching the other budget.
GEMINI_CHAT_RATE_LIMIT = _int("GEMINI_CHAT_RATE_LIMIT", 6)
GEMINI_CHAT_RATE_WINDOW = _float("GEMINI_CHAT_RATE_WINDOW", 60.0)

# One person's own brake. The window above bounds the whole deployment; this
# bounds a single conversation, so nobody can hold down the send key and spend
# the day's allowance before anyone else gets a reply. Keyed by (chat, user), so
# it follows the person rather than the room.
GEMINI_CHAT_USER_RATE_LIMIT = _int("GEMINI_CHAT_USER_RATE_LIMIT", 5)
GEMINI_CHAT_USER_RATE_WINDOW = _float("GEMINI_CHAT_USER_RATE_WINDOW", 30.0)

# Its own daily ceiling, counted on the same Pacific boundary but in its own
# table, so the two can never be added together by accident.
#
# **This is a per-account allowance, not a deployment-wide one.** It is handed
# to the chat pool, which spends one account's day and then fails over to the
# next — the same failover it performs for a 429 — so a deployment with two chat
# accounts can serve twice this per day, and one with three can serve three
# times it. That is the point: a single shared ceiling was reached while a
# second configured key with a full day sat unused, and the group was told its
# quota was used up when it was not.
#
# The workload still reports `daily_cap` to a user only when *every* chat
# account has spent its own allowance. The total actually spendable is
# `GEMINI_CHAT_DAILY_LIMIT × number of chat accounts`, and `/chat` reports the
# remaining figure directly rather than leaving it to be worked out.
GEMINI_CHAT_DAILY_LIMIT = _int("GEMINI_CHAT_DAILY_LIMIT", 200)

GEMINI_CHAT_CIRCUIT_FAILURES = _int("GEMINI_CHAT_CIRCUIT_FAILURES", 5)
GEMINI_CHAT_CIRCUIT_SECONDS = _float("GEMINI_CHAT_CIRCUIT_SECONDS", 300.0)

# What one incoming message is truncated to before it leaves the server.
GEMINI_CHAT_MAX_CHARS = _int("GEMINI_CHAT_MAX_CHARS", 1500)

# Conversation memory: how many turns are replayed to the model, and how long a
# quiet conversation is remembered. Bounds growth from both directions — turns
# bound a busy conversation, the TTL bounds an abandoned one.
GEMINI_CHAT_HISTORY_TURNS = _int("GEMINI_CHAT_HISTORY_TURNS", 8)
GEMINI_CHAT_HISTORY_TTL = _int("GEMINI_CHAT_HISTORY_TTL", 1800)

# The reply is truncated to this before it is sent. Telegram's hard limit is
# 4096 characters; the margin is for the escaping and the length notice.
GEMINI_CHAT_REPLY_CHARS = _int("GEMINI_CHAT_REPLY_CHARS", 3500)

# Sent by /reset. The one piece of this feature's copy that a user can trigger
# deliberately, so it lives here with the rest of the wording.
GEMINI_CHAT_RESET_TEXT = os.getenv(
    "GEMINI_CHAT_RESET_TEXT",
    "گفتگو پاک شد. از نو شروع کن 🙂",
)

# Sent when somebody presses Start in a private chat. Without it the Start button
# — the first thing anyone presses — produces silence, because /start is a
# command and the conversational handler deliberately ignores commands.
GEMINI_CHAT_START_TEXT = os.getenv(
    "GEMINI_CHAT_START_TEXT",
    "سلام {name} 👋\n"
    "هر سوالی داشتی همین‌جا بپرس.\n"
    "اگه خواستی گفتگو رو از صفر شروع کنی، /reset رو بزن.",
)


# ---------------- Bot identity ------------------------------------------------
# What the bot answers to. The authoritative source is Telegram itself: the bot's
# own id and username come from ``getMe`` at startup and are what ``@mention``
# and reply-to-bot matching use, because those are the only two mechanisms
# Telegram makes unambiguous (see app/main.py `_addressed_to_bot`).
#
# BOT_ALIASES is for the *human* names the group actually uses in text — the
# Persian word for "robot", a nickname, a transliteration. It is deliberately
# empty by default and deliberately separate from the username: matching a
# bare word is a heuristic, and a heuristic that decides whether the bot speaks
# should be a decision an operator makes rather than something this file
# assumes. Entries are matched case-insensitively against a whole word.
BOT_ALIASES = _str_list(os.getenv("BOT_ALIASES", ""))

# How long a Telegram chat-member lookup is trusted. The admin check is a live
# API call, and an unprivileged member can trigger it by talking; caching is
# what stops that from being a way to spend the bot's rate limit. Short enough
# that a demotion takes effect quickly.
ADMIN_CACHE_SECONDS = _float("ADMIN_CACHE_SECONDS", 300.0)


# ---------------- Application authorization (RBAC) ---------------------------
# The primary owner, by Telegram user id. This is the highest application-level
# authority and it is **immutable at runtime**: no command, no button and no
# group message can create it, change it, or take it away. It is a deployment
# fact, which is exactly what makes it safe to compare against — a privilege
# that can be granted from inside the system is a privilege an attacker can ask
# for.
#
# 0 means "no owner configured", in which case every administrative command is
# refused (fail closed) and the startup log says so loudly. It does not fall
# back to "the first admin wins" or "the whitelist is the owner": those are both
# ways for the wrong person to end up in charge.
OWNER_USER_ID = _int("OWNER_USER_ID", 0)

# Roles that are seeded from configuration at startup rather than through the
# bot. Format: ``<user_id>:<role>``, comma separated, e.g.
# ``123456:senior_admin,789012:moderator``. This exists so a deployment can come
# up with its staff already in place without the owner having to promote
# everyone by hand through Telegram — and it is the only way to recover if the
# admins table is lost.
#
# A configured role can never be the owner (that is OWNER_USER_ID alone) and can
# never exceed the role's own permission ceiling. An entry that names a higher
# role than the owner's own grant list would allow is refused at load time and
# logged, not silently applied.
CONFIG_ADMINS = _str_list(os.getenv("CONFIG_ADMINS", ""))


# ---------------- AI-mediated administration ---------------------------------
# Whether the assistant may propose administrative actions at all.
#
# This switch does **not** change what any action requires. It only decides
# whether the conversational model is given administrative tools to call, and
# whether a tool call it produces is executed. Every call is authorised by
# ``app/rbac.py`` regardless of this setting, so turning it on cannot widen
# anybody's authority — it only adds a second way to *ask*.
#
# Turning it off leaves the direct commands working, which is the whole point of
# having two modes: a Gemini outage must never take group administration down.
ADMIN_AI_ENABLED = _bool("ADMIN_AI_ENABLED", True)

# Whether the direct Python commands work. Off is for a deployment that wants
# the model to be the only interface; it is not a security control, because the
# commands are the fallback that keeps the group manageable when the model is
# not.
ADMIN_PYTHON_ENABLED = _bool("ADMIN_PYTHON_ENABLED", True)

# How old an administrative request may be before it is refused as stale.
#
# This is the replay window. A request that a model produced is stamped when it
# is produced and must be executed in the same turn; anything older has been
# sitting somewhere, which is the shape of a replay rather than of a live
# request. 0 disables the check, which is only sensible for the in-process
# Python path — that path cannot be replayed, because it has no representation
# outside the call stack.
ADMIN_REQUEST_REPLAY_WINDOW = _int("ADMIN_REQUEST_REPLAY_WINDOW", 120)

# How long a request id is remembered. Must be at least the replay window, or a
# request could be forgotten while it is still replayable. The floor is applied
# here rather than trusted to the operator, because the failure is silent.
ADMIN_IDEMPOTENCY_RETENTION = max(
    _int("ADMIN_IDEMPOTENCY_RETENTION", 86400),
    ADMIN_REQUEST_REPLAY_WINDOW,
)

# How long administrative audit rows are kept. The audit trail is the answer to
# "who did this", so the default is long; an operator with a data-retention
# obligation can shorten it. Pruning happens on the administrative path, since
# this process has no scheduler.
ADMIN_ACTIVITY_RETENTION = _int("ADMIN_ACTIVITY_RETENTION", 90 * 86400)

# How many recent administrative events the assistant may be shown when it asks
# for context, and over what window. Both bounds exist: the count keeps a busy
# room from filling the prompt, the window keeps an old incident from being
# re-litigated. This is the only administrative history that reaches the model.
ADMIN_CONTEXT_LIMIT = _int("ADMIN_CONTEXT_LIMIT", 12)
ADMIN_CONTEXT_WINDOW = _int("ADMIN_CONTEXT_WINDOW", 6 * 3600)

# How many tool calls the model may make in one turn before the loop stops.
# A bound rather than a timeout, because the failure mode is a model that keeps
# asking, and a turn that never ends is worse than one that ends with "I could
# not finish". Each call is still authorised individually.
ADMIN_TOOL_MAX_CALLS = _int("ADMIN_TOOL_MAX_CALLS", 4)

# Whether an ordinary member is offered the *read-only* tools.
#
# Off by default, for two reasons that point the same way. The brief asks that
# the complete tool list not be exposed to normal members unnecessarily, and the
# read tools are not free: they let any member enumerate the administrator
# roster and read anybody's role and permission set. Neither is a secret inside
# a group, but neither is something an ordinary conversation needs either.
#
# The second reason is cost. Offering tools at all switches the turn onto the
# tool-aware transport, which sends every declaration with every message. For an
# administrator that is the price of being able to act; for a guest it is the
# price of nothing.
#
# An operator who wants members to be able to ask "what is my role?" can turn
# this on. It grants no write tool under any setting — that is decided by RBAC,
# not here.
ADMIN_TOOL_GUEST_TOOLS = _bool("ADMIN_TOOL_GUEST_TOOLS", False)

# The sentences for the two outcomes that only exist because requests are typed
# and replay-protected. They live with the other administrative copy below, in
# the text section, so every operator-facing string is in one place.


# ---------------- Nexus: the conversational layer ----------------------------
# "Nexus" is the name this project gives to the conversational AI layer as a
# *role*: natural-language understanding, context, intent and orchestration. It
# is not a model identity. Which provider and which model answer is decided by
# the pool and by the ``GEMINI_CHAT_*`` settings above, and changing them does
# not change what Nexus is.
#
# The layer is deliberately narrow. Nexus may *ask*; ``app/admin_service.py``
# decides and executes. Nothing here widens anybody's authority: these settings
# decide who Nexus listens to, when it is awake, and how much it remembers.

# Whether Nexus answers only authorized administrators, or anybody who addresses
# it.
#
# On by default, and that default is a product decision rather than a technical
# one: the brief requires that an ordinary member cannot activate the assistant
# by replying to it, mentioning it, or wording an administrative-sounding
# request. With this on, an unauthorized message costs one dictionary lookup and
# is never sent to Gemini.
#
# Turning it off restores the earlier behaviour, where the assistant answered any
# member who addressed it directly. It does not change what an *action* requires
# — every tool call is authorised against the real actor id either way — so the
# only thing this switch moves is who gets to have a conversation.
NEXUS_ACTORS_ONLY = _bool("NEXUS_ACTORS_ONLY", True)

# The names Nexus answers to, in addition to the bot's own username and
# ``BOT_ALIASES``. A group often calls the assistant by its role name rather
# than by the bot's Telegram username, and "نکسوس" is not a username anybody
# can mention with an @.
#
# Matched as whole words, case-insensitively, and only for the purposes of
# deciding that a message is *aimed at Nexus*. It is not an authority of any
# kind: a stranger writing "نکسوس" is still refused by the actor gate.
NEXUS_NAMES = _str_list(os.getenv("NEXUS_NAMES", "nexus,نکسوس"))

# Whether Nexus records what an authorized administrator says when they are not
# talking to it.
#
# This is the feature the brief calls "watch without reply": an administrator
# says something in the room, Nexus stores it in that administrator's own
# bounded conversation context, and stays silent. It is what lets a later
# "بنش کن" be understood as a follow-up to "این کاربر خیلی مزاحم شده".
#
# Off makes Nexus stateless between addressed messages. On costs one row per
# administrator message and no AI call at all — the model is not consulted until
# somebody actually asks for something.
NEXUS_OBSERVE_ADMINS = _bool("NEXUS_OBSERVE_ADMINS", True)

# Extra words that make an unaddressed message worth consulting the model about.
#
# The built-in lexicon lives in ``app/nexus.py`` and covers the Persian and
# English verbs of moderation. This is the operator's escape hatch for a room
# whose slang the lexicon does not know: comma separated, matched as whole
# words. It can only ever cause *more* messages to be looked at — the model still
# decides what was asked, and ``admin_service`` still decides whether it may
# happen — so a wrong entry costs one AI call, never an action.
NEXUS_EXTRA_ACTION_WORDS = _str_list(os.getenv("NEXUS_EXTRA_ACTION_WORDS", ""))

# The identity memory: the mapping from a name somebody said out loud to the
# Telegram user id that actually identifies them.
#
# It records names and usernames only, for people who have spoken in a monitored
# group. There is no column for a message body. It exists so "میلاد رو بن کن"
# can be resolved, and it deliberately cannot grant anything — a row is written
# for every speaker, including people with no role at all, and authority is
# resolved separately from ``app/rbac.py``.
NEXUS_PEOPLE_ENABLED = _bool("NEXUS_PEOPLE_ENABLED", True)
# A ceiling on the table. The least recently seen rows are dropped first, so a
# busy group keeps the people who are actually present.
NEXUS_PEOPLE_MAX = _int("NEXUS_PEOPLE_MAX", 5000)
# And an age bound, so somebody who left months ago does not stay resolvable
# forever. Pruned on the observation path, since this process has no scheduler.
NEXUS_PEOPLE_RETENTION = _int("NEXUS_PEOPLE_RETENTION", 90 * 86400)
# How many candidates a name lookup may return before it is treated as
# ambiguous. Not a matching threshold — an exact, normalised comparison decides
# a match — this only bounds what is shown to the model when several people
# share a name.
NEXUS_PEOPLE_MAX_CANDIDATES = _int("NEXUS_PEOPLE_MAX_CANDIDATES", 8)


# ---------------- Nexus Awareness: the room, understood -----------------------
# The observation layer. Everything above decides *who may talk to Nexus and what
# it may do*; this decides *what Nexus understands about the room it is in*.
#
# The distinction the brief draws, and the one this section implements:
#
#   collecting a bounded recent window of the room's conversation  ← no AI call
#   deciding whether that conversation concerns Nexus, and whether
#   speaking would help                                          ← Gemini's job
#   deciding whether a privileged action may happen              ← the server
#
# The window is captured for every message the bot can actually receive
# (see ``main._nexus_can_observe``), and a *batched, debounced* pass hands the
# window to Gemini. One pass covers a whole burst of messages, which is what
# keeps "continuously aware" from meaning "one API call per message".
#
# Nothing here widens anybody's authority. Awareness observes; it never
# authorises. A member's message is understood and still cannot produce an
# action, because every tool call is authorised again from the actor's id.

# The master switch. Off restores the pre-Awareness behaviour: an unaddressed
# message is never analysed, and the only path to the model is the addressed
# one. Note that ``nexus.looks_actionable`` no longer reaches the model by
# itself — since Awareness it is a timing hint for this layer, so switching
# Awareness off removes the unaddressed path entirely rather than handing it
# back to the keyword list.
NEXUS_AWARENESS_ENABLED = _bool("NEXUS_AWARENESS_ENABLED", True)

# How often the sweeper looks for a chat with something new to understand. It is
# a *poll*, not a call: a tick that finds nothing pending costs one indexed read
# per configured group and no API call at all.
NEXUS_AWARENESS_TICK_SECONDS = _float("NEXUS_AWARENESS_TICK_SECONDS", 15.0)

# Wait for the room to go quiet for this long before analysing. A burst of
# twenty messages therefore costs one pass rather than twenty.
NEXUS_AWARENESS_DEBOUNCE_SECONDS = _float("NEXUS_AWARENESS_DEBOUNCE_SECONDS", 8.0)

# But a busy room never goes quiet, so there is also a ceiling on how long a
# message may sit unread. Whichever comes first — the room falling silent or
# this much time passing since the oldest unread message — triggers the pass.
NEXUS_AWARENESS_MAX_WAIT_SECONDS = _float("NEXUS_AWARENESS_MAX_WAIT_SECONDS", 45.0)

# And a floor between two passes in the same chat, so a room that is busy
# continuously is understood at a steady, bounded rate rather than as fast as
# messages arrive.
NEXUS_AWARENESS_MIN_INTERVAL_SECONDS = _float(
    "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 20.0
)

# The bounded window itself: how many recent messages are shown, and how many
# characters they may occupy in total. Both bounds are applied — a count bound
# alone lets forty long messages become a huge prompt, and a character bound
# alone lets a flood of one-word messages push the real context out.
NEXUS_AWARENESS_WINDOW_MESSAGES = _int("NEXUS_AWARENESS_WINDOW_MESSAGES", 40)
NEXUS_AWARENESS_WINDOW_CHARS = _int("NEXUS_AWARENESS_WINDOW_CHARS", 6000)

# How long a captured message is kept. The window is a *recent* view of the
# room, not a transcript: rows older than this are dropped, which is what stops
# the table from becoming a permanent record of the group's conversation.
NEXUS_AWARENESS_RETENTION_SECONDS = _int("NEXUS_AWARENESS_RETENTION_SECONDS", 3600)

# A ceiling on the table as well as on the age, because a busy hour can produce
# more rows than the age bound alone would remove. Applied per chat, oldest
# first.
NEXUS_AWARENESS_MAX_ROWS = _int("NEXUS_AWARENESS_MAX_ROWS", 400)

# How often the age-based purge may run. It is the one capture-path statement
# that is not scoped to a chat, and the retention window it enforces is measured
# in hours, so enforcing it once per received message was a full-table scan on
# the hottest path in the feature. The per-chat row ceiling above still runs on
# every capture, which is what actually bounds the table within a burst.
NEXUS_AWARENESS_PURGE_INTERVAL_SECONDS = _float(
    "NEXUS_AWARENESS_PURGE_INTERVAL_SECONDS", 60.0
)

# How many chats one tick may analyse. A tick that is still working when the
# next one arrives would otherwise pile passes on top of each other; this keeps
# the sweeper's cost bounded and its behaviour predictable.
NEXUS_AWARENESS_MAX_CHATS_PER_TICK = _int("NEXUS_AWARENESS_MAX_CHATS_PER_TICK", 2)

# The awareness workload's own daily ceiling, per account, exactly as
# ``GEMINI_CHAT_DAILY_LIMIT`` is per account. When it is spent, awareness stops
# for the day and the assistant keeps working — the two budgets are separate on
# purpose, so an observant Nexus can never spend the allowance a person is
# waiting on an answer to.
NEXUS_AWARENESS_DAILY_LIMIT = _int("NEXUS_AWARENESS_DAILY_LIMIT", 200)

# How many recent messages are replayed to the model when Nexus *answers*
# somebody directly. This is the same window, sized for a prompt that also
# carries the current turn and the administrative context.
NEXUS_AWARENESS_CONTEXT_MESSAGES = _int("NEXUS_AWARENESS_CONTEXT_MESSAGES", 20)

# Sent when an awareness pass actually performed an action but the model gave no
# wording for it. Rare, and the alternative is worse: an administrator whose
# instruction was carried out and never acknowledged believes it was ignored,
# and repeats it. The action's own outcome is in the audit log either way.
NEXUS_AWARENESS_ACTION_TEXT = os.getenv(
    "NEXUS_AWARENESS_ACTION_TEXT",
    "انجام شد ✅",
)


# ---------------- Gemini: moderation / content understanding ------------------
# A **third** independent Gemini workload. It is not the acquisition classifier
# and not the conversational assistant, and it shares nothing with either: its
# own key setting, its own model, its own rate window, its own daily cap, its
# own circuit breaker, its own counters table and its own client.
#
# What it is for: understanding what a piece of group content *is*, well enough
# for a deterministic policy to act on. The local detectors are good at one
# narrow question (is there an explicit body region in this frame) and bad at
# everything else — a sexual act with no exposed anatomy, a suggestive cartoon,
# harassment, a threat. This layer answers the wider question, and answers it
# with a small structured verdict rather than prose.
#
# What it is NOT for, and this is the architectural line the whole design turns
# on: **it never executes anything.** It cannot delete, restrict, ban or reply.
# Its output is data. The decision to act is made by app/mod_policy.py, in code,
# from its verdict plus the local detectors plus the configuration. There is no
# code path from this module's return value to a Telegram call, which is why a
# prompt-injected group message cannot make the bot do anything.
#
# Why a separate key matters here more than anywhere else: moderation runs on
# *every* media item and a large share of text, so it is by far the largest
# consumer. If it shared the classifier's project it would starve acquisition
# and chat — and Google's limits are per project, not per key.
GEMINI_MOD_ENABLED = _bool("GEMINI_MOD_ENABLED", False)

# Must belong to a different Google Cloud project from GEMINI_API_KEY and
# GEMINI_CHAT_API_KEY for the budget to be genuinely separate. Never logged.
GEMINI_MOD_API_KEY = os.getenv("GEMINI_MOD_API_KEY", "").strip()

# The same explicit opt-in as the assistant's, for the same reason: a shared key
# is a shared Google allowance even though our counters are separate.
GEMINI_MOD_ALLOW_SHARED_KEY = _bool("GEMINI_MOD_ALLOW_SHARED_KEY", False)

# Its own model. Measured on this key on 2026-09-21: `gemini-flash-lite-latest`
# answers, and is the default because moderation runs on a per-message budget
# and this is the cheapest model that is good enough. A moderation verdict is
# short and structured, so the stronger (and slower) models buy little here.
GEMINI_MOD_MODEL = os.getenv("GEMINI_MOD_MODEL", "gemini-flash-lite-latest").strip()

# Longer than the classifier's 10s, because a video or a voice clip is a much
# bigger input than a line of text. Still bounded, and the media path runs off
# the event loop.
GEMINI_MOD_TIMEOUT_SECONDS = _float("GEMINI_MOD_TIMEOUT_SECONDS", 20.0)
GEMINI_MOD_MAX_RETRIES = _int("GEMINI_MOD_MAX_RETRIES", 1)
GEMINI_MOD_BACKOFF_SECONDS = _float("GEMINI_MOD_BACKOFF_SECONDS", 1.5)

# The tightest brake of the three workloads. Moderation is the highest-volume
# consumer, and its failure mode is benign (content is allowed and logged), so
# it is the one that should yield first when the project is under pressure.
GEMINI_MOD_RATE_LIMIT = _int("GEMINI_MOD_RATE_LIMIT", 20)
GEMINI_MOD_RATE_WINDOW = _float("GEMINI_MOD_RATE_WINDOW", 60.0)
GEMINI_MOD_DAILY_LIMIT = _int("GEMINI_MOD_DAILY_LIMIT", 500)
GEMINI_MOD_CIRCUIT_FAILURES = _int("GEMINI_MOD_CIRCUIT_FAILURES", 5)
GEMINI_MOD_CIRCUIT_SECONDS = _float("GEMINI_MOD_CIRCUIT_SECONDS", 300.0)

# Text is truncated to this before it leaves the server. Media is bounded
# separately, by bytes and by duration — see GEMINI_MEDIA_* below.
GEMINI_MOD_MAX_CHARS = _int("GEMINI_MOD_MAX_CHARS", 2000)


# ---------------- Media understanding (shared by moderation and chat) --------
# One builder for "Telegram media -> something Gemini can read", used by both
# the moderation workload and the conversational one. The *builder* is shared;
# the policies, limits and keys above and below are not.
#
# Measured on this deployment's key on 2026-09-21, one real call each, to decide
# what the builder may actually send:
#
#     image/png   inline  64 B..  OK   (colour described correctly)
#     image/gif   inline  1.2 KB  OK
#     video/mp4   inline  1.9 KB  OK
#     video/webm  inline  1.1 KB  OK
#     audio/wav   inline   32 KB  OK
#     audio/ogg   inline  2.6 KB  OK
#
# So all five families work as **inline** parts, which is what this builder
# uses. The Files API also works (upload -> PROCESSING -> generateContent by
# URI -> delete), and is deliberately *not* used: it would leave a copy of a
# group member's media in Google's storage for the life of the file, for no
# capability we need. Telegram's own Bot API download limit is 20 MB, and the
# inline request limit is the same order, so there is nothing the Files API
# would unlock for us anyway.
GEMINI_MEDIA_MAX_MB = _float("GEMINI_MEDIA_MAX_MB", 18.0)

# A video that is too large or too long to send whole is reduced to this many
# frames, which are sent as images. This is the documented fallback the API
# supports, and it is why a long clip does not silently become "not analysed".
GEMINI_MEDIA_FRAMES = _int("GEMINI_MEDIA_FRAMES", 4)

# A hard ceiling on the parts one request may carry, so a pathological message
# cannot turn into an unbounded upload.
GEMINI_MEDIA_MAX_PARTS = _int("GEMINI_MEDIA_MAX_PARTS", 6)

# The longest video/audio we will send whole. Beyond this the video path falls
# back to frames, and the audio path refuses rather than truncating mid-word
# (a half sentence is a wrong sentence).
GEMINI_MEDIA_MAX_SECONDS = _float("GEMINI_MEDIA_MAX_SECONDS", 60.0)


# ---------------- The moderation policy --------------------------------------
# The deterministic engine that turns signals into an action. Everything here is
# about *how sure we have to be before we destroy something*, and the defaults
# are deliberately the conservative end.
#
# The problem this solves, stated plainly: the local detector alone was deleting
# media at a threshold low enough that an ordinary celebrity photograph could
# cross it. A single uncalibrated score is not a good enough reason to delete
# somebody's message. So the local detector's role is now **evidence, not a
# verdict**: it can raise a candidate, and it can no longer delete on its own.
MODERATION_ENABLED = _bool("MODERATION_ENABLED", True)

# Whether a deletion must be confirmed by the moderation AI.
#
# True (the default) means: local detector says explicit, AI disagrees or cannot
# be asked -> the content is *allowed and logged*, never deleted. That is the
# fail-safe direction the brief asks for, and it is why turning this on makes the
# bot strictly less destructive than it was.
#
# False means the local detector may delete alone, but only at or above
# MODERATION_LOCAL_HARD_THRESHOLD — a much higher bar than the old
# EXPLICIT_DELETE_THRESHOLD, and one that is deliberately hard to reach. Set it
# False only if you have decided the AI layer is unavailable and you still want
# deletions; the startup log says which mode is in force.
MODERATION_REQUIRE_AI_CONFIRM = _bool("MODERATION_REQUIRE_AI_CONFIRM", True)

# The AI's confidence must be at least this before its "clearly explicit"
# classification is acted on. Below it the verdict is treated as uncertain and
# the content is only logged.
MODERATION_DELETE_CONFIDENCE = _float("MODERATION_DELETE_CONFIDENCE", 0.80)

# The band below the delete confidence that is still worth recording: the
# content is allowed, but an operator can see it in the log and in the review
# queue. A false positive here costs a log line, not a message.
MODERATION_REVIEW_CONFIDENCE = _float("MODERATION_REVIEW_CONFIDENCE", 0.45)

# The local detector's own hard bar, used only when
# MODERATION_REQUIRE_AI_CONFIRM is False. Set above every true positive measured
# on this deployment (0.50/0.51/0.56/0.67) on purpose: this mode exists for a
# deployment that has chosen to run without the AI layer, and it should be
# visibly stricter than the AI-confirmed path rather than quietly equivalent.
MODERATION_LOCAL_HARD_THRESHOLD = _float("MODERATION_LOCAL_HARD_THRESHOLD", 0.85)

# Which content classes the policy may ever act destructively on. This is the
# closed vocabulary the moderation AI's verdict is coerced into, and the set the
# policy switches on — a category outside it can never produce a deletion, so a
# hallucinated label is inert.
#
#   explicit_sexual   clearly explicit sexual content          -> deletable
#   suggestive        sexual but not explicit                  -> never deleted
#   harassment        targeted abuse of a person               -> never deleted
#   threat            a threat of harm                         -> never deleted
#   spam              advertising / flooding                   -> never deleted
#   normal            ordinary content                         -> never deleted
#   unknown           could not be judged                      -> never deleted
#
# Only `explicit_sexual` is in MODERATION_DELETABLE_CLASSES by default. The
# others are logged so an operator can see them and decide, which is the
# "recommend a future restriction" half of the brief: the architecture carries
# the signal, the policy decides not to act on it yet.
MODERATION_CLASSES = (
    "explicit_sexual",
    "suggestive",
    "harassment",
    "threat",
    "spam",
    "normal",
    "unknown",
)
MODERATION_DELETABLE_CLASSES = set(
    _str_list(os.getenv("MODERATION_DELETABLE_CLASSES", "explicit_sexual"))
)

# Whether text is sent to the moderation layer at all.
#
# **Off by default**, and that is a deliberate judgement rather than an
# oversight. This is the one part of the moderation layer that can delete a
# person's *words* rather than a picture, in a language the model may misjudge,
# and a false positive here removes something somebody wrote and cannot get
# back. The capability is implemented and tested; turning it on is a decision
# the operator should make after watching the review log for a while, not a
# default this file imposes.
#
# Media moderation does not depend on this: a photo is still sent to the
# moderation layer when this is off.
MODERATION_TEXT_ENABLED = _bool("MODERATION_TEXT_ENABLED", False)

# Messages shorter than this are not sent to the moderation layer. A three-word
# line has almost no signal for a content classifier, and the cost is a request
# against a shared quota.
MODERATION_TEXT_MIN_CHARS = _int("MODERATION_TEXT_MIN_CHARS", 25)

# Whether media is sent to it. This is the expensive half — a video is a much
# larger request than a line of text — so it has its own switch.
MODERATION_MEDIA_ENABLED = _bool("MODERATION_MEDIA_ENABLED", True)

# Whether the moderation AI is asked about media the local stage found *nothing*
# to say about.
#
# **Off by default**, and the default is the documented intent: the AI is the
# second opinion, so it is asked exactly when the first one had something to
# say. That is the cheapest rule and it is also the correct one, because the
# AI's value here is that it can *disagree* — asking it about content nobody
# doubted spends the quota to confirm the obvious.
#
# The guard used to be written as "skip when SAFE **and** the scene score is
# absent", and the second clause made the first one dead: the scene classifier
# returns a number for every image it touches, so the AI was asked about every
# image posted in the group. On a deployment whose moderation workload has no
# daily budget, that is an unbounded cost, and it was never a decision anybody
# made — it was a conjunction that read as a condition.
#
# Set this to true to put the AI back on every image. It is a lever, not a
# recommendation: it trades quota for coverage, and it is the setting to reach
# for if a genuinely explicit image is ever reported as having been missed.
MODERATION_AI_ASK_ON_SAFE = _bool("MODERATION_AI_ASK_ON_SAFE", False)


# ── Update deduplication ──────────────────────────────────────────────────
# Whether one Telegram update may be handled more than once.
#
# It may not, and the reason is not tidiness. Telegram re-delivers an update it
# is not certain was received — after a network failure, and after a restart,
# because the update offset is not persisted and the bot asks for the backlog
# again. Handled twice, a message is answered twice, a moderation action runs
# twice, and a model call is paid for twice.
#
# See ``db.update_claim`` for the mechanism. The switch exists so that a
# deployment which somehow sees updates dropped can turn the guard off and get
# the previous behaviour back without a code change — but the default is on,
# because "handle every update exactly once" is the correct behaviour and the
# failure it prevents is invisible until it happens.
UPDATE_DEDUP_ENABLED = _bool("UPDATE_DEDUP_ENABLED", True)
# How long a claimed id is remembered. It only has to outlast Telegram's
# willingness to re-deliver, which is bounded by how long the bot was away —
# Telegram keeps undelivered updates for 24 hours. A day is therefore the honest
# bound, and the table costs a few hundred bytes a day at this bot's traffic.
UPDATE_DEDUP_TTL_SECONDS = _int("UPDATE_DEDUP_TTL_SECONDS", 24 * 3600)
# How often the claimed ids are swept. The sweep is one indexed DELETE, so it
# runs on the captcha reaper's existing timer rather than on one of its own.
UPDATE_DEDUP_PRUNE_INTERVAL_SECONDS = _float(
    "UPDATE_DEDUP_PRUNE_INTERVAL_SECONDS", 3600.0
)


# ── The coding-agent bridge ───────────────────────────────────────────────
# See ``app/agent_bridge.py`` for the design and ``AgentMD.md`` §36 for the
# deployment. The short version: the owner asks Nexus for a coding task in the
# group, Nexus turns it into a *tool call*, the one authority model in
# ``app/admin_service.py`` decides, and a host process runs CodeBuddy because
# this container ships neither Node nor the CLI.
AGENT_ENABLED = _bool("AGENT_ENABLED", True)

# The repository allowlist, as ``name=path`` pairs. A request names a name and
# the path is looked up here, so no expression a model can produce becomes a
# filesystem path. Both the container and the host runner check this list.
AGENT_REPOSITORIES = _kv_list(
    os.getenv(
        "AGENT_REPOSITORIES",
        "guardbot=/root/guardbot,vpn-bot=/opt/vpn-bot",
    )
)

# How many tasks may be in flight at once, and how many on one repository.
# One per repository is the number that matters: two agents editing one working
# tree produce a state neither of them can describe. The global ceiling is about
# the host, which has two cores and about a gigabyte free.
AGENT_MAX_ACTIVE = _int("AGENT_MAX_ACTIVE", 2)
AGENT_MAX_PER_REPOSITORY = _int("AGENT_MAX_PER_REPOSITORY", 1)

# Where the two halves meet. The runner writes ``<request_id>.progress`` and
# ``<request_id>.result`` here and the container reads them; the directory is
# inside the bind mount the compose file already provides, so neither side needs
# a new port or a new shared secret.
AGENT_SPOOL_DIR = os.getenv("AGENT_SPOOL_DIR", "/data/agent")

# How often the container looks for progress and results written by the runner.
# One directory listing per tick and no query when nothing is running, which is
# what makes a short interval affordable.
AGENT_POLL_SECONDS = _float("AGENT_POLL_SECONDS", 3.0)

# The runner's bounds. A coding task that has not finished in this long is
# stopped and reported rather than left to consume the host; the turn ceiling is
# the second bound, and it is the one that stops a loop early.
AGENT_TIMEOUT_SECONDS = _int("AGENT_TIMEOUT_SECONDS", 1800)
AGENT_MAX_TURNS = _int("AGENT_MAX_TURNS", 40)

# What the runner executes. The *mechanism* is the runner's, not configuration:
# it launches ``codebuddy --bg --name <task> --session-id <id> -p <prompt>``,
# because ``--bg`` is the only invocation measured to work on this host (the
# foreground ``-p`` never returns). What is left to configure is the executable
# and any *extra* flags, and both live in the runner's own environment rather
# than here — the container never names the program that runs. See AgentMD.md
# §40.15.
AGENT_CLI = os.getenv("AGENT_CLI", "codebuddy")
AGENT_CLI_ARGS = _str_list(os.getenv("AGENT_CLI_ARGS", ""))

# How long a progress line or a result may be before it is split, and how long
# an answer may be before a file is kinder than a wall of chat.
AGENT_CHUNK_CHARS = _int("AGENT_CHUNK_CHARS", 3500)
AGENT_DOCUMENT_CHARS = _int("AGENT_DOCUMENT_CHARS", 3500)
AGENT_PROGRESS_MAX_CHARS = _int("AGENT_PROGRESS_MAX_CHARS", 600)
# Progress is throttled: a chatty agent must not become a chatty bot.
AGENT_PROGRESS_MIN_INTERVAL_SECONDS = _float(
    "AGENT_PROGRESS_MIN_INTERVAL_SECONDS", 10.0
)
AGENT_PROGRESS_MAX_MESSAGES = _int("AGENT_PROGRESS_MAX_MESSAGES", 20)

# How long a finished task is kept before the retention prune drops it.
AGENT_RETENTION_SECONDS = _int("AGENT_RETENTION_SECONDS", 14 * 24 * 3600)

# Where the host runner may keep its own per-run HOME. Empty — the default —
# means the child inherits the real one, and that is deliberate: the CodeBuddy
# authentication lives in ``$HOME/.codebuddy``, and a child that cannot see it
# does not fail, it *succeeds* with "Authentication required" as its answer.
# Set this only to point the runner at a profile that is logged in.
AGENT_RUNNER_HOME = os.getenv("AGENT_RUNNER_HOME", "")

# ── The bridge's copy ─────────────────────────────────────────────────────
# One sentence per outcome, in the same place as every other outcome's sentence
# and reached through the same ``admin_service.message_for`` table — so the
# assistant and the typed commands cannot describe the same state two ways.
#
# Each one says what is true rather than what failed, because the four outcomes
# have four different next steps: switch the bridge on, name a repository that
# exists, wait for the repository to be free, or go and confirm the task.
AGENT_DISABLED_TEXT = os.getenv(
    "AGENT_DISABLED_TEXT",
    "⛔️ پل عامل برنامه‌نویسی خاموشه، پس درخواستی ثبت نشد.",
)
AGENT_REJECTED_TEXT = os.getenv(
    "AGENT_REJECTED_TEXT",
    "⛔️ این درخواست پذیرفته نشد: مخزن یا نوع کار شناخته نشد، یا متن کار خالی بود.",
)
AGENT_BUSY_TEXT = os.getenv(
    "AGENT_BUSY_TEXT",
    "⌛️ الان به اندازهٔ کافی کار در جریانه، یا روی این مخزن یکی در حال اجراست. "
    "بعد از تمام شدنش دوباره بگو.",
)
AGENT_DUPLICATE_TEXT = os.getenv(
    "AGENT_DUPLICATE_TEXT",
    "♻️ همین درخواست قبلاً ثبت شده و هنوز در جریانه؛ دوباره ساخته نشد.",
)
AGENT_WAITING_TEXT = os.getenv(
    "AGENT_WAITING_TEXT",
    "🔐 این کار خطرناکه، پس شروع نشد. برای اجرا باید خودت صریح تأییدش کنی.",
)
# The question the bridge asks when a bare «اوکی» arrives with more than one
# dangerous task waiting. It lists the ids, because the answer it wants is one
# of them.
AGENT_CONFIRM_AMBIGUOUS_TEXT = os.getenv(
    "AGENT_CONFIRM_AMBIGUOUS_TEXT",
    "چند کار منتظر تأیید هستن؛ کدوم رو تأیید می‌کنی؟",
)
AGENT_CONFIRM_NOTHING_TEXT = os.getenv(
    "AGENT_CONFIRM_NOTHING_TEXT",
    "الان هیچ کار خطرناکی منتظر تأیید نیست.",
)
AGENT_CONFIRM_NOT_WAITING_TEXT = os.getenv(
    "AGENT_CONFIRM_NOT_WAITING_TEXT",
    "اون کار در انتظار تأیید نیست.",
)
AGENT_CONFIRM_OWNER_ONLY_TEXT = os.getenv(
    "AGENT_CONFIRM_OWNER_ONLY_TEXT",
    "⛔️ تأیید کارهای خطرناک فقط کار مالکه.",
)
AGENT_CONFIRMED_TEXT = os.getenv(
    "AGENT_CONFIRMED_TEXT",
    "✅ تأیید شد؛ کار در صف اجرا قرار گرفت.",
)
AGENT_RESUMED_TEXT = os.getenv(
    "AGENT_RESUMED_TEXT",
    "✅ پاسخ ثبت شد؛ کار از همان‌جا ادامه پیدا می‌کند.",
)
AGENT_CANCELLED_TEXT = os.getenv(
    "AGENT_CANCELLED_TEXT",
    "🛑 کار لغو شد.",
)
AGENT_NOT_YOURS_TEXT = os.getenv(
    "AGENT_NOT_YOURS_TEXT",
    "⛔️ این کار مال تو نیست.",
)
# The header on a progress message. It carries the id and nothing else, so a
# progress line is never mistaken for the final answer.
AGENT_PROGRESS_HEADER = os.getenv(
    "AGENT_PROGRESS_HEADER",
    "🤖 {request_id} — {repository} ({status})",
)
AGENT_RESULT_HEADER = os.getenv(
    "AGENT_RESULT_HEADER",
    "✅ {request_id} — {repository}: انجام شد",
)
AGENT_FAILED_HEADER = os.getenv(
    "AGENT_FAILED_HEADER",
    "❌ {request_id} — {repository}: ناموفق",
)
AGENT_TIMEOUT_HEADER = os.getenv(
    "AGENT_TIMEOUT_HEADER",
    "⌛️ {request_id} — {repository}: از زمان خارج شد",
)
AGENT_QUESTION_HEADER = os.getenv(
    "AGENT_QUESTION_HEADER",
    "❓ {request_id} — {repository} می‌پرسه:",
)
# Shown in place of a result that was too long for a document to be sent.
AGENT_DOCUMENT_NAME = os.getenv("AGENT_DOCUMENT_NAME", "{request_id}.txt")

# Where a review verdict is reported. Empty means "only the log". This is
# deliberately the same private chat the moderation reports already use, so an
# operator has one place to look.
MODERATION_REVIEW_NOTIFY = _bool("MODERATION_REVIEW_NOTIFY", True)


# ---------------- Inbound text filters (links, words, phishing) --------------
# The rules that do not need a model: a banned word, a link, the shapes a scam
# message takes. Deterministic, cheap, and applied before anything is sent to
# Gemini — a rule that can be a regex should not be a request against a shared
# quota.
#
# OFF by default, and that is the whole safety argument. These rules delete
# somebody's message, and a false positive cannot be undone. The capability is
# implemented and tested; turning it on is a decision to make after reading the
# review log, not a default this file imposes.
FILTER_ENABLED = _bool("FILTER_ENABLED", False)

# What to do about each family. "off" disables that family even when the filter
# as a whole is on, which is what makes it possible to run the phishing rules
# without running the link rules.
#   off | review | delete
FILTER_LINK_ACTION = os.getenv("FILTER_LINK_ACTION", "review").strip().lower()
FILTER_WORD_ACTION = os.getenv("FILTER_WORD_ACTION", "delete").strip().lower()
FILTER_PHISHING_ACTION = os.getenv("FILTER_PHISHING_ACTION", "delete").strip().lower()

# The banned words, comma-separated. Matched on word boundaries, so a short word
# cannot fire from inside a longer one — "ass" must not match "class". Persian
# and English entries are both fine; matching is case-folded for Latin text.
FILTER_BANNED_WORDS = _str_list(os.getenv("FILTER_BANNED_WORDS", ""))

# Domains that are always allowed, comma-separated, matched on the host and its
# subdomains. This is what keeps a legitimate link from a banned-word rule from
# firing, and it is the escape hatch an operator needs on day one.
FILTER_ALLOWED_DOMAINS = _str_list(os.getenv("FILTER_ALLOWED_DOMAINS", ""))

# Whether administrators are exempt. On by default: an administrator posting a
# link is usually doing it on purpose, and a filter that mutes the moderation
# team is a filter that gets switched off.
FILTER_EXEMPT_ADMINS = _bool("FILTER_EXEMPT_ADMINS", True)

# Messages shorter than this are not filtered at all, for the same reason the
# text moderation layer has a floor: a two-character line carries no signal.
FILTER_MIN_CHARS = _int("FILTER_MIN_CHARS", 4)

# Whether a filter hit also counts as a violation (a strike, and eventually the
# timed restriction). Off by default: a deleted link and a deleted explicit
# image are not the same offence, and conflating them would mute somebody for
# posting a URL once.
FILTER_COUNTS_AS_VIOLATION = _bool("FILTER_COUNTS_AS_VIOLATION", False)


# ---------------- Speech to text (the fourth workload) -----------------------
# Its own workload with its own key, model, limits and breaker, for the same
# isolation reasons as the other three.
#
# It is separate from the conversational assistant on purpose even though the
# assistant *uses* it: transcription is a mechanical, cheap, cacheable operation
# with one right answer, while a reply is a generation. Sharing a budget between
# them would mean a busy voice chat could silence the assistant, and it would
# make "the transcript was wrong" indistinguishable from "the reply was wrong".
#
# This is also what keeps ordinary group voice messages out of acquisition and
# moderation: nothing transcribes a voice note unless something explicitly asks
# for it. There is no handler that does so automatically.
TRANSCRIBE_ENABLED = _bool("TRANSCRIBE_ENABLED", False)
TRANSCRIBE_API_KEY = os.getenv("TRANSCRIBE_API_KEY", "").strip()
TRANSCRIBE_ALLOW_SHARED_KEY = _bool("TRANSCRIBE_ALLOW_SHARED_KEY", False)
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gemini-flash-lite-latest").strip()
TRANSCRIBE_TIMEOUT_SECONDS = _float("TRANSCRIBE_TIMEOUT_SECONDS", 25.0)
TRANSCRIBE_MAX_RETRIES = _int("TRANSCRIBE_MAX_RETRIES", 1)
TRANSCRIBE_BACKOFF_SECONDS = _float("TRANSCRIBE_BACKOFF_SECONDS", 1.5)
TRANSCRIBE_RATE_LIMIT = _int("TRANSCRIBE_RATE_LIMIT", 10)
TRANSCRIBE_RATE_WINDOW = _float("TRANSCRIBE_RATE_WINDOW", 60.0)
TRANSCRIBE_DAILY_LIMIT = _int("TRANSCRIBE_DAILY_LIMIT", 300)
TRANSCRIBE_CIRCUIT_FAILURES = _int("TRANSCRIBE_CIRCUIT_FAILURES", 5)
TRANSCRIBE_CIRCUIT_SECONDS = _float("TRANSCRIBE_CIRCUIT_SECONDS", 300.0)
# Longest voice note we will transcribe. Telegram's own voice notes are capped
# at an hour, which is far past anything a chat reply should wait for; beyond
# this the pipeline refuses rather than sending a truncated clip.
TRANSCRIBE_MAX_SECONDS = _float("TRANSCRIBE_MAX_SECONDS", 300.0)
TRANSCRIBE_MAX_MB = _float("TRANSCRIBE_MAX_MB", 18.0)

# The transcription-only interface: a command that returns the transcript and
# nothing else, so the pipeline can be used and tested on its own without the
# conversational assistant being involved. Empty disables it.
TRANSCRIBE_COMMAND = os.getenv("TRANSCRIBE_COMMAND", "transcribe").strip()

TRANSCRIBE_UNAVAILABLE_TEXT = os.getenv(
    "TRANSCRIBE_UNAVAILABLE_TEXT",
    "الان نمی‌تونم صدا رو تبدیل کنم. بعداً دوباره امتحان کن.",
)
TRANSCRIBE_EMPTY_TEXT = os.getenv(
    "TRANSCRIBE_EMPTY_TEXT",
    "چیزی توی صدا نفهمیدم. واضح‌تر بفرست.",
)


# ---------------- Voice replies from the assistant ---------------------------
# When somebody talks to the assistant with a voice message, answering in voice
# is the natural thing to do — and it is technically possible: the TTS models
# below were measured working on this key on 2026-09-21, returning raw PCM at
# 24 kHz mono, which ffmpeg turns into the OGG/Opus that Telegram's sendVoice
# wants.
#
# Off by default, and deliberately so. A voice reply costs a second model call
# plus an ffmpeg pass, it cannot be read silently in a meeting, and a synthetic
# voice is a much stronger claim to personhood than text is. The brief asks for
# a natural conversation rather than a convincing impersonation, so this is an
# opt-in the operator makes rather than a default the bot imposes.
GEMINI_CHAT_VOICE_REPLY = _bool("GEMINI_CHAT_VOICE_REPLY", False)

# Measured available on this key 2026-09-21:
#   gemini-3.1-flash-tts-preview      -> 159 KB PCM for one short sentence
#   gemini-2.5-flash-preview-tts      -> 143 KB PCM
# Both are `preview`, which is why this is a separate setting: the preview
# surface moves, and when it does only voice replies should be affected.
GEMINI_CHAT_TTS_MODEL = os.getenv(
    "GEMINI_CHAT_TTS_MODEL", "gemini-3.1-flash-tts-preview"
).strip()
# A prebuilt voice name from the API's own list. Kore is a neutral default.
GEMINI_CHAT_TTS_VOICE = os.getenv("GEMINI_CHAT_TTS_VOICE", "Kore").strip()
# Above this, the reply is sent as text instead. Synthesising a wall of text is
# slow, expensive and unpleasant to listen to; the text is right there anyway.
GEMINI_CHAT_VOICE_MAX_CHARS = _int("GEMINI_CHAT_VOICE_MAX_CHARS", 400)

# The conversational media bound: how many media parts one conversational turn
# may carry, so a message with a dozen attachments cannot become a dozen calls.
GEMINI_CHAT_MEDIA_MAX_PARTS = _int("GEMINI_CHAT_MEDIA_MAX_PARTS", 3)

# Said when somebody addresses the assistant with an attachment that could not
# be read — too large, an unsupported format, a download that failed. The
# assistant is told not to guess what it was, and this is what the person sees
# when even that is not possible.
GEMINI_CHAT_UNREADABLE_TEXT = os.getenv(
    "GEMINI_CHAT_UNREADABLE_TEXT",
    "این فایل رو نتونستم باز کنم 🙏 یه بار دیگه بفرست یا توضیح بده چیه.",
)

# Said by the transcription-only command when the message carries no audio.
TRANSCRIBE_NEED_AUDIO_TEXT = os.getenv(
    "TRANSCRIBE_NEED_AUDIO_TEXT",
    "روی یک پیام صوتی ریپلای کن یا خودش رو با این دستور بفرست.",
)


# ---------------- Admin command copy -----------------------------------------
# Every word the administrative interface says. Kept here with the rest of the
# product's wording so no Persian literal lives in a handler.
ADMIN_DENIED_TEXT = os.getenv(
    "ADMIN_DENIED_TEXT",
    "⛔️ این کار رو نمی‌تونی انجام بدی.",
)
# The two outcomes that only exist because administrative requests are typed and
# replay-protected. Both are deliberately *not* the generic denial: "it was
# already done" and "it was too old" are different facts, and telling an operator
# they were refused when the action actually succeeded once is how somebody
# performs it a second time by hand.
ADMIN_DONE_TEXT = os.getenv("ADMIN_DONE_TEXT", "✅ انجام شد.")
ADMIN_DUPLICATE_TEXT = os.getenv(
    "ADMIN_DUPLICATE_TEXT",
    "♻️ این درخواست قبلاً انجام شده بود؛ دوباره اجرا نشد.",
)
ADMIN_STALE_TEXT = os.getenv(
    "ADMIN_STALE_TEXT",
    "⌛️ این درخواست قدیمی بود و اجرا نشد. لطفاً دوباره بگو.",
)
ADMIN_NOT_CONFIGURED_TEXT = os.getenv(
    "ADMIN_NOT_CONFIGURED_TEXT",
    "⛔️ هیچ مالکی برای این ربات تنظیم نشده، پس هیچ دستور مدیریتی اجرا نمی‌شه.",
)
ADMIN_OWNER_PROTECTED_TEXT = os.getenv(
    "ADMIN_OWNER_PROTECTED_TEXT",
    "⛔️ این کاربر مالک اصلیه و قابل تغییر نیست.",
)
ADMIN_HIGHER_RANK_TEXT = os.getenv(
    "ADMIN_HIGHER_RANK_TEXT",
    "⛔️ این کاربر سطح بالاتری از تو داره؛ نمی‌تونی تغییرش بدی.",
)
# Aimed at a role change on the person issuing it. This is not a hierarchy
# refusal and must not read like one: the level arithmetic is irrelevant here,
# because nobody — not even the owner — rewrites their own role or permissions.
ADMIN_SELF_TARGET_TEXT = os.getenv(
    "ADMIN_SELF_TARGET_TEXT",
    "⛔️ نمی‌تونی نقش یا دسترسی‌های خودت رو تغییر بدی.",
)
ADMIN_TARGET_NOT_FOUND_TEXT = os.getenv(
    "ADMIN_TARGET_NOT_FOUND_TEXT",
    "روی پیام کسی ریپلای کن تا مشخص بشه منظورت کیه.",
)
ADMIN_TARGET_IS_BOT_TEXT = os.getenv(
    "ADMIN_TARGET_IS_BOT_TEXT",
    "⛔️ ربات رو نمی‌شه مدیر کرد.",
)
ADMIN_TELEGRAM_FAILED_TEXT = os.getenv(
    "ADMIN_TELEGRAM_FAILED_TEXT",
    "❌ تلگرام این کار رو قبول نکرد؛ چیزی تغییر نکرد.",
)
ADMIN_BOT_LACKS_RIGHT_TEXT = os.getenv(
    "ADMIN_BOT_LACKS_RIGHT_TEXT",
    "❌ خودم دسترسی لازم رو توی این گروه ندارم، پس نمی‌تونم این کار رو بکنم.",
)
ADMIN_PROMOTE_TITLE = os.getenv(
    "ADMIN_PROMOTE_TITLE",
    "انتخاب دسترسی‌ها برای {name}\nهر مورد رو بزن تا روشن/خاموش بشه، بعد تأیید کن.",
)
ADMIN_PROMOTE_CONFIRM_BUTTON = os.getenv("ADMIN_PROMOTE_CONFIRM_BUTTON", "✅ تأیید")
ADMIN_PROMOTE_CANCEL_BUTTON = os.getenv("ADMIN_PROMOTE_CANCEL_BUTTON", "✖️ لغو")
ADMIN_PROMOTE_DONE_TEXT = os.getenv(
    "ADMIN_PROMOTE_DONE_TEXT",
    "✅ {name} با دسترسی‌های زیر ثبت شد:\n{perms}",
)
ADMIN_PROMOTE_NO_TELEGRAM_TEXT = os.getenv(
    "ADMIN_PROMOTE_NO_TELEGRAM_TEXT",
    "ℹ️ توی تلگرام چیزی تغییر نکرد (فقط دسترسی داخلی ربات ثبت شد).",
)
ADMIN_PROMOTE_TELEGRAM_TEXT = os.getenv(
    "ADMIN_PROMOTE_TELEGRAM_TEXT",
    "ℹ️ دسترسی‌های تلگرام هم اعمال شد.",
)
ADMIN_DEMOTE_DONE_TEXT = os.getenv(
    "ADMIN_DEMOTE_DONE_TEXT",
    "✅ دسترسی مدیریتی {name} برداشته شد.",
)
ADMIN_DEMOTE_NOTHING_TEXT = os.getenv(
    "ADMIN_DEMOTE_NOTHING_TEXT",
    "این کاربر از قبل مدیر نبود.",
)
ADMIN_LIST_TITLE = os.getenv("ADMIN_LIST_TITLE", "مدیرهای این ربات:")
ADMIN_LIST_EMPTY = os.getenv("ADMIN_LIST_EMPTY", "هیچ مدیری ثبت نشده.")
ADMIN_LIST_LINE = os.getenv("ADMIN_LIST_LINE", "• {name} — {role}")
ADMIN_WHOAMI_TEXT = os.getenv(
    "ADMIN_WHOAMI_TEXT",
    "شناسه: {user_id}\nسطح: {role}\nدسترسی‌ها: {perms}",
)
ADMIN_CANCELLED_TEXT = os.getenv("ADMIN_CANCELLED_TEXT", "لغو شد.")
ADMIN_STALE_BUTTON_TEXT = os.getenv(
    "ADMIN_STALE_BUTTON_TEXT",
    "این دکمه دیگه معتبر نیست. دوباره دستور رو بزن.",
)

# Nexus state copy.
#
# The two confirmations are separate sentences rather than one sentence with the
# state interpolated, because "Nexus is off" and "Nexus is on" are the two facts
# an owner most needs to read unambiguously — and a templated sentence that got
# the state wrong would be read as the opposite of what happened.
NEXUS_OFFLINE_DONE_TEXT = os.getenv(
    "NEXUS_OFFLINE_DONE_TEXT",
    "🌙 نکسوس خاموش شد. دیگه جواب نمی‌دم تا خودت روشنم کنی.",
)
NEXUS_ONLINE_DONE_TEXT = os.getenv(
    "NEXUS_ONLINE_DONE_TEXT",
    "☀️ نکسوس روشن شد. در خدمتم.",
)
NEXUS_ALREADY_TEXT = os.getenv(
    "NEXUS_ALREADY_TEXT",
    "نکسوس از قبل {state} بود.",
)
NEXUS_OFFLINE_DENIED_TEXT = os.getenv(
    "NEXUS_OFFLINE_DENIED_TEXT",
    "نکسوس الان خاموشه، پس کاری انجام نمی‌دم.",
)
NEXUS_OWNER_ONLY_TEXT = os.getenv(
    "NEXUS_OWNER_ONLY_TEXT",
    "فقط مالک ربات می‌تونه نکسوس رو روشن یا خاموش کنه.",
)
NEXUS_STATUS_TITLE = os.getenv("NEXUS_STATUS_TITLE", "وضعیت نکسوس:")
NEXUS_STATUS_TEXT = os.getenv(
    "NEXUS_STATUS_TEXT",
    "وضعیت: {state}\n"
    "آخرین تغییر: {changed}\n"
    "توسط: {changed_by}\n"
    "پایش پیام‌های مدیرها: {observe}\n"
    "پاسخ‌دهی به: {actors_only}\n"
    "درک گفتگوی گروه: {awareness}\n"
    "{mode}",
)
NEXUS_STATE_ONLINE_LABEL = os.getenv("NEXUS_STATE_ONLINE_LABEL", "روشن (ONLINE)")
NEXUS_STATE_OFFLINE_LABEL = os.getenv("NEXUS_STATE_OFFLINE_LABEL", "خاموش (OFFLINE)")
NEXUS_OBSERVE_ON_LABEL = os.getenv("NEXUS_OBSERVE_ON_LABEL", "فعال")
NEXUS_OBSERVE_OFF_LABEL = os.getenv("NEXUS_OBSERVE_OFF_LABEL", "غیرفعال")
# Reported in `/nexus status` so the owner can see, from inside the group, which
# of the two "who gets answered" switches is in force. Without this line the
# actor gate is invisible: a member who addresses Nexus and gets no reply cannot
# tell whether the gate refused them or something further in failed, and the
# owner asked to be able to verify exactly that. The two labels read as answers
# to "پاسخ‌دهی به" ("answers to") and are deliberately distinct from the observe
# labels above, so a rendered status says which switch is which.
NEXUS_ACTORS_ONLY_ON_LABEL = os.getenv("NEXUS_ACTORS_ONLY_ON_LABEL", "فقط مدیرها")
NEXUS_ACTORS_ONLY_OFF_LABEL = os.getenv("NEXUS_ACTORS_ONLY_OFF_LABEL", "همه")
# The awareness line, reported for the same reason the actor gate is: "Nexus did
# not react" and "Nexus is not reading the room at all" look identical from
# inside a group, and only one of them is a bug.
NEXUS_AWARENESS_ON_LABEL = os.getenv("NEXUS_AWARENESS_ON_LABEL", "فعال")
NEXUS_AWARENESS_OFF_LABEL = os.getenv("NEXUS_AWARENESS_OFF_LABEL", "غیرفعال")
NEXUS_NEVER_CHANGED_TEXT = os.getenv("NEXUS_NEVER_CHANGED_TEXT", "—")
NEXUS_STATUS_HINT = os.getenv(
    "NEXUS_STATUS_HINT",
    "دستورها: /nexus on | /nexus off | /nexus status",
)
NEXUS_VISIBILITY_WARNING = os.getenv(
    "NEXUS_VISIBILITY_WARNING",
    "⚠️ ربات در گروه {chat_id} ادمین نیست، پس پیام‌های عادی مدیرها را نمی‌بیند و "
    "نمی‌تواند آن‌ها را برای زمینه‌ی گفتگو ذخیره کند. فقط دستورها، ریپلای‌ها و "
    "منشن‌ها به آن می‌رسد.",
)

# Moderation command copy.
MOD_BAN_DONE_TEXT = os.getenv("MOD_BAN_DONE_TEXT", "🚫 {name} از گروه بن شد.")
MOD_UNBAN_DONE_TEXT = os.getenv("MOD_UNBAN_DONE_TEXT", "✅ {name} آن‌بن شد.")
MOD_MUTE_DONE_TEXT = os.getenv(
    "MOD_MUTE_DONE_TEXT", "🔇 ارسال پیام {name} برای {minutes} دقیقه محدود شد."
)
MOD_UNMUTE_DONE_TEXT = os.getenv("MOD_UNMUTE_DONE_TEXT", "🔊 محدودیت {name} برداشته شد.")
MOD_DELETE_DONE_TEXT = os.getenv("MOD_DELETE_DONE_TEXT", "🗑 پیام حذف شد.")
MOD_DELETE_FAILED_TEXT = os.getenv(
    "MOD_DELETE_FAILED_TEXT", "❌ نتونستم پیام رو حذف کنم."
)
MOD_WARN_DONE_TEXT = os.getenv("MOD_WARN_DONE_TEXT", "⚠️ اخطار به {name} داده شد.")
MOD_TARGET_REQUIRED_TEXT = os.getenv(
    "MOD_TARGET_REQUIRED_TEXT",
    "روی پیام کاربر ریپلای کن یا شناسه‌اش رو بنویس.",
)
MOD_WARN_USER_TEXT = os.getenv(
    "MOD_WARN_USER_TEXT",
    "{name} جان، این اخطاره: {reason}",
)
MOD_COMMAND_FAILED_TEXT = os.getenv(
    "MOD_COMMAND_FAILED_TEXT", "❌ تلگرام این کار رو انجام نداد."
)






# ---------------- The Gemini account pool ------------------------------------
# Every workload above is backed by a *pool* rather than a single credential.
# See ``app/gemini_pool.py`` for the two levels of failover; what lives here is
# only the configuration.
#
# The rule that shapes all of it: **every API key is a separate Google account
# and a separate project**, with its own quota. Two keys are not one bigger
# allowance, and nothing here may treat them as interchangeable. That is why
# ``app/gemini_pool.py`` keeps per-account state and why a key that appears in
# two slots is collapsed to one account rather than counted twice.

# Ask the provider which models each credential can actually see. Verified
# against the live API: ``models.list`` returns names, token limits and
# supported generation methods, and *nothing else* — in particular it does not
# report input or output modalities. So discovery answers "does this model exist
# for this key", and capability stays a curated table in the pool module.
GEMINI_POOL_DISCOVERY_ENABLED = _bool("GEMINI_MODEL_DISCOVERY_ENABLED", True)
# How long a discovery result is trusted before it is asked for again.
GEMINI_POOL_DISCOVERY_TTL = _int("GEMINI_MODEL_DISCOVERY_TTL", 21600)

# Cooldowns. These are the three different reasons a resource goes quiet, and
# they are deliberately different lengths: a model rate limit clears in seconds
# to a minute, a project quota in minutes to hours, and a transient provider
# wobble almost immediately.
GEMINI_POOL_MODEL_COOLDOWN = _int("GEMINI_POOL_MODEL_COOLDOWN", 120)
GEMINI_POOL_QUOTA_COOLDOWN = _int("GEMINI_POOL_QUOTA_COOLDOWN", 900)
GEMINI_POOL_TRANSIENT_COOLDOWN = _int("GEMINI_POOL_TRANSIENT_COOLDOWN", 15)

# Pool events are deduplicated per (workload, event, account, model) against
# this window, so a hundred consecutive 429s produce one row rather than a
# hundred.
#
# This was ``GEMINI_POOL_NOTIFY_COOLDOWN`` and it gated a Telegram message. The
# message is gone; the deduplication is not, because it is what keeps the events
# table a record of transitions instead of a copy of the counters. The variable
# was renamed rather than quietly reinterpreted, so a deployment that still sets
# the old name is not left believing it configured something.
GEMINI_POOL_EVENT_COOLDOWN = _int("GEMINI_POOL_EVENT_COOLDOWN", 900)

# How long pool events are kept, and how often the sweep runs.
#
# `gemini_events` is the only table in the schema that grows with activity
# rather than with the number of accounts, days or people, so it is the only one
# that needs a bound. Ninety days matches the administrative activity window and
# is chosen to be long enough to still answer "why was this rate-limited last
# month" — that question was asked for real during the incident that produced
# the awareness pacing change, and a shorter window would have discarded the
# evidence.
#
# The sweep runs every N provider requests rather than on a timer, following the
# same rule as every other retention rule here: this process has no scheduler,
# and a rule that only runs when somebody remembers is not a rule. Zero disables
# the sweep, which is the operator's choice to make rather than a silent default.
GEMINI_EVENTS_RETENTION_SECONDS = _int(
    "GEMINI_EVENTS_RETENTION_SECONDS", 90 * 24 * 3600
)
# The per-day spend table, in days rather than seconds because its rows *are*
# days — a sub-day window would mean deleting the row that is currently being
# incremented. Kept as long as the event window so the two tell the same story:
# "we were rate-limited on the 14th" and "the 14th cost 780 requests" are read
# together or not at all.
GEMINI_DAILY_RETENTION_DAYS = _int("GEMINI_DAILY_RETENTION_DAYS", 90)

# A hard ceiling on provider calls for one logical request. Without it a large
# pool with retries could spend a minute of wall clock on a single message.
GEMINI_POOL_MAX_ATTEMPTS = _int("GEMINI_POOL_MAX_ATTEMPTS", 12)

# The model preference order. The primary is tried first and the rest only when
# it is unavailable, which is why normal operation is unchanged by the pool.
#
# Every name here is validated before use: discovery must list it, and its
# family must be capable of the workload. A name that fails either check is
# skipped silently rather than being sent and rejected.
#
# Order is cheapest-and-fastest first. Nothing in this list is invented: every
# name was both returned by ``models.list`` AND answered a real generateContent
# call on 2026-09-21.
#
# The 2.5 family was removed after a live probe, and the reason is worth
# keeping: ``models.list`` still lists ``gemini-2.5-flash``,
# ``gemini-2.5-flash-lite`` and ``gemini-2.5-pro``, but calling any of them
# answers
#
#   404 NOT_FOUND  This model ... is no longer available to new users.
#
# So being listed is not the same as being usable, and a preference list built
# from the listing alone spends one wasted call per account on each retired
# name before the pool disables it. Discovery cannot catch this; the 404 can,
# and does — this list is simply the cheaper way to learn it.
DEFAULT_FALLBACK_MODELS = (
    "gemini-flash-lite-latest,gemini-flash-latest,gemini-3.5-flash-lite,"
    "gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3.6-flash,"
    "gemini-3.7-flash,gemini-pro-latest"
)
# Transcription has its own order. ``gemini-3.5-transcribe`` is purpose-built
# and appears last rather than first: it is unmeasured on this deployment, and
# a working default should not be replaced by an assumption.
DEFAULT_TRANSCRIBE_FALLBACKS = (
    "gemini-flash-lite-latest,gemini-flash-latest,gemini-3.5-flash-lite,"
    "gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3.5-transcribe"
)
# Speech synthesis exists only as preview models, so this is the one workload
# that opts into them. It is stated here rather than assumed in the pool.
DEFAULT_TTS_FALLBACKS = (
    "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts,"
    "gemini-2.5-pro-preview-tts"
)

GEMINI_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
)
GEMINI_CHAT_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_CHAT_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
)
GEMINI_MOD_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_MOD_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
)
TRANSCRIBE_FALLBACK_MODELS = _str_list(
    os.getenv("TRANSCRIBE_FALLBACK_MODELS", DEFAULT_TRANSCRIBE_FALLBACKS)
)
GEMINI_CHAT_TTS_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_CHAT_TTS_FALLBACK_MODELS", DEFAULT_TTS_FALLBACKS)
)


def _pool_key_list(primary: str, prefix: str, shared: list, allow_shared: bool):
    """The ordered ``(slot, credential)`` pairs for one workload.

    Slot 1 is the workload's own primary key. ``<PREFIX>_2`` … ``<PREFIX>_20``
    are extra credentials for *this* workload only, which is how an operator
    gives one workload a deeper pool without loosening the isolation of the
    others. The shared pool is added only when the workload opts in with its
    existing ``*_ALLOW_SHARED_KEY`` flag.

    Duplicates are dropped here as well as in the pool, so the configuration is
    honest about how many accounts it really describes.
    """
    out: list = []
    seen: set = set()

    def add(slot: str, value: str) -> None:
        key = (value or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        out.append((slot, key))

    add("1", primary)
    for index in range(2, 21):
        add(str(index), os.getenv(f"{prefix}_{index}", ""))
    if allow_shared:
        for index, key in enumerate(shared, start=1):
            add(f"shared{index}", key)
    return out


def _shared_pool_keys() -> list:
    """Credentials any workload may draw on when it opts in.

    Two spellings, both supported because the brief used both: numbered slots
    (``GEMINI_KEY_1`` … ``GEMINI_KEY_20``) and one comma-separated list
    (``GEMINI_POOL_KEYS``). Order is preserved, so the numbered slots are tried
    first.
    """
    keys: list = []
    for index in range(1, 21):
        value = os.getenv(f"GEMINI_KEY_{index}", "").strip()
        if value:
            keys.append(value)
    keys.extend(_str_list(os.getenv("GEMINI_POOL_KEYS", "")))
    return keys


SHARED_POOL_KEYS = _shared_pool_keys()


def _models(primary: str, fallbacks: list) -> list:
    """The preference order: the primary first, then the fallbacks, deduped."""
    order: list = []
    for name in [primary, *fallbacks]:
        clean = (name or "").strip()
        if clean and clean not in order:
            order.append(clean)
    return order


# The API's own floor on a manually-set deadline. Verified the expensive way:
# with a 6-second deadline every call answers
#
#   400 INVALID_ARGUMENT  Manually set deadline 6s is too short.
#                        Minimum allowed deadline is 10s.
#
# so the layer looks active and answers nothing. Each workload clamps its own
# timeout; the pool has to clamp too, or an operator lowering one of those
# variables would reintroduce the same silent, total failure through the pool.
MIN_GEMINI_DEADLINE_SECONDS = 10.0


def _deadline(seconds: float) -> float:
    return max(MIN_GEMINI_DEADLINE_SECONDS, float(seconds))


# The pool definitions, one per AI workload. ``capabilities`` is what the
# workload needs a model to be able to do; the pool refuses to offer a model
# that does not satisfy it in full, which is what stops a text-only model being
# handed an image or an audio model being asked for text.
#
# ── The awareness workload's own transport settings ──
#
# A **sixth** workload, and a separate one on purpose. Awareness reads the whole
# room rather than one person's conversation: it is the highest-volume workload
# in a busy group, and the one most likely to be rate-limited. Folding it into
# ``chat`` would mean a chatty room silently spending the allowance somebody is
# waiting on an answer to — so it gets its own key slot, its own model
# preference, its own timeout, and its own breaker inside the pool.
#
# Its default model is the same family as the conversation's because the job is
# the same shape (read text, reason, write text) and the cost profile is the
# one an operator already knows. It is a *separate setting* so it can be moved
# without touching the assistant.
#
# The credential is its own, and there is no fallback. This is the operator's
# decision, taken deliberately, and it reverses what this comment used to say.
#
# What the previous arrangement got right: the isolation that matters
# structurally *was* already structural — histories, rate windows, circuit
# breakers, failure state and daily allowances are all keyed by **workload**, so
# awareness never spent the conversation's allowance even when the two shared a
# key. What it got wrong was everything the provider does above us. Sharing a
# credential means sharing a Google project, and therefore sharing one
# provider-side rate limit that no per-workload counter can partition. Measured
# on the deployment that produced the earlier comment, awareness and chat shared
# this key and the collision cost 26% of conversational turns to rate limits,
# plus an awareness pool that exhausted its allowance and then failed every pass
# until the reset.
#
# So the fallback is gone and ``GEMINI_AWARENESS_ALLOW_SHARED_KEY`` now defaults
# to False. The consequence is deliberate and must be stated plainly: with no
# ``GEMINI_AWARENESS_API_KEY`` set, awareness has **no** credential and does no
# work at all. That is the fail-closed direction — a workload that cannot run on
# its own allowance does not run on somebody else's — and it is reported at boot
# rather than discovered later. Setting the key is the operator action; there is
# no way to satisfy this from inside the application, and inventing one would
# mean going back to sharing.
GEMINI_AWARENESS_API_KEY = os.getenv("GEMINI_AWARENESS_API_KEY", "").strip()
GEMINI_AWARENESS_ALLOW_SHARED_KEY = _bool("GEMINI_AWARENESS_ALLOW_SHARED_KEY", False)
GEMINI_AWARENESS_MODEL = os.getenv(
    "GEMINI_AWARENESS_MODEL", GEMINI_CHAT_MODEL
).strip()
GEMINI_AWARENESS_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_AWARENESS_FALLBACK_MODELS", "")
) or GEMINI_CHAT_FALLBACK_MODELS
# Shorter than the conversation's, because nobody is waiting on an awareness
# pass and a pass that runs long delays the next one.
GEMINI_AWARENESS_TIMEOUT_SECONDS = _float("GEMINI_AWARENESS_TIMEOUT_SECONDS", 20.0)
GEMINI_AWARENESS_MAX_RETRIES = _int("GEMINI_AWARENESS_MAX_RETRIES", 1)
GEMINI_AWARENESS_BACKOFF_SECONDS = _float(
    "GEMINI_AWARENESS_BACKOFF_SECONDS", 1.5
)
# The breaker is the pool's, per workload, so an awareness outage opens only the
# awareness circuit. The conversation keeps answering.
GEMINI_AWARENESS_CIRCUIT_FAILURES = _int("GEMINI_AWARENESS_CIRCUIT_FAILURES", 5)
GEMINI_AWARENESS_CIRCUIT_SECONDS = _float(
    "GEMINI_AWARENESS_CIRCUIT_SECONDS", 300.0
)

GEMINI_POOLS = [
    {
        "workload": "intent",
        "keys": _pool_key_list(
            GEMINI_API_KEY, "GEMINI_API_KEY", SHARED_POOL_KEYS, True
        ),
        "models": _models(GEMINI_MODEL, GEMINI_FALLBACK_MODELS),
        "capabilities": frozenset({"text"}),
        "allow_experimental": False,
        "retries": GEMINI_MAX_RETRIES,
        "backoff": GEMINI_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_TIMEOUT_SECONDS),
    },
    {
        "workload": "chat",
        "keys": _pool_key_list(
            GEMINI_CHAT_API_KEY,
            "GEMINI_CHAT_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_CHAT_ALLOW_SHARED_KEY,
        ),
        "models": _models(GEMINI_CHAT_MODEL, GEMINI_CHAT_FALLBACK_MODELS),
        "capabilities": frozenset({"text"}),
        "allow_experimental": False,
        "retries": GEMINI_CHAT_MAX_RETRIES,
        "backoff": GEMINI_CHAT_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_CHAT_TIMEOUT_SECONDS),
        # The only workload with a per-account daily allowance. It is the same
        # number the workload's own daily cap always used, but it now belongs to
        # each account rather than to the deployment, so the total a deployment
        # can serve is the allowance times the number of chat accounts — and the
        # pool fails over from a spent account to a fresh one instead of
        # stopping. The floor of 1 preserves the old meaning of 0, which used to
        # mean "one request, then stop".
        "daily_budget": max(1, GEMINI_CHAT_DAILY_LIMIT),
    },
    {
        "workload": "moderation",
        "keys": _pool_key_list(
            GEMINI_MOD_API_KEY,
            "GEMINI_MOD_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_MOD_ALLOW_SHARED_KEY,
        ),
        "models": _models(GEMINI_MOD_MODEL, GEMINI_MOD_FALLBACK_MODELS),
        # Moderation is the media workload: it sends images and extracted video
        # frames, so its models must accept both. This is the requirement that
        # must never be relaxed to keep a request alive.
        "capabilities": frozenset({"text", "image", "video"}),
        "allow_experimental": False,
        "retries": GEMINI_MOD_MAX_RETRIES,
        "backoff": GEMINI_MOD_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_MOD_TIMEOUT_SECONDS),
    },
    {
        "workload": "transcribe",
        "keys": _pool_key_list(
            TRANSCRIBE_API_KEY,
            "TRANSCRIBE_API_KEY",
            SHARED_POOL_KEYS,
            TRANSCRIBE_ALLOW_SHARED_KEY,
        ),
        "models": _models(TRANSCRIBE_MODEL, TRANSCRIBE_FALLBACK_MODELS),
        # Audio in, text out. A text-only model here would answer with a
        # confident invention rather than an error, which is the worst possible
        # failure for a transcript.
        "capabilities": frozenset({"audio_in"}),
        "allow_experimental": False,
        "retries": TRANSCRIBE_MAX_RETRIES,
        "backoff": TRANSCRIBE_BACKOFF_SECONDS,
        "timeout": _deadline(TRANSCRIBE_TIMEOUT_SECONDS),
    },
    {
        "workload": "tts",
        "keys": _pool_key_list(
            GEMINI_CHAT_API_KEY,
            "GEMINI_CHAT_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_CHAT_ALLOW_SHARED_KEY,
        ),
        "models": _models(GEMINI_CHAT_TTS_MODEL, GEMINI_CHAT_TTS_FALLBACK_MODELS),
        "capabilities": frozenset({"audio_out"}),
        "allow_experimental": True,
        "retries": 0,
        "backoff": GEMINI_CHAT_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_CHAT_TIMEOUT_SECONDS),
    },
    {
        "workload": "awareness",
        "keys": _pool_key_list(
            GEMINI_AWARENESS_API_KEY,
            "GEMINI_AWARENESS_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_AWARENESS_ALLOW_SHARED_KEY,
        ),
        "models": _models(
            GEMINI_AWARENESS_MODEL, GEMINI_AWARENESS_FALLBACK_MODELS
        ),
        # Text only, and that is the whole requirement: awareness reads a
        # transcript. A photo in the room is summarised as the fact that a photo
        # was sent, not by sending the bytes to a second model — the moderation
        # workload already looks at the content itself, and duplicating that here
        # would both double the cost and blur which workload is responsible for
        # what.
        "capabilities": frozenset({"text"}),
        "allow_experimental": False,
        "retries": GEMINI_AWARENESS_MAX_RETRIES,
        "backoff": GEMINI_AWARENESS_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_AWARENESS_TIMEOUT_SECONDS),
        # Per account, like chat's, and deliberately its own number: an
        # observant Nexus must not be able to spend the allowance a person is
        # waiting on an answer to.
        "daily_budget": max(1, NEXUS_AWARENESS_DAILY_LIMIT),
    },
]
