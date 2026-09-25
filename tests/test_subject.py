"""What the room is talking about — and whether it is talking about Nexus.

The subject reader is the deterministic half of Nexus's self-awareness: it reads
the recent window and answers "who or what is this conversation about?" with a
classification, a confidence and a chain of evidence. The model still decides
whether to speak; this is the reading it is given.

The tests are written to pin the property the brief cares about most: a generic
word — «ربات», «هوش مصنوعی», "the bot" — is a *candidate referent*, not a
trigger. It can never make Nexus the subject on its own. Only a reference can:
a call, a reply edge, the name, a deictic pointing at the assistant present in
the room, or a continuation of a subject already established.
"""
from __future__ import annotations

import pytest

from app import config, subject

NEXUS = 99
ZAHRA = 111
ALI = 222
OTHER_BOT = 333


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "BOT_ALIASES", [])


def row(message_id, user_id, text, *, role="member", directed=False,
        reply_user_id=0, reply_name=""):
    return {
        "message_id": message_id,
        "user_id": user_id,
        "text": text,
        "role": role,
        "directed": directed,
        "reply_user_id": reply_user_id,
        "reply_name": reply_name,
    }


def read(rows, **kwargs):
    return subject.read_subject(
        -1001234567890, rows, bot_id=NEXUS, bot_username="guardbot", **kwargs
    )


# ══════════════════════════════════════════════════════════════════════════
# Part 1 — the strong signals
# ══════════════════════════════════════════════════════════════════════════
def test_a_direct_mention_of_nexus_is_the_subject():
    s = read([row(1, ZAHRA, "نکسوس اینو دیدی؟", directed=True)])
    assert s.kind == subject.DIRECT
    assert s.is_nexus is True
    assert s.confidence >= 90


def test_an_explicit_reply_to_nexus_is_the_subject():
    s = read([
        row(1, NEXUS, "اینو بزن", role="nexus"),
        row(2, ZAHRA, "ممنون از جوابت", reply_user_id=NEXUS, reply_name="Nexus"),
    ])
    assert s.kind == subject.IMPLICIT
    assert s.is_nexus is True
    assert s.confidence >= 80


def test_the_name_coming_up_without_a_call_is_about_nexus():
    s = read([row(1, ZAHRA, "نکسوس گفت که فردا میاد")])
    assert s.kind == subject.ABOUT
    assert s.is_nexus is True


# ══════════════════════════════════════════════════════════════════════════
# Part 2 — discussing Nexus without the name
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "text",
    [
        "این ربات چقدر خوبه",
        "این ربات خیلی سره",
        "این ربات چرا اینطوری جواب میده؟",
        "این هوش مصنوعی خیلی عجیبه",
        "این ربات فلان کار رو انجام میده؟",
    ],
)
def test_a_deictic_description_of_the_assistant_is_about_nexus(text):
    s = read([row(1, ZAHRA, text)])
    assert s.kind == subject.ABOUT
    assert s.is_nexus is True
    assert s.confidence >= 60


def test_the_assistant_name_in_the_text_is_about_nexus():
    s = read([row(1, ZAHRA, "نکسوس امروز خیلی خوب جواب داد")])
    assert s.kind == subject.ABOUT
    assert s.is_nexus is True


# ══════════════════════════════════════════════════════════════════════════
# Part 3 — continuity across turns
# ══════════════════════════════════════════════════════════════════════════
def test_a_subject_survives_turns_that_omit_the_name():
    s = read([
        row(1, ZAHRA, "نکسوس اینو دیدی؟", directed=True),
        row(2, ALI, "آره، خیلی عجیبه"),
        row(3, ZAHRA, "چرا اینجوری جواب داد؟"),
    ])
    assert s.kind in subject.NEXUS_KINDS
    assert s.is_nexus is True
    assert s.message_id == 3


def test_a_pronoun_continues_an_established_subject():
    s = read([
        row(1, ZAHRA, "این ربات چقدر خوبه"),
        row(2, ALI, "به نظرت خودش میفهمه داریم درباره‌ش حرف می‌زنیم؟"),
    ])
    assert s.kind in subject.NEXUS_KINDS
    assert s.is_nexus is True


def test_a_pronoun_with_no_established_subject_is_not_resolved_by_picking():
    s = read([row(1, ZAHRA, "به نظرت خودش میفهمه داریم درباره‌ش حرف می‌زنیم؟")])
    assert s.is_nexus is False


def test_this_one_continues_an_established_subject():
    s = read([
        row(1, ZAHRA, "این ربات چقدر خوبه"),
        row(2, ALI, "این یکی خیلی بهتر جواب میده"),
    ])
    assert s.kind in subject.NEXUS_KINDS


