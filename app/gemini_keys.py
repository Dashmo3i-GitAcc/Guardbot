"""The owner's Gemini control plane, rendered inside Telegram.

What this is, and what it deliberately is not
---------------------------------------------
It is a set of screens over the pool that already exists: the same accounts, the
same counters, the same events, the same single registry in
``app/gemini_pool.py``. There is no second pool, no second set of counters and no
second definition of "how many requests has this key made". If a number here
disagrees with ``/pool``, that is a bug in this module and nothing else.

It is not a web dashboard, and it is not a menu bolted onto ``/pool``. ``/pool``
is a dump of everything for a human who already knows what they are looking at;
this is a set of questions the owner can ask one at a time — which accounts does
the conversation have, what has this one been doing, when was it last limited,
what happened in the last hour — with the one write action the owner actually
needed: add or remove a credential.

The separation the brief asks for is structural, not cosmetic
------------------------------------------------------------
Chat and awareness are different workloads in the pool already: different
accounts, different daily allowances, different model preferences, different
breakers. They appear here as separate sections because they *are* separate, and
the dashboard has no code path that could pool them together — it asks the pool
for one workload at a time and renders what it is given.

Where the authority lives
-------------------------
Nothing here authorises anything. Every function is a pure renderer or a parser;
the caller in ``app/main.py`` resolves the actor, checks that they are the owner,
and only then calls in. ``parse`` is the only thing that touches
attacker-controlled input, and all it does is split a callback payload into
three short strings which are then used as *lookups* — a workload name and a slot
that must already exist in the pool. A crafted payload can therefore name a
workload that exists and a slot that exists, and it can name nothing else.

Screen text lives in this module rather than in ``app/config.py`` for the same
reason ``app/rbac.py`` and ``app/nexus.py`` keep their own: it is specific to
this one screen family, and it is read alongside the code that lays it out.
"""
from __future__ import annotations

import html
import threading
import time

from . import config, db, gemini_pool, key_store

# The callback namespace. Short, because Telegram caps ``callback_data`` at 64
# bytes and the longest payload here is ``gk:a:awareness:shared12``.
PREFIX = "gk:"

# ── Labels ────────────────────────────────────────────────────────────────
# Persian names for the workloads, in the order they are shown. The order comes
# from ``config.GEMINI_POOLS``, so a workload added to the pool appears here
# without this dictionary having to be the thing that was remembered.
WORKLOAD_LABELS = {
    "chat": "گفتگو",
    "awareness": "اورنس",
    "intent": "اینتنت",
    "moderation": "مدیریت محتوا",
    "transcribe": "رونویسی",
    "tts": "گفتار",
}

STATE_LABELS = {
    "ACTIVE": "فعال",
    "RATE_LIMITED": "محدود شده",
    "QUOTA_EXHAUSTED": "سهمیه تمام",
    "UNAVAILABLE": "در دسترس نیست",
    "INVALID": "کلید نامعتبر",
    "DISABLED": "غیرفعال",
    "RECOVERING": "در حال بازیابی",
}

EVENT_LABELS = {
    "model_failover": "جایگزینی مدل",
    "account_failover": "جایگزینی حساب",
    "account_recovered": "بازیابی حساب",
    "pool_empty": "استخر خالی",
    "pool_critical": "یک حساب باقی مانده",
    "no_compatible_model": "مدل سازگار نیست",
    "time_budget": "سقف زمان درخواست",
}

SOURCE_LABELS = {
    "env": "از تنظیمات سرور",
    "runtime": "افزوده‌شده از تلگرام",
}

# The most rows any one screen prints. Telegram rejects a message over 4096
# characters, and a pool with twenty accounts would reach that; the cap is on
# the screen rather than on the data, and the totals above it are always exact.
MAX_ROWS = 12
MAX_EVENTS = 10
DAILY_DAYS = 7

# Telegram rejects a message over 4096 characters, and a pool with a dozen
# accounts plus a long model list can reach that. The cap is applied to the
# rendered text rather than to the data, so the totals and the counts above the
# list are always exact and it is only the tail of a long list that is dropped —
# and it says so when it drops it, because a silently truncated report reads as a
# complete one.
MAX_CHARS = 3800
TRUNCATED_NOTE = "<i>… ادامه کوتاه شد. برای دیدن بقیه، حساب‌ها را یکی‌یکی باز کن.</i>"


