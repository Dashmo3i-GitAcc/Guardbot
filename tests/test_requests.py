"""Does this message ask for the action, or forbid it?

The claim this file holds: the server can tell «بنش کن» from «بنش نکن» — the
same directive, opposite in force — and it can say *which word* reverses the
message, without a model and without guessing.

The reason it matters is the failure it prevents. ``app/discourse.py`` reads both
messages as ``instruction`` and quotes the directive «بنش»; a transcript that
stops there invites the model to ban the person the room just protected, and a
wrong-person moderation action is the worst mistake available here. So the reader
adds the direction, and the two are rendered as one block on purpose — an
``instruction`` reading must never outlive the negation that reverses it.

Everything here is **evidence**: nothing branches on it, nothing is authorised by
it, and the purity tests at the bottom are what keep that true as the module grows.
"""
import pytest

from app import requests as R


# ── The claim: the prohibitor directly after the directive ────────────────
@pytest.mark.parametrize(
    "text,directive,negator",
    [
        ("بنش نکن", "بنش", "نکن"),
        ("اینو بن نکن", "بن", "نکن"),
        ("اینو ساکتش نکن", "ساکتش", "نکن"),
        ("فایل رو پاکش نکنید", "پاکش", "نکنید"),
        ("بنش نکنی", "بنش", "نکنی"),
        ("بنش نکنن", "بنش", "نکنن"),
        ("بنش نکنیم", "بنش", "نکنیم"),
    ],
)
def test_a_prohibitor_directly_after_the_directive_negates_it(text, directive, negator):
    r = R.read_request(text)
    assert r.directive == directive
    assert r.negated()
    assert r.negator == negator


def test_the_reason_names_the_word_that_does_the_reversing():
    """The reason quotes the message rather than the server's category."""
    r = R.read_request("بنش نکن")
    assert r.why[0] == "the directive «بنش»"
    assert "نکن" in r.why[1]


# ── The scope of the claim: the prohibitor must be *adjacent* ─────────────
def test_a_prohibitor_further_away_does_not_claim_a_negation():
    """«بنش رو نکن» is a message the server cannot scope, not a claim.

    The rule that may *claim* a negation is positional on purpose: a wider window
    would claim a negation the message does not make. Here the word after the
    directive is «رو», so the reader abstains instead.
    """
    r = R.read_request("بنش رو نکن")
    assert r.directive == "بنش"
    assert r.polarity == ""
    assert not r.negated()


# ── The other language's word order ───────────────────────────────────────
@pytest.mark.parametrize(
    "text,negator",
    [
        ("don't ban him", "don"),
        ("do not ban him", "not"),
        ("never ban him", "never"),
        ("not ban him", "not"),
    ],
)
def test_english_negation_goes_before_the_directive(text, negator):
    """The tokenizer splits the apostrophe, so «don't» arrives as «don» + «t»."""
    r = R.read_request(text)
    assert r.directive == "ban"
    assert r.negated()
    assert r.negator == negator


def test_a_persian_word_before_a_directive_never_claims_a_negation():
    """The look-back window lists English forms only, and that is load-bearing.

    «نه بنش کن» is "no, ban him" — a Persian word before a directive is not a
    negator, and claiming one from it would be exactly backwards. The Persian
    negation still downgrades the reading to an abstention through the broad
    rule; it never makes it a claim.
    """
    r = R.read_request("نه بنش کن")
    assert r.directive == "بنش"
    assert r.polarity == ""
    assert not r.negated()


# ── The downgrade: a negation the reader cannot scope ─────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "این آدم خوب نیست، بنش کن",
        "نه بنش کن",
        "چرا پاکش نکردی، بنش کن",
        "خوب نیست بنش کن",
    ],
)
def test_a_negation_elsewhere_abstains_rather_than_claiming_the_action(text):
    """Abstention is the honest reading, and the safe error direction.

    A false *claim* of "affirmative" for a message that forbids the action is the
    mistake this module exists to prevent; an abstention only withholds a signal
    the model did not have before this stage.
    """
    r = R.read_request(text)
    assert r
    assert r.polarity == ""
    assert not r.negated()


# ── Two directives, two directions ────────────────────────────────────────
def test_a_later_negated_directive_stops_the_reader_claiming_the_action():
    """The dangerous direction again, in the shape the corpus does not show.

    «بنش کن، پاکش نکن» both asks for a ban and forbids a deletion. The first
    directive is unnegated, so the naive reading is ``affirmative`` — and that
    would put "the room asks for a ban" in the prompt for a message that also
    carries a prohibition. A one-line summary cannot hold both directions, so
    the reader abstains.
    """
    r = R.read_request("بنش کن، پاکش نکن")
    assert r.directive == "بنش"
    assert r.polarity == ""
    assert not r.negated()


def test_the_reading_is_about_the_first_directive():
    """Documented, and pinned so it cannot drift into an accident."""
    assert R.read_request("پاکش نکن، بنش کن").directive == "پاکش"
    assert R.read_request("پاکش نکن، بنش کن").negated()


# ── The affirmative ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,directive",
    [
        ("بنش کن", "بنش"),
        ("اینو بن کن", "بن"),
        ("فایل رو بفرست", "بفرست"),
        ("ban him", "ban"),
        ("لطفا پاکش کن", "پاکش"),
    ],
)
def test_a_directive_with_no_negation_asks_for_the_action(text, directive):
    r = R.read_request(text)
    assert r.directive == directive
    assert r.polarity == R.POLARITY_AFFIRMATIVE
    assert not r.negated()


