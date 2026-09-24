"""Who is talking to whom, and whether this message is still the same thread.

Two of the three readings here are the room's own record and one is a heuristic,
and the file is arranged to keep that difference visible:

* the **reply graph** and the **focus** are read off the stored ``reply_user_id``
  column — the same fact ``awareness.instruction_block`` states for one message,
  generalised to the window;
* the **thread** is a reading of meaning — whether the anchor's content words
  overlap the words before it — and it carries its evidence (the shared words)
  and abstains when the anchor is too short to be about anything.

Everything here is **evidence**: nothing branches on it, nothing is authorised by
it, and the purity test at the bottom is what keeps that true as the module grows.

The interesting tests are the ones about restraint: a single reply edge is not a
convergence, a two-word message is not a topic shift, and a window with nothing
before the anchor has nothing to continue.
"""
import pytest

from app import room_state as R

# The zero-width non-joiner, spelled out so the tests read the way Persian does:
# «بچهها» is one word to a reader and two to the fold.
ZWNJ = "\u200c"


def row(user_id, text, at, *, mid=0, reply=0, role="member", name=""):
    """One window row, with the columns ``db.group_window`` returns."""
    return {
        "user_id": int(user_id),
        "text": text,
        "at": int(at),
        "message_id": int(mid),
        "reply_user_id": int(reply),
        "role": role,
        "name": name,
    }


# ── The content words ─────────────────────────────────────────────────────
def test_content_words_drop_the_function_words():
    words = R.content_tokens("فایل رو دیروز برای من فرستادم")
    assert "فایل" in words
    assert "فرستادم" in words
    assert "رو" not in words
    assert "برای" not in words
    assert "من" not in words


def test_content_words_are_deduped_and_keep_their_order():
    assert R.content_tokens("فایل مشکل داره فایل خرابه") == ("فایل", "مشکل", "خرابه")


def test_a_two_letter_content_word_is_kept():
    """«چک» is two characters and is what a message about a file is about.

    The stopword list is what removes function words; a length cutoff would drop
    this one and make «فایل رو چک کن» too short to judge.
    """
    assert R.content_tokens("فایل رو چک کن") == ("فایل", "چک")


def test_a_single_character_is_never_a_topic():
    assert R.content_tokens("ی ب") == ()


def test_the_fold_survives_the_orthographies():
    assert "فایل" in R.content_tokens("فايل")          # Arabic yeh
    assert "مشکل" in R.content_tokens("مشکل")          # the plain word


def test_an_empty_or_missing_text_has_no_content_words():
    assert R.content_tokens("") == ()
    assert R.content_tokens(None) == ()


# ── The clitic the fold split off ─────────────────────────────────────────
def test_the_plural_clitic_is_not_a_content_word():
    """«ها» is a bound morpheme, not a thing a message is about.

    The fold turns the zero-width non-joiner into a space, so «بچهها» and
    «بچه ها» both arrive as two tokens. Without this, two messages that share any
    plural noun "continue" each other, and the reason rendered to the model names
    «ها» beside the word that mattered.
    """
    assert R.content_tokens("سلام بچه ها") == ("بچه",)
    assert R.content_tokens("فایل" + ZWNJ + "ها رو چک کن") == ("فایل", "چک")


@pytest.mark.parametrize(
    "clitic",
    ["ها", "های", "هایی", "هام", "هات", "هاش", "هاشون", "هایم", "هایش"],
)
def test_every_clitic_form_is_dropped_and_the_stem_kept(clitic):
    words = R.content_tokens("کتاب" + ZWNJ + clitic)
    assert clitic not in words
    assert "کتاب" in words


def test_a_greeting_is_too_short_to_continue():
    """The defect's shape: a greeting "continued" the greeting before it.

    «سلام بچه ها» has one content word once the clitic is gone — «بچه» — which is
    below ``MIN_TOPIC_TOKENS``, so the reading abstains. That is the honest
    answer for a greeting: it is not about anything to continue.
    """
    prior = [row(11, "سلام بچه ها", 900)]
    state = R.read_state(prior, row(11, "سلام بچه ها", 1000))
    assert state.relation == R.RELATION_UNCLEAR
    assert state.shared == ()
    assert R.render_thread(state) == ""


