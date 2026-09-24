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
import asyncio
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

# ── The automatic-extraction corpus ───────────────────────────────────────
# Ordinary conversation, no trigger word. Each positive names the slot and value
# the deterministic layer must produce; each negative is a message it must leave
# alone — and the negatives are the interesting half, because the failure that
# matters is not a missed fact but an invented one.
AUTO_POSITIVE: tuple[tuple[str, str, str], ...] = (
    ("من یه برنامه‌نویس هستم", "identity.occupation", "برنامه نویس"),
    ("من برنامه‌نویسم و بیشتر با Python کار می‌کنم", "identity.programming", "Python"),
    ("I have switched to Python", "identity.programming", "Python"),
    ("زبانم فارسیه", "identity.language", "فارسی"),
    ("من شنا بلدم", "identity.skill", "شنا"),
    ("دارم روی یه ربات تلگرام کار می‌کنم", "identity.project", "یه ربات تلگرام"),
    ("منو رضا صدا کن", "identity.name", "رضا"),
    ("من COD بازی می‌کنم", "interest.gaming", "COD"),
    ("من راک گوش می‌دم", "interest.music", "راک"),
    ("فیلم ترسناک می‌بینم", "interest.movies", "ترسناک"),
    ("به موسیقی علاقه دارم", "interest.topic", "موسیقی"),
    ("من جواب‌های کوتاه رو بیشتر دوست دارم", "preference.answers", "concise"),
    ("I prefer detailed answers", "preference.answers", "detailed"),
    ("من خودمونی حرف زدن رو دوست دارم", "preference.style", "informal"),
    ("من شوخی‌های بزرگسال دوست دارم", "humor.adult", "preferred"),
    ("من طنز و کنایه دوست دارم", "humor.sarcasm", "preferred"),
)

AUTO_NEGATIVE: tuple[str, ...] = (
    "",
    "امروز خیلی خسته‌ام",
    "الان حالم خوب نیست",
    "I'm tired today",
    "برادرم برنامه‌نویس است",
    "My brother is a programmer",
    "دوستام پایتون کار می‌کنن",
    "با چه زبانی کار می‌کنی؟",
    "برنامه‌نویسی سخته؟",
    "جواب کوتاه بده",
    "امروز هوا خوبه",
    "فکر کنم باید یه چیز دیگه امتحان کنیم",
    "من خیلی باهوشم",
    "این پروژه رو با Go می‌خوام بنویسم",
    "من اینو دوست دارم https://example.com",
)

# A mixed stream of ordinary group messages, used to size the gate: how often a
# message could hold durable self-information at all, and how often the rules
# already know the answer. The model seam is reached only by the difference.
GATE_CORPUS: tuple[str, ...] = (
    "سلام بچه‌ها",
    "امروز هوا خوبه",
    "من یه برنامه‌نویس هستم",
    "کی آنلاینه؟",
    "برادرم معلمه",
    "من COD بازی می‌کنم",
    "این لینک رو ببین",
    "فردا میام",
    "من اهل شیرازم",
    "قیمت چنده؟",
    "هاها خیلی خنده‌دار بود",
    "داداش چطوری",
    "من جواب‌های کوتاه رو بیشتر دوست دارم",
    "چی شده اینجا؟",
    "من بیشتر با Go کار می‌کنم",
    "خواهرم پزشکه",
    "الان حالم خوب نیست",
    "به موسیقی علاقه دارم",
)

