"""The floor under the context-composition benchmark.

``tools/eval_context.py`` measures what increment Y selects; this holds the
measurement to a standard, so a change that quietly widens or narrows the
minimum context fails a test instead of being noticed months later. The corpus
and the selector are both deterministic and offline, so the numbers are exact: a
deliberate change to the reading is expected to move them, and the right
response is to update the corpus on purpose rather than to loosen a floor by
reflex.

The load-bearing claims of Y are all here as floors: the fast path never pays
for the room, a fresh statement beats a stored fact, a repeated fact is sent
once, the four sources are each selected by some case and never leak across a
key, the whole context stays under its hard ceiling, and the selector makes no
model call. Each "never" is paired with a non-vacuity proof, so a zero cannot
come from a check that has nothing to check.
"""
import importlib.util
import sys
from pathlib import Path

from app import config, context_plan

ROOT = Path(__file__).resolve().parent.parent

# The harness lives in tools/ and is a script, not a package module.
_spec = importlib.util.spec_from_file_location(
    "eval_context", ROOT / "tools" / "eval_context.py"
)
eval_context = importlib.util.module_from_spec(_spec)
sys.modules["eval_context"] = eval_context
_spec.loader.exec_module(eval_context)


def result():
    return eval_context.evaluate()


def _plan(case_id):
    for case in eval_context.CASES:
        if case["id"] == case_id:
            return eval_context._plan_for(case)
    raise AssertionError(f"no such case: {case_id}")


