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


# ── The ک ب shorthand ─────────────────────────────────────────────────────
# Iranian Persian abbreviates hard, and "ک ب X" is a complete, deliberate way of
# asking for help with X. It is written with the Persian ک, not the English K,
# and these are the exact forms the owner listed.
@pytest.mark.parametrize(
    "text",
    [
        "ک ب نت",
        "ک ب اینترنت",
        "ک ب وی پی ان",
        "ک ب VPN",
        "ک ب پروکسی",
        "ک ب فیلتر",
        "ک ب ایرانسل",
    ],
)
def test_the_k_b_shorthand_is_a_request_on_its_own(text):
    match = intent.detect(text)
    assert match.matched, f"missed: {text!r} -> {match.normalised!r}"
    assert "standalone" in match.reasons


@pytest.mark.parametrize(
    "text",
    [
        # The same thing, typed the other ways it arrives in a real group.
        "کبنت",              # no spaces at all
        "ک\u200cب\u200cنت",  # ZWNJ between the letters
        "ک  ب  نت",          # doubled spaces
        "ك ب نت",            # Arabic kaf
        "ک ب نت؟",           # with a question mark
        "سلام، ک ب نت",      # inside a sentence
        "ک ب نتم",           # with the possessive suffix
        "ک ب vpn لازم دارم",
        "ک ب فیلتر شکن",
        "ک ب اینترنتم",
    ],
)
def test_the_k_b_shorthand_survives_how_it_is_actually_typed(text):
    match = intent.detect(text)
    assert match.matched, f"missed: {text!r} -> {match.normalised!r}"


def test_the_k_b_shorthand_does_not_fire_inside_unrelated_words():
    """'ک ب' must not start matching words that merely contain those letters."""
    for text in ["کتاب میخوام", "یه کتاب خوب", "بک بک"]:
        assert not intent.detect(text).matched, f"false positive: {text!r}"


# ── The expanded vocabulary: what the rules now decide ────────────────────
@pytest.mark.parametrize(
    "text",
    [
        # How to get past the block, in the words people actually use.
        "چطور میتونم اینستاگرام رو باز کنم",
        "چطور فیلتر رو دور بزنم",
        "یه راهی برای عبور از فیلتر میخوام",
        "بدون محدودیت میخوام",
        "اینستاگرام بالا نمیاد",
        "باز کردن اینستا چطوریه",
        "چطور یوتیوب رو باز کنم",
        # Tools, named or transliterated.
        "کانفیگ vless میخواستم",
        "یه کانفیگ خوب دارید؟",
        "سینگ باکس داری؟",
        "هایستریا وصل نمیشه",
        "کلش دارید؟",
        "v2ray وصل نمیشه",
        # Buying and pricing, when the product is named.
        "قیمت اشتراکتون چنده؟",
        "اشتراکتون ماهانه چنده",
        # Complaints that are also requests.
        "فیلترشکنم دیسکانکت میشه",
        # A mobile operator, asked about directly.
        "فیلترشکن میخوام برای ایرانسل",
        # English mixed in.
        "proxies available?",
        "xray config میخواستم",
    ],
)
def test_the_expanded_vocabulary_recognises_a_request(text):
    assert intent.detect(text).matched, f"missed: {text!r}"


