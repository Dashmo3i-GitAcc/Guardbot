"""The moderation policy engine: what may be deleted, and what may not.

This is the suite the brief's moderation requirements map onto most directly,
and it is deliberately organised around the *cases* rather than around the
functions: an ordinary message, a clearly explicit one, an ambiguous one, an AI
failure, an exempt author.

The engine is pure, so every test here is a plain function call. No Telegram, no
network, no database, no clock.

The policy used to weigh a local visual detector against the AI as well. That
whole subsystem was removed, so there is no longer a `local` input to feed: the
AI's verdict is the only evidence, and the only thing that can produce a
deletion is a confident one.
"""
import pytest

from app import config, mod_policy
from app.ai_moderation import ModerationVerdict
from app.decision import Decision


def ai(classification, confidence, *, uncertain=False):
    return ModerationVerdict(
        decided=True,
        classification=classification,
        confidence=confidence,
        uncertain=uncertain,
        model="test-model",
    )


def run(*, verdict=None, exempt=False):
    return mod_policy.decide(mod_policy.PolicyInput(ai=verdict, exempt=exempt))


# ── The action vocabulary ─────────────────────────────────────────────────
def test_the_action_set_contains_no_punishment():
    """The AI must not be able to ban or mute anybody, so there is no word for it.

    Asserted on the enum rather than on the rules: a future rule cannot
    accidentally punish without someone first adding a member here, which is a
    change a reviewer will see.
    """
    assert {action.value for action in mod_policy.Action} == {
        "allow",
        "review",
        "delete_warn",
    }
    for forbidden in ("ban", "mute", "restrict", "kick"):
        assert forbidden not in {action.value for action in mod_policy.Action}


# ── The cases the brief names ─────────────────────────────────────────────
def test_ordinary_content_is_allowed():
    """The AI looked and said normal: nothing happens at all."""
    outcome = run(verdict=ai("normal", 0.95))

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.reason == "no_evidence"


def test_clearly_explicit_content_is_deleted():
    """A confident, non-uncertain explicit verdict is the one thing that deletes."""
    outcome = run(verdict=ai("explicit_sexual", 0.93))

    assert outcome.action is mod_policy.Action.DELETE_WARN
    assert outcome.reason == "ai_confirmed_explicit"
    assert outcome.source == mod_policy.SOURCE_AI
    assert outcome.deletes is True


def test_an_uncertain_ai_verdict_never_deletes():
    """`uncertain` is a veto even at a high confidence.

    A model that says "explicit, 0.95, but I am guessing" has told us it does not
    know, and a deletion on that is exactly the mistake this design forbids.
    """
    outcome = run(verdict=ai("explicit_sexual", 0.99, uncertain=True))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_an_explicit_verdict_below_the_review_floor_is_allowed():
    """Below the review floor there is nothing for a human to look at.

    A deletable class at 0.40 is neither a deletion nor a review: the AI is not
    pointing at anything confidently, and inventing a review from it would fill
    the operator's channel with noise.
    """
    outcome = run(verdict=ai("explicit_sexual", 0.40))

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False


def test_an_explicit_verdict_between_the_floors_is_reviewed():
    """Above the review floor but below the delete floor: a human looks."""
    outcome = run(verdict=ai("explicit_sexual", 0.60))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


# ── Failures ──────────────────────────────────────────────────────────────
def test_an_ai_failure_does_not_cause_a_destructive_action():
    """The requirement, stated as a test.

    The AI was asked and could not answer. Nothing is deleted — the fail-safe
    direction is to leave the content alone.
    """
    broken = ModerationVerdict(error="timeout", model="test-model")
    outcome = run(verdict=broken)

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False


def test_an_ai_that_was_never_asked_does_not_cause_a_deletion():
    outcome = run(verdict=None)

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False


def test_a_skipped_ai_call_does_not_cause_a_deletion():
    """A quota skip is the same fact as a failure, as far as the policy goes."""
    skipped = ModerationVerdict(skipped="daily_cap", model="test-model")
    outcome = run(verdict=skipped)

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False


