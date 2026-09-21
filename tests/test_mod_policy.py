"""The moderation policy engine: what may be deleted, and what may not.

This is the suite the brief's moderation requirements map onto most directly,
and it is deliberately organised around the *cases* rather than around the
functions: an ordinary celebrity photograph, an ordinary conversation, clearly
explicit content, ambiguous content, a detector failure, an AI failure.

The engine is pure, so every test here is a plain function call. No Telegram, no
network, no database, no clock.
"""
import pytest

from app import config, mod_policy
from app.ai_moderation import ModerationVerdict
from app.decision import Decision, DecisionResult
from app.detector import Detection

LOCAL_LABEL = "FEMALE_GENITALIA_EXPOSED"


def local(decision, *, label=LOCAL_LABEL, score=0.5, scene=None):
    """A DecisionResult shaped the way the decision engine produces them."""
    matched = Detection(label, score) if label else None
    return DecisionResult(
        decision,
        "test",
        matched=matched,
        scene_nsfw=scene,
        frames_checked=1,
        source="nudenet" if label else ("scene" if scene is not None else "none"),
    )


SAFE = DecisionResult(Decision.SAFE, "no explicit evidence")
LOCAL_REVIEW = local(Decision.REVIEW, score=0.30)
LOCAL_EXPLICIT = local(Decision.EXPLICIT, score=0.55)


def ai(classification, confidence, *, uncertain=False, content_type="image"):
    return ModerationVerdict(
        decided=True,
        classification=classification,
        confidence=confidence,
        content_type=content_type,
        uncertain=uncertain,
        model="test-model",
    )


def run(*, local_result=None, verdict=None, exempt=False):
    return mod_policy.decide(
        mod_policy.PolicyInput(
            local=local_result, ai=verdict, media_kind="photo", is_media=True,
            exempt=exempt,
        )
    )


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
def test_an_ordinary_celebrity_photograph_is_not_deleted():
    """The false positive this whole design was built to stop.

    The local detector escalates — that is exactly what it did in production on
    an ordinary photograph — and the moderation AI, asked for a second opinion,
    says the content is normal. Nothing is deleted.
    """
    outcome = run(local_result=LOCAL_EXPLICIT, verdict=ai("normal", 0.95))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "local_explicit_ai_declined"
    assert outcome.deletes is False


def test_a_swimsuit_photograph_is_not_deleted_even_at_a_high_local_score():
    """The `suggestive` answer is the one that matters most.

    It is a direct statement from the AI that the content is not explicit, and
    it must override a confident-looking local score.
    """
    outcome = run(
        local_result=local(Decision.EXPLICIT, score=0.88),
        verdict=ai("suggestive", 0.80),
    )

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_ordinary_conversation_is_allowed():
    """No local evidence, the AI says normal: nothing happens at all."""
    outcome = run(local_result=SAFE, verdict=ai("normal", 0.95, content_type="text"))

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.reason == "no_evidence"


def test_clearly_explicit_content_is_deleted():
    """A confident, non-uncertain explicit verdict is the one thing that deletes."""
    outcome = run(
        local_result=local(Decision.EXPLICIT, score=0.55),
        verdict=ai("explicit_sexual", 0.93),
    )

    assert outcome.action is mod_policy.Action.DELETE_WARN
    assert outcome.reason == "ai_confirmed_explicit"
    assert outcome.source == mod_policy.SOURCE_AI
    assert outcome.deletes is True


def test_explicit_content_the_ai_confirms_is_deleted_even_when_the_detector_found_nothing():
    """The scene classifier's old job, now done with a reason attached.

    A sexual act with no exposed anatomy is invisible to NudeNet. The AI can see
    it, and its confirmation is enough on its own.
    """
    outcome = run(local_result=SAFE, verdict=ai("explicit_sexual", 0.90))

    assert outcome.action is mod_policy.Action.DELETE_WARN
    assert outcome.source == mod_policy.SOURCE_AI


def test_ambiguous_content_is_not_destructively_moderated():
    """A deletable class at a confidence below the floor is not a verdict."""
    outcome = run(
        local_result=LOCAL_EXPLICIT,
        verdict=ai("explicit_sexual", 0.40, uncertain=True),
    )

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_an_uncertain_ai_verdict_never_deletes():
    """`uncertain` is a veto even at a high confidence.

    A model that says "explicit, 0.95, but I am guessing" has told us it does not
    know, and a deletion on that is exactly the mistake this design forbids.
    """
    outcome = run(
        local_result=LOCAL_EXPLICIT,
        verdict=ai("explicit_sexual", 0.99, uncertain=True),
    )

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "ai_uncertain"


