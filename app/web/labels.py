"""The panel's status vocabulary: one place that turns a machine value into a
Persian label and a colour.

This exists for the same reason ``app/web/copy.py`` exists: a page must not
invent its own wording for a status. Every mapping here is a *closed* vocabulary
that the bot already defines — the workload names in ``app/config.py`` and the
pool event kinds in ``app/gemini_pool.py`` — copied here rather than imported,
because importing ``app/gemini_pool`` into the panel would build the pools from
the panel's own process (see ``app/web/queries.py``).

The fallback is deliberate and is not a bug: an unknown value returns itself,
untouched, with the neutral ``muted`` colour. A vocabulary that grows in the bot
shows up on the panel as the raw token until this file is taught the word for it,
which is visible and honest, rather than being swallowed by a default that
claims to know.

Colour here is semantic and maps onto the stylesheet's tokens (``positive``,
``warn``, ``danger``, ``info``, and neutral). It is never decorative.

Only what the pages that exist actually use is here. The account-state labels,
for instance, are not: the Overview shows *counts* by state, not per-account
states, so a state vocabulary would be dead weight until the page that shows one
account arrives.
"""

# Workload name -> label. The nine names are the ones ``config.GEMINI_POOLS``
# builds, and the four with their own ``*_usage`` table are the ones the Overview
# can also count requests for.
WORKLOADS: dict[str, str] = {
    "intent": "تشخیص نیت",
    "chat": "گفتگو",
    "moderation": "بررسی پیام",
    "transcribe": "تبدیل صدا به متن",
    "tts": "ساخت صدا",
    "awareness": "آگاهی از گروه",
    "memory": "حافظه",
    "live_voice": "گفتگوی زنده",
    "search": "جست‌وجوی وب",
}

# Pool event ``kind`` -> (label, css). The keys are exactly the string literals
# passed to ``Pool.record`` in ``app/gemini_pool.py``. ``danger`` is reserved for
# events that mean the pool could not serve a request; a failover that worked is
# ``info``, because it is the mechanism doing its job, not a fault.
EVENTS: dict[str, tuple[str, str]] = {
    "models_cooling": ("همه‌ی مدل‌ها در کول‌داون", "warn"),
    "no_compatible_model": ("مدل سازگاری پیدا نشد", "danger"),
    "account_recovered": ("حساب برگشت به چرخه", "positive"),
    "time_budget": ("سقف زمانی یه درخواست پر شد", "warn"),
    "model_failover": ("مدل عوض شد", "info"),
    "account_failover": ("حساب عوض شد", "info"),
    "pool_empty": ("هیچ حساب قابل‌استفاده‌ای نموند", "danger"),
    "pool_critical": ("فقط یه حساب قابل‌استفاده مونده", "warn"),
    "bench_withheld": ("کنارگذاری حساب متوقف شد", "muted"),
}

# The "it did its job" outcome for each workload's usage table. Different
# workloads call a good outcome different things — a chat turn that produced a
# reply, a moderation call that flagged a message, a transcription that yielded
# text — so the column cannot have one word. The row carries its own.
USAGE_POSITIVE: dict[str, str] = {
    "relevant": "مرتبط",
    "replies": "پاسخ",
    "flagged": "علامت‌خورده",
    "transcripts": "متن",
}

# A stored on/off switch. ``None`` is the third value on purpose: it is what the
# database returns for a switch nobody has ever touched, and it is not the same
# fact as "off" — the deploy-time configuration decides in that case, and the
# label says so rather than guessing.
SWITCH_ON = ("روشن", "positive")
SWITCH_OFF = ("خاموش", "muted")
SWITCH_DEFAULT = ("پیش‌فرض تنظیمات", "muted")


def switch(value) -> tuple[str, str]:
    """Label and colour for a persisted switch, where ``None`` means unset."""
    if value is None:
        return SWITCH_DEFAULT
    return SWITCH_ON if value else SWITCH_OFF


def positive(key: str) -> str:
    """The label for a workload's positive-outcome column, or ``''``."""
    return USAGE_POSITIVE.get(str(key or ""), "")


def workload(value: str) -> str:
    """The Persian name of a workload, or the raw name if it is new to us."""
    name = str(value or "")
    return WORKLOADS.get(name, name or "—")


def event(value: str) -> tuple[str, str]:
    """Label and colour for a pool event kind."""
    kind = str(value or "")
    return EVENTS.get(kind, (kind or "—", "muted"))


__all__ = [
    "EVENTS",
    "SWITCH_DEFAULT",
    "SWITCH_OFF",
    "SWITCH_ON",
    "USAGE_POSITIVE",
    "WORKLOADS",
    "event",
    "positive",
    "switch",
    "workload",
]
