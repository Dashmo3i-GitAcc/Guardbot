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

# ---------------- Instant media flood (burst) ----------------
# A burst is *more than* BURST_MAX_ITEMS qualifying media messages from the
# same user inside BURST_WINDOW_SECONDS. The window is deliberately very
# short: this rule stops an instant flood, it is not a "sent a lot of media
# today" rule, and it must not flag normal sharing over 20-30 seconds.
#
# Only BURST_MEDIA_KINDS are counted. Ordinary photos and videos are never in
# this set, so sending several of them quickly is not a flood — this rule is
# about a burst of one light-weight kind, not about the volume of content.
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
# One confirmed deletion counts as one violation — a pattern-filter hit the
# operator has chosen to count, or an AI-confirmed text deletion. A warning is
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

# How long a settled VPN operation stays in ``vpn_pending_ops`` afterwards.
#
# The table had no retention rule at all before this: every offer the bot ever
# made stayed in it for the life of the database. A day is long enough to answer
# "did that go through?" from the row and short enough that the table stays a
# working set rather than a history. It is measured from the operation's own
# expiry, so it can never cut short an operation that is still confirmable —
# see ``db.vpn_pending_prune``.
VPN_PENDING_RETENTION_SECONDS = _int("VPN_PENDING_RETENTION_SECONDS", 86400)

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
# The API refuses a manually-set deadline below 10 seconds outright
# ("400 INVALID_ARGUMENT ... Minimum allowed deadline is 10s"), so the floor is
# the API's and `app/ai_intent.py` clamps to it. The value is deliberately *at*
# that floor, and the reasoning is worth keeping because the obvious move —
# raise it, the 504s must mean the model is slow — was measured and rejected.
#
# Measured on 2026-09-22 18:51 against this deployment, on the very account
# whose row shows the 504s (`****bxvA`), same model, same prompt, only the
# deadline varying:
#
#     gemini-flash-lite-latest @ 10s   12 of 12 answered, at 0.8-1.8s
#     gemini-flash-lite-latest @ 25s   12 of 12 answered, at 0.8-1.8s
#
# — including a burst of six concurrent calls at each deadline, which is the
# shape that would expose queueing. So this classification answers in about a
# second, and ten seconds is a ten-fold headroom, not a budget the model needs.
#
# The 504s in the log (`provider_error detail=504`) are the provider honouring
# the deadline it was sent and aborting a call that had *hung* — a provider-side
# slowness episode at 17:59-18:21, not a systematically short deadline. Raising
# the deadline would not have made those calls finish; it would only have made
# the group message handler wait longer for the same answer. Failover to a
# healthy account is the remedy for a hung provider, and it works best when the
# deadline is short — so this stays at the floor and the fix is elsewhere
# (more accounts, and the wall-clock ceiling below).
GEMINI_TIMEOUT_SECONDS = _float("GEMINI_TIMEOUT_SECONDS", 10.0)

# One retry, with exponential backoff, and only for transient failures. A 429 or
# a 5xx is worth one more try; a malformed answer is not (it will be malformed
# again, and it is already counted).
GEMINI_MAX_RETRIES = _int("GEMINI_MAX_RETRIES", 1)
GEMINI_BACKOFF_SECONDS = _float("GEMINI_BACKOFF_SECONDS", 1.5)

# A ceiling on the **wall clock** of one logical request, on top of the
# per-attempt deadline and the attempt count.
#
# This is the defect the incident actually exposed. The pool tries every
# account, every compatible model and `retries + 1` attempts on each, capped
# only by `GEMINI_POOL_MAX_ATTEMPTS` (12). Twelve attempts at ten seconds is two
# minutes of wall clock for one ambiguous group message, and `on_group_text`
# *awaits* this — so the handler that offers a trial can be blocked for minutes.
# The attempt count bounds the spend; nothing bounded the time.
#
# The default is one failover — a second attempt at the full deadline, after the
# backoff between them — because this workload runs inside the group-message
# handler, and the most useful thing it can do with more time than that is hand
# the decision back to the rule engine. `0` means no ceiling, which is the
# behaviour every other workload keeps.
#
# It is enforced by the pool (`Pool.time_budget`), checked *before* each attempt,
# so it bounds the whole failover walk — every account and every model — and not
# one model's retries. The pool raises `time_budget` and records an event, so a
# request stopped by the ceiling is distinguishable in the log from one the
# provider refused.
GEMINI_INTENT_TIME_BUDGET_SECONDS = _float(
    "GEMINI_INTENT_TIME_BUDGET_SECONDS",
    2 * GEMINI_TIMEOUT_SECONDS + GEMINI_BACKOFF_SECONDS,
)

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

# ── The turn queue: bounded concurrency, ordered per conversation ─────────
#
# A group where ten people talk at once must not lose a message, and nobody may
# be blamed for the deployment's own limits. The two windows above therefore
# **queue** a turn rather than drop it: when a window is full, `app/chat_queue.py`
# waits and retries with a bounded backoff instead of returning a refusal, and
# the refused sentence was removed from `chat._MESSAGES` entirely.
#
# This is a different thing from the provider's rate limits, which stay in the
# pool (`gemini_pool` owns cooldowns, retries and failover). The internal brake
# and the external limit are never mixed: an internal full window is waited out,
# an external 429 is the pool's to absorb.
#
# `GEMINI_CHAT_MAX_CONCURRENCY` bounds how many model calls run at once so a
# burst cannot stampede the provider. `GEMINI_CHAT_QUEUE_MAX_WAIT` is how long a
# queued turn waits for a window to free before it gives up **silently** — never
# with a message to the user. It is derived from the larger of the two windows
# plus a margin so a burst inside one window is served rather than dropped; an
# operator may raise it but not lower it below the windows it has to outlast.
GEMINI_CHAT_MAX_CONCURRENCY = _int("GEMINI_CHAT_MAX_CONCURRENCY", 4)
GEMINI_CHAT_QUEUE_MAX_WAIT = _float(
    "GEMINI_CHAT_QUEUE_MAX_WAIT",
    max(GEMINI_CHAT_RATE_WINDOW, GEMINI_CHAT_USER_RATE_WINDOW) + 30.0,
)
GEMINI_CHAT_QUEUE_BACKOFF = _float("GEMINI_CHAT_QUEUE_BACKOFF", 1.5)

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

# How long one Telegram message may be. Telegram's hard limit is 4096
# characters and it counts after escaping, so the margin is for that. A reply
# longer than this is *split* across several messages, never truncated — the
# answer the person asked for is the answer they get.
GEMINI_CHAT_REPLY_CHARS = _int("GEMINI_CHAT_REPLY_CHARS", 3500)

# The ceiling on a whole answer, across all of its messages. It is a runaway
# guard, not a style rule: a model that loops must not turn one turn into a
# hundred messages. It is far above what the brief allows ("up to a hundred, up
# to two hundred lines"), so it never truncates an answer somebody asked for.
GEMINI_CHAT_REPLY_MAX_CHARS = _int("GEMINI_CHAT_REPLY_MAX_CHARS", 12000)

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

# How long an action the assistant proposed stays confirmable.
#
# Short, and shorter than the VPN confirmation's 900s would be too long: what is
# waiting is a decision the owner has just been asked about, in a conversation
# that is still open. A confirmation arriving an hour later is a different
# intention from the one that was recorded — the room has moved on, and the
# model may be acting on a sentence about something else.
ADMIN_CONFIRMATION_TTL_SECONDS = _int("ADMIN_CONFIRMATION_TTL_SECONDS", 600)

# How long a settled proposal stays in ``admin_pending_ops`` afterwards. Same
# shape and same reasoning as ``VPN_PENDING_RETENTION_SECONDS``: the window is
# measured from the proposal's own expiry, so it can never cut short an action
# that is still confirmable.
ADMIN_PENDING_RETENTION_SECONDS = _int("ADMIN_PENDING_RETENTION_SECONDS", 86400)

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

# The names the awareness layer is called by, when an owner switches it off or
# on out loud.
#
# This exists because the layer has no Telegram username to mention and no one
# obvious name. The room in this deployment calls it «اورنس» — a transliteration
# of "awareness" that is in neither dictionary — while the code and the
# documentation call it "awareness" and Persian would call it «آگاهی». All three
# have to work, or the owner says the word they actually use and nothing happens,
# which is exactly the bug that produced this setting.
#
# «پایش» is here for a different reason than the rest, and it is the reason the
# list has to include *descriptions* and not only names. The owner says «قطع کن
# این پایش رو» — "stop this watching" — and without this entry the router reads
# the verb «قطع کن», finds no awareness name, and silences the *assistant*
# instead of the layer the owner was talking about. That is the exact confusion
# this list exists to prevent, so the word that describes the layer counts as
# naming it. It is a setting precisely so an operator who finds «پایش» too
# general can take it out without touching code.
#
# Matched as whole words, case-insensitively, and used only to decide *which
# switch* a spoken command is about. It is not an authority of any kind: the
# speaker is checked against the owner id separately, and the transition itself
# goes through ``app/admin_service.py`` like every other administrative act.
NEXUS_AWARENESS_NAMES = _str_list(
    os.getenv("NEXUS_AWARENESS_NAMES", "awareness,اورنس,آگاهی,اگاهی,پایش")
)

# The names the Web Search switch is called by, when the owner turns it off or
# on out loud. It is a third switch beside Nexus and awareness, and it is
# matched the same way: whole words, case-insensitively, and only to decide
# *which* switch a spoken command is about. It grants nothing — the speaker is
# still checked against the owner id, and the transition goes through
# ``app/admin_service.py``.
#
# «سرچ» is the word the owner actually types; «جستجو» is the formal Persian for
# it, and the two ZWNJ spellings are both here because Persian writes the word
# both ways and a whole-word match would otherwise miss one of them.
NEXUS_SEARCH_NAMES = _str_list(
    os.getenv("NEXUS_SEARCH_NAMES", "search,سرچ,جستجو,جست‌وجو")
)

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

# The name-awareness block: when a message mentions people the room knows, the
# model is given who they are — name, username, id — even if they have not spoken
# in the window. It is relevance-filtered by construction (only names the message
# actually contains) and bounded twice, so it never becomes a member dump. This is
# what makes "Nexus knows everybody's name" true without carrying the roster.
NEXUS_PEOPLE_CONTEXT_ITEMS = _int("NEXUS_PEOPLE_CONTEXT_ITEMS", 8)
NEXUS_PEOPLE_CONTEXT_CHARS = _int("NEXUS_PEOPLE_CONTEXT_CHARS", 300)

# How many of the room's most-recently-seen people the mention reader scans.
# It runs on every room-dependent reply, so it is bounded by MEASUREMENT: an
# unbounded scan of a 5000-member room cost ~90 ms per message (the read plus a
# normalisation per row), against ~10 ms for the 500 most recent. A name a
# message mentions is overwhelmingly somebody recently present, and a miss is a
# missing context line, never a wrong one — so this is the right thing to bound.
NEXUS_PEOPLE_ROSTER_SCAN = _int("NEXUS_PEOPLE_ROSTER_SCAN", 500)


# ---------------- Nexus Memory: what the server may remember about a person ---
# A bounded, structured long-term memory about ONE person, so Nexus knows
# something durable about a member it has not met this hour. It is deliberately
# NOT: conversation history, the room window, Awareness, Intent, or raw messages.
#
# What it may hold is narrow on purpose. It records only what a person explicitly
# asked to be remembered — the clause they typed, bounded and verbatim — never a
# fact the server inferred from ordinary conversation. That refusal is what keeps
# the write path free: extracting a fact from ordinary talk would need a model
# call or a change to the awareness prompt, and the evidence rule forbids both.
# Nothing here grants anything: a memory is data the model may read, never a
# permission, an authorisation or a gate. Authority stays in ``app/rbac.py``.
NEXUS_MEMORY_ENABLED = _bool("NEXUS_MEMORY_ENABLED", True)

# The per-person ceiling. Sized by MEASUREMENT, not by the brief's 20–50: storage
# is 208 bytes/row, so 30 items x 3000 members is 17.9 MB — an order of magnitude
# under the 200 MB budget, meaning disk is not what should choose this number. The
# real bound is what can ever be *read*: the retrieval block is ~300 characters
# and surfaces at most ~4 items, so 30 leaves a ~7x recall margin while keeping
# the table a bounded fact set rather than a log. The least recently updated row
# is dropped first when a person goes over.
NEXUS_MEMORY_MAX_PER_USER = _int("NEXUS_MEMORY_MAX_PER_USER", 30)

# A global ceiling and an age bound, both applied on the observation path because
# this process has no scheduler. The global cap is enforced rarely — a
# whole-table prune is the one expensive statement here — while the age delete is
# indexed and cheap. Either way a table that only grows is a table that
# eventually stops being written to.
NEXUS_MEMORY_MAX = _int("NEXUS_MEMORY_MAX", 50000)
NEXUS_MEMORY_RETENTION = _int("NEXUS_MEMORY_RETENTION", 180 * 86400)

