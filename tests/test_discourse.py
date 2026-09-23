"""What a message is doing, and what the room has left unanswered.

Two claims are held here, and they are the two the module makes: that the
server can read the *act* of a Persian message — asking, instructing,
correcting, greeting, reporting — without a model, and that it can name the
questions in a window that no reply points at an answer for. Both are
**evidence**: nothing here gates a reply, an action or a permission, and the
purity test at the bottom is what keeps that true as the module grows.

The precedence tests are the interesting ones. «نه گفتم مهدی نه سارا» carries a
first-person reporting verb and a correction; «نکسوس گفت اینو بن کن» carries a
reporting verb and an imperative. Reading either as the weaker act is the
mistake the precedence exists to prevent.
"""
import pytest

from app import discourse as D


# ── The act, by class ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,kind",
    [
        # question
        ("چرا اینطوری شد؟", D.ACT_QUESTION),
        ("قیمت چنده؟", D.ACT_QUESTION),
        ("چخبر؟", D.ACT_QUESTION),
        ("کی میاد؟", D.ACT_QUESTION),
        ("این چیه", D.ACT_QUESTION),
        ("آیا درسته", D.ACT_QUESTION),
        ("what happened?", D.ACT_QUESTION),
        # instruction
        ("اینو بن کن", D.ACT_INSTRUCTION),
        ("ساکتش کن", D.ACT_INSTRUCTION),
        ("ادمینه رو محدود کن", D.ACT_INSTRUCTION),
        ("بررسی کن ببین چی شده", D.ACT_INSTRUCTION),
        ("ادامه بده", D.ACT_INSTRUCTION),
        ("لطفا check کن", D.ACT_INSTRUCTION),
        ("اینو ببین 22", D.ACT_INSTRUCTION),
        ("بن", D.ACT_INSTRUCTION),
        ("درستش کن", D.ACT_INSTRUCTION),
        # correction
        ("نه منظورم مهدی بود، اینو بن کن", D.ACT_CORRECTION),
        ("نه گفتم مهدی نه سارا", D.ACT_CORRECTION),
        ("اشتباه شد", D.ACT_CORRECTION),
        ("نگفتم که", D.ACT_CORRECTION),
        # social
        ("سلام بچه ها چطوری", D.ACT_SOCIAL),
        ("سلام صبح بخیر", D.ACT_SOCIAL),
        ("ممنون", D.ACT_SOCIAL),
        ("خداحافظ", D.ACT_SOCIAL),
        # report
        ("نکسوس گفت که اینو بن کنه", D.ACT_REPORT),
        ("نکسوس گفت اینو بن کن", D.ACT_REPORT),
        ("رضا گفت که بعداً میاد", D.ACT_REPORT),
        ("قبلاً گفتم که اینکار رو نکنید", D.ACT_REPORT),
        # abstention
        ("من با نکسوس کار نکردم", D.ACT_UNKNOWN),
        ("امروز خیلی شلوغ بود", D.ACT_UNKNOWN),
        ("یکی اینجا خیلی داره شلوغ میکنه", D.ACT_UNKNOWN),
        ("باشه", D.ACT_UNKNOWN),
        ("", D.ACT_UNKNOWN),
        (None, D.ACT_UNKNOWN),
    ],
)
def test_the_act_is_read(text, kind):
    assert D.read_act(text).kind == kind


def test_the_vocabulary_is_closed_and_the_abstention_is_not_in_it():
    """``unknown`` is the absence of a reading, not one of the readings."""
    assert D.ACT_UNKNOWN not in D.ACTS
    assert set(D.ACTS) == {
        D.ACT_QUESTION,
        D.ACT_INSTRUCTION,
        D.ACT_CORRECTION,
        D.ACT_SOCIAL,
        D.ACT_REPORT,
    }


def test_a_correction_outranks_the_instruction_it_carries():
    """«نه منظورم مهدی بود، اینو بن کن» is fixing, and it happens to instruct."""
    act = D.read_act("نه منظورم مهدی بود، اینو بن کن")
    assert act.kind == D.ACT_CORRECTION


def test_a_first_person_recollection_opened_by_no_is_a_correction():
    assert D.read_act("نه گفتم مهدی نه سارا").kind == D.ACT_CORRECTION


def test_a_first_person_recollection_without_the_opener_is_a_report():
    """The reminder keeps its own reading: «قبلاً گفتم» is not a correction."""
    assert D.read_act("قبلاً گفتم که اینکار رو نکنید").kind == D.ACT_REPORT


