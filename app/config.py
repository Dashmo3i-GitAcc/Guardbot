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
# Deliberately high: false positives are worse than missing borderline media.
EXPLICIT_DELETE_THRESHOLD = _float("EXPLICIT_DELETE_THRESHOLD", 0.80)
# >= this (but below delete) -> REVIEW: allowed and logged only, never deleted.
EXPLICIT_REVIEW_THRESHOLD = _float("EXPLICIT_REVIEW_THRESHOLD", 0.40)
# A generic NSFW score may only ever raise REVIEW, never EXPLICIT.
GENERIC_REVIEW_THRESHOLD = _float("GENERIC_REVIEW_THRESHOLD", 0.90)

# Video / GIF / animated sticker: number of frames sampled
VIDEO_FRAMES = _int("VIDEO_FRAMES", 4)

# Bot API download limit is 20 MB. Larger files: only the thumbnail is checked.
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 20)

# How many media items are analyzed in parallel
MEDIA_WORKERS = _int("MEDIA_WORKERS", 2)

DB_PATH = os.getenv("DB_PATH", "/data/guardbot.db")
TMP_DIR = os.getenv("TMP_DIR", "/tmp/guardbot")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