# ── Reviews that are not deletions ────────────────────────────────────────
def test_harassment_is_reported_for_review_and_never_deleted():
    """A category the policy carries but does not act on yet.

    The brief asks the architecture to support a future restriction without
    rewriting the AI layer; this is the signal existing and being recorded, with
    the decision deliberately not to act on it.
    """
    outcome = run(verdict=ai("harassment", 0.80))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "ai_harassment"
    assert outcome.deletes is False


def test_a_threat_is_reported_for_review_and_never_deleted():
    outcome = run(verdict=ai("threat", 0.75))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_spam_is_reported_for_review_and_never_deleted():
    outcome = run(verdict=ai("spam", 0.70))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_suggestive_content_is_reported_for_review_and_never_deleted():
    """The answer that matters most: a direct statement that it is not explicit."""
    outcome = run(verdict=ai("suggestive", 0.80))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_a_low_confidence_ai_opinion_is_not_even_reported():
    """Below the review floor the verdict is recorded in the counters only."""
    outcome = run(verdict=ai("harassment", 0.20))

    assert outcome.action is mod_policy.Action.ALLOW


def test_an_unknown_classification_is_not_reported():
    """`unknown` means the model could not judge; it is not a signal to act on."""
    outcome = run(verdict=ai("unknown", 0.99))

    assert outcome.action is mod_policy.Action.ALLOW


# ── Exemption and the switch ──────────────────────────────────────────────
def test_an_exempt_author_is_allowed_whatever_the_evidence_says():
    outcome = run(verdict=ai("explicit_sexual", 0.99), exempt=True)

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.source == mod_policy.SOURCE_EXEMPT


def test_turning_the_policy_off_allows_everything(monkeypatch):
    """A kill switch that cannot delete, only allow."""
    monkeypatch.setattr(config, "MODERATION_ENABLED", False)
    outcome = run(verdict=ai("explicit_sexual", 0.99))

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False
    assert outcome.source == mod_policy.SOURCE_DISABLED


# ── The adapter to the executor ───────────────────────────────────────────
def test_the_adapter_only_marks_explicit_for_a_deletion():
    """The single property that keeps the executor from deleting by accident.

    `moderation.enforce` deletes exactly when it is handed `Decision.EXPLICIT`.
    So this mapping is the last place a wrong answer could become a destroyed
    message, and it is asserted directly.
    """
    delete = run(verdict=ai("explicit_sexual", 0.95))
    review = run(verdict=ai("suggestive", 0.95))
    allow = run(verdict=ai("normal", 0.95))

    assert mod_policy.enforce_result(delete).decision is Decision.EXPLICIT
    assert mod_policy.enforce_result(review).decision is Decision.REVIEW
    assert mod_policy.enforce_result(allow).decision is Decision.SAFE


def test_the_adapter_carries_the_reason_through():
    outcome = run(verdict=ai("explicit_sexual", 0.95))
    adapted = mod_policy.enforce_result(outcome)

    assert "ai_confirmed_explicit" in adapted.reason


# ── The log line ──────────────────────────────────────────────────────────
def test_the_description_carries_no_content():
    """The policy log line is metadata only — never the message itself."""
    outcome = run(verdict=ai("explicit_sexual", 0.95))
    line = mod_policy.describe(outcome)

    assert "action=delete_warn" in line
    assert "explicit_sexual" in line
    assert "source=ai" in line


@pytest.mark.parametrize(
    "classification", ["explicit_sexual", "suggestive", "harassment", "threat", "spam",
                       "normal", "unknown"]
)
def test_every_classification_the_ai_may_return_is_handled(classification):
    """No classification may fall through to a deletion by accident.

    Only `explicit_sexual` is deletable, and only at a confidence the config
    allows — so every other class must land on allow or review.
    """
    outcome = run(verdict=ai(classification, 0.99))

    if classification == "explicit_sexual":
        assert outcome.action is mod_policy.Action.DELETE_WARN
    else:
        assert outcome.deletes is False
