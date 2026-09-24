"""The floor under the deterministic-understanding benchmark.

``tools/eval_intent.py`` measures; this holds the measurement to a standard, so a
change that quietly costs understanding fails a test instead of being noticed
months later. The corpus is fixed, so the deterministic numbers are exact: a
deliberate change to the lexicon or the resolver is expected to move them, and
the right response is to update the corpus on purpose rather than to loosen the
floor by reflex.

The one addressing case left as a gap is asserted as a gap. It is a real miss —
an exact name mid-sentence in a message that is talking *about* Nexus — and it is
left because every deterministic rule that catches it also demotes a real
request with the name in the same position. Pinning it here means the day it is
fixed, this test fails and says so.
"""
import importlib.util
import sys
from pathlib import Path

from app import discourse, entities, objects, referents, room_state, temporal

ROOT = Path(__file__).resolve().parent.parent

# The harness lives in tools/ and is a script, not a package module.
_spec = importlib.util.spec_from_file_location(
    "eval_intent", ROOT / "tools" / "eval_intent.py"
)
eval_intent = importlib.util.module_from_spec(_spec)
sys.modules["eval_intent"] = eval_intent
_spec.loader.exec_module(eval_intent)

# The addressing case the matcher used to get wrong: an exact name in the middle
# of a sentence talking *about* Nexus rather than to it — «من با نکسوس کار
# نکردم». It is empty now: a name immediately after a preposition is the object
# of that preposition, so the message is about the assistant and the strong grade
# no longer reads it as a call. The set is kept rather than deleted, because the
# assertion below is what catches the next gap — and what catches a *new* one
# appearing in a case that had been passing.
KNOWN_ADDRESSING_GAPS: set[str] = set()


def result():
    return eval_intent.evaluate(eval_intent.load_cases())


def test_the_corpus_is_well_formed():
    cases = eval_intent.load_cases()
    assert len(cases["cases"]) >= 30
    ids = [case["id"] for case in cases["cases"]]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in cases["cases"]:
        assert "window" in case and "anchor" in case and "expect" in case
        assert "text" in case["anchor"]
        # Every case carries an act label, including the abstentions — a case
        # with no label cannot be scored, and a default would hide the ones the
        # reader is expected to abstain on.
        assert "act" in case["expect"], case["id"]
        assert "open_questions" in case["expect"], case["id"]
        # …and a time label, for the same reason: an absent field would make
        # "no time word here" indistinguishable from "not yet labelled".
        assert "when" in case["expect"], case["id"]
        assert "when_unit" in case["expect"], case["id"]
        # A state label must carry the graph; `relation` is optional, because a
        # case that only records a reply row is not a case about the thread, and
        # labelling one it did not judge would score a guess.
        if "state" in case["expect"]:
            keys = set(case["expect"]["state"])
            assert {"edges", "focus"} <= keys, case["id"]
            assert keys <= {"edges", "focus", "relation"}, case["id"]
        # An entities label must carry the two facts read off the rows; `named`
        # is optional, because the class the words name is a reading and only
        # the cases that judge it should be scored on it.
        assert "entities" in case["expect"], case["id"]
        ent_keys = set(case["expect"]["entities"])
        assert {"newest_media", "has_link"} <= ent_keys, case["id"]
        assert ent_keys <= {"newest_media", "has_link", "named"}, case["id"]
        # A direction label carries all three parts, because they are one
        # reading: a label with the polarity missing would score an abstention
        # as a pass. Optional, because a case about the thread is not a case
        # about the directive.
        if "request" in case["expect"]:
            assert set(case["expect"]["request"]) == {
                "directive",
                "polarity",
                "manner",
            }, case["id"]
        # …and an object label carries both halves, for the same reason: a label
        # with the source missing would score an abstention as a pass.
        if "object" in case["expect"]:
            assert set(case["expect"]["object"]) == {"kind", "source"}, case["id"]


def test_expression_detection_is_exact_on_the_corpus():
    m = result()
    assert m["expression_accuracy"] == 1.0


def test_referent_resolution_finds_the_right_person_every_time():
    m = result()
    assert m["resolution_top1_accuracy"] == 1.0


def test_it_never_guesses_when_the_room_is_ambiguous():
    """The property the whole design rests on: no wrong-person certainty.

    The labelled ambiguity set is all person-directed requests — a role two
    people hold, a split room, three recent speakers, «قبلیش». A request that
    acts on a *thing* is deliberately not in it: there is no person question
    there, so a tied pair of speakers is not an ambiguity to report. Those cases
    carry ``ambiguous: false`` and are held by the object floors below.
    """
    m = result()
    assert m["wrong_confident"] == 0
    assert m["ambiguity_recall"] == 1.0


def test_it_does_not_cry_ambiguity_when_the_room_has_settled():
    """Precision matters too: a needless "I cannot tell" is a wasted round trip.

    The anaphoric reading is what earns this — «همون کاربر» in a room whose
    replies have all been aimed at one person is not ambiguous, it is answered.
    """
    m = result()
    assert m["ambiguity_precision"] == 1.0