# How long a single remembered clause may be, and how many of a person's memories
# one context block may show. Both are small because this is context, not a
# dossier: the point is that the model knows a durable thing or two about the
# person the batch is about, not that it can enumerate them.
NEXUS_MEMORY_VALUE_CHARS = _int("NEXUS_MEMORY_VALUE_CHARS", 200)
NEXUS_MEMORY_ITEMS = _int("NEXUS_MEMORY_ITEMS", 4)
NEXUS_MEMORY_CHARS = _int("NEXUS_MEMORY_CHARS", 300)

# ── Automatic extraction ──────────────────────────────────────────────────
# W v1 stored a memory only when a person explicitly asked. Automatic extraction
# also learns stable, useful characteristics from ordinary conversation — the
# same store, a second write path, and a deliberately conservative one.
#
# It is **deterministic first**: a closed vocabulary of SLOTS (identity,
# interest, preference, style, humour) matched by rules over the message the
# person typed. A slot is the memory's *identity*, so a new value for the same
# slot replaces the old one — which is what stops "I program in JavaScript" and
# "I program in Python" from both living for ever. The slot vocabulary is closed
# and small, so a person's automatic memories are bounded by the vocabulary
# rather than by how much they talk.
NEXUS_MEMORY_AUTO_ENABLED = _bool("NEXUS_MEMORY_AUTO_ENABLED", True)

# A repeated *behaviour* (a style or humour signal) is only promoted to a memory
# after this many observations, because one playful message is not a personality.
# The counters live in their own bounded table and decay by age, so a person who
# was playful a year ago is not labelled playful for ever.
NEXUS_MEMORY_SIGNAL_THRESHOLD = _int("NEXUS_MEMORY_SIGNAL_THRESHOLD", 5)
NEXUS_MEMORY_SIGNAL_RETENTION = _int(
    "NEXUS_MEMORY_SIGNAL_RETENTION", 30 * 86400
)

# ── Relationship: how this person has treated Nexus ───────────────────────
# The behavioural memory the persona reads. It answers a different question from
# the rest of this section — not "what do I know about them" but "how have they
# treated *me*" — and it exists so the ability to answer rudeness in kind is
# gated on a real history instead of being the state the model starts in.
#
# It is counted, not inferred: only messages **directed at Nexus** are observed,
# a single message never promotes, and the counters decay on the same sweep as
# every other signal. So one bad evening does not brand a member, and somebody
# who was hostile a month ago and has been friendly since is not hostile now.
# The two directions are deliberately separate signals, so the promoted tone is
# the *more recently demonstrated* one rather than a total: see
# ``memory.relationship``.
NEXUS_RELATIONSHIP_ENABLED = _bool("NEXUS_RELATIONSHIP_ENABLED", True)

# How many directed hostile (or friendly) messages before the tone is promoted.
# Lower than ``NEXUS_MEMORY_SIGNAL_THRESHOLD`` on purpose: a style is a
# preference and can wait, while being cursed at repeatedly is a fact the next
# reply should know about. Still a repetition, so a single angry message — which
# the persona already answers at the strength it was given — changes nothing.
NEXUS_RELATIONSHIP_THRESHOLD = _int("NEXUS_RELATIONSHIP_THRESHOLD", 3)

# The rendered block's ceiling. Small: it is two or three sentences of the
# server's own observation, prepended beside the owner note, and it is never a
# transcript of what was said. Sized so the instruction — which is the part that
# must never be clipped — is followed by the counts rather than preceded by them.
NEXUS_RELATIONSHIP_CHARS = _int("NEXUS_RELATIONSHIP_CHARS", 320)

# ── The model seam (OFF by default, and isolated when on) ──────────────────
# For a sentence that looks like durable self-information but matches no slot,
# the deterministic layer may ask a model to structure it. That path is OFF
# unless an operator turns it on AND gives the ``memory`` workload its own
# credential; with either absent, extraction is deterministic-only and makes no
# provider call at all. When it does run it uses its own workload pool (its own
# key slots, model preference, breaker and daily allowance) and its output is
# treated as **untrusted candidate data**: the server validates it against the
# same slot vocabulary and the same rejections as the deterministic path, and
# drops anything it does not recognise.
NEXUS_MEMORY_EXTRACT_MODEL = _bool("NEXUS_MEMORY_EXTRACT_MODEL", False)

# The model path's own per-account daily allowance, separate from chat's and
# awareness's so it can never spend the request a person is waiting on an answer
# to. Only consulted when the path is enabled and credentialed.
NEXUS_MEMORY_MODEL_DAILY_LIMIT = _int("NEXUS_MEMORY_MODEL_DAILY_LIMIT", 50)


# ---------------- Nexus State: what the interaction is trying to do -----------
# Increment X. A *different layer* from Memory, and the distinction is the whole
# design: Memory answers "what durable thing do I know about this person", State
# answers "what is the current interaction trying to accomplish". A preference
# for Python is Memory; "currently debugging the Python authentication bug" is
# State. State is keyed by ``(chat_id, user_id)`` — one active state per person
# per room, never a global state — so a group can never inherit another group's
# task and a private task can never render in a group.
#
# It is deterministic-only and makes **no provider call**: the roadmap scopes
# increment X at "Gemini: 0 expected", the request allowance is rationed, and
# the deterministic signals (an explicit task statement, an explicit completion,
# a continuation marker, a question about the active task) cover the cases that
# matter. There is deliberately no ``state`` pool and no model seam; see
# ``app/state.py`` for why, and for the refusal that keeps it out of the request
# budget entirely.
NEXUS_STATE_ENABLED = _bool("NEXUS_STATE_ENABLED", True)

# The automatic write path (reading ordinary messages for a state transition).
# Turning it off keeps the read and the block but stops the learning, the same
# two-switch shape Memory uses.
NEXUS_STATE_AUTO_ENABLED = _bool("NEXUS_STATE_AUTO_ENABLED", True)

# One active row per person per room, so there is no per-person ceiling to size —
# the row *is* the bound. This is the global backstop for many members, applied
# on the observation path because this process has no scheduler.
NEXUS_STATE_MAX = _int("NEXUS_STATE_MAX", 50000)

# How long a task stays "current". The single window, used both for rendering
# (a state older than this is not shown — a task idle for three days is over) and
# for the age prune. Three days is chosen so "let's continue this tomorrow"
# survives and an abandoned task from last week does not linger; one number, and
# an operator who wants longer continuity changes one environment variable.
NEXUS_STATE_TTL = _int("NEXUS_STATE_TTL", 72 * 3600)

# The per-field cap and the block budget. Both are small because State is a
# compact summary — a topic, a goal, an unresolved question — and never a
# transcript. The value cap keeps a pasted paragraph out of the row; the block
# budget keeps the rendered context a sentence or three.
NEXUS_STATE_VALUE_CHARS = _int("NEXUS_STATE_VALUE_CHARS", 120)
NEXUS_STATE_CHARS = _int("NEXUS_STATE_CHARS", 300)


# ---------------- Nexus context composition (increment Y) ---------------------
# The hard ceiling on the four **selectable** context sources together: the room
# window, the server's reading of the message, the active state and the person's
# memory — the sources the selector chooses between and is therefore allowed to
# drop.
#
# It deliberately does **not** count the administrative roster, the server date
# or the web findings. Those are never dropped — the roster is a security
# property, the date is what stops a date being invented, and the findings are
# the only reason a live answer can be grounded — so counting them would make
# the ceiling self-defeating: a large roster (an owner's is ~3700 characters on
# its own) would exceed the limit with nothing left to drop, and the only effect
# would be to strip the room out of the answer. A ceiling may only bound what
# the thing enforcing it is able to remove.
#
# It is a **safety valve, not a target**. Each source already has its own cap
# beneath it (``NEXUS_AWARENESS_WINDOW_CHARS`` for the room window,
# ``NEXUS_AWARENESS_CONTEXT_CHARS`` for the reading, ``NEXUS_STATE_CHARS`` and
# ``NEXUS_MEMORY_CHARS`` for the two personal blocks), so the sum is already
# bounded; this number bounds the sum. Y enforces it by **dropping whole
# sources** in reverse precedence — memory first, then state, then the room
# window — never by slicing a rendered block in half, because a fragment of a
# sentence costs tokens and tells the model less than nothing.
#
# 3500 sits below the sum of the per-source caps (a worst case near 8100) and
# well above the ordinary turn (the fast path carries no room window at all).
# It is measured in ``tools/eval_context.py``; an operator who wants a smaller
# prompt lowers it, and the ceiling only ever *removes* context.
NEXUS_CONTEXT_CHARS = _int("NEXUS_CONTEXT_CHARS", 3500)


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
# alone lets a hundred and fifty long messages become a huge prompt, and a
# character bound alone lets a flood of one-word messages push the real context
# out.
#
# The count was 40, and on this deployment that made the character bound
# decorative rather than a second guard: the room's messages average eighteen
# characters, so forty of them occupy about 700 of the 6000 available and the
# count was the only bound that ever applied. The price of that is coverage, and
# it is measured rather than theoretical. A pass runs once every ~8.7 minutes —
# the daily allowance spread evenly across the API day, see
# ``_awareness_allowance_gap`` in ``app/main.py`` — and 106 messages arrived
# between two consecutive passes, so a 40-message window read at most 38% of
# them. Worse, a pass records the *newest* unread id as understood, so the other
# 62% were not read late, they were not read at all.
#
# 150 is sized so that one pass covers the interval it is spaced at, leaving the
# character bound to do the trimming when the messages are long. It costs
# nothing in the currency that is actually rationed: ``NEXUS_AWARENESS_DAILY_LIMIT``
# counts *requests*, not tokens, so reading more of the same conversation per
# pass is free, and the prompt stays bounded by the character budget either way.
#
# **The window is now measured in time, and the count is only a safety cap.**
# The owner's requirement: «برحسب پیام نباشه، برحسب روز باید باشه تا سه روز» —
# the room should be read as "the last three days", not as "the last N messages".
# ``NEXUS_AWARENESS_WINDOW_SECONDS`` is therefore the bound that decides what is
# in the window, and the count below is what stops a flood from turning one read
# into an unbounded scan: the read is capped, the *time* is not.
#
# The transcript that reaches the model is still bounded by the character budget
# beneath this (6000), so widening the time does not widen the prompt — in a busy
# room the character bound is what actually trims, and in a quiet room the time
# bound is what keeps the window from reaching back past three days. What the
# wider time buys in a busy room is the *digest* (see
# ``NEXUS_AWARENESS_ACTIVITY_*``), which summarises the whole three days for a
# few hundred characters rather than trying to put them in the prompt.
NEXUS_AWARENESS_WINDOW_MESSAGES = _int("NEXUS_AWARENESS_WINDOW_MESSAGES", 400)
NEXUS_AWARENESS_WINDOW_CHARS = _int("NEXUS_AWARENESS_WINDOW_CHARS", 6000)

# How far back the room window reaches. Three days, in seconds. Measured, not
# guessed: the production room runs at ~610 messages/hour, so three days is
# ~44,000 rows and ~9 MB in this table, and the time-bounded read of the newest
# 400 of them is 0.9 ms (the full 44,000-row scan is 277 ms, which is why the
# count cap above exists). The digest's GROUP BY over the same three days is
# 18 ms and runs once per pass, not per message.
NEXUS_AWARENESS_WINDOW_SECONDS = _int(
    "NEXUS_AWARENESS_WINDOW_SECONDS", 3 * 86400
)

# How long a captured message is kept. The window is a *recent* view of the
# room, not a transcript: rows older than this are dropped, which is what stops
# the table from becoming a permanent record of the group's conversation.
#
# It has to be at least ``NEXUS_AWARENESS_WINDOW_SECONDS`` or the window it
# describes would be emptied from behind: the time bound would ask for three
# days of a table that retention had already cut to one. It is three days for
# the same reason the window is — that is the horizon the owner asked for.
NEXUS_AWARENESS_RETENTION_SECONDS = _int(
    "NEXUS_AWARENESS_RETENTION_SECONDS", 3 * 86400
)

# A ceiling on the table as well as on the age, because a busy hour can produce
# more rows than the age bound alone would remove. Applied per chat, oldest
# first.
#
# It is a **flood** bound, not the ordinary bound: three days at the measured
# rate is ~44,000 rows, so 60,000 leaves headroom and only a genuine flood
# reaches it. It is deliberately *not* enforced on every message any more — a
# 60,000-row trim costs 58 ms (measured) and this is the hottest path in the
# feature — so it runs on the same clock as the age purge, once per
# ``NEXUS_AWARENESS_PURGE_INTERVAL_SECONDS``. A flood that arrives inside one
# such interval is bounded by the next one, and the age purge beside it is the
# bound that actually applies in normal traffic.
NEXUS_AWARENESS_MAX_ROWS = _int("NEXUS_AWARENESS_MAX_ROWS", 60000)

