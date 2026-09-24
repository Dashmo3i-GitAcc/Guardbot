#!/usr/bin/env python3
"""Score increment Y on the *real* addressed path: before vs after.

Why this exists
---------------
``tools/eval_context.py`` measures the selector in isolation, over marker
blocks. It deliberately touches no database and no handler, so it cannot show
what Y does to a real prompt — an owner's roster, a real room window, the real
reading, a real state block and a real memory block together. This does, and it
is the measurement behind the checkpoint's real-path figures.

"Before" is reproduced faithfully rather than remembered: ``context_plan.read``
is replaced with a reading that wants all four sources, which is exactly what
the addressed path did before Y. The same code then renders the same blocks, so
the only difference between the two runs is the selection.

It stubs the model (``chat.reply``) and the search, so it spends nothing and
reaches no network; it seeds an in-memory database and resets it afterwards.

    python tools/bench_context_real.py
    python tools/bench_context_real.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "eval-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-bench")
os.environ.setdefault("GEMINI_KEY_STORE_PATH", "/tmp/guardbot-bench/gemini_keys.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    awareness,
    awareness_context,
    chat,
    config,
    context_plan,
    db,
    memory,
    nexus,
    state,
)
from app import main as app_main  # noqa: E402

OWNER = 999
MEMBER = 42
CHAT = -1001234567890
BOT_ID = 1

# A room with a realistic recent conversation (the window holds 20).
ROOM_LINES = (
    "بچه‌ها سرور دیشب ریست شد",
    "من دیدم، لاگ‌ها هم پاک شدن",
    "این روش خوب نیست راستش",
    "موافقم، باید بکاپ بذاریم",
    "قرار بود امروز دیپلوی کنیم؟",
    "نه، دیپلوی رو گذاشتیم فردا",
    "پس کی تست‌ها رو اجرا می‌کنیم",
    "من می‌تونم بعدازظهر انجام بدم",
    "مشکل لاگین هم هنوز پابرجاست",
    "اون رو باید جدا بررسی کنیم",
    "کاربرا شکایت کردن دیروز",
    "آره تیکت زیاد اومده",
    "کی می‌خوایم رفعش کنیم",
    "بذار اول بکاپ رو درست کنیم",
    "باشه من با فلانی هماهنگ می‌کنم",
    "ممنون، خبرش رو بده",
    "سرور دوم هم ریست شد؟",
    "نه فقط اولی",
    "خوبه، پس مشکل جدی نیست",
    "فعلا همین رو داشته باش",
)

# The representative turns: two the fast path should pay nothing for, and six
# that genuinely depend on the room or the person's own thread.
MESSAGES = (
    ("greeting", "سلام"),
    ("ack", "ممنون"),
    ("self-contained question", "قیمت چنده؟"),
    ("anaphora", "همونو بزن"),
    ("continuation", "قدم بعدی چیه؟"),
    ("opinion", "نکسوس نظرت چیه؟"),
    ("correction", "نه، من پایتون استفاده نمی‌کنم"),
    ("instruction", "بیا مشکل لاگین رو درست کنیم"),
)

# What the addressed path asked for before Y: everything, always.
FULL = context_plan.Reading(
    mode=context_plan.FULL,
    reasons=(context_plan.R_INSTRUCTION,),
    wants_conversation=True,
    wants_awareness=True,
    wants_state=True,
    wants_memory=True,
)


class _Bot:
    id = BOT_ID
    username = "guardbot"

    async def send_message(self, chat_id, text, **kwargs):
        return SimpleNamespace(message_id=1)

    async def send_chat_action(self, *args, **kwargs):
        pass

    async def get_chat_member(self, chat_id, user_id):
        # The rights the roster renders. Without this the rights block is
        # dropped (the call fails and is logged non-fatally) and the roster is
        # smaller than the one a real turn builds.
        return SimpleNamespace(
            status="administrator",
            can_restrict_members=True,
            can_delete_messages=True,
            can_promote_members=True,
            can_manage_chat=True,
        )


def _update(text):
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            message_id=10, photo=None, video=None, animation=None, video_note=None,
            sticker=None, voice=None, audio=None, document=None, text=text,
            caption=None, reply_to_message=None,
        ),
        effective_chat=SimpleNamespace(id=CHAT, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=OWNER, full_name="Tester", username="tester", is_bot=False
        ),
    )


def _setup():
    """A deployment with an owner, awareness on, and a seeded room."""
    config.OWNER_USER_ID = OWNER
    config.CONFIG_ADMINS = [f"{OWNER}:owner", "55:admin", "66:moderator"]
    config.GROUP_IDS = [CHAT]
    config.NEXUS_ACTORS_ONLY = True
    config.NEXUS_OBSERVE_ADMINS = True
    config.NEXUS_NAMES = ["nexus", "نکسوس"]
    config.NEXUS_PEOPLE_ENABLED = True
    config.NEXUS_EXTRA_ACTION_WORDS = []
    config.ADMIN_AI_ENABLED = True
    config.ADMIN_TOOL_GUEST_TOOLS = False
    config.BOT_ALIASES = []
    config.GEMINI_CHAT_ENABLED = True
    config.GEMINI_CHAT_API_KEY = "bench-chat-key"
    config.GEMINI_AWARENESS_API_KEY = "bench-awareness-key"
    config.NEXUS_AWARENESS_ENABLED = True
    config.NEXUS_AWARENESS_CONTEXT_MESSAGES = 20
    config.NEXUS_AWARENESS_WINDOW_CHARS = 6000

    # This tool calls the destructive ``db.*_reset`` helpers below, so it must
    # never be pointed at a real database. It forces the in-memory path rather
    # than trusting ``DB_PATH`` (which a host ``.env`` may set), exactly as the
    # sibling benchmarks force their own temp path.
    config.DB_PATH = ":memory:"
    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    nexus.reset_state()
    chat.reset_state()
    awareness_context.reset_rooms()
    awareness.reset_switch()
    app_main._nexus_visibility[CHAT] = "administrator"
    app_main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )

    for index, line in enumerate(ROOM_LINES):
        awareness.capture(
            CHAT,
            MEMBER if index % 2 else OWNER,
            awareness.ROLE_MEMBER,
            f"u{index}",
            line,
        )
    asyncio.run(
        memory.observe(
            {"id": OWNER, "is_bot": False}, CHAT,
            "من برنامه‌نویسم و بیشتر با Python کار می‌کنم",
        )
    )
    asyncio.run(
        state.observe(
            {"id": OWNER, "is_bot": False}, CHAT,
            "بیا مشکل لاگین بات رو درست کنیم", message_id=1,
        )
    )

    # The model, the search and the reply note are all stubbed: this measures
    # the server's composition, not the provider.
    seen: list[str] = []

    async def _reply(chat_id, user_id, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        seen.append(context)
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    app_main.chat.reply = _reply
    app_main.web_search.enabled = lambda: False
    app_main._awareness_note_reply = lambda *a, **k: None
    return seen


def _run(text, reading, seen):
    real_read = context_plan.read
    context_plan.read = lambda *a, **k: reading
    try:
        seen.clear()
        started = time.perf_counter()
        asyncio.run(app_main._answer_conversationally(_update(text), _ctx()))
        elapsed = (time.perf_counter() - started) * 1000.0
    finally:
        context_plan.read = real_read
    return (seen[0] if seen else ""), elapsed


def _ctx():
    bot = _Bot()
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def measure(iterations: int = 40):
    """The before/after comparison, over the real addressed path.

    Restores the database path and closes the connection afterwards, so calling
    it in-process leaves nothing of its setup behind.
    """
    old_db_path = config.DB_PATH
    try:
        return _measure(iterations)
    finally:
        db._conn = None
        config.DB_PATH = old_db_path


def _measure(iterations: int = 40):
    seen = _setup()
    real_read = context_plan.read

    rows = []
    for label, text in MESSAGES:
        after, _ = _run(text, real_read(text), seen)
        before, _ = _run(text, FULL, seen)
        rows.append({"case": label, "before": len(before), "after": len(after)})

    def latency(reading):
        samples = []
        for _ in range(iterations):
            for _label, text in MESSAGES:
                samples.append(
                    _run(text, real_read(text) if reading is None else reading, seen)[1]
                )
        ordered = sorted(samples)
        return {
            "p50": round(statistics.median(ordered), 2),
            "p95": round(ordered[int(len(ordered) * 0.95)], 2),
            "n": len(ordered),
        }

    before_ms = latency(FULL)
    after_ms = latency(None)

    # How often each source is actually read, before vs after.
    counts = {"window": 0, "reading": 0, "memory": 0, "state": 0}

    def count(reading):
        for key in counts:
            counts[key] = 0
        originals = {}
        for module, name, key in (
            (awareness, "window", "window"),
            (awareness, "room_block", "window"),
            (app_main, "_room_reading", "reading"),
            (app_main, "_memory_context", "memory"),
            (app_main, "_state_context", "state"),
        ):
            originals[(module, name)] = getattr(module, name)
            setattr(module, name, _counter(originals[(module, name)], counts, key))
        try:
            for _label, text in MESSAGES:
                _run(text, real_read(text) if reading is None else reading, seen)
        finally:
            for (module, name), original in originals.items():
                setattr(module, name, original)
        return dict(counts)

    before_reads = count(FULL)
    after_reads = count(None)

    total_before = sum(row["before"] for row in rows)
    total_after = sum(row["after"] for row in rows)
    return {
        "cases": rows,
        "before_chars": total_before,
        "after_chars": total_after,
        "saved_chars": total_before - total_after,
        "saved_pct": (
            round(100.0 * (1 - total_after / total_before), 1)
            if total_before else 0.0
        ),
        "room_messages": len(ROOM_LINES),
        "assembly_ms_before": before_ms,
        "assembly_ms_after": after_ms,
        "reads_before": before_reads,
        "reads_after": after_reads,
    }


def _counter(original, counts, key):
    def inner(*args, **kwargs):
        counts[key] += 1
        return original(*args, **kwargs)

    return inner


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--iterations",
        type=int,
        default=40,
        help="latency samples per case (lower for a quick check)",
    )
    args = parser.parse_args(argv)

    report = measure(iterations=max(1, int(args.iterations)))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("Increment Y — the real addressed path, before vs after")
    print("=" * 60)
    print(f"room messages     {report['room_messages']}")
    print(f"{'case':24} {'before':>8} {'after':>8} {'saved':>8}")
    for row in report["cases"]:
        print(
            f"{row['case']:24} {row['before']:8d} {row['after']:8d} "
            f"{row['before'] - row['after']:8d}"
        )
    print(
        f"{'TOTAL':24} {report['before_chars']:8d} {report['after_chars']:8d} "
        f"{report['saved_chars']:8d}   ({report['saved_pct']}% smaller)"
    )
    print()
    print("ASSEMBLY (DB + composition, model excluded)")
    print(
        f"  before  p50 {report['assembly_ms_before']['p50']} / "
        f"p95 {report['assembly_ms_before']['p95']} ms"
    )
    print(
        f"  after   p50 {report['assembly_ms_after']['p50']} / "
        f"p95 {report['assembly_ms_after']['p95']} ms"
    )
    print()
    print(f"READS over {len(MESSAGES)} messages (before -> after)")
    for key in ("window", "reading", "memory", "state"):
        print(f"  {key:9} {report['reads_before'][key]:3d} -> "
              f"{report['reads_after'][key]:3d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
