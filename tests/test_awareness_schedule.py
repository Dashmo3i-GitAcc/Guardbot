"""Increment U: the spend decision that makes 200 requests go further.

The brief for this increment was a scheduling problem, and the tests below pin
down the three things a scheduler has to be able to promise:

* **It cannot see the messages.** The decision that spends a request must not be
  able to read the words, or it becomes a keyword filter with extra steps.
  ``defer`` takes a room id and two numbers; the content is read *once*, at
  capture time, and reduced to a one-word class before it is stored. The blind
  timing gate (``awareness.due``) is not touched at all.
* **It cannot starve a room.** The deferral is bounded by the retention window,
  a room with no evidence is never deferred, and a room whose batch says it
  needs reading is never deferred at all. Every safety gate — ``awareness.due``,
  the allowance, the brake, the breaker — is *above* this decision, and none of
  them is relaxed by it.
* **It holds nothing it should not.** The store is ``chat_id -> (class, stamp)``
  — two words and a float — bounded, expiring, and isolated by room.

Nothing here calls a model or reaches the network. ``read`` is the project's
existing deterministic reader; the scheduler is a lookup and a comparison.
"""
import ast
import inspect

import pytest

from app import (
    admin_service,
    admin_tools,
    awareness,
    awareness_schedule,
    config,
    context_plan,
    rbac,
)

CHAT = -1001234567890
OTHER_CHAT = -1009999999999


@pytest.fixture(autouse=True)
def clean_hints():
    """Every test starts and ends with no hint in the store."""
    awareness_schedule.reset()
    yield
    awareness_schedule.reset()


# ── The boundary: the decision cannot see the message ─────────────────────
def test_the_spend_decision_cannot_see_a_message():
    """``defer`` takes a room and two numbers, and nothing else.

    The same guarantee ``tests/test_awareness.py`` asserts for ``due``, asserted
    for the function U adds: if the decision that spends the rationed request
    could see the words, it would start deciding *whether the words matter*, and
    the scheduler would quietly become the semantic reader the architecture
    keeps out of it. Asserted over the parsed body, so a comment explaining the
    rule cannot fail it.
    """
    parameters = set(inspect.signature(awareness_schedule.defer).parameters)
    assert parameters == {"chat_id", "waited", "waiting", "now"}

    tree = ast.parse(inspect.getsource(awareness_schedule.defer))
    function = tree.body[0]
    body = [
        node
        for node in function.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    referenced = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    forbidden = {
        "text",
        "body",
        "msg",
        "message",
        "messages",
        "content",
        "words",
        "row",
        "rows",
        "pending",
        "reading",
        "wants_awareness",
    }
    assert not (referenced & forbidden)


def test_the_blind_timing_gate_does_not_know_about_the_scheduler():
    """``awareness.due`` is still message-blind *and* scheduler-blind.

    U is applied after ``due`` has already answered, by a different function.
    The moment the timing gate imported the scheduler it would have grown a
    reason to look at what the scheduler knows, so the separation is asserted at
    the source rather than trusted to review.
    """
    assert "awareness_schedule" not in inspect.getsource(awareness)


def test_the_scheduler_makes_no_model_call():
    """No model client, no HTTP client, no Telegram: it is arithmetic."""
    tree = ast.parse(inspect.getsource(awareness_schedule))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "google",
        "gemini_pool",
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "telegram",
    }
    assert not (imported & forbidden)


def test_the_scheduler_is_not_a_permission():
    """Nothing that authorises or executes consults the hint.

    A room being read first is not a permission, a promotion or an action. The
    structural form of that claim: the authorisation modules cannot even see
    this one.
    """
    for module in (rbac, admin_service, admin_tools):
        assert "awareness_schedule" not in inspect.getsource(module)


# ── The reading: one word, from a closed vocabulary ───────────────────────
def test_the_vocabulary_is_closed_and_ordinal():
    assert awareness_schedule.P_NONE == ""
    assert awareness_schedule.P_LOW == "low"
    assert awareness_schedule.P_HIGH == "high"
    # The only numbers are ordinals over the three words — no weight table.
    assert awareness_schedule.RANK == {
        awareness_schedule.P_NONE: 0,
        awareness_schedule.P_LOW: 1,
        awareness_schedule.P_HIGH: 2,
    }