def test_the_resolver_is_a_strict_improvement_over_the_reply_edge():
    """The claim, as a fraction: the reply edge alone versus the resolver."""
    m = result()
    assert m["provided_before"] < m["provided_after"]
    assert m["provided_after"] == 1.0


def test_the_block_the_model_is_shown_stays_small():
    m = result()
    assert m["block_chars_max"] <= 900


def test_the_resolver_is_fast_enough_to_run_on_every_pass():
    """Pure Python, no query: the mean must stay far under a millisecond."""
    m = result()
    assert m["us_mean"] < 3000


def test_the_addressing_gaps_are_exactly_the_ones_we_know_about():
    """A gap that closes, or a new one, must fail here and be looked at."""
    detail = result()["detail"]
    misses = {r["id"] for r in detail if not r["addressed_ok"]}
    assert misses == KNOWN_ADDRESSING_GAPS, misses


# ── The act ───────────────────────────────────────────────────────────────
def test_the_act_reader_never_claims_an_act_it_cannot_defend():
    """The property the precedence and the abstention exist for.

    A wrong act in the prompt is worse than no act, so the floor is on the
    claimed direction: everything it says, it is right about.
    """
    m = result()
    assert m["act_false_positives"] == 0
    assert m["act_claimed_precision"] == 1.0


def test_the_act_reader_finds_every_labelled_act():
    m = result()
    assert m["act_false_negatives"] == 0
    assert m["act_recall"] == 1.0


def test_the_act_reader_still_abstains_rather_than_guessing():
    """Coverage is high, not total — the difference is the honest part.

    The abstentions are the implicit complaint, the plain statement and the
    two empty anchors: messages whose act is a matter of meaning. If this ever
    reaches 100%, something started guessing.

    The floor moved from 0.85 to 0.75 when the temporal slice was added, and the
    move is a statement about the corpus rather than about the reader: sixteen
    messages were added, and nine of them are plain statements that place
    themselves in time («امروز هوا خیلی گرمه») — whose act *is* a matter of
    meaning, exactly the kind of message this reader is built to abstain on. The
    load-bearing floors are untouched: claimed precision is still 1.0 and the
    false-positive count is still 0.
    """
    m = result()
    assert 0 < m["act_abstentions"] < m["cases"]
    assert m["act_coverage"] >= 0.75


def test_every_act_class_is_exercised():
    """A corpus that only tested instructions would score a constant well."""
    by_class = result()["act_by_class"]
    for kind, counts in by_class.items():
        if kind == "unknown":
            continue
        assert counts["total"] >= 2, f"{kind} has only {counts['total']} cases"


# ── The room's open questions ─────────────────────────────────────────────
def test_the_open_questions_are_exact():
    m = result()
    assert m["questions_precision"] == 1.0
    assert m["questions_recall"] == 1.0
    assert m["questions_exact"] == m["questions_cases"]
    assert m["questions_cases"] >= 3


def test_the_question_block_stays_small():
    assert result()["questions_block_chars_max"] <= 600


# ── The time words ────────────────────────────────────────────────────────
def test_the_time_reader_never_claims_a_time_nobody_stated():
    """The dangerous direction: a time placed in the prompt from words that
    are not there. Everything it claims, it is right about."""
    m = result()
    assert m["when_false_positives"] == 0
    assert m["when_claimed_precision"] == 1.0


def test_the_time_reader_finds_every_labelled_time_word():
    m = result()
    assert m["when_false_negatives"] == 0
    assert m["when_recall"] == 1.0


def test_the_time_reader_abstains_when_there_is_no_time_word():
    """An empty reading is the common case and the cheap one.

    If coverage ever reaches 100% the reader is claiming a time on messages that
    state none, which is the false-positive direction above.
    """
    m = result()
    assert 0 < m["when_coverage"] < 1.0
    assert m["when_by_kind"][""]["total"] >= 40


def test_every_time_kind_is_exercised():
    by_kind = result()["when_by_kind"]
    for kind in temporal.WHENS:
        assert by_kind[kind]["total"] >= 1, f"{kind} has no cases"


def test_the_time_block_stays_small():
    assert result()["when_block_chars_max"] <= 300


def test_the_time_sentence_never_contradicts_itself():
    """The sentence is the product, and it was the one thing never scored.

    ``when_ok`` compares the structured ``kind`` and ``unit``, so «فردا» scored
    100% while the line handed to the model said *"points forwards, after now at
    a scale of days — about 1 day(s) ago"*. The reading was right and the
    sentence was wrong, and nothing looked at the sentence. This is the check
    that would have caught it: a direction stated in words and an offset stated
    in words, both from one reading, must not point opposite ways.
    """
    m = result()
    assert m["when_prose_contradictions"] == 0