# The three-day digest: who was in this room, who spoke and how much, what each
# of them said last, and who has been silent. This is what makes "the window is
# three days" mean something the model can actually use — three days of raw
# messages cannot fit in a prompt, but one line per person can.
#
# It is a **server count, not a model reading**: a single indexed GROUP BY over
# the time window (18 ms measured over 44,000 rows, once per pass) plus one
# indexed lookup per listed person for their newest words (0.07 ms for eight,
# measured). No provider is called for it, and it is bounded by rows and by
# characters so a large room degrades to a shorter digest rather than a longer
# prompt.
NEXUS_AWARENESS_ACTIVITY_ENABLED = _bool("NEXUS_AWARENESS_ACTIVITY_ENABLED", True)

# How many people the digest lists, most active first. Bounded because the
# digest is context, not a membership dump.
NEXUS_AWARENESS_ACTIVITY_PEOPLE = _int("NEXUS_AWARENESS_ACTIVITY_PEOPLE", 12)

# The digest's whole ceiling, header included.
NEXUS_AWARENESS_ACTIVITY_CHARS = _int("NEXUS_AWARENESS_ACTIVITY_CHARS", 1200)

# How much of each person's newest message is quoted. One short line is what
# makes "who said what" concrete without turning the digest into a transcript.
NEXUS_AWARENESS_ACTIVITY_SNIPPET_CHARS = _int(
    "NEXUS_AWARENESS_ACTIVITY_SNIPPET_CHARS", 60
)

# How many silent people are named. "Who did not say anything" is part of the
# requirement, and it is the one half a transcript cannot show.
NEXUS_AWARENESS_ACTIVITY_SILENT = _int("NEXUS_AWARENESS_ACTIVITY_SILENT", 8)

# How long a scheduling *hint* may live, and therefore how long a low-priority
# room may be postponed. It used to be derived from the retention window with no
# cap — one number, so the two could not drift — and that was right while the
# retention was an hour. Now that the retention is three days, deriving it
# unchanged would let a room whose batch is idle chatter be postponed for three
# days, which is a behaviour change nobody asked for. The bound is therefore the
# **smaller** of this and the retention: the invariant the derivation protected
# (a hint never outlives the rows it describes) still holds, and the delay keeps
# a sane ceiling. See ``app/awareness_schedule._bound``.
NEXUS_AWARENESS_HINT_SECONDS = _int("NEXUS_AWARENESS_HINT_SECONDS", 3600)

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

# ── The staged context ────────────────────────────────────────────────────
# The awareness pass is handed a transcript plus whatever else the server
# already knows about the room. That extra context is assembled from *sources*
# (see ``app/awareness_context.py``), and these four numbers bound it. The point
# of the split is that the cheap half is always there and the expensive half is
# only built when the batch calls for it — the allowance is rationed in
# requests, so tokens spent on context nobody asked for are paid on every pass.
#
# ``NEXUS_AWARENESS_CONTEXT_CHARS`` is the hard ceiling on all of it together;
# each source has its own cap beneath it. ``NEXUS_AWARENESS_CONTEXT_DEEP`` is the
# kill switch for the conditional tier, which is the half that reads the
# database: switching it off leaves the free context and removes every extra
# query a pass could make.
NEXUS_AWARENESS_CONTEXT_CHARS = _int("NEXUS_AWARENESS_CONTEXT_CHARS", 1500)
NEXUS_AWARENESS_CONTEXT_DEEP = _bool("NEXUS_AWARENESS_CONTEXT_DEEP", True)

# How many recent administrative actions the context may show, and how many
# people it may describe. Both are bounded because this is context rather than a
# directory: the point is that a pass can see what has just happened here and
# who the batch is about, not that it can enumerate the room.
NEXUS_AWARENESS_ADMIN_ACTIONS = _int("NEXUS_AWARENESS_ADMIN_ACTIONS", 5)
NEXUS_AWARENESS_REFERENCED_PEOPLE = _int("NEXUS_AWARENESS_REFERENCED_PEOPLE", 4)

# How many referent candidates a deictic instruction may be shown. Bounded for
# the same reason as the two above — this is context, not a directory — and
# kept small deliberately: a list of six people is a list the model has to
# reason about, and the resolver's whole point is that the answer is usually
# one or two names, or an honest "these are too close to tell apart".
NEXUS_AWARENESS_REFERENTS = _int("NEXUS_AWARENESS_REFERENTS", 4)

# How confident the server must be that a reply would be a natural continuation
# before an ambient pass is allowed to speak. It is the answer to "do not reply
# to every conversation": the model still decides *whether* a reply is called for,
# but a `respond` with no confidence behind it — from either the server's own
# subject reading or the model's own participation score — stays silent.
#
# The two are combined as a maximum, so either can carry the decision and neither
# is a veto. 60 is chosen against the server's own grades: a call (95), a reply
# edge (88), the name coming up (78) and a deictic pointing at the assistant (72)
# all clear it, a subject that has merely been continued for several turns does
# not clear it on continuity alone, and a general discussion (0) never does.
NEXUS_AWARENESS_PARTICIPATION_FLOOR = _int(
    "NEXUS_AWARENESS_PARTICIPATION_FLOOR", 60
)

# Sent when an awareness pass actually performed an action but the model gave no
# wording for it. Rare, and the alternative is worse: an administrator whose
# instruction was carried out and never acknowledged believes it was ignored,
# and repeats it. The action's own outcome is in the audit log either way.
NEXUS_AWARENESS_ACTION_TEXT = os.getenv(
    "NEXUS_AWARENESS_ACTION_TEXT",
    "انجام شد ✅",
)

# How long the bot's own Telegram rights in a chat are trusted before being
# re-read. Short, because it is a *capability* rather than a secret: a bot
# promoted or demoted a moment ago should be described correctly almost at once,
# and a stale answer is what produces the defect the owner reported — the
# assistant announcing it has no permission while holding it.
#
# A negative is never served from the cache at all; see ``main._bot_right``.
BOT_RIGHTS_TTL_SECONDS = _float("BOT_RIGHTS_TTL_SECONDS", 45.0)


# ---------------- Gemini: moderation / content understanding ------------------
# A **third** independent Gemini workload. It is not the acquisition classifier
# and not the conversational assistant, and it shares nothing with either: its
# own key setting, its own model, its own rate window, its own daily cap, its
# own circuit breaker, its own counters table and its own client.
#
# What it is for: understanding what a group message *is*, well enough for a
# deterministic policy to act on. A pattern rule can see a link or a banned
# word; it cannot see targeted abuse, a threat, or a scam phrased in words it
# has never been given. This layer answers that wider question, and answers it
# with a small structured verdict rather than prose.
#
# What it is NOT for, and this is the architectural line the whole design turns
# on: **it never executes anything.** It cannot delete, restrict, ban or reply.
# Its output is data. The decision to act is made by app/mod_policy.py, in code,
# from its verdict plus the configuration. There is no
# code path from this module's return value to a Telegram call, which is why a
# prompt-injected group message cannot make the bot do anything.
#
# Why a separate key matters here more than anywhere else: text moderation can
# run on a large share of a busy group's messages, so this is a heavy consumer.
# If it shared the classifier's project it would starve acquisition
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

# A generous bound, because the request is a single message: it is only the
# classifier's 10s that this is comfortably above.
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

# Text is truncated to this before it leaves the server.
GEMINI_MOD_MAX_CHARS = _int("GEMINI_MOD_MAX_CHARS", 2000)


# ---------------- Media understanding (the conversational path) --------------
# One builder for "Telegram media -> something Gemini can read", used by the
# assistant when someone sends it a photo, a video or a voice note. The
# moderation workload used to share it; that path was removed, so this is the
# only caller now.
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
# The deterministic engine that turns the moderation AI's verdict into an
# action. Everything here is about *how sure we have to be before we destroy
# something*, and the defaults are deliberately the conservative end.
#
# The media path used to feed a local visual detector into this policy, and its
# settings lived here too: a hard local threshold, a "delete without the AI"
# mode, a media switch, an ask-the-AI-about-everything lever. That whole
# pipeline was removed, so the policy is now simply: a confident AI verdict can
# delete, and nothing else can.
MODERATION_ENABLED = _bool("MODERATION_ENABLED", True)

# The AI's confidence must be at least this before its "clearly explicit"
# classification is acted on. Below it the verdict is treated as uncertain and
# the content is only logged.
MODERATION_DELETE_CONFIDENCE = _float("MODERATION_DELETE_CONFIDENCE", 0.80)

# The band below the delete confidence that is still worth recording: the
# content is allowed, but an operator can see it in the log and in the review
# queue. A false positive here costs a log line, not a message.
MODERATION_REVIEW_CONFIDENCE = _float("MODERATION_REVIEW_CONFIDENCE", 0.45)

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
# oversight. This is the only path left that can delete a person's *words*, in a
# language the model may misjudge, and a false positive here removes something
# somebody wrote and cannot get back. The capability is implemented and tested;
# turning it on is a decision the operator should make after watching the review
# log for a while, not a default this file imposes.
MODERATION_TEXT_ENABLED = _bool("MODERATION_TEXT_ENABLED", False)

# Messages shorter than this are not sent to the moderation layer. A three-word
# line has almost no signal for a content classifier, and the cost is a request
# against a shared quota.
MODERATION_TEXT_MIN_CHARS = _int("MODERATION_TEXT_MIN_CHARS", 25)


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
# runs on its own timer rather than being folded into another job's.
UPDATE_DEDUP_PRUNE_INTERVAL_SECONDS = _float(
    "UPDATE_DEDUP_PRUNE_INTERVAL_SECONDS", 3600.0
)


# ── The coding-agent bridge ───────────────────────────────────────────────
# See ``app/agent_bridge.py`` for the design and ``AgentMD.md`` §39 for the
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
# §39.15.
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

