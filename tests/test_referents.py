"""Resolving «این» and «همون کاربر» to a person — as candidates, never a choice.

The requirement is that Nexus understand who a pronoun points at, and that it
refuse to guess when the room genuinely does not say. These tests hold the
resolver to both halves: it must find the referent the conversation determines,
and it must report ambiguity rather than pick a winner when two people are
equally plausible. They also hold it to the property that matters most for the
architecture — it is evidence, so it can never name a person the window does not
contain, and it never decides anything.
"""
import pytest

from app import referents as R


def row(user_id, name, text="", *, at=1000, role="member", reply=0, reply_name=""):
    """One ``db.group_window`` row, with only the fields the resolver reads."""
    return {
        "id": at,
        "user_id": user_id,
        "role": role,
        "name": name,
        "text": text,
        "at": at,
        "message_id": 0,
        "reply_user_id": reply,
        "reply_name": reply_name,
        "reply_message_id": 0,
        "directed": False,
        "actor": False,
        "kind": "",
    }


def anchor(text, *, user_id=33, name="مالک", at=1000, role="owner", reply=0, reply_name=""):
    return {
        "user_id": user_id,
        "name": name,
        "text": text,
        "at": at,
        "role": role,
        "reply_user_id": reply,
        "reply_name": reply_name,
    }


# ── Finding the expression ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,kind",
    [
        ("اینو بن کن", R.KIND_DEICTIC),
        ("این رو بن کن", R.KIND_DEICTIC),
        ("همونو ساکت کن", R.KIND_DEICTIC),
        ("این کاربر رو بن کن", R.KIND_PERSON),
        ("همون طرف", R.KIND_PERSON),
        ("اون یوزر رو بیرون کن", R.KIND_PERSON),
        ("ادمینه رو محدود کن", R.KIND_ROLE),
        ("مدیره رو بن کن", R.KIND_ROLE),
        ("قبلیش رو بن کن", R.KIND_PRIOR),
    ],
)
def test_the_expression_is_found_and_typed(text, kind):
    expression = R.find_expression(text)
    assert expression.kind == kind
    assert expression.surface


@pytest.mark.parametrize(
    "text",
    [
        "سلام چطوری",
        "قیمت چنده",
        "رضا رو بن کن",  # a name needs no resolution; the transcript carries it
        "یه چیز دیگه",
        "",
        "   ",
    ],
)
def test_ordinary_text_points_at_nobody(text):
    assert not R.find_expression(text)


def test_the_surface_keeps_the_words_the_message_used():
    """«اینو» is reported as «اینو», not folded to «این» — the log reads right."""
    assert R.find_expression("اینو بن کن").surface == "اینو"
    assert R.find_expression("این رو بن کن").surface == "این رو"


def test_a_word_ending_in_vav_is_not_mistaken_for_a_demonstrative():
    """The bug the explicit surface list exists to prevent: no generic «و» strip."""
    assert not R.find_expression("آموزش رو ببین")
    assert not R.find_expression("تو خوبی")


@pytest.mark.parametrize(
    "text,anaphoric",
    [
        ("همون کاربر رو بن کن", True),
        ("اونو ساکتش کن", True),  # the clitic «ـش» is reported, and it is anaphoric
        ("همونو بن کن", True),
        ("ساکتش کن", True),
        ("اینو بن کن", False),
        ("این کاربر رو بن کن", False),
        ("قبلیش رو بن کن", False),  # backwards at a position, not at the subject
    ],
)
def test_anaphora_is_the_far_demonstratives_and_the_object_clitic(text, anaphoric):
    """«همون»/«اون»/«ـش» point at an established person; «این» points at the nearest."""
    assert R.find_expression(text).anaphoric() is anaphoric


# ── The verdict ───────────────────────────────────────────────────────────
def test_no_expression_resolves_to_nothing():
    result = R.resolve(anchor("سلام"), messages=[row(11, "رضا")])
    assert not result
    assert result.candidates == ()


def test_an_expression_with_no_room_resolves_to_nothing():
    result = R.resolve(anchor("اینو بن کن"), messages=[])
    assert result.expression.surface == "اینو"
    assert result.candidates == ()


