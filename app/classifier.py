"""The layered decision: fast rules first, Gemini only when the rules are unsure.

This module is the only place the two classifiers meet, and it exists so that
neither of them has to know about the other. ``app/intent.py`` stays pure and
offline; ``app/ai_intent.py`` stays ignorant of what the rules said. The policy
about which one to believe — and what "unsure" means — is here, in one readable
function.

The order is not an optimisation, it is the design:

1. **A veto is final.** A message that matched an ``ignore`` pattern (a rival
   seller advertising) is decided, and the AI layer is never even asked. The
   point of the veto is to protect the group from noise; letting a language
   model reconsider it would make the guard only as reliable as the model's
   resistance to a persuasive message.
2. **A rule match is a decision, not a suggestion.** The rules were written so
   that a match means topic *and* a request/problem, or an unambiguous phrase.
   When they say yes, the answer is yes — and no quota is spent confirming it.
3. **A message with no subject signal is ordinary.** Silence from the rules is
   only ambiguous when the message was *about* something relevant. Otherwise
   the silence stands, and no call is made.
4. **Only then, the model.** What is left is the genuinely uncertain middle:
   messages that are about circumvention or connectivity but that no pattern
   described well enough to decide. That is the gap this whole layer exists to
   close, and it is a small slice of a group's traffic.

Every path returns a ``Verdict`` and none of them raise. When the AI layer
fails, step 3's answer is the answer, so a Gemini outage is indistinguishable
from the bot as it was before the layer existed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import ai_intent, intent

log = logging.getLogger("guardbot.intent")

# What decided a message. Reported in the log and asserted in the tests.
SOURCE_RULES = "rules"
SOURCE_AI = "ai"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class Verdict:
    """The final answer, and enough detail to explain it afterwards."""

    triggered: bool
    source: str
    reasons: tuple
    normalised: str
    score: int
    ai: ai_intent.AiVerdict | None = None

    def __bool__(self) -> bool:
        return self.triggered


def _log(verdict: Verdict, user_id) -> None:
    """One line per decision, with every field a later investigation needs.

    Deliberately one line rather than several: the interesting question is
    always "why did (or didn't) this message get an offer", and that is only
    answerable if the rule verdict and the AI verdict are visible together.

    Every AI field is read through ``ai is not None`` rather than ``if ai``.
    ``AiVerdict.__bool__`` reports *relevance*, so truthiness blanks the whole
    AI half of the line on exactly the verdicts worth investigating — the ones
    where the model was asked and said no. That was a real defect: a successful
    `200 OK` answering ``ordinary_conversation`` printed as
    ``ai_consulted=False ai_category=-``, which reads as "never asked".
    """
    ai = verdict.ai
    log.info(
        "[intent] user=%s triggered=%s source=%s score=%d rules=%s "
        "ai_consulted=%s ai_skip=%s ai_error=%s ai_category=%s "
        "ai_problem=%s ai_response=%s ai_confidence=%.2f "
        "ai_reason=%s text=%r",
        user_id,
        verdict.triggered,
        verdict.source,
        verdict.score,
        ",".join(verdict.reasons) or "-",
        ai.consulted if ai is not None else False,
        (ai.skipped if ai is not None else "") or "-",
        (ai.error if ai is not None else "") or "-",
        (ai.category if ai is not None else "") or "-",
        (ai.problem_kind if ai is not None else "") or "-",
        (ai.response_kind if ai is not None else "") or "-",
        (ai.confidence if ai is not None else 0.0),
        (ai.reason if ai is not None else "") or "-",
        verdict.normalised[:120],
    )


async def classify(text: str | None, *, user_id=None) -> Verdict:
    """Decide whether one group message is a lead.

    ``user_id`` is only used to make the log line attributable; it changes no
    decision. Never raises — the caller is a Telegram handler and a classifier
    that can fail a message handler is worse than one that misses a lead.
    """
    match = intent.detect(text)

    # 1. The veto. Final, and never re-opened by the model.
    if "ignore" in match.reasons:
        verdict = Verdict(False, SOURCE_RULES, match.reasons, match.normalised, match.score)
        _log(verdict, user_id)
        return verdict

    # 2. The rules are sure. No call, no quota, no latency.
    if match.matched:
        verdict = Verdict(True, SOURCE_RULES, match.reasons, match.normalised, match.score)
        _log(verdict, user_id)
        return verdict

    # 3. Nothing about this message was relevant. The rules' silence stands.
    if not intent.is_candidate(match):
        return Verdict(False, SOURCE_NONE, match.reasons, match.normalised, match.score)

    # 4. The uncertain middle. One bounded, structured question.
    ai = await ai_intent.classify(match.normalised)
    verdict = Verdict(
        bool(ai),
        # `decided` rather than `consulted`: a call that failed or came back
        # malformed contributed nothing, and the decision really was the rules'
        # silence. Reporting "ai" for it would overstate what happened.
        SOURCE_AI if ai.decided else SOURCE_NONE,
        match.reasons,
        match.normalised,
        match.score,
        ai=ai,
    )
    _log(verdict, user_id)
    return verdict
