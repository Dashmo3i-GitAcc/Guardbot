"""Being called by name, in the spellings a group actually uses.

The brief's requirement is that Nexus recognise its own name however it is
written and whatever it is called, and understand from the context whether it is
really being addressed. These tests hold the matcher to that, and — just as
importantly — hold it to *not* firing on words that merely share its consonants.
"""
import pytest

from app import addressing, config


@pytest.fixture(autouse=True)
def known_names(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "NEXUS_EXTRA_ACTION_WORDS", [])
    yield


# ── The spellings that must be recognised ─────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "نکسوس",
        "نکسی",
        "نکس",
        "نکسوس جان",
        "هی نکسوس",
        "نکسوسو ساکتش کن",
        "نکسوسیه",
        "نیکسوس",
        "نکسس",
        "نکسووووس",
        "nexus",
        "nexsus",
        "Nexus",
        "NEXUS",
        "@nexus_bot سلام",
        "nexus, mute him",
    ],
)
def test_every_way_a_group_writes_the_name_is_a_call(text):
    """A person calling Nexus is calling Nexus, whichever letters they used."""
    assert addressing.addressed(text) is True, text


@pytest.mark.parametrize(
    "text",
    [
        "سلام چطوری",
        "قیمت چنده",
        "برو بابا",
        "این آدم ناکسه",
        "ناکس",
        "noxious",
        "Noxious, do it",
        "anxious",
        "بنش کن",
        "",
        "   ",
    ],
)
def test_ordinary_talk_is_not_a_call(text):
    """The other half of the contract: it must not answer what was not for it."""
    assert addressing.addressed(text) is False, text


# ── The two grades ────────────────────────────────────────────────────────
def test_a_quotation_about_nexus_is_a_mention_not_a_call():
    """«نکسوس گفت که...» is somebody talking *about* the assistant.

    The name is spelled exactly, so it reads as addressed — that is the
    pre-existing rule for a configured name and it is not weakened here. What
    this test pins is the weaker reading being available at all, so the
    awareness layer can put the fact in front of the model without the model
    having to guess from the transcript.
    """
    reading = addressing.detect("نکسوس گفت که فلانی رو ساکت کنه")
    assert reading.found is True
    assert reading.name == "نکسوس"


def test_the_weak_grade_is_reachable_without_the_strong_one():
    """A skeleton match with no call in the sentence is context, not a trigger.

    «ناکس» shares the assistant's consonants exactly, and the word alone shows
    nothing about whether anybody is being called. That is precisely the case
    the two grades exist for: the model is told, and the model decides.
    """
    assert addressing.MENTION < addressing.ADDRESSED
    reading = addressing.detect("ناکس")
    assert reading.strength < addressing.ADDRESSED


# ── The mechanics, so a regression is legible ─────────────────────────────
@pytest.mark.parametrize(
    "left,right",
    [
        ("نکسوس", "نکسی"),
        ("نکسوس", "نکس"),
        ("نکسوس", "نیکسوس"),
        ("نکسوس", "نکسووووس"),
        ("nexus", "nexsus"),
    ],
)
def test_spellings_of_one_name_share_a_skeleton(left, right):
    """The skeleton is what makes the variants one word instead of a word list.

    Within a script. The Latin and Persian spellings of the name are two
    configured names rather than one skeleton — a skeleton is consonants, and
    the two scripts share none — which is why the parameter list above keeps
    them apart and ``test_every_way_a_group_writes_the_name_is_a_call`` is where
    they are asserted to be recognised together.
    """
    assert addressing.skeleton(left) == addressing.skeleton(right)


@pytest.mark.parametrize("word", ["noxious", "ناکس", "anxious"])
def test_a_lookalike_does_not_share_the_distance(word):
    """A skeleton is an outline, and outlines are shared. Distance separates them.

    This is the guard that keeps the skeleton test from being a licence to
    match anything with the right consonants.
    """
    name = "نکسوس" if any("\u0600" <= ch <= "\u06ff" for ch in word) else "nexus"
    assert addressing._distance(addressing._letters(word), name, 2) > 2


def test_an_attached_clitic_is_the_name():
    """Persian attaches the object marker: «نکسوسو» is «نکسوس» plus «و»."""
    assert addressing.detect("نکسوسو").reason == "clitic"


def test_the_reading_says_why():
    """A log line has to be able to explain a reply, not just assert one."""
    reading = addressing.detect("نکسوس جان")
    assert reading.reason == "exact"
    assert reading.form
    assert reading.name == "نکسوس"


def test_no_configured_names_means_no_recognition(monkeypatch):
    """A deployment that configured nothing must not match anything."""
    monkeypatch.setattr(config, "NEXUS_NAMES", [])
    assert addressing.detect("نکسوس nexus").found is False


def test_a_short_configured_name_is_not_matched_by_half_the_language(monkeypatch):
    """The typo test is bounded by length for a reason.

    A two-letter name one edit away from itself is one edit away from a great
    deal of ordinary vocabulary, and the consequence of a false positive is the
    assistant answering a message that was not for it.
    """
    monkeypatch.setattr(config, "NEXUS_NAMES", ["نک"])
    assert addressing.detect("یک").addressed is False


def test_nexus_delegates_to_the_matcher():
    """One implementation. The trigger policy reads it, it does not repeat it."""
    from app import nexus

    assert nexus.is_named("نکسی") is True
    assert nexus.is_named("سلام") is False
    # The weak grade is not a second implementation either: the trigger keeps
    # only the strong reading, and the room renderer asks the matcher itself.
    assert addressing.mentioned("نکسوس گفت که") is True
    assert addressing.addressed("نکسی") is True


# ── Quotation ─────────────────────────────────────────────────────────────
# «نکسوس گفت که...» repeats what the assistant said. It is the clearest case
# there is of a message that is *about* Nexus rather than *to* it, and the
# strong grade must not fire on it — the assistant answering a quotation is
# exactly the false positive the two grades exist to prevent.
@pytest.mark.parametrize(
    "text",
    [
        "نکسوس گفت که فلانی رو ساکت کنه",
        "نکسوس گفت اینو بن کن",
        "نکسوس میگه بیا",
        "نکسوس گفته بود",
        "نکسی گفت که",
        "nexus said that",
    ],
)
def test_a_quotation_is_a_mention_not_a_call(text):
    reading = addressing.detect(text)
    assert reading.found is True
    assert reading.addressed is False, text


@pytest.mark.parametrize(
    "text",
    [
        # The rule is one token wide, and these are what that protects.
        "نکسوس",
        "نکسوس جان",
        "هی نکسوس",
        "نکسوس بگو سلام",
        "نکسوس اینو بررسی کن",
        "میشه نکسوس اینو بررسی کنی؟",
        "سلام نکسوس",
        "نکسوسو ساکتش کن",
    ],
)
def test_a_call_with_the_name_in_any_position_is_still_a_call(text):
    assert addressing.addressed(text) is True, text
