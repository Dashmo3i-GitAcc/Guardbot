"""What a call counts, and how long each part of it took.

Two jobs, and they are separate on purpose.

**Counters** answer "is this working, and how much of it is happening" —
turns taken, barge-ins absorbed, reconnects survived, failures by reason. They
are cheap integers, incremented on hot paths, and they exist so that the owner's
status line can be answered without keeping a log of anything anybody said.

**The stopwatch** answers the question this feature was measured on before it
was designed: how long after somebody stops speaking does Nexus start? That
number decides whether a voice conversation feels like a conversation, and it is
the one quantity that cannot be reasoned about from the code — it was measured,
and the measurement overturned the obvious choice of model (the purpose-built
native-audio model was twice as slow as the general live one on real Persian
speech).

The stages are fixed and named, because a latency figure is meaningless without
saying which two moments it spans. ``utterance_end`` to ``first_audio`` is the
one that matters; ``first_audio`` to ``turn_complete`` is how long Nexus talked
for; ``connect_start`` to ``ready`` is how long a call takes to come up.

**Nothing here records text.** Not a transcript, not a prompt, not a reply — the
counters are integers, the stopwatch is timestamps, and ``describe`` returns
numbers. That is not a convention this module follows, it is the reason it has
no field that could hold a string: "no sensitive logging" is easier to keep when
the data structure cannot express the sensitive thing.

Process-wide totals are kept as well as per-session ones, and they are bounded
in the only way that matters: they are scalars. A session that ends adds its
counters to the totals and is forgotten, so an hour-long call and a one-minute
call leave the same footprint.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# ── The stages a latency figure can span ──────────────────────────────────
# Named constants rather than bare strings, because these names appear in the
# log, in the owner's report and in the tests, and a typo in one of those would
# produce a missing number rather than an error.
CONNECT_START = "connect_start"
READY = "ready"
UTTERANCE_START = "utterance_start"
UTTERANCE_END = "utterance_end"
FIRST_AUDIO = "first_audio"
TURN_COMPLETE = "turn_complete"
INTERRUPT = "interrupt"
DISCONNECT = "disconnect"

STAGES = (
    CONNECT_START,
    READY,
    UTTERANCE_START,
    UTTERANCE_END,
    FIRST_AUDIO,
    TURN_COMPLETE,
    INTERRUPT,
    DISCONNECT,
)

# The two spans worth reporting by name. The first is the conversation's own
# responsiveness; the second is how long it takes to get a call up at all.
SPAN_RESPONSE = (UTTERANCE_END, FIRST_AUDIO)
SPAN_CONNECT = (CONNECT_START, READY)
SPAN_TURN = (FIRST_AUDIO, TURN_COMPLETE)


class Stopwatch:
    """Timestamps for the stages of one call, and the gaps between them.

    Keeps the *last* mark for each stage rather than a list, for the same reason
    ``app/awareness.py``'s pass trace does: the interesting figure is always the
    most recent turn, and a call can take hundreds of them. An unbounded list
    here would grow for exactly as long as the workload that runs longest.

    ``gaps`` accumulates every completed span so that an average can be reported
    at the end of a call without keeping the individual samples. That is a
    running total and a count, not a series.
    """

    def __init__(self, clock=time.monotonic) -> None:
        # ``monotonic`` and not ``time.time``: a wall clock can step backwards
        # when the host syncs, and a negative latency in a report is worse than
        # no latency at all.
        self._clock = clock
        self.marks: dict[str, float] = {}
        self.gaps: dict[str, list[float]] = {}

    def mark(self, stage: str) -> float:
        """Record a stage now. Returns the timestamp."""
        now = self._clock()
        self.marks[stage] = now
        return now

    def gap(self, start: str, end: str) -> float | None:
        """Seconds between two marked stages, or None if either is missing.

        None rather than 0.0, and that distinction is load-bearing: a turn that
        never produced audio has *no* response latency, and reporting it as zero
        would drag every average down and make the feature look faster than it
        is. A missing measurement must not become a measurement of nothing.
        """
        a, b = self.marks.get(start), self.marks.get(end)
        if a is None or b is None:
            return None
        return max(0.0, b - a)

    def close(self, start: str, end: str) -> float | None:
        """Measure a span and add it to that span's running total."""
        value = self.gap(start, end)
        if value is not None:
            key = f"{start}->{end}"
            self.gaps.setdefault(key, []).append(value)
        return value

    def average(self, start: str, end: str) -> float | None:
        """Mean of every closed sample of a span. None when there are none."""
        samples = self.gaps.get(f"{start}->{end}") or []
        if not samples:
            return None
        return sum(samples) / len(samples)

    def forget(self, *stages: str) -> None:
        """Drop marks, so a turn's measurement cannot leak into the next turn.

        Called at the start of each turn. Without it a turn that never produced
        audio would report the *previous* turn's latency, which is a wrong
        number rather than a missing one — and a wrong number is worse, because
        nothing looks broken.
        """
        for stage in stages:
            self.marks.pop(stage, None)


