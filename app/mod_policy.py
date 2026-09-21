"""The decision. Deterministic, in code, and the only thing that can act.

Everything upstream of this module produces *evidence*: the local detectors
produce scores, the moderation AI produces a structured verdict, the message
produces a media kind. None of them may act, and none of them can — they have no
Telegram client and no reference to one. This module turns evidence into an
action, and ``app/main.py`` performs it.

That ordering is the architecture the brief asks for, and it is worth stating
what it buys:

* **A prompt-injected message cannot do anything.** The worst a manipulated
  model can achieve is a wrong verdict, which still has to pass the rules below
  and still cannot delete anything on its own.
* **A miscalibrated detector cannot do anything either.** This is the change
  that fixes the false positives: the local detector's role was demoted from
  "verdict" to "evidence". It can no longer delete, because a single
  uncalibrated score is not a good enough reason to destroy somebody's message.
* **The rules are testable.** Every branch below is a pure function of its
  inputs, so "an ordinary celebrity photograph is not deleted" is a unit test
  rather than a hope.

**The action set is closed and contains no punishment.** There is no BAN and no
MUTE member of ``Action``, and that is the point rather than an omission: the
brief asks that the moderation AI must not be able to ban or mute anybody, and
the way to guarantee that is for the vocabulary to have no word for it. A future
phase that wants automatic restriction adds a member here, a rule below, and a
permission in ``app/rbac.py`` — the AI layer does not change at all.

**Fail safe, always.** Every uncertainty resolves to ALLOW or REVIEW. The only
inputs that can produce a deletion are a confident AI verdict, or — in the
explicitly opted-in local-only mode — an anatomical detection above a threshold
set higher than every true positive this deployment has ever measured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from . import config
from .ai_moderation import ModerationVerdict
from .decision import Decision, DecisionResult

log = logging.getLogger("guardbot.policy")


class Action(str, Enum):
    """What to do. Note what is *not* here: there is no restrict, no ban, no
    kick. The moderation path cannot punish, so no bug in it can."""

    ALLOW = "allow"
    REVIEW = "review"
    DELETE_WARN = "delete_warn"


# Why a verdict counts as "the AI spoke". Used for the log and for the tests
# that assert a failure is distinguishable from an answer.
SOURCE_AI = "ai"
SOURCE_LOCAL = "local"
SOURCE_BOTH = "local+ai"
SOURCE_NONE = "none"
SOURCE_EXEMPT = "exempt"
SOURCE_DISABLED = "disabled"


@dataclass(frozen=True)
class PolicyInput:
    """Everything the decision may depend on, and nothing else.

    Keeping this a plain value rather than reaching into a Telegram update is
    what makes the engine testable and what makes it impossible for a new field
    of a message to start influencing a deletion by accident.
    """

    # The local detector's verdict, or None when the local stage did not run.
    local: DecisionResult | None = None
    # The moderation AI's verdict, or None when it was not asked.
    ai: ModerationVerdict | None = None
    # The ``app/media.py`` kind, for the log. Never used to decide anything:
    # what matters is the evidence, not the container format.
    media_kind: str = ""
    is_media: bool = False
    # Whether the author is exempt (the owner, the whitelist, a chat admin).
    # Exemption is checked by the caller, which has the Telegram objects; this
    # engine only honours the flag.
    exempt: bool = False


@dataclass(frozen=True)
class PolicyOutcome:
    action: Action
    reason: str
    source: str = SOURCE_NONE
    detail: str = ""
    ai_classification: str = ""
    ai_confidence: float = 0.0
    local_score: float = 0.0
    local_label: str = ""

    @property
    def deletes(self) -> bool:
        return self.action is Action.DELETE_WARN

    @property
    def reviews(self) -> bool:
        return self.action is Action.REVIEW

    @property
    def allows(self) -> bool:
        return self.action is Action.ALLOW


def _local_top_score(local: DecisionResult | None) -> float:
    """The strongest explicit-class detection score the local stage found."""
    if local is None or local.matched is None:
        return 0.0
    return float(local.matched.score)


def _local_explicit(local: DecisionResult | None) -> bool:
    """Whether the local stage called this EXPLICIT.

    Note this is the *old* threshold's verdict, and it is deliberately still
    computed: it is what makes a disagreement with the AI visible, and the
    disagreement is the interesting case — it is the shape of every false
    positive this design was built to stop.
    """
    return local is not None and local.decision is Decision.EXPLICIT


def _local_hard(local: DecisionResult | None) -> bool:
    """Whether the local evidence clears the *hard* bar.

    Only the anatomical detector can clear it. A scene-only score cannot,
    because the scene classifier is the less interpretable of the two and the
    cases it was added for — a sexual act with no exposed anatomy — are exactly
    the cases the moderation AI now handles with a reason attached.

    ``MODERATION_LOCAL_HARD_THRESHOLD`` defaults above every true positive this
    deployment has measured, so this is a deliberately narrow door.
    """
    if local is None or local.matched is None:
        return False
    return float(local.matched.score) >= float(config.MODERATION_LOCAL_HARD_THRESHOLD)


def _ai_confirms(ai: ModerationVerdict | None) -> bool:
    """A confident, non-uncertain, deletable classification."""
    return bool(ai is not None and ai.explicit)


def _ai_declines(ai: ModerationVerdict | None) -> bool:
    """The AI answered, and its answer was not a deletable class.

    This includes `suggestive`, which is the answer that matters most: a
    suggestive classification from the AI is a direct statement that the content
    is *not* explicit, and it is what stops the local detector's borderline score
    from becoming a deletion.
    """
    return bool(
        ai is not None and ai.decided and not ai.deletable_class
    )


def _ai_uncertain(ai: ModerationVerdict | None) -> bool:
    """The AI answered with a deletable class but flagged itself unsure, or at
    a confidence below the delete floor.

    Distinct from ``_ai_declines``: the AI is pointing at the right thing but
    does not stand behind it, which is a review, not an allow and not a delete.
    """
    if ai is None or not ai.decided:
        return False
    if not ai.deletable_class:
        return False
    return bool(ai.uncertain or ai.confidence < float(config.MODERATION_DELETE_CONFIDENCE))


def _ai_unavailable(ai: ModerationVerdict | None) -> bool:
    """The AI was asked and could not answer.

    ``None`` counts: the caller did not ask, which is the same fact as far as
    this engine is concerned.
    """
    if ai is None:
        return True
    return not ai.decided


def decide(inp: PolicyInput) -> PolicyOutcome:
    """Apply the policy. Pure: no I/O, no clock, no randomness.

    The rules, in order, and each one is a sentence from the brief:

    1. Moderation switched off entirely -> allow everything.
    2. The author is exempt -> allow.
    3. The AI confirms clearly explicit content -> delete and warn.
    4. The local detector says explicit and the AI says it is *not* -> review.
       Nothing is deleted. **This is the false-positive fix.**
    5. The local detector says explicit and the AI is unsure -> review.
    6. The local detector says explicit and the AI could not be asked ->
       review, unless the operator has explicitly opted into the local-only
       path *and* the anatomical evidence clears the hard bar.
    7. The AI thinks it is worth a human's attention -> review.
    8. The local stage wanted a human's attention -> review.
    9. Otherwise -> allow.
    """
    if not config.MODERATION_ENABLED:
        return PolicyOutcome(Action.ALLOW, "policy_disabled", source=SOURCE_DISABLED)

    if inp.exempt:
        return PolicyOutcome(Action.ALLOW, "exempt", source=SOURCE_EXEMPT)

    ai = inp.ai
    local = inp.local
    local_explicit = _local_explicit(local)
    score = _local_top_score(local)
    label = local.matched.label if local is not None and local.matched else ""
    shared = dict(
        ai_classification=ai.classification if ai and ai.decided else "",
        ai_confidence=ai.confidence if ai else 0.0,
        local_score=score,
        local_label=label,
    )

    # 3. The AI's confirmation is the strongest evidence available, and the only
    #    signal that can delete while the AI layer is in force.
    if _ai_confirms(ai):
        return PolicyOutcome(
            Action.DELETE_WARN,
            "ai_confirmed_explicit",
            source=SOURCE_AI,
            detail=f"{ai.classification} {ai.confidence:.2f}",
            **shared,
        )

    # 4. Disagreement. The detector escalated, the AI declined. The AI wins,
    #    because "a second opinion that can say no" is the entire reason it is
    #    here — and because deleting on a disputed signal is the exact failure
    #    this design exists to stop.
    if local_explicit and _ai_declines(ai):
        return PolicyOutcome(
            Action.REVIEW,
            "local_explicit_ai_declined",
            source=SOURCE_BOTH,
            detail=f"{label} {score:.2f} vs {ai.classification} {ai.confidence:.2f}",
            **shared,
        )

    # 5. The AI sees something, but will not stand behind it.
    if local_explicit and _ai_uncertain(ai):
        return PolicyOutcome(
            Action.REVIEW,
            "ai_uncertain",
            source=SOURCE_BOTH,
            detail=f"{label} {score:.2f} vs {ai.classification} {ai.confidence:.2f}",
            **shared,
        )

    # 6. Nobody could confirm. Fail safe: a human looks, nothing is destroyed.
    if local_explicit:
        if not config.MODERATION_REQUIRE_AI_CONFIRM and _local_hard(local):
            return PolicyOutcome(
                Action.DELETE_WARN,
                "local_only_hard_evidence",
                source=SOURCE_LOCAL,
                detail=f"{label} {score:.2f} >= "
                f"{float(config.MODERATION_LOCAL_HARD_THRESHOLD):.2f}",
                **shared,
            )
        return PolicyOutcome(
            Action.REVIEW,
            "no_ai_confirmation",
            source=SOURCE_LOCAL,
            detail=f"{label} {score:.2f}",
            **shared,
        )

    # 7. The AI flagged something that is not deletable — suggestive, spam,
    #    harassment, a threat. Logged for a human; no action taken. This is the
    #    "recommend a future restriction" half of the brief, made visible
    #    without being acted on.
    if ai is not None and ai.worth_reviewing:
        return PolicyOutcome(
            Action.REVIEW,
            f"ai_{ai.classification}",
            source=SOURCE_AI,
            detail=f"{ai.confidence:.2f} {ai.category}".strip(),
            **shared,
        )

    # 8. The local stage wanted a human. Preserved from the original policy so
    #    the REVIEW signal keeps working exactly as before.
    if local is not None and local.decision is Decision.REVIEW:
        return PolicyOutcome(
            Action.REVIEW,
            "local_review",
            source=SOURCE_LOCAL,
            detail=local.reason,
            **shared,
        )

    # 9.
    return PolicyOutcome(Action.ALLOW, "no_evidence", source=SOURCE_NONE, **shared)


def enforce_result(outcome: PolicyOutcome, local: DecisionResult | None) -> DecisionResult:
    """Adapt a policy outcome to the shape ``moderation.enforce`` already takes.

    The executor is reused rather than reimplemented, and that matters: its
    safety contract — a failed delete applies no strike and no restriction — is
    the property that stops an internal error from punishing a member. Writing a
    second deleter would mean writing that contract twice.

    The mapping is deliberately lossy in one direction only: an action that is
    not ``DELETE_WARN`` can never produce a ``Decision.EXPLICIT``, so nothing
    downstream of this function can delete by accident.
    """
    base = dict(
        matched=local.matched if local is not None else None,
        scene_nsfw=local.scene_nsfw if local is not None else None,
        frames_checked=local.frames_checked if local is not None else 0,
    )
    if outcome.deletes:
        return DecisionResult(
            Decision.EXPLICIT,
            f"{outcome.reason}: {outcome.detail}".strip(": "),
            source=outcome.source,
            **base,
        )
    if outcome.reviews:
        return DecisionResult(
            Decision.REVIEW,
            f"{outcome.reason}: {outcome.detail}".strip(": "),
            source=outcome.source,
            **base,
        )
    return DecisionResult(
        Decision.SAFE,
        f"{outcome.reason}: {outcome.detail}".strip(": "),
        source=outcome.source,
        **base,
    )


def describe(outcome: PolicyOutcome) -> str:
    """One compact line for the log, with no content in it."""
    return (
        f"action={outcome.action.value} reason={outcome.reason} "
        f"source={outcome.source} ai={outcome.ai_classification or '-'}"
        f":{outcome.ai_confidence:.2f} local={outcome.local_label or '-'}"
        f":{outcome.local_score:.2f}"
    )
