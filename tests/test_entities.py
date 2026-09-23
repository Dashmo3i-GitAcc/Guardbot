"""What «این» points at when it is not a person.

The claim this file holds: the server can name the *things* a demonstrative may
mean — a media message by its stored kind, a link, the message the anchor replies
to — and it can name the class the anchor *says* («این لینک», «این فایل»), without
a model and without guessing.

The reason it matters is the failure it prevents. ``referents`` offers the room's
*people* for a demonstrative, and when the demonstrative means the photograph
somebody just posted, that is a wrong lead — and a wrong-person moderation action
is the worst mistake available here. So the block says, in words, that these are
things and not people.

Everything here is **evidence**: nothing branches on it, nothing is authorised by
it, and the purity test at the bottom is what keeps that true as the module grows.
"""
import pytest

from app import entities as E


def row(user_id, text, at, *, mid=0, kind="", reply_mid=0, role="member", name=""):
    """One window row, with the columns ``db.group_window`` returns."""
    return {
        "user_id": int(user_id),
        "text": text,
        "at": int(at),
        "message_id": int(mid),
        "kind": kind,
        "reply_message_id": int(reply_mid),
        "role": role,
        "name": name,
    }


# ── The class the message names ───────────────────────────────────────────
@pytest.mark.parametrize(
    "text,kind",
    [
        ("این لینک چیه", E.KIND_LINK),
        ("لینکش خرابه", E.KIND_LINK),
        ("این فایل رو بفرست", E.KIND_MEDIA),
        ("عکسشو ببین", E.KIND_MEDIA),
        ("ویدیو رو پاک کن", E.KIND_MEDIA),
        ("ویسش واضح نیست", E.KIND_MEDIA),
        ("استیکر نذار", E.KIND_MEDIA),
        ("این پیام رو حذف کن", E.KIND_MESSAGE),
        ("کامنتش رو پاک کن", E.KIND_MESSAGE),
        ("پست رو ببین", E.KIND_MESSAGE),
    ],
)
def test_the_thing_the_message_names_is_read(text, kind):
    assert E.named_kind(text)[0] == kind


@pytest.mark.parametrize(
    "text",
    ["سلام بچه ها", "اینو بن کن", "این کاربر رو محدود کن", "چرا اینطوری شد", ""],
)
def test_a_message_that_names_no_thing_is_read_as_none(text):
    assert E.named_kind(text) == ("", "")


def test_the_word_the_message_used_is_kept():
    """The reason line quotes the message, not the server's category."""
    assert E.named_kind("این لینک چیه") == (E.KIND_LINK, "لینک")


def test_the_fold_survives_the_orthographies():
    assert E.named_kind("فايل رو بفرست")[0] == E.KIND_MEDIA   # Arabic yeh
    assert E.named_kind("اين لينک چيه")[0] == E.KIND_LINK


@pytest.mark.parametrize(
    "text,kind",
    [
        ("لینکشو ببین", E.KIND_LINK),
        ("فایلشو بفرست", E.KIND_MEDIA),
        ("عکسش رو ببین", E.KIND_MEDIA),
        ("ویسش واضح نیست", E.KIND_MEDIA),
        ("کامنتش رو پاک کن", E.KIND_MESSAGE),
        ("پیامشو حذف کن", E.KIND_MESSAGE),
        ("فیلمها رو ببین", E.KIND_MEDIA),
    ],
)
def test_a_clitic_attached_to_a_thing_noun_is_still_read(text, kind):
    """One clitic is stripped, because the table holds stems rather than every
    combination — the first draft listed forms and missed three of them."""
    assert E.named_kind(text)[0] == kind


@pytest.mark.parametrize("text", ["فایده داره", "عکاس اومد", "پیامدش چیه", "صدامو شنیدی"])
def test_an_ordinary_word_that_ends_like_a_clitic_is_not_a_thing(text):
    """The strip is only accepted on a known noun, so it cannot invent one."""
    assert E.named_kind(text) == ("", "")


# ── The single-token form, which another module borrows ───────────────────
@pytest.mark.parametrize(
    "token,kind",
    [
        ("لینک", E.KIND_LINK),
        ("لینکشو", E.KIND_LINK),
        ("url", E.KIND_LINK),
        ("فایل", E.KIND_MEDIA),
        ("عکسش", E.KIND_MEDIA),
        ("ویدیو", E.KIND_MEDIA),
        ("ویسش", E.KIND_MEDIA),
        ("پیام", E.KIND_MESSAGE),
        ("کامنتش", E.KIND_MESSAGE),
        ("پست", E.KIND_MESSAGE),
    ],
)
def test_a_single_token_is_read_on_its_own(token, kind):
    """``thing_kind`` is what ``referents`` asks about one position.

    It has to agree with ``named_kind`` exactly, or the two readers would
    disagree about whether «این لینک» names a link.
    """
    assert E.thing_kind(token) == kind
    assert E.named_kind(token)[0] == kind


