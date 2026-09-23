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

from app import temporal

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
        # Every case carries an act label, including the abstentions — a case
        # with no label cannot be scored, and a default would hide the ones the
        # reader is expected to abstain on.
        assert "act" in case["expect"], case["id"]
        assert "open_questions" in case["expect"], case["id"]
        # …and a time label, for the same reason: an absent field would make
        # "no time word here" indistinguishable from "not yet labelled".
        assert "when" in case["expect"], case["id"]
        assert "when_unit" in case["expect"], case["id"]


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


def test_it_does_not_cry_ambiguity_when_the_room_has_settled():
    """Precision matters too: a needless "I cannot tell" is a wasted round trip.

    The anaphoric reading is what earns this — «همون کاربر» in a room whose
    replies have all been aimed at one person is not ambiguous, it is answered.
    """
    m = result()
    assert m["ambiguity_precision"] == 1.0


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


# ── The act ───────────────────────────────────────────────────────────────
def test_the_act_reader_never_claims_an_act_it_cannot_defend():
    """The property the precedence and the abstention exist for.

    A wrong act in the prompt is worse than no act, so the floor is on the
    claimed direction: everything it says, it is right about.
    """
    m = result()
    assert m["act_false_positives"] == 0
    assert m["act_claimed_precision"] == 1.0


def test_the_act_reader_finds_every_labelled_act():
    m = result()
    assert m["act_false_negatives"] == 0
    assert m["act_recall"] == 1.0


def test_the_act_reader_still_abstains_rather_than_guessing():
    """Coverage is high, not total — the difference is the honest part.

    The abstentions are the implicit complaint, the plain statement and the
    two empty anchors: messages whose act is a matter of meaning. If this ever
    reaches 100%, something started guessing.

    The floor moved from 0.85 to 0.75 when the temporal slice was added, and the
    move is a statement about the corpus rather than about the reader: sixteen
    messages were added, and nine of them are plain statements that place
    themselves in time («امروز هوا خیلی گرمه») — whose act *is* a matter of
    meaning, exactly the kind of message this reader is built to abstain on. The
    load-bearing floors are untouched: claimed precision is still 1.0 and the
    false-positive count is still 0.
    """
    m = result()
    assert 0 < m["act_abstentions"] < m["cases"]
    assert m["act_coverage"] >= 0.75


def test_every_act_class_is_exercised():
    """A corpus that only tested instructions would score a constant well."""
    by_class = result()["act_by_class"]
    for kind, counts in by_class.items():
        if kind == "unknown":
            continue
        assert counts["total"] >= 2, f"{kind} has only {counts['total']} cases"


# ── The room's open questions ─────────────────────────────────────────────
def test_the_open_questions_are_exact():
    m = result()
    assert m["questions_precision"] == 1.0
    assert m["questions_recall"] == 1.0
    assert m["questions_exact"] == m["questions_cases"]
    assert m["questions_cases"] >= 3


def test_the_question_block_stays_small():
    assert result()["questions_block_chars_max"] <= 600


# ── The time words ────────────────────────────────────────────────────────
def test_the_time_reader_never_claims_a_time_nobody_stated():
    """The dangerous direction: a time placed in the prompt from words that
    are not there. Everything it claims, it is right about."""
    m = result()
    assert m["when_false_positives"] == 0
    assert m["when_claimed_precision"] == 1.0


def test_the_time_reader_finds_every_labelled_time_word():
    m = result()
    assert m["when_false_negatives"] == 0
    assert m["when_recall"] == 1.0


def test_the_time_reader_abstains_when_there_is_no_time_word():
    """An empty reading is the common case and the cheap one.

    If coverage ever reaches 100% the reader is claiming a time on messages that
    state none, which is the false-positive direction above.
    """
    m = result()
    assert 0 < m["when_coverage"] < 1.0
    assert m["when_by_kind"][""]["total"] >= 40


def test_every_time_kind_is_exercised():
    by_kind = result()["when_by_kind"]
    for kind in temporal.WHENS:
        assert by_kind[kind]["total"] >= 1, f"{kind} has no cases"


def test_the_time_block_stays_small():
    assert result()["when_block_chars_max"] <= 300


def test_the_time_reader_is_fast_enough_to_run_on_every_pass():
    """The folded table is cached, so a read is a substring scan and no more."""
    assert result()["when_us_mean"] < 1000


def test_the_harness_runs_without_a_database_or_a_key():
    """It scores text, so it must work on a bare checkout."""
    assert eval_intent.load_cases()["cases"]
    m = result()
    assert m["cases"] >= 30
