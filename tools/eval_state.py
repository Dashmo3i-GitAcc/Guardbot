#!/usr/bin/env python3
"""Score conversational state: what it reads, what it costs, what it stores.

Why this exists
---------------
Increment X claims a *bounded*, *deterministic* state layer that costs no request
and cannot grow into a transcript. Every one of those words is a measurement, and
this harness produces it. It is deterministic and offline: no Telegram, no model,
no network. The transitions are regexes and the store is SQLite, so everything
here is reproducible.

What it measures
----------------
* **transitions** — precision and recall of the deterministic reader over a
  labelled corpus, including the negatives that matter most (an ordinary message,
  a claim of authority, a request for an action must all change nothing).
* **storage** — one row per person, bytes per row, and the projected size at the
  owner's scale (3000 members) against the 200 MB budget.
* **lifecycle** — a scripted conversation with known ground truth: the row count
  at the end is what proves the store is one active task rather than a log.
* **concurrency** — the compare-and-swap refusal and the idempotent duplicate,
  counted rather than asserted.
* **sync cost** — the only work State adds to a chat turn: the bounded read and
  render, against the same with the feature off.
* **model calls** — asserted to be zero: the module imports no model client and
  no ``state`` workload exists.

    python tools/eval_state.py
    python tools/eval_state.py --json
    python tools/eval_state.py --users 3000
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

from app import config, db, state  # noqa: E402

# ── The labelled transition corpus ────────────────────────────────────────
# Every positive is a message that *does* state something about the interaction,
# and the transition and topic the reader should produce. Every negative is a
# message that must change nothing — including the three that would be dangerous
# if it did: an ordinary remark, a claim of authority, and a request for an
# action.
POSITIVE: tuple[tuple[str, str, str], ...] = (
    ("بیا مشکل لاگین بات رو درست کنیم", state.TRANSITION_ACTIVATE, "مشکل لاگین بات"),
    ("بریم سراغ مشکل پرداخت", state.TRANSITION_ACTIVATE, "مشکل پرداخت"),
    ("باید این باگ رو بررسی کنیم", state.TRANSITION_ACTIVATE, "باگ"),
    ("let's fix the login bug", state.TRANSITION_ACTIVATE, "login bug"),
    ("let's work on the deployment issue", state.TRANSITION_ACTIVATE, "deployment issue"),
    ("حل شد", state.TRANSITION_COMPLETE, ""),
    ("مشکل حل شد", state.TRANSITION_COMPLETE, ""),
    ("it's fixed", state.TRANSITION_COMPLETE, ""),
    ("بحث رو عوض کنیم", state.TRANSITION_RESET, ""),
    ("بریم سراغ یه چیز دیگه", state.TRANSITION_RESET, ""),
    ("خب الان قدم بعدی چیه؟", state.TRANSITION_CONTINUE, ""),
    ("ادامه بده", state.TRANSITION_CONTINUE, ""),
    ("کجا بودیم؟", state.TRANSITION_CONTINUE, ""),
)

NEGATIVE: tuple[str, ...] = (
    "",
    "   ",
    "سلام",
    "ممنون",
    "😂",
    "خوبی؟",
    "امروز هوا خوبه",
    "فکر کنم باید صبر کنیم",
    "برادرم برنامه‌نویس است",
    "من ادمینم",
    "من مالک این گروه‌ام",
    "اینو بن کن",
    "میلاد رو بن کن",
    "این لینک رو ببین https://example.com",
)

# ── The scripted lifecycle ────────────────────────────────────────────────
# A conversation with known ground truth. ``topic`` of ``None`` means the state
# is expected to be empty after the message.
SCRIPT: tuple[tuple[str, str, str | None], ...] = (
    ("بیا مشکل لاگین بات رو درست کنیم", state.TRANSITION_ACTIVATE, "مشکل لاگین بات"),
    ("خب الان قدم بعدی چیه؟", state.TRANSITION_CONTINUE, "مشکل لاگین بات"),
    ("مشکل لاگین از DNS هست؟", state.TRANSITION_UPDATE, "مشکل لاگین بات"),
    ("بریم سراغ مشکل پرداخت", state.TRANSITION_REPLACE, "مشکل پرداخت"),
    ("حل شد", state.TRANSITION_COMPLETE, None),
)


def _rnd(n: int, rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def measure_transitions() -> dict:
    """Precision and recall of the deterministic reader.

    Recall is about how much of a real task statement is understood; precision is
    about how much is *invented*. Both are reported, and the misses are listed by
    hand, because a false positive here would make Nexus believe the conversation
    is about something it is not.
    """
    true_positive = false_negative = false_positive = true_negative = 0
    misses: list[str] = []
    for text, transition, topic in POSITIVE:
        found = state.read(text)
        ok = bool(found) and found["transition"] == transition
        if ok and topic:
            ok = found.get("topic") == topic
        if ok:
            true_positive += 1
        else:
            false_negative += 1
            misses.append(f"{text} -> {found or 'nothing'}")
    for text in NEGATIVE:
        if state.read(text) is None:
            true_negative += 1
        else:
            false_positive += 1
            misses.append(f"{text} -> {state.read(text)}")
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


def measure_storage(users: int, seed: int = 7) -> dict:
    """Insert ``users`` rows through the real store and size the result.

    One row per person, because that is the design: the row *is* the bound. Uses
    the file-backed path so the size is real, then cleans up.
    """
    rng = random.Random(seed)
    path = f"/tmp/guardbot-state-bench-{os.getpid()}.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    old = config.DB_PATH
    old_max = config.NEXUS_STATE_MAX
    config.DB_PATH = path
    # The global ceiling is lifted for the measurement, and only here: this
    # benchmark is sizing a row, not exercising retention.
    config.NEXUS_STATE_MAX = max(1, users + 1)
    try:
        db.init()
        db.state_reset()
        writes = []
        for u in range(users):
            topic = f"مشکل شماره {u} {_rnd(12, rng)}"
            t = time.perf_counter()
            db.state_put(
                u + 1,
                1,
                topic=topic,
                goal=topic,
                question=f"آیا {_rnd(10, rng)} درست است؟",
                status=state.STATUS_ACTIVE,
                transition=state.TRANSITION_ACTIVATE,
                message_id=1,
            )
            writes.append((time.perf_counter() - t) * 1000)
        db._conn.execute("VACUUM")
        size = os.path.getsize(path)
        rows = db.state_count()
        writes.sort()
        return {
            "users": users,
            "rows": rows,
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 2),
            "bytes_per_row": round(size / rows, 1) if rows else 0.0,
            "write_ms_p50": round(statistics.median(writes), 3),
            "write_ms_p95": round(writes[int(0.95 * len(writes))], 3),
            "projected_mb_at_3000_users": (
                round((size / rows) * 3000 / 1024 / 1024, 2) if rows else 0.0
            ),
        }
    finally:
        db._conn = None
        config.DB_PATH = old
        config.NEXUS_STATE_MAX = old_max
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def measure_lifecycle() -> dict:
    """Replay the scripted conversation and count what the store did with it.

    The row count at the end is the point: after a task, a continuation, an open
    question, a replacement and a completion, there is no active state — not five.
    """
    db.init()
    db.state_reset()
    state.reset_state()
    counts: dict[str, int] = {}
    wrong: list[str] = []
    latencies: list[float] = []
    for index, (text, transition, topic) in enumerate(SCRIPT, start=1):
        t = time.perf_counter()
        result = asyncio.run(
            state.observe(
                {"id": 1, "is_bot": False}, 1, text, message_id=index
            )
        )
        latencies.append((time.perf_counter() - t) * 1000)
        counts[transition] = counts.get(transition, 0) + 1
        if not result or result.get("transition") != transition:
            wrong.append(f"{text}: expected {transition}, got {result}")
        row = db.state_get(1, 1)
        got_topic = row["topic"] if row else None
        if got_topic != topic:
            wrong.append(f"{text}: expected topic {topic!r}, got {got_topic!r}")
    latencies.sort()
    return {
        "messages": len(SCRIPT),
        "transitions": counts,
        "rows_final": db.state_count(),
        "observe_ms_p50": round(statistics.median(latencies), 3),
        "observe_ms_p95": round(latencies[int(0.95 * len(latencies))], 3),
        "ground_truth_mismatches": wrong,
    }


def measure_concurrency() -> dict:
    """The compare-and-swap refusal and the idempotent duplicate, counted.

    A stale worker's write must be refused rather than clobbering a newer state,
    and a duplicate delivery must be a no-op rather than a second transition.
    """
    db.init()
    db.state_reset()
    state.reset_state()
    asyncio.run(
        state.observe({"id": 1, "is_bot": False}, 1, "بیا مشکل لاگین رو درست کنیم", message_id=1)
    )
    stale = db.state_get(1, 1)
    asyncio.run(
        state.observe({"id": 1, "is_bot": False}, 1, "بریم سراغ مشکل پرداخت", message_id=2)
    )
    refused = state._write(
        1,
        1,
        stale,
        topic="مشکل قدیمی",
        goal="مشکل قدیمی",
        question="",
        status=state.STATUS_ACTIVE,
        transition=state.TRANSITION_REPLACE,
        message_id=3,
    )
    before = db.state_get(1, 1)
    duplicate = asyncio.run(
        state.observe({"id": 1, "is_bot": False}, 1, "بریم سراغ مشکل پرداخت", message_id=2)
    )
    after = db.state_get(1, 1)
    return {
        "stale_write_refused": refused is None,
        "state_after_stale_write": before["topic"] if before else None,
        "duplicate_is_noop": bool(duplicate and duplicate.get("applied") is False),
        "version_unchanged_on_duplicate": (
            before["version"] == after["version"] if before and after else False
        ),
        "rows": db.state_count(),
    }


def measure_sync_cost(samples: int = 500) -> dict:
    """The only work State adds to a chat turn: the bounded read and render.

    Nothing else is synchronous. Extraction and the write happen in a background
    task, so the comparison that matters is this read against the same read with
    the feature off — which is a single configuration check and no query.
    """
    db.init()
    db.state_reset()
    asyncio.run(
        state.observe({"id": 1, "is_bot": False}, 1, "بیا مشکل لاگین بات رو درست کنیم", message_id=1)
    )
    on: list[float] = []
    for _ in range(samples):
        t = time.perf_counter()
        row = state.current(1, 1, text="قدم بعدی چیه")
        state.render(row, budget=config.NEXUS_STATE_CHARS)
        on.append((time.perf_counter() - t) * 1000)
    old = config.NEXUS_STATE_ENABLED
    config.NEXUS_STATE_ENABLED = False
    off: list[float] = []
    for _ in range(samples):
        t = time.perf_counter()
        state.current(1, 1, text="قدم بعدی چیه")
        off.append((time.perf_counter() - t) * 1000)
    config.NEXUS_STATE_ENABLED = old
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
    """The read the context block makes, and the characters it adds."""
    db.init()
    db.state_reset()
    for u in range(samples):
        asyncio.run(
            state.observe(
                {"id": u + 1, "is_bot": False},
                1,
                f"بیا مشکل شماره {u} رو درست کنیم",
                message_id=1,
            )
        )
    times = []
    chars = []
    for u in range(samples):
        t = time.perf_counter()
        row = state.current(1, u + 1, text="قدم بعدی چیه")
        times.append((time.perf_counter() - t) * 1000)
        chars.append(len(state.render(row, budget=config.NEXUS_STATE_CHARS)))
    times.sort()
    return {
        "samples": samples,
        "retrieve_ms_p50": round(statistics.median(times), 4),
        "retrieve_ms_p95": round(times[int(0.95 * len(times))], 4),
        "block_chars_mean": round(statistics.mean(chars), 1),
        "block_chars_max": max(chars) if chars else 0,
        "block_budget": config.NEXUS_STATE_CHARS,
    }


def measure_model_calls() -> int:
    """Zero by construction: the module imports no model client, and there is no
    ``state`` workload to call."""
    import inspect

    source = inspect.getsource(state)
    forbidden = ("gemini", "requests.", "httpx", "urllib", "socket")
    in_source = sum(1 for token in forbidden if token in source)
    workloads = {spec["workload"] for spec in config.GEMINI_POOLS}
    return in_source + (1 if "state" in workloads else 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--users", type=int, default=3000)
    args = parser.parse_args(argv)

    transitions = measure_transitions()
    storage = measure_storage(args.users)
    lifecycle = measure_lifecycle()
    concurrency = measure_concurrency()
    sync_cost = measure_sync_cost()
    retrieval = measure_retrieval()
    model_calls = measure_model_calls()

    report = {
        "transitions": transitions,
        "storage": storage,
        "lifecycle": lifecycle,
        "concurrency": concurrency,
        "sync_cost": sync_cost,
        "retrieval": retrieval,
        "model_calls_in_source": model_calls,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("Nexus conversational state — deterministic benchmark")
    print("=" * 60)
    print("TRANSITIONS (the deterministic reader)")
    print(
        f"  corpus            {transitions['positives']} positive / "
        f"{transitions['negatives']} negative"
    )
    print(
        f"  precision         {transitions['precision']:.4f}  "
        f"recall {transitions['recall']:.4f}"
    )
    print(
        f"  true/false        +{transitions['true_positive']} "
        f"-{transitions['false_negative']} "
        f"FP {transitions['false_positive']}"
    )
    for miss in transitions["misses"]:
        print(f"  miss              {miss}")
    print()
    print("STORAGE (one active row per person, through the real store)")
    print(f"  rows              {storage['rows']}")
    print(f"  size              {storage['size_mb']} MB")
    print(f"  bytes/row         {storage['bytes_per_row']}")
    print(
        f"  projected @3000   {storage['projected_mb_at_3000_users']} MB "
        "(budget 200 MB)"
    )
    print(
        f"  write ms p50/p95  {storage['write_ms_p50']} / {storage['write_ms_p95']}"
    )
    print()
    print("LIFECYCLE (a scripted conversation, ground truth known)")
    print(f"  messages          {lifecycle['messages']}")
    print(f"  transitions       {lifecycle['transitions']}")
    print(f"  rows at the end   {lifecycle['rows_final']}")
    print(
        f"  observe ms p50/p95 {lifecycle['observe_ms_p50']} / "
        f"{lifecycle['observe_ms_p95']}"
    )
    if lifecycle["ground_truth_mismatches"]:
        print(f"  mismatches        {lifecycle['ground_truth_mismatches']}")
    print()
    print("CONCURRENCY")
    print(f"  stale write refused   {concurrency['stale_write_refused']}")
    print(f"  duplicate is a no-op  {concurrency['duplicate_is_noop']}")
    print(f"  rows                  {concurrency['rows']}")
    print()
    print("SYNC COST (the only work state adds to a chat turn)")
    print(
        f"  read+render ms p50/p95 {sync_cost['read_ms_p50']} / "
        f"{sync_cost['read_ms_p95']}"
    )
    print(
        f"  disabled ms p50/p95    {sync_cost['disabled_ms_p50']} / "
        f"{sync_cost['disabled_ms_p95']}"
    )
    print()
    print("RETRIEVAL (the context block's read)")
    print(
        f"  retrieve ms p50/p95 {retrieval['retrieve_ms_p50']} / "
        f"{retrieval['retrieve_ms_p95']}"
    )
    print(
        f"  block chars mean/max {retrieval['block_chars_mean']} / "
        f"{retrieval['block_chars_max']} (budget {retrieval['block_budget']})"
    )
    print()
    print(f"MODEL CALLS         {model_calls} (0 = none in the module's source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
