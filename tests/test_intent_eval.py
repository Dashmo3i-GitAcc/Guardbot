"""The floor under the deterministic-understanding benchmark.

``tools/eval_intent.py`` measures; this holds the measurement to a standard, so a
change that quietly costs understanding fails a test instead of being noticed
months later. The corpus is fixed, so the deterministic numbers are exact: a
deliberate change to the lexicon or the resolver is expected to move them, and
the right response is to update the corpus on purpose rather than to loosen the
floor by reflex.

The one addressing case left as a gap is asserted as a gap. It is a real miss —
an exact name mid-sentence in a message that is talking *about* Nexus — and it is
left because every deterministic rule that catches it also demotes a real
request with the name in the same position. Pinning it here means the day it is
fixed, this test fails and says so.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The harness lives in tools/ and is a script, not a package module.
_spec = importlib.util.spec_from_file_location(
    "eval_intent", ROOT / "tools" / "eval_intent.py"
)
eval_intent = importlib.util.module_from_spec(_spec)
sys.modules["eval_intent"] = eval_intent
_spec.loader.exec_module(eval_intent)

# The addressing case the matcher gets wrong today: an exact name in the middle
# of a sentence that is talking *about* Nexus rather than to it — «من با نکسوس
# کار نکردم». It is left alone on purpose. Every deterministic rule that catches
# it also demotes a real request with the name in the same position («میشه نکسوس
# اینو بررسی کنی؟»), and missing a call is the worse of the two mistakes; the
# weak grade already marks the line "⋯ about you" for the model to read.
KNOWN_ADDRESSING_GAPS = {
    "about-nexus-mid-sentence",
}


def result():
    return eval_intent.evaluate(eval_intent.load_cases())


def test_the_corpus_is_well_formed():
    cases = eval_intent.load_cases()
    assert len(cases["cases"]) >= 30
    ids = [case["id"] for case in cases["cases"]]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in cases["cases"]:
        assert "window" in case and "anchor" in case and "expect" in case
        assert "text" in case["anchor"]


def test_expression_detection_is_exact_on_the_corpus():
    m = result()
    assert m["expression_accuracy"] == 1.0


def test_referent_resolution_finds_the_right_person_every_time():
    m = result()
    assert m["resolution_top1_accuracy"] == 1.0


def test_it_never_guesses_when_the_room_is_ambiguous():
    """The property the whole design rests on: no wrong-person certainty."""
    m = result()
    assert m["wrong_confident"] == 0
    assert m["ambiguity_recall"] == 1.0


def test_the_resolver_is_a_strict_improvement_over_the_reply_edge():
    """The claim, as a fraction: the reply edge alone versus the resolver."""
    m = result()
    assert m["provided_before"] < m["provided_after"]
    assert m["provided_after"] == 1.0


def test_the_block_the_model_is_shown_stays_small():
    m = result()
    assert m["block_chars_max"] <= 900


def test_the_resolver_is_fast_enough_to_run_on_every_pass():
    """Pure Python, no query: the mean must stay far under a millisecond."""
    m = result()
    assert m["us_mean"] < 3000


def test_the_addressing_gaps_are_exactly_the_ones_we_know_about():
    """A gap that closes, or a new one, must fail here and be looked at."""
    detail = result()["detail"]
    misses = {r["id"] for r in detail if not r["addressed_ok"]}
    assert misses == KNOWN_ADDRESSING_GAPS, misses


def test_the_harness_runs_without_a_database_or_a_key():
    """It scores text, so it must work on a bare checkout."""
    assert eval_intent.load_cases()["cases"]
    m = result()
    assert m["cases"] >= 30