def _render(lines: list[str]) -> str:
    """Join the lines, dropping whole ones from the end if the result is too long.

    Whole lines rather than a slice, because slicing HTML in the middle of a tag
    is a parse error at best and a mangled message at worst. A single line is
    never near the limit: every value interpolated here is a count, a short
    label or a truncated error string.
    """
    text = "\n".join(lines)
    if len(text) <= MAX_CHARS:
        return text
    budget = MAX_CHARS - len(TRUNCATED_NOTE) - 2
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append("")
    kept.append(TRUNCATED_NOTE)
    return "\n".join(kept)


# ── Small formatting helpers ──────────────────────────────────────────────
def _num(value) -> str:
    return f"{int(value or 0):,}"


def label(workload: str) -> str:
    return WORKLOAD_LABELS.get(workload, workload)


def _state(value: str) -> str:
    return STATE_LABELS.get(value, value or "—")


def _ago(seconds: int) -> str:
    if seconds <= 0:
        return "همین حالا"
    if seconds < 60:
        return f"{seconds} ثانیه پیش"
    if seconds < 3600:
        return f"{seconds // 60} دقیقه پیش"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت پیش"
    return f"{seconds // 86400} روز پیش"


def _stamp(epoch: int) -> str:
    """An absolute time, in UTC, or a dash.

    UTC rather than local on purpose: the host's timezone is not something this
    module knows, and a timestamp silently rendered in the wrong zone is worse
    than one that says which zone it is in.
    """
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(epoch))) + " UTC"


def _cooldown(account, now: float) -> str:
    left = int(account.cooldown_until - now)
    if left <= 0:
        return "ندارد"
    return f"{_ago(left).replace(' پیش', '')} باقی مانده"


# ── The pending "send me the key" prompt ──────────────────────────────────
# One entry per owner. The value is the workload and the moment the prompt
# expires. It is held in memory rather than in the database on purpose: it is a
# five-minute invitation, and one that survived a restart would be an invitation
# to hand a credential to a process that had already forgotten asking for it.
_pending: dict[int, tuple[str, float]] = {}
_pending_lock = threading.Lock()


def begin_add(user_id: int, workload: str, *, now: float | None = None) -> bool:
    """Arm the key prompt for one owner. False when the workload is not managed."""
    if not key_store.is_managed(workload):
        return False
    moment = time.time() if now is None else now
    with _pending_lock:
        _pending[int(user_id)] = (str(workload), moment + config.GEMINI_KEY_ADD_TTL_SECONDS)
    return True


def pending_for(user_id: int, *, now: float | None = None) -> str:
    """The workload this owner is being asked for a key for, or "".

    Expiry is evaluated on read rather than swept by a timer: the only moment it
    matters is the moment a message arrives, and a background job for a
    dictionary with at most one entry would be more machinery than the thing it
    manages.
    """
    moment = time.time() if now is None else now
    with _pending_lock:
        found = _pending.get(int(user_id))
        if found is None:
            return ""
        workload, expires = found
        if moment >= expires:
            _pending.pop(int(user_id), None)
            return ""
        return workload


def clear_pending(user_id: int) -> None:
    with _pending_lock:
        _pending.pop(int(user_id), None)


def reset_pending() -> None:
    """Drop every armed prompt. For tests, which share one process."""
    with _pending_lock:
        _pending.clear()


def pending_ttl_seconds() -> int:
    return int(config.GEMINI_KEY_ADD_TTL_SECONDS)


# ── Pool facts ────────────────────────────────────────────────────────────
def workload_names() -> list[str]:
    """Every configured workload, in configuration order."""
    return [spec["workload"] for spec in config.GEMINI_POOLS]


def _pool(workload: str):
    return gemini_pool.pool_for(workload)


def _runtime_slots(workload: str) -> dict[str, key_store.Entry]:
    return {
        entry.slot: entry
        for entry in key_store.entries_or_empty()[0]
        if entry.workload == workload
    }


