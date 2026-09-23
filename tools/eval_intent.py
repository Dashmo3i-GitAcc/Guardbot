#!/usr/bin/env python3
"""Score the deterministic half of Nexus understanding against a labelled corpus.

Why this exists
---------------
The brief for this stage is explicit that no claim of "smarter" may be made
without a number, and that the number must be reproducible. This is where the
numbers come from. It runs the *deterministic* layers — the name matcher, the
deictic expression finder, the referent resolver, the act reader, the polarity
reader, the open-question reader, the time-word reader, the room-state reader and
the entity reader — over a labelled corpus and reports accuracy, ambiguity
behaviour, the cost in microseconds, and the size of the block the model would be
shown.

What it can and cannot measure
------------------------------
It measures, exactly, the parts of understanding the server does without a model
call. It does **not** measure the model's semantic judgement: whether the
assistant decides a conversation concerns it, and whether it chooses to speak,
are decisions only a live pass can make, and this harness will not pretend
otherwise. Every number below is about what the *server* determines before the
model is asked.

The before/after pair
---------------------
``provided_before`` is what the server could hand the model before this stage:
the reply edge, and nothing else. ``provided_after`` is what it hands over now.
The gap between them is the whole claim, stated as a fraction.

    python tools/eval_intent.py
    python tools/eval_intent.py --json
    python tools/eval_intent.py --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# The app reads its settings at import time and refuses to load without the
# handful that have no sane default. The harness needs none of them for real —
# it never sends, never writes, never calls a model — but the import must
# succeed, so they are set to the same placeholders the test suite uses.
os.environ.setdefault("BOT_TOKEN", "eval-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-eval")
os.environ.setdefault("GEMINI_KEY_STORE_PATH", "/tmp/guardbot-eval/gemini_keys.json")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    addressing,
    config,
    discourse,
    entities,
    objects,
    referents,
    requests,
    room_state,
    temporal,
)

CASES_PATH = Path(__file__).resolve().parent / "eval_cases.json"

# The names the matcher answers to, pinned so the addressing column is
# reproducible regardless of the host's .env.
EVAL_NAMES = ("nexus", "نکسوس")


def load_cases(path: Path = CASES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _row(raw: dict) -> dict:
    """One window row, with every column the resolver, the matcher and the
    discourse reader consult."""
    return {
        "id": int(raw.get("at") or 0),
        "user_id": int(raw.get("user_id") or 0),
        "role": str(raw.get("role") or "member"),
        "name": str(raw.get("name") or ""),
        "text": str(raw.get("text") or ""),
        "at": int(raw.get("at") or 0),
        "message_id": int(raw.get("message_id") or 0),
        "reply_user_id": int(raw.get("reply_user_id") or 0),
        "reply_name": str(raw.get("reply_name") or ""),
        "reply_message_id": int(raw.get("reply_message_id") or 0),
        "directed": bool(raw.get("directed") or False),
        "actor": bool(raw.get("actor") or False),
        "kind": str(raw.get("kind") or ""),
    }


def evaluate(cases: dict) -> dict:
    """Run every case and return the metrics, plus the per-case detail."""
    config.NEXUS_NAMES = list(EVAL_NAMES)
    config.NEXUS_EXTRA_ACTION_WORDS = []

    detail: list[dict] = []
    for case in cases["cases"]:
        window = [_row(row) for row in case.get("window") or ()]
        anchor = _row(case["anchor"])
        expect = case["expect"]

        started = time.perf_counter()
        expression = referents.find_expression(anchor["text"])
        resolution = referents.resolve(anchor, messages=window)
        block = referents.render(resolution)
        elapsed_us = (time.perf_counter() - started) * 1_000_000
        addressed = addressing.detect(anchor["text"]).addressed
        act = discourse.read_act(anchor["text"])
        questions = discourse.open_questions(window)
        questions_block = discourse.render_questions(questions)

        # Timed apart from the resolver: the report quotes each layer's own
        # cost, and folding two layers into one number would hide which one grew.
        when_started = time.perf_counter()
        when = temporal.read_when(anchor["text"])
        window_start = min(
            (int(row["at"]) for row in window if int(row.get("at") or 0)), default=0
        )
        when_block = temporal.render(when, now=anchor["at"], window_start=window_start)
        when_us = (time.perf_counter() - when_started) * 1_000_000

        state = room_state.read_state(window, anchor)
        graph_block = room_state.render_graph(state)
        thread_block = room_state.render_thread(state)

        # Timed apart, for the same reason the time reader is: the entity reader
        # walks the window, and its cost must not be hidden inside the resolver's.
        entity_started = time.perf_counter()
        ent = entities.read_entities(window, anchor)
        entity_block = entities.render(ent)
        entity_us = (time.perf_counter() - entity_started) * 1_000_000
        media = ent.of_kind(entities.KIND_MEDIA)
        newest_media = media[0].detail if media else ""

        # Timed apart again: the polarity reader borrows the act lexicon and
        # tokenizes the anchor once, and its cost must not be hidden.
        request_started = time.perf_counter()
        request = requests.read_request(anchor["text"])
        request_block = requests.render(request)
        request_us = (time.perf_counter() - request_started) * 1_000_000

        # …and the object reader, which walks the window for the thing the
        # message points at.
        object_started = time.perf_counter()
        target = objects.read_object(anchor["text"], window, anchor)
        object_block = objects.render(target)
        object_us = (time.perf_counter() - object_started) * 1_000_000

        top = resolution.top()
        top_id = top.user_id if top else None

        expected_questions = [str(q) for q in (expect.get("open_questions") or ())]
        expected_when = str(expect.get("when") or "")
        expected_when_unit = str(expect.get("when_unit") or "")
        # The reply graph and the focus are facts read off a stored column, so
        # every case can be scored on them — a case with no reply row legitimately
        # expects no edge. The relation is a reading of meaning, so it is scored
        # only where it was labelled; an unlabelled case is not evidence of
        # anything.
        expected_state = expect.get("state") or {}
        expected_edges = sorted(
            (int(pair[0]), int(pair[1])) for pair in (expected_state.get("edges") or ())
        )
        expected_focus = int(expected_state.get("focus") or 0)
        has_relation_label = "relation" in expected_state
        expected_relation = str(expected_state.get("relation") or "")
        # The newest media kind and whether a link is present are read off the
        # row's stored kind and its text, so they are facts and are scored over
        # every case. The class the message *names* is scored only where labelled.
        expected_entities = expect.get("entities") or {}
        expected_newest_media = str(expected_entities.get("newest_media") or "")
        expected_has_link = bool(expected_entities.get("has_link") or False)
        has_named_label = "named" in expected_entities
        expected_named = str(expected_entities.get("named") or "")

        # The directive, its direction and its manner are a reading of the words,
        # so they are scored only where labelled — the same rule the relation and
        # the named class follow. The default is the empty reading, which is what
        # a message with no directive legitimately gets.
        expected_request = expect.get("request") or {}
        has_request_label = "request" in expect
        expected_directive = str(expected_request.get("directive") or "")
        expected_polarity = str(expected_request.get("polarity") or "")
        expected_manner = str(expected_request.get("manner") or "")

        # The object is a reading of the words, so it is scored only where
        # labelled — and its label says the *truth*, which is why the harness can
        # count the residual wrong lead below rather than hiding it.
        expected_object = expect.get("object") or {}
        has_object_label = "object" in expect
        expected_object_kind = str(expected_object.get("kind") or "")
        expected_object_source = str(expected_object.get("source") or "")

        detail.append(
            {
                "id": case["id"],
                "category": case.get("category", ""),
                "note": case.get("note", ""),
                "expected_kind": expect["expression_kind"],
                "got_kind": expression.kind,
                "expected_referent": expect["referent"],
                "got_referent": top_id,
                "requires_resolution": bool(expect["requires_resolution"]),
                "expected_ambiguous": bool(expect["ambiguous"]),
                "got_ambiguous": bool(resolution.ambiguous),
                "got_confident": bool(resolution.confident),
                "reply_user_id": int(anchor.get("reply_user_id") or 0),
                "expected_addressed": bool(expect["addressed"]),
                "got_addressed": bool(addressed),
                "expected_act": expect.get("act", discourse.ACT_UNKNOWN),
                "got_act": act.kind,
                "act_why": act.why[0] if act.why else "",
                "expected_questions": expected_questions,
                "got_questions": [q.text for q in questions],
                "questions_block_chars": len(questions_block),
                "expected_when": expected_when,
                "got_when": when.kind,
                "expected_when_unit": expected_when_unit,
                "got_when_unit": when.unit,
                "when_why": when.why,
                "when_block_chars": len(when_block),
                "expected_edges": expected_edges,
                "got_edges": sorted((e.source_id, e.target_id) for e in state.edges),
                "expected_focus": expected_focus,
                "got_focus": state.focus_id,
                "has_relation_label": has_relation_label,
                "expected_relation": expected_relation,
                "got_relation": state.relation,
                "graph_chars": len(graph_block),
                "thread_chars": len(thread_block),
                "expected_newest_media": expected_newest_media,
                "got_newest_media": newest_media,
                "expected_has_link": expected_has_link,
                "got_has_link": bool(ent.of_kind(entities.KIND_LINK)),
                "has_named_label": has_named_label,
                "expected_named": expected_named,
                "got_named": ent.named,
                "entity_chars": len(entity_block),
                "has_request_label": has_request_label,
                "expected_directive": expected_directive,
                "got_directive": request.directive,
                "expected_polarity": expected_polarity,
                "got_polarity": request.polarity,
                "expected_manner": expected_manner,
                "got_manner": request.manner,
                "request_why": request.why[0] if request.why else "",
                "request_chars": len(request_block),
                "has_object_label": has_object_label,
                "expected_object_kind": expected_object_kind,
                "got_object_kind": target.kind,
                "expected_object_source": expected_object_source,
                "got_object_source": target.source,
                "object_surface": target.surface,
                "object_why": target.why[0] if target.why else "",
                "object_chars": len(object_block),
                "block_chars": len(block),
                "us": elapsed_us,
                "when_us": when_us,
                "entity_us": entity_us,
                "request_us": request_us,
                "object_us": object_us,
                "kind_ok": expression.kind == expect["expression_kind"],
                "addressed_ok": bool(addressed) == bool(expect["addressed"]),
                "act_ok": act.kind == expect.get("act", discourse.ACT_UNKNOWN),
                "when_ok": when.kind == expected_when and when.unit == expected_when_unit,
                "edges_ok": sorted((e.source_id, e.target_id) for e in state.edges)
                == expected_edges,
                "focus_ok": state.focus_id == expected_focus,
                "relation_ok": state.relation == expected_relation,
                "media_ok": newest_media == expected_newest_media,
                "link_ok": bool(ent.of_kind(entities.KIND_LINK)) == expected_has_link,
                "named_ok": ent.named == expected_named,
                "directive_ok": request.directive == expected_directive,
                "polarity_ok": request.polarity == expected_polarity,
                "manner_ok": request.manner == expected_manner,
                "request_ok": (
                    request.directive == expected_directive
                    and request.polarity == expected_polarity
                    and request.manner == expected_manner
                ),
                "object_ok": (
                    target.kind == expected_object_kind
                    and target.source == expected_object_source
                ),
                "object_kind_ok": target.kind == expected_object_kind,
                "object_source_ok": target.source == expected_object_source,
            }
        )

    return {"detail": detail, **_metrics(detail)}


def _metrics(detail: list[dict]) -> dict:
    """Turn the per-case readings into the numbers the report quotes."""
    n = len(detail) or 1

    def rate(rows, ok) -> float:
        rows = list(rows)
        return (sum(1 for r in rows if ok(r)) / len(rows)) if rows else 0.0

    # The cases where the text alone leaves the referent open — the ones the
    # resolver exists for.
    needs = [r for r in detail if r["requires_resolution"]]
    # …and the subset that has a determinate answer, where top-1 can be scored.
    answerable = [r for r in needs if r["expected_referent"] is not None]
    ambiguous_cases = [r for r in detail if r["expected_ambiguous"]]
    predicted_ambiguous = [r for r in detail if r["got_ambiguous"]]

    # BEFORE: the reply edge and nothing else. The server names a person only
    # when the instruction was sent as a reply, and it names that edge's target.
    provided_before = rate(
        answerable,
        lambda r: r["reply_user_id"] and r["reply_user_id"] == r["expected_referent"],
    )
    # AFTER: the resolver ranks the right person first.
    provided_after = rate(answerable, lambda r: r["got_referent"] == r["expected_referent"])
    # …and the subset it is also sure about, which is the set that needs no
    # clarification round trip.
    confident_correct = rate(
        answerable,
        lambda r: r["got_confident"] and r["got_referent"] == r["expected_referent"],
    )

    # ── The act ───────────────────────────────────────────────────────────
    # Scored three ways, because accuracy alone is a number a constant also
    # gets on a corpus where most messages are instructions.
    unknown = discourse.ACT_UNKNOWN
    claimed = [r for r in detail if r["got_act"] != unknown]
    labelled = [r for r in detail if r["expected_act"] != unknown]
    act_abstentions = [r for r in detail if r["got_act"] == unknown]
    # The two directions of being wrong, and they are not symmetric: an
    # over-claim puts a wrong act in the prompt, an abstention only withholds a
    # signal the model did not have before this stage.
    act_false_positive = [r for r in claimed if r["expected_act"] == unknown]
    act_false_negative = [r for r in labelled if r["got_act"] == unknown]

    # ── The room's open questions ─────────────────────────────────────────
    # Scored over the cases that *have* a question, not over every case: an
    # empty window and an empty expectation agree trivially, and counting those
    # would make the number a statement about the corpus size.
    question_cases = [r for r in detail if r["expected_questions"]]
    question_predicted = [r for r in detail if r["got_questions"]]
    question_hits = sum(
        1 for r in question_cases if set(r["got_questions"]) == set(r["expected_questions"])
    )

    # ── The time words ────────────────────────────────────────────────────
    # Scored the same way the act is, and for the same reason: a reading of ""
    # is an abstention, not a wrong answer, so accuracy alone would let a
    # constant win. A claimed reading that should have been empty is the
    # dangerous direction — it puts a wrong time in the prompt — and it is
    # counted separately.
    when_claimed = [r for r in detail if r["got_when"]]
    when_labelled = [r for r in detail if r["expected_when"]]
    when_false_positive = [
        r for r in when_claimed if not r["expected_when"]
    ]
    when_false_negative = [
        r for r in when_labelled if not r["got_when"]
    ]

    # ── The room's state ──────────────────────────────────────────────────
    # The reply graph and the focus are facts off a stored column, so they are
    # scored over every case. The relation is a reading, so it is scored only
    # where it was labelled.
    relation_cases = [r for r in detail if r["has_relation_label"]]
    edges_expected = [r for r in detail if r["expected_edges"]]
    edges_claimed = [r for r in detail if r["got_edges"]]

    # ── The things the anchor may point at ────────────────────────────────
    # The newest media kind and whether a link is present are read off the row, so
    # they are scored over every case. The class the message names is a reading of
    # the words, so it is scored only where it was labelled.
    named_cases = [r for r in detail if r["has_named_label"]]

    # ── The directive's direction ─────────────────────────────────────────
    # Scored only where labelled, because it is a reading of the words. The
    # metric that matters is the *dangerous direction*: a message that forbids
    # the action read as ``affirmative`` — «بنش نکن» reported as "asks for a
    # ban" — which is the mistake this reader exists to prevent. The safe
    # direction, an abstention (``""``), is counted apart from it.
    request_cases = [r for r in detail if r["has_request_label"]]
    request_negated = [r for r in request_cases if r["expected_polarity"] == requests.POLARITY_NEGATED]
    request_affirmative = [
        r for r in request_cases if r["expected_polarity"] == requests.POLARITY_AFFIRMATIVE
    ]
    request_false_affirmative = [
        r
        for r in request_negated
        if r["got_polarity"] == requests.POLARITY_AFFIRMATIVE
    ]

    # ── What the request acts on ──────────────────────────────────────────
    # Scored only where labelled, because it is a reading of the words. Two
    # numbers matter and they are different numbers. The accuracy says whether the
    # server read the target right. The *residual lead* says whether the prompt
    # still contains the wrong one: a request whose object is a thing, with the
    # resolver still offering a person as who it might mean. The object line
    # corrects that in words; the resolver's own guard is what removes the lead,
    # and this number is what says whether it did.
    object_cases = [r for r in detail if r["has_object_label"]]
    # "A thing" is the labelled classes, and the abstention is deliberately not
    # one of them: a case whose expected class is "" asserts that the server has
    # *no* reading of what the request acts on, so a person offered there is the
    # resolver doing its ordinary job — not a thing-lead. Counting it would make
    # the metric's name false in the direction that flatters the guard.
    object_thing = [
        r
        for r in object_cases
        if r["expected_object_kind"] in objects.CLASSES
        and r["expected_object_kind"] != objects.CLASS_PERSON
    ]
    object_person = [
        r for r in object_cases if r["expected_object_kind"] == objects.CLASS_PERSON
    ]
    object_person_offered = [r for r in object_thing if r["got_referent"] is not None]

    return {
        "cases": len(detail),
        "expression_accuracy": rate(detail, lambda r: r["kind_ok"]),
        "addressing_accuracy": rate(detail, lambda r: r["addressed_ok"]),
        "act_accuracy": rate(detail, lambda r: r["act_ok"]),
        # Of the acts it claimed, the fraction it got right.
        "act_claimed_precision": rate(claimed, lambda r: r["act_ok"]),
        # Of all cases, the fraction it offered a reading for at all.
        "act_coverage": len(claimed) / n,
        # Of the cases that have an act to find, the fraction it found right.
        "act_recall": rate(labelled, lambda r: r["act_ok"]),
        "act_false_positives": len(act_false_positive),
        "act_false_negatives": len(act_false_negative),
        "act_abstentions": len(act_abstentions),
        # Per class, because the corpus is lopsided — most messages in a
        # moderation room are instructions — and a single accuracy figure would
        # be a number a constant could also get. This is the honest shape.
        "act_by_class": {
            kind: {
                "total": sum(1 for r in detail if r["expected_act"] == kind),
                "correct": sum(
                    1 for r in detail if r["expected_act"] == kind and r["act_ok"]
                ),
            }
            for kind in discourse.ACTS + (unknown,)
        },
        "questions_cases": len(question_cases),
        "questions_exact": question_hits,
        "questions_precision": rate(
            question_predicted,
            lambda r: set(r["got_questions"]) <= set(r["expected_questions"]),
        ),
        "questions_recall": rate(
            question_cases, lambda r: set(r["expected_questions"]) <= set(r["got_questions"])
        ),
        "questions_block_chars_max": max(
            (r["questions_block_chars"] for r in detail), default=0
        ),
        "when_accuracy": rate(detail, lambda r: r["when_ok"]),
        "when_claimed_precision": rate(when_claimed, lambda r: r["when_ok"]),
        "when_coverage": len(when_claimed) / n,
        "when_recall": rate(when_labelled, lambda r: r["when_ok"]),
        "when_false_positives": len(when_false_positive),
        "when_false_negatives": len(when_false_negative),
        "when_by_kind": {
            kind: {
                "total": sum(1 for r in detail if r["expected_when"] == kind),
                "correct": sum(
                    1 for r in detail if r["expected_when"] == kind and r["when_ok"]
                ),
            }
            for kind in ("",) + temporal.WHENS
        },
        "when_block_chars_max": max(
            (r["when_block_chars"] for r in detail), default=0
        ),
        "state_cases": len(relation_cases),
        "edges_expected": len(edges_expected),
        "edges_exact": sum(1 for r in detail if r["edges_ok"]),
        # Of the cases where an edge was claimed, the fraction where every
        # claimed edge was one the corpus knows about.
        "edges_precision": rate(
            edges_claimed, lambda r: set(r["got_edges"]) <= set(r["expected_edges"])
        ),
        # …and of the cases where one is expected, the fraction it found.
        "edges_recall": rate(
            edges_expected, lambda r: set(r["expected_edges"]) <= set(r["got_edges"])
        ),
        "edges_false_positives": sum(
            1 for r in edges_claimed if not r["expected_edges"]
        ),
        "edges_false_negatives": sum(
            1 for r in edges_expected if not r["got_edges"]
        ),
        "focus_accuracy": rate(detail, lambda r: r["focus_ok"]),
        "relation_correct": sum(1 for r in relation_cases if r["relation_ok"]),
        "relation_accuracy": rate(relation_cases, lambda r: r["relation_ok"]),
        "relation_by_kind": {
            kind: {
                "total": sum(1 for r in relation_cases if r["expected_relation"] == kind),
                "correct": sum(
                    1
                    for r in relation_cases
                    if r["expected_relation"] == kind and r["relation_ok"]
                ),
            }
            for kind in ("",) + room_state.RELATIONS
        },
        "graph_chars_max": max((r["graph_chars"] for r in detail), default=0),
        "thread_chars_max": max((r["thread_chars"] for r in detail), default=0),
        "media_exact": sum(1 for r in detail if r["media_ok"]),
        "link_exact": sum(1 for r in detail if r["link_ok"]),
        "named_cases": len(named_cases),
        "named_correct": sum(1 for r in named_cases if r["named_ok"]),
        "named_accuracy": rate(named_cases, lambda r: r["named_ok"]),
        "entity_block_chars_max": max((r["entity_chars"] for r in detail), default=0),
        "request_cases": len(request_cases),
        "request_correct": sum(1 for r in request_cases if r["request_ok"]),
        "request_accuracy": rate(request_cases, lambda r: r["request_ok"]),
        # Per field, because the directive and the manner are inherited from the
        # act lexicon while the polarity is what this increment added: a single
        # accuracy figure would let the new column hide behind the old ones.
        "request_directive_accuracy": rate(request_cases, lambda r: r["directive_ok"]),
        "request_polarity_accuracy": rate(request_cases, lambda r: r["polarity_ok"]),
        "request_manner_accuracy": rate(request_cases, lambda r: r["manner_ok"]),
        "request_negated_cases": len(request_negated),
        "request_negated_recall": rate(request_negated, lambda r: r["polarity_ok"]),
        # The dangerous direction, named: a message that forbids the action read
        # as asking for it. Should be zero, and it is the number to watch.
        "request_false_affirmative": len(request_false_affirmative),
        # The safe direction: the reader abstained on a case that has an answer.
        "request_abstained": sum(
            1
            for r in request_cases
            if r["got_polarity"] == "" and r["expected_polarity"] != ""
        ),
        # …split by what was expected, because abstaining on a negated case still
        # withholds the warning while abstaining on an affirmative one only
        # withholds a nicety.
        "request_affirmative_cases": len(request_affirmative),
        "request_affirmative_abstained": sum(
            1 for r in request_affirmative if r["got_polarity"] == ""
        ),
        "request_chars_max": max((r["request_chars"] for r in detail), default=0),
        "request_us_mean": (
            statistics.fmean([r["request_us"] for r in detail]) if detail else 0.0
        ),
        "request_us_p95": (
            sorted(r["request_us"] for r in detail)[
                min(len(detail) - 1, int(len(detail) * 0.95))
            ]
            if detail
            else 0.0
        ),
        "object_cases": len(object_cases),
        "object_correct": sum(1 for r in object_cases if r["object_ok"]),
        "object_accuracy": rate(object_cases, lambda r: r["object_ok"]),
        # Split, because the class is what the model acts on and the source is how
        # much the server is claiming to know.
        "object_kind_accuracy": rate(object_cases, lambda r: r["object_kind_ok"]),
        "object_source_accuracy": rate(object_cases, lambda r: r["object_source_ok"]),
        "object_by_kind": {
            kind: {
                "total": sum(
                    1 for r in object_cases if r["expected_object_kind"] == kind
                ),
                "correct": sum(
                    1
                    for r in object_cases
                    if r["expected_object_kind"] == kind and r["object_kind_ok"]
                ),
            }
            for kind in ("",) + objects.CLASSES
        },
        # Of the requests whose object is a person, the fraction read as a person.
        "object_person_cases": len(object_person),
        "object_person_recall": rate(
            object_person, lambda r: r["got_object_kind"] == objects.CLASS_PERSON
        ),
        # The residual wrong lead, counted rather than hidden: a thing-object
        # request for which the prompt still lists a person. The object line
        # corrects it in words; driving this to zero is its own change.
        "object_thing_cases": len(object_thing),
        "object_person_offered_for_a_thing": len(object_person_offered),
        "object_chars_max": max((r["object_chars"] for r in detail), default=0),
        "object_us_mean": (
            statistics.fmean([r["object_us"] for r in detail]) if detail else 0.0
        ),
        "object_us_p95": (
            sorted(r["object_us"] for r in detail)[
                min(len(detail) - 1, int(len(detail) * 0.95))
            ]
            if detail
            else 0.0
        ),
        "needs_resolution": len(needs),
        "answerable": len(answerable),
        "resolution_top1_accuracy": rate(
            answerable, lambda r: r["got_referent"] == r["expected_referent"]
        ),
        "ambiguity_recall": rate(ambiguous_cases, lambda r: r["got_ambiguous"]),
        "ambiguity_precision": rate(predicted_ambiguous, lambda r: r["expected_ambiguous"]),
        # The dangerous direction: the server was sure, and wrong.
        "wrong_confident": sum(
            1
            for r in ambiguous_cases
            if r["got_confident"] and r["got_referent"] != r["expected_referent"]
        ),
        "provided_before": provided_before,
        "provided_after": provided_after,
        "confident_and_correct": confident_correct,
        "block_chars_mean": statistics.fmean([r["block_chars"] for r in detail]) if detail else 0.0,
        "block_chars_max": max((r["block_chars"] for r in detail), default=0),
        "us_mean": statistics.fmean([r["us"] for r in detail]) if detail else 0.0,
        "us_p95": (
            sorted(r["us"] for r in detail)[min(len(detail) - 1, int(len(detail) * 0.95))]
            if detail
            else 0.0
        ),
        "when_us_mean": statistics.fmean([r["when_us"] for r in detail]) if detail else 0.0,
        "when_us_p95": (
            sorted(r["when_us"] for r in detail)[
                min(len(detail) - 1, int(len(detail) * 0.95))
            ]
            if detail
            else 0.0
        ),
        "entity_us_mean": (
            statistics.fmean([r["entity_us"] for r in detail]) if detail else 0.0
        ),
        "entity_us_p95": (
            sorted(r["entity_us"] for r in detail)[
                min(len(detail) - 1, int(len(detail) * 0.95))
            ]
            if detail
            else 0.0
        ),
        "n": n,
    }


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def report(result: dict, *, verbose: bool = False) -> str:
    m = {k: v for k, v in result.items() if k != "detail"}
    lines = [
        "",
        "Nexus deterministic-understanding benchmark",
        "==========================================",
        f"cases: {m['cases']}",
        "",
        "understanding",
        f"  expression accuracy        {_pct(m['expression_accuracy'])}",
        f"  addressing accuracy        {_pct(m['addressing_accuracy'])}",
        "",
        f"the act (abstention is {discourse.ACT_UNKNOWN!r}, and it is not a failure)",
        f"  claimed precision          {_pct(m['act_claimed_precision'])}",
        f"  coverage                   {_pct(m['act_coverage'])}",
        f"  recall on labelled cases   {_pct(m['act_recall'])}",
        f"  false positives/negatives  {m['act_false_positives']} / {m['act_false_negatives']}",
        f"  abstentions                {m['act_abstentions']}",
        "  per class (correct/total)  "
        + "  ".join(
            f"{kind} {v['correct']}/{v['total']}"
            for kind, v in m["act_by_class"].items()
            if v["total"]
        ),
        "",
        f"the room's open questions ({m['questions_cases']} cases that have one)",
        f"  exact match                {m['questions_exact']} / {m['questions_cases']}",
        f"  precision                  {_pct(m['questions_precision'])}",
        f"  recall                     {_pct(m['questions_recall'])}",
        f"  block chars max            {m['questions_block_chars_max']}",
        "",
        "time words (an empty reading is an abstention, not a failure)",
        f"  claimed precision          {_pct(m['when_claimed_precision'])}",
        f"  coverage                   {_pct(m['when_coverage'])}",
        f"  recall on labelled cases   {_pct(m['when_recall'])}",
        f"  false positives/negatives  {m['when_false_positives']} / {m['when_false_negatives']}",
        "  per kind (correct/total)   "
        + "  ".join(
            f"{kind or 'none'} {v['correct']}/{v['total']}"
            for kind, v in m["when_by_kind"].items()
            if v["total"]
        ),
        f"  block chars max            {m['when_block_chars_max']}",
        "",
        f"the room's state (reply graph over every case; relation over {m['state_cases']} labelled)",
        f"  edges exact                {m['edges_exact']} / {m['cases']}",
        f"  edges precision            {_pct(m['edges_precision'])}",
        f"  edges recall               {_pct(m['edges_recall'])}",
        f"  edges false positives/neg  {m['edges_false_positives']} / {m['edges_false_negatives']}",
        f"  focus accuracy             {_pct(m['focus_accuracy'])}",
        f"  relation exact             {m['relation_correct']} / {m['state_cases']}",
        f"  relation accuracy          {_pct(m['relation_accuracy'])}",
        "  per relation (correct/total)  "
        + "  ".join(
            f"{kind or 'none'} {v['correct']}/{v['total']}"
            for kind, v in m["relation_by_kind"].items()
            if v["total"]
        ),
        f"  graph / thread chars max   {m['graph_chars_max']} / {m['thread_chars_max']}",
        "",
        f"the things a demonstrative may point at (class over {m['named_cases']} labelled)",
        f"  media exact                {m['media_exact']} / {m['cases']}",
        f"  link exact                 {m['link_exact']} / {m['cases']}",
        f"  named class exact          {m['named_correct']} / {m['named_cases']}",
        f"  named class accuracy       {_pct(m['named_accuracy'])}",
        f"  block chars max            {m['entity_block_chars_max']}",
        "",
        f"the directive's direction (over {m['request_cases']} labelled cases)",
        f"  exact (word·direction·manner)  {m['request_correct']} / {m['request_cases']}",
        f"  accuracy                   {_pct(m['request_accuracy'])}",
        "  per field                  "
        f"word {_pct(m['request_directive_accuracy'])}  "
        f"direction {_pct(m['request_polarity_accuracy'])}  "
        f"manner {_pct(m['request_manner_accuracy'])}",
        f"  negated recall             {m['request_negated_cases']} cases, "
        f"{_pct(m['request_negated_recall'])} found",
        f"  forbidden read as asked-for {m['request_false_affirmative']} "
        "(the dangerous direction — should be 0)",
        f"  abstained (safe)           {m['request_abstained']} "
        f"of which affirmative {m['request_affirmative_abstained']}"
        f"/{m['request_affirmative_cases']}",
        f"  block chars max            {m['request_chars_max']}",
        "",
        f"what the request acts on (over {m['object_cases']} labelled cases)",
        f"  exact (class·source)       {m['object_correct']} / {m['object_cases']}",
        f"  accuracy                   {_pct(m['object_accuracy'])}",
        "  per field                  "
        f"class {_pct(m['object_kind_accuracy'])}  "
        f"source {_pct(m['object_source_accuracy'])}",
        "  per class (correct/total)  "
        + "  ".join(
            f"{kind or 'none'} {v['correct']}/{v['total']}"
            for kind, v in m["object_by_kind"].items()
            if v["total"]
        ),
        f"  person recall              {m['object_person_cases']} cases, "
        f"{_pct(m['object_person_recall'])} read as a person",
        f"  a person still offered for a thing-object request  "
        f"{m['object_person_offered_for_a_thing']} / {m['object_thing_cases']} "
        "(the resolver's guard scopes the guessing; an explicit name, id or "
        "reply edge still identifies the thing's author)",
        f"  block chars max            {m['object_chars_max']}",
        "",
        f"referent resolution ({m['answerable']} answerable of {m['needs_resolution']} open)",
        f"  top-1 accuracy             {_pct(m['resolution_top1_accuracy'])}",
        f"  ambiguity recall           {_pct(m['ambiguity_recall'])}",
        f"  ambiguity precision        {_pct(m['ambiguity_precision'])}",
        f"  wrong-but-confident        {m['wrong_confident']}",
        "",
        "the claim, as a fraction",
        f"  provided before (reply edge only)  {_pct(m['provided_before'])}",
        f"  provided after  (resolver)         {_pct(m['provided_after'])}",
        f"  confident and correct              {_pct(m['confident_and_correct'])}",
        "",
        "cost",
        # ``block_chars`` is the **referent candidates** block and nothing else —
        # the other readers report their own sizes beside their own metrics. The
        # label says so, because "block chars" read as "the whole context" once,
        # in a report, and that is a claim the harness does not measure.
        f"  referent block chars mean / max  {m['block_chars_mean']:.0f} / {m['block_chars_max']}",
        f"  resolver us mean / p95     {m['us_mean']:.0f} / {m['us_p95']:.0f}",
        f"  time-word us mean / p95    {m['when_us_mean']:.0f} / {m['when_us_p95']:.0f}",
        f"  entity us mean / p95       {m['entity_us_mean']:.0f} / {m['entity_us_p95']:.0f}",
        f"  polarity us mean / p95     {m['request_us_mean']:.0f} / {m['request_us_p95']:.0f}",
        f"  object us mean / p95       {m['object_us_mean']:.0f} / {m['object_us_p95']:.0f}",
        "",
    ]
    failures = [
        r
        for r in result["detail"]
        if not r["kind_ok"] or not r["addressed_ok"] or not r["act_ok"]
        or not r["when_ok"] or not r["edges_ok"] or not r["focus_ok"]
        or not r["media_ok"] or not r["link_ok"]
        or (r["has_relation_label"] and not r["relation_ok"])
        or (r["has_named_label"] and not r["named_ok"])
        or (r["has_request_label"] and not r["request_ok"])
        or (r["has_object_label"] and not r["object_ok"])
        or (r["requires_resolution"] and r["expected_referent"] is not None
            and r["got_referent"] != r["expected_referent"])
        or (r["expected_ambiguous"] and not r["got_ambiguous"])
        or set(r["got_questions"]) != set(r["expected_questions"])
    ]
    if failures:
        lines.append(f"not met ({len(failures)}):")
        for r in failures:
            lines.append(
                f"  [{r['category']}] {r['id']}: "
                f"kind {r['got_kind']!r}/{r['expected_kind']!r} "
                f"ref {r['got_referent']}/{r['expected_referent']} "
                f"amb {r['got_ambiguous']}/{r['expected_ambiguous']} "
                f"addr {r['got_addressed']}/{r['expected_addressed']} "
                f"act {r['got_act']}/{r['expected_act']} "
                f"when {r['got_when']}/{r['expected_when']}"
                f"·{r['got_when_unit']}/{r['expected_when_unit']} "
                f"edges {r['got_edges']}/{r['expected_edges']} "
                f"focus {r['got_focus']}/{r['expected_focus']} "
                f"rel {r['got_relation']!r}/{r['expected_relation']!r} "
                f"media {r['got_newest_media']!r}/{r['expected_newest_media']!r} "
                f"link {r['got_has_link']}/{r['expected_has_link']} "
                f"named {r['got_named']!r}/{r['expected_named']!r} "
                f"req {r['got_directive']!r}·{r['got_polarity']!r}·{r['got_manner']!r}"
                f"/{r['expected_directive']!r}·{r['expected_polarity']!r}"
                f"·{r['expected_manner']!r} "
                f"obj {r['got_object_kind']!r}·{r['got_object_source']!r}"
                f"/{r['expected_object_kind']!r}·{r['expected_object_source']!r} "
                f"q {r['got_questions']}/{r['expected_questions']}"
            )
            if verbose and r["note"]:
                lines.append(f"      {r['note']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="explain each miss")
    parser.add_argument("--cases", default=str(CASES_PATH), help="corpus path")
    args = parser.parse_args(argv)

    result = evaluate(load_cases(Path(args.cases)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(report(result, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())