@pytest.mark.parametrize(
    "token", ["", None, "سلام", "فایده", "عکاس", "پیامدش", "ویدی", "لینکا", "پاک"]
)
def test_a_token_that_names_no_thing_reads_as_empty(token):
    """Whole-token only: «ویدی» and «لینکا» are not the nouns they resemble."""
    assert E.thing_kind(token) == ""


def test_the_single_token_form_is_exported():
    """It is a borrow point, so it must be in ``__all__`` and stable."""
    assert "thing_kind" in E.__all__


# ── The demonstrative ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    ["اینو پاک کن", "اون چیه", "همون رو بفرست", "قبلیش خرابه", "این لینک"],
)
def test_a_demonstrative_is_seen(text):
    assert E.has_demonstrative(text) is True


@pytest.mark.parametrize("text", ["سلام", "بن کن", "کاربر رو محدود کن", ""])
def test_a_message_with_no_demonstrative_is_not_pointing(text):
    assert E.has_demonstrative(text) is False


# ── Media, by the stored column and by the text prefix ────────────────────
def test_a_media_row_is_read_from_its_stored_kind():
    state = E.read_entities(
        [row(11, "ببین", 900, mid=1, kind="photo")], row(33, "اینو پاک کن", 1000, mid=2)
    )
    assert [(i.kind, i.detail) for i in state.items] == [(E.KIND_MEDIA, "photo")]


def test_a_media_row_is_read_from_the_text_prefix_when_the_column_is_empty():
    """``awareness.capture`` writes the same fact into the text."""
    state = E.read_entities(
        [row(11, "[video] ببین", 900, mid=1)],
        row(33, "اینو پاک کن", 1000, mid=2),
    )
    assert [(i.kind, i.detail) for i in state.items] == [(E.KIND_MEDIA, "video")]


def test_an_ordinary_message_is_not_media():
    state = E.read_entities(
        [row(11, "سلام", 900, mid=1)], row(33, "اینو پاک کن", 1000, mid=2)
    )
    assert state.items == ()


def test_the_media_excerpt_has_the_prefix_taken_off():
    state = E.read_entities(
        [row(11, "[photo] این چیه", 900, mid=1)],
        row(33, "اینو پاک کن", 1000, mid=2),
    )
    assert state.items[0].text == "این چیه"


# ── Links ─────────────────────────────────────────────────────────────────
def test_a_link_is_found_and_named_by_its_host():
    state = E.read_entities(
        [row(11, "ببین https://example.com/a/b?c=1", 900, mid=1)],
        row(33, "این لینک چیه", 1000, mid=2),
    )
    assert [(i.kind, i.detail) for i in state.items] == [(E.KIND_LINK, "example.com")]


def test_a_bare_www_link_is_found():
    state = E.read_entities(
        [row(11, "www.example.org/x", 900, mid=1)], row(33, "این چیه", 1000, mid=2)
    )
    assert [i.detail for i in state.items] == ["www.example.org"]


def test_a_word_with_a_dot_is_not_a_link():
    state = E.read_entities(
        [row(11, "فایل.txt رو بفرست", 900, mid=1)],
        row(33, "اینو بفرست", 1000, mid=2),
    )
    assert state.items == ()


# ── The anchor's own row, and what came after it ──────────────────────────
def test_the_anchor_is_not_part_of_what_it_points_at():
    """Its own media must not be offered as the thing it points at."""
    rows = [
        row(33, "اینو پاک کن", 1000, mid=2, kind="photo"),
    ]
    state = E.read_entities(rows, rows[0])
    assert state.items == ()


def test_a_message_that_arrived_after_the_anchor_is_not_prior():
    rows = [
        row(11, "ببین", 900, mid=1, kind="photo"),
        row(22, "بعدش", 1200, mid=2, kind="video"),
    ]
    state = E.read_entities(rows, row(33, "اینو پاک کن", 1000, mid=3))
    assert [i.detail for i in state.items] == ["photo"]


