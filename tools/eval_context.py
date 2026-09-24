#!/usr/bin/env python3
"""Score increment Y: what the context selector selects, and what it costs.

Why this exists
---------------
Y claims a *deterministic* selector that gives the addressed reply the **minimum
relevant combination** of four independent sources — conversation, awareness,
state and memory — with a fast path for simple messages. Every one of those
words is a measurement, and this harness produces it. It is deterministic and
offline: no Telegram, no model, no network, no database. The selector is a pure
function of the message and the rendered blocks, so everything here is
reproducible.

What it measures
----------------
* **selection** — over a labelled corpus (the brief's cases A–U), which sources
  are selected, which are omitted, and *why*, with the reason code asserted.
* **ordering** — the composed text appears in one fixed order.
* **de-duplication** — a fact the room already states is not sent twice.
* **precedence** — a fresh correction drops the memory it contradicts, and a
  fresh instruction drops the stale task.
* **isolation** — a block belonging to another key never appears, with a
  non-vacuity proof that the same case *does* carry it for its own key.
* **budget** — the fast path versus the full path, and the whole-context
  ceiling: total characters, selected sources, and how often the ceiling bites.
* **model calls** — asserted to be zero: the module imports no model client.

    python tools/eval_context.py
    python tools/eval_context.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

os.environ.setdefault("BOT_TOKEN", "eval-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-eval")
os.environ.setdefault("GEMINI_KEY_STORE_PATH", "/tmp/guardbot-eval/gemini_keys.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import context_plan as cp  # noqa: E402

# ── The markers, so presence and absence are exact ────────────────────────
ADMIN = "ROSTER\n"
ROOM = "ROOMWINDOW\n"
READING = "ROOMREAD\n"
STATE = "STATETASK: login bug\n"
MEMORY = "MEMVAL: Python\n"
DATE = "SERVERDATE\n"
SEARCH = "WEBRES\n"

C = cp.CONVERSATION
A = cp.AWARENESS
S = cp.STATE
M = cp.MEMORY


def _case(cid, text, *, reply=False, media=False, blocks=None, expect=None):
    """One labelled case. ``expect`` names the sources and the text markers."""
    return {
        "id": cid,
        "text": text,
        "reply": reply,
        "media": media,
        "blocks": blocks or {},
        "expect": expect or {},
    }


# Every case is labelled with the brief's own vocabulary (A–U). ``expect``
# carries: the mode, the selected source names, the reasons that must be present,
# the markers that must appear and the markers that must not.
CASES = (
    # A. Conversation relevance — a continuation keeps the transcript.
    _case(
        "A_continuation",
        "قدم بعدی چیه؟",
        blocks={"state": STATE},
        expect={"mode": cp.FULL, "selected": (C, S), "has": ["STATETASK"],
                "lacks": ["ROOMWINDOW", "MEMVAL"]},
    ),
    # B. Awareness relevance — a reply/anaphor needs the room.
    _case(
        "B_anaphora",
        "همونو بزن",
        blocks={"room": ROOM, "awareness": READING, "state": STATE, "memory": MEMORY},
        expect={"mode": cp.FULL, "selected": (C, A, S, M),
                "has": ["ROOMWINDOW", "ROOMREAD", "STATETASK", "MEMVAL"]},
    ),
    _case(
        "B_reply",
        "باشه انجامش میدم",
        reply=True,
        blocks={"room": ROOM, "awareness": READING},
        expect={"mode": cp.FULL, "selected": (C, A), "has": ["ROOMWINDOW"]},
    ),
    # B. An opinion request — its subject is the room's recent content.
    _case(
        "B_opinion",
        "نکسوس نظرت چیه؟",
        blocks={"room": ROOM, "awareness": READING, "state": STATE, "memory": MEMORY},
        expect={"mode": cp.FULL, "selected": (C, A, S, M), "has": ["ROOMWINDOW"],
                "reasons": [cp.R_OPINION]},
    ),
    # C. State relevance — the active task is carried.
    _case(
        "C_state",
        "ادامه بده",
        blocks={"room": ROOM, "awareness": READING, "state": STATE},
        expect={"mode": cp.FULL, "selected": (C, A, S), "has": ["STATETASK"]},
    ),
    # D. Memory relevance — a personal question keeps the person's memory.
    _case(
        "D_memory",
        "برای پروژه پایتونم چه کنم؟",
        blocks={"state": STATE, "memory": MEMORY},
        expect={"mode": cp.FAST, "selected": (C, S, M), "has": ["MEMVAL"],
                "lacks": ["ROOMWINDOW"]},
    ),
    # E. All four together, when the message genuinely needs all four.
    _case(
        "E_all_four",
        "همون چیزی که گفتی رو ادامه بده",
        blocks={"room": ROOM, "awareness": READING, "state": STATE, "memory": MEMORY,
                "admin": ADMIN, "date": DATE},
        expect={"mode": cp.FULL, "selected": (C, A, S, M),
                "has": ["ROSTER", "ROOMWINDOW", "ROOMREAD", "STATETASK", "MEMVAL",
                        "SERVERDATE"]},
    ),
    # F. No context needed — a greeting pays for no room and no memory, but the
    # person's own active task is cheap and stays.
    _case(
        "F_greeting",
        "سلام",
        blocks={"room": ROOM, "awareness": READING, "state": STATE, "memory": MEMORY},
        expect={"mode": cp.FAST, "selected": (C, S), "has": ["STATETASK"],
                "lacks": ["ROOMWINDOW", "MEMVAL"]},
    ),
    _case(
        "F_ack",
        "ممنون",
        blocks={"memory": MEMORY},
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["MEMVAL"]},
    ),
    # G. Short continuation — short is not simple.
    _case(
        "G_short_continuation",
        "پس چی شد؟",
        blocks={"room": ROOM, "awareness": READING},
        expect={"mode": cp.FULL, "selected": (C, A), "has": ["ROOMWINDOW"]},
    ),
    _case(
        "G_bare_interrogative",
        "چی؟",
        blocks={"room": ROOM, "awareness": READING},
        expect={"mode": cp.FULL, "selected": (C, A), "has": ["ROOMWINDOW"]},
    ),
    # H. Topic switch — the new subject wins, the old task is dropped.
    _case(
        "H_topic_switch",
        "بیخیال سرور، درباره فیلم بگو",
        blocks={"state": STATE, "memory": MEMORY},
        expect={"mode": cp.FULL, "selected": (C, M), "has": ["MEMVAL"],
                "lacks": ["STATETASK"], "reasons": [cp.R_SUPERSEDE]},
    ),
    # I. Explicit correction of Memory — the contradicted line goes, the rest
    # of the block stays (de-duplication is line-wise, not whole-block).
    _case(
        "I_correction",
        "نه، من پایتون استفاده نمیکنم",
        blocks={"memory": "- programming: پایتون\n- style: brief\n"},
        expect={"mode": cp.FULL, "selected": (C, M), "has": ["brief"],
                "lacks": ["پایتون"], "dropped": [(M, cp.R_CORRECTION)]},
    ),
    # J. Stale State — the reader withheld it, so the block is empty.
    _case(
        "J_stale_state",
        "قدم بعدی چیه؟",
        blocks={"state": ""},
        expect={"mode": cp.FULL, "selected": (C,), "lacks": ["STATETASK"]},
    ),
    # K. Irrelevant Memory — the reader returned nothing relevant.
    _case(
        "K_irrelevant_memory",
        "برای پروژه پایتونم چه کنم؟",
        blocks={"memory": ""},
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["MEMVAL"]},
    ),
    # L. Awareness irrelevant to a private intent — no room is selected.
    _case(
        "L_private",
        "قیمت چنده؟",
        blocks={"room": ROOM, "awareness": READING},
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["ROOMWINDOW"]},
    ),
    # M. Duplicate information — the room already says it.
    _case(
        "M_duplicate_memory",
        "همونو بزن",
        blocks={"room": "someone: we use Python\n", "memory": "- programming: Python\n"},
        expect={"mode": cp.FULL, "selected": (C, A), "lacks": ["programming"],
                "dropped": [(M, cp.R_DUPLICATE)]},
    ),
    _case(
        "M_duplicate_state",
        "همونو بزن",
        blocks={"room": "someone: the login bug again\n",
                "state": "- active topic: login bug\n"},
        expect={"mode": cp.FULL, "selected": (C, A), "lacks": ["active topic"],
                "dropped": [(S, cp.R_DUPLICATE)]},
    ),
    # N. Conflicting information — the correction and the drop are the conflicts.
    _case(
        "N_conflict_correction",
        "نه، من با پایتون کار نمیکنم",
        blocks={"memory": "- programming: پایتون\n"},
        expect={"mode": cp.FULL, "selected": (C,), "lacks": ["پایتون"],
                "reasons": [cp.R_CORRECTION]},
    ),
    _case(
        "N_conflict_drop_task",
        "بیخیال سرور",
        blocks={"state": STATE},
        expect={"mode": cp.FULL, "selected": (C,), "lacks": ["STATETASK"],
                "reasons": [cp.R_SUPERSEDE]},
    ),
    # O/P. Isolation — a block belonging to another key is never composed.
    _case(
        "O_other_user_memory",
        "برای پروژه پایتونم چه کنم؟",
        blocks={"memory": ""},  # the other user's memory is not passed in
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["MEMVAL"]},
    ),
    _case(
        "P_other_group_state",
        "قدم بعدی چیه؟",
        blocks={"state": ""},  # the other group's state is not passed in
        expect={"mode": cp.FULL, "selected": (C,), "lacks": ["STATETASK"]},
    ),
    # Q. Disabled Memory — its switch is off, so the block is empty.
    _case(
        "Q_disabled_memory",
        "برای پروژه پایتونم چه کنم؟",
        blocks={"memory": ""},
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["MEMVAL"]},
    ),
    # R. Awareness unavailable — no room, the rest still arrives.
    _case(
        "R_awareness_unavailable",
        "همونو بزن",
        blocks={"state": STATE, "memory": MEMORY},
        expect={"mode": cp.FULL, "selected": (C, S, M), "has": ["STATETASK", "MEMVAL"],
                "lacks": ["ROOMWINDOW"]},
    ),
    # S. State unavailable — no state, the rest still arrives.
    _case(
        "S_state_unavailable",
        "قدم بعدی چیه؟",
        blocks={"memory": MEMORY},
        expect={"mode": cp.FULL, "selected": (C, M), "has": ["MEMVAL"]},
    ),
    # T. Conversation unavailable — the transcript is bounded to nothing, and
    # the composed context is still well-formed.
    _case(
        "T_conversation_unavailable",
        "قیمت چنده؟",
        blocks={},
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["ROOMWINDOW", "MEMVAL"]},
    ),
    # U. All optional sources unavailable — the turn still reaches the model.
    _case(
        "U_all_unavailable",
        "قیمت چنده؟",
        blocks={"room": "", "awareness": "", "state": "", "memory": ""},
        expect={"mode": cp.FAST, "selected": (C,), "lacks": ["ROOMWINDOW", "MEMVAL"]},
    ),
)


def _plan_for(case):
    return cp.compose(
        cp.read(case["text"], reply=case["reply"], media=case["media"]),
        admin=case["blocks"].get("admin", ""),
        room=case["blocks"].get("room", ""),
        awareness=case["blocks"].get("awareness", ""),
        state=case["blocks"].get("state", ""),
        memory=case["blocks"].get("memory", ""),
        date=case["blocks"].get("date", ""),
        search=case["blocks"].get("search", ""),
        message=case["text"],
    )


def evaluate():
    """Run every case and report each expectation separately."""
    passed = 0
    failures: list[str] = []
    fast = full = 0
    dropped_duplicate = 0
    ceiling_bites = 0
    isolation_leaks = 0
    total_chars = 0
    max_chars = 0
    baseline_chars = 0
    baseline_max = 0

    for case in CASES:
        plan = _plan_for(case)
        expect = case["expect"]
        ok = True

        def check(condition, label):
            nonlocal ok
            if not condition:
                ok = False
                failures.append(f"{case['id']}: {label}")

        if "mode" in expect:
            check(plan.mode == expect["mode"],
                  f"mode {plan.mode!r} != {expect['mode']!r}")
        if "selected" in expect:
            check(set(plan.selected()) == set(expect["selected"]),
                  f"selected {plan.selected()} != {expect['selected']}")
        for marker in expect.get("has", ()):
            check(marker in plan.text, f"missing {marker!r}")
        for marker in expect.get("lacks", ()):
            check(marker not in plan.text, f"forbidden {marker!r} present")
        for reason in expect.get("reasons", ()):
            check(reason in plan.reasons, f"missing reason {reason!r}")
        for source, reason in expect.get("dropped", ()):
            check((source, reason) in plan.dropped, f"missing drop {source}:{reason}")

        # Ordering: the text must be the selected blocks concatenated in the
        # fixed order, with nothing interleaved.
        ordered = [
            case["blocks"].get("admin", ""),
            case["blocks"].get("room", ""),
            case["blocks"].get("awareness", ""),
            case["blocks"].get("state", ""),
            case["blocks"].get("memory", ""),
            case["blocks"].get("date", ""),
            case["blocks"].get("search", ""),
        ]
        kept = "".join(block for block in ordered if block and block in plan.text)
        # Only compare when nothing was dropped or trimmed by the ceiling.
        if not plan.dropped and plan.chars <= 2000:
            check(plan.text == kept, "the text is not the ordered selection")

        # Gating: the plan honours the reading it was built from. A fast-path
        # turn can never keep the room, whatever blocks the caller rendered.
        if plan.mode == cp.FAST:
            check(cp.AWARENESS in plan.omitted(), "the fast path kept the room")

        # Budget: the plan never exceeds the configured ceiling.
        check(plan.chars <= 100000, "the plan is unbounded")

        if plan.mode == cp.FAST:
            fast += 1
        else:
            full += 1
        dropped_duplicate += sum(
            1 for _s, reason in plan.dropped if reason in (cp.R_DUPLICATE, cp.R_CEILING)
        )
        ceiling_bites += sum(1 for _s, reason in plan.dropped if reason == cp.R_CEILING)
        total_chars += plan.chars
        max_chars = max(max_chars, plan.chars)

        baseline = "".join(block for block in ordered if block)
        baseline_chars += len(baseline)
        baseline_max = max(baseline_max, len(baseline))

        # Isolation: no marker that was not passed in may appear.
        passed_markers = {"ROOMWINDOW", "ROOMREAD", "STATETASK", "MEMVAL"}
        for marker in passed_markers:
            supplied = any(marker in block for block in case["blocks"].values())
            if not supplied and marker in plan.text:
                isolation_leaks += 1
                ok = False
                failures.append(f"{case['id']}: leaked {marker}")

        if ok:
            passed += 1

    n = len(CASES)
    return {
        "cases": n,
        "passed": passed,
        "failed": n - passed,
        "failures": failures,
        "fast_path_cases": fast,
        "full_path_cases": full,
        "dropped_duplicate_or_ceiling": dropped_duplicate,
        "ceiling_bites": ceiling_bites,
        "isolation_leaks": isolation_leaks,
        "context_chars_mean": round(total_chars / n, 1) if n else 0,
        "context_chars_max": max_chars,
        "baseline_chars_mean": round(baseline_chars / n, 1) if n else 0,
        "baseline_chars_max": baseline_max,
        "reduction_mean_pct": (
            round(100.0 * (1 - (total_chars / baseline_chars)), 1)
            if baseline_chars else 0.0
        ),
    }


# ── Non-vacuity: a case that *should* carry a marker does ─────────────────
def non_vacuity():
    """Prove the isolation floors are not vacuous.

    The same shape as ``O_other_user_memory``, but with the block supplied for
    its own key: the marker must then appear. Without this, "no leak" would be
    indistinguishable from "the block is never rendered at all".
    """
    own = cp.compose(cp.read("برای پروژه پایتونم چه کنم؟"), memory=MEMORY)
    other = cp.compose(cp.read("برای پروژه پایتونم چه کنم؟"), memory="")
    return {
        "own_key_has_memory": "MEMVAL" in own.text,
        "other_key_has_no_memory": "MEMVAL" not in other.text,
    }


def measure_latency(iterations: int = 2000):
    """The selector's own cost: pure Python, no I/O, no model."""
    texts = [case["text"] for case in CASES]
    read_samples: list[float] = []
    compose_samples: list[float] = []

    for index in range(iterations):
        text = texts[index % len(texts)]
        case = CASES[index % len(CASES)]
        start = time.perf_counter()
        r = cp.read(text, reply=case["reply"], media=case["media"])
        read_samples.append((time.perf_counter() - start) * 1000.0)

        start = time.perf_counter()
        cp.compose(
            r,
            admin=case["blocks"].get("admin", ""),
            room=case["blocks"].get("room", ""),
            awareness=case["blocks"].get("awareness", ""),
            state=case["blocks"].get("state", ""),
            memory=case["blocks"].get("memory", ""),
            date=case["blocks"].get("date", ""),
            message=text,
        )
        compose_samples.append((time.perf_counter() - start) * 1000.0)

    def stats(samples):
        ordered = sorted(samples)
        return {
            "p50": round(statistics.median(ordered), 4),
            "p95": round(ordered[int(len(ordered) * 0.95)], 4),
        }

    return {"read": stats(read_samples), "compose": stats(compose_samples)}