@dataclass
class Metrics:
    """One session's counters. Plain integers, incremented on hot paths."""

    turns: int = 0
    utterances: int = 0
    barge_ins: int = 0
    reconnects: int = 0
    actions_requested: int = 0
    actions_refused: int = 0
    context_refreshes: int = 0
    frames_in: int = 0
    frames_out: int = 0
    seconds_in: float = 0.0
    seconds_out: float = 0.0
    failures: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    watch: Stopwatch = field(default_factory=Stopwatch)

    def failure(self, reason: str) -> None:
        """Count a failure by its machine reason. Never by its message."""
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def note_frames(self, *, inbound: int = 0, outbound: int = 0) -> None:
        """Count frames and derive seconds from the fixed frame duration.

        Seconds are computed rather than timed, so that a paused or stalled
        stream does not accrue "audio played" for time that passed with nothing
        in it.
        """
        from . import audio

        frame_seconds = audio.FRAME_MS / 1000.0
        if inbound:
            self.frames_in += inbound
            self.seconds_in += inbound * frame_seconds
        if outbound:
            self.frames_out += outbound
            self.seconds_out += outbound * frame_seconds

    def response_latency(self) -> float | None:
        """Mean seconds from end of utterance to first audio, over the call."""
        return self.watch.average(*SPAN_RESPONSE)

    def last_response_latency(self) -> float | None:
        return self.watch.gap(*SPAN_RESPONSE)

    def duration(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.started_at)

    def describe(self, now: float | None = None) -> dict:
        """A safe summary. Numbers and machine keys only — no text, no ids.

        The failure map is keyed by reason, which is a fixed vocabulary from
        ``errors``, so even the keys cannot carry anything a provider said.
        """
        return {
            "seconds": round(self.duration(now), 1),
            "turns": self.turns,
            "utterances": self.utterances,
            "barge_ins": self.barge_ins,
            "reconnects": self.reconnects,
            "actions_requested": self.actions_requested,
            "actions_refused": self.actions_refused,
            "context_refreshes": self.context_refreshes,
            "audio_in_seconds": round(self.seconds_in, 1),
            "audio_out_seconds": round(self.seconds_out, 1),
            "response_ms": _ms(self.response_latency()),
            "last_response_ms": _ms(self.last_response_latency()),
            "failures": dict(self.failures),
        }


def _ms(seconds: float | None) -> int | None:
    """Seconds to whole milliseconds, or None. None survives; 0 would not."""
    if seconds is None:
        return None
    return int(round(seconds * 1000))


# ── Process-wide totals ───────────────────────────────────────────────────
# Scalars only. A session adds to these when it ends and is then forgotten, so
# the footprint of an hour-long call and a one-minute call is identical.
_totals: dict[str, float] = {}
_latency_samples: list[float] = []
_LATENCY_SAMPLES_LIMIT = 64


def note_session(metrics: Metrics) -> None:
    """Fold a finished session into the process totals.

    The latency samples are kept as a short bounded list rather than a running
    mean, because a mean of means is not a mean: a call with two turns and one
    with two hundred should not weigh the same. The bound keeps the memory
    fixed while still being a real sample of recent calls.
    """
    _totals["sessions"] = _totals.get("sessions", 0) + 1
    _totals["seconds"] = _totals.get("seconds", 0.0) + metrics.duration()
    _totals["turns"] = _totals.get("turns", 0) + metrics.turns
    _totals["barge_ins"] = _totals.get("barge_ins", 0) + metrics.barge_ins
    _totals["reconnects"] = _totals.get("reconnects", 0) + metrics.reconnects
    _totals["audio_in_seconds"] = (
        _totals.get("audio_in_seconds", 0.0) + metrics.seconds_in
    )
    _totals["audio_out_seconds"] = (
        _totals.get("audio_out_seconds", 0.0) + metrics.seconds_out
    )
    for reason, count in metrics.failures.items():
        key = f"failure.{reason}"
        _totals[key] = _totals.get(key, 0) + count
    for start, end in (SPAN_RESPONSE, SPAN_CONNECT):
        value = metrics.watch.average(start, end)
        if value is not None:
            _latency_samples.append(value)
            del _latency_samples[:-_LATENCY_SAMPLES_LIMIT]


def totals() -> dict:
    """The process-wide counters, as a safe dict."""
    out = {key: round(value, 1) if isinstance(value, float) else value
           for key, value in _totals.items()}
    if _latency_samples:
        out["mean_response_ms"] = _ms(sum(_latency_samples) / len(_latency_samples))
    return out


def reset_state() -> None:
    """Clear the totals. For tests, and for the same reason every other reset in
    this codebase exists: a total that cannot be cleared makes a test depend on
    the order it ran in."""
    _totals.clear()
    _latency_samples.clear()