def test_the_time_sentence_check_is_not_vacuous():
    """…and the check above has something to check.

    A contradiction count of zero is also what a renderer that says nothing
    produces, so the future cases — the ones the defect hit — are held to the
    forward wording directly, and the past cases to the backward one.
    """
    m = result()
    assert m["when_prose_cases"] >= 20
    future = [r for r in m["detail"] if r["expected_when"] == "future" and r["when_prose"]]
    assert len(future) >= 4, "no future reading renders a sentence"
    for r in future:
        head = r["when_prose"].split("The window this pass is reading")[0]
        assert "forwards" in head, r["id"]
    forward = [r for r in future if "from now" in r["when_prose"]]
    assert len(forward) >= 4, "no future reading states a forward offset"
    past = [r for r in m["detail"] if r["expected_when"] == "past" and r["when_prose"]]
    assert any("ago" in r["when_prose"] for r in past)


def test_the_entity_block_never_claims_a_pointer_the_message_lacks():
    """The block's header is a claim, and it must be backed.

    The header says the message *may point at* the things under it, so it may
    only appear for a message that points at something. Nothing scored the
    rendered block before this: ``named_ok`` compares the class the message
    names, which is a different fact — the reader could be right while the prompt
    told the model a greeting had things to point at.
    """
    m = result()
    assert m["entity_claims_a_pointer_cases"] == 0


def test_the_entity_block_never_offers_things_for_a_person():
    """…and it must not contradict the block printed beside it.

    The closing line says "do not act on a person unless the message names one".
    Next to the object block's "acts on a **person**" that is the opposite claim,
    and the model has to choose which to believe.
    """
    m = result()
    assert m["entity_offers_things_for_a_person_cases"] == 0


def test_the_entity_block_check_is_not_vacuous():
    """A zero is also what a block that never renders produces.

    Measured against the renderer before the fix, these two counts were **11** and
    **3**; splitting the two halves of the fix, the reader's widened pointer
    accounts for five of the eleven and the rendering gate for the other six.
    """
    m = result()
    assert m["entity_pointer_header_cases"] >= 15, "no block offers a pointer at all"
    assert m["entity_items_offered_total"] >= 10, "the guard suppressed everything"
    assert m["entity_items_found_total"] >= m["entity_items_offered_total"]


def test_the_entity_block_states_evidence_and_never_an_order():
    """The block's contract, in its own docstring, is evidence.

    It closed with "do not act on a person unless the message names one" — an
    order, printed under the resolver's ranked people, telling the model to
    disregard the block above it. The order belongs to the block that knows the
    side; the object line states it when the verb decides and is silent when the
    verb is unclassified, which is exactly when the resolver's people are live.
    """
    m = result()
    assert m["entity_gives_an_order_cases"] == 0


def test_the_entity_order_check_is_not_vacuous():
    """Nine cases render the line the check is about."""
    m = result()
    assert m["entity_items_offered_total"] >= 10, "no block renders the closing line"


# ── The thread's content words ────────────────────────────────────────────
def test_the_thread_never_claims_a_clitic_as_a_content_word():
    """The thread reading names its evidence, and the evidence can be wrong.

    The verdict was scored; the words it named as the overlap never were. The
    ZWNJ fold splits a plural clitic off its noun («بچهها» → «بچه ها»), so «ها»
    arrived as a content word — two messages that share any plural noun
    "continued" each other, and the reason rendered to the model named «ها»
    beside the word that mattered.
    """
    m = result()
    assert m["anchor_clitic_cases"] == 0
    assert m["thread_shared_clitic_cases"] == 0


def test_the_clitic_check_is_not_vacuous():
    """The corpus contains the defect's shape, so the zero above is a real pass.

    Two anchors fold to a bare clitic token — «سلام بچه ها» → «بچه», «ها» — and
    one of them reached a ``continues`` verdict on it before the fix. The scan
    below is over the *folded tokens*, not over ``content_tokens``, so it stays
    true after the fix: the shape is still in the corpus, and only the reading
    changed. The relation is still scored, and the case the fix moved is now
    labelled for it.
    """
    clitic = eval_intent._CLITIC_FORMS
    shaped = [
        case["id"]
        for case in eval_intent.load_cases()["cases"]
        if set(room_state._tokens(case["anchor"]["text"])) & clitic
    ]
    assert len(shaped) >= 2, shaped
    m = result()
    assert m["state_cases"] >= 13, "the relation stopped being scored"


# ── The expression, and the wrong lead ────────────────────────────────────
def test_the_resolver_never_offers_a_person_for_a_thing_word():
    """A false positive here is a wrong lead — the worst mistake available.

    The corpus says a message points at no person — a demonstrative bound to a
    config, a link, a time — and the resolver offered one anyway. §46 fixed it
    for the room-held nouns («این لینک»); §56 for the domain's own («همون
    کانفیگ»).
    """
    m = result()
    assert m["expression_false_positives"] == 0


def test_the_expression_check_is_not_vacuous():
    """The corpus carries the shape, and most cases point at a person.

    Measured against the resolver before the fix, ``expression_false_positives``
    was 3 — exactly the three ``thing-noun-*`` cases — and accuracy 97.7%. The
    false *negative* is the safe direction and is counted apart.
    """
    m = result()
    assert m["expression_cases"] >= 55, "no case points at a person"
    leads = [r["id"] for r in m["detail"] if r["id"].startswith("thing-noun-")]
    assert len(leads) >= 3, leads