# One "working" message, edited in place, instead of one message per progress
# line. A task used to be able to produce twenty near-identical messages, each
# repeating the same header; the owner asked for fewer and calmer. The throttle
# above now limits *edits* rather than messages, and the answer at the end is
# still always sent as its own message — only progress is overwritten. Set this
# to 0 to go back to a message per progress line.
AGENT_WORKING_MESSAGE = _bool("AGENT_WORKING_MESSAGE", True)

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
# The header on the second and later parts of a long answer. It carries the part
# number so a three-part answer reads as one answer rather than three identical
# "done" messages.
AGENT_CONTINUATION_HEADER = os.getenv(
    "AGENT_CONTINUATION_HEADER",
    "↩️ {request_id} — {repository} ({part}/{total})",
)
AGENT_FAILED_HEADER = os.getenv(
    "AGENT_FAILED_HEADER",
    "❌ {request_id} — {repository}: ناموفق",
)
AGENT_TIMEOUT_HEADER = os.getenv(
    "AGENT_TIMEOUT_HEADER",
    "⌛️ {request_id} — {repository}: از زمان خارج شد",
)
# Sent when a dangerous task waited for an approval nobody gave. It is a
# different sentence from the two timeout bodies because it is a different
# event: nothing ran, nothing is running, and the runner was never asked to do
# anything. The owner has to be able to tell "you did not approve this in time"
# from "the host never picked this up".
AGENT_APPROVAL_LAPSED_TEXT = os.getenv(
    "AGENT_APPROVAL_LAPSED_TEXT",
    "این درخواست تأیید نشد و باطل شد؛ چیزی اجرا نشد. اگر هنوز لازمه دوباره بفرست.",
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
# Sent when the assistant proposed a privileged action and recorded it instead
# of doing it. It has to say three things and in this order: what was recorded,
# that nothing happened yet, and what the next step is. A sentence that only
# said "are you sure?" would leave the owner unable to tell whether the action
# was pending or already done.
ADMIN_AWAITING_CONFIRMATION_TEXT = os.getenv(
    "ADMIN_AWAITING_CONFIRMATION_TEXT",
    "🔐 این کار هنوز اجرا نشده و ثبت شد تا خودت تأییدش کنی. "
    "اگه می‌خوای انجام بشه بگو «تأیید می‌کنم».",
)
# Sent when a confirmation is refused — the wrong person asked, nothing was
# waiting, or the reference did not resolve to a waiting action. Four causes,
# one sentence, because the Persian wording is the same for all of them: the
# answer to "why" is the English gloss the model gets, not this line.
ADMIN_CONFIRM_REFUSED_TEXT = os.getenv(
    "ADMIN_CONFIRM_REFUSED_TEXT",
    "⛔️ تأیید نشد؛ چیزی برای تأیید کردن پیدا نشد یا تأییدکننده مالک نبود.",
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
    "پاسخ‌دهی به: {answer_scope}\n"
    "درک گفتگوی گروه: {awareness}\n"
    "سرچ وب: {search}\n"
    "کانتکست صوتی: {voice_context}\n"
    "{mode}",
)
NEXUS_STATE_ONLINE_LABEL = os.getenv("NEXUS_STATE_ONLINE_LABEL", "روشن (ONLINE)")
NEXUS_STATE_OFFLINE_LABEL = os.getenv("NEXUS_STATE_OFFLINE_LABEL", "خاموش (OFFLINE)")
NEXUS_OBSERVE_ON_LABEL = os.getenv("NEXUS_OBSERVE_ON_LABEL", "فعال")
NEXUS_OBSERVE_OFF_LABEL = os.getenv("NEXUS_OBSERVE_OFF_LABEL", "غیرفعال")
# Reported in `/nexus status` so the owner can see, from inside the group, who
# gets answered. The boundary is the **room**, not the speaker: every member of
# a registered group is answered, and an administrator is not a different kind
# of member. The line is deliberately about the room, because the previous
# "answers only administrators / everyone" line described a speaker gate that no
# longer decides anything.
NEXUS_ANSWER_SCOPE_LABEL = os.getenv(
    "NEXUS_ANSWER_SCOPE_LABEL", "همهٔ اعضای گروه‌های ثبت‌شده"
)
# Kept for a deployment that still sets them: they no longer affect who is
# answered, because group conversational eligibility is decided by the room
# allowlist and not by the speaker. Read nowhere.
NEXUS_ACTORS_ONLY_ON_LABEL = os.getenv("NEXUS_ACTORS_ONLY_ON_LABEL", "فقط مدیرها")
NEXUS_ACTORS_ONLY_OFF_LABEL = os.getenv("NEXUS_ACTORS_ONLY_OFF_LABEL", "همه")

# ── The group allowlist's typed commands ──────────────────────────────────
# `/registergroup` and `/unregistergroup` register or revoke the room the
# command is typed in; `/groups` lists them. The sentences are the operator's,
# and they say what happened rather than "done", because "which room did I just
# authorize?" is the question that matters when the answer is not what was
# meant.
GROUP_REGISTER_DONE_TEXT = os.getenv(
    "GROUP_REGISTER_DONE_TEXT",
    "این گروه ثبت شد. از این به بعد نکسوس در این گروه فعال است و به همهٔ اعضا پاسخ می‌دهد.",
)
GROUP_REGISTER_NOT_A_GROUP_TEXT = os.getenv(
    "GROUP_REGISTER_NOT_A_GROUP_TEXT", "این دستور فقط داخل گروه کار می‌کند."
)
GROUP_REVOKE_DONE_TEXT = os.getenv(
    "GROUP_REVOKE_DONE_TEXT",
    "ثبت این گروه لغو شد. نکسوس دیگر در این گروه پاسخ نمی‌دهد.",
)
GROUP_LIST_TITLE = os.getenv("GROUP_LIST_TITLE", "گروه‌های ثبت‌شده:")
GROUP_LIST_EMPTY = os.getenv("GROUP_LIST_EMPTY", "هیچ گروهی ثبت نشده است.")
GROUP_LIST_LINE = os.getenv(
    "GROUP_LIST_LINE", "{chat_id} — {state} — افزوده توسط {added_by}"
)
GROUP_STATUS_ENABLED_LABEL = os.getenv("GROUP_STATUS_ENABLED_LABEL", "فعال")
GROUP_STATUS_DISABLED_LABEL = os.getenv("GROUP_STATUS_DISABLED_LABEL", "لغو‌شده")
# The awareness line, reported for the same reason the actor gate is: "Nexus did
# not react" and "Nexus is not reading the room at all" look identical from
# inside a group, and only one of them is a bug.
NEXUS_AWARENESS_ON_LABEL = os.getenv("NEXUS_AWARENESS_ON_LABEL", "فعال")
NEXUS_AWARENESS_OFF_LABEL = os.getenv("NEXUS_AWARENESS_OFF_LABEL", "غیرفعال")

# The two confirmations for the awareness switch, separate sentences for the
# same reason Nexus's own two are: "the room is being read again" and "the room
# is not being read" are the two facts an owner most needs to read
# unambiguously, and a templated sentence that got the state wrong would be read
# as the opposite of what happened.
#
# They deliberately do **not** say Nexus is off. An owner who reads a bare
# «خاموش شد» after switching awareness off would reasonably conclude the
# assistant had stopped answering, which is the one thing this switch must never
# do — so the wording names the layer and says the chat keeps working.
NEXUS_AWARENESS_OFF_DONE_TEXT = os.getenv(
    "NEXUS_AWARENESS_OFF_DONE_TEXT",
    "🙈 آگاهی خاموش شد. از این به بعد چت معمولی و سریع جواب می‌دم و اتاق رو تحلیل نمی‌کنم.",
)
NEXUS_AWARENESS_ON_DONE_TEXT = os.getenv(
    "NEXUS_AWARENESS_ON_DONE_TEXT",
    "👁 آگاهی روشن شد. از این به بعد اتاق رو هم تحلیل می‌کنم.",
)
NEXUS_AWARENESS_ALREADY_TEXT = os.getenv(
    "NEXUS_AWARENESS_ALREADY_TEXT",
    "آگاهی از قبل {state} بود.",
)
# Said when the owner asks for the layer back and the *deployment* has it off.
# The spoken switch and the deploy-time setting are two halves of one answer, and
# «آگاهی روشن» can only move one of them: the row is stored, the layer still does
# not run, and a confirmation that said otherwise would be the same class of lie
# this whole feature exists to remove. The fix is a restart, and only the
# operator can do it, so the sentence says so rather than pretending.
NEXUS_AWARENESS_CONFIG_OFF_TEXT = os.getenv(
    "NEXUS_AWARENESS_CONFIG_OFF_TEXT",
    "⚠️ آگاهی توی تنظیمات این ربات خاموش شده، پس با پیام روشن نمی‌شه. "
    "برای روشن کردنش باید NEXUS_AWARENESS_ENABLED=true باشه و ربات ری‌استارت بشه.",
)

# The Web Search switch, reported for the same reason the awareness line is:
# "Nexus did not look that up" and "Nexus is not allowed to search at all" look
# identical from inside a group, and only one of them is a bug. The switch is
# the owner's, it is persisted, and it survives a restart.
NEXUS_SEARCH_ON_LABEL = os.getenv("NEXUS_SEARCH_ON_LABEL", "فعال")
NEXUS_SEARCH_OFF_LABEL = os.getenv("NEXUS_SEARCH_OFF_LABEL", "غیرفعال")
# Separate sentences for the two directions, and neither says Nexus is off: an
# owner who read a bare «خاموش شد» after switching search off would reasonably
# conclude the assistant had stopped answering, which is the one thing this
# switch must never do. The wording names the layer and says chat keeps working.
NEXUS_SEARCH_OFF_DONE_TEXT = os.getenv(
    "NEXUS_SEARCH_OFF_DONE_TEXT",
    "🔎 سرچ خاموش شد. از این به بعد از اینترنت چیزی نمی‌گیرم و جواب‌ها بر اساس "
    "دانش خودم می‌مونه. چت و آگاهی دست‌نخورده‌اند.",
)
NEXUS_SEARCH_ON_DONE_TEXT = os.getenv(
    "NEXUS_SEARCH_ON_DONE_TEXT",
    "🔎 سرچ روشن شد. از این به بعد برای اطلاعات زنده می‌تونم از اینترنت چک کنم.",
)
NEXUS_SEARCH_ALREADY_TEXT = os.getenv(
    "NEXUS_SEARCH_ALREADY_TEXT", "سرچ از قبل {state} بود."
)
# Said when the owner asks for search back and the *deployment* has it off. The
# spoken switch and the deploy-time setting are two halves of one answer, and
# «سرچ روشن» can only move one of them — the row is stored, the workload still
# does not run, and the fix is a restart only the operator can do.
NEXUS_SEARCH_CONFIG_OFF_TEXT = os.getenv(
    "NEXUS_SEARCH_CONFIG_OFF_TEXT",
    "⚠️ سرچ توی تنظیمات این ربات خاموش شده، پس با پیام روشن نمی‌شه. "
    "برای روشن کردنش باید GEMINI_SEARCH_ENABLED=true باشه و ربات ری‌استارت بشه.",
)
# Asked before an *inferred* search. The bot believes a live lookup would help
# but the person did not ask for one, so it asks instead of spending a request —
# and the topic is remembered so the answer to this question is what runs the
# search, not a second guess.
NEXUS_SEARCH_CONFIRM_TEXT = os.getenv(
    "NEXUS_SEARCH_CONFIRM_TEXT",
    "می‌خوای برات از اینترنت سرچ کنم؟ اگه آره بگو «آره».",
)
NEXUS_SEARCH_NEVER_CHANGED_TEXT = os.getenv("NEXUS_SEARCH_NEVER_CHANGED_TEXT", "—")
NEXUS_NEVER_CHANGED_TEXT = os.getenv("NEXUS_NEVER_CHANGED_TEXT", "—")
# Asked when a spoken name matches more than one person in the room. The server
# refuses to pick between them — the same rule `people.resolve` follows — and
# instead says who the candidates are so the asker can be specific. `{name}` is
# the spoken name and `{options}` is one line per candidate, each carrying the
# distinguishing detail (a username, or who spoke last) the asker can use.
NEXUS_TARGET_AMBIGUOUS_TEXT = os.getenv(
    "NEXUS_TARGET_AMBIGUOUS_TEXT",
    "چند نفر با اسم «{name}» اینجا هستن؛ منظورت کدومه؟\n{options}",
)
# One candidate line inside the question above. `{name}`, `{username}` and
# `{hint}` (what distinguishes them, e.g. «آخرین پیام رو خودش فرستاد»).
NEXUS_TARGET_CANDIDATE_LINE = os.getenv(
    "NEXUS_TARGET_CANDIDATE_LINE",
    "• {name}{username}{hint}",
)
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

# The transient one has a floor it must not go below, and the reason is
# arithmetic rather than taste. A transient failure is usually a *timeout*: the
# attempt ran the full per-attempt deadline (25s for chat and transcribe, 20s
# for awareness and moderation) before being abandoned. The cooldown is stamped
# when that failure is recorded, so if it is shorter than the deadline the model
# is usable again before the call that just failed could even have finished —
# and the next request walks straight back into it, paying the full timeout
# again. That is exactly what happened on 2026-09-23: with the value at 15s and
# the chat deadline at 25s, every request re-burned the same dead models, the
# attempt budget was spent before the walk reached the one model that answered,
# and six of fourteen replies were dropped with ``reason=attempt_budget``.
#
# 60s is comfortably above the longest deadline any retryable workload has
# (25s), so a model that just timed out stays benched for at least as long as
# the call that failed, and usually for two of them. It stays far below
# ``GEMINI_POOL_MODEL_COOLDOWN`` (120s), because a transient wobble still says
# nothing about the model itself.
GEMINI_POOL_TRANSIENT_COOLDOWN = _int("GEMINI_POOL_TRANSIENT_COOLDOWN", 60)

# How many failures *in a row* take a whole account out of rotation, and for
# how long. The cooldown reuses ``GEMINI_POOL_TRANSIENT_COOLDOWN`` above, so one
# number describes how long the pool waits before re-trusting a credential.
#
# The model cooldown above keeps one *model* out of the walk. It says nothing
# about the account, and the account is the unit Google limits: quotas are per
# project, and one project can be out of allowance on every model it offers
# while another is fine. Measured live on 2026-09-24, four chat accounts carried
# 388-1058 failures each, every one of them still ACTIVE with
# ``cooldown_until=0``, and not a single ``account_failover`` event in the whole
# table — so every message re-walked all four accounts and re-paid for the same
# failures. The breaker below is what ends that.
#
# Three, not one: a single 503 is the provider wobbling, and benching an account
# for it would turn a blip into an outage. Three in a row, across *different*
# models, is the credential's project being the problem. It is deliberately
# above the two failures a single retried model produces, so a retry that
# succeeds on its second attempt never trips it.
GEMINI_POOL_ACCOUNT_FAILURE_THRESHOLD = _int(
    "GEMINI_POOL_ACCOUNT_FAILURE_THRESHOLD", 3
)

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

# A wall-clock ceiling on one logical request for the two text workloads that
# ran without one. ``intent`` got the first ceiling because its caller is a
# group-message handler that must answer in bounded time; these two get one for
# the opposite reason. Their failover walk can legitimately take minutes — the
# provider is *slow* rather than down, so each dead model costs a full timeout
# before the walk moves on — which means the ceiling here is a **safety net
# above the observed worst case**, not a target to trim towards.
#
# The numbers are measurements, not guesses. On 2026-09-23, during the provider
# slowness that left one of eight preferred models answering, one window of the
# live log gave:
#
#   awareness  gemini_ms  n=14  min 11.1s  p50  95.8s  p90 113.6s  max 138.5s
#   chat       gemini_ms  n=20  min  9.1s  p50 121.5s  p90 317.0s  max 383.7s
#
# A ceiling set anywhere near the healthy path (a first-model answer is 9-12s)
# would have cut replies that were going to succeed, and that is a worse outcome
# than a slow reply: it turns "the model was slow" into "the bot said nothing".
# So the defaults sit above the measured maximum with room to spare — 180s for
# awareness and 480s for chat — and they fire only on a walk that is already
# pathological. What they buy is a bound that is explicit and configurable,
# instead of one implied by ``GEMINI_POOL_MAX_ATTEMPTS`` times the per-attempt
# timeout, which moves whenever either of those is changed.
#
# ``0`` still means "no ceiling", so a deployment that wants the old behaviour
# sets it and gets it.
GEMINI_CHAT_TIME_BUDGET_SECONDS = _float(
    "GEMINI_CHAT_TIME_BUDGET_SECONDS", 480.0
)
GEMINI_AWARENESS_TIME_BUDGET_SECONDS = _float(
    "GEMINI_AWARENESS_TIME_BUDGET_SECONDS", 180.0
)

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

# Which workloads spread their requests across the **whole** model list instead
# of preferring the first name until it fails.
#
# The pool has always spread *accounts* this way — ``Pool.ordered_accounts``
# sorts them least-recently-succeeded-first, precisely so that a pool of five
# does not leave four unused — and that is the behaviour an operator sees as "it
# does not use just one". The model list was left in strict preference order, so
# the leading model took every request until the provider rate-limited it and
# only then did the pool move on. Under a per-model daily allowance that means
# the leading model is spent first while the rest of the list sits idle, and the
# workload's real capacity is one model's, not the list's.
#
# Awareness and the conversation are the defaults because both are high-volume
# and both were measured hitting a ceiling. ``gemini_daily`` shows chat spending
# 500/500 and 481/500 of its two accounts on 2026-09-21 — its full allowance —
# and the per-model provider limit is reached *before* the account's when every
# request goes to one model. Awareness is the third case: nobody is waiting on a
# pass, so it can afford to land on a heavier model, which the conversation
# cannot. Remove a workload's name to put it back on strict preference order.
GEMINI_POOL_ROTATE_MODELS = frozenset(
    _str_list(os.getenv("GEMINI_POOL_ROTATE_MODELS", "awareness,chat"))
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

# ── The memory-extraction workload (its own pool, and off without a key) ────
#
# A **seventh** workload, and separate for the same reason awareness is: the
# provider's limits are per project, so a workload that shares a credential
# shares a rate limit no per-workload counter can partition. It also has its own
# breaker, so an extraction outage opens only its own circuit and cannot stop a
# conversation or an awareness pass.
#
# ``ALLOW_SHARED_KEY`` defaults to **False**, unlike chat's and awareness's. The
# point of this workload is isolation; letting it fall back onto the shared pool
# would quietly undo that, so the default is "no credential, no extraction".
# With no ``GEMINI_MEMORY_API_KEY`` the pool has no account, the deterministic
# path is the whole of extraction, and no provider call is ever made.
GEMINI_MEMORY_API_KEY = os.getenv("GEMINI_MEMORY_API_KEY", "").strip()
GEMINI_MEMORY_ALLOW_SHARED_KEY = _bool("GEMINI_MEMORY_ALLOW_SHARED_KEY", False)
GEMINI_MEMORY_MODEL = os.getenv("GEMINI_MEMORY_MODEL", GEMINI_CHAT_MODEL)
GEMINI_MEMORY_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_MEMORY_FALLBACK_MODELS", "")
)
# The API's floor is 10s; the default is comfortably above it.
GEMINI_MEMORY_TIMEOUT_SECONDS = _float("GEMINI_MEMORY_TIMEOUT_SECONDS", 20.0)
# No retries: an extraction is a background nicety, never something a person is
# waiting on, so a failed attempt is simply "no candidate this time".
GEMINI_MEMORY_MAX_RETRIES = _int("GEMINI_MEMORY_MAX_RETRIES", 0)
GEMINI_MEMORY_BACKOFF_SECONDS = _float("GEMINI_MEMORY_BACKOFF_SECONDS", 1.5)
GEMINI_MEMORY_CIRCUIT_FAILURES = _int("GEMINI_MEMORY_CIRCUIT_FAILURES", 5)
GEMINI_MEMORY_CIRCUIT_SECONDS = _float("GEMINI_MEMORY_CIRCUIT_SECONDS", 300.0)
# A hard wall-clock ceiling on one logical request, so a degraded pool cannot
# hold a background task open. Short on purpose: the work is one short sentence
# in, one small JSON out.
GEMINI_MEMORY_TIME_BUDGET_SECONDS = _float(
    "GEMINI_MEMORY_TIME_BUDGET_SECONDS", 30.0
)

# ── Nexus Voice Live ──────────────────────────────────────────────────────
#
# A live voice call: Nexus joins a Telegram voice chat and holds a realtime,
# spoken, bidirectional conversation. It is the same Nexus — the same awareness,
# the same authority model, the same audit trail — reached through a voice
# interface rather than a text one. It is not a second assistant.
#
# The flag defaults to **off**, and that default is the point rather than
# caution for its own sake. A live call is the only thing this bot does that
# holds a socket open for minutes, spends an allowance in a stream rather than
# per request, and joins a channel other people are in. A deployment that has
# not opted in must not be able to reach any of that, so the gate is a
# configuration read on the path itself and not a promise in a comment.
GEMINI_LIVE_ENABLED = _bool("GEMINI_LIVE_ENABLED", False)

# The model preference order, measured rather than assumed. On real Persian
# speech synthesised by this project's own TTS, the time from end of utterance to
# the first audio byte was:
#
#   gemini-3.8-live                 1.12 s   (fa-IR)
#   gemini-3.1-flash-live-preview   1.12 s   (fa-IR)
#   gemini-2.5-flash-native-audio   2.21 s   (auto-detect only)
#
# The purpose-built native-audio model is the obvious choice and it is the wrong
# one: it is twice as slow here and it refuses every explicit Persian language
# code, so it can only be used on auto-detect. The general live model is first
# for that reason, and the flash preview is the fallback because it measured
# identically.
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live").strip()
GEMINI_LIVE_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_LIVE_FALLBACK_MODELS", "gemini-3.1-flash-live-preview")
)

