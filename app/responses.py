"""Which reply a lead gets, and the words for it.

Two layers, and the split matters:

* The AI layer returns a **key** — ``connectivity_offer``, ``access_offer``,
  ``vpn_offer``, ``pricing_offer``, ``generic_offer`` — and never a sentence.
  A language model is very good at telling a complaint about a slow connection
  from a question about price, and a very bad thing to trust with the exact
  words a stranger reads. So it picks, and this module speaks.
* The deterministic rules decided some messages before the model was ever
  consulted. Those have no AI verdict to ask, so the reply is derived from
  *which* patterns matched — coarser, and honest about being coarser: the rules
  know that a problem pattern fired, not what the person is complaining about.

Everything the group reads therefore comes from ``app/config.py``, which is the
same place every other word the group sees comes from. Nothing here can be
influenced by the message text: a key that is not in the closed set falls back
to the generic wording, so a hostile or confused model answer can change *which*
of five fixed sentences is sent and nothing else.
"""
from __future__ import annotations

from . import ai_intent, config

# kind -> the config attribute holding its wording. Resolved through getattr at
# call time rather than copied into a dict at import, so a test — or an
# operator's .env — that changes the copy is honoured without reimporting.
_ATTRS = {
    "connectivity_offer": "GROUP_TRIAL_REPLY_CONNECTIVITY",
    "access_offer": "GROUP_TRIAL_REPLY_ACCESS",
    "vpn_offer": "GROUP_TRIAL_REPLY_VPN",
    "pricing_offer": "GROUP_TRIAL_REPLY_PRICING",
    # The wording this flow used before it could tell the cases apart.
    ai_intent.DEFAULT_RESPONSE_KIND: "GROUP_TRIAL_INVITE_TEXT",
}

# Which rule patterns imply which reply. Ordered: the first match wins.
#
# The mapping is the fix for a reported symptom: the assistant seemed to answer
# everything with the "your internet is weak" sentence. The cause was here, and
# it was not a phrase to delete — it was that the broad ``problem`` group (وصل
# نمیشه، باز نمیشه، کار نمیکنه — a blocked service or a thing that will not
# load) was mapped to the *connectivity* wording, which is written for a
# complaint about the speaker's own line. So every blocked-app complaint was
# answered as though the person had said their internet was slow.
#
# Now only the specific ``poor_internet`` signal — «اینترنتم ضعیفه», «نتم خراب
# شده» — produces the connectivity wording, and a generic ``problem`` produces
# the access wording, which is what the AI layer already distinguishes in its
# own prompt (a named blocked service is ``access_offer``, a slow line is
# ``connectivity_offer``). The rule path and the model path now agree.
_RULE_HINTS = (
    ("poor_internet", "connectivity_offer"),
    ("problem", "access_offer"),
    ("request", "vpn_offer"),
)


def kind_for(verdict) -> str:
    """The response kind for a verdict that is about to be answered.

    The model's answer is used only when the model actually decided something.
    A failed or malformed call contributed nothing, so falling back to the rule
    hints is not a downgrade — it is the same answer the flow would have given
    before the AI layer existed.

    The value is checked against the closed set *again* here, even though
    ``parse_verdict`` already coerced it. The reason is that this string is
    interpolated into a log line, and a value carrying a newline could forge
    one. The invariant this function owes its callers is that it returns a
    member of ``RESPONSE_KINDS`` no matter what it was handed.
    """
    ai = verdict.ai
    if ai is not None and ai.decided:
        if ai.response_kind in ai_intent.RESPONSE_KINDS:
            return ai.response_kind
        return ai_intent.DEFAULT_RESPONSE_KIND

    reasons = verdict.reasons or ()
    for reason, kind in _RULE_HINTS:
        if reason in reasons:
            return kind
    return ai_intent.DEFAULT_RESPONSE_KIND


def text_for(kind: str, name: str) -> str:
    """The lead-in sentence for a kind, with ``{name}`` filled in.

    An unknown kind is not an error: it is the generic wording. That is the one
    path by which a model answer can reach the group, and all it can do is
    select one of a fixed set.
    """
    attr = _ATTRS.get(kind) or _ATTRS[ai_intent.DEFAULT_RESPONSE_KIND]
    return getattr(config, attr).format(name=name)


def reply_for(verdict, name: str) -> tuple[str, str]:
    """``(kind, body)`` for a lead. The kind is for the log, the body for the group.

    The allowance hint is appended here rather than repeated in every wording,
    so the five sentences above stay about the person's problem and the one
    sentence about what the test *is* stays in one place.
    """
    kind = kind_for(verdict)
    return kind, f"{text_for(kind, name)}\n\n{config.GROUP_TRIAL_HINT}"


__all__ = ["kind_for", "reply_for", "text_for"]