def _source_of(workload: str, slot: str, runtime: dict) -> str:
    return "runtime" if slot in runtime else "env"


def _slot_title(workload: str, slot: str, runtime: dict) -> str:
    entry = runtime.get(slot)
    if entry is not None:
        return entry.label
    return f"API #{slot}"


# ── Screens ───────────────────────────────────────────────────────────────
def parse(data: str) -> tuple[str, str, str] | None:
    """Split one callback payload into ``(verb, arg, arg2)``.

    Returns None for anything that is not this namespace. Every part is
    truncated to a sane length before it is returned, so a hostile payload
    cannot become a hostile string further down.
    """
    if not data or not data.startswith(PREFIX):
        return None
    parts = data[len(PREFIX):].split(":")
    verb = parts[0][:8] if parts and parts[0] else ""
    if not verb:
        return None
    first = parts[1][:40] if len(parts) > 1 else ""
    second = parts[2][:40] if len(parts) > 2 else ""
    return verb, first, second


def overview():
    """Every workload, one line each, with the numbers that say if it is well."""
    lines = ["🧠 <b>کلیدهای Gemini</b>", ""]
    rows: list[list[tuple[str, str]]] = []
    for workload in workload_names():
        pool = _pool(workload)
        if pool is None:
            continue
        health = pool.health()
        mark = "✍️" if key_store.is_managed(workload) else "👁"
        lines.append(
            f"{mark} <b>{html.escape(label(workload))}</b> — "
            f"{health['accounts']} حساب · {health['active']} فعال · "
            f"{health['limited']} محدود"
        )
        if health["exhausted"] or health["invalid"]:
            lines.append(
                f"     سهمیه تمام: {health['exhausted']} · نامعتبر: {health['invalid']}"
            )
        if health["daily_budget"]:
            lines.append(
                f"     امروز: {_num(health['daily_remaining'])} از "
                f"{_num(health['daily_budget'] * health['accounts'])} باقی"
            )
        rows.append([(f"{mark} {label(workload)}", f"{PREFIX}w:{workload}")])
    lines.append("")
    lines.append("✍️ قابل افزودن و حذف · 👁 فقط نمایش")
    lines.append("<i>فقط مالک. هیچ کلیدی در این پیام‌ها نمایش داده نمی‌شود.</i>")
    rows.append([("🔄 بروزرسانی", f"{PREFIX}home"), ("✖️ بستن", f"{PREFIX}x")])
    return _render(lines), rows