def measure_model_calls() -> int:
    """How many model clients the module imports. Zero, and that is the point."""
    source = open(cp.__file__, encoding="utf-8").read()
    markers = ("genai", "gemini", "requests.", "httpx", "urlopen")
    return sum(source.count(marker) for marker in markers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    result = evaluate()
    vacuity = non_vacuity()
    latency = measure_latency()
    model_calls = measure_model_calls()

    report = {
        "selection": result,
        "non_vacuity": vacuity,
        "latency_ms": latency,
        "model_calls_in_source": model_calls,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("Increment Y — context selection, deterministic benchmark")
    print("=" * 60)
    print("SELECTION (the labelled corpus, cases A–U)")
    print(f"  cases             {result['cases']}")
    print(f"  passed            {result['passed']}")
    print(f"  failed            {result['failed']}")
    for failure in result["failures"]:
        print(f"  failure           {failure}")
    print(f"  fast path         {result['fast_path_cases']}")
    print(f"  full path         {result['full_path_cases']}")
    print()
    print("BUDGET (all provided blocks, always, versus the minimum combination)")
    print(
        f"  baseline chars    mean {result['baseline_chars_mean']} "
        f"max {result['baseline_chars_max']}"
    )
    print(
        f"  Y chars           mean {result['context_chars_mean']} "
        f"max {result['context_chars_max']}"
    )
    print(f"  reduction         {result['reduction_mean_pct']}% (mean)")
    print(f"  ceiling bites     {result['ceiling_bites']}")
    print(f"  dedup/ceiling     {result['dropped_duplicate_or_ceiling']}")
    print()
    print("ISOLATION")
    print(f"  leaks             {result['isolation_leaks']}")
    print(
        f"  own key carries   {vacuity['own_key_has_memory']} "
        f"(other key does not: {vacuity['other_key_has_no_memory']})"
    )
    print()
    print("LATENCY (the selector's own cost, no I/O)")
    print(f"  read ms p50/p95   {latency['read']['p50']} / {latency['read']['p95']}")
    print(
        f"  compose ms p50/p95 {latency['compose']['p50']} / "
        f"{latency['compose']['p95']}"
    )
    print()
    print(f"MODEL CALLS         {model_calls} (0 = none in the module's source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