@pytest.mark.parametrize(
    "text",
    ["همونو بزن", "اینو سکوت کن", "بازش کن", "چی؟", "پاکش کن", "بنش کن"],
)
def test_a_message_that_depends_on_the_room_is_high(text):
    assert awareness_schedule.read(text) == awareness_schedule.P_HIGH


@pytest.mark.parametrize(
    "text",
    ["سلام", "ممنون ازت", "قیمت دلار امروز چنده؟", "من دیروز رفتم سفر", "خب تمومه"],
)
def test_a_self_contained_message_is_low(text):
    assert awareness_schedule.read(text) == awareness_schedule.P_LOW


def test_the_structural_facts_the_capture_path_holds_are_part_of_the_class():
    # An attachment takes the room with it; so does a message that addressed
    # Nexus, even when its words are trivial on their own.
    assert awareness_schedule.read("x", media=True, kind="voice") == (
        awareness_schedule.P_HIGH
    )
    assert awareness_schedule.read("سلام", directed=True) == (
        awareness_schedule.P_HIGH
    )
    # A reply raises a message that is not self-contained on its own...
    assert awareness_schedule.read("منم موافقم") == awareness_schedule.P_LOW
    assert awareness_schedule.read("منم موافقم", reply=True) == (
        awareness_schedule.P_HIGH
    )


