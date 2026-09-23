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


# ── Purity ────────────────────────────────────────────────────────────────
def test_the_module_reaches_no_database_model_or_authority():
    """It is evidence about text: no db, no config, no pool, no rbac."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "re", "unicodedata", "dataclasses", "people"}, imported
