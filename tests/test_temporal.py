"""When does «الان» mean? The server's clock, and only the server's clock.

The claim this file holds is narrow and worth stating exactly: the server can
read the *direction* and the *granularity* a Persian time word states, from the
words alone, without a model and without inventing a date. «دیروز» points
backwards at a scale of days; «فردا» points forwards at a scale of days;
«چند دقیقه پیش» points backwards at a scale of minutes and states no number of
them. None of those is a date, and the module is not allowed to produce one —
a date would be a claim, and «قبلاً» does not contain one.

Everything here is **evidence**: nothing branches on a reading, nothing is
authorised by it, and the purity test at the bottom is what keeps that true as
the module grows.

The ordering tests are the interesting ones. The table is scanned in order and
the first hit wins, so «نیم ساعت پیش» must be tried before «ساعت پیش» — the two
differ only in the offset the words state — and the words that *state* a
direction must be tried before the demonstrative forms that inherit one from the
room, or «فردا اون موقع» would read as the past.
"""
import pytest

from app import temporal as T


# ── The reading, by class ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,kind",
    [
        # now
        ("الان", T.WHEN_NOW),
        ("همین الان", T.WHEN_NOW),
        ("همین حالا", T.WHEN_NOW),
        ("در حال حاضر", T.WHEN_NOW),
        ("هماکنون", T.WHEN_NOW),
        ("فعلا", T.WHEN_NOW),
        ("هنوز", T.WHEN_NOW),
        ("امروز", T.WHEN_NOW),
        # past
        ("قبلاً", T.WHEN_PAST),
        ("پیشتر", T.WHEN_PAST),
        ("چند دقیقه پیش", T.WHEN_PAST),
        ("دیروز", T.WHEN_PAST),
        ("دیشب", T.WHEN_PAST),
        ("پریروز", T.WHEN_PAST),
        ("پارسال", T.WHEN_PAST),
        ("هفته پیش", T.WHEN_PAST),
        ("ماه قبل", T.WHEN_PAST),
        ("چند وقت پیش", T.WHEN_PAST),
        # future
        ("فردا", T.WHEN_FUTURE),
        ("پس فردا", T.WHEN_FUTURE),
        ("بعداً", T.WHEN_FUTURE),
        ("هفته بعد", T.WHEN_FUTURE),
        ("هفته آینده", T.WHEN_FUTURE),
        ("در آینده", T.WHEN_FUTURE),
        # repeat
        ("دوباره", T.WHEN_REPEAT),
        ("بازم", T.WHEN_REPEAT),
        ("باز هم", T.WHEN_REPEAT),
        ("مجدداً", T.WHEN_REPEAT),
        ("از نو", T.WHEN_REPEAT),
        # the demonstrative forms
        ("این هفته", T.WHEN_NOW),
        ("همین موقع", T.WHEN_NOW),
        ("اون موقع", T.WHEN_PAST),
        ("همون روز", T.WHEN_PAST),
        ("این مدت", T.WHEN_NOW),
        ("اون مدت", T.WHEN_PAST),
    ],
)
def test_the_direction_is_read(text, kind):
    assert T.read_when(text).kind == kind