def test_the_newest_thing_is_marked():
    rows = [
        row(11, "ببین", 900, mid=1, kind="photo"),
        row(22, "اینم", 940, mid=2, kind="video"),
    ]
    state = E.read_entities(rows, row(33, "اینو پاک کن", 1000, mid=3))
    assert state.items[0].detail == "video" and state.items[0].newest is True
    assert state.items[1].newest is False


# ── The reply target, only when the anchor names a message ────────────────
def test_the_reply_target_is_named_when_the_anchor_says_message():
    rows = [row(11, "فایل رو فرستادم", 900, mid=1)]
    anchor = row(33, "این پیام رو پاک کن", 1000, mid=2, reply_mid=1)
    state = E.read_entities(rows, anchor)
    assert [(i.kind, i.text) for i in state.items] == [(E.KIND_MESSAGE, "فایل رو فرستادم")]


def test_the_reply_target_is_not_named_when_the_anchor_says_nothing():
    """A reply edge always has a target; printing it back on every reply would
    spend tokens on text already on screen."""
    rows = [row(11, "فایل رو فرستادم", 900, mid=1)]
    anchor = row(33, "اینو پاک کن", 1000, mid=2, reply_mid=1)
    assert E.read_entities(rows, anchor).items == ()


def test_a_reply_to_a_message_outside_the_window_names_nothing():
    anchor = row(33, "این پیام رو پاک کن", 1000, mid=2, reply_mid=99)
    assert E.read_entities([], anchor).items == ()


# ── Bounding ──────────────────────────────────────────────────────────────
def test_the_list_is_bounded():
    rows = [
        row(100 + i, "ببین", 900 + i, mid=i + 1, kind="photo") for i in range(10)
    ]
    state = E.read_entities(rows, row(33, "اینو پاک کن", 2000, mid=99), limit=3)
    assert len(state.items) == 3


# ── Rendering ─────────────────────────────────────────────────────────────
def test_the_media_block_names_the_kind_and_the_poster():
    rows = [row(11, "ببین", 900, mid=1, kind="photo")]
    out = E.render(E.read_entities(rows, row(33, "اینو پاک کن", 1000, mid=2)))
    assert "a photo by 11" in out
    assert "things, not people" in out
    assert "evidence, not a decision" in out


def test_the_block_says_the_things_are_not_people():
    """The correction the module exists to make."""
    rows = [row(11, "ببین", 900, mid=1, kind="photo")]
    out = E.render(E.read_entities(rows, row(33, "اینو پاک کن", 1000, mid=2)))
    assert "not about a person" in out


def test_the_named_class_replaces_the_not_a_person_line():
    """When the message names a file, there is nothing to warn about."""
    rows = [row(11, "ببین", 900, mid=1, kind="photo")]
    out = E.render(E.read_entities(rows, row(33, "این فایل رو بفرست", 1000, mid=2)))
    assert "names «فایل»" in out
    assert "not about a person" not in out


def test_the_block_names_a_link_by_host():
    rows = [row(11, "https://example.com/x", 900, mid=1)]
    out = E.render(E.read_entities(rows, row(33, "این لینک چیه", 1000, mid=2)))
    assert "a link to example.com" in out
    assert "names «لینک»" in out


def test_nothing_is_rendered_when_there_is_nothing_to_point_at():
    assert E.render(E.read_entities([], row(33, "سلام", 1000, mid=1))) == ""
    assert E.render(E.Entities()) == ""


def test_the_block_is_bounded():
    rows = [
        row(100 + i, "ببین " + "ب" * 80, 900 + i, mid=i + 1, kind="photo")
        for i in range(20)
    ]
    out = E.render(E.read_entities(rows, row(33, "اینو پاک کن", 2000, mid=99), limit=20))
    assert len(out) <= 600


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
    """No db, no config, no pool, no rbac — and not even ``media``."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(E))
    assert _imports(tree, top_level_only=True) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_the_shared_fold_is_borrowed_lazily_and_guarded():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(E))
    top = _imports(tree, top_level_only=True)
    lazy = _imports(tree, top_level_only=False) - top
    assert "people" in lazy
    assert lazy & top == set()


def test_the_media_kinds_are_not_a_second_copy_of_the_media_table():
    """The kind is read from the row, so the module never imports ``media``."""
    import inspect

    assert "media" not in _imports(
        __import__("ast").parse(inspect.getsource(E)), top_level_only=False
    )


def test_the_module_holds_no_path_to_an_action():
    import inspect

    source = inspect.getsource(E)
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