def test_a_bare_no_is_not_a_correction():
    """Otherwise every disagreement in the room reads as fixing the record."""
    assert not D.read_act("نه")


def test_a_quotation_outranks_the_order_it_repeats():
    """Reading «نکسوس گفت اینو بن کن» as an instruction is the false positive."""
    assert D.read_act("نکسوس گفت اینو بن کن").kind == D.ACT_REPORT


def test_a_greeting_outranks_the_question_word_inside_it():
    assert D.read_act("سلام بچه ها چطوری").kind == D.ACT_SOCIAL


def test_a_question_outranks_the_ordinary_verb_that_used_to_match_it():
    """The suffix bug: «درسته» ends in «کن»-like letters and is not a directive."""
    assert D.read_act("آیا درسته").kind == D.ACT_QUESTION


def test_a_negated_verb_is_not_an_imperative():
    """«نمیکن» ends with «کن»; the suffix rule that read it as one was removed."""
    assert D.read_act("نمیکن").kind == D.ACT_UNKNOWN
    assert D.read_act("میکنم").kind == D.ACT_UNKNOWN


# ── A duration is not a question ──────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "چند دقیقه پیش فرستادم",
        "چند لحظه پیش دیدمش",
        "چند ساعت پیش اومد",
        "چند روز پیش فرستادم",
        "چند هفته پیش بود",
        "چند ماه پیش دیدم",
        "چند سال پیش رفت",
        "چند وقت پیش بود",
    ],
)
def test_a_question_word_before_a_time_noun_is_a_duration(text):
    """«چند» asks "how many"; «چند دقیقه پیش» says "a few minutes ago".

    The word is the same and the reading is opposite. The noun after it is what
    separates them, and it is read from the same list the temporal reader uses.
    """
    assert D.read_act(text).kind != D.ACT_QUESTION


@pytest.mark.parametrize(
    "text",
    [
        "چند تا میخوای",
        "چند نفر؟",
        "کدومش",
        "قیمت چنده؟",
        "چند دقیقه پیش فرستادی؟",   # the mark makes it a question again
    ],
)
def test_a_question_word_that_is_not_a_duration_still_asks(text):
    assert D.read_act(text).kind == D.ACT_QUESTION


def test_a_duration_beside_a_directive_is_an_instruction():
    """«چند دقیقه صبر کن» asks nothing — it is an order, with a duration in it."""
    assert D.read_act("چند دقیقه صبر کن").kind == D.ACT_INSTRUCTION


def test_the_reading_carries_the_word_that_decided_it():
    act = D.read_act("اینو بن کن")
    assert act.why and "بن" in act.why[0]


def test_a_reading_is_truthy_and_an_abstention_is_not():
    assert bool(D.read_act("اینو بن کن")) is True
    assert bool(D.read_act("امروز خیلی شلوغ بود")) is False


# ── Folding: the orthographies a real room writes in ──────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "این‌کاربر رو بن کن",   # ZWNJ
        "اين كاربر رو بن كن",   # Arabic yeh and kaf
        "این کاربر رو بن کن",
    ],
)
def test_the_fold_survives_the_orthographies(text):
    assert D.read_act(text).kind == D.ACT_INSTRUCTION


def test_a_mixed_persian_english_directive_is_read():
    assert D.read_act("این user رو check کن").kind == D.ACT_INSTRUCTION


# ── The room's open questions ─────────────────────────────────────────────
def row(user_id, name, text, *, at=1000, role="member", mid=0, reply_uid=0, reply_mid=0):
    """One ``db.group_window`` row, with only the fields the reader consults."""
    return {
        "id": at,
        "user_id": user_id,
        "role": role,
        "name": name,
        "text": text,
        "at": at,
        "message_id": mid,
        "reply_user_id": reply_uid,
        "reply_name": "",
        "reply_message_id": reply_mid,
        "directed": False,
        "actor": False,
        "kind": "",
    }


def test_a_question_nothing_replies_to_is_open():
    questions = D.open_questions([row(11, "رضا", "قیمت چنده؟", mid=5)])
    assert [q.text for q in questions] == ["قیمت چنده؟"]


def test_a_question_a_reply_points_at_is_not_open():
    window = [
        row(11, "رضا", "قیمت چنده؟", at=900, mid=5),
        row(22, "سارا", "نمیدونم", at=920, reply_uid=11, reply_mid=5),
    ]
    assert D.open_questions(window) == ()


