"""How much answer the message is asking for.

The owner's complaint this module closes: he asked for the news, the search ran,
and the answer came back summarised. The fix has two halves — the persona no
longer states a house length (``test_chat.py`` pins that) and this reader says,
per message, when completeness was actually requested.

These tests pin the *reading*, which is a closed vocabulary and therefore
assertable. They also pin the two properties that make it safe to place in the
trusted context: it is empty for an ordinary message, and it can never fail a
turn.
"""
import pytest

from app import answer_shape


# ── The reading ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "کامل توضیح بده",
        "همه چیز رو بگو",
        "با جزییات بگو",
        "مفصل توضیح بده",
        "explain in detail",
        "tell me everything",
    ],
)
def test_an_explicit_request_for_completeness_reads_as_full(text):
    assert answer_shape.read(text) == answer_shape.KIND_FULL


@pytest.mark.parametrize(
    "text",
    [
        "خبر بده",
        "آخرین خبرها چیه؟",
        "خبراش رو بیار",
        "اخبار جدید رو بگو",
        "latest news",
        "search for it",
        "سرچ کن",
    ],
)
def test_a_request_for_current_information_reads_as_news(text):
    assert answer_shape.read(text) == answer_shape.KIND_NEWS


@pytest.mark.parametrize(
    "text",
    ["خلاصه بگو", "مختصر بگو", "توی یه خط بگو", "briefly", "tldr"],
)
def test_an_explicit_request_for_brevity_reads_as_short(text):
    assert answer_shape.read(text) == answer_shape.KIND_SHORT


@pytest.mark.parametrize(
    "text",
    ["سلام", "حالت چطوره؟", "قیمت چنده؟", "این عکس قشنگه", "باشه ممنون"],
)
def test_an_ordinary_message_asks_for_nothing_in_particular(text):
    """The default is empty: the persona's own default is right for these."""
    assert answer_shape.read(text) == answer_shape.KIND_NORMAL


def test_a_question_about_a_summary_is_not_a_request_for_one():
    """«خلاصهش چی بود؟» asks what the summary was, not for a summary."""
    assert answer_shape.read("خلاصه‌ش چی بود؟") == answer_shape.KIND_NORMAL


def test_full_beats_news_when_both_are_present():
    assert answer_shape.read("خبر بده و کامل توضیح بده") == answer_shape.KIND_FULL


# ── The rendering ─────────────────────────────────────────────────────────
def test_an_ordinary_message_renders_nothing():
    """No heading for something that is not there."""
    assert answer_shape.render(answer_shape.KIND_NORMAL) == ""


def test_every_other_kind_renders_a_server_reading():
    for kind in (answer_shape.KIND_FULL, answer_shape.KIND_NEWS, answer_shape.KIND_SHORT):
        block = answer_shape.render(kind)
        assert block.startswith("\n")
        assert "read by the server" in block
        assert block.endswith("\n")


def test_the_full_reading_forbids_summarising():
    block = answer_shape.render(answer_shape.KIND_FULL).lower()
    assert "do not summarise" in block


def test_the_news_reading_forbids_a_one_line_summary():
    block = answer_shape.render(answer_shape.KIND_NEWS).lower()
    assert "not a one-line summary" in block


def test_the_reading_never_raises_on_junk():
    for junk in (None, "", 0, object()):
        assert answer_shape.read(junk) == answer_shape.KIND_NORMAL
    assert answer_shape.render("not-a-kind") == ""