# A scripted conversation with known ground truth, so acceptance, rejection,
# duplication and replacement can be counted rather than asserted. Each entry is
# (message, "accept" | "reject" | "duplicate" | "replace").
SCRIPT: tuple[tuple[str, str], ...] = (
    ("من یه برنامه‌نویس هستم", "accept"),
    ("من بیشتر با JavaScript کار می‌کنم", "accept"),
    ("من بیشتر با JavaScript کار می‌کنم", "duplicate"),
    ("رفتم روی Python", "replace"),
    ("امروز خستم", "reject"),
    ("با چی کار می‌کنی؟", "reject"),
    ("برادرم برنامه‌نویس است", "reject"),
    ("من COD بازی می‌کنم", "accept"),
    ("من COD بازی می‌کنم", "duplicate"),
    ("من جواب‌های کوتاه رو بیشتر دوست دارم", "accept"),
    ("این پروژه رو با Go می‌خوام بنویسم", "reject"),
    ("منو رضا صدا کن", "accept"),
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
    old_max = config.NEXUS_MEMORY_MAX
    config.DB_PATH = path
    # The global ceiling is lifted for the measurement, and only here: this
    # benchmark is sizing a row, not exercising retention. Left at its production
    # value it would fire the whole-table prune every ``PRUNE_EVERY`` writes once
    # the table passed 50000 rows, which is correct behaviour but turns a sizing
    # run into a retention run and takes minutes instead of seconds.
    config.NEXUS_MEMORY_MAX = max(1, users * per_user + 1)
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
        config.NEXUS_MEMORY_MAX = old_max
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def measure_automatic() -> dict:
    """Precision and recall of the automatic layer over ordinary conversation.

    Recall is about how much durable fact is captured; precision is about how
    much is *invented*. Both are reported, and the misses are listed by hand,
    because a false positive here is a wrong belief about a real person.
    """
    true_positive = 0
    false_negative = 0
    false_positive = 0
    true_negative = 0
    misses: list[str] = []
    for text, slot, value in AUTO_POSITIVE:
        found = {c["slot"]: c["value"] for c in memory.automatic(text)}
        if found.get(slot) == value:
            true_positive += 1
        else:
            false_negative += 1
            misses.append(f"{text} -> {found or 'nothing'}")
    for text in AUTO_NEGATIVE:
        if memory.automatic(text) == []:
            true_negative += 1
        else:
            false_positive += 1
            misses.append(f"{text} -> {memory.automatic(text)}")
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
        "positives": len(AUTO_POSITIVE),
        "negatives": len(AUTO_NEGATIVE),
        "true_positive": true_positive,
        "false_negative": false_negative,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "misses": misses,
    }


def measure_gate() -> dict:
    """How much of an ordinary stream could ever reach the model seam.

    The deterministic layer is free; the gate is what keeps the provider out of
    the ordinary path. Two numbers, and the second is what matters: a message
    that the rules already answered never reaches the seam either.
    """
    known = sum(1 for text in GATE_CORPUS if memory.automatic(text))
    self_info = sum(1 for text in GATE_CORPUS if memory._looks_like_self_info(text))
    to_seam = sum(
        1
        for text in GATE_CORPUS
        if memory._looks_like_self_info(text) and not memory.automatic(text)
    )
    total = len(GATE_CORPUS)
    return {
        "messages": total,
        "deterministic_candidates": known,
        "self_information": self_info,
        "reaching_model_seam": to_seam,
        "reaching_model_pct": round(100.0 * to_seam / total, 1) if total else 0.0,
        "model_calls": 0,
    }


def measure_lifecycle() -> dict:
    """Replay a scripted conversation and count what the store did with it.

    Acceptance, rejection, duplication and replacement are measured rather than
    asserted: the script has known ground truth, and the row count at the end is
    the thing that proves the store is a fact set rather than a log.
    """
    db.init()
    db.memory_reset()
    db.signal_reset()
    accepted = rejected = duplicate = replaced = 0
    wrong: list[str] = []
    seen: dict[str, str] = {}
    latencies: list[float] = []
    for text, expectation in SCRIPT:
        before = dict(seen)
        t = time.perf_counter()
        found = asyncio.run(
            memory.observe({"id": 1, "is_bot": False}, 1, text)
        )
        latencies.append((time.perf_counter() - t) * 1000)
        for row in memory.about(1, 1):
            seen[str(row["key"])] = str(row["value"])
        if found:
            accepted += 1
            kind = "duplicate" if before == seen else "replace"
            if kind == "duplicate":
                duplicate += 1
            else:
                replaced += 1
            if expectation not in ("accept", kind):
                wrong.append(f"{text}: expected {expectation}, got {kind}")
        else:
            rejected += 1
            if expectation != "reject":
                wrong.append(f"{text}: expected {expectation}, got reject")
    latencies.sort()
    return {
        "messages": len(SCRIPT),
        "accepted": accepted,
        "rejected": rejected,
        "duplicates": duplicate,
        "replacements": replaced,
        "accept_pct": round(100.0 * accepted / len(SCRIPT), 1),
        "reject_pct": round(100.0 * rejected / len(SCRIPT), 1),
        "rows_final": db.memory_count(),
        "observe_ms_p50": round(statistics.median(latencies), 3),
        "observe_ms_p95": round(latencies[int(0.95 * len(latencies))], 3),
        "ground_truth_mismatches": wrong,
    }