# Its own credential, with the shared pool as an opt-in — the same shape every
# other workload has. A live call is the most expensive thing here by the
# minute, so the default is isolation: an operator who wants calls to draw on
# the shared keys has to say so.
GEMINI_LIVE_API_KEY = os.getenv("GEMINI_LIVE_API_KEY", "").strip()
GEMINI_LIVE_ALLOW_SHARED_KEY = _bool("GEMINI_LIVE_ALLOW_SHARED_KEY", False)

# Per account, like chat's and awareness's, and its own number for the same
# reason those two are separate from each other: a live call must not be able to
# spend the allowance somebody is waiting on a text answer for. The unit is
# *connections*, not minutes — the pool rations requests, and one call is one
# request — so this is a cap on how many calls one credential will start in an
# API day, not on how long they last. Session length is bounded separately, by
# ``GEMINI_LIVE_MAX_SECONDS``.
GEMINI_LIVE_DAILY_LIMIT = _int("GEMINI_LIVE_DAILY_LIMIT", 60)

# How long the provider is given to accept a connection. A live session is
# opened once per call, so this is a connect timeout and not a per-turn one —
# there is no per-turn deadline to set, because the turn is a stream.
GEMINI_LIVE_TIMEOUT_SECONDS = _float("GEMINI_LIVE_TIMEOUT_SECONDS", 30.0)

# Persian, first-class. The native-audio family rejects ``fa-IR`` and accepts
# only auto-detect; the general live models accept it, which is the second
# reason they are preferred. Kept configurable because a deployment serving a
# different language should not have to edit code.
GEMINI_LIVE_LANGUAGE = os.getenv("GEMINI_LIVE_LANGUAGE", "fa-IR").strip()

# The voice the model speaks in. A prebuilt name; an unknown one is refused by
# the provider at setup, which surfaces as ``setup_rejected`` rather than as
# silence.
GEMINI_LIVE_VOICE = os.getenv("GEMINI_LIVE_VOICE", "Puck").strip()

# ── Ceilings ──────────────────────────────────────────────────────────────
# A live call is the one workload that can hold a resource for an unbounded
# time, so every dimension of that is bounded here rather than left to the
# operator to notice.
#
# Concurrent calls. One is the honest default for a single-group deployment: two
# simultaneous calls would mean two provider sessions, two voice channels and
# two allowances spent, and nothing in the feature needs it.
GEMINI_LIVE_MAX_SESSIONS = _int("GEMINI_LIVE_MAX_SESSIONS", 1)

# How long one call may last before it is ended. Not a technical limit — a
# policy one: a session left open holds a provider connection and a voice
# channel, and the failure mode of "it was never closed" is expensive and
# silent. An hour is far longer than a conversation and far shorter than a leak.
GEMINI_LIVE_MAX_SECONDS = _int("GEMINI_LIVE_MAX_SECONDS", 3600)

# How long a call with nobody speaking is kept open. The room being empty and
# the room being quiet are the same thing from here, and both mean nobody is
# waiting for an answer.
GEMINI_LIVE_IDLE_SECONDS = _int("GEMINI_LIVE_IDLE_SECONDS", 180)

# ── Reconnect ─────────────────────────────────────────────────────────────
# A dropped provider session is retried, with backoff, up to this many times.
# The call stays joined to Telegram across the retries: leaving and rejoining a
# voice chat every time a socket hiccups would be far more disruptive than the
# hiccup.
GEMINI_LIVE_RECONNECT_ATTEMPTS = _int("GEMINI_LIVE_RECONNECT_ATTEMPTS", 3)
GEMINI_LIVE_RECONNECT_BACKOFF_SECONDS = _float(
    "GEMINI_LIVE_RECONNECT_BACKOFF_SECONDS", 1.5
)

# ── Barge-in ──────────────────────────────────────────────────────────────
# Whether speech over the top of Nexus stops it. On by default because a
# conversation where you cannot interrupt is not a conversation — but it is a
# switch, because the provider's own detector is what decides, and a deployment
# where it misbehaves needs a way to turn the *reaction* off without turning the
# feature off.
GEMINI_LIVE_BARGE_IN = _bool("GEMINI_LIVE_BARGE_IN", True)

# ── Awareness ─────────────────────────────────────────────────────────────
# How long a context snapshot is reused before it is rebuilt. This is the number
# that makes "refreshable during a call" affordable: a snapshot is assembled
# from the room's own records, and rebuilding it on every utterance would run a
# query per sentence. The *conversation* is continuous regardless — only the
# server-side context is cached.
GEMINI_LIVE_CONTEXT_TTL_SECONDS = _float("GEMINI_LIVE_CONTEXT_TTL_SECONDS", 45.0)

# The ceiling on the context block handed to a live session. Deliberately the
# same shape as the awareness ceiling and a separate number, because a live
# session re-sends its context as part of the session rather than per request,
# and the two budgets should not move together by accident.
GEMINI_LIVE_CONTEXT_CHARS = _int("GEMINI_LIVE_CONTEXT_CHARS", 1500)

# ── Spoken actions ────────────────────────────────────────────────────────
# The model may *ask* for an administrative action; it may never perform one.
# What it produces is a structured request that goes through the same
# authorisation and the same audit trail as every other action in this bot.
#
# Two limits, because a spoken action is the one thing here with a side effect
# in a chat other people are watching. The cooldown stops a model that has
# misheard from repeating the same request in a loop; the per-session ceiling
# stops a long call from becoming an unbounded run of them.
GEMINI_LIVE_ACTION_COOLDOWN_SECONDS = _float(
    "GEMINI_LIVE_ACTION_COOLDOWN_SECONDS", 3.0
)
GEMINI_LIVE_MAX_ACTIONS = _int("GEMINI_LIVE_MAX_ACTIONS", 20)

