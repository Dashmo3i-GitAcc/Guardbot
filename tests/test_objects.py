"""What does the request act on?

The claim this file holds: the server can say what a directive acts on — a person,
a media message, a link, a message, or a thing whose kind the message does not
state — and it can say *how* it knows, without a model and without guessing.

The reason it matters is the failure it prevents, and it is the same failure
``app/entities.py`` and ``app/requests.py`` exist for, one join further along.
``discourse`` says "instruction, the directive «پاک»"; ``entities`` says "things —
media"; ``referents`` says "who «پاکش» may mean — مهدی". Nothing said which of
those the directive was aimed at, and the model had to join them itself. That join
is where a person gets banned over a photograph, and it was measured: on a window
holding both a person and a media row, five of ten thing-object requests handed
the model a person as the target.

Everything here is **evidence**: nothing branches on it, nothing is authorised by
it, and the purity tests at the bottom are what keep that true as the module grows.
"""
import pytest

from app import objects as O

ADMIN = 33
MEMBER = 11
OTHER = 14


def row(user_id, text, at, *, mid=0, kind="", name="", reply_mid=0, role="member"):
    """One window row, with the columns ``db.group_window`` returns."""
    return {
        "id": int(mid),
        "user_id": int(user_id),
        "text": text,
        "at": int(at),
        "name": name,
        "message_id": int(mid),
        "kind": kind,
        "reply_message_id": int(reply_mid),
        "reply_user_id": 0,
        "role": role,
    }


def anchor(text, at=1300):
    return row(ADMIN, text, at, mid=99, name="Admin", role="admin")


# A room that holds a document and has two members who spoke — the shape that
# makes the wrong lead possible.
ROOM = [
    row(MEMBER, "سلام بچه ها", 900, mid=1, name="Reza"),
    row(OTHER, "چطوری", 960, mid=4, name="Mahdi"),
    row(OTHER, "", 940, mid=3, kind="document"),
]
ROOM_LINK = [
    row(MEMBER, "سلام بچه ها", 900, mid=1, name="Reza"),
    row(OTHER, "اینو ببینید https://example.com/report", 920, mid=2, name="Mahdi"),
]


# ── The class the request acts on ─────────────────────────────────────────
@pytest.mark.parametrize(
    "text,kind,source",
    [
        ("این فایل رو پاک کن", O.CLASS_MEDIA, O.SOURCE_NAMED),
        ("این لینک رو حذف کن", O.CLASS_LINK, O.SOURCE_NAMED),
        ("این پیام رو پاک کن", O.CLASS_MESSAGE, O.SOURCE_NAMED),
        ("پاکش کن", O.CLASS_MEDIA, O.SOURCE_POINTED),
        ("حذفش کن", O.CLASS_MEDIA, O.SOURCE_POINTED),
        ("پاک کن", O.CLASS_THING, O.SOURCE_VERB),
        ("ساکتش کن", O.CLASS_PERSON, O.SOURCE_VERB),
        ("اینو بن کن", O.CLASS_PERSON, O.SOURCE_VERB),
    ],
)
def test_what_the_request_acts_on_is_read(text, kind, source):
    state = O.read_object(text, ROOM, anchor(text))
    assert state.kind == kind
    assert state.source == source


@pytest.mark.parametrize(
    "text,kind",
    [
        ("سلام بچه ها", ""),
        ("", ""),
        (None, ""),
        ("مرسی", ""),
        # A generic imperative carries no side. Guessing one is the mistake this
        # module exists to prevent, so it abstains.
        ("برای اینم همین کارو بکن", ""),
    ],
)
def test_a_message_with_nothing_to_read_reads_as_nothing(text, kind):
    state = O.read_object(text, ROOM, anchor(text or ""))
    assert state.kind == kind
    assert not state
    assert state.source == ""


def test_the_verb_decides_the_side_and_the_shape_cannot():
    """«پاکش کن» and «بنش کن» are the same shape — a directive carrying the object
    clitic «ـش» — and they act on opposite things. Only the verb separates them,
    which is why the split lives beside the lexicon it splits."""
    thing = O.read_object("پاکش کن", ROOM, anchor("پاکش کن"))
    person = O.read_object("بنش کن", ROOM, anchor("بنش کن"))
    assert thing.kind == O.CLASS_MEDIA
    assert person.kind == O.CLASS_PERSON


def test_a_named_thing_wins_over_the_verb():
    """The message's own words are the strongest evidence: a noun that names a
    thing is what the room actually said, and the verb only says which side an
    argument has."""
    assert O.read_object("فایل رو بن کن", ROOM, anchor("فایل رو بن کن")).kind == (
        O.CLASS_MEDIA
    )


def test_the_kind_is_not_guessed_from_the_room_when_nothing_is_pointed_at():
    """«پاک کن» acts on a thing, and the message does not say which. The room's
    newest photograph is not its object just because the room has one."""
    state = O.read_object("پاک کن", ROOM, anchor("پاک کن"))
    assert state.kind == O.CLASS_THING
    assert state.source == O.SOURCE_VERB


def test_a_message_that_points_at_nothing_does_not_borrow_the_rooms_thing():
    """The same rule, stated as the thing it prevents: with no window there is
    nothing to point at, so the reading is the verb's and nothing more."""
    assert O.read_object("پاکش کن", [], anchor("پاکش کن")).kind == O.CLASS_THING
    assert O.read_object("پاکش کن", [], anchor("پاکش کن")).source == O.SOURCE_VERB


def test_the_thing_pointed_at_is_read_from_the_window():
    """A bare demonstrative with a content verb, in a room whose newest thing is
    a link rather than a media row."""
    state = O.read_object("اینو حذف کن", ROOM_LINK, anchor("اینو حذف کن"))
    assert state.kind == O.CLASS_LINK
    assert state.source == O.SOURCE_POINTED