@pytest.mark.parametrize(
    "text,unit,seconds",
    [
        ("دیروز", T.UNIT_DAY, T.DAY),
        ("پریروز", T.UNIT_DAY, 2 * T.DAY),
        ("فردا", T.UNIT_DAY, T.DAY),
        ("پس فردا", T.UNIT_DAY, 2 * T.DAY),
        ("هفته پیش", T.UNIT_WEEK, T.WEEK),
        ("ماه پیش", T.UNIT_MONTH, T.MONTH),
        ("سال پیش", T.UNIT_YEAR, T.YEAR),
        ("نیم ساعت پیش", T.UNIT_HOUR, T.HOUR // 2),
        ("یه ربع پیش", T.UNIT_MINUTE, 0),
        # «چند دقیقه پیش» states a scale and no number of minutes.
        ("چند دقیقه پیش", T.UNIT_MINUTE, 0),
        # A time word with no stated span states none.
        ("قبلاً", "", 0),
        ("الان", "", 0),
        # «چند وقت پیش» is "a while ago": a direction and no scale.
        ("چند وقت پیش", "", 0),
        # A demonstrative and a unit states the unit, not an offset.
        ("این هفته", T.UNIT_WEEK, 0),
        ("اون روز", T.UNIT_DAY, 0),
    ],
)
def test_the_granularity_and_offset_are_read(text, unit, seconds):
    when = T.read_when(text)
    assert when.unit == unit
    assert when.seconds == seconds


def test_an_ordinary_message_has_no_reading():
    for text in ("بنش کن", "سلام بچه ها", "اینو ببین", "", None):
        assert not T.read_when(text)
        assert T.read_when(text).kind == ""


def test_a_reading_is_truthy_and_an_empty_one_is_not():
    assert T.read_when("دیروز")
    assert not T.read_when("سلام")


# ── Ordering: the whole correctness argument for the table ────────────────
def test_the_longer_phrase_wins_where_it_carries_more():
    """«نیم ساعت پیش» must beat «ساعت پیش» — the offset is the difference.

    Both are past, both at a scale of hours, so the *only* thing that
    distinguishes them is ``seconds``. If the shorter phrase matched first the
    reading would still look plausible and would be wrong, which is exactly the
    kind of miss a kind-only assertion would not catch.
    """
    assert T.read_when("نیم ساعت پیش").seconds == T.HOUR // 2
    assert T.read_when("یه ساعت پیش").seconds == T.HOUR
    assert T.read_when("ساعت پیش").seconds == 0
    # …and «پس فردا» must beat «فردا».
    assert T.read_when("پس فردا").seconds == 2 * T.DAY
    assert T.read_when("پریروز").seconds == 2 * T.DAY


def test_the_words_that_state_a_direction_are_tried_before_the_ones_that_inherit_it():
    """«فردا اون موقع» is the future; the explicit word decides.

    «اون موقع» points back at a time the room established, and on its own reads
    as the past. In a sentence that also says «فردا» the explicit word is the one
    that knows, so the table must offer it first.
    """
    assert T.read_when("فردا اون موقع میام").kind == T.WHEN_FUTURE
    assert T.read_when("اون موقع").kind == T.WHEN_PAST


def test_the_table_is_the_explicit_words_then_the_demonstrative_ones():
    assert T._PHRASES[: len(T._EXPLICIT_PHRASES)] == T._EXPLICIT_PHRASES
    assert T._PHRASES[len(T._EXPLICIT_PHRASES) :] == T._DEICTIC_PHRASES


def test_every_demonstrative_phrase_names_a_time_noun():
    """The generated table cannot drift from the noun list it is built over."""
    for phrase, _, _, _ in T._DEICTIC_PHRASES:
        noun = phrase.split(" ", 1)[1]
        assert noun in T.TEMPORAL_NOUNS, phrase


# ── Folding, typos and the mixed sentence ─────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "همین الان",       # the ZWNJ, which the shared fold does not always take
        "همین\u200cالان",
        "دیروز",           # a plain word
        "قبلاً",           # the tanwin, which the fold strips
        "چند دقیقه پیش",   # the ordinary spacing
    ],
)
def test_the_fold_survives_the_orthographies(text):
    assert T.read_when(text)


def test_a_zwnj_inside_a_phrase_is_folded_away():
    """«همینالان» and «همین الان» are the same time."""
    assert T.read_when("همین\u200cالان").kind == T.WHEN_NOW
    assert T.read_when("همین الان").kind == T.WHEN_NOW


def test_an_arabic_yeh_is_read_as_a_persian_one():
    """«آينده» with the Arabic yeh is «آینده», and it is the future.

    The shared fold maps the Arabic yeh to the Persian one, so a word typed on an
    Arabic keyboard is read exactly as one typed on a Persian one.
    """
    assert T.read_when("هفته آینده").kind == T.WHEN_FUTURE
    assert T.read_when("هفته آينده").kind == T.WHEN_FUTURE