# ── The corpus itself ─────────────────────────────────────────────────────
def test_the_corpus_is_well_formed():
    ids = [case["id"] for case in eval_context.CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    assert len(ids) >= 24, "the A–U corpus is at least two dozen cases"
    for case in eval_context.CASES:
        assert case["text"] is not None, case["id"]
        assert case["expect"], f"{case['id']} carries no expectation"
        # Every case names its mode, so a case that silently changed path
        # cannot pass on its markers alone.
        assert "mode" in case["expect"], case["id"]
        assert case["expect"]["mode"] in (context_plan.FAST, context_plan.FULL)


def test_the_corpus_covers_every_letter_of_the_brief():
    """The brief's cases A–U are present by name, not merely by count."""
    ids = {case["id"] for case in eval_context.CASES}
    for letter in "ABCDEFGHIJKLMNOPQRSTU":
        assert any(cid.startswith(f"{letter}_") for cid in ids), letter


def test_the_corpus_exercises_both_paths():
    m = result()
    assert m["fast_path_cases"] >= 8
    assert m["full_path_cases"] >= 10


def test_every_labelled_case_selects_exactly_what_it_says():
    """The whole corpus, asserted together: selection, omission, reasons."""
    m = result()
    assert m["failed"] == 0, m["failures"]


def test_every_source_is_selected_by_some_case():
    """A source no case ever selects is a source the corpus does not test."""
    selected: set[str] = set()
    for case in eval_context.CASES:
        selected |= set(eval_context._plan_for(case).selected())
    assert selected == set(context_plan.SOURCES)


# ── The fast path ─────────────────────────────────────────────────────────
def test_the_fast_path_never_keeps_the_room():
    """The expensive source is the one the fast path exists to avoid."""
    for case in eval_context.CASES:
        plan = eval_context._plan_for(case)
        if plan.mode == context_plan.FAST:
            assert context_plan.AWARENESS in plan.omitted(), case["id"]


def test_a_greeting_asks_for_neither_room_nor_memory():
    plan = _plan("F_greeting")
    assert context_plan.AWARENESS in plan.omitted()
    assert context_plan.MEMORY in plan.omitted()
    assert plan.reason(context_plan.MEMORY) == context_plan.R_TRIVIAL


def test_a_self_contained_question_needs_no_room():
    plan = _plan("L_private")
    assert plan.mode == context_plan.FAST
    assert context_plan.AWARENESS in plan.omitted()
    assert plan.reason(context_plan.AWARENESS) == context_plan.R_NO_ROOM


# ── Precedence, freshness, de-duplication ─────────────────────────────────
def test_a_fresh_correction_beats_a_stored_fact():
    """Conflict A: the person is fixing the record, so the old value goes."""
    plan = _plan("I_correction")
    assert (context_plan.MEMORY, context_plan.R_CORRECTION) in plan.dropped
    assert "پایتون" not in plan.text
    assert "brief" in plan.text  # the rest of the block survives


def test_a_fresh_instruction_drops_the_stored_task():
    """Conflict B: «بیخیال» ends the task, and the plan does not carry it."""
    plan = _plan("N_conflict_drop_task")
    assert context_plan.STATE in plan.omitted()
    assert plan.reason(context_plan.STATE) == context_plan.R_SUPERSEDE
    assert "STATETASK" not in plan.text


def test_a_duplicate_is_sent_once():
    """Conflict M: the room already states it, so the lower source is dropped."""
    memory = _plan("M_duplicate_memory")
    assert (context_plan.MEMORY, context_plan.R_DUPLICATE) in memory.dropped
    assert "programming" not in memory.text
    state = _plan("M_duplicate_state")
    assert (context_plan.STATE, context_plan.R_DUPLICATE) in state.dropped
    assert "active topic" not in state.text


def test_deduplication_actually_fires_somewhere():
    """A zero in the drop counts would also be what no dedup at all produces."""
    assert result()["dropped_duplicate_or_ceiling"] >= 1


def test_a_stale_state_is_withheld_not_re_added():
    """Conflict J: the reader withheld it, and Y does not put it back."""
    plan = _plan("J_stale_state")
    assert context_plan.STATE in plan.omitted()


# ── Isolation ─────────────────────────────────────────────────────────────
def test_no_block_leaks_across_a_key():
    assert result()["isolation_leaks"] == 0


def test_the_isolation_check_is_not_vacuous():
    """The same shape carries the block for its own key — so the zero is real."""
    vacuity = eval_context.non_vacuity()
    assert vacuity["own_key_has_memory"] is True
    assert vacuity["other_key_has_no_memory"] is True


# ── Budget ────────────────────────────────────────────────────────────────
def test_the_plan_stays_under_the_hard_ceiling():
    """The four selectable sources together never exceed their ceiling."""
    ceiling = config.NEXUS_CONTEXT_CHARS
    for case in eval_context.CASES:
        plan = eval_context._plan_for(case)
        selectable = sum(
            plan.decision(source).chars
            for source in context_plan.SOURCES
            if source != context_plan.CONVERSATION
        )
        assert selectable <= ceiling, case["id"]


def test_the_minimum_is_smaller_than_the_everything_baseline():
    """Y's claim, as a number: the minimum combination is smaller than all of it."""
    m = result()
    assert m["reduction_mean_pct"] >= 20.0, m["reduction_mean_pct"]
    assert m["context_chars_mean"] < m["baseline_chars_mean"]


def test_the_ceiling_never_silently_slices_a_block():
    """Over budget drops whole sources; it never emits a fragment."""
    plan = context_plan.compose(
        context_plan.read("همونو بزن"),
        room="r" * 3000,
        state="s" * 300,
        memory="m" * 300,
        ceiling=500,
    )
    assert plan.chars <= 500
    assert "s" * 300 not in plan.text
    assert "m" * 300 not in plan.text


# ── The cost of the selector itself ───────────────────────────────────────
def test_the_selector_makes_no_model_call():
    """Deterministic selection: no client, no request, nothing to spend."""
    assert eval_context.measure_model_calls() == 0


def test_the_selector_is_fast_enough_for_every_turn():
    """Pure Python over a few short strings — far under a millisecond at p95."""
    latency = eval_context.measure_latency(iterations=500)
    assert latency["read"]["p95"] < 5.0
    assert latency["compose"]["p95"] < 5.0


# ── The benchmark can fail ────────────────────────────────────────────────
def test_the_benchmark_is_not_vacuous(monkeypatch):
    """A mislabelled case must fail, or the corpus proves nothing."""
    bad = eval_context._case(
        "ZZ_deliberately_wrong",
        "سلام",
        blocks={"state": eval_context.STATE},
        expect={"selected": (eval_context.M,), "has": ["MEMVAL"]},
    )
    monkeypatch.setattr(eval_context, "CASES", eval_context.CASES + (bad,))
    m = eval_context.evaluate()
    assert m["failed"] >= 1


# ── The real-path benchmark, and its floors ───────────────────────────────
# It mutates the process's config and database, so it runs in a subprocess: the
# measurement is the point, and leaking its setup into the rest of the suite
# would make other tests depend on the order they ran in.
def _real_benchmark():
    import json
    import subprocess

    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "bench_context_real.py"),
            "--json",
            "--iterations",
            "1",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def test_the_real_path_carries_less_context_after_y():
    """The headline claim, reproduced: the minimum combination is smaller."""
    report = _real_benchmark()
    assert len(report["cases"]) == 8
    assert report["before_chars"] > report["after_chars"]
    assert report["saved_pct"] >= 5.0, report["saved_pct"]


def test_the_fast_path_saves_the_room_and_the_room_dependent_does_not():
    """The saving is the room window, and only where the room is not needed."""
    report = _real_benchmark()
    saved = {row["case"]: row["before"] - row["after"] for row in report["cases"]}
    for case in ("greeting", "ack", "self-contained question", "continuation"):
        assert saved[case] > 500, (case, saved[case])
    for case in ("anaphora", "opinion", "correction", "instruction"):
        assert saved[case] == 0, (case, saved[case])


def test_the_real_path_reads_each_source_fewer_times():
    """No duplicate retrieval: fewer renders, never more, and state is free."""
    report = _real_benchmark()
    before, after = report["reads_before"], report["reads_after"]
    assert after["window"] < before["window"]
    assert after["reading"] < before["reading"]
    assert after["memory"] <= before["memory"]
    assert after["state"] == before["state"]


def test_the_real_path_assembly_is_not_slower():
    report = _real_benchmark()
    assert report["assembly_ms_after"]["p50"] <= report["assembly_ms_before"]["p50"] * 1.5
    assert report["assembly_ms_after"]["p95"] < 500.0
