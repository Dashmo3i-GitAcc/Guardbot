"""The decision. Deterministic, in code, and the only thing that can act.

Everything upstream of this module produces *evidence*: the moderation AI
produces a structured verdict, a pattern rule produces a hit. None of them may
act, and none of them can — they have no Telegram client and no reference to
one. This module turns evidence into an action, and ``app/main.py`` performs it.

That ordering is the architecture the brief asks for, and it is worth stating
what it buys:

* **A prompt-injected message cannot do anything.** The worst a manipulated
  model can achieve is a wrong verdict, which still has to pass the rules below
  and still cannot delete anything on its own.
* **The rules are testable.** Every branch below is a pure function of its
  inputs, so "an ordinary message is not deleted" is a unit test rather than a
  hope.

**The action set is closed and contains no punishment.** There is no BAN and no
MUTE member of ``Action``, and that is the point rather than an omission: the
brief asks that the moderation AI must not be able to ban or mute anybody, and
the way to guarantee that is for the vocabulary to have no word for it. A future
phase that wants automatic restriction adds a member here, a rule below, and a
permission in ``app/rbac.py`` — the AI layer does not change at all.

**Fail safe, always.** Every uncertainty resolves to ALLOW or REVIEW. The only
input that can produce a deletion is a confident AI verdict.

**What is no longer here.** This module used to weigh a local visual detector
against the AI — a demoted-to-evidence NudeNet score, a scene classifier, and a
``local_only_hard_evidence`` mode in which an anatomical detection could delete
on its own. That whole subsystem was removed, so those rules and their helpers
are gone with it. The policy is now exactly: the AI's verdict, the exemption
flag, and the master switch.
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

    # The moderation AI's verdict, or None when it was not asked.
    ai: ModerationVerdict | None = None
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

    @property
    def deletes(self) -> bool:
        return self.action is Action.DELETE_WARN

    @property
    def reviews(self) -> bool:
        return self.action is Action.REVIEW

    @property
    def allows(self) -> bool:
        return self.action is Action.ALLOW


def _ai_confirms(ai: ModerationVerdict | None) -> bool:
    """A confident, non-uncertain, deletable classification."""
    return bool(ai is not None and ai.explicit)


def decide(inp: PolicyInput) -> PolicyOutcome:
    """Apply the policy. Pure: no I/O, no clock, no randomness.

    The rules, in order, and each one is a sentence from the brief:

    1. Moderation switched off entirely -> allow everything.
    2. The author is exempt -> allow.
    3. The AI confirms clearly explicit content -> delete and warn.
    4. The AI flagged something that is not deletable — suggestive, spam,
       harassment, a threat — or a deletable class it is not confident enough
       about -> review. Logged for a human; no action taken.
    5. Otherwise -> allow.
    """
    if not config.MODERATION_ENABLED:
        return PolicyOutcome(Action.ALLOW, "policy_disabled", source=SOURCE_DISABLED)

    if inp.exempt:
        return PolicyOutcome(Action.ALLOW, "exempt", source=SOURCE_EXEMPT)

    ai = inp.ai
    shared = dict(
        ai_classification=ai.classification if ai and ai.decided else "",
        ai_confidence=ai.confidence if ai else 0.0,
    )

    # 3. The AI's confirmation is the strongest evidence available, and the only
    #    signal that can delete.
    if _ai_confirms(ai):
        return PolicyOutcome(
            Action.DELETE_WARN,
            "ai_confirmed_explicit",
            source=SOURCE_AI,
            detail=f"{ai.classification} {ai.confidence:.2f}",
            **shared,
        )

    # 4. The AI flagged something a human should see — a suggestive
    #    classification, or a deletable one whose confidence fell short. This is
    #    the "recommend a future restriction" half of the brief, made visible
    #    without being acted on.
    if ai is not None and ai.worth_reviewing:
        return PolicyOutcome(
            Action.REVIEW,
            f"ai_{ai.classification}",
            source=SOURCE_AI,
            detail=f"{ai.confidence:.2f} {ai.category}".strip(),
            **shared,
        )

    # 5.
    return PolicyOutcome(Action.ALLOW, "no_evidence", source=SOURCE_NONE, **shared)


def enforce_result(outcome: PolicyOutcome) -> DecisionResult:
    """Adapt a policy outcome to the shape ``moderation.enforce`` already takes.

    The executor is reused rather than reimplemented, and that matters: its
    safety contract — a failed delete applies no strike and no restriction — is
    the property that stops an internal error from punishing a member. Writing a
    second deleter would mean writing that contract twice.

    The mapping is deliberately lossy in one direction only: an action that is
    not ``DELETE_WARN`` can never produce a ``Decision.EXPLICIT``, so nothing
    downstream of this function can delete by accident.
    """
    reason = f"{outcome.reason}: {outcome.detail}".strip(": ")
    if outcome.deletes:
        return DecisionResult(Decision.EXPLICIT, reason)
    if outcome.reviews:
        return DecisionResult(Decision.REVIEW, reason)
    return DecisionResult(Decision.SAFE, reason)


def describe(outcome: PolicyOutcome) -> str:
    """One compact line for the log, with no content in it."""
    return (
        f"action={outcome.action.value} reason={outcome.reason} "
        f"source={outcome.source} ai={outcome.ai_classification or '-'}"
        f":{outcome.ai_confidence:.2f}"
    )