def workload_view(workload: str):
    """One workload: its accounts, and the write buttons if it is managed."""
    pool = _pool(workload)
    if pool is None:
        return TEXT_UNKNOWN_WORKLOAD, [[("⬅️ بازگشت", f"{PREFIX}home")]]
    health = pool.health()
    runtime = _runtime_slots(workload)
    managed = key_store.is_managed(workload)
    now = time.time()

    lines = [f"<b>{html.escape(label(workload))}</b>", ""]
    lines.append(
        f"حساب‌ها: {health['accounts']}   فعال: {health['active']}   "
        f"محدود: {health['limited']}   سهمیه تمام: {health['exhausted']}   "
        f"نامعتبر: {health['invalid']}"
    )
    if pool.models:
        lines.append(
            "مدل‌ها: " + html.escape(" → ".join(pool.models[:4]))
            + (" …" if len(pool.models) > 4 else "")
        )
    if health["daily_budget"]:
        lines.append(
            f"سهمیه روزانه: {_num(health['daily_remaining'])} از "
            f"{_num(health['daily_budget'] * health['accounts'])} "
            f"({_num(health['daily_budget'])} برای هر حساب)"
        )
    if health["empty"]:
        lines.append("⚠️ هیچ حساب قابل استفاده‌ای نیست.")
    elif health["degraded"]:
        lines.append("⚠️ فقط یک حساب قابل استفاده مانده؛ جایگزینی وجود ندارد.")

    rows: list[list[tuple[str, str]]] = []
    accounts = pool.accounts[:MAX_ROWS]
    if not accounts:
        lines.append("")
        lines.append("<i>هیچ حسابی تنظیم نشده است.</i>")
    for account in accounts:
        row = account.describe()
        lines.append("")
        lines.append(
            f"▪️ <b>{html.escape(_slot_title(workload, account.slot, runtime))}</b> "
            f"({html.escape(row['masked'])}) — "
            f"{html.escape(SOURCE_LABELS[_source_of(workload, account.slot, runtime)])}"
        )
        lines.append(
            f"وضعیت: {html.escape(_state(row['state']))} · "
            f"درخواست: {_num(row['requests'])} · موفق: {_num(row['successes'])} · "
            f"ناموفق: {_num(row['failures'])}"
        )
        lines.append(
            f"محدودیت نرخ: {_num(row['rate_limits'])} · "
            f"اتمام سهمیه: {_num(row['quota_events'])} · "
            f"خنک‌سازی: {_cooldown(account, now)}"
        )
        if pool.daily_budget:
            lines.append(
                f"امروز: {_num(account.daily_calls())} از {_num(pool.daily_budget)}"
            )
        rows.append(
            [
                (
                    f"🔍 {_slot_title(workload, account.slot, runtime)}",
                    f"{PREFIX}a:{workload}:{account.slot}",
                )
            ]
        )
    if len(pool.accounts) > MAX_ROWS:
        lines.append("")
        lines.append(f"<i>… و {len(pool.accounts) - MAX_ROWS} حساب دیگر.</i>")

    if managed:
        rows.append([("➕ افزودن کلید", f"{PREFIX}+:{workload}")])
    rows.append(
        [
            ("📊 مصرف", f"{PREFIX}u:{workload}"),
            ("🗂 رویدادها", f"{PREFIX}e:{workload}"),
            ("📅 روزانه", f"{PREFIX}h:{workload}"),
        ]
    )
    rows.append(
        [
            ("🔄 بروزرسانی", f"{PREFIX}w:{workload}"),
            ("⬅️ بازگشت", f"{PREFIX}home"),
        ]
    )
    return _render(lines), rows


def account_view(workload: str, slot: str):
    """One account: its counters, its last error, and what may be done to it."""
    pool = _pool(workload)
    account = _find_account(pool, slot)
    if pool is None or account is None:
        return TEXT_UNKNOWN_ACCOUNT, [[("⬅️ بازگشت", f"{PREFIX}w:{workload}")]]
    runtime = _runtime_slots(workload)
    row = account.describe()
    source = _source_of(workload, slot, runtime)
    managed = key_store.is_managed(workload)
    now = time.time()

    lines = [
        f"<b>{html.escape(label(workload))}</b> › "
        f"<b>{html.escape(_slot_title(workload, slot, runtime))}</b>",
        "",
        f"شناسه: <code>{html.escape(row['slot'])}</code> · "
        f"اثر انگشت: <code>{html.escape(account.fingerprint)}</code>",
        f"پوشانده‌شده: <code>{html.escape(row['masked'])}</code> · "
        f"منبع: {html.escape(SOURCE_LABELS[source])}",
        f"وضعیت: <b>{html.escape(_state(row['state']))}</b> · "
        f"خنک‌سازی: {_cooldown(account, now)}",
        "",
        f"درخواست: {_num(row['requests'])}",
        f"موفق: {_num(row['successes'])}",
        f"ناموفق: {_num(row['failures'])}",
        f"محدودیت نرخ: {_num(row['rate_limits'])}",
        f"اتمام سهمیه: {_num(row['quota_events'])}",
        "",
        f"آخرین موفقیت: {_stamp(row['last_success'])}",
        f"آخرین شکست: {_stamp(row['last_failure'])}",
    ]
    if pool.daily_budget:
        lines.append(
            f"امروز: {_num(account.daily_calls())} از {_num(pool.daily_budget)}"
        )
    lines.append("")
    lines.append("<b>سهمیه و ریست</b>")
    lines.append(
        "سرویس‌دهنده سهمیه باقی‌مانده را برای این کلیدها اعلام نمی‌کند؛ "
        "پس اینجا عددی ساخته نمی‌شود."
    )
    if account.cooldown_until and account.cooldown_until > now:
        lines.append(
            f"خنک‌سازی تا: {_stamp(account.cooldown_until)} "
            f"({_cooldown(account, now)})"
        )
    if row["last_error"]:
        lines.append("")
        lines.append(f"آخرین خطا: <code>{html.escape(row['last_error'])}</code>")

    rows: list[list[tuple[str, str]]] = [
        [("🧩 مدل‌ها", f"{PREFIX}m:{workload}:{slot}")]
    ]
    if managed and source == "runtime":
        rows.append([("🗑 حذف این کلید", f"{PREFIX}-:{workload}:{slot}")])
    elif managed:
        rows.append([("ℹ️ کلید سرور", f"{PREFIX}i:{workload}:{slot}")])
    rows.append(
        [
            ("🔄 بروزرسانی", f"{PREFIX}a:{workload}:{slot}"),
            ("⬅️ بازگشت", f"{PREFIX}w:{workload}"),
        ]
    )
    return _render(lines), rows


