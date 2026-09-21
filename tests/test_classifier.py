"""The seam between the rule engine and Gemini.

What matters here is not what either classifier decides — those have their own
tests — but *which one is allowed to decide*. Four properties are pinned:

* a rule match is final, and costs no quota;
* a veto is final, and the model is never even asked;
* ordinary chatter is not escalated, so the daily quota is not spent on it;
* everything in between goes to the model, and a model that is down leaves the
  rules' answer standing.

The tests replace ``ai_intent._request``, so "was the AI consulted" is a
question about a call count rather than about a mock's mood.
"""
import asyncio
import json

import pytest

from app import ai_intent, classifier, config, db, intent


@pytest.fixture(autouse=True)
def layer(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key-not-a-real-one")
    monkeypatch.setattr(config, "GEMINI_MIN_CONFIDENCE", 0.55)
    monkeypatch.setattr(config, "GEMINI_DAILY_LIMIT", 100)
    monkeypatch.setattr(config, "GEMINI_RATE_LIMIT", 100)
    monkeypatch.setattr(config, "GEMINI_CIRCUIT_FAILURES", 5)
    monkeypatch.setattr(config, "GEMINI_MAX_RETRIES", 0)
    ai_intent.reset_state()
    yield
    ai_intent.reset_state()


class Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    @property
    def count(self):
        return len(self.calls)


def install(monkeypatch, reply) -> Recorder:
    recorder = Recorder(reply)
    monkeypatch.setattr(ai_intent, "_request", recorder)
    return recorder


def says(**overrides) -> str:
    payload = {
        "is_relevant": True,
        "intent_category": "vpn_request",
        "confidence": 0.9,
        "needs_acquisition_offer": True,
        "reason": "Asks for a VPN.",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def classify(text, **kwargs):
    return asyncio.run(classifier.classify(text, **kwargs))


# ── The rules decide, and the AI is not asked ─────────────────────────────
def test_a_rule_match_is_a_decision_and_costs_nothing(monkeypatch):
    recorder = install(monkeypatch, says())

    verdict = classify("فیلترشکن میخوام")

    assert verdict.triggered is True
    assert verdict.source == classifier.SOURCE_RULES
    assert recorder.count == 0, "a confident rule match must not spend a call"


def test_the_ai_cannot_talk_us_out_of_a_rule_match(monkeypatch):
    """Precision is not the model's job. The rules were written deliberately, and
    a second opinion on a message they were sure about would only add a way to
    lose a lead."""
    recorder = install(
        monkeypatch, says(is_relevant=False, needs_acquisition_offer=False)
    )
    assert classify("فیلترشکن میخوام").triggered is True
    assert recorder.count == 0


def test_a_veto_is_final_and_the_ai_is_never_asked(monkeypatch):
    """A rival seller advertising in the group is not a customer, and no amount
    of prompt-shaped persuasion should be able to reopen that."""
    recorder = install(monkeypatch, says(confidence=1.0))

    verdict = classify("فیلترشکن میفروشم ارزون")

    assert verdict.triggered is False
    assert verdict.source == classifier.SOURCE_RULES
    assert "ignore" in verdict.reasons
    assert recorder.count == 0


def test_ordinary_chatter_is_not_escalated(monkeypatch):
    """The quota is for the uncertain middle, not for everyday sentences."""
    recorder = install(monkeypatch, says())

    for text in [
        "سلام صبح بخیر",
        "دیشب بازی رو دیدی؟",
        "لینک گروه رو بفرست",
        "چطور ثبت‌نام کنم؟",
        "ممنون از راهنماییت",
        "کسی اینجا مدیره؟ کمک",
    ]:
        verdict = classify(text)
        assert verdict.triggered is False, text
        assert verdict.source == classifier.SOURCE_NONE, text

    assert recorder.count == 0, "not one call for a group's ordinary traffic"


def test_a_pricing_question_is_the_model_s_job(monkeypatch):
    """'قیمتتون چنده' and 'قیمت گوشی چنده' have the same shape. A rule cannot
    tell them apart, so both are escalated and the model decides — which is the
    clearest example of what the second layer is for."""
    install(monkeypatch, says())
    assert classify("قیمت گوشی چنده؟").triggered is True, "the model said it is a lead"

    install(monkeypatch, says(is_relevant=False, needs_acquisition_offer=False))
    verdict = classify("قیمت گوشی چنده؟")
    assert verdict.triggered is False
    assert verdict.source == classifier.SOURCE_AI, "asked, and answered no"


# ── The uncertain middle goes to the model ────────────────────────────────
def test_a_message_about_the_subject_is_escalated(monkeypatch):
    recorder = install(monkeypatch, says())

    verdict = classify("کسی کانفیگ خوب داره؟")

    assert recorder.count == 1
    assert verdict.source == classifier.SOURCE_AI
    assert "topic" in verdict.reasons, "the rules' reasons travel with the verdict"
    assert verdict.ai is not None and verdict.ai.consulted is True


def test_the_model_can_promote_a_message_the_rules_missed(monkeypatch):
    install(monkeypatch, says(intent_category="connectivity_problem"))

    # No rule fires on this: the rules have no pattern for a plain ISP complaint
    # with no circumvention angle. That is exactly the gap this layer is for.
    assert intent.detect("اینترنت ایرانسل وصل نمیشه").matched is False
    verdict = classify("اینترنت ایرانسل وصل نمیشه")

    assert verdict.triggered is True
    assert verdict.source == classifier.SOURCE_AI


def test_the_model_can_leave_a_candidate_alone(monkeypatch):
    install(monkeypatch, says(is_relevant=False, needs_acquisition_offer=False))

    verdict = classify("vpn چیه؟")

    assert verdict.triggered is False
    assert verdict.source == classifier.SOURCE_AI, "it was asked; it said no"
    assert verdict.ai.consulted is True
    assert verdict.ai.relevant is False


def test_only_a_subject_hit_escalates_not_a_bare_request_word(monkeypatch):
    """'بفرست' and 'کمک' are the most common words in a group and mean nothing on
    their own, so a message that hit only those is ordinary, not ambiguous."""
    recorder = install(monkeypatch, says())

    # Support-only: not escalated.
    assert classify("لطفا راهنمایی کن").source == classifier.SOURCE_NONE
    assert recorder.count == 0

    # Subject-only: escalated.
    classify("اینستاگرام")
    assert recorder.count == 1


def test_the_model_sees_the_normalised_text(monkeypatch):
    """Both layers must be looking at the same string, or a verdict is about a
    message neither of them actually saw."""
    recorder = install(monkeypatch, says())

    classify("چرا اينستاگرام باز نميشه ۱۲۳")

    assert recorder.calls == ["چرا اینستاگرام باز نمیشه 123"]


# ── Failure: the rules' answer stands ─────────────────────────────────────
def test_a_gemini_outage_degrades_to_the_rules(monkeypatch):
    install(monkeypatch, RuntimeError("the network is gone"))

    ambiguous = classify("کسی کانفیگ خوب داره؟")
    assert ambiguous.triggered is False
    assert ambiguous.source == classifier.SOURCE_NONE
    assert ambiguous.ai.error, "the failure is visible in the verdict"

    # And the deterministic layer is entirely unaffected.
    assert classify("فیلترشکن میخوام").triggered is True
    assert classify("فیلترشکن میفروشم").triggered is False


def test_a_timeout_degrades_to_the_rules(monkeypatch):
    install(monkeypatch, asyncio.TimeoutError())
    verdict = classify("کسی کانفیگ خوب داره؟")
    assert verdict.triggered is False
    assert verdict.ai.error == "timeout"


def test_switching_the_layer_off_leaves_the_rules_working(monkeypatch):
    recorder = install(monkeypatch, says())
    monkeypatch.setattr(config, "GEMINI_ENABLED", False)

    assert classify("فیلترشکن میخوام").triggered is True
    assert classify("کسی کانفیگ خوب داره؟").triggered is False
    assert recorder.count == 0


def test_without_a_key_the_rules_are_all_there_is(monkeypatch):
    recorder = install(monkeypatch, says())
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    assert classify("فیلترشکن میخوام").triggered is True
    assert classify("کسی کانفیگ خوب داره؟").triggered is False
    assert recorder.count == 0


def test_an_exhausted_quota_degrades_to_the_rules(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_DAILY_LIMIT", 1)
    install(monkeypatch, says())

    assert classify("کسی کانفیگ خوب داره؟").triggered is True, "the one call we had"
    verdict = classify("اینستاگرام باز نمیشه")
    assert verdict.triggered is False
    assert verdict.ai.skipped == "daily_cap"

    # The rules are still the rules.
    assert classify("فیلترشکن میخوام").triggered is True


# ── The verdict is always a verdict ───────────────────────────────────────
def test_classify_never_raises(monkeypatch):
    install(monkeypatch, RuntimeError("anything at all"))

    for text in [None, "", " ", "x", "سلام", "🦄", "a" * 5000, "۱۲۳", "\x00"]:
        verdict = classify(text)
        assert isinstance(verdict, classifier.Verdict)
        assert isinstance(verdict.triggered, bool)


def test_the_verdict_carries_the_evidence(monkeypatch):
    install(monkeypatch, says())

    verdict = classify("کسی کانفیگ خوب داره؟", user_id=12345)

    assert verdict.normalised == intent.normalise("کسی کانفیگ خوب داره؟")
    assert verdict.score == 2, "a topic hit alone"
    assert verdict.reasons == ("topic", "candidate")
    assert verdict.ai.category == "vpn_request"


def test_the_log_line_names_the_user_and_both_verdicts(monkeypatch, caplog):
    install(monkeypatch, says(intent_category="connectivity_problem", confidence=0.8))

    with caplog.at_level("INFO"):
        classify("اینترنت ایرانسل وصل نمیشه", user_id=987654)

    text = caplog.text
    assert "user=987654" in text
    assert "triggered=True" in text
    assert "source=ai" in text
    assert "ai_category=connectivity_problem" in text
    assert "ai_confidence=0.80" in text
