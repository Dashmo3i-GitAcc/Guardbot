#!/usr/bin/env python3
"""Score the deterministic half of Nexus understanding against a labelled corpus.

Why this exists
---------------
The brief for this stage is explicit that no claim of "smarter" may be made
without a number, and that the number must be reproducible. This is where the
numbers come from. It runs the *deterministic* layers — the name matcher, the
deictic expression finder and the referent resolver — over a labelled corpus and
reports accuracy, ambiguity behaviour, the cost in microseconds, and the size of
the block the model would be shown.

What it can and cannot measure
------------------------------
It measures, exactly, the parts of understanding the server does without a model
call. It does **not** measure the model's semantic judgement: whether the
assistant decides a conversation concerns it, and whether it chooses to speak,
are decisions only a live pass can make, and this harness will not pretend
otherwise. Every number below is about what the *server* determines before the
model is asked.

The before/after pair
---------------------
``provided_before`` is what the server could hand the model before this stage:
the reply edge, and nothing else. ``provided_after`` is what it hands over now.
The gap between them is the whole claim, stated as a fraction.

    python tools/eval_intent.py
    python tools/eval_intent.py --json
    python tools/eval_intent.py --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# The app reads its settings at import time and refuses to load without the
# handful that have no sane default. The harness needs none of them for real —
# it never sends, never writes, never calls a model — but the import must
# succeed, so they are set to the same placeholders the test suite uses.
os.environ.setdefault("BOT_TOKEN", "eval-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-eval")
os.environ.setdefault("GEMINI_KEY_STORE_PATH", "/tmp/guardbot-eval/gemini_keys.json")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import addressing, config, discourse, referents  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "eval_cases.json"

# The names the matcher answers to, pinned so the addressing column is
# reproducible regardless of the host's .env.
EVAL_NAMES = ("nexus", "نکسوس")


def load_cases(path: Path = CASES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _row(raw: dict) -> dict:
    """One window row, with every column the resolver, the matcher and the
    discourse reader consult."""
    return {
        "id": int(raw.get("at") or 0),
        "user_id": int(raw.get("user_id") or 0),
        "role": str(raw.get("role") or "member"),
        "name": str(raw.get("name") or ""),
        "text": str(raw.get("text") or ""),
        "at": int(raw.get("at") or 0),
        "message_id": int(raw.get("message_id") or 0),
        "reply_user_id": int(raw.get("reply_user_id") or 0),
        "reply_name": str(raw.get("reply_name") or ""),
        "reply_message_id": int(raw.get("reply_message_id") or 0),
        "directed": bool(raw.get("directed") or False),
        "actor": bool(raw.get("actor") or False),
        "kind": "",
    }


def evaluate(cases: dict) -> dict:
    """Run every case and return the metrics, plus the per-case detail."""
    config.NEXUS_NAMES = list(EVAL_NAMES)
    config.NEXUS_EXTRA_ACTION_WORDS = []

    detail: list[dict] = []
    for case in cases["cases"]:
        window = [_row(row) for row in case.get("window") or ()]
        anchor = _row(case["anchor"])
        expect = case["expect"]

        started = time.perf_counter()
        expression = referents.find_expression(anchor["text"])
        resolution = referents.resolve(anchor, messages=window)
        block = referents.render(resolution)
        elapsed_us = (time.perf_counter() - started) * 1_000_000
        addressed = addressing.detect(anchor["text"]).addressed
        act = discourse.read_act(anchor["text"])
        questions = discourse.open_questions(window)
        questions_block = discourse.render_questions(questions)

        top = resolution.top()
        top_id = top.user_id if top else None

        expected_questions = [str(q) for q in (expect.get("open_questions") or ())]

        detail.append(
            {
                "id": case["id"],
                "category": case.get("category", ""),
                "note": case.get("note", ""),
                "expected_kind": expect["expression_kind"],
                "got_kind": expression.kind,
                "expected_referent": expect["referent"],
                "got_referent": top_id,
                "requires_resolution": bool(expect["requires_resolution"]),
                "expected_ambiguous": bool(expect["ambiguous"]),
                "got_ambiguous": bool(resolution.ambiguous),
                "got_confident": bool(resolution.confident),
                "reply_user_id": int(anchor.get("reply_user_id") or 0),
                "expected_addressed": bool(expect["addressed"]),
                "got_addressed": bool(addressed),
                "expected_act": expect.get("act", discourse.ACT_UNKNOWN),
                "got_act": act.kind,
                "act_why": act.why[0] if act.why else "",
                "expected_questions": expected_questions,
                "got_questions": [q.text for q in questions],
                "questions_block_chars": len(questions_block),
                "block_chars": len(block),
                "us": elapsed_us,
                "kind_ok": expression.kind == expect["expression_kind"],
                "addressed_ok": bool(addressed) == bool(expect["addressed"]),
                "act_ok": act.kind == expect.get("act", discourse.ACT_UNKNOWN),
            }
        )

    return {"detail": detail, **_metrics(detail)}


def _metrics(detail: list[dict]) -> dict:
    """Turn the per-case readings into the numbers the report quotes."""
    n = len(detail) or 1

    def rate(rows, ok) -> float:
        rows = list(rows)
        return (sum(1 for r in rows if ok(r)) / len(rows)) if rows else 0.0

    # The cases where the text alone leaves the referent open — the ones the
    # resolver exists for.
    needs = [r for r in detail if r["requires_resolution"]]
    # …and the subset that has a determinate answer, where top-1 can be scored.
    answerable = [r for r in needs if r["expected_referent"] is not None]
    ambiguous_cases = [r for r in detail if r["expected_ambiguous"]]
    predicted_ambiguous = [r for r in detail if r["got_ambiguous"]]

    # BEFORE: the reply edge and nothing else. The server names a person only
    # when the instruction was sent as a reply, and it names that edge's target.
    provided_before = rate(
        answerable,
        lambda r: r["reply_user_id"] and r["reply_user_id"] == r["expected_referent"],
    )
    # AFTER: the resolver ranks the right person first.
    provided_after = rate(answerable, lambda r: r["got_referent"] == r["expected_referent"])
    # …and the subset it is also sure about, which is the set that needs no
    # clarification round trip.
    confident_correct = rate(
        answerable,
        lambda r: r["got_confident"] and r["got_referent"] == r["expected_referent"],
    )

    # ── The act ───────────────────────────────────────────────────────────
    # Scored three ways, because accuracy alone is a number a constant also
    # gets on a corpus where most messages are instructions.
    unknown = discourse.ACT_UNKNOWN
    claimed = [r for r in detail if r["got_act"] != unknown]
    labelled = [r for r in detail if r["expected_act"] != unknown]
    act_abstentions = [r for r in detail if r["got_act"] == unknown]
    # The two directions of being wrong, and they are not symmetric: an
    # over-claim puts a wrong act in the prompt, an abstention only withholds a
    # signal the model did not have before this stage.
    act_false_positive = [r for r in claimed if r["expected_act"] == unknown]
    act_false_negative = [r for r in labelled if r["got_act"] == unknown]

    # ── The room's open questions ─────────────────────────────────────────
    # Scored over the cases that *have* a question, not over every case: an
    # empty window and an empty expectation agree trivially, and counting those
    # would make the number a statement about the corpus size.
    question_cases = [r for r in detail if r["expected_questions"]]
    question_predicted = [r for r in detail if r["got_questions"]]
    question_hits = sum(
        1 for r in question_cases if set(r["got_questions"]) == set(r["expected_questions"])
    )

    return {
        "cases": len(detail),
        "expression_accuracy": rate(detail, lambda r: r["kind_ok"]),
        "addressing_accuracy": rate(detail, lambda r: r["addressed_ok"]),
        "act_accuracy": rate(detail, lambda r: r["act_ok"]),
        # Of the acts it claimed, the fraction it got right.
        "act_claimed_precision": rate(claimed, lambda r: r["act_ok"]),
        # Of all cases, the fraction it offered a reading for at all.
        "act_coverage": len(claimed) / n,
        # Of the cases that have an act to find, the fraction it found right.
        "act_recall": rate(labelled, lambda r: r["act_ok"]),
        "act_false_positives": len(act_false_positive),
        "act_false_negatives": len(act_false_negative),
        "act_abstentions": len(act_abstentions),
        # Per class, because the corpus is lopsided — most messages in a
        # moderation room are instructions — and a single accuracy figure would
        # be a number a constant could also get. This is the honest shape.
        "act_by_class": {
            kind: {
                "total": sum(1 for r in detail if r["expected_act"] == kind),
                "correct": sum(
                    1 for r in detail if r["expected_act"] == kind and r["act_ok"]
                ),
            }
            for kind in discourse.ACTS + (unknown,)
        },
        "questions_cases": len(question_cases),
        "questions_exact": question_hits,
        "questions_precision": rate(
            question_predicted,
            lambda r: set(r["got_questions"]) <= set(r["expected_questions"]),
        ),
        "questions_recall": rate(
            question_cases, lambda r: set(r["expected_questions"]) <= set(r["got_questions"])
        ),
        "questions_block_chars_max": max(
            (r["questions_block_chars"] for r in detail), default=0
        ),
        "needs_resolution": len(needs),
        "answerable": len(answerable),
        "resolution_top1_accuracy": rate(
            answerable, lambda r: r["got_referent"] == r["expected_referent"]
        ),
        "ambiguity_recall": rate(ambiguous_cases, lambda r: r["got_ambiguous"]),
        "ambiguity_precision": rate(predicted_ambiguous, lambda r: r["expected_ambiguous"]),
        # The dangerous direction: the server was sure, and wrong.
        "wrong_confident": sum(
            1
            for r in ambiguous_cases
            if r["got_confident"] and r["got_referent"] != r["expected_referent"]
        ),
        "provided_before": provided_before,
        "provided_after": provided_after,
        "confident_and_correct": confident_correct,
        "block_chars_mean": statistics.fmean([r["block_chars"] for r in detail]) if detail else 0.0,
        "block_chars_max": max((r["block_chars"] for r in detail), default=0),
        "us_mean": statistics.fmean([r["us"] for r in detail]) if detail else 0.0,
        "us_p95": (
            sorted(r["us"] for r in detail)[min(len(detail) - 1, int(len(detail) * 0.95))]
            if detail
            else 0.0
        ),
        "n": n,
    }


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def report(result: dict, *, verbose: bool = False) -> str:
    m = {k: v for k, v in result.items() if k != "detail"}
    lines = [
        "",
        "Nexus deterministic-understanding benchmark",
        "==========================================",
        f"cases: {m['cases']}",
        "",
        "understanding",
        f"  expression accuracy        {_pct(m['expression_accuracy'])}",
        f"  addressing accuracy        {_pct(m['addressing_accuracy'])}",
        "",
        f"the act (abstention is {discourse.ACT_UNKNOWN!r}, and it is not a failure)",
        f"  claimed precision          {_pct(m['act_claimed_precision'])}",
        f"  coverage                   {_pct(m['act_coverage'])}",
        f"  recall on labelled cases   {_pct(m['act_recall'])}",
        f"  false positives/negatives  {m['act_false_positives']} / {m['act_false_negatives']}",
        f"  abstentions                {m['act_abstentions']}",
        "  per class (correct/total)  "
        + "  ".join(
            f"{kind} {v['correct']}/{v['total']}"
            for kind, v in m["act_by_class"].items()
            if v["total"]
        ),
        "",
        f"the room's open questions ({m['questions_cases']} cases that have one)",
        f"  exact match                {m['questions_exact']} / {m['questions_cases']}",
        f"  precision                  {_pct(m['questions_precision'])}",
        f"  recall                     {_pct(m['questions_recall'])}",
        f"  block chars max            {m['questions_block_chars_max']}",
        "",
        f"referent resolution ({m['answerable']} answerable of {m['needs_resolution']} open)",
        f"  top-1 accuracy             {_pct(m['resolution_top1_accuracy'])}",
        f"  ambiguity recall           {_pct(m['ambiguity_recall'])}",
        f"  ambiguity precision        {_pct(m['ambiguity_precision'])}",
        f"  wrong-but-confident        {m['wrong_confident']}",
        "",
        "the claim, as a fraction",
        f"  provided before (reply edge only)  {_pct(m['provided_before'])}",
        f"  provided after  (resolver)         {_pct(m['provided_after'])}",
        f"  confident and correct              {_pct(m['confident_and_correct'])}",
        "",
        "cost",
        f"  block chars mean / max     {m['block_chars_mean']:.0f} / {m['block_chars_max']}",
        f"  resolver us mean / p95     {m['us_mean']:.0f} / {m['us_p95']:.0f}",
        "",
    ]
    failures = [
        r
        for r in result["detail"]
        if not r["kind_ok"] or not r["addressed_ok"] or not r["act_ok"]
        or (r["requires_resolution"] and r["expected_referent"] is not None
            and r["got_referent"] != r["expected_referent"])
        or (r["expected_ambiguous"] and not r["got_ambiguous"])
        or set(r["got_questions"]) != set(r["expected_questions"])
    ]
    if failures:
        lines.append(f"not met ({len(failures)}):")
        for r in failures:
            lines.append(
                f"  [{r['category']}] {r['id']}: "
                f"kind {r['got_kind']!r}/{r['expected_kind']!r} "
                f"ref {r['got_referent']}/{r['expected_referent']} "
                f"amb {r['got_ambiguous']}/{r['expected_ambiguous']} "
                f"addr {r['got_addressed']}/{r['expected_addressed']} "
                f"act {r['got_act']}/{r['expected_act']} "
                f"q {r['got_questions']}/{r['expected_questions']}"
            )
            if verbose and r["note"]:
                lines.append(f"      {r['note']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="explain each miss")
    parser.add_argument("--cases", default=str(CASES_PATH), help="corpus path")
    args = parser.parse_args(argv)

    result = evaluate(load_cases(Path(args.cases)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(report(result, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())