def models_view(workload: str, slot: str):
    """The per-model state inside one account.

    This is the screen that answers "the account is fine, so why is it failing":
    the two levels of failover are tracked separately, and a rate-limited model
    on a healthy account is exactly what that looks like.
    """
    pool = _pool(workload)
    account = _find_account(pool, slot)
    if pool is None or account is None:
        return TEXT_UNKNOWN_ACCOUNT, [[("⬅️ بازگشت", f"{PREFIX}w:{workload}")]]
    now = time.time()
    lines = [
        f"<b>{html.escape(label(workload))}</b> › "
        f"<b>{html.escape(account.masked)}</b> › مدل‌ها",
        "",
    ]
    known = sorted(account.model_states)
    if not known:
        lines.append(
            "<i>این حساب هنوز درخواستی نزده، پس هیچ مدلی برایش ثبت نشده است.</i>"
        )
    for name in known[:MAX_ROWS]:
        state = account.model_states[name]
        row = state.describe()
        lines.append(f"▪️ <code>{html.escape(name)}</code>")
        lines.append(
            f"وضعیت: {html.escape(_state(row['state']))} · "
            f"درخواست: {_num(row['requests'])} · موفق: {_num(row['successes'])} · "
            f"ناموفق: {_num(row['failures'])}"
        )
        lines.append(
            f"محدودیت نرخ: {_num(row['rate_limits'])} · "
            f"سهمیه: {_num(row['quota_events'])} · "
            f"آخرین استفاده: {_stamp(row['last_use'])}"
        )
        if row["cooldown_until"] and row["cooldown_until"] > now:
            lines.append(f"خنک‌سازی تا: {_stamp(row['cooldown_until'])}")
        lines.append("")
    lines.append("<b>ترتیب مدل‌ها برای این بار کاری</b>")
    lines.append(html.escape(" → ".join(pool.models)) or "—")
    rows = [
        [("🔄 بروزرسانی", f"{PREFIX}m:{workload}:{slot}")],
        [("⬅️ بازگشت", f"{PREFIX}a:{workload}:{slot}")],
    ]
    return _render(lines), rows


