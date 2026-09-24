#!/usr/bin/env python3
"""Score long-term user memory: what it extracts, what it costs, what it stores.

Why this exists
---------------
The roadmap requires W's bounded model to be chosen by measurement rather than by
the brief's "20–50 items", and every claim about the stage to come with a number.
This harness produces those numbers. It is deterministic and offline: no Telegram,
no model, no network. Memory extraction is a regex and the store is SQLite, so
everything here is reproducible.

What it measures
----------------
* **extraction** — precision and recall of the explicit-trigger detector over a
  labelled corpus, including the false positives that matter most (an ordinary
  claim of authority must not become a memory).
* **storage** — rows, bytes per row, and the projected size at the owner's scale
  (3000 members) for several per-person ceilings, against the 200 MB budget.
* **write cost** — the upsert plus the per-person prune, per remembered clause.
* **retrieval** — the indexed read the context block makes, and the characters it
  adds.
* **model calls** — asserted to be zero: the module imports no model client.

    python tools/eval_memory.py
    python tools/eval_memory.py --json
    python tools/eval_memory.py --users 3000 --per-user 30
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import string
import sys
import time

os.environ.setdefault("BOT_TOKEN", "eval-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-eval")
os.environ.setdefault("GEMINI_KEY_STORE_PATH", "/tmp/guardbot-eval/gemini_keys.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import awareness_context, config, db, memory  # noqa: E402

# ── The labelled extraction corpus ────────────────────────────────────────
# Every positive is an explicit request to be remembered, and the clause the
# server should store is the part after the trigger. Every negative is a message
# the detector must leave alone — including the two that would be dangerous if
# it did not: an ordinary claim of authority, and a request to remember an
# *action* rather than a fact.
POSITIVE: tuple[tuple[str, str], ...] = (
    ("یادت باشه من برنامه‌نویسم", "من برنامه‌نویسم"),
    ("یادت باشه که من فارسی دوست دارم", "من فارسی دوست دارم"),
    ("یادت بمونه من تهرانی‌ام", "من تهرانی‌ام"),
    ("به یاد داشته باش من ورزش می‌کنم", "من ورزش می‌کنم"),
    ("به یاد بسپار من صبح‌ها فعال‌ترم", "من صبح‌ها فعال‌ترم"),
    ("یادداشت کن من قهوه دوست دارم", "من قهوه دوست دارم"),
    ("remember that I prefer short answers", "I prefer short answers"),
    ("remember: I am a teacher", "I am a teacher"),
    ("note that I am a teacher", "I am a teacher"),
    ("keep in mind I am a teacher", "I am a teacher"),
    ("یادت باشه من توی گروه فلان ادمینم", "من توی گروه فلان ادمینم"),
    ("یادت باشه من کتاب بوف کور رو دوست دارم", "من کتاب بوف کور رو دوست دارم"),
)

NEGATIVE: tuple[str, ...] = (
    "",
    "سلام خوبی؟",
    "من ادمینم",
    "من مالک این گروه‌ام",
    "یادت نره بهم خبر بدی",
    "یادت نره فردا زنگ بزنی",
    "میلاد رو بن کن",
    "چی شده اینجا؟",
    "این لینک رو ببین",
    "قیمت چنده؟",
    "یادت باشه",
    "یادت باشه که",
    "remember that",
)


def _rnd(n: int, rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_letters + " ") for _ in range(n))


def measure_extraction() -> dict:
    true_positive = 0
    false_negative = 0
    false_positive = 0
    true_negative = 0
    misses: list[str] = []
    for text, clause in POSITIVE:
        found = memory.extract(text)
        if found and found["value"] == clause:
            true_positive += 1
        else:
            false_negative += 1
            misses.append(text)
    for text in NEGATIVE:
        if memory.extract(text) is None:
            true_negative += 1
        else:
            false_positive += 1
            misses.append(text)
    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive)
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative)
        else 0.0
    )
    return {
        "positives": len(POSITIVE),
        "negatives": len(NEGATIVE),
        "true_positive": true_positive,
        "false_negative": false_negative,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "misses": misses,
    }


def measure_storage(users: int, per_user: int, seed: int = 7) -> dict:
    """Insert ``users`` x ``per_user`` rows through the real write path and size
    the result. Uses the file-backed path so the size is real, then cleans up."""
    rng = random.Random(seed)
    path = f"/tmp/guardbot-memory-bench-{os.getpid()}.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    old = config.DB_PATH
    config.DB_PATH = path
    try:
        db.init()
        db.memory_reset()
        writes = []
        for u in range(users):
            for i in range(per_user):
                text = f"یادت باشه من نکتهٔ {i} برای کاربر {u} هستم {_rnd(20, rng)}"
                t = time.perf_counter()
                memory.remember({"id": u + 1, "is_bot": False}, 1, text)
                writes.append((time.perf_counter() - t) * 1000)
        db._conn.execute("VACUUM")
        size = os.path.getsize(path)
        rows = db.memory_count()
        writes.sort()
        return {
            "users": users,
            "per_user": per_user,
            "rows": rows,
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 2),
            "bytes_per_row": round(size / rows, 1) if rows else 0.0,
            "write_ms_p50": round(statistics.median(writes), 3),
            "write_ms_p95": round(writes[int(0.95 * len(writes))], 3),
            "projected_mb_at_3000_users": round(
                (size / rows) * 3000 * per_user / 1024 / 1024, 2
            )
            if rows
            else 0.0,
        }
    finally:
        db._conn = None
        config.DB_PATH = old
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def measure_retrieval(samples: int = 200) -> dict:
    times = []
    chars = []
    for u in range(samples):
        t = time.perf_counter()
        rows = memory.about(1, u + 1, limit=config.NEXUS_MEMORY_ITEMS)
        times.append((time.perf_counter() - t) * 1000)
        chars.append(len(memory.render(rows, budget=config.NEXUS_MEMORY_CHARS)))
    times.sort()
    return {
        "samples": samples,
        "retrieve_ms_p50": round(statistics.median(times), 4),
        "retrieve_ms_p95": round(times[int(0.95 * len(times))], 4),
        "block_chars_mean": round(statistics.mean(chars), 1),
        "block_chars_max": max(chars) if chars else 0,
        "block_budget": config.NEXUS_MEMORY_CHARS,
    }


def measure_model_calls() -> int:
    """Zero by construction: the module imports no model client."""
    import inspect

    source = inspect.getsource(memory)
    forbidden = ("gemini", "requests.", "httpx", "urllib", "socket")
    return sum(1 for token in forbidden if token in source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--users", type=int, default=3000)
    parser.add_argument("--per-user", type=int, default=30)
    args = parser.parse_args(argv)

    extraction = measure_extraction()
    storage = measure_storage(args.users, args.per_user)

    # Retrieval is measured against the storage benchmark's live database, which
    # has just been closed — so reopen an in-memory one with a sample of rows.
    db.init()
    db.memory_reset()
    for u in range(50):
        for i in range(config.NEXUS_MEMORY_ITEMS + 2):
            memory.remember(
                {"id": u + 1, "is_bot": False}, 1,
                f"یادت باشه من نکتهٔ {i} هستم",
            )
    retrieval = measure_retrieval()
    model_calls = measure_model_calls()

    report = {
        "extraction": extraction,
        "storage": storage,
        "retrieval": retrieval,
        "model_calls_in_source": model_calls,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("Nexus user memory — deterministic benchmark")
    print("=" * 60)
    print("EXTRACTION (explicit trigger detector)")
    print(
        f"  corpus            {extraction['positives']} positive / "
        f"{extraction['negatives']} negative"
    )
    print(
        f"  precision         {extraction['precision']:.4f}"
        f"   recall {extraction['recall']:.4f}"
    )
    print(
        f"  false positives   {extraction['false_positive']}"
        f"   false negatives {extraction['false_negative']}"
    )
    if extraction["misses"]:
        print(f"  misses            {extraction['misses']}")
    print()
    print("STORAGE (through the real write path)")
    print(
        f"  {storage['users']} users x {storage['per_user']} items = "
        f"{storage['rows']} rows, {storage['size_mb']} MB"
    )
    print(f"  bytes/row         {storage['bytes_per_row']}")
    print(
        f"  write             {storage['write_ms_p50']} ms p50 / "
        f"{storage['write_ms_p95']} ms p95"
    )
    print(
        f"  projected @3000   {storage['projected_mb_at_3000_users']} MB "
        f"(budget 200 MB)"
    )
    print()
    print("RETRIEVAL (the context block's read)")
    print(
        f"  read              {retrieval['retrieve_ms_p50']} ms p50 / "
        f"{retrieval['retrieve_ms_p95']} ms p95"
    )
    print(
        f"  block chars       mean {retrieval['block_chars_mean']} / "
        f"max {retrieval['block_chars_max']} (budget {retrieval['block_budget']})"
    )
    print()
    print(f"MODEL CALLS         {model_calls} (0 = none in the module's source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