def test_a_reader_that_raises_contributes_no_evidence(monkeypatch):
    """A broken reader must never cost a capture, and must fail toward reading.

    The fallback is ``P_NONE`` — *no evidence* — which ``note`` refuses to
    store, so the room is read exactly as it was before this module existed.
    ``P_LOW`` would be the wrong direction: a reader that broke on every message
    would silently defer every room in the deployment, and the slowdown would
    look like a scheduling choice rather than a bug.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("reader is broken")

    monkeypatch.setattr(context_plan, "read", boom)
    assert awareness_schedule.read("همونو بزن") == awareness_schedule.P_NONE
    # And the refused class really does leave no hint behind.
    awareness_schedule.note(CHAT, awareness_schedule.read("همونو بزن"), now=100.0)
    assert awareness_schedule.size() == 0
    assert awareness_schedule.defer(CHAT, waited=1.0, now=100.0) is False


# ── The store: bounded, expiring, isolated ────────────────────────────────
def test_the_store_cannot_hold_a_sentence():
    """The value is a class and a stamp; there is no field a message fits in."""
    awareness_schedule.note(CHAT, awareness_schedule.P_HIGH, now=100.0)
    stored = awareness_schedule._hints[CHAT]
    assert isinstance(stored, tuple) and len(stored) == 2
    cls, stamp = stored
    assert cls in {awareness_schedule.P_LOW, awareness_schedule.P_HIGH}
    assert isinstance(stamp, float)


def test_no_evidence_is_never_stored():
    """``P_NONE`` means "unknown", and an unknown room is not a quiet room."""
    awareness_schedule.note(CHAT, awareness_schedule.P_NONE, now=100.0)
    assert awareness_schedule.size() == 0
    assert awareness_schedule.priority(CHAT, now=100.0) == (
        awareness_schedule.P_NONE
    )


def test_the_strongest_class_wins_and_a_weaker_one_refreshes_the_stamp():
    """A room with one message that needs it is still a room that needs it."""
    awareness_schedule.note(CHAT, awareness_schedule.P_HIGH, now=100.0)
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=200.0)
    assert awareness_schedule.priority(CHAT, now=200.0) == (
        awareness_schedule.P_HIGH
    )
    # The stamp moved: the strong message it refers to is still unread, so the
    # hint is not stale merely because the room kept talking.
    assert awareness_schedule._hints[CHAT][1] == 200.0


def test_a_hint_is_scoped_to_its_room():
    awareness_schedule.note(CHAT, awareness_schedule.P_HIGH, now=100.0)
    assert awareness_schedule.priority(OTHER_CHAT, now=100.0) == (
        awareness_schedule.P_NONE
    )
    assert awareness_schedule.defer(OTHER_CHAT, waited=1.0, now=100.0) is False


def test_forget_drops_one_room_only():
    awareness_schedule.note(CHAT, awareness_schedule.P_HIGH, now=100.0)
    awareness_schedule.note(OTHER_CHAT, awareness_schedule.P_LOW, now=100.0)
    awareness_schedule.forget(CHAT)
    assert awareness_schedule.priority(CHAT, now=100.0) == (
        awareness_schedule.P_NONE
    )
    assert awareness_schedule.priority(OTHER_CHAT, now=100.0) == (
        awareness_schedule.P_LOW
    )


def test_an_expired_hint_is_dropped_rather_than_left_to_accumulate():
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    awareness_schedule.priority(
        CHAT, now=100.0 + awareness_schedule._bound() + 1
    )
    assert CHAT not in awareness_schedule._hints


def test_the_store_is_bounded_and_evicts_the_oldest_evidence():
    for index in range(awareness_schedule.MAX_ROOMS + 5):
        awareness_schedule.note(-1000 - index, awareness_schedule.P_LOW, now=100.0 + index)
    assert awareness_schedule.size() == awareness_schedule.MAX_ROOMS
    latest = 100.0 + awareness_schedule.MAX_ROOMS + 5
    # The five oldest stamps are the five that went.
    for index in range(5):
        assert awareness_schedule.priority(-1000 - index, now=latest) == (
            awareness_schedule.P_NONE
        )


def test_a_malformed_room_is_refused_rather_than_raising():
    for bad in (None, "x", object()):
        awareness_schedule.note(bad, awareness_schedule.P_HIGH, now=100.0)
        assert awareness_schedule.priority(bad, now=100.0) == (
            awareness_schedule.P_NONE
        )
        assert awareness_schedule.defer(bad, waited=1.0, now=100.0) is False
        awareness_schedule.forget(bad)
    assert awareness_schedule.size() == 0


# ── The decision: spend, or wait? ─────────────────────────────────────────
def test_a_room_with_no_evidence_is_never_deferred():
    """No hint is not low. An unknown room is read exactly as before."""
    assert awareness_schedule.defer(CHAT, waited=1.0, now=100.0) is False


def test_a_low_room_is_deferred_only_until_its_bound():
    bound = awareness_schedule._bound()
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    assert awareness_schedule.defer(CHAT, waited=bound - 1, now=100.0) is True
    assert awareness_schedule.defer(CHAT, waited=bound, now=100.0) is False
    assert awareness_schedule.defer(CHAT, waited=bound + 1, now=100.0) is False


def test_a_high_room_is_never_deferred():
    """A batch that says it needs the room is read on its own deadline."""
    awareness_schedule.note(CHAT, awareness_schedule.P_HIGH, now=100.0)
    assert awareness_schedule.defer(CHAT, waited=1.0, now=100.0) is False


def test_a_room_the_server_is_waiting_on_is_never_deferred():
    """The one place this mechanism can be *wrong* rather than merely slow.

    A pending admin confirmation is consumed by the pass, and the message that
    confirms it («تأیید میکنم») is self-contained — it does not need the room's
    context — so the class is correctly LOW and the deferral would postpone the
    owner's already-approved action by up to the retention window. *The message
    does not need the room* is not *the pass does not need to run*, and the
    server's own waiting flag is what tells the two apart.
    """
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    assert awareness_schedule.defer(CHAT, waited=1.0, now=100.0) is True
    assert (
        awareness_schedule.defer(CHAT, waited=1.0, waiting=True, now=100.0) is False
    )
    # And it refuses on its own, before any other evidence is consulted.
    assert awareness_schedule.defer(CHAT, waited=1.0, waiting=True, now=100.0) is False


def test_a_stale_hint_is_not_evidence_and_the_room_is_read():
    """Stale evidence degrades to no evidence; it never becomes authority."""
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    later = 100.0 + awareness_schedule._bound() + 1
    assert awareness_schedule.priority(CHAT, now=later) == (
        awareness_schedule.P_NONE
    )
    assert awareness_schedule.defer(CHAT, waited=1.0, now=later) is False


def test_the_deferral_bound_is_the_retention_window(monkeypatch):
    """The bound is derived, not configured: past retention the rows are gone.

    A hint describes a batch of unread messages, and a deferral postpones
    reading that batch. Past the retention window the rows have been purged, so
    both would describe nothing. Deriving the one number from the other is what
    stops them drifting apart.
    """
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 120)
    assert awareness_schedule._bound() == 120.0
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    assert awareness_schedule.defer(CHAT, waited=119.0, now=100.0) is True
    assert awareness_schedule.defer(CHAT, waited=120.0, now=100.0) is False


def test_a_nonpositive_retention_cannot_make_the_bound_vanish(monkeypatch):
    """A mis-set window must not turn the deferral into "hold for ever"."""
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 0)
    assert awareness_schedule._bound() == 1.0
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    assert awareness_schedule.defer(CHAT, waited=1.0, now=100.0) is False


def test_the_decision_is_deterministic():
    """Same inputs, same answer: no clock read, no randomness inside ``defer``."""
    awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0)
    first = [awareness_schedule.defer(CHAT, waited=w, now=100.0) for w in (0, 1, 2)]
    second = [awareness_schedule.defer(CHAT, waited=w, now=100.0) for w in (0, 1, 2)]
    assert first == second == [True, True, True]


def test_reset_empties_the_store():
    awareness_schedule.note(CHAT, awareness_schedule.P_HIGH, now=100.0)
    awareness_schedule.note(OTHER_CHAT, awareness_schedule.P_LOW, now=100.0)
    assert awareness_schedule.size() == 2
    awareness_schedule.reset()
    assert awareness_schedule.size() == 0


# ── The failure matrix (A–P) ──────────────────────────────────────────────
def test_the_failure_matrix_a_to_p(monkeypatch):
    """Every way the seam can be asked a question it cannot answer.

    The safe direction is always *read*: a hint that is missing, stale,
    malformed or refused defers nothing, and a room whose evidence says it needs
    reading is never held back. Each row is built from the public functions, so
    the matrix tests the shipped behaviour rather than a description of it.

    A no hint at all            → read
    B hint expired              → read
    C ``P_NONE`` passed to note → refused, so read
    D an unknown class          → refused, so read
    E a malformed room id       → read, and never raises
    F a live HIGH hint          → read
    G a live LOW hint, just arrived      → wait
    H a live LOW hint, waited < bound    → wait
    I a live LOW hint, waited == bound   → read
    J a live LOW hint, waited > bound    → read
    K one room's hint, another room asks → read
    L a hint forgotten by a pass         → read
    M a LOW hint raised to HIGH          → read
    N a LOW hint refreshed by later chatter → wait, and expires from the refresh
    O retention mis-set to zero          → bound floors at 1s, so it still reads
    P retention widened                  → the bound follows it
    """
    bound = awareness_schedule._bound()
    outcomes: dict[str, tuple[bool, bool]] = {}

    def run(letter, expected, *, setup=None, waited=1.0, ask=CHAT, now=100.0):
        awareness_schedule.reset()
        if setup is not None:
            setup()
        outcomes[letter] = (awareness_schedule.defer(ask, waited=waited, now=now), expected)

    low = lambda: awareness_schedule.note(  # noqa: E731 - a one-line scenario
        CHAT, awareness_schedule.P_LOW, now=100.0
    )
    high = lambda: awareness_schedule.note(  # noqa: E731
        CHAT, awareness_schedule.P_HIGH, now=100.0
    )

    run("A", False)
    run("B", False, setup=low, now=100.0 + bound + 1)
    run(
        "C",
        False,
        setup=lambda: awareness_schedule.note(CHAT, awareness_schedule.P_NONE, now=100.0),
    )
    run(
        "D",
        False,
        setup=lambda: awareness_schedule.note(CHAT, "urgent-ish", now=100.0),
    )
    run("E", False, ask=None)
    run("F", False, setup=high)
    run("G", True, setup=low, waited=0.0)
    run("H", True, setup=low, waited=bound - 1)
    run("I", False, setup=low, waited=bound)
    run("J", False, setup=low, waited=bound + 1)
    run("K", False, setup=low, ask=OTHER_CHAT)
    run("L", False, setup=lambda: (low(), awareness_schedule.forget(CHAT)))
    run("M", False, setup=lambda: (low(), high()))
    run(
        "N",
        True,
        setup=lambda: (
            awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0),
            awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=200.0),
        ),
        waited=1.0,
        now=200.0,
    )
    run(
        "N2",
        False,
        setup=lambda: (
            awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=100.0),
            awareness_schedule.note(CHAT, awareness_schedule.P_LOW, now=200.0),
        ),
        waited=1.0,
        now=200.0 + bound + 1,
    )
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 0)
    run("O", False, setup=low, waited=1.0)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_RETENTION_SECONDS", 7200)
    run("P", True, setup=low, waited=100.0)

    wrong = {letter: value for letter, value in outcomes.items() if value[0] is not value[1]}
    assert not wrong, wrong