def usage_view(workload: str):
    """Totals, and an explicit statement of what the totals do and do not count."""
    pool = _pool(workload)
    if pool is None:
        return TEXT_UNKNOWN_WORKLOAD, [[("⬅️ بازگشت", f"{PREFIX}home")]]
    totals = db.pool_counts(workload)
    health = pool.health()
    lines = [
        f"<b>{html.escape(label(workload))}</b> › مصرف",
        "",
        "<b>جمع کل (از ابتدا)</b>",
        f"درخواست: {_num(totals['requests'])}",
        f"موفق: {_num(totals['successes'])}",
        f"ناموفق: {_num(totals['failures'])}",
        f"محدودیت نرخ: {_num(totals['rate_limits'])}",
        f"اتمام سهمیه: {_num(totals['quota_events'])}",
        "",
        "<b>همین حالا</b>",
        f"حساب‌های قابل استفاده: {health['usable']} از {health['accounts']}",
    ]
    if health["daily_budget"]:
        lines.append(
            f"سهمیه امروز: {_num(health['daily_remaining'])} از "
            f"{_num(health['daily_budget'] * health['accounts'])} باقی"
        )
    else:
        lines.append("سهمیه روزانه: تنظیم نشده")
    lines.append("")
    lines.append("<b>این اعداد چه چیزی را می‌شمارند</b>")
    lines.append(
        "هر عدد بالا یک <b>درخواست به سرویس‌دهنده</b> است، نه یک پیام کاربر. "
        "یک درخواست منطقی ممکن است روی چند مدل و چند حساب امتحان شود و هر "
        "تلاش جداگانه شمرده می‌شود — پس «درخواست» می‌تواند از تعداد پاسخ‌های "
        "داده‌شده بیشتر باشد، و این طبیعی است."
    )
    lines.append(
        "مصرف <b>توکن</b> ردیابی نمی‌شود: سرویس‌دهنده برای این کلیدها تعداد "
        "توکن را اعلام نمی‌کند، و عددی که ساخته شود فقط شبیه یک اندازه‌گیری است."
    )
    lines.append(
        "سهمیه باقی‌مانده و زمان ریست هم فقط وقتی نمایش داده می‌شود که خودِ "
        "پاسخ خطا آن را همراه داشته باشد."
    )
    rows = [
        [
            ("🗂 رویدادها", f"{PREFIX}e:{workload}"),
            ("📅 روزانه", f"{PREFIX}h:{workload}"),
        ],
        [
            ("🔄 بروزرسانی", f"{PREFIX}u:{workload}"),
            ("⬅️ بازگشت", f"{PREFIX}w:{workload}"),
        ],
    ]
    return _render(lines), rows


def events_view(workload: str):
    """The workload's recent transitions, newest first."""
    rows_db = db.pool_events(MAX_EVENTS, workload=workload)
    lines = [
        f"<b>{html.escape(label(workload))}</b> › رویدادها",
        "",
    ]
    if not rows_db:
        lines.append(
            "<i>رویدادی ثبت نشده. این جدول فقط تغییر وضعیت‌ها را نگه می‌دارد، "
            "نه هر درخواست را.</i>"
        )
    for row in rows_db:
        kind = EVENT_LABELS.get(row["kind"], row["kind"])
        head = f"{_stamp(row['at'])} · <b>{html.escape(kind)}</b>"
        if row["slot"]:
            head += f" · <code>{html.escape(row['slot'])}</code>"
        lines.append(head)
        detail = row["detail"] or row["reason"]
        if row["model"]:
            detail = f"{row['model']} — {detail}" if detail else row["model"]
        if detail:
            lines.append(f"     {html.escape(str(detail))}")
    rows = [
        [("🔄 بروزرسانی", f"{PREFIX}e:{workload}")],
        [("⬅️ بازگشت", f"{PREFIX}w:{workload}")],
    ]
    return _render(lines), rows


def daily_view(workload: str):
    """The last few API days, per account.

    The day boundary is the provider's own (``db.ai_day``), not local midnight,
    because that is when the allowance it describes actually resets.
    """
    pool = _pool(workload)
    if pool is None:
        return TEXT_UNKNOWN_WORKLOAD, [[("⬅️ بازگشت", f"{PREFIX}home")]]
    runtime = _runtime_slots(workload)
    now = time.time()
    days = [db.ai_day(now - index * 86400) for index in range(DAILY_DAYS)]
    lines = [
        f"<b>{html.escape(label(workload))}</b> › روزانه",
        "",
        "<i>روز سرویس‌دهنده، نه نیمه‌شب محلی — سهمیه همان‌جا ریست می‌شود.</i>",
    ]
    if not pool.daily_budget:
        lines.append("")
        lines.append("این بار کاری سهمیه روزانه ندارد؛ پس فقط شمارش است.")
    for day in days:
        counts = db.daily_for(workload, day)
        if not counts and not pool.daily_budget:
            continue
        parts = [
            f"{html.escape(_slot_title(workload, account.slot, runtime))}: "
            f"{_num(counts.get(account.slot, 0))}"
            for account in pool.accounts[:MAX_ROWS]
        ]
        lines.append("")
        lines.append(f"<b>{html.escape(day)}</b>")
        lines.append(" · ".join(parts) if parts else "—")
        if pool.daily_budget:
            total = sum(counts.values())
            lines.append(
                f"جمع: {_num(total)} از {_num(pool.daily_budget * len(pool.accounts))}"
            )
    rows = [
        [("🔄 بروزرسانی", f"{PREFIX}h:{workload}")],
        [("⬅️ بازگشت", f"{PREFIX}w:{workload}")],
    ]
    return _render(lines), rows