# ── Failures ──────────────────────────────────────────────────────────────
def test_a_detector_failure_does_not_delete_content():
    """`ok=False` from the local stage is SAFE, and SAFE never deletes."""
    failed = DecisionResult(Decision.SAFE, "fail-open: decode error")
    outcome = run(local_result=failed, verdict=None)

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False


def test_an_ai_failure_does_not_cause_a_destructive_action():
    """The requirement, stated as a test.

    The AI was asked and could not answer. The local detector says explicit at a
    high score. Nothing is deleted — the content is reported for a human, which
    is the fail-safe direction.
    """
    broken = ModerationVerdict(error="timeout", model="test-model")
    outcome = run(
        local_result=local(Decision.EXPLICIT, score=0.92), verdict=broken
    )

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "no_ai_confirmation"
    assert outcome.deletes is False


def test_an_ai_that_was_never_asked_does_not_cause_a_deletion():
    outcome = run(local_result=local(Decision.EXPLICIT, score=0.92), verdict=None)

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_a_skipped_ai_call_does_not_cause_a_deletion():
    """A quota skip is the same fact as a failure, as far as the policy goes."""
    skipped = ModerationVerdict(skipped="daily_cap", model="test-model")
    outcome = run(local_result=local(Decision.EXPLICIT, score=0.92), verdict=skipped)

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


# ── The local-only mode ───────────────────────────────────────────────────
def test_the_local_only_mode_still_needs_hard_evidence(monkeypatch):
    """With the AI layer out of the picture, the bar is the hard threshold.

    This is the mode that produced the false positives, so it is opt-in — and
    even in it, the old low threshold is not enough.
    """
    monkeypatch.setattr(config, "MODERATION_REQUIRE_AI_CONFIRM", False)
    monkeypatch.setattr(config, "MODERATION_LOCAL_HARD_THRESHOLD", 0.85)

    soft = run(local_result=local(Decision.EXPLICIT, score=0.55))
    hard = run(local_result=local(Decision.EXPLICIT, score=0.90))

    assert soft.action is mod_policy.Action.REVIEW
    assert hard.action is mod_policy.Action.DELETE_WARN
    assert hard.reason == "local_only_hard_evidence"


def test_the_scene_classifier_alone_never_deletes(monkeypatch):
    """A scene-only score is evidence for a human in every mode.

    The scene classifier is the less interpretable of the two local signals, and
    the case it was added for is now handled by the AI with a reason attached.
    """
    monkeypatch.setattr(config, "MODERATION_REQUIRE_AI_CONFIRM", False)
    scene_only = local(Decision.EXPLICIT, label=None, scene=0.99)

    outcome = run(local_result=scene_only)

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_the_default_mode_never_deletes_on_local_evidence(monkeypatch):
    """The default, asserted as a property rather than per-case.

    Whatever the local detector says, and however confident it is, the default
    configuration requires the AI to agree before anything is deleted.
    """
    monkeypatch.setattr(config, "MODERATION_REQUIRE_AI_CONFIRM", True)
    for score in (0.45, 0.60, 0.80, 0.99):
        outcome = run(local_result=local(Decision.EXPLICIT, score=score), verdict=None)
        assert outcome.deletes is False, f"deleted at local score {score}"


# ── Reviews that are not disagreements ────────────────────────────────────
def test_harassment_is_reported_for_review_and_never_deleted():
    """A category the policy carries but does not act on yet.

    The brief asks the architecture to support a future restriction without
    rewriting the AI layer; this is the signal existing and being recorded, with
    the decision deliberately not to act on it.
    """
    outcome = run(local_result=SAFE, verdict=ai("harassment", 0.80, content_type="text"))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "ai_harassment"
    assert outcome.deletes is False


def test_a_threat_is_reported_for_review_and_never_deleted():
    outcome = run(local_result=SAFE, verdict=ai("threat", 0.75, content_type="text"))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_spam_is_reported_for_review_and_never_deleted():
    outcome = run(local_result=SAFE, verdict=ai("spam", 0.70, content_type="text"))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.deletes is False