def test_this_one_with_no_established_subject_is_not_nexus():
    s = read([row(1, ZAHRA, "این یکی خیلی بهتر جواب میده")])
    assert s.is_nexus is False


def test_the_assistant_speaking_does_not_make_the_room_about_it():
    """An answer about VPNs leaves the room talking about VPNs."""
    s = read([
        row(1, ZAHRA, "کانفیگ میخوام"),
        row(2, NEXUS, "اینو بزن", role="nexus"),
        row(3, ALI, "ممنون، کانفیگ بعدی کیه؟"),
    ])
    assert s.is_nexus is False


# ══════════════════════════════════════════════════════════════════════════
# Part 4 — the false positives this must not make
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "text",
    [
        "ربات‌های تلگرام چطور کار می‌کنند؟",
        "هوش مصنوعی‌ها چقدر ترسناکن",
        "ربات چطور کار می‌کنه؟",
        "ربات‌ها همه جا هستن",
    ],
)
def test_a_general_discussion_is_not_about_nexus(text):
    s = read([row(1, ZAHRA, text)])
    assert s.is_nexus is False


def test_a_general_discussion_resets_an_earlier_nexus_subject():
    s = read([
        row(1, ZAHRA, "این ربات چقدر خوبه"),
        row(2, ALI, "ربات‌های تلگرام چطور کار می‌کنند؟"),
    ])
    assert s.is_nexus is False


def test_a_reply_to_another_person_makes_them_the_subject():
    s = read([
        row(1, ZAHRA, "سلام"),
        row(2, ALI, "خوبی؟", reply_user_id=ZAHRA, reply_name="زهرا"),
    ])
    assert s.kind == subject.OTHER
    assert s.is_nexus is False
    assert s.subject_user_id == ZAHRA
    assert s.subject_name == "زهرا"


def test_a_room_talking_about_another_bot_is_not_about_nexus():
    s = read([
        row(1, ZAHRA, "ربات‌های تلگرام چطور کار می‌کنند؟"),
        row(2, ALI, "خیلی‌هاشون همون هوش مصنوعی آنلاینن"),
    ])
    assert s.is_nexus is False


# ══════════════════════════════════════════════════════════════════════════
# Part 5 — continuity across passes
# ══════════════════════════════════════════════════════════════════════════
def test_a_previous_subject_is_carried_in_when_the_window_has_moved_on():
    previous = {
        "subject_kind": subject.DIRECT,
        "subject_confidence": 95,
        "subject_message_id": 10,
    }
    s = read([row(20, ALI, "بله دقیقاً")], previous=previous)
    assert s.kind in subject.NEXUS_KINDS
    assert s.is_nexus is True


def test_a_stale_previous_subject_below_the_floor_is_dropped():
    previous = {"subject_kind": subject.ABOUT, "subject_confidence": 50}
    s = read([], previous=previous)
    assert s.kind == subject.NONE
    assert s.is_nexus is False


def test_a_previous_subject_never_raises_on_a_bare_object():
    s = read([], previous=object())
    assert s.kind == subject.NONE


# ══════════════════════════════════════════════════════════════════════════
# Part 6 — rendering and the destination
# ══════════════════════════════════════════════════════════════════════════
def test_the_block_states_the_reading_and_its_confidence():
    s = read([row(1, ZAHRA, "این ربات چقدر خوبه")])
    out = subject.render(s)
    assert "confidence" in out
    assert str(s.confidence) in out


def test_the_block_is_empty_when_there_is_no_subject():
    assert subject.render(read([row(1, ZAHRA, "سلام خوبی")])) == ""


def test_the_block_is_bounded():
    s = read([row(1, ZAHRA, "این ربات چقدر خوبه")])
    assert len(subject.render(s, cap=120)) <= 121


def test_the_destination_is_the_turn_the_reading_is_about():
    rows = [
        row(1, ZAHRA, "این ربات چقدر خوبه"),
        row(2, ALI, "آره واقعاً"),
    ]
    s = read(rows)
    assert subject.destination(s, rows, bot_id=NEXUS) == 2


def test_the_destination_is_never_a_nexus_message():
    """A reading that somehow pointed at Nexus's own row resolves to nothing."""
    s = subject.Subject(kind=subject.ABOUT, confidence=70, message_id=2)
    rows = [
        row(1, ZAHRA, "این ربات چقدر خوبه"),
        row(2, NEXUS, "ممنون", role="nexus"),
    ]
    assert subject.destination(s, rows, bot_id=NEXUS) == 0


def test_the_destination_must_be_a_real_stored_message():
    s = read([row(1, ZAHRA, "این ربات چقدر خوبه")])
    assert subject.destination(s, [], bot_id=NEXUS) == 0