# ── The manner ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "میشه بنش کنی؟",
        "لطفا بنش کن",
        "ممنون میشم پاکش کنی",
        "ممکنه پاکش کنی",
        "please ban him",
        "could you ban him",
    ],
)
def test_a_politeness_frame_makes_it_a_request(text):
    assert R.read_request(text).manner == R.MANNER_REQUEST


@pytest.mark.parametrize("text", ["بنش کن", "ban him", "فایل رو بفرست"])
def test_a_bare_imperative_is_a_command(text):
    assert R.read_request(text).manner == R.MANNER_COMMAND


def test_the_manner_is_read_even_when_the_direction_is_negated():
    """A polite prohibition is still a request, and still negated."""
    r = R.read_request("میشه بنش نکنی؟")
    assert r.negated()
    assert r.manner == R.MANNER_REQUEST


# ── The Arabic block's punctuation is not part of the word ────────────────
# «؟» «،» «؛» live inside \u0600-\u06ff, so «نکنی؟» is not «نکنی»: the last word
# of the message was never looked up and the prohibition read as a plain request
# *to* ban. This is the dangerous direction, and it is what the reader is most
# sensitive to.
@pytest.mark.parametrize(
    "text,directive",
    [
        ("میشه بنش نکنی؟", "بنش"),
        ("بنش نکن،", "بنش"),
        ("پاکش نکنید؟", "پاکش"),
        ("بنش نکن!", "بنش"),
    ],
)
def test_a_trailing_mark_does_not_hide_the_prohibitor(text, directive):
    r = R.read_request(text)
    assert r.directive == directive
    assert r.negated()


def test_a_mark_does_not_invent_a_directive():
    """The other direction: punctuation alone is not a reading."""
    assert not R.read_request("؟")
    assert not R.read_request("، ،")


# ── The empty reading ─────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["سلام بچه ها", "", None, "چطوری؟", "مرسی"])
def test_a_message_with_no_directive_reads_as_nothing(text):
    r = R.read_request(text)
    assert not r
    assert r.directive == ""
    assert r.polarity == ""
    assert r.manner == ""


def test_the_empty_reading_renders_nothing():
    assert R.render(R.read_request("سلام بچه ها")) == ""


# ── Rendering ─────────────────────────────────────────────────────────────
def test_the_negation_is_rendered_in_words():
    out = R.render(R.read_request("بنش نکن"))
    assert "negates" in out
    assert "بنش" in out and "نکن" in out
    assert "not" in out


def test_a_bare_affirmative_command_renders_nothing():
    """Silence is the right render: the act reader already says ``instruction``,
    and a line saying "and it is affirmative" would be a line of noise on every
    ordinary moderation message."""
    assert R.render(R.read_request("بنش کن")) == ""


def test_a_polite_affirmative_is_rendered():
    out = R.render(R.read_request("میشه بنش کنی؟"))
    assert "بنش" in out
    assert "polite" in out


def test_the_unscoped_negation_is_rendered_as_an_abstention():
    out = R.render(R.read_request("این آدم خوب نیست، بنش کن"))
    assert "بنش" in out
    assert "could not scope" in out or "not assume" in out


def test_every_rendered_line_ends_with_a_newline():
    """The act line and the polarity line are concatenated into one block, so a
    missing newline would run the two sentences together."""
    for text in ("بنش نکن", "میشه بنش کنی؟", "این آدم خوب نیست، بنش کن"):
        out = R.render(R.read_request(text))
        assert out and out.endswith("\n")


# ── Purity ────────────────────────────────────────────────────────────────
def _imports(tree, *, top_level_only: bool) -> set[str]:
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
    """No db, no config, no pool, no rbac — the module is a reader."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    assert _imports(tree, top_level_only=True) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_both_borrows_are_lazy_and_guarded():
    """``people`` for the fold and ``discourse`` for the lexicon, both inside the
    functions that need them and both wrapped, so a host without either loses the
    handling rather than the module."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    top = _imports(tree, top_level_only=True)
    lazy = _imports(tree, top_level_only=False) - top
    assert {"people", "discourse"} <= lazy
    assert lazy & top == set()


def test_the_directive_lexicon_is_borrowed_not_copied():
    """A second list would be a second answer that drifts.

    The module must not define an imperative or action-word set of its own: it
    asks ``discourse`` which word made the message an instruction.
    """
    import inspect

    source = inspect.getsource(R)
    assert "_IMPERATIVES" not in source
    assert "_ACTION_WORDS" not in source
    assert "def _action_words" not in source


def test_the_module_holds_no_path_to_an_action():
    """The action verbs, and ``ctx.bot`` — a reader calls none of them.

    ``admin_service`` is checked as an *import* rather than as a string, because
    the docstring names it in prose to say it re-authorises every request.
    """
    import inspect

    source = inspect.getsource(R)
    for forbidden in (
        "send_message",
        "delete_message",
        "ban_chat_member",
        "restrict_chat_member",
        "gemini",
        "ctx.bot",
    ):
        assert forbidden not in source


def test_it_imports_no_authority():
    """No db, no config, no pool, no rbac, no admin_service — not even lazily."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    assert _imports(tree, top_level_only=False) & {
        "db",
        "config",
        "pool",
        "rbac",
        "admin_service",
        "gemini",
        "bot",
        "telegram",
        "main",
    } == set()


def test_the_module_never_decides_only_reports():
    """The dataclass carries a reason, and no field is named like a verdict."""
    r = R.read_request("بنش نکن")
    assert r.why
    assert all(isinstance(part, str) for part in r.why)
    for verdict in ("action", "allowed", "denied", "should", "execute"):
        assert not hasattr(r, verdict)