def test_a_reply_target_is_the_referent_outright():
    """When the message *is* a reply, that id is the answer, not a candidate."""
    messages = [row(11, "رضا", at=900), row(22, "سارا", at=950)]
    result = R.resolve(anchor("اینو بن کن", reply=22, reply_name="سارا"), messages=messages)
    assert result.confident is True
    assert result.ambiguous is False
    assert result.top().user_id == 22
    assert result.top().score == 1.0


def test_a_reply_is_never_ambiguous_even_when_somebody_else_is_named():
    messages = [row(11, "رضا", at=900), row(22, "سارا", at=950)]
    result = R.resolve(
        anchor("رضا اینو بن کن", reply=22, reply_name="سارا"), messages=messages
    )
    assert result.confident is True
    assert result.ambiguous is False
    assert result.top().user_id == 22


def test_a_named_person_wins_over_recency():
    messages = [row(11, "رضا", at=990), row(22, "سارا", at=995)]
    result = R.resolve(anchor("رضا رو ببین اینو"), messages=messages)
    assert result.top().user_id == 11
    assert "named in the message" in " ".join(result.top().why)
    assert result.confident is True


def test_a_stated_id_is_stronger_than_a_name():
    messages = [row(11, "رضا", at=990), row(22, "سارا", at=995)]
    result = R.resolve(anchor("سارا اینو ببین 11"), messages=messages)
    assert result.top().user_id == 11
    assert result.top().score >= R.SCORE_STATED_ID


def test_a_role_expression_resolves_to_whoever_holds_it_now():
    messages = [
        row(11, "رضا", at=990),
        row(44, "نیما", at=995, role="admin"),
        row(55, "سارا", at=997, role="member"),
    ]
    result = R.resolve(anchor("ادمینه رو محدود کن"), messages=messages)
    assert result.top().user_id == 44
    assert result.confident is True


def test_a_member_is_not_a_role_candidate():
    """Only owner/admin hold a role worth naming; a member cannot be «ادمینه»."""
    messages = [row(11, "رضا", at=990, role="member"), row(44, "نیما", at=995, role="admin")]
    result = R.resolve(anchor("ادمینه رو محدود کن"), messages=messages)
    assert [c.user_id for c in result.candidates] == [44, 11]


def test_equal_recency_is_reported_as_ambiguous_not_guessed():
    """The whole point: three plausible people is not a referent."""
    messages = [row(11, "رضا", at=990), row(22, "سارا", at=995), row(44, "نیما", at=998)]
    result = R.resolve(anchor("اینو بن کن"), messages=messages)
    assert result.ambiguous is True
    assert result.confident is False
    assert len(result.candidates) == 3


def test_a_single_recent_speaker_is_not_ambiguous_but_is_not_confident_either():
    """One candidate, weak evidence: nothing to confuse it with, nothing to trust."""
    messages = [row(11, "رضا", at=1000 - 2000), row(22, "سارا", at=999)]
    result = R.resolve(anchor("اینو بن کن"), messages=messages)
    assert result.top().user_id == 22
    assert result.ambiguous is False
    assert result.confident is False
    assert result.top().score < R.CONFIDENT_MIN


def test_a_single_candidate_with_strong_evidence_is_confident():
    messages = [row(11, "رضا", at=999), row(22, "سارا", at=1000 - 2000)]
    result = R.resolve(anchor("رضا رو ببین اینو"), messages=messages)
    assert result.top().user_id == 11
    assert result.confident is True
    assert result.ambiguous is False


def test_the_room_being_about_one_person_is_evidence():
    """Three replies aimed at رضا is what «همون کاربر» means."""
    messages = [
        row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"),
        row(33, "مالک", "ب", at=820, reply=11, reply_name="رضا"),
        row(22, "سارا", "ج", at=840, reply=11, reply_name="رضا"),
        row(11, "رضا", "د", at=850),
    ]
    result = R.resolve(anchor("همون کاربر رو بن کن"), messages=messages)
    assert result.top().user_id == 11
    assert any("aimed at them" in why for why in result.top().why)