# ── The copula, and the order it invented ─────────────────────────────────
def test_the_act_never_quotes_a_copula_directive():
    """A directive ending in the copula «ه» is an order the reader invented.

    «ه» ends a *predicate*, so peeling it turned questions into orders:
    «ادمینه کیه؟» reached the prompt as "the directive «ادمینه»", «چرا ساکته؟»
    as "the directive «ساکته»", and the subjunctive «کنه» as the imperative
    «کن». Seven cases quoted one; now none do.
    """
    m = result()
    assert m["act_copula_directives"] == 0


def test_the_copula_check_is_not_vacuous():
    """The corpus carries the shape, and the unfixed reader quotes it.

    Re-adding «ه» to the reader's clitic list brings the defect back: seven
    cases then quote a copula as the directive and act accuracy falls below
    1.0. The scan below is over the corpus *text*, so it stays true after the
    fix — the shape is still there, only the reading changed.
    """
    shaped = [
        case["id"]
        for case in eval_intent.load_cases()["cases"]
        if any(
            eval_intent._copula_directive(t)
            for t in discourse._tokens(case["anchor"]["text"])
        )
    ]
    assert len(shaped) >= 3, shaped
    original = discourse._CLITICS
    try:
        discourse._CLITICS = tuple(original) + ("ه",)
        unfixed = eval_intent.evaluate(eval_intent.load_cases())
    finally:
        discourse._CLITICS = original
    assert unfixed["act_copula_directives"] >= 3
    assert unfixed["act_accuracy"] < 1.0


# ── The reading the prompt renders, not a cheaper one ─────────────────────
def test_the_scored_verdict_is_the_one_the_block_renders():
    """The harness must score the resolution the renderer renders.

    It used to resolve with the window *without* the anchor and with no roles,
    while ``_render_referent_candidates`` resolved with the anchor in the window
    and the pass's roles. The two disagreed on five cases, and the prompt's
    reading is the one the model acts on — so the block the model reads and the
    verdict the benchmark scores have to be the same object.
    """
    m = result()
    for r in m["detail"]:
        prose = r["referents_prose"]
        if not prose:
            continue
        if r["got_confident"]:
            assert "is confident in the first" in prose, r["id"]
        elif r["got_ambiguous"]:
            assert "could not tell the top" in prose, r["id"]


def test_the_referent_block_check_is_not_vacuous():
    """The corpus carries the shape: a role expression with a role-holding
    speaker, and the ambiguity the prompt must not over-report."""
    cases = eval_intent.load_cases()["cases"]
    shaped = [
        c["id"]
        for c in cases
        if c["expect"]["expression_kind"] == "role"
        and any(
            str(r.get("role") or "") in ("owner", "admin")
            for r in [*(c.get("window") or ()), c["anchor"]]
        )
    ]
    assert len(shaped) >= 3, shaped
    m = result()
    assert m["ambiguity_precision"] == 1.0
    assert m["wrong_confident"] == 0


# ── The object line, scored against the block printed beside it ───────────
def test_the_object_line_never_denies_a_person_the_block_names():
    """A request can act on a thing *and* carry an explicit source for a person.

    A reply edge, a stated id and a name are facts, and ``referents`` keeps them
    for exactly this case — they identify who the thing belongs to. So the object
    line must not order the model to ignore the block beside it. The corpus
    carries the shape once per explicit source.
    """
    m = result()
    assert m["object_denies_a_person_cases"] == 0
    shaped = [
        r["id"]
        for r in m["detail"]
        if r["got_object_kind"] not in ("", objects.CLASS_PERSON)
        and r["referents_prose"].startswith("Who ")
    ]
    assert len(shaped) >= 3, shaped


def test_the_object_denial_check_is_not_vacuous():
    """Putting the order back brings the defect back.

    The check reads the two rendered blocks, so it stays true after the fix — the
    shape is still in the corpus and only the sentence changed. This reconstructs
    the unfixed line by string, which is the point: the check is on the prose the
    model reads, not on the reader's internals.
    """
    original = objects.render
    try:
        objects.render = lambda state: original(state).replace(
            "The thing is not a member of the room.",
            "Do not read it as aimed at anybody in the room.",
        )
        unfixed = eval_intent.evaluate(eval_intent.load_cases())
    finally:
        objects.render = original
    assert unfixed["object_denies_a_person_cases"] >= 3


# ── The graph's focus sentence, scored against the edges ──────────────────
def test_the_graph_never_says_the_room_converged_on_one_member():
    """The word "converged" is a claim about the room, not about the edge count.

    ``anaphoric-split-room`` is one member replying twice to each of two people,
    and the corpus's own note calls that room "split". The sentence still said
    the room had "converged on 22", because the focus held two *edges* — both
    from the same member. The check re-derives the claim from the rendered id
    and the raw edges, so it cannot pass by agreeing with ``converged()``.
    """
    m = result()
    assert m["graph_claims_convergence_cases"] == 0
    split = [
        r["id"]
        for r in m["detail"]
        if "all from one member" in r["graph_prose"]
    ]
    assert split, "the single-member split shape left the corpus"