def test_a_real_shared_word_still_continues_beside_a_clitic():
    """The fix drops the clitic, not the overlap.

    «گزارشها» and «گزارش» share the stem «گزارش», so the reading still says the
    thread continues — it just no longer claims «ها» is a shared word.
    """
    prior = [row(12, "گزارش رو فرستادم", 900)]
    state = R.read_state(prior, row(12, "گزارش" + ZWNJ + "ها آماده شد", 1000))
    assert state.relation == R.RELATION_CONTINUES
    assert state.shared == ("گزارش",)


# ── The reply graph ───────────────────────────────────────────────────────
def test_the_edges_are_read_off_the_stored_column():
    rows = [
        row(22, "الف", 920, mid=2, reply=11),
        row(55, "ب", 940, mid=3, reply=11),
    ]
    edges = R.read_state(rows, row(33, "خب", 1000, mid=4)).edges
    assert [(e.source_id, e.target_id) for e in edges] == [(22, 11), (55, 11)]


def test_the_edges_are_oldest_first():
    rows = [
        row(55, "ب", 940, mid=3, reply=11),
        row(22, "الف", 920, mid=2, reply=11),
    ]
    edges = R.read_state(rows, row(33, "خب", 1000, mid=4)).edges
    assert [e.at for e in edges] == [920, 940]


def test_a_message_that_replies_to_itself_is_not_an_edge():
    rows = [row(11, "خودم", 900, mid=1, reply=11)]
    assert R.read_state(rows, row(33, "خب", 1000, mid=2)).edges == ()


def test_the_assistants_own_reply_is_part_of_the_graph():
    """«The assistant answered X» is part of who is talking to whom."""
    rows = [row(0, "پاسخ", 950, mid=2, reply=11, role="nexus")]
    edges = R.read_state(rows, row(33, "خب", 1000, mid=3)).edges
    assert [(e.source_id, e.target_id) for e in edges] == [(0, 11)]


def test_the_anchors_own_reply_edge_is_in_the_graph():
    """The edge the pass is *about*, even when the anchor is not in the window."""
    anchor = row(33, "اینو بن کن", 1000, mid=9, reply=11)
    edges = R.read_state([], anchor).edges
    assert [(e.source_id, e.target_id) for e in edges] == [(33, 11)]


# ── The focus ─────────────────────────────────────────────────────────────
def test_the_focus_is_the_person_the_replies_converge_on():
    rows = [
        row(22, "الف", 920, mid=2, reply=11),
        row(55, "ب", 940, mid=3, reply=11),
    ]
    state = R.read_state(rows, row(33, "خب", 1000, mid=4))
    assert (state.focus_id, state.focus_count) == (11, 2)
    assert state.converged() is True


def test_one_reply_edge_is_not_a_convergence():
    """The word is withheld even though the edge is reported."""
    rows = [row(22, "الف", 920, mid=2, reply=11)]
    state = R.read_state(rows, row(33, "خب", 1000, mid=3))
    assert (state.focus_id, state.focus_count) == (11, 1)
    assert state.converged() is False
    assert "not a convergence" in R.render_graph(state)


def test_a_tie_is_broken_by_the_most_recent_reply():
    rows = [
        row(22, "الف", 920, mid=2, reply=11),
        row(55, "ب", 940, mid=3, reply=22),
    ]
    state = R.read_state(rows, row(33, "خب", 1000, mid=4))
    assert state.focus_id == 22


def test_no_replies_means_no_focus():
    state = R.read_state([row(11, "سلام", 900, mid=1)], row(33, "خب", 1000, mid=2))
    assert (state.focus_id, state.focus_count) == (0, 0)
    assert state.converged() is False