def remove_prompt(workload: str, slot: str):
    """The confirmation screen for removing one runtime credential.

    A confirmation rather than a single press, because this is the one action
    here that can take a workload offline: if the removed key was the last one
    that worked, the next message that needs an answer does not get one.
    """
    pool = _pool(workload)
    account = _find_account(pool, slot)
    entry = key_store.entry_for(workload, slot)
    if not key_store.is_managed(workload):
        return TEXT_NOT_MANAGED, [[("⬅️ بازگشت", f"{PREFIX}w:{workload}")]]
    if entry is None:
        return TEXT_NOT_RUNTIME_KEY, [[("⬅️ بازگشت", f"{PREFIX}w:{workload}")]]
    masked = account.masked if account is not None else entry.masked
    others = 0
    if pool is not None:
        others = sum(1 for a in pool.accounts if a.slot != slot and a.usable(time.time()))
    lines = [
        f"<b>حذف کلید</b> — {html.escape(label(workload))}",
        "",
        f"حساب: <code>{html.escape(slot)}</code> ({html.escape(masked)})",
        f"افزوده‌شده: {_stamp(entry.added_at)}",
        "",
        "این کلید از استخر حذف می‌شود و از درخواست بعدی دیگر استفاده نخواهد شد.",
        "کلیدهای تنظیمات سرور از این‌جا حذف نمی‌شوند.",
        "",
    ]
    if others == 0:
        lines.append(
            "⚠️ <b>هیچ حساب قابل استفاده دیگری باقی نمی‌ماند.</b> "
            "این بار کاری تا افزودن کلید تازه پاسخ نخواهد داد."
        )
    else:
        lines.append(f"حساب‌های دیگرِ قابل استفاده: {others}")
    rows = [
        [("🗑 حذف کن", f"{PREFIX}!:{workload}:{slot}")],
        [("✖️ انصراف", f"{PREFIX}a:{workload}:{slot}")],
    ]
    return _render(lines), rows


def add_prompt(workload: str, *, private: bool):
    """The screen shown when the owner presses "add a key"."""
    if not key_store.is_managed(workload):
        return TEXT_NOT_MANAGED, [[("⬅️ بازگشت", f"{PREFIX}home")]]
    if not private:
        # A key typed into a group is a key that has already been published, so
        # the prompt is never even offered there.
        return (
            TEXT_ADD_NEEDS_PRIVATE.format(label=html.escape(label(workload))),
            [[("⬅️ بازگشت", f"{PREFIX}w:{workload}")]],
        )
    return (
        TEXT_ADD_PROMPT.format(
            label=html.escape(label(workload)),
            ttl=pending_ttl_seconds() // 60,
        ),
        [[("✖️ انصراف", f"{PREFIX}w:{workload}")]],
    )


def _find_account(pool, slot: str):
    if pool is None:
        return None
    for account in pool.accounts:
        if account.slot == str(slot):
            return account
    return None


