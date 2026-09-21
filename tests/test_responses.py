"""Which reply a lead gets, and the boundary that keeps a model out of it.

The rule under test is the one that matters for safety: the AI layer returns a
*key*, this module turns that key into a sentence, and every sentence is a
constant in ``app/config.py``. A model answer that is not one of the five keys
can only select the generic wording — it has no path to writing anything.

The second half is that the deterministic rules still get a sensible reply. They
decided many leads before the model was ever consulted, and they must keep
working when it is switched off, out of quota, or broken.
"""
from app import ai_intent, config, responses
from app.classifier import SOURCE_RULES, Verdict

KINDS = (
    "connectivity_offer",
    "access_offer",
    "vpn_offer",
    "pricing_offer",
    ai_intent.DEFAULT_RESPONSE_KIND,
)


def _verdict(*, ai=None, reasons=(), source=SOURCE_RULES):
    """A verdict that is going to be answered."""
    return Verdict(True, source, tuple(reasons), "متن پیام", 2, ai=ai)


def _ai(**overrides):
    base = {
        "consulted": True,
        "relevant": True,
        "category": "vpn_request",
        "confidence": 0.9,
        "needs_offer": True,
        "problem_kind": "wants_access_tool",
        "response_kind": "vpn_offer",
    }
    base.update(overrides)
    return ai_intent.AiVerdict(**base)


# ── The model picks a key; the app writes the sentence ────────────────────
def test_the_models_key_chooses_the_wording():
    verdict = _verdict(ai=_ai(response_kind="access_offer"))
    assert responses.kind_for(verdict) == "access_offer"

    _, body = responses.reply_for(verdict, "سارا")
    assert "فیلتر" in body, "the blocked-service wording, not the generic one"


def test_a_complaint_about_a_slow_connection_gets_the_connectivity_wording():
    verdict = _verdict(ai=_ai(problem_kind="slow_or_unstable",
                              response_kind="connectivity_offer"))
    _, body = responses.reply_for(verdict, "رضا")
    assert "ضعیف" in body or "ناپایدار" in body


def test_an_invented_key_cannot_reach_the_group():
    """The one path by which a model answer could touch the wording, closed."""
    for hostile in ("../../etc/passwd", "vpn_offer ", "", "GENERIC_OFFER", None, 7):
        verdict = _verdict(ai=_ai(response_kind=hostile))
        kind = responses.kind_for(verdict)
        assert kind in KINDS, kind
        assert responses.text_for(kind, "x") == config.GROUP_TRIAL_INVITE_TEXT.format(
            name="x"
        )


def test_an_unknown_kind_in_the_constant_table_is_the_generic_reply():
    assert responses.text_for("no_such_kind", "x") == (
        config.GROUP_TRIAL_INVITE_TEXT.format(name="x")
    )


# ── The deterministic path still works with no AI verdict at all ──────────
def test_a_rules_only_lead_gets_the_hint_its_patterns_imply():
    cases = (
        (("topic", "poor_internet", "candidate"), "connectivity_offer"),
        (("topic", "problem", "candidate"), "connectivity_offer"),
        (("topic", "request", "candidate"), "vpn_offer"),
        (("topic",), ai_intent.DEFAULT_RESPONSE_KIND),
        (("standalone",), ai_intent.DEFAULT_RESPONSE_KIND),
    )
    for reasons, expected in cases:
        assert responses.kind_for(_verdict(reasons=reasons)) == expected, reasons


def test_poor_internet_wins_over_a_general_problem_pattern():
    """A message can match both; the more specific one is the better answer."""
    verdict = _verdict(reasons=("problem", "poor_internet"))
    assert responses.kind_for(verdict) == "connectivity_offer"


def test_a_failed_ai_verdict_does_not_blank_the_reply():
    """`decided` is False for a failure, so the rule hints are used."""
    verdict = _verdict(ai=ai_intent.AiVerdict(consulted=True, error="timeout"),
                       reasons=("topic", "request"))
    assert responses.kind_for(verdict) == "vpn_offer"


# ── The wording itself ────────────────────────────────────────────────────
def test_every_kind_has_its_own_wording():
    texts = {kind: responses.text_for(kind, "x") for kind in KINDS}
    for kind, text in texts.items():
        assert text.strip(), kind
        assert "{name}" not in text, f"{kind} was not formatted"
    assert len(set(texts.values())) == len(KINDS), (
        "two kinds share a sentence, so the reply is not actually context-aware"
    )


def test_no_reply_can_carry_a_link_or_a_credential():
    """Nothing the group reads may contain connection material, ever."""
    forbidden = ("http", "://", "vless", "vmess", "trojan", "uuid", "subscription")
    for kind in KINDS:
        text = responses.text_for(kind, "سارا").lower()
        for word in forbidden:
            assert word not in text, f"{kind} mentions {word}"


def test_the_reply_carries_the_allowance_hint_exactly_once():
    for kind in KINDS:
        verdict = _verdict(ai=_ai(response_kind=kind))
        _, body = responses.reply_for(verdict, "سارا")
        assert body.count(config.GROUP_TRIAL_HINT) == 1, kind
        assert "سارا" in body
        assert "{name}" not in body
