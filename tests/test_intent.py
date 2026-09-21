"""Intent detection, against the way people actually write in the group.

Two things are being protected here. The obvious one is that someone asking for
a VPN is noticed. The less obvious, and the one that decides whether the feature
is usable, is that everyone *else* is left alone — a group where half the
messages get an offer of a free trial is a group nobody stays in.

The corpus below is written the way the group writes: Arabic yeh and kaf,
ZWNJ, missing spaces, slang, and complaints that are not requests.
"""
import json

import pytest

from app import intent


# ── Normalisation ─────────────────────────────────────────────────────────
def test_normalise_folds_arabic_and_persian_letter_forms():
    assert intent.normalise("ميخوام") == intent.normalise("میخوام")
    assert intent.normalise("فيلترشكن") == intent.normalise("فیلترشکن")
    assert intent.normalise("كتاب") == "کتاب"


def test_normalise_drops_zero_width_and_diacritics():
    # ZWNJ inside فیلترشکن, and a kasra that nobody types deliberately.
    assert intent.normalise("فیلتر\u200cشکن") == "فیلترشکن"
    assert intent.normalise("مُشکل") == "مشکل"


def test_normalise_folds_digits_and_punctuation():
    assert intent.normalise("۱۲۳") == "123"
    assert intent.normalise("٤٥٦") == "456"
    assert intent.normalise("چطوری؟") == "چطوری?"


def test_normalise_collapses_whitespace():
    assert intent.normalise("  وی   پی   ان  ") == "وی پی ان"


def test_normalise_handles_empty_input():
    assert intent.normalise("") == ""
    assert intent.normalise(None) == ""


# ── Positive: someone asking ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "سلام، یه فیلترشکن خوب معرفی میکنید؟",
        "وی پی ان میخوام",
        "vpn لازم دارم",
        "اینترنتم خیلی کنده، فیلترشکن داری؟",
        "فیلترشکن من کار نمیکنه چیکار کنم",
        "v2ray وصل نمیشه",
        "پروکسی دارید؟",
        "قیمت اشتراک vpn چنده؟",
        "فیلترشکن رایگان دارید",
        "تست رایگان دارید؟",
        "نتم خرابه، vpn چی پیشنهاد میدی؟",
        "سلام دوستان، چطور میتونم بدون فیلتر اینستا برم؟",
        "یه برنامه میخوام که اینستا باز شه",
        "وی‌پی‌ان لازم دارم، کمک کنید",
        "ميخوام فيلتر شكن بخرم",
        "ضد فیلتر چی دارین؟",
        "کانفیگ vless میخواستم",
        "کسی اینجا کلش داره؟ فیلترشکنم قطع و وصل داره",
        "از دیشب vpn بالا نمیاد",
        "vpn اشتراکش چنده؟",
    ],
)
def test_recognises_a_request_for_a_vpn(text):
    match = intent.detect(text)
    assert match.matched, f"missed: {text!r} -> {match.normalised!r}"


# ── Negative: everyone else ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "سلام صبح بخیر",
        "دیشب بازی رو دیدی؟",
        "لینک گروه رو بفرست",
        "چطور ثبت‌نام کنم؟",
        "کسی اینجا مدیره؟ کمک",
        "این برنامه رو از کجا دانلود کنم",
        "قیمت گوشی چنده؟",
        "ممنون از راهنماییت",
    ],
)
def test_ignores_ordinary_chatter(text):
    assert not intent.detect(text).matched, f"false positive: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # A bare mention is not a request. This is the single most important
        # guard: it is what stops a conversation *about* VPNs from triggering.
        "vpn هست",
        "فیلترشکن",
        "درباره فیلترشکن حرف زدیم",
        "vpn چیه؟",
    ],
)
def test_a_bare_mention_is_not_an_intent(text):
    match = intent.detect(text)
    assert not match.matched, f"topic alone must not trigger: {text!r}"
    assert "topic" in match.reasons


def test_a_connectivity_complaint_alone_is_not_an_intent():
    """Half the group complains about slow internet. That is not a lead."""
    for text in [
        "اینترنتم خیلی ضعیفه",
        "نتم خرابه",
        "اینترنت قطع و وصل داره",
    ]:
        assert not intent.detect(text).matched, f"false positive: {text!r}"


def test_a_competing_seller_is_vetoed():
    assert not intent.detect("فیلترشکن میفروشم ارزون").matched
    assert not intent.detect("vpn میفروشم، پیوی").matched


def test_empty_and_very_short_messages_are_ignored():
    assert not intent.detect("").matched
    assert not intent.detect(None).matched
    assert not intent.detect("سلام").matched
    # Below the minimum length, so it is not even examined for topics.
    assert not intent.detect("vpn").matched


# ── The knobs ─────────────────────────────────────────────────────────────
def test_relaxing_the_topic_rule_lets_connectivity_complaints_through():
    """Documented behaviour of INTENT_REQUIRE_TOPIC=0, not the default."""
    text = "اینترنتم خیلی ضعیفه"
    assert not intent.detect(text, require_topic=True).matched
    assert intent.detect(text, require_topic=False).matched


def test_min_length_is_respected():
    assert not intent.detect("فیلترشکن دارید", min_length=50).matched
    assert intent.detect("فیلترشکن دارید", min_length=4).matched


def test_reasons_explain_the_decision():
    match = intent.detect("فیلترشکن من کار نمیکنه چیکار کنم")
    assert "topic" in match.reasons
    assert "problem" in match.reasons
    assert "request" in match.reasons
    assert match.score >= 4


def test_standalone_phrase_needs_no_topic_word():
    match = intent.detect("سلام، چطور بدون فیلتر اینستا برم؟")
    assert match.matched
    assert "standalone" in match.reasons


# ── Rule loading ──────────────────────────────────────────────────────────
def test_shipped_rules_load_and_compile():
    rules = intent.load_rules()
    assert rules.topic, "the shipped rules must have topic patterns"
    assert rules.support, "the shipped rules must have supporting patterns"


def test_rules_can_be_extended_from_a_file(tmp_path, monkeypatch):
    """The whole point of keeping the vocabulary in JSON."""
    custom = {
        "topic_groups": ["topic"],
        "support_groups": ["request"],
        "groups": {
            "topic": {"patterns": ["شکننده\\s*فیلتر"]},
            "request": {"patterns": ["می\\s*خوام"]},
        },
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(custom), encoding="utf-8")

    monkeypatch.setattr(intent, "rules_path", lambda: str(path))
    intent.reload_rules()

    assert intent.detect("شکننده فیلتر میخوام").matched
    # The shipped vocabulary is gone, so a phrase it knew no longer matches.
    assert not intent.detect("فیلترشکن میخوام").matched


def test_a_broken_pattern_is_reported_clearly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "topic_groups": ["topic"],
                "support_groups": ["request"],
                "groups": {
                    "topic": {"patterns": ["(unclosed"]},
                    "request": {"patterns": ["میخوام"]},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid intent pattern"):
        intent._load(str(bad))