def test_a_time_word_in_a_mixed_sentence_is_still_read():
    assert T.read_when("اینو check کن، دیروز فرستادم").kind == T.WHEN_PAST
    assert T.read_when("لطفا الان بنش کن").kind == T.WHEN_NOW


def test_the_surface_is_the_words_the_message_used():
    """The surface is taken from the original, so a log reads as it was written."""
    assert T.read_when("دیروز رفتم").surface == "دیروز"
    assert T.read_when("چند دقیقه پیش اومد").surface == "چند دقیقه پیش"
    assert T.read_when("همین\u200cالان").surface == "همین الان"
    assert T.read_when("قبلاً گفتم").surface == "قبلاً"


# ── The shared noun list ──────────────────────────────────────────────────
def test_the_time_nouns_are_exported_for_the_referent_reader():
    """``referents`` consults this list so «این هفته» is not a person.

    The two readers share the fact rather than each keeping a list, which is why
    the list is a module constant with a name rather than a literal buried in a
    function.
    """
    for noun in ("الان", "هفته", "ماه", "سال", "روز", "شب", "موقع", "وقت"):
        assert noun in T.TEMPORAL_NOUNS


def test_a_time_word_is_not_a_person_reference():
    """The guard ``referents`` applies, asserted from this side of the seam."""
    from app import referents

    for text in ("همین الان", "این هفته", "اون موقع", "همون روز", "این ماه"):
        assert referents.find_expression(text).kind == "", text


def test_a_demonstrative_before_a_person_noun_is_still_a_person():
    """The guard must not swallow the case the resolver exists for."""
    from app import referents

    assert referents.find_expression("این کاربر").kind == referents.KIND_PERSON
    assert referents.find_expression("همون کاربر").kind == referents.KIND_PERSON


# ── Rendering ─────────────────────────────────────────────────────────────
def test_nothing_is_rendered_without_a_time_word():
    assert T.render(T.read_when("سلام بچه ها")) == ""
    assert T.render(T.When()) == ""


def test_the_line_names_the_word_and_the_direction():
    line = T.render(T.read_when("دیروز"))
    assert "دیروز" in line
    assert "backwards, before now" in line
    assert "at a scale of days" in line


def test_the_line_says_the_clock_is_the_servers():
    line = T.render(T.read_when("الان"))
    assert "server's clock" in line
    assert "not a reading of the words" in line


def test_the_line_never_states_a_date():
    """A relative word is not a date, and the block must not invent one.

    The calendar block is the one that may state a date, and it reads the
    server's clock for it. This block must never look like a date: no calendar
    names, and no absolute day.
    """
    line = T.render(T.read_when("چند دقیقه پیش"), now=1_700_000_000, window_start=1_699_999_000)
    assert "Gregorian" not in line
    assert "Jalali" not in line
    assert "2023" not in line


def test_the_window_age_is_stated_when_both_ends_are_known():
    line = T.render(T.read_when("قبلاً"), now=1000, window_start=100)
    assert "starts" in line
    assert "work from the server's times" in line


def test_the_window_age_is_not_stated_without_a_clock():
    """A second clock is a second answer, so a missing one renders nothing."""
    assert "starts" not in T.render(T.read_when("قبلاً"), now=1000, window_start=0)
    assert "starts" not in T.render(T.read_when("قبلاً"), now=0, window_start=100)
    # A window that starts after "now" is not a window this pass can describe.
    assert "starts" not in T.render(T.read_when("قبلاً"), now=100, window_start=1000)


def test_the_stated_span_is_spoken_as_an_approximation():
    line = T.render(T.read_when("نیم ساعت پیش"))
    assert "about 1 hour(s) ago" not in line  # half an hour is not "about 1 hour"
    assert "minute" in line