def test_an_anaphoric_demonstrative_settles_a_unanimous_room():
    """«همون» means "that same one": a room replying to one person is the answer.

    The about-signal is only a hint for a bare «این», but for «همون»/«اون» it is
    what the word points at — so the resolver is confident rather than unsure.
    """
    messages = [
        row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"),
        row(33, "مالک", "ب", at=820, reply=11, reply_name="رضا"),
        row(22, "سارا", "ج", at=840, reply=11, reply_name="رضا"),
        row(11, "رضا", "د", at=850),
    ]
    for text in ("همون کاربر رو بن کن", "همونو بن کن", "اونو ساکتش کن"):
        result = R.resolve(anchor(text), messages=messages)
        assert result.top().user_id == 11, text
        assert result.confident is True, text
        assert result.ambiguous is False, text


def test_a_near_demonstrative_does_not_get_the_anaphoric_reading():
    """«این» points at whatever is nearest, which the room does not settle."""
    messages = [
        row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"),
        row(33, "مالک", "ب", at=820, reply=11, reply_name="رضا"),
        row(22, "سارا", "ج", at=840, reply=11, reply_name="رضا"),
        row(11, "رضا", "د", at=850),
    ]
    result = R.resolve(anchor("اینو بن کن"), messages=messages)
    assert result.top().user_id == 11
    assert result.confident is False


def test_a_prior_expression_is_not_treated_as_anaphoric():
    """«قبلی» points at a position in a sequence, not at the room's subject."""
    messages = [
        row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"),
        row(33, "مالک", "ب", at=820, reply=11, reply_name="رضا"),
        row(22, "سارا", "ج", at=840, reply=11, reply_name="رضا"),
        row(11, "رضا", "د", at=850),
    ]
    result = R.resolve(anchor("قبلیش رو بن کن"), messages=messages)
    assert result.confident is False


def test_a_split_room_is_not_an_anaphoric_focus():
    """Replies aimed at two people is exactly when «همون» must stay unsure."""
    messages = [
        row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"),
        row(33, "مالک", "ب", at=820, reply=11, reply_name="رضا"),
        row(22, "سارا", "ج", at=840, reply=22, reply_name="سارا"),
        row(22, "سارا", "د", at=860, reply=22, reply_name="سارا"),
        row(11, "رضا", "ه", at=870),
        row(22, "سارا", "و", at=880),
    ]
    result = R.resolve(anchor("همون کاربر رو بن کن"), messages=messages)
    assert result.confident is False
    assert result.ambiguous is True