def test_the_graph_convergence_check_is_not_vacuous():
    """Putting the edge-count gate back brings the overclaim back.

    The check reads the rendered sentence, so it stays true after the fix. This
    reconstructs the unfixed gate on the reader, which is the point: the check is
    on the prose the model reads, not on the reader's internals.
    """
    original = room_state.RoomState.converged
    try:
        room_state.RoomState.converged = lambda self: self.focus_count >= 2
        unfixed = eval_intent.evaluate(eval_intent.load_cases())
    finally:
        room_state.RoomState.converged = original
    assert unfixed["graph_claims_convergence_cases"] >= 1


# ── The act sentence's evidence word ──────────────────────────────────────
def test_the_act_sentence_quotes_a_word_the_message_holds():
    """The parenthetical names a token of the message, or nothing.

    R's baseline read all 112 rendered act sentences: the template reports
    ``why[0]`` verbatim, and ``read_act`` always takes its evidence word from
    the message's own tokens. No defect was found — the floor is kept so a
    reader that interpolates a word the message never held fails here rather
    than reaching the prompt.
    """
    m = result()
    assert m["act_quote_not_in_anchor_cases"] == 0
    quoting = [r["id"] for r in m["detail"] if "«" in r["act_why"]]
    assert len(quoting) >= 50, len(quoting)


def test_the_act_quote_check_is_not_vacuous():
    """A sentence quoting a word the message never held is caught."""
    original = discourse.render_act
    try:
        discourse.render_act = lambda act: (
            original(act).replace(act.why[0], "the directive «zzz»")
            if act and act.why
            else original(act)
        )
        broken = eval_intent.evaluate(eval_intent.load_cases())
    finally:
        discourse.render_act = original
    assert broken["act_quote_not_in_anchor_cases"] >= 1


# ── The role scenario (increment S) ───────────────────────────────────────
def test_a_role_word_with_two_holders_is_never_confident():
    """«ادمینه» does not name *which* administrator, so it is not certainty.

    Two holders of the role, the room's replies converged on one of them, and a
    holder who just spoke are three different shapes — all three must read
    ``ambiguous``. Choosing one would be the "sure, and wrong" the resolver
    exists to withhold, and it would be choosing an admin *because* they are an
    admin.
    """
    m = result()
    assert m["role_two_admin_confident_cases"] == 0
    shaped = [
        r["id"]
        for r in m["detail"]
        if r["role_holders"] >= 2 and r["expected_kind"] == referents.KIND_ROLE
    ]
    assert len(shaped) >= 3, shaped


def test_the_two_holder_certainty_check_is_not_vacuous():
    """Removing both confidence guards trips the metric on the tied admins.

    The two-holder shape is held apart by two guards, not one: the top must
    clear ``CONFIDENT_MIN`` *and* lead the runner-up by ``MARGIN``. Two admins
    at the same score fail the second even when the first is removed, so the
    non-vacuity patch must drop both — otherwise the metric could read zero
    for the wrong reason and the check would prove nothing.
    """
    floor, margin = referents.CONFIDENT_MIN, referents.MARGIN
    try:
        referents.CONFIDENT_MIN = 0.0
        referents.MARGIN = -1.0
        broken = eval_intent.evaluate(eval_intent.load_cases())
    finally:
        referents.CONFIDENT_MIN = floor
        referents.MARGIN = margin
    assert broken["role_two_admin_confident_cases"] >= 1


def test_the_role_signal_never_uses_the_anaphoric_focus():
    """Role and convergence are different mechanisms and must stay so.

    The room's reply convergence (``_about_focus``) settles an *anaphoric* word
    — «همون»/«اون», the object clitic — by what the room has been replying to.
    A role word names a role, not the room's topic, so the focus reason must
    never appear in a role candidate's evidence. The check reads the evidence
    list, not the verdict, so a right-looking answer by the wrong mechanism
    still fails.
    """
    m = result()
    assert m["role_focus_used_cases"] == 0
    # Non-vacuous: the same reason DOES appear on an anaphoric case, so the
    # check is not "the string is never built".
    anaphoric = [
        r["id"]
        for r in m["detail"]
        if r["expected_kind"] != referents.KIND_ROLE and r["role_focus_reason"]
    ]
    assert anaphoric, "no case carries the focus reason at all — vacuous check"


def test_the_role_focus_check_is_not_vacuous():
    """Removing the anaphoric gate lets a role tie run convergence — caught."""
    original = referents.Expression.anaphoric
    try:
        referents.Expression.anaphoric = lambda self: True
        broken = eval_intent.evaluate(eval_intent.load_cases())
    finally:
        referents.Expression.anaphoric = original
    assert broken["role_focus_used_cases"] >= 1