def test_the_fold_survives_the_orthographies():
    assert O.read_object("فايل رو پاك كن", ROOM, anchor("فايل رو پاك كن")).kind == (
        O.CLASS_MEDIA
    )


def test_a_trailing_mark_does_not_hide_the_object():
    """«؟» «،» «؛» live inside the Persian block, so a "not a Persian letter"
    split keeps them glued to the word before it — the directive would not be
    found, and with it the object would not either."""
    assert O.read_object("این فایل رو پاک کن؟", ROOM, anchor("x")).kind == O.CLASS_MEDIA
    assert O.read_object("پاکش کن،", ROOM, anchor("پاکش کن،")).kind == O.CLASS_MEDIA
    assert O.read_object("فایل رو پاک کن!", ROOM, anchor("x")).kind == O.CLASS_MEDIA


# ── The reason, and the surface ───────────────────────────────────────────
def test_the_surface_word_the_message_used_is_kept():
    state = O.read_object("این فایل رو پاک کن", ROOM, anchor("این فایل رو پاک کن"))
    assert state.surface == "فایل"


def test_the_clitic_inside_the_directive_is_not_quoted_as_the_object():
    """«بنش» carries the object marker inside the verb; reporting «بنش» as the
    word that points at the person would be a second quotation of the directive."""
    assert O.read_object("بنش کن", ROOM, anchor("بنش کن")).surface == ""
    assert O.read_object("اینو بن کن", ROOM, anchor("اینو بن کن")).surface == "اینو"


def test_every_reading_carries_a_reason():
    for text in ("این فایل رو پاک کن", "پاکش کن", "ساکتش کن", "پاک کن"):
        state = O.read_object(text, ROOM, anchor(text))
        assert state.why
        assert state.why[0].startswith("the directive")


# ── Rendering ─────────────────────────────────────────────────────────────
def test_the_person_reading_says_it_is_not_a_thing():
    out = O.render(O.read_object("اینو بن کن", ROOM, anchor("اینو بن کن")))
    assert "person" in out
    assert "not on a thing" in out


def test_the_thing_reading_says_what_the_object_is_and_stops():
    """The correction the resolver's person-candidates need — and no more.

    Acting on a person when the message was about a photograph is the worst
    mistake here, so the line says "a thing, not a person". It used to add an
    order — "Do not read it as aimed at anybody in the room" — which is false
    whenever an *explicit* source names somebody: a reply edge, a stated id and a
    name are facts, and ``app/referents.py`` keeps them for exactly the case where
    the request acts on a thing, because they identify who the thing belongs to.
    The block printed beside this one then names that person, often as
    ``confident``, and the model has to choose which to believe.

    The block that knows the object side does not know the people side, so it
    states its own half and stops — the rule the entity block's closing line was
    already corrected to.
    """
    out = O.render(O.read_object("پاکش کن", ROOM, anchor("پاکش کن")))
    assert "not a person" in out
    assert "anybody" not in out
    assert "Do not" not in out


@pytest.mark.parametrize(
    "text,room",
    [
        ("پاکش کن", ROOM),                    # a media row the room holds
        ("این لینک رو حذف کن", ROOM_LINK),     # a named link
    ],
)
def test_a_thing_object_and_a_named_person_can_be_true_together(text, room):
    """The two halves are not in conflict, so the line must not make them be."""
    state = O.read_object(text, room, anchor(text))
    assert state.kind not in ("", O.CLASS_PERSON)
    out = O.render(state)
    assert "not a person" in out
    assert "aimed at anybody" not in out


def test_an_unknown_kind_says_so_rather_than_naming_one():
    out = O.render(O.read_object("پاک کن", ROOM, anchor("پاک کن")))
    assert "a thing, not a person" in out
    assert "does not say which thing" in out


def test_nothing_renders_nothing():
    assert O.render(O.read_object("سلام بچه ها", ROOM, anchor("سلام"))) == ""
    assert O.render(O.Object()) == ""


def test_every_rendered_line_ends_with_a_newline():
    for text in ("پاکش کن", "اینو بن کن", "پاک کن"):
        out = O.render(O.read_object(text, ROOM, anchor(text)))
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

    tree = ast.parse(inspect.getsource(O))
    assert _imports(tree, top_level_only=True) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_every_borrow_is_lazy_and_guarded():
    """``people`` for the fold, ``discourse`` for the lexicon, ``entities`` for the
    thing nouns and ``referents`` for the pointing — all inside the functions that
    need them and all wrapped, so a host without any of them loses the handling
    rather than the module."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(O))
    top = _imports(tree, top_level_only=True)
    lazy = _imports(tree, top_level_only=False) - top
    assert {"people", "discourse", "entities", "referents"} <= lazy
    assert lazy & top == set()


def test_the_verb_split_is_not_a_second_copy():
    """The split lives in ``discourse``, beside the lexicon it splits, because two
    readers need it. A copy here would be a second answer that drifts — so the
    test looks for the *assignments*, not for a mention of them in prose."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(O))
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
    assert "_PERSON_VERBS" not in assigned
    assert "_THING_VERBS" not in assigned
    assert "ACTION_WORDS" not in assigned


def test_the_module_holds_no_path_to_an_action():
    import inspect

    source = inspect.getsource(O)
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
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(O))
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
    state = O.read_object("پاکش کن", ROOM, anchor("پاکش کن"))
    assert state.why
    for verdict in ("action", "allowed", "denied", "should", "execute"):
        assert not hasattr(state, verdict)