def test_a_single_reply_edge_is_not_a_focus():
    """One reply is not "what the room has been about" — the bar is repeated."""
    messages = [row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"), row(11, "رضا", "د", at=850)]
    result = R.resolve(anchor("همون کاربر رو بن کن"), messages=messages)
    assert result.top().user_id == 11
    assert result.confident is False
    assert not any("all been aimed" in why for why in result.top().why)


def test_nexus_is_never_a_candidate():
    messages = [row(11, "رضا", at=990), row(0, "نکسوس", at=995, role="nexus")]
    result = R.resolve(anchor("اینو بن کن"), messages=messages)
    assert 0 not in [c.user_id for c in result.candidates]


def test_the_candidate_list_is_bounded():
    messages = [row(uid, f"کاربر{uid}", at=990) for uid in range(1, 40)]
    result = R.resolve(anchor("اینو بن کن"), messages=messages)
    assert len(result.candidates) <= R.CANDIDATE_LIMIT


def test_two_independent_signals_score_above_one():
    """A person who spoke recently *and* is who the room replies to ranks higher."""
    messages = [
        row(33, "مالک", "الف", at=800, reply=11, reply_name="رضا"),
        row(33, "مالک", "ب", at=820, reply=11, reply_name="رضا"),
        row(11, "رضا", at=999),
        row(22, "سارا", at=990),
    ]
    result = R.resolve(anchor("اینو بن کن"), messages=messages)
    top = result.top()
    assert top.user_id == 11
    assert len(top.why) >= 2
    assert top.score > R.SCORE_RECENT_MAX


def test_candidates_are_ranked_by_score():
    messages = [row(11, "رضا", at=990), row(44, "نیما", at=995, role="admin")]
    result = R.resolve(anchor("ادمینه رو محدود کن"), messages=messages)
    scores = [c.score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)


# ── Rendering ─────────────────────────────────────────────────────────────
def test_nothing_is_rendered_without_an_expression():
    result = R.resolve(anchor("سلام"), messages=[row(11, "رضا")])
    assert R.render(result) == ""


def test_the_block_names_the_expression_and_the_candidates():
    messages = [row(11, "رضا", at=990), row(44, "نیما", at=995, role="admin")]
    text = R.render(R.resolve(anchor("ادمینه رو محدود کن"), messages=messages))
    assert "ادمینه" in text
    assert "44" in text
    assert "نیما" in text
    assert "evidence, not a decision" in text


def test_an_ambiguous_block_tells_the_model_to_ask():
    messages = [row(11, "رضا", at=990), row(22, "سارا", at=995), row(44, "نیما", at=998)]
    text = R.render(R.resolve(anchor("اینو بن کن"), messages=messages))
    assert "could not tell" in text
    assert "ask" in text


def test_a_confident_block_tells_the_model_to_use_the_id():
    messages = [row(11, "رضا", at=990)]
    text = R.render(R.resolve(anchor("رضا رو ببین اینو"), messages=messages))
    assert "confident" in text


def test_a_found_expression_with_no_candidate_is_stated_plainly():
    result = R.resolve(anchor("اینو بن کن"), messages=[])
    text = R.render(result)
    assert "no person it could be" in text
    assert "do not guess" in text


def test_the_block_is_bounded():
    messages = [row(uid, f"کاربر{uid}", at=990) for uid in range(1, 40)]
    text = R.render(R.resolve(anchor("اینو بن کن"), messages=messages), cap=120)
    assert len(text) <= 120


# ── The object clitic ─────────────────────────────────────────────────────
# «ساکتش کن» points at a person with no demonstrative at all: the object is the
# clitic «ـش» on the verb. It is the most common way a Persian instruction names
# its target, and missing it means missing the instruction.
@pytest.mark.parametrize(
    "text",
    ["ساکتش کن", "بنش کن", "حذفش کن", "بیرونش کن", "اخراجش کن", "محدودش کن"],
)
def test_an_action_verb_with_the_object_clitic_is_a_person_reference(text):
    assert R.find_expression(text).kind == R.KIND_CLITIC


def test_a_bare_action_stem_is_not_a_person_reference():
    """«بن» on its own is a topic word; only the clitic makes the object real."""
    assert R.find_expression("بن").kind == ""
    assert R.find_expression("ساکت").kind == ""


@pytest.mark.parametrize("text", ["بنفش", "خواهش", "آرش", "چشم"])
def test_ordinary_words_ending_in_sheen_are_not_clitics(text):
    assert R.find_expression(text).kind == ""


def test_a_clitic_instruction_resolves_through_the_reply_edge():
    messages = [row(11, "رضا", at=960), row(22, "سارا", at=980)]
    result = R.resolve(anchor("ساکتش کن", reply=22, reply_name="سارا"), messages=messages)
    assert result.top().user_id == 22
    assert result.confident is True


def test_a_person_noun_beats_a_clitic():
    """«این کاربر» names what the object is; the clitic only implies one."""
    assert R.find_expression("این کاربر رو ساکتش کن").kind == R.KIND_PERSON


# ── A time word is not a person ───────────────────────────────────────────
def test_a_demonstrative_before_a_time_word_is_not_a_person_reference():
    """«این هفته» is a week, not somebody. The near demonstratives are the
    weakest person pointers already; before a time noun they point at nothing
    at all, and the resolver must not offer the room's people for them.
    """
    for text in ("همین الان", "این هفته", "اون موقع", "همون روز", "این ماه",
                 "اون سال", "این شب", "همین وقت"):
        assert R.find_expression(text).kind == "", text


def test_the_time_guard_reads_the_raw_token_not_the_clitic_stripped_one():
    """«هفته» would strip to «هفت», so the guard must look before stripping.

    This is the whole reason the check reads ``tokens`` and not ``bare``: the
    generic clitic stripper takes the «ه» off «هفته» and the time noun would no
    longer match the list.
    """
    assert R._bare("هفته") == "هفت"
    assert R.find_expression("این هفته").kind == ""
    assert R.find_expression("همون هفته").kind == ""


def test_the_time_guard_does_not_swallow_a_real_person_reference():
    assert R.find_expression("این کاربر").kind == R.KIND_PERSON
    assert R.find_expression("دیروز این کاربر اذیتم کرد").kind == R.KIND_PERSON
    assert R.find_expression("ساکتش کن").kind == R.KIND_CLITIC


def test_the_time_lexicon_is_borrowed_lazily_and_guarded():
    """The second cross-module reach, held to the same rule as the first.

    It must be inside a function — so importing this module never pulls in
    ``temporal`` — and it must be wrapped, so a host without the list falls back
    to the reading this module gave before the temporal reader existed rather
    than failing to import.
    """
    import inspect

    tree = __import__("ast").parse(inspect.getsource(R))
    lazy = _imports(tree, top_level_only=False) - _imports(tree, top_level_only=True)
    assert "temporal" in lazy
    assert "temporal" not in _module_level_imports(R)
    source = inspect.getsource(R._temporal_nouns)
    assert "try:" in source and "except Exception" in source
    # With no list at all, the old behaviour returns: the bare demonstrative.
    original = R._temporal_nouns
    try:
        R._temporal_nouns = lambda: frozenset()
        assert R.find_expression("این هفته").kind == R.KIND_DEICTIC
    finally:
        R._temporal_nouns = original


# ── A thing word is not a person ──────────────────────────────────────────
def test_a_demonstrative_before_a_thing_word_is_not_a_person_reference():
    """«این لینک» is a link, «اون عکس» a photo, «همین پیام» a message.

    Offering the room's members as the people «این» may mean is a wrong lead,
    and a wrong-person moderation action is the worst mistake available here.
    The things themselves are the entity reader's block.
    """
    for text in ("این لینک چیه", "اون عکس رو پاک کن", "همین پیام رو پاک کن",
                 "این فایل رو بفرست", "اون ویس رو گوش کن", "این کامنت رو حذف کن",
                 "این ویدیو رو ببین", "همون پست رو پاک کن", "این استیکر چیه"):
        assert R.find_expression(text).kind == "", text


def test_the_thing_guard_reads_the_raw_token_not_the_clitic_stripped_one():
    """«پیام» would strip to «پی», so the guard must look before stripping.

    The same trap the time guard documents, and it caught this one too: with
    ``bare`` the noun never reached ``entities.thing_kind``.
    """
    assert R._bare("پیام") == "پی"
    assert R.find_expression("این پیام رو پاک کن").kind == ""


def test_a_clitic_thing_word_is_still_a_thing():
    """«لینکشو» is «لینک» + the object marker, and it is still a link.

    This module's stripper does not remove a bare «و» (it is also an ordinary
    letter), so the strip that makes this work is the entity reader's own.
    """
    assert R._bare("لینکشو") == "لینکشو"
    assert R.find_expression("این لینکشو ببین").kind == ""
    assert R.find_expression("اون عکسشو پاک کن").kind == ""


def test_the_thing_guard_does_not_swallow_a_real_person_reference():
    """Only the word *immediately after* the demonstrative is checked."""
    assert R.find_expression("این کاربر").kind == R.KIND_PERSON
    assert R.find_expression("اینو پاک کن").kind == R.KIND_DEICTIC
    assert R.find_expression("لینک این کاربر رو بده").kind == R.KIND_PERSON
    assert R.find_expression("ساکتش کن").kind == R.KIND_CLITIC


def test_an_ordinary_word_that_starts_like_a_thing_is_not_a_thing():
    """The lookup is on the whole token, never a prefix.

    «عکاس» is a photographer, «پیامدش» is a consequence, «فایده» is a use — and
    each of them begins with a word that names a thing. The borrow only accepts
    a hit on the *whole* token, so none of them is swallowed.
    """
    for text in ("این عکاس کیه", "این پیامدش چیه", "این فایده داره",
                 "این متنفرم", "این صدامو شنیدی"):
        assert R.find_expression(text).kind == R.KIND_DEICTIC, text


def test_the_thing_lexicon_is_borrowed_lazily_and_guarded():
    """The third cross-module reach, held to the same rule as the first two.

    It must be inside a function — so importing this module never pulls in
    ``entities`` — and it must be wrapped, so a host without the list falls back
    to the reading this module gave before the entity reader existed rather than
    failing to import.
    """
    import inspect

    tree = __import__("ast").parse(inspect.getsource(R))
    lazy = _imports(tree, top_level_only=False) - _imports(tree, top_level_only=True)
    assert "entities" in lazy
    assert "entities" not in _module_level_imports(R)
    source = inspect.getsource(R._thing_named)
    assert "try:" in source and "except Exception" in source
    # With no list at all, the old behaviour returns: the bare demonstrative.
    original = R._thing_named
    try:
        R._thing_named = lambda token: False
        assert R.find_expression("این لینک چیه").kind == R.KIND_DEICTIC
    finally:
        R._thing_named = original


# ── The Arabic block's punctuation is not part of the word ────────────────
# «؟» «،» «؛» live inside \u0600-\u06ff, so they stayed glued to the word before
# them. Two failures followed from that: a name at the end of a question was
# never matched, and a thing noun at the end of a question was never recognized,
# so the resolver offered the room's members for a message about a link.
def test_a_name_at_the_end_of_a_question_is_still_a_name():
    people = {11: {"name": "سارا", "at": 900}}
    assert R._name_hits("بن کن سارا؟", people) == {11: "سارا"}
    assert R._name_hits("بن کن سارا،", people) == {11: "سارا"}
    assert R._name_hits("بن کن سارا", people) == {11: "سارا"}


def test_a_stated_id_at_the_end_of_a_question_is_still_an_id():
    people = {22: {"name": "رضا", "at": 900}}
    assert R._stated_id("بنش کن 22؟", people) == 22


@pytest.mark.parametrize(
    "text",
    ["این لینک؟", "این لینکو ببین،", "این پیام؟", "این فایل!", "این عکس؛"],
)
def test_a_thing_word_before_a_mark_is_still_not_a_person(text):
    assert R.find_expression(text).kind == ""


def test_a_bare_demonstrative_with_a_mark_is_still_a_person_pointer():
    """The mark is stripped from the token, not from the message's meaning."""
    assert R.find_expression("اینو بن کن؟").kind == R.KIND_DEICTIC
    assert R.find_expression("اونو پاک کن!").kind == R.KIND_DEICTIC


# ── Purity ────────────────────────────────────────────────────────────────
def _imports(tree, *, top_level_only: bool) -> set[str]:
    """The module names a parsed file imports.

    ``from . import x`` is an ``ImportFrom`` with ``module=None`` and the name in
    ``names``, so a helper that only read ``module`` would miss exactly the
    relative imports this module uses — and the purity test would pass on a
    module that had grown a top-level ``from . import db``.
    """
    import ast

    nodes = tree.body if top_level_only else ast.walk(tree)
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            else:
                # A bare relative import: ``from . import addressing``. The
                # module is the imported name, not ``node.module``.
                names.update(alias.name.split(".")[0] for alias in node.names)
    return names


def _module_level_imports(module) -> set[str]:
    """Imports at the top of the file, not the guarded ones inside functions."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    return _imports(tree, top_level_only=True)


def test_the_module_is_pure_at_import_time():
    """No db, no config, no pool, no rbac at import — it is evidence about text."""
    assert _module_level_imports(R) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_the_action_lexicon_is_borrowed_lazily_and_guarded():
    """The one cross-module reach is late and degrades to nothing.

    It must be inside a function — so importing this module never pulls in
    ``config`` — and it must be wrapped, so a host without the lexicon loses a
    clitic match rather than the module.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    lazy = _imports(tree, top_level_only=False) - _imports(tree, top_level_only=True)
    assert "addressing" in lazy
    assert "addressing" not in _module_level_imports(R)
    source = inspect.getsource(R._action_words)
    assert "try:" in source and "except Exception" in source
    # And with no lexicon at all, it finds no clitic rather than raising.
    original = R._action_words
    try:
        R._action_words = lambda: frozenset()
        assert R.find_expression("ساکتش کن").kind == ""
        assert R.find_expression("اینو بن کن").kind == R.KIND_DEICTIC
    finally:
        R._action_words = original