# ── The reading the addressed conversation borrows (increment T) ──────────
def test_the_addressed_reading_is_a_bounded_subset_of_the_pass_reading():
    """It is drawn from the same blocks, so it can never be larger.

    The addressed path borrows the pass's reading minus what the chat prompt
    already states and minus the database-backed room memory. If it ever
    exceeded the pass reading, it would be rendering something the pass did not
    — a second reading rather than the same one.
    """
    m = result()
    assert m["conversation_reading_cases"] == m["context_cases"]
    assert m["conversation_reading_over_reading_cases"] == 0
    assert m["conversation_reading_over_ceiling_cases"] == 0
    assert m["conversation_reading_chars_mean"] < m["context_chars_mean"]
    assert m["conversation_reading_chars_max"] <= m["context_chars_max"]


def test_the_addressed_reading_carries_the_resolver_where_it_matters():
    """The part that answers "who does this mean" must reach the conversation.

    Non-vacuity: the count is over the corpus, and at least one case renders the
    resolver's ranked candidates into the borrowed reading — so the check is not
    "the string is never built".
    """
    m = result()
    assert m["conversation_reading_resolves_cases"] >= 1


# ── The assembled context ─────────────────────────────────────────────────
def test_the_context_is_assembled_for_every_case():
    m = result()
    assert m["context_cases"] == m["cases"]


def test_every_window_source_renders_in_the_benchmark():
    """A source that never renders is a block the model never sees.

    Two of them were dead in the whole corpus and nothing said so. The referent
    candidates were the worse of the two: ``_wants_referents`` asks
    ``is_authority``, which reads ``rbac``, and the harness had never given its
    world an owner — so the block that carries person resolution to the model
    read 0 of 127, including the 80 cases whose anchor the corpus labels an
    owner. The database-backed sources are excluded by name, and the assertion
    below is what keeps a new source from being added and forgotten.
    """
    m = result()
    dead = [
        name
        for name in m["context_source_names"]
        if name not in m["context_sources_rendered"]
        and name not in eval_intent._CONTEXT_DB_BACKED
    ]
    assert dead == [], f"these sources never rendered: {dead}"
    # …and the exclusion set is exactly the database-backed sources, so a new
    # source has to be classified before it can be dead.
    assert set(m["context_source_names"]) == (
        set(m["context_sources_rendered"]) | set(m["context_sources_dead"])
    )


def test_the_person_candidates_reach_the_model():
    """The block the whole resolver exists for, asserted by name.

    It rendered on no case at all before the harness was given an owner, so every
    referent number in this file was scored on the resolver's return value and
    never on the block.
    """
    m = result()
    assert m["context_sources"]["referent_candidates"] >= 1


def test_the_context_stays_within_its_ceiling():
    m = result()
    assert m["context_chars_max"] <= m["context_ceiling"]
    assert m["context_chars_mean"] < m["context_ceiling"]


def test_the_time_reader_is_fast_enough_to_run_on_every_pass():
    """The folded table is cached, so a read is a substring scan and no more."""
    assert result()["when_us_mean"] < 1000


# ── The room's state ──────────────────────────────────────────────────────
def test_every_reply_edge_is_read_exactly():
    """The graph is a stored column, so the reading must be exact, not close."""
    m = result()
    assert m["edges_exact"] == m["cases"]
    assert m["edges_precision"] == 1.0
    assert m["edges_recall"] == 1.0
    assert m["edges_false_positives"] == 0
    assert m["edges_false_negatives"] == 0


def test_the_focus_is_always_right():
    assert result()["focus_accuracy"] == 1.0


def test_the_thread_reading_is_exact_on_the_labelled_cases():
    m = result()
    assert m["state_cases"] >= 10
    assert m["relation_correct"] == m["state_cases"]
    assert m["relation_accuracy"] == 1.0


def test_every_relation_kind_is_exercised():
    """Including the empty reading: a window with nothing before the anchor."""
    by_kind = result()["relation_by_kind"]
    for kind in room_state.RELATIONS:
        assert by_kind[kind]["total"] >= 1, f"{kind} has no cases"
    assert by_kind[""]["total"] >= 1


def test_the_room_state_blocks_stay_small():
    m = result()
    assert m["graph_chars_max"] <= 600
    assert m["thread_chars_max"] <= 500


def test_the_corpus_labels_every_reply_row_it_contains():
    """A case whose rows carry a reply id must carry a graph label.

    The graph is a fact read off a stored column, so an unlabelled reply row
    shows up in the report as a false positive and reads as a bug in the reader.
    This makes the gap fail as the labelling gap it actually is.
    """
    for case in eval_intent.load_cases()["cases"]:
        rows = list(case.get("window") or []) + [case["anchor"]]
        has_reply = any(
            int(r.get("reply_user_id") or 0)
            and int(r.get("reply_user_id") or 0) != int(r.get("user_id") or 0)
            for r in rows
        )
        if has_reply:
            assert "state" in case["expect"], case["id"]
            assert case["expect"]["state"]["edges"], case["id"]