# ── The participants ──────────────────────────────────────────────────────
def test_the_participants_are_newest_first_and_deduped():
    rows = [
        row(11, "الف", 900, mid=1),
        row(22, "ب", 920, mid=2),
        row(11, "ج", 940, mid=3),
    ]
    assert R.read_state(rows, None).participants == (11, 22)


def test_the_assistant_is_not_a_participant():
    """The question is which *people* are in play."""
    rows = [
        row(11, "الف", 900, mid=1),
        row(0, "پاسخ", 950, mid=2, role="nexus"),
    ]
    assert R.read_state(rows, None).participants == (11,)


# ── The anchor's own row, taken out of "what came before" ─────────────────
def test_the_anchor_is_excluded_by_its_message_id():
    rows = [
        row(11, "قیمت دلار چنده", 900, mid=1),
        row(33, "فایل رو چک کن", 1000, mid=2),
    ]
    state = R.read_state(rows, rows[1])
    assert state.anchor_tokens == ("فایل", "چک")
    # If the anchor's own row were left in "what came before", its own words
    # would overlap themselves and every message would look like a continuation.
    assert state.shared == ()
    assert state.relation == R.RELATION_SHIFTS


def test_the_anchor_is_excluded_by_who_when_and_what_when_it_has_no_id():
    rows = [
        row(11, "قیمت دلار چنده", 900, mid=1),
        row(33, "فایل رو چک کن", 1000, mid=0),
    ]
    anchor = {"user_id": 33, "text": "فایل رو چک کن", "at": 1000, "message_id": 0}
    assert R.read_state(rows, anchor).shared == ()


def test_a_message_that_arrived_after_the_anchor_is_not_prior():
    rows = [
        row(11, "فایل رو فرستادم", 900, mid=1),
        row(22, "بعدش چی شد", 1200, mid=2),
    ]
    state = R.read_state(rows, row(33, "فایل رو چک کن", 1000, mid=3))
    assert state.relation == R.RELATION_CONTINUES
    assert state.shared == ("فایل",)


# ── The thread ────────────────────────────────────────────────────────────
def test_a_shared_word_continues_the_thread():
    rows = [row(11, "فایل مشکل داره", 900, mid=1)]
    state = R.read_state(rows, row(33, "فایل رو دوباره چک کن", 1000, mid=2))
    assert state.relation == R.RELATION_CONTINUES
    assert state.shared == ("فایل",)


def test_no_shared_word_on_a_substantive_message_is_a_shift():
    rows = [row(11, "فایل مشکل داره", 900, mid=1)]
    state = R.read_state(rows, row(33, "قیمت دلار چنده", 1000, mid=2))
    assert state.relation == R.RELATION_SHIFTS
    assert state.shared == ()


def test_a_short_message_is_not_judged():
    """«باشه» shares nothing with anything, and that is not a topic change."""
    rows = [row(11, "فایل مشکل داره", 900, mid=1)]
    state = R.read_state(rows, row(33, "باشه", 1000, mid=2))
    assert state.relation == R.RELATION_UNCLEAR


def test_a_window_with_nothing_before_has_nothing_to_continue():
    state = R.read_state([], row(33, "فایل رو چک کن", 1000, mid=1))
    assert state.relation == ""
    assert "nothing before" in state.why


def test_only_a_stopword_message_does_not_continue_anything():
    """Overlap on «این»/«که» would make every message continue every other."""
    rows = [row(11, "این که اینه", 900, mid=1)]
    state = R.read_state(rows, row(33, "این هم اینه", 1000, mid=2))
    assert state.relation == R.RELATION_UNCLEAR


# ── Rendering ─────────────────────────────────────────────────────────────
def test_the_graph_block_names_the_edges_and_the_focus():
    rows = [
        row(22, "الف", 920, mid=2, reply=11),
        row(55, "ب", 940, mid=3, reply=11),
    ]
    out = R.render_graph(R.read_state(rows, row(33, "خب", 1000, mid=4)))
    assert "22 → 11" in out
    assert "55 → 11" in out
    assert "converged on 11 (2 of 2)" in out
    assert "evidence, not a judgement" in out