# ── Texts ─────────────────────────────────────────────────────────────────
TEXT_UNKNOWN_WORKLOAD = "این بار کاری شناخته‌شده نیست."
TEXT_UNKNOWN_ACCOUNT = "این حساب پیدا نشد — ممکن است حذف شده باشد."
TEXT_NOT_MANAGED = (
    "کلیدهای این بخش از تلگرام قابل تغییر نیستند و فقط نمایش داده می‌شوند."
)
TEXT_NOT_RUNTIME_KEY = (
    "این حساب یک کلید تنظیمات سرور است. حذف آن از این‌جا ممکن نیست — "
    "باید از فایل تنظیمات سرور برداشته شود."
)
TEXT_STALE = "این دکمه منقضی شده است. دوباره بازش کن."
TEXT_ADD_PROMPT = (
    "افزودن کلید به <b>{label}</b>\n\n"
    "کلید را در همین چت خصوصی بفرست. پیام کلید بلافاصله حذف می‌شود، "
    "و کلید هیچ‌وقت در گزارش‌ها یا لاگ‌ها نمایش داده نمی‌شود.\n\n"
    "این درخواست {ttl} دقیقه معتبر است."
)
TEXT_ADD_NEEDS_PRIVATE = (
    "برای افزودن کلید به <b>{label}</b> باید در چت خصوصی با ربات باشی.\n"
    "کلیدی که در گروه فرستاده شود دیگر محرمانه نیست."
)
TEXT_ADD_VERIFYING = "🔎 کلید دریافت شد و پیام حذف شد. در حال بررسی با سرویس‌دهنده…"
TEXT_ADD_OK = (
    "✅ کلید برای <b>{label}</b> افزوده شد.\n"
    "شناسه: <code>{slot}</code> · پوشانده‌شده: <code>{masked}</code>\n"
    "تأیید سرویس‌دهنده: {detail}\n\n"
    "استخر همین حالا بازسازی شد؛ از درخواست بعدی استفاده می‌شود."
)
TEXT_ADD_DUPLICATE = (
    "این کلید از قبل برای <b>{label}</b> ثبت شده بود؛ چیزی تغییر نکرد."
)
TEXT_ADD_FAILED = (
    "❌ کلید ذخیره نشد.\n"
    "دلیل: {reason}\n\n"
    "اگر سرویس‌دهنده در دسترس نبود، دوباره تلاش کن."
)
TEXT_ADD_BAD_SHAPE = (
    "این متن شکل یک کلید را ندارد (یک توکن بدون فاصله لازم است). "
    "کلید درست را بفرست، یا انصراف بزن."
)
TEXT_REMOVE_OK = (
    "🗑 کلید <code>{slot}</code> از <b>{label}</b> حذف شد.\n"
    "استخر همین حالا بازسازی شد."
)
TEXT_REMOVE_MISSING = "این کلید از قبل حذف شده بود."
TEXT_ENV_KEY_INFO = (
    "این حساب از فایل تنظیمات سرور می‌آید. برای تغییرش باید "
    "<code>.env</code> روی سرور ویرایش و ربات دوباره راه‌اندازی شود."
)
TEXT_DENIED = "این بخش فقط برای مالک است."
# The one-tap way in. Telegram's command menu already lists /keys, but a menu has
# to be noticed before it can be used, and this is the same entry point sitting
# where the owner actually lands.
TEXT_BUTTON_OPEN = "🔑 کلیدهای Gemini"
TEXT_STORE_BROKEN = (
    "⚠️ فایل کلیدها خوانده نشد. برای جلوگیری از پاک‌شدن بقیه کلیدها، "
    "هیچ تغییری اعمال نمی‌شود. گزارش سرور لازم است."
)
TEXT_PROBE_REASONS = {
    "invalid_credential": "سرویس‌دهنده این کلید را نامعتبر می‌داند.",
    "unsupported_model": "این کلید به هیچ مدل قابل استفاده‌ای دسترسی ندارد.",
    "rate_limited": "سرویس‌دهنده همین حالا درخواست را محدود کرده است.",
    "quota_exhausted": "سهمیه این کلید تمام شده است.",
    "provider_error": "سرویس‌دهنده در دسترس نبود.",
    "timeout": "پاسخی از سرویس‌دهنده نرسید.",
    "network_error": "ارتباط با سرویس‌دهنده برقرار نشد.",
    "sdk_missing": "کتابخانه Gemini روی سرور نصب نیست.",
}


def probe_reason(kind: str, detail: str) -> str:
    """A Persian sentence for a failed verification. Never includes the key."""
    sentence = TEXT_PROBE_REASONS.get(kind)
    if sentence is None:
        sentence = f"بررسی ناموفق بود ({kind})."
    if detail:
        return f"{sentence} <code>{html.escape(detail)}</code>"
    return sentence