# ── The things a demonstrative may point at ───────────────────────────────
def test_the_newest_media_and_the_link_are_read_exactly():
    """Both are facts — a stored column and a regex over the text."""
    m = result()
    assert m["media_exact"] == m["cases"]
    assert m["link_exact"] == m["cases"]


def test_every_named_thing_class_is_read():
    m = result()
    assert m["named_cases"] >= 8
    assert m["named_correct"] == m["named_cases"]
    assert m["named_accuracy"] == 1.0


def test_every_thing_kind_is_exercised():
    """A class with no case is a class the reader was never tested on."""
    labelled = {
        case["expect"]["entities"]["named"]
        for case in eval_intent.load_cases()["cases"]
        if "named" in case["expect"]["entities"]
    }
    for kind in entities.KINDS:
        assert kind in labelled, f"{kind} has no case"


def test_the_entity_block_stays_small():
    assert result()["entity_block_chars_max"] <= 600


def test_the_entity_reader_is_fast_enough_to_run_on_every_pass():
    """It walks the window once and folds a handful of tokens — no query."""
    assert result()["entity_us_mean"] < 1000


def test_the_corpus_labels_every_media_or_link_row_it_contains():
    """The mirror of the reply-row test, for the same reason.

    A media row or a link in the window without an entities label reads in the
    report as a false positive in the reader rather than as a missing label.
    Only the *window* is checked, because that is what the reader reads: the
    thing a demonstrative points at is what the room already has, so a link in
    the anchor itself is deliberately not an entity.
    """
    for case in eval_intent.load_cases()["cases"]:
        for row in case.get("window") or []:
            labelled = case["expect"]["entities"]
            if str(row.get("kind") or "").strip() or "http" in str(row.get("text") or ""):
                assert labelled["newest_media"] or labelled["has_link"], case["id"]


# ── The Arabic block's punctuation ────────────────────────────────────────
def test_a_trailing_mark_never_changes_a_reading():
    """«؟» «،» «؛» are separators, not part of the word before them.

    Each of these cases was wrong before the tokenizer fix, and in the direction
    that matters: a thing noun at the end of a question went unrecognized, so the
    person resolver offered the room's members for a message about a link.
    """
    m = result()
    detail = {r["id"]: r for r in m["detail"]}
    ids = sorted(
        c["id"]
        for c in eval_intent.load_cases()["cases"]
        if c.get("category") == "punctuation"
    )
    assert len(ids) >= 6, ids
    for case_id in ids:
        r = detail[case_id]
        assert r["kind_ok"], case_id
        assert r["act_ok"], case_id
        assert r["edges_ok"] and r["focus_ok"], case_id
        assert r["media_ok"] and r["link_ok"], case_id
        assert r["when_ok"], case_id
        if r["has_named_label"]:
            assert r["named_ok"], case_id
        if r["has_relation_label"]:
            assert r["relation_ok"], case_id
        if r["has_request_label"]:
            assert r["request_ok"], case_id
        if r["has_object_label"]:
            assert r["object_ok"], case_id


# ── The directive's direction ─────────────────────────────────────────────
def test_the_direction_reader_is_exact_on_the_labelled_cases():
    m = result()
    assert m["request_cases"] >= 16
    assert m["request_correct"] == m["request_cases"]
    assert m["request_accuracy"] == 1.0


def test_the_direction_reader_never_reads_a_forbidden_action_as_asked_for():
    """The floor the whole increment exists for.

    ``app/discourse.py`` reads «بنش کن» and «بنش نکن» identically — both are
    ``instruction`` with the directive «بنش». Before this reader the transcript
    said the room was asking for a ban in both cases, and the second one is the
    message where the room is protecting somebody. This number is that mistake,
    counted. It is zero, and it must stay zero.
    """
    m = result()
    assert m["request_negated_cases"] >= 6
    assert m["request_false_affirmative"] == 0


def test_every_negated_directive_is_found():
    m = result()
    assert m["request_negated_recall"] == 1.0
    assert m["request_polarity_accuracy"] == 1.0


def test_the_direction_reader_abstains_rather_than_guessing():
    """A negation the reader cannot scope is not an affirmative.

    Scored on the polarity cases the corpus labels ``""``: the negation is in the
    message but not attached to the directive, so the honest reading is silence.
    A third such case lives in the ``correction`` category, where the negation
    «نه» is the correction marker itself.
    """
    m = result()
    detail = {r["id"]: r for r in m["detail"]}
    unscoped = [
        c["id"]
        for c in eval_intent.load_cases()["cases"]
        if c.get("category") == "polarity"
        and (c["expect"].get("request") or {}).get("polarity") == ""
    ]
    assert len(unscoped) >= 2, unscoped
    for case_id in unscoped:
        assert detail[case_id]["got_polarity"] == "", case_id
        assert detail[case_id]["request_ok"], case_id