def measure_sync_cost(samples: int = 500) -> dict:
    """The only work memory adds to a chat turn: the bounded read and render.

    Nothing else is synchronous. Extraction, the counters, the writes and the
    provider call all happen in a background task, so the comparison that matters
    is this read against the same read with the feature off — which is a single
    configuration check and no query.
    """
    db.init()
    db.memory_reset()
    for i in range(config.NEXUS_MEMORY_ITEMS):
        memory.remember({"id": 1, "is_bot": False}, 1, f"یادت باشه من نکتهٔ {i} هستم")
    on: list[float] = []
    for _ in range(samples):
        t = time.perf_counter()
        rows = memory.about(
            1, 1, limit=config.NEXUS_MEMORY_ITEMS, topic="نکته"
        )
        memory.render(rows, budget=config.NEXUS_MEMORY_CHARS)
        on.append((time.perf_counter() - t) * 1000)
    old = config.NEXUS_MEMORY_ENABLED
    config.NEXUS_MEMORY_ENABLED = False
    off: list[float] = []
    for _ in range(samples):
        t = time.perf_counter()
        memory.about(1, 1, limit=config.NEXUS_MEMORY_ITEMS, topic="نکته")
        off.append((time.perf_counter() - t) * 1000)
    config.NEXUS_MEMORY_ENABLED = old
    on.sort()
    off.sort()
    return {
        "samples": samples,
        "read_ms_p50": round(statistics.median(on), 4),
        "read_ms_p95": round(on[int(0.95 * len(on))], 4),
        "disabled_ms_p50": round(statistics.median(off), 4),
        "disabled_ms_p95": round(off[int(0.95 * len(off))], 4),
    }


def measure_retrieval(samples: int = 200) -> dict:
    """The read the context block makes, and the characters it adds.

    Seeds its own rows so the caller's ordering cannot change the number.
    """
    db.init()
    db.memory_reset()
    for u in range(samples):
        for i in range(config.NEXUS_MEMORY_ITEMS + 2):
            memory.remember(
                {"id": u + 1, "is_bot": False}, 1, f"یادت باشه من نکتهٔ {i} هستم"
            )
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
    automatic = measure_automatic()
    gate = measure_gate()
    storage = measure_storage(args.users, args.per_user)
    sync_cost = measure_sync_cost()
    lifecycle = measure_lifecycle()
    retrieval = measure_retrieval()
    model_calls = measure_model_calls()

    report = {
        "explicit_extraction": extraction,
        "automatic_extraction": automatic,
        "gate": gate,
        "storage": storage,
        "sync_cost": sync_cost,
        "lifecycle": lifecycle,
        "retrieval": retrieval,
        "model_calls_in_source": model_calls,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("Nexus user memory — deterministic benchmark")
    print("=" * 60)
    print("EXPLICIT EXTRACTION (trigger detector)")
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
    print("AUTOMATIC EXTRACTION (ordinary conversation)")
    print(
        f"  corpus            {automatic['positives']} positive / "
        f"{automatic['negatives']} negative"
    )
    print(
        f"  precision         {automatic['precision']:.4f}"
        f"   recall {automatic['recall']:.4f}"
    )
    print(
        f"  false positives   {automatic['false_positive']}"
        f"   false negatives {automatic['false_negative']}"
    )
    if automatic["misses"]:
        print(f"  misses            {automatic['misses']}")
    print()
    print("GATE (how little reaches the model seam)")
    print(
        f"  stream            {gate['messages']} ordinary messages"
    )
    print(
        f"  rules answered    {gate['deterministic_candidates']}"
        f"   self-information {gate['self_information']}"
    )
    print(
        f"  to model seam     {gate['reaching_model_seam']}"
        f" ({gate['reaching_model_pct']}%)"
        f"   provider calls {gate['model_calls']}"
    )
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
    print("LIFECYCLE (a scripted conversation, ground truth known)")
    print(
        f"  accepted          {lifecycle['accepted']} ({lifecycle['accept_pct']}%)"
        f"   rejected {lifecycle['rejected']} ({lifecycle['reject_pct']}%)"
    )
    print(
        f"  duplicates        {lifecycle['duplicates']}"
        f"   replacements {lifecycle['replacements']}"
    )
    print(f"  rows at the end   {lifecycle['rows_final']}")
    print(
        f"  observe           {lifecycle['observe_ms_p50']} ms p50 / "
        f"{lifecycle['observe_ms_p95']} ms p95 (off the answer path)"
    )
    if lifecycle["ground_truth_mismatches"]:
        print(f"  mismatches        {lifecycle['ground_truth_mismatches']}")
    print()
    print("SYNC COST (the only work memory adds to a chat turn)")
    print(
        f"  read + render     {sync_cost['read_ms_p50']} ms p50 / "
        f"{sync_cost['read_ms_p95']} ms p95"
    )
    print(
        f"  memory disabled   {sync_cost['disabled_ms_p50']} ms p50 / "
        f"{sync_cost['disabled_ms_p95']} ms p95"
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