# ── The deterministic commands ────────────────────────────────────────────
# Bringing Nexus into a voice chat and taking it out are *commands*, not
# requests to a model: they must work with no model, no network and no
# allowance, for the same reason the spoken on/off switch does. So they are
# matched as phrases, before any conversational path is reached, and they are
# owner-only.
#
# They are deliberately phrases rather than a ``/command``: the point is to be
# able to say them out loud, which is what this whole feature is for.
GEMINI_LIVE_JOIN_PHRASES = _str_list(
    os.getenv(
        "GEMINI_LIVE_JOIN_PHRASES",
        "نکسوس برو ویس‌کال,نکسوس برو ویس چت,نکسوس بیا تو ویس,برو ویس‌کال",
    )
)
GEMINI_LIVE_LEAVE_PHRASES = _str_list(
    os.getenv(
        "GEMINI_LIVE_LEAVE_PHRASES",
        "نکسوس بیا بیرون,نکسوس بیا بیرون از ویس,از ویس‌کال بیا بیرون,بیا بیرون از ویس",
    )
)

# How the *layer* is named, for the same purpose ``NEXUS_AWARENESS_NAMES``
# serves for the awareness layer: so a phrase that names voice live is
# recognised as being about this layer and not about the assistant's own
# on/off switch. See ``app/nexus.py``'s vocabulary and §35.15 of AgentMD for
# why the two vocabularies are kept apart.
GEMINI_LIVE_NAMES = _str_list(
    os.getenv("GEMINI_LIVE_NAMES", "ویس‌کال,ویس چت,ویس,voice live,voice")
)

# What the owner is told when one of those commands is used. Every outcome gets
# its own sentence, because the four failures need four different fixes and a
# single "could not" would send the operator looking in the wrong place: the
# feature being switched off is a decision, a missing transport is a
# configuration, a busy group is a state, and a refused join is Telegram.
#
# None of them names a credential, a model or an id.
GEMINI_LIVE_JOINED_TEXT = os.getenv(
    "GEMINI_LIVE_JOINED_TEXT", "نکسوس آمد داخل ویس‌کال و گوش می‌دهد."
).strip()
GEMINI_LIVE_LEFT_TEXT = os.getenv(
    "GEMINI_LIVE_LEFT_TEXT", "نکسوس از ویس‌کال بیرون آمد."
).strip()
GEMINI_LIVE_NOT_IN_CALL_TEXT = os.getenv(
    "GEMINI_LIVE_NOT_IN_CALL_TEXT", "نکسوس الان داخل ویس‌کال نیست."
).strip()
# The feature is off. A decision, not a fault, so it is said plainly rather
# than apologised for.
GEMINI_LIVE_OFF_TEXT = os.getenv(
    "GEMINI_LIVE_OFF_TEXT", "قابلیت ویس‌کال روی این نصب خاموش است."
).strip()
# On, but nothing here can carry a call. The transport reports which of the two
# things is missing and this sentence does not guess at it.
GEMINI_LIVE_UNAVAILABLE_TEXT = os.getenv(
    "GEMINI_LIVE_UNAVAILABLE_TEXT",
    "الان نمی‌توانم داخل ویس‌کال بیایم؛ ورودی ویس‌کال روی این نصب آماده نیست.",
).strip()
GEMINI_LIVE_BUSY_TEXT = os.getenv(
    "GEMINI_LIVE_BUSY_TEXT", "الان یک تماس صوتی در این گروه باز است."
).strip()
GEMINI_LIVE_FAILED_TEXT = os.getenv(
    "GEMINI_LIVE_FAILED_TEXT", "نتوانستم داخل ویس‌کال بیایم."
).strip()
# The feature is on, but the assistant itself is switched off. A call *is* the
# assistant, so a call cannot start while the assistant has been told to stop —
# and the sentence says which of the two switches is in the way, because the
# owner's next action is different for each.
GEMINI_LIVE_NEXUS_OFF_TEXT = os.getenv(
    "GEMINI_LIVE_NEXUS_OFF_TEXT", "نکسوس خاموش است؛ اول روشنش کن."
).strip()

# ── The transport ─────────────────────────────────────────────────────────
# Which implementation carries the call. ``auto`` uses the real one when it is
# usable and reports ``not_configured`` when it is not, which is the honest
# answer on a build or an account that cannot hold a call yet.
#
# The missing piece is a **credential**, not a library: ``ntgcalls 2.2.5``
# publishes ``cp312-manylinux_2_28_x86_64`` wheels, so ``py-tgcalls`` installs
# on this deployment's interpreter. What is absent is the ``api_id``/``api_hash``
# pair and the logged-in MTProto session that joining a voice chat requires. An
# earlier version of this comment said no wheel existed, which was wrong and sent
# the fix in the wrong direction.
#
# ``fake`` is for tests and for a deployment that wants the subsystem's plumbing
# exercised without a voice channel. It is not a way to run this in production:
# a fake transport joins nothing, the session refuses to hold a production call
# on one, and the feature says so rather than pretending.
GEMINI_LIVE_TRANSPORT = os.getenv("GEMINI_LIVE_TRANSPORT", "auto").strip().lower()

# The MTProto credentials the real transport needs. A *user* session, not the
# bot token: the Bot API has no method to join a voice chat at all — verified
# against all 277 public ``Bot`` methods — so this feature cannot be built on the
# bot's own credentials and must not pretend to be. Empty by default, and the
# transport reports ``not_configured`` until both are set.
TELEGRAM_API_ID = _int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
# Where the MTProto session is stored. Inside the data volume, like the key
# store, so it survives a container rebuild — and deliberately a separate file,
# because it is a login and not a setting.
GEMINI_LIVE_SESSION_PATH = os.getenv(
    "GEMINI_LIVE_SESSION_PATH", "/data/voice_live.session"
)


# ── Voice Context ─────────────────────────────────────────────────────────
#
# A Telegram **voice message** answered by Nexus as a spoken turn, with the
# same context a text message gets. It is not the live call above, and the
# difference is the whole design:
#
#   * the call joins a voice channel and holds a socket open for minutes;
#     Voice Context is one turn — one voice note in, one voice note out;
#   * the call hands the model a microphone; Voice Context hands it the
#     server's own assembled context — identity, reply and target, memory,
#     awareness, conversation state, the date — exactly the blocks the text
#     path composes, and *then* the audio.
#
# The two share one thing deliberately: the provider's Live API, reached
# through the same transport. What they do not share is an allowance, a
# breaker, a timeout or a failure domain, which is why this is a workload of
# its own rather than a corner of ``live_voice`` — a busy afternoon of voice
# notes must not exhaust the allowance a call is waiting on, and the other way
# round.
#
# The flag defaults **on**, like the search workload's and unlike the call's.
# The reason is the same in both directions: nothing here joins a channel or
# holds a resource open, and the credential is what makes the default safe. A
# deployment with no live credential has no ``voice_context`` account, the
# feature reports itself inert, and a voice message takes exactly the path it
# took before this existed — transcribed and answered in text.
VOICE_CONTEXT_ENABLED = _bool("VOICE_CONTEXT_ENABLED", True)

# Its own credential when one is set, and the live call's otherwise — because
# it is literally the same provider capability, and an operator who has already
# provisioned a Live key should not have to provision a second one to try
# this. The isolation that matters is not the key here but the *workload*: the
# two draw on separate daily allowances and separate breakers even when they
# point at one credential, which is what stops one spending the other's day.
VOICE_CONTEXT_API_KEY = (
    os.getenv("VOICE_CONTEXT_API_KEY", "").strip() or GEMINI_LIVE_API_KEY
)
VOICE_CONTEXT_ALLOW_SHARED_KEY = _bool("VOICE_CONTEXT_ALLOW_SHARED_KEY", False)

# The model and the voice. The same measured preference order as the call —
# see the block above for the numbers — and a separate setting because a
# deployment may want the cheaper model for a one-shot turn and the faster one
# for a live conversation, or the other way round.
VOICE_CONTEXT_MODEL = os.getenv("VOICE_CONTEXT_MODEL", GEMINI_LIVE_MODEL).strip()
VOICE_CONTEXT_FALLBACK_MODELS = _str_list(
    os.getenv(
        "VOICE_CONTEXT_FALLBACK_MODELS", ",".join(GEMINI_LIVE_FALLBACK_MODELS)
    )
)
# Persian, first-class, and its own setting for the same reason the model is.
VOICE_CONTEXT_LANGUAGE = os.getenv(
    "VOICE_CONTEXT_LANGUAGE", GEMINI_LIVE_LANGUAGE
).strip()
VOICE_CONTEXT_VOICE = os.getenv("VOICE_CONTEXT_VOICE", GEMINI_LIVE_VOICE).strip()

# ── Ceilings ──────────────────────────────────────────────────────────────
# How many voice notes one credential will answer in an API day. Per account,
# and its own number: this is the busiest of the two Live workloads by a wide
# margin — a voice note is a normal thing to send and a call is not — so it
# needs a budget sized for chat rather than for calls.
VOICE_CONTEXT_DAILY_LIMIT = _int("VOICE_CONTEXT_DAILY_LIMIT", 300)

# How many voice turns may be in flight at once. Two is the honest default for
# a single-group deployment: it lets a second person be served while the first
# is being answered, and it bounds the provider connections and the memory one
# busy moment can hold. The text queue's own gate is separate and untouched —
# a voice turn never waits on it and never holds it.
VOICE_CONTEXT_MAX_CONCURRENCY = _int("VOICE_CONTEXT_MAX_CONCURRENCY", 2)

# How long the provider is given to accept the connection. The same shape as
# the call's connect timeout, and separate so the two can move independently.
VOICE_CONTEXT_CONNECT_TIMEOUT_SECONDS = _float(
    "VOICE_CONTEXT_CONNECT_TIMEOUT_SECONDS", 30.0
)

# How long one whole turn may take, from connect to the last audio byte. This
# is the number that stops a wedged provider from holding a voice turn open
# for ever — the failure the call's ``MAX_SECONDS`` bounds for a session, at
# the scale of one turn. Generous, because a genuine "explain it fully" answer
# is a long piece of speech; it is a net, not the expected duration.
#
# **It must exceed the connect timeout plus the reply ceiling**, or the reply
# is cut by the deadline before it can ever reach its own bound — which is
# exactly the defect this default was raised for: the turn's own timeout was
# below the reply ceiling, so a long spoken answer was truncated by the clock
# and still reported as complete. ``voice_context.answer`` now derives a floor
# of ``connect + reply + 20s`` so the invariant holds even when an operator
# raises only the reply ceiling.
VOICE_CONTEXT_TURN_TIMEOUT_SECONDS = _float(
    "VOICE_CONTEXT_TURN_TIMEOUT_SECONDS", 200.0
)

# The reply's own ceiling, in seconds of speech. A model that decides to
# lecture is cut off here rather than sent as a five-minute voice note. High
# enough that a real full answer never meets it, low enough that a runaway
# generation cannot become a file nobody will listen to. The owner's own
# instruction is that a long answer is fine: the length follows the request,
# exactly as it does for a typed message, and being spoken is never a reason to
# say less.
VOICE_CONTEXT_MAX_REPLY_SECONDS = _float(
    "VOICE_CONTEXT_MAX_REPLY_SECONDS", 150.0
)

# ── The input ─────────────────────────────────────────────────────────────
# What will be accepted as a voice note. The same bounds as the transcription
# workload's and deliberately its own numbers: this path spends two provider
# calls per message rather than one, so the point at which it stops being worth
# answering is its own decision. A clip outside the bounds is not refused with
# a sentence — it simply takes the ordinary text path.
VOICE_CONTEXT_MAX_SECONDS = _float("VOICE_CONTEXT_MAX_SECONDS", 300.0)
VOICE_CONTEXT_MAX_MB = _float("VOICE_CONTEXT_MAX_MB", 18.0)

# Whether the model is given the audio as well as the server's transcript of
# it. On by default, because that is the feature: the transcript grounds the
# words and the audio carries how they were said. Off is the cheap path — one
# text turn against the same context — and it exists so a deployment that finds
# the audio unnecessary can say so without a code change.
VOICE_CONTEXT_SEND_AUDIO = _bool("VOICE_CONTEXT_SEND_AUDIO", True)

# How much silence is pushed after the utterance to let the provider's own
# voice-activity detector find the end of it. This is not a courtesy: the
# detector looks for the end of speech in the silence that follows it, and a
# caller that sends only the utterance gets no answer at all — measured, and
# the reason the call's silence pump exists. 1200 ms is comfortably past the
# detector's own threshold without adding a noticeable wait.
VOICE_CONTEXT_SILENCE_MS = _int("VOICE_CONTEXT_SILENCE_MS", 1200)

# ── Retry ─────────────────────────────────────────────────────────────────
# A one-shot turn is retried as a whole, and the audio is cheap to re-send —
# measured at far faster than real time, so a retry costs a connection and not
# a minute. Two attempts: enough to ride out the provider's ordinary weather,
# few enough that a rejected configuration does not loop (a non-retryable
# failure is not retried at all, which is decided by the transport's own
# taxonomy).
VOICE_CONTEXT_MAX_ATTEMPTS = _int("VOICE_CONTEXT_MAX_ATTEMPTS", 2)
VOICE_CONTEXT_RETRY_BACKOFF_SECONDS = _float(
    "VOICE_CONTEXT_RETRY_BACKOFF_SECONDS", 0.8
)

