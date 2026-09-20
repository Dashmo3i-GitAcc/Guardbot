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
