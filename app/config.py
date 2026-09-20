"""All settings come from environment variables (.env)."""
import os


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


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

# ---------------- Media moderation ----------------
MEDIA_ENABLED = _bool("MEDIA_ENABLED", True)

# NSFW score thresholds (0..1)
NSFW_DELETE_THRESHOLD = _float("NSFW_DELETE_THRESHOLD", 0.60)  # delete + report
NSFW_BAN_THRESHOLD = _float("NSFW_BAN_THRESHOLD", 0.90)        # delete + ban

# Action on high confidence: "ban" or "mute"
HIGH_CONF_ACTION = os.getenv("HIGH_CONF_ACTION", "ban")
MUTE_HOURS = _int("MUTE_HOURS", 24)

# Video / GIF / animated sticker: number of frames sampled
VIDEO_FRAMES = _int("VIDEO_FRAMES", 4)

# Bot API download limit is 20 MB. Larger files: only the thumbnail is checked.
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 20)

# How many media items are analyzed in parallel
MEDIA_WORKERS = _int("MEDIA_WORKERS", 2)

# Classifier: "falconsai" (HF model, needs ~350MB) - runs locally on CPU
NSFW_MODEL = os.getenv("NSFW_MODEL", "Falconsai/nsfw_image_detection")

# ---------------- Trust levels ----------------
# New users' media is checked strictly; after this many clean messages
# the user is "trusted" and media checks use a higher delete threshold.
TRUST_AFTER_MESSAGES = _int("TRUST_AFTER_MESSAGES", 30)
TRUSTED_EXTRA_MARGIN = _float("TRUSTED_EXTRA_MARGIN", 0.10)

# Repeated offenders: strikes before automatic ban (for delete-level hits)
MAX_STRIKES = _int("MAX_STRIKES", 2)

DB_PATH = os.getenv("DB_PATH", "/data/guardbot.db")
TMP_DIR = os.getenv("TMP_DIR", "/tmp/guardbot")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