# ── The deterministic commands ────────────────────────────────────────────
# Switching Voice Context is a *command*, not a request to a model: like the
# other switches it has to work with no model, no network and no allowance, and
# it is owner-only. So it is matched as a phrase, before any conversational
# path is reached.
#
# How the layer is named, for the same purpose ``NEXUS_AWARENESS_NAMES`` serves
# for awareness: so a phrase about Voice Context is recognised as being about
# this layer and not about the assistant's own switch. Deliberately *not* the
# bare word «voice» — that is in ``GEMINI_LIVE_NAMES`` and belongs to the call,
# and a name that two layers answer to is a name that moves the wrong switch.
VOICE_CONTEXT_NAMES = _str_list(
    os.getenv(
        "VOICE_CONTEXT_NAMES",
        "voice context,voice-context,voicecontext,ویس کانتکست,ویس‌کانتکست,"
        "کانتکست صوتی,ویس هوشمند",
    )
)

# The two directions, as extra vocabulary. The shared switch words — «روشن»,
# «خاموش», «فعال شو» — already move this switch through ``nexus.command_from``
# because the layer is named; these are the words that are only unambiguous
# *because* the layer is named, and «باز»/«بسته» are the two the owner actually
# says. They are not added to the shared list: «باز» is one of the commonest
# words in Persian, and a shared vocabulary that read it as "turn on" would
# make «نکسوس باز خراب شد» a command.
#
# The bare English «on»/«off» are here for the same reason and carry the same
# condition: the shared list deliberately leaves them out because they are
# ambiguous alone (that is why it has «turn on» and «turn off»), but «voice
# context off» names the layer and so the direction is not in doubt. The name is
# checked inside ``voice_context.command_from``, not left to the caller, so this
# vocabulary can never be read on a sentence that does not name the layer.
VOICE_CONTEXT_ON_PHRASES = _str_list(
    os.getenv("VOICE_CONTEXT_ON_PHRASES", "باز,باز کن,بازش کن,بازش,on")
)
VOICE_CONTEXT_OFF_PHRASES = _str_list(
    os.getenv("VOICE_CONTEXT_OFF_PHRASES", "بسته,بسته کن,ببند,بستن,off")
)

# ── What the owner is told ────────────────────────────────────────────────
VOICE_CONTEXT_ON_LABEL = os.getenv("VOICE_CONTEXT_ON_LABEL", "فعال")
VOICE_CONTEXT_OFF_LABEL = os.getenv("VOICE_CONTEXT_OFF_LABEL", "غیرفعال")
# Separate sentences for the two directions, and neither says Nexus is off: an
# owner who read a bare «خاموش شد» after switching Voice Context off would
# reasonably conclude the assistant had stopped answering. The wording names
# the layer and says what still works.
VOICE_CONTEXT_ON_DONE_TEXT = os.getenv(
    "VOICE_CONTEXT_ON_DONE_TEXT",
    "🎙 کانتکست صوتی روشن شد. از این به بعد ویس‌ها رو با کل کانتکست "
    "(حافظه، آگاهی، گفتگو) جواب می‌دم و جواب هم صوتیه.",
)
VOICE_CONTEXT_OFF_DONE_TEXT = os.getenv(
    "VOICE_CONTEXT_OFF_DONE_TEXT",
    "🎙 کانتکست صوتی خاموش شد. از این به بعد ویس‌ها مثل قبل متن می‌شن و "
    "جواب متنی می‌گیرن. چت و آگاهی دست‌نخورده‌اند.",
)
VOICE_CONTEXT_ALREADY_TEXT = os.getenv(
    "VOICE_CONTEXT_ALREADY_TEXT", "کانتکست صوتی از قبل {state} بود."
)
# Said when the owner asks for the layer back and the *deployment* has it off.
# The spoken switch and the deploy-time setting are two halves of one answer,
# and «کانتکست صوتی روشن» can only move one of them — the row is stored, the
# workload still does not run, and the fix is a restart only the operator can do.
VOICE_CONTEXT_CONFIG_OFF_TEXT = os.getenv(
    "VOICE_CONTEXT_CONFIG_OFF_TEXT",
    "⚠️ کانتکست صوتی توی تنظیمات این ربات خاموش شده، پس با پیام روشن نمی‌شه. "
    "برای روشن کردنش باید VOICE_CONTEXT_ENABLED=true باشه و ربات ری‌استارت بشه.",
)


# ── Web Search ────────────────────────────────────────────────────────────
#
# A workload that answers a question from the live web, using the provider's own
# Google Search grounding. It is a **separate workload**, and that is the whole
# design rather than a detail of it.
#
# Grounding runs *inside* a Gemini request — ``tools=[Tool(google_search=...)]``
# — so the obvious shortcut is to switch it on for the conversational call and
# get search for free. That shortcut is refused, because it would make every
# grounded answer spend the conversation's credential and the conversation's
# daily allowance: a busy afternoon of factual questions would exhaust the
# budget a person is waiting on a reply to, and the two would share one circuit
# breaker. So the grounding request is made by ``app/web_search.py`` on this
# workload's own credential, its own allowance, its own breaker and its own
# timeout, and what crosses back into the conversation is *data* — a bounded,
# delimited block of findings the assistant is told to treat as untrusted
# reference material, never as instructions.
#
# The switch defaults on, because the point of the feature is that Nexus checks
# the web by default rather than only when told to. What makes that safe is the
# credential: with no ``GEMINI_SEARCH_API_KEY`` (and no opt-in to the shared
# pool) the workload has no account, reports itself inert, and the assistant
# answers exactly as it did before this existed. Same fail-closed shape as
# awareness.
GEMINI_SEARCH_ENABLED = _bool("GEMINI_SEARCH_ENABLED", True)

# Its own credential, and the shared pool is opt-in — the same shape every other
# workload has. Isolation is the default because grounding is the one workload
# whose provider-side quota (grounding requests per day) is separate from
# generateContent, and a shared key would merge the two projects' limits.
GEMINI_SEARCH_API_KEY = os.getenv("GEMINI_SEARCH_API_KEY", "").strip()
GEMINI_SEARCH_ALLOW_SHARED_KEY = _bool("GEMINI_SEARCH_ALLOW_SHARED_KEY", False)

# Which provider answers a search: ``gemini`` (Google Search grounding, the
# original) or ``tavily``. The default is the existing provider, so a deployment
# that sets nothing behaves exactly as before this existed. Exactly one provider
# is active — there is deliberately **no** automatic cross-provider fallback,
# because a fallback would make one question spend two requests and would let a
# failure on one provider quietly draw on the other's allowance.
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "gemini").strip()

# Tavily's own credential, kept separate from every Gemini workload so that its
# quota and its failures are its own. Empty means Tavily search is inert and the
# assistant is unchanged. Read from the environment at call time, never logged,
# never in a status and never in an exception.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

# Its own model preference, defaulting to the conversation's family because the
# job has the same shape (read text, reason, write text) — but a *separate*
# setting, so it can be moved without touching the assistant. Every model in the
# default list supports Google Search grounding.
GEMINI_SEARCH_MODEL = os.getenv("GEMINI_SEARCH_MODEL", GEMINI_CHAT_MODEL).strip()
GEMINI_SEARCH_FALLBACK_MODELS = _str_list(
    os.getenv("GEMINI_SEARCH_FALLBACK_MODELS", "")
) or GEMINI_CHAT_FALLBACK_MODELS

# A tighter deadline than the conversation's. Nobody is waiting on the search
# itself — they are waiting on the *answer*, and a search that runs long has to
# give the answer its turn. When this expires the reply is written without web
# findings and says so, which is the honest failure and a bounded one.
GEMINI_SEARCH_TIMEOUT_SECONDS = _float("GEMINI_SEARCH_TIMEOUT_SECONDS", 15.0)
GEMINI_SEARCH_MAX_RETRIES = _int("GEMINI_SEARCH_MAX_RETRIES", 1)
GEMINI_SEARCH_BACKOFF_SECONDS = _float("GEMINI_SEARCH_BACKOFF_SECONDS", 1.5)

# Its own breaker, like every other workload's: a search outage opens the search
# circuit and nothing else. The conversation keeps answering from what it knows.
GEMINI_SEARCH_CIRCUIT_FAILURES = _int("GEMINI_SEARCH_CIRCUIT_FAILURES", 5)
GEMINI_SEARCH_CIRCUIT_SECONDS = _float("GEMINI_SEARCH_CIRCUIT_SECONDS", 300.0)

# Its own sliding window, on top of the pool's provider-side accounting.
GEMINI_SEARCH_RATE_LIMIT = _int("GEMINI_SEARCH_RATE_LIMIT", 8)
GEMINI_SEARCH_RATE_WINDOW = _float("GEMINI_SEARCH_RATE_WINDOW", 60.0)

# Per account, like chat's and awareness's, and deliberately its own number: a
# factual question must not be able to spend the allowance a reply is waiting on
# — nor the other way round.
GEMINI_SEARCH_DAILY_LIMIT = _int("GEMINI_SEARCH_DAILY_LIMIT", 150)

# Bounds on what is kept. ``MAX_RESULTS`` caps how many sources are surfaced;
# ``MAX_CHARS`` caps the findings block that enters the prompt.
#
# Sized for the answer the owner asked for, not for a summary: "bring me the
# news" is answered in full, and a full answer needs more material than a
# five-snippet digest. The lever is deliberately the *findings*, never a second
# request — one Tavily call returns all of these results, so the rationed
# resource (requests, per the cost ceiling) is untouched and only the tokens of
# one turn move. 8 results at up to 600 characters each is ~4800 characters
# before the block cap trims it to 3600 (~1k tokens), which fits the raised
# output budget with room to spare.
GEMINI_SEARCH_MAX_RESULTS = _int("GEMINI_SEARCH_MAX_RESULTS", 8)
GEMINI_SEARCH_MAX_CHARS = _int("GEMINI_SEARCH_MAX_CHARS", 3600)
# How much of one result's snippet is kept. The provider returns a content chunk;
# this is what makes a result worth more than a headline.
GEMINI_SEARCH_SNIPPET_CHARS = _int("GEMINI_SEARCH_SNIPPET_CHARS", 600)
# How much of the question is sent. A question longer than this is truncated
# rather than refused, the same way the conversation truncates its own input.
GEMINI_SEARCH_QUERY_CHARS = _int("GEMINI_SEARCH_QUERY_CHARS", 600)
# How much recent conversation is handed to the search call so a follow-up
# («و قیمتش؟») is searched in context. Deliberately small: it is other people's
# text, and it is sent to a provider.
GEMINI_SEARCH_MAX_HISTORY_CHARS = _int("GEMINI_SEARCH_MAX_HISTORY_CHARS", 600)