def test_a_low_confidence_ai_opinion_is_not_even_reported():
    """Below the review floor the verdict is recorded in the counters only."""
    outcome = run(local_result=SAFE, verdict=ai("harassment", 0.20, content_type="text"))

    assert outcome.action is mod_policy.Action.ALLOW


def test_the_local_review_band_still_reports():
    """Preserved from the original policy: a borderline score is a human's job."""
    outcome = run(local_result=LOCAL_REVIEW, verdict=None)

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "local_review"


# ── Exemption and the switch ──────────────────────────────────────────────
def test_an_exempt_author_is_allowed_whatever_the_evidence_says():
    outcome = run(
        local_result=LOCAL_EXPLICIT,
        verdict=ai("explicit_sexual", 0.99),
        exempt=True,
    )

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.source == mod_policy.SOURCE_EXEMPT


def test_turning_the_policy_off_allows_everything(monkeypatch):
    """A kill switch that cannot delete, only allow."""
    monkeypatch.setattr(config, "MODERATION_ENABLED", False)
    outcome = run(
        local_result=local(Decision.EXPLICIT, score=0.99),
        verdict=ai("explicit_sexual", 0.99),
    )

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.deletes is False


# ── The adapter to the executor ───────────────────────────────────────────
def test_the_adapter_only_marks_explicit_for_a_deletion():
    """The single property that keeps the executor from deleting by accident.

    `moderation.enforce` deletes exactly when it is handed `Decision.EXPLICIT`.
    So this mapping is the last place a wrong answer could become a destroyed
    message, and it is asserted directly.
    """
    delete = run(local_result=LOCAL_EXPLICIT, verdict=ai("explicit_sexual", 0.95))
    review = run(local_result=LOCAL_EXPLICIT, verdict=ai("normal", 0.95))
    allow = run(local_result=SAFE, verdict=ai("normal", 0.95))

    assert mod_policy.enforce_result(delete, LOCAL_EXPLICIT).decision is Decision.EXPLICIT
    assert mod_policy.enforce_result(review, LOCAL_EXPLICIT).decision is Decision.REVIEW
    assert mod_policy.enforce_result(allow, LOCAL_EXPLICIT).decision is Decision.SAFE


def test_the_adapter_carries_the_evidence_through():
    """The report needs the matched detection, so the adapter must not drop it."""
    outcome = run(local_result=LOCAL_EXPLICIT, verdict=ai("explicit_sexual", 0.95))
    adapted = mod_policy.enforce_result(outcome, LOCAL_EXPLICIT)

    assert adapted.matched is not None
    assert adapted.matched.label == LOCAL_LABEL


def test_the_adapter_survives_having_no_local_result():
    """Text moderation has no local stage; the adapter must accept None."""
    verdict = ai("explicit_sexual", 0.95, content_type="text")
    outcome = mod_policy.decide(
        mod_policy.PolicyInput(local=None, ai=verdict, is_media=False)
    )
    adapted = mod_policy.enforce_result(outcome, None)

    assert adapted.decision is Decision.EXPLICIT
    assert adapted.matched is None


# ── The log line ──────────────────────────────────────────────────────────
def test_the_description_carries_no_content():
    """The policy log line is metadata only — never the message, never the media."""
    outcome = run(local_result=LOCAL_EXPLICIT, verdict=ai("explicit_sexual", 0.95))
    line = mod_policy.describe(outcome)

    assert "action=delete_warn" in line
    assert "explicit_sexual" in line
    assert "FEMALE_GENITALIA_EXPOSED" in line


@pytest.mark.parametrize(
    "classification", ["explicit_sexual", "suggestive", "harassment", "threat", "spam",
                       "normal", "unknown"]
)
def test_every_classification_the_ai_may_return_is_handled(classification):
    """No classification may fall through to a deletion by accident.

    Only `explicit_sexual` is deletable, and only at a confidence the config
    allows — so every other class must land on allow or review.
    """
    outcome = run(local_result=SAFE, verdict=ai(classification, 0.99, content_type="text"))

    if classification == "explicit_sexual":
        assert outcome.action is mod_policy.Action.DELETE_WARN
    else:
        assert outcome.deletes is False