# ── The sentence must agree with itself ───────────────────────────────────
# The direction and the offset are two halves of one reading, and they were
# worded by one function — so a reading that pointed forwards was rendered as
# "points forwards, after now ... about 1 day(s) ago". The reading was right and
# the sentence contradicted itself, in eight of the ninety-four phrases. The
# benchmark could not see it because it compares the structured kind and unit;
# a reading is not a sentence.
def _contradicts(line: str) -> bool:
    """Whether the rendered sentence's two halves point opposite ways.

    The window's age is stated on its own line and is always in the past, so it
    is excluded — otherwise a future reading with a window behind it would look
    like a contradiction.
    """
    head = line.split("The window this pass is reading")[0]
    return ("forwards" in head and " ago" in head) or (
        "backwards" in head and "from now" in head
    )


def test_no_phrase_renders_a_sentence_that_contradicts_itself():
    """The property, over every phrase the reader knows — not a sample.

    A sample is how this survived: «فردا» was in the corpus and passed, because
    the corpus scores the reading. The eight phrases that broke were the other
    future offsets, and a property over the whole table is what makes the class
    impossible to reintroduce rather than the one phrase impossible to reintroduce.
    """
    broken = []
    for phrase, _kind, _unit, _seconds in T._PHRASES:
        when = T.read_when(phrase)
        if not when:
            continue
        line = T.render(when, now=1000, window_start=100)
        if _contradicts(line):
            broken.append((phrase, line.strip()))
    assert broken == [], broken


def test_a_forward_reading_states_a_forward_offset():
    """The positive half: the fix is a wording, not a suppression."""
    line = T.render(T.read_when("فردا"))
    assert "forwards, after now" in line
    assert "about 1 day(s) from now" in line
    assert " ago" not in line.split("The window")[0]


def test_a_backward_reading_states_a_backward_offset():
    line = T.render(T.read_when("دیروز"))
    assert "backwards, before now" in line
    assert "about 1 day(s) ago" in line
    assert "from now" not in line


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("فردا", "about 1 day(s) from now"),
        ("پس فردا", "about 2 day(s) from now"),
        ("هفته بعد", "about 1 week(s) from now"),
        ("ماه بعد", "about 1 month(s) from now"),
        ("سال بعد", "about 1 year(s) from now"),
    ],
)
def test_every_future_magnitude_is_worded_forwards(phrase, expected):
    """One case per magnitude band, because the band is a separate branch."""
    assert expected in T.render(T.read_when(phrase))


def test_a_repeat_reading_states_no_offset():
    """«دوباره» states a repetition, not an age.

    Its span would be a *period* — "about 1 day(s)" of what? — so it renders
    none, and saying "ago" for it would be a different wrong sentence.
    """
    for phrase in ("دوباره", "بازم", "مجدد"):
        line = T.render(T.read_when(phrase))
        assert "ago" not in line
        assert "from now" not in line


def test_the_window_age_is_always_worded_backwards():
    """The window started before the pass read it, whatever the message says."""
    line = T.render(T.read_when("فردا"), now=1000, window_start=100)
    assert "starts about 15 minute(s) ago" in line
    assert "forwards" in line  # the message's own half is untouched


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

    tree = ast.parse(inspect.getsource(T))
    assert _imports(tree, top_level_only=True) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_the_shared_fold_is_borrowed_lazily_and_guarded():
    """The reach into ``people`` is late, so importing this never pulls config.

    A host without the shared fold loses the orthography handling and keeps the
    module — the read degrades, the import does not fail.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(T))
    top = _imports(tree, top_level_only=True)
    lazy = _imports(tree, top_level_only=False) - top
    assert "people" in lazy
    assert lazy & top == set()


def test_the_module_holds_no_path_to_an_action():
    """It reads the clock and the words, and nothing else."""
    import inspect

    source = inspect.getsource(T)
    for forbidden in (
        "send_message",
        "delete_message",
        "ban_chat_member",
        "restrict_chat_member",
        "admin_service",
        "gemini",
        "requests",
        "ctx.bot",
    ):
        assert forbidden not in source