# The server-authored note that goes into the prompt when a search was warranted
# and did not return usable results. English because it is an instruction to the
# model, not a sentence for a person; the model is what says the honest thing to
# the person, in their language.
GEMINI_SEARCH_UNAVAILABLE_NOTE = os.getenv(
    "GEMINI_SEARCH_UNAVAILABLE_NOTE",
    "A live web search was attempted for this question and returned no usable "
    "results. Do not claim to have current or live information. If the question "
    "depends on current information, say plainly that you could not check the "
    "web just now.",
).strip()


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
        # The only workload with a wall-clock ceiling on one logical request,
        # because it is the only one whose caller is a group message handler
        # that must answer in bounded time: the pool's failover walk can
        # otherwise spend minutes on one ambiguous message. See
        # ``GEMINI_INTENT_TIME_BUDGET_SECONDS``.
        "time_budget": GEMINI_INTENT_TIME_BUDGET_SECONDS,
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
        # A safety net, not a target: a person is waiting on this reply, and a
        # reply that arrives slowly still beats one that never arrives. See
        # ``GEMINI_CHAT_TIME_BUDGET_SECONDS`` for why the number is where it is.
        "time_budget": GEMINI_CHAT_TIME_BUDGET_SECONDS,
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
        # Text only. The workload used to send images and extracted video
        # frames, and required models that accepted both; with the media
        # pipeline removed, requiring an image capability would only shrink the
        # set of models a text classification can use.
        "capabilities": frozenset({"text"}),
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
        # The sweep awaits this, so an unbounded walk holds the whole sweep. The
        # ceiling is still above the measured worst case rather than below it —
        # cutting a pass short would lose the observation, not just the time.
        # See ``GEMINI_AWARENESS_TIME_BUDGET_SECONDS``.
        "time_budget": GEMINI_AWARENESS_TIME_BUDGET_SECONDS,
    },
    {
        # Automatic memory extraction's own workload. It is a *seventh* pool
        # rather than a corner of chat's or awareness's, because the thing being
        # protected is the isolation: the provider's limits are per project, so a
        # shared credential means a shared limit, and an extraction must never be
        # able to spend the request a person is waiting on an answer to — nor the
        # other way round.
        #
        # It has no shared-pool fallback (see ``GEMINI_MEMORY_ALLOW_SHARED_KEY``),
        # so with no credential of its own it has no accounts and the pool is
        # disabled. That is the default state: extraction is deterministic-only
        # and costs no provider call at all.
        "workload": "memory",
        "keys": _pool_key_list(
            GEMINI_MEMORY_API_KEY,
            "GEMINI_MEMORY_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_MEMORY_ALLOW_SHARED_KEY,
        ),
        "models": _models(GEMINI_MEMORY_MODEL, GEMINI_MEMORY_FALLBACK_MODELS),
        # Text in, a small JSON object out — the same capability every text
        # workload needs, and nothing more.
        "capabilities": frozenset({"text"}),
        "allow_experimental": False,
        "retries": GEMINI_MEMORY_MAX_RETRIES,
        "backoff": GEMINI_MEMORY_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_MEMORY_TIMEOUT_SECONDS),
        # Per account, like chat's and awareness's, and its own number: an
        # extraction must not be able to spend either of the other two.
        "daily_budget": max(1, NEXUS_MEMORY_MODEL_DAILY_LIMIT),
        # The caller is a background task, but a degraded pool must still not
        # hold it open indefinitely.
        "time_budget": GEMINI_MEMORY_TIME_BUDGET_SECONDS,
    },
    {
        # The live voice call. Its own workload, for the same reason awareness
        # has one: a call spends a connection and holds it for minutes, and it
        # must not be able to spend the allowance a text answer is waiting on —
        # nor the other way round.
        "workload": "live_voice",
        "keys": _pool_key_list(
            GEMINI_LIVE_API_KEY,
            "GEMINI_LIVE_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_LIVE_ALLOW_SHARED_KEY,
        ),
        "models": _models(GEMINI_LIVE_MODEL, GEMINI_LIVE_FALLBACK_MODELS),
        # ``live`` is the gate, not a modality: it is what keeps these
        # streaming-only models out of every other workload's candidate set, and
        # what keeps a model that cannot stream out of this one. ``audio_out`` is
        # required as well, so a live *transcription* model — which can listen
        # and cannot speak — is never selected for a conversation.
        "capabilities": frozenset({"audio_in", "audio_out", "live"}),
        # The live models are previews and previews are reachable only when an
        # operator names one, which is exactly what the two settings above do.
        "allow_experimental": True,
        # No retries at the pool level. A call is a stream, not a request: there
        # is no partial answer to fail over from, and a second attempt after a
        # failure is the session's own reconnect logic — which knows whether the
        # session was ever established and can resume rather than restart.
        "retries": 0,
        "backoff": 0.0,
        "timeout": _deadline(GEMINI_LIVE_TIMEOUT_SECONDS),
        # Per account. One call is one request, so this caps how many calls one
        # credential will start in an API day; how long each may last is bounded
        # by ``GEMINI_LIVE_MAX_SECONDS``.
        "daily_budget": max(1, GEMINI_LIVE_DAILY_LIMIT),
    },
    {
        # Voice Context: a Telegram voice message answered as a spoken turn.
        #
        # Its own workload beside ``live_voice`` even though the two share a
        # credential by default, because the *shape* of what they spend is
        # different in every dimension that matters. A call is one connection
        # held for minutes and is rationed by how many a day an account may
        # open; a voice note is a short request-shaped turn, sent by ordinary
        # members, and is rationed by how many an account may answer. Sharing
        # one budget would let a busy room of voice notes spend the day a call
        # was waiting on — and, with one breaker, one bad afternoon would stop
        # both. The credential may be shared; the accounting is not.
        "workload": "voice_context",
        "keys": _pool_key_list(
            VOICE_CONTEXT_API_KEY,
            "VOICE_CONTEXT_API_KEY",
            SHARED_POOL_KEYS,
            VOICE_CONTEXT_ALLOW_SHARED_KEY,
        ),
        "models": _models(
            VOICE_CONTEXT_MODEL, VOICE_CONTEXT_FALLBACK_MODELS
        ),
        # The same three as the call, and for the same reason: ``live`` is the
        # gate that keeps these streaming-only models out of every other
        # workload, and ``audio_out`` is what keeps a listen-only live model
        # from being selected to answer.
        "capabilities": frozenset({"audio_in", "audio_out", "live"}),
        "allow_experimental": True,
        # Retried by the turn itself, which knows whether the failure was
        # retryable and re-sends the audio as a whole — there is no partial
        # answer for the pool to fail over from.
        "retries": 0,
        "backoff": 0.0,
        "timeout": _deadline(VOICE_CONTEXT_CONNECT_TIMEOUT_SECONDS),
        "daily_budget": max(1, VOICE_CONTEXT_DAILY_LIMIT),
    },
    {
        # The live web, as its own workload. Its own credential, model
        # preference, timeout, retries, breaker and daily allowance — see the
        # Web Search section above for why the grounding call is not simply made
        # on the conversation's workload. ``capabilities`` is text-only: the
        # search call is a text question in and a grounded text answer out, and
        # the grounding tool is provider-side, not a modality this table models.
        "workload": "search",
        "keys": _pool_key_list(
            GEMINI_SEARCH_API_KEY,
            "GEMINI_SEARCH_API_KEY",
            SHARED_POOL_KEYS,
            GEMINI_SEARCH_ALLOW_SHARED_KEY,
        ),
        "models": _models(GEMINI_SEARCH_MODEL, GEMINI_SEARCH_FALLBACK_MODELS),
        "capabilities": frozenset({"text"}),
        "allow_experimental": False,
        "retries": GEMINI_SEARCH_MAX_RETRIES,
        "backoff": GEMINI_SEARCH_BACKOFF_SECONDS,
        "timeout": _deadline(GEMINI_SEARCH_TIMEOUT_SECONDS),
        # Per account, and its own number, for the same reason awareness's is:
        # a factual question must not be able to spend the allowance a reply is
        # waiting on. Deliberately **no** ``time_budget`` — the wall-clock
        # ceiling is the conversation-handler's and stays exactly one workload's
        # (see ``test_only_the_intent_workload_has_a_wall_clock_ceiling``).
        "daily_budget": max(1, GEMINI_SEARCH_DAILY_LIMIT),
    },
]

# ── Credentials the owner may manage from Telegram ────────────────────────
#
# The pool reads the environment at boot. These settings are for the other
# direction: giving one workload a new key without editing `.env` and
# restarting. `app/key_store.py` holds the store itself; what lives here is
# where it goes and who is allowed to change it.
#
# The path is inside the data volume on purpose. `docker-compose.yml` mounts
# `./data` at `/data`, and that volume is what survives a container rebuild —
# a store anywhere else would lose every runtime credential the first time the
# image was rebuilt, which is precisely the failure this feature exists to
# avoid. It is a separate file from the database so that a database copy taken
# for support reasons is not also a keyring.
GEMINI_KEY_STORE_PATH = os.getenv(
    "GEMINI_KEY_STORE_PATH", "/data/gemini_keys.json"
)

# Which workloads may have credentials added or removed from Telegram. The
# three whose keys the owner is actually rotating, and no others.
#
# This is a closed set rather than a setting. `moderation`, `transcribe` and
# `tts` are deliberately absent: they are reachable read-only in the dashboard
# so nothing is hidden, but a callback payload that names one of them is
# refused by `key_store.is_managed` regardless of who sent it. An environment
# variable here would let a typo widen the set, and the set is the whole of the
# write surface.
GEMINI_KEY_MANAGED_WORKLOADS = frozenset({"chat", "awareness", "intent"})

# How long a "send me the key now" prompt stays armed. Long enough to switch
# apps and copy the key, short enough that a prompt the owner walked away from
# is not still waiting to swallow the next thing they type.
GEMINI_KEY_ADD_TTL_SECONDS = _int("GEMINI_KEY_ADD_TTL_SECONDS", 300)

# The deadline for the one verification call made before a key is stored. It
# lists models rather than generating anything, so it costs no generation quota
# — but it is still a network round trip, and this is the bound on it.
GEMINI_KEY_PROBE_TIMEOUT_SECONDS = _float(
    "GEMINI_KEY_PROBE_TIMEOUT_SECONDS", 15.0
)

# How many runtime credentials one workload may hold. A ceiling rather than a
# target: it stops a loop that adds a key per request from growing the store
# without bound, and it is well above what a deployment needs.
GEMINI_KEY_MAX_PER_WORKLOAD = _int("GEMINI_KEY_MAX_PER_WORKLOAD", 10)

# The owner's command. Named for what it manages rather than for the provider,
# so it reads the same way `/pool` does.
GEMINI_KEYS_COMMAND = os.getenv("GEMINI_KEYS_COMMAND", "keys").strip().lstrip("/")

# ── The admin dashboard (a separate process, not the bot) ─────────────────
#
# The dashboard is its own compose service running `python -m app.web` from
# this same image, sharing the same SQLite volume. Its settings live here so
# there is one config source, but nothing in the bot's runtime reads them.
#
# Its identity is deliberately **separate from Telegram membership**: being an
# administrator of a Telegram group does not make anybody a dashboard
# administrator, and the dashboard never trusts a role, a group scope or an
# owner claim supplied by the client. See AgentMD §54.24 and app/web/auth.py.
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "owner").strip() or "owner"
# One of these two is the password. The hash wins when both are set, so a
# password changed from the panel/CLI is not silently overridden by a stale
# `.env` value. Neither is ever logged, returned, or rendered.
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DASHBOARD_PASSWORD_HASH = os.getenv("DASHBOARD_PASSWORD_HASH", "")
# The cookie-signing key. Unset means a random one per process: sessions then do
# not survive a restart (a warning is logged) — never a silent insecure default.
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")
# Bound to loopback by default, like the VPN bot's internal API: the dashboard
# is reached through nginx/TLS, never straight from the internet.
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8100)
# 12 hours, and the cookie is re-minted on every login (session rotation).
DASHBOARD_SESSION_SECONDS = _int("DASHBOARD_SESSION_SECONDS", 43200)
# Login brute-force brake: N failures per window, per client address.
DASHBOARD_LOGIN_MAX_FAILURES = _int("DASHBOARD_LOGIN_MAX_FAILURES", 5)
DASHBOARD_LOGIN_WINDOW_SECONDS = _int("DASHBOARD_LOGIN_WINDOW_SECONDS", 900)
# Set the Secure cookie flag on the session cookie. There is no inference: with
# the default (off) the cookie is also sent over plain HTTP, which is only
# correct while the panel is reachable solely over loopback. Turn it on as soon
# as TLS terminates in front of it (ops/nginx-dashboard.conf.example).
DASHBOARD_SECURE_COOKIES = _bool("DASHBOARD_SECURE_COOKIES", False)
# Where a password set from the panel is written. A file, not a row: the SQLite
# database is backed up, copied to a laptop and attached to bug reports, and the
# panel's credential should not travel with it. It lives under the mounted
# volume so it survives a container replacement, and is written mode 600.
DASHBOARD_CREDENTIALS_PATH = (
    os.getenv("DASHBOARD_CREDENTIALS_PATH", "").strip()
    or "/data/dashboard_credentials.json"
)
# The Telegram identity the panel operator is bound to.
#
# This is the whole of the panel's authority model, so it is worth stating
# plainly: the dashboard authorizes **this one id** and nobody else, and the id
# comes from configuration only — never from the `admins` table, never from
# CONFIG_ADMINS, and never from a request. A Telegram group administrator is
# therefore **not** a dashboard administrator; being promoted in a chat grants
# nothing here. See AgentMD §53.13.
#
# It defaults to the owner because the owner is the only identity guaranteed to
# hold every permission, and it fails closed: if this is 0 (no owner configured
# and no override), `rbac.authorize` answers `no_owner` and every protected page
# is refused.
DASHBOARD_OPERATOR_ID = _int("DASHBOARD_OPERATOR_ID", OWNER_USER_ID)
# How long the panel's own audit trail is kept. The panel writes its events to
# `dashboard_audit` — its own table, deliberately separate from `admin_audit`, so
# dashboard logins do not appear in the bot's audit view and the bot's behaviour
# is unchanged. Pruned by the dashboard process on its own writes.
DASHBOARD_AUDIT_RETENTION_SECONDS = _int(
    "DASHBOARD_AUDIT_RETENTION_SECONDS", 90 * 86400
)