def test_an_answer_without_a_reply_edge_still_reads_as_open():
    """The limit is stated rather than hidden: the edge is the whole test."""
    window = [
        row(11, "رضا", "قیمت چنده؟", at=900, mid=5),
        row(22, "سارا", "نمیدونم", at=920),
    ]
    assert [q.text for q in D.open_questions(window)] == ["قیمت چنده؟"]


def test_only_the_questions_are_reported():
    window = [
        row(11, "رضا", "اینو بن کن", at=900, mid=5),
        row(22, "سارا", "چرا؟", at=910, mid=6),
    ]
    assert [q.text for q in D.open_questions(window)] == ["چرا؟"]


def test_the_newest_questions_come_first():
    window = [
        row(11, "رضا", "قدیمی؟", at=900, mid=5),
        row(22, "سارا", "جدید؟", at=920, mid=6),
    ]
    assert [q.text for q in D.open_questions(window)] == ["جدید؟", "قدیمی؟"]


def test_the_list_is_bounded():
    window = [row(11, "رضا", f"سوال {i}؟", at=900 + i, mid=10 + i) for i in range(9)]
    assert len(D.open_questions(window)) == 3


def test_an_empty_window_has_no_questions():
    assert D.open_questions([]) == ()
    assert D.open_questions(None) == ()


def test_the_assistants_own_unanswered_question_is_included():
    """A question Nexus asked that nobody picked up is what a room forgets."""
    window = [row(0, "نکسوس", "کدومشون رو منظورت بود؟", at=900, role="nexus", mid=5)]
    questions = D.open_questions(window)
    assert [q.role for q in questions] == ["nexus"]


# ── Rendering ─────────────────────────────────────────────────────────────
def test_nothing_is_rendered_for_an_abstention():
    assert D.render_act(D.read_act("امروز خیلی شلوغ بود")) == ""


def test_the_act_line_names_the_act_and_the_word():
    text = D.render_act(D.read_act("اینو بن کن"))
    assert "instruction" in text
    assert "بن" in text


def test_the_question_block_is_labelled_as_evidence_not_a_judgement():
    block = D.render_questions(D.open_questions([row(11, "رضا", "چرا؟", mid=5)]))
    assert "no reply pointing at an answer" in block
    assert "not a judgement" in block
    assert "رضا" in block


def test_the_question_block_names_the_assistant_as_you():
    block = D.render_questions(
        D.open_questions([row(0, "نکسوس", "چرا؟", role="nexus", mid=5)])
    )
    assert "you:" in block


def test_the_question_block_is_bounded():
    long = "س" * 400 + "؟"
    block = D.render_questions(D.open_questions([row(11, "رضا", long, mid=5)]), cap=200)
    assert len(block) <= 220


def test_nothing_is_rendered_without_questions():
    assert D.render_questions(()) == ""


# ── Purity ────────────────────────────────────────────────────────────────
def _imports(tree, *, top_level_only: bool) -> set[str]:
    """The module names a parsed file imports."""
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
                names.update(alias.name.split(".")[0] for alias in node.names)
    return names


def test_the_module_is_pure_at_import_time():
    """No db, no config, no pool, no rbac at import — it is a reading of text."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(D))
    assert _imports(tree, top_level_only=True) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_the_lexicons_are_borrowed_lazily_and_guarded():
    """The cross-module reaches are late and degrade to nothing.

    They must be inside functions — so importing this module never pulls in
    ``config`` — and wrapped, so a host without the lexicons loses a reading
    rather than the module.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(D))
    top = _imports(tree, top_level_only=True)
    lazy = _imports(tree, top_level_only=False) - top
    assert lazy, "the borrow must exist for this test to mean anything"
    assert "addressing" in lazy
    assert "people" in lazy
    assert "temporal" in lazy
    assert lazy & top == set()


def test_without_the_time_nouns_the_duration_guard_degrades_to_the_old_reading():
    """A host missing the list loses the guard, not the module."""
    original = D._temporal_nouns
    try:
        D._temporal_nouns = lambda: frozenset()
        assert D.read_act("چند دقیقه پیش فرستادم").kind == D.ACT_QUESTION
        # …and the ordinary question is unchanged either way.
        assert D.read_act("قیمت چنده؟").kind == D.ACT_QUESTION
    finally:
        D._temporal_nouns = original