def test_the_graph_block_says_when_nobody_replied():
    out = R.render_graph(R.read_state([row(11, "سلام", 900, mid=1)], None))
    assert "No reply in this window" in out


def test_the_graph_block_is_bounded():
    rows = [
        row(100 + i, "متن " + "ب" * 40, 900 + i, mid=i + 1, reply=11)
        for i in range(20)
    ]
    out = R.render_graph(R.read_state(rows, row(33, "خب", 2000, mid=99)))
    assert len(out) <= 600


def test_the_thread_block_states_the_shared_words():
    rows = [row(11, "فایل مشکل داره", 900, mid=1)]
    out = R.render_thread(R.read_state(rows, row(33, "فایل رو چک کن", 1000, mid=2)))
    assert "continues the thread" in out
    assert "«فایل»" in out
    assert "the server's reading of the words" in out


def test_the_thread_block_says_a_shift_plainly():
    rows = [row(11, "فایل مشکل داره", 900, mid=1)]
    out = R.render_thread(R.read_state(rows, row(33, "قیمت دلار چنده", 1000, mid=2)))
    assert "change of subject" in out


def test_nothing_is_rendered_when_the_thread_cannot_be_judged():
    """An abstention is silent — a line saying "unclear" would spend tokens to
    tell the model what it can already see."""
    rows = [row(11, "فایل مشکل داره", 900, mid=1)]
    assert R.render_thread(R.read_state(rows, row(33, "باشه", 1000, mid=2))) == ""
    assert R.render_thread(R.read_state([], row(33, "خب", 1000, mid=1))) == ""


def test_nothing_is_rendered_for_an_empty_room():
    assert R.render_graph(R.RoomState()) == ""
    assert R.render_thread(R.RoomState()) == ""


# ── The Arabic block's punctuation is not part of the word ────────────────
# «؟» «،» «؛» live inside \u0600-\u06ff, so «شده؟» was not the stopword «شده» and
# two messages that differed only by a question mark shared no content word.
@pytest.mark.parametrize(
    "with_mark,without",
    [
        ("چی شده؟", "چی شده"),
        ("قیمت چنده؟", "قیمت چنده"),
        ("نتیجه چیه،", "نتیجه چیه"),
        ("فایل رو دیدی؛", "فایل رو دیدی"),
    ],
)
def test_a_mark_does_not_change_the_content_words(with_mark, without):
    assert R.content_tokens(with_mark) == R.content_tokens(without)


def test_a_marked_word_still_continues_the_thread():
    """The one shared content word carries the question mark.

    Before the fix «چنده؟» was not «چنده», the two messages shared nothing, and
    the thread read as a topic shift.
    """
    window = [row(11, "قیمت چنده", 900, mid=1, name="سارا")]
    anchor = row(22, "چنده؟ گرون شده", 1000, mid=2, name="رضا")
    state = R.read_state(window, anchor)
    assert state.relation == R.RELATION_CONTINUES
    assert "چنده" in state.shared


def test_a_thing_word_with_a_mark_is_still_content():
    """And the mark does not turn a thing noun into a different word."""
    assert R.content_tokens("این لینک؟") == R.content_tokens("این لینک")


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
    """No db, no config, no pool, no rbac at import — it is evidence about a room."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    assert _imports(tree, top_level_only=True) <= {
        "__future__", "re", "unicodedata", "dataclasses"
    }


def test_the_shared_fold_is_borrowed_lazily_and_guarded():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    top = _imports(tree, top_level_only=True)
    lazy = _imports(tree, top_level_only=False) - top
    assert "people" in lazy
    assert lazy & top == set()


def test_the_module_holds_no_path_to_an_action():
    import inspect

    source = inspect.getsource(R)
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