def test_every_polarity_case_is_exact():
    """The cases the increment added, scored on every column they label."""
    m = result()
    detail = {r["id"]: r for r in m["detail"]}
    ids = sorted(
        c["id"]
        for c in eval_intent.load_cases()["cases"]
        if c.get("category") == "polarity"
    )
    assert len(ids) >= 11, ids
    for case_id in ids:
        r = detail[case_id]
        assert r["request_ok"], case_id
        assert r["kind_ok"] and r["act_ok"] and r["when_ok"], case_id
        assert r["edges_ok"] and r["focus_ok"], case_id
        assert r["media_ok"] and r["link_ok"], case_id
        if r["has_named_label"]:
            assert r["named_ok"], case_id


def test_the_direction_block_stays_small():
    """The act line and the polarity line share one source, budget 320."""
    assert result()["request_chars_max"] <= 320


def test_the_direction_reader_is_fast_enough_to_run_on_every_pass():
    """It tokenizes the anchor once and borrows the act lexicon — no query."""
    assert result()["request_us_mean"] < 1000


def test_every_direction_is_exercised():
    """A corpus that only holds affirmatives would score a constant at 1.0."""
    polarities = {
        (c["expect"].get("request") or {}).get("polarity")
        for c in eval_intent.load_cases()["cases"]
        if "request" in c["expect"]
    }
    assert {"affirmative", "negated", ""} <= polarities


def test_the_corpus_labels_the_direction_where_it_matters():
    """Every directive case in the polarity category carries the label."""
    for case in eval_intent.load_cases()["cases"]:
        if case.get("category") == "polarity":
            assert "request" in case["expect"], case["id"]


def test_the_harness_runs_without_a_database_or_a_key():
    """It scores text, so it must work on a bare checkout."""
    assert eval_intent.load_cases()["cases"]
    m = result()
    assert m["cases"] >= 30


# ── What the request acts on ──────────────────────────────────────────────
def test_what_the_request_acts_on_is_exact_on_the_labelled_cases():
    m = result()
    assert m["object_cases"] >= 13
    assert m["object_correct"] == m["object_cases"]
    assert m["object_accuracy"] == 1.0
    assert m["object_kind_accuracy"] == 1.0
    assert m["object_source_accuracy"] == 1.0


def test_every_object_class_is_exercised():
    """A corpus that only held «person» would score a constant at 1.0."""
    kinds = {
        (c["expect"].get("object") or {}).get("kind")
        for c in eval_intent.load_cases()["cases"]
        if "object" in c["expect"]
    }
    assert {"person", "media", "link", "message", "thing", ""} <= kinds


def test_the_person_reading_is_never_lost():
    """The other direction of the same rule: a request that acts on a member must
    still read as a person. A reader that called everything a thing would be safe
    and useless."""
    m = result()
    assert m["object_person_cases"] >= 3
    assert m["object_person_recall"] == 1.0


def test_the_residual_person_lead_is_driven_to_zero():
    """The *guess* is gone; the explicit half survives and is right.

    ``referents`` used to offer a person for a request whose object is a thing:
    the clitic on a content verb («پاکش کن»), and the bare demonstrative with one
    («اینو پاک کن»). The guard scopes the guessing away, so no *wrong* lead
    remains and this pins the result. The floor is stated as a floor as well, so a
    change that made the labelled set shrink — rather than the lead disappear —
    fails here instead of reading as a win.

    The leads that *do* remain are all explicit, and the corpus now carries them:
    a reply edge, a stated id and a name identify who the thing belongs to and
    must survive — ``tests/test_referents.py`` asserts that directly, and
    ``object-media-reply-author`` / ``object-link-reply-author`` /
    ``object-media-named-author`` are the labelled shape. Before those existed the
    assertion could be ``== 0`` only because the corpus never rendered the two
    blocks together, which is the blind spot the object block's order lived in.
    """
    m = result()
    assert m["object_thing_cases"] >= 8
    assert m["object_person_offered_for_a_thing_wrong"] == 0
    assert m["object_person_offered_for_a_thing"] >= 3


def test_the_object_block_stays_small():
    assert result()["object_chars_max"] <= 200


def test_the_object_reader_is_fast_enough_to_run_on_every_pass():
    """It tokenizes the anchor and walks the window once — no query."""
    assert result()["object_us_mean"] < 1000


def test_every_object_case_is_exact():
    """The cases the increment added, scored on every column they label."""
    m = result()
    detail = {r["id"]: r for r in m["detail"]}
    ids = sorted(
        c["id"]
        for c in eval_intent.load_cases()["cases"]
        if c.get("category") == "object"
    )
    assert len(ids) >= 13, ids
    for case_id in ids:
        r = detail[case_id]
        assert r["object_ok"], case_id
        assert r["kind_ok"] and r["act_ok"] and r["when_ok"], case_id
        assert r["edges_ok"] and r["focus_ok"], case_id
        assert r["media_ok"] and r["link_ok"], case_id
        if r["has_named_label"]:
            assert r["named_ok"], case_id
        if r["has_request_label"]:
            assert r["request_ok"], case_id


def test_the_corpus_labels_the_object_where_it_matters():
    """Every case in the object category carries the label."""
    for case in eval_intent.load_cases()["cases"]:
        if case.get("category") == "object":
            assert "object" in case["expect"], case["id"]
