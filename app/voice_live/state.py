"""The states one live call moves through, and which moves are legal.

A call is not a function call, so it cannot be described by a return value. It
is a sequence of conditions a long-lived object is *in*, and almost every bug
this feature can have is a move between two of them that should not have been
possible. Nexus starts speaking while it is still listening. A reconnect is
begun for a session that was already left. A barge-in interrupts a turn that had
already finished, and the interrupt then swallows the answer to the *next*
question. None of those is a crash; all of them are a conversation that behaves
strangely for reasons nobody can see.

So the states are a closed set, the moves are a table, and the table is data
rather than a chain of ``if`` statements — the same choice ``app/awareness.py``
makes about its context sources, and for the same reason: a rule that is a table
can be read in one place and tested without constructing the thing it governs.

Two decisions worth stating, because both could reasonably have gone the other
way:

**An illegal move is refused, not raised.** This machine runs inside audio
callbacks and provider event loops, where an exception does not surface as a
failed test — it surfaces as a dropped call, or as a task that dies quietly and
leaves a voice channel open. So ``go`` logs the attempt with both states and
stays where it is. The *table* is what tests assert against, which is where an
illegal move should be caught: at the rule, in a test, rather than in
production at the moment it matters.

**``INTERRUPTED`` is a state and not a flag.** A barge-in is not "speaking, but
with a flag set"; it is a condition with its own legal exits, and the one that
matters is that it goes to ``LISTENING`` — the human who interrupted is *still
talking*, and their audio is already arriving. Modelling it as a flag is how a
barge-in comes to be handled by stopping playback and then waiting for an
utterance that the detection logic has already decided is over.

What this module does not do: it does not decide *when* to move. It says which
moves exist, and the session decides which one is happening. Keeping the
decision out of here is what makes the table testable in isolation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("guardbot.voice.state")

# ── The states ────────────────────────────────────────────────────────────
# The feature is off. Nothing is joined, nothing is polled, and the process
# holds no provider session. This is the state a deployment that has not opted
# in stays in for its whole life.
DISABLED = "disabled"
# The feature is on and no call is in progress. The resting state.
IDLE = "idle"
# Joining the Telegram voice chat and opening the provider session. Both, and
# the state is not left until both are up: a call with media but no model would
# listen to people and answer nobody, which is worse than not joining.
JOINING = "joining"
# Both ends are up and the call is quiet. Waiting.
CONNECTED = "connected"
# Somebody is speaking and their audio is being forwarded.
LISTENING = "listening"
# The utterance ended and the model is producing an answer.
THINKING = "thinking"
# The answer is being played into the call.
SPEAKING = "speaking"
# Playback stopped because a person started talking over it. Their audio is
# arriving; the model's turn has been abandoned.
INTERRUPTED = "interrupted"
# The provider session dropped and is being restored. The call is still joined.
RECONNECTING = "reconnecting"
# Shutting down: playback stopped, streams closed, leaving the call.
LEAVING = "leaving"
# This session is over and cannot be resumed. Terminal until cleanup returns the
# machine to ``IDLE`` or the operator starts a new call.
FAILED = "failed"

STATES = (
    DISABLED,
    IDLE,
    JOINING,
    CONNECTED,
    LISTENING,
    THINKING,
    SPEAKING,
    INTERRUPTED,
    RECONNECTING,
    LEAVING,
    FAILED,
)

# Persian labels, kept beside the vocabulary rather than in ``app/config.py``,
# for the same reason ``app/rbac.py`` keeps its permission labels there: these
# are the names of these exact keys, and a state added without a label would
# render as a raw English key in the owner's status line.
STATE_LABELS = {
    DISABLED: "خاموش",
    IDLE: "آماده",
    JOINING: "در حال ورود",
    CONNECTED: "در تماس",
    LISTENING: "دارد گوش می‌دهد",
    THINKING: "دارد فکر می‌کند",
    SPEAKING: "دارد صحبت می‌کند",
    INTERRUPTED: "حرفش قطع شد",
    RECONNECTING: "در حال اتصال دوباره",
    LEAVING: "در حال خروج",
    FAILED: "ناموفق",
}


def label(state: str) -> str:
    """The Persian label for a state, or the key itself if it is unknown.

    Falls back to the key rather than to a placeholder because an unknown state
    is a bug worth seeing, and ``"?"`` would hide which one.
    """
    return STATE_LABELS.get(state, state)


# ── The table ─────────────────────────────────────────────────────────────
# Every legal move, stated once. A state not listed as a key can be entered but
# never left, which is the correct reading for a terminal state — and writing it
# as an omission rather than as an empty tuple keeps the two cases from looking
# alike.
_TRANSITIONS: dict[str, frozenset[str]] = {
    # Turning the feature on or off. ``DISABLED`` is reachable from anywhere a
    # call is not in flight, because the operator switching the feature off must
    # always be honoured; a call in flight is left first, so ``LEAVING`` reaches
    # it too.
    DISABLED: frozenset({IDLE}),
    IDLE: frozenset({JOINING, DISABLED}),
    # Both ends must come up, or the attempt fails. ``LEAVING`` is reachable
    # because the owner can cancel a join that is taking too long.
    JOINING: frozenset({CONNECTED, LEAVING, FAILED}),
    # Quiet and waiting. It is left when somebody speaks (LISTENING) — but also
    # when the *model* begins, which happens without the session having observed
    # an utterance at all: context injected between turns, a tool result, or a
    # provider that reports audio before it reports a transcript. Leaving those
    # edges out made the session attempt `connected -> speaking` on the first
    # answer of every call, and the machine refused it while the audio played
    # anyway — a state that disagreed with the call it described.
    CONNECTED: frozenset(
        {LISTENING, THINKING, SPEAKING, LEAVING, RECONNECTING, FAILED}
    ),
    # Speech in. It ends one of three ways: the utterance completes and the
    # model is asked (THINKING); the speaker stops without a complete utterance
    # (CONNECTED); or the provider drops (RECONNECTING). A barge-in while Nexus
    # is silent is not a barge-in, so INTERRUPTED is not reachable from here.
    # SPEAKING is, because the model can start answering while this side still
    # believes the person is talking — the transcript marker arrives late or not
    # at all, and the audio is the fact.
    LISTENING: frozenset(
        {THINKING, SPEAKING, CONNECTED, LEAVING, RECONNECTING, FAILED}
    ),
    # The model is producing. It may produce speech (SPEAKING) or produce
    # nothing worth saying (CONNECTED) — a turn that ends with no audio is
    # normal, and treating it as a failure would end calls for no reason.
    THINKING: frozenset({SPEAKING, CONNECTED, LEAVING, RECONNECTING, FAILED}),
    # Playback. Ends when the turn completes, or when somebody talks over it.
    SPEAKING: frozenset(
        {CONNECTED, INTERRUPTED, LEAVING, RECONNECTING, FAILED}
    ),
    # The interrupter is still speaking, so the only two honest exits are
    # "listen to them" and "the provider dropped". ``CONNECTED`` is reachable
    # because the interrupter may also stop immediately — a cough, a door — and
    # then there is nothing to listen to.
    INTERRUPTED: frozenset(
        {LISTENING, CONNECTED, LEAVING, RECONNECTING, FAILED}
    ),
    # A reconnect either works, gives up, or is cancelled by the owner leaving.
    # It never goes straight to LISTENING: after a reconnect the session does not
    # know what is being said, so it waits in CONNECTED until speech is detected
    # again rather than assuming the pre-drop utterance is still in progress.
    RECONNECTING: frozenset({CONNECTED, LEAVING, FAILED}),
    LEAVING: frozenset({IDLE, FAILED}),
    # Terminal for the session. Cleanup returns it to IDLE so the next start is
    # a start and not a revival; the operator may also switch the feature off.
    # ``LEAVING`` is reachable because a failed session is still a session
    # holding a voice channel, and a teardown that could not move a failed
    # machine out of its own state would leave the call joined for ever — which
    # is the one outcome this subsystem exists to avoid.
    FAILED: frozenset({LEAVING, IDLE, DISABLED}),
}

# States that mean "a call is joined, or is being joined". This is what the
# concurrent-call ceiling counts, and it deliberately includes the transitional
# ones: a join that has been in progress for a minute is holding a slot.
CALL_STATES = frozenset(
    {JOINING, CONNECTED, LISTENING, THINKING, SPEAKING, INTERRUPTED,
     RECONNECTING, LEAVING}
)

# States from which a barge-in is meaningful: Nexus is making or about to make
# sound. Used by the session to decide whether incoming speech should cancel a
# turn, so that the rule is one fact rather than a condition written twice.
SPEAKING_STATES = frozenset({THINKING, SPEAKING})


def is_call_state(state: str) -> bool:
    """Whether a call occupies a slot in this state."""
    return state in CALL_STATES


def allows(from_state: str, to_state: str) -> bool:
    """Whether the table permits this move.

    Exposed so that tests can assert the *rule* rather than a sequence of moves
    that happens to exercise it. An unknown source state allows nothing, which is
    the safe direction: a state nobody declared is a bug, and a bug that permits
    arbitrary moves is one that hides itself.
    """
    return to_state in _TRANSITIONS.get(from_state, frozenset())


def destinations(from_state: str) -> frozenset[str]:
    """Every legal next state. For the error message and for the tests."""
    return _TRANSITIONS.get(from_state, frozenset())


# ── The machine ───────────────────────────────────────────────────────────
@dataclass
class Machine:
    """One session's position in the table, and how it got there.

    ``history`` is bounded because a call can run for hours and a barge-in-heavy
    conversation produces a move every few seconds; an unbounded list here would
    be a slow memory leak in exactly the workload that runs longest. The bound is
    generous enough to explain any incident and small enough not to matter.
    """

    state: str = DISABLED
    since: float = field(default_factory=time.time)
    reason: str = ""
    history: list[tuple[str, str, float]] = field(default_factory=list)

    HISTORY_LIMIT = 32

    def go(self, to_state: str, *, reason: str = "") -> bool:
        """Move, if the table allows it. Returns whether it moved.

        A refusal is logged at warning level rather than raised — see the module
        docstring. The log line carries both states, because "an illegal move was
        refused" is not actionable while "speaking -> joining was refused" is.
        """
        if to_state == self.state:
            # Not a move. Refused without a warning: several callers
            # legitimately re-assert the state they expect to be in (the media
            # callback announces "listening" on every frame batch), and logging
            # those would bury the real refusals.
            return False
        if not allows(self.state, to_state):
            log.warning(
                "voice session refused an illegal move %s -> %s (reason=%s)",
                self.state,
                to_state,
                reason or "-",
            )
            return False
        self._record(to_state, reason)
        return True

    def force(self, to_state: str, *, reason: str = "") -> None:
        """Move regardless of the table. For teardown and for tests only.

        Exists because cleanup must always be able to reach ``IDLE``: if the
        machine were wedged in a state the table cannot leave, an illegal-move
        refusal would leave the call joined for ever, and "we cannot clean up
        because the state machine says no" is not an acceptable answer for a
        subsystem that holds a voice channel.
        """
        if to_state == self.state:
            return
        self._record(to_state, reason or "forced")

    def _record(self, to_state: str, reason: str) -> None:
        now = time.time()
        self.history.append((self.state, to_state, now))
        if len(self.history) > self.HISTORY_LIMIT:
            del self.history[: len(self.history) - self.HISTORY_LIMIT]
        self.state = to_state
        self.since = now
        self.reason = reason or ""

    def elapsed(self, now: float | None = None) -> float:
        """Seconds spent in the current state."""
        return max(0.0, (now if now is not None else time.time()) - self.since)

    def trail(self, limit: int = 8) -> str:
        """The last few moves as ``a->b``, oldest first. For the log only."""
        recent = self.history[-limit:]
        return " ".join(f"{a}->{b}" for a, b, _ in recent) or "-"

    def describe(self) -> dict:
        """A safe summary: state, label, age, and the last reason. No text."""
        return {
            "state": self.state,
            "label": label(self.state),
            "seconds": round(self.elapsed(), 1),
            "reason": self.reason,
            "in_call": is_call_state(self.state),
        }