# ── The expanded vocabulary: what it deliberately leaves to the AI ────────
# These are the uncertain middle, and they are *supposed* to stay unresolved
# here. The rule engine's job is to be sure or to say nothing; deciding that
# "قیمت اشتراکتون چنده" is about a VPN rather than a Netflix subscription is
# exactly the judgement app/classifier.py sends to Gemini.
@pytest.mark.parametrize(
    "text",
    [
        "تعرفه تون چیه",
        "نرخ ماهانه چقدره",
        "میخواستم بدونم چنده",
        "لگ دارم رو نت",
        "سرعتم افتاده",
        "همراه اول چی پیشنهاد میدی",
        "قیمتتون چنده",
        "قطعی زیاد دارم چیکار کنم",
        "پینگم بالاست راهی داره",
    ],
)
def test_an_ambiguous_request_is_left_for_the_ai_layer(text):
    match = intent.detect(text)
    assert not match.matched, f"the rules should not decide this: {text!r}"
    assert intent.is_candidate(match), f"but it must reach the AI layer: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "اینترنت ایرانسل وصل نمیشه",
        "نت مخابرات افتضاحه",
        "رایتل نتم ضعیفه",
        "شاتل خیلی کنده",
        "اینترنتم خیلی ضعیفه",
        "نتم خرابه",
    ],
)
def test_a_connectivity_complaint_is_not_a_request_on_its_own(text):
    """Half the group complains about slow internet. Naming your ISP is not a
    statement about circumvention, so these stay ambiguous rather than decided."""
    assert not intent.detect(text).matched, f"false positive: {text!r}"


def test_a_seller_looking_for_a_seller_is_not_vetoed():
    """The veto is for people advertising, not for people shopping. The verb is
    first person on purpose."""
    assert intent.detect("فیلترشکن میفروشم ارزون").reasons == ("ignore",)
    assert intent.detect("vpn میفروشم").reasons == ("ignore",)

    looking = intent.detect("کسی فیلترشکن میفروشه؟")
    assert "ignore" not in looking.reasons


# ── The candidate gate ────────────────────────────────────────────────────
def test_a_candidate_hit_never_decides_anything():
    """Weight 0 is load-bearing: this group exists to permit an AI call, not to
    reach a verdict. If it could match on its own it would be a topic rule, and
    the whole point is that it is much looser than one."""
    match = intent.detect("اینستاگرام")
    assert not match.matched, "a candidate term alone is not an intent"
    assert "candidate" in match.reasons
    assert match.score == 0, "and it adds no score"

    # Adding the candidate group did not change what already matched.
    assert intent.detect("فیلترشکن میخوام").score == 3


def test_the_candidate_gate_marks_the_uncertain_middle():
    # About the subject, but no request and no complaint -> ask the AI.
    assert intent.is_candidate(intent.detect("vpn چیه؟"))
    assert intent.is_candidate(intent.detect("کسی کانفیگ خوب داره؟"))
    assert intent.is_candidate(intent.detect("اینستاگرام"))
    assert intent.is_candidate(intent.detect("اینترنت ایرانسل وصل نمیشه"))
    # A pricing question with no product named. "قیمتتون چنده" and "قیمت گوشی
    # چنده" are the same shape, and which one is a lead is a judgement — so the
    # rules escalate both and the model answers. One call each, on purpose.
    assert intent.is_candidate(intent.detect("قیمت گوشی چنده؟"))

    # Already decided, either way -> do not ask.
    assert not intent.is_candidate(intent.detect("فیلترشکن میخوام"))
    assert not intent.is_candidate(intent.detect("فیلترشکن میفروشم"))

    # Only a supporting signal, and no subject -> ordinary, not ambiguous.
    assert not intent.is_candidate(intent.detect("لطفا راهنمایی کن"))
    assert not intent.is_candidate(intent.detect("ممنون از راهنماییت"))


def test_the_candidate_gate_is_not_spent_on_everyday_words():
    """A call each for 'لینک' or 'بدون' would burn the daily quota on the
    messages least likely to be leads."""
    for text in [
        "لینک گروه رو بفرست",
        "بدون شک دیشب بازی قشنگی بود",
        "سلام صبح بخیر",
        "دیشب بازی رو دیدی؟",
        "ممنون از راهنماییت",
        "چطور ثبت‌نام کنم؟",
    ]:
        assert not intent.is_candidate(intent.detect(text)), f"wasteful: {text!r}"


def test_the_candidate_group_is_optional():
    """A deployment with its own rules file simply never escalates anything."""
    from dataclasses import replace

    rules = intent.load_rules()
    assert rules.candidates, "the shipped rules do have a candidate group"

    stripped = replace(rules, candidates=())
    assert not intent.is_candidate(intent.detect("اینستاگرام", rules=stripped))
