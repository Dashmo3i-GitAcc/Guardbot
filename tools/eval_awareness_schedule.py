#!/usr/bin/env python3
"""Score awareness *scheduling*: which room gets the next of 200 requests.

Why this exists
---------------
Increment U claims one thing and nothing else: that **declining to spend a
rationed awareness pass on a room whose unread batch carries no evidence that
it needs the room** makes the fixed daily allowance go further, without
starving any room. "Useful" has to be measurable before it can be claimed, so
this harness measures it — deterministically, offline, with no Telegram and no
model.

It also records the mechanism that was *rejected*, because the negative result
is the reason the shipped one exists. The first candidate was **ordering** the
pending list by class before the pass loop walked it. The scheduler turned out
to be event-driven *per room*, not batch-driven — each room is offered a pass
on its own debounce deadline and admitted or refused by its own share of the
allowance — so at any instant there is about one candidate and nothing to
sort. Measured over eight seeds, ordering changed the outcome by exactly zero
passes. ``--compare-ordering`` reproduces that. The shipped mechanism is the
other half: *spend, or wait?* — ``awareness_schedule.defer``, which is a
per-room decision and therefore has leverage the list order never had.

What it measures, and what it deliberately does not
---------------------------------------------------
It measures **scheduling quality**, never language-model quality. A pass here
is "useful" when the batch it read contained a message whose meaning cannot be
resolved without the room's recent content — an anaphor («همونو بزن»), a bare
interrogative («چی؟»), a reply, an attachment, an instruction, or a message
that addressed the assistant. Those labels are **hand-authored per message** in
the workload below, from that definition; the scheduler never sees them. The
only thing compared is which rooms the two policies spend the passes on.

The simulation is the real policy, reproduced exactly and kept honest by
``tests/test_awareness_schedule_eval.py``:

* ``awareness.due`` is called, not copied — the debounce, the starvation
  ceiling and the minimum interval are the production ones.
* the allowance gap is the production formula (``main._awareness_allowance_gap``):
  ``max(floor, seconds_left_in_the_day / requests_left)``, so the day's 200
  requests are spent at the same pace the deployment spends them.
* the shipped decision is the production function: ``defer_fn`` is
  ``awareness_schedule.defer`` itself, asked on the same ordinary path and after
  the same gates as ``main._awareness_run_room`` asks it.
* both scheduling paths run: the one-second deadline tick (a room whose
  debounce expired, oldest deadline first) and the fifteen-second sweeper
  (bounded to ``NEXUS_AWARENESS_MAX_CHATS_PER_TICK`` runs), sharing one
  allowance, exactly as ``app/main.py`` runs them.

Baseline and U run over the **same** arrivals, the same day, the same
allowance and the same seeds. Only the scheduler differs.

    python tools/eval_awareness_schedule.py
    python tools/eval_awareness_schedule.py --json
    python tools/eval_awareness_schedule.py --seeds 8
    python tools/eval_awareness_schedule.py --compare-ordering
    python tools/eval_awareness_schedule.py --seed 7 --day 86400
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The same offline defaults every tool in this directory sets, so importing the
# application cannot read a real deployment's environment or touch its database.
os.environ.setdefault("BOT_TOKEN", "eval-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-eval-schedule")
os.environ.setdefault("GEMINI_KEY_STORE_PATH", "/tmp/guardbot-eval-schedule/keys.json")

from app import awareness, awareness_schedule, config  # noqa: E402


# ── The workload ──────────────────────────────────────────────────────────
# One message, with the structural facts the capture path stores and the
# hand-authored label the scheduler is never shown.
@dataclass
class Msg:
    at: float
    text: str
    directed: bool = False
    reply: bool = False
    media: bool = False
    # The ground truth: does understanding this message need the room's recent
    # content? Authored from that definition, per message, by hand.
    depends: bool = False


@dataclass
class Room:
    chat_id: int
    shape: str
    msgs: list[Msg] = field(default_factory=list)


# The message shapes the ten room types are built from. Each carries its own
# label, so a shape's label is decided once, where the words are, rather than
# by a rule the scheduler could share.
_DEPENDENT = [
    # An anaphor: "that same one" has no antecedent inside the sentence.
    ("همونو بزن", True, False),
    ("اینو سکوت کن", True, False),
    ("بازش کن", True, False),
    ("اون کاربر رو محدود کن", True, False),
    # A bare interrogative: no content word to answer.
    ("چی؟", True, False),
    # A reply: the referent is the edge.
    ("باشه، همین کار رو بکن", True, True),
    ("درسته، ادامه بده", True, True),
    # An instruction: who or what it means is the room reading's job.
    ("پاکش کن", True, False),
    ("بنش کن", True, False),
]

_INDEPENDENT = [
    ("قیمت دلار امروز چنده؟", False, False),
    ("من دیروز رفتم سفر", False, False),
    ("هوا امروز خیلی خوبه", False, False),
    ("سلام", False, False),
    ("ممنون ازت", False, False),
    ("خب پس فردا میبینمت", False, False),
    ("لینک مقاله رو پیدا کردم", False, False),
    ("جلسه ساعت ده برگزار میشه", False, False),
]

# A task transition — «ادامه بده», «تمومه» — is deliberately **not** labelled
# dependent. It is the person's own thread with the assistant, carried by the
# conversation and by State; it does not make the *room* worth reading. The hint
# does not always agree: «ادامه بده» is read as an *instruction* by
# ``discourse.read_act`` and so classifies as HIGH, which is a false positive
# against this label. That disagreement is left in rather than smoothed away,
# for two reasons — it is the safe direction (a false HIGH costs one pass; a
# false LOW would defer a room that needed reading), and a precision cost that
# is tuned out of the workload cannot be seen in the result. The ``task_state``
# room's row in the per-shape table is where this cost shows up.
_TASK = [
    "بریم سراغ پروژه پایتون",
    "ادامه بده",
    "خب تمومه",
    "یه کار جدید شروع کنیم",
]


def _spread(rng: random.Random, count: int, start: float, end: float) -> list[float]:
    """``count`` arrival times in ``[start, end)``, sorted, deterministic."""
    if count <= 0:
        return []
    return sorted(rng.uniform(start, end) for _ in range(count))


def _fill(
    rng: random.Random,
    chat_id: int,
    shape: str,
    count: int,
    start: float,
    end: float,
    pool: list[tuple],
) -> Room:
    room = Room(chat_id=chat_id, shape=shape)
    for at, (text, depends, reply) in zip(
        _spread(rng, count, start, end), (pool[i % len(pool)] for i in range(count))
    ):
        room.msgs.append(Msg(at=at, text=text, reply=reply, depends=depends))
    return room


def build_workload(*, seed: int = 20260924, day: float = 86400.0) -> list[Room]:
    """The ten room shapes the brief names, as labelled arrivals over one day.

    Every shape is a *rate*, not a script: arrivals are uniform over the day
    with a fixed seed, so the workload is reproducible and the two policies see
    identical traffic. ``chat_id`` is negative (a Telegram group) and ascending
    in the order the rooms are built, which is what makes the baseline's
    ordering "the order the database happened to return" — the same thing the
    un-ordered ``db.group_pending`` produces.
    """
    rng = random.Random(seed)
    base = -1001000000000
    rooms: list[Room] = []
    n = 0

    def cid() -> int:
        nonlocal n
        n += 1
        return base - n

    # 1. quiet — nothing to read, ever.
    rooms.append(Room(chat_id=cid(), shape="quiet"))

    # 2. busy but context-independent — the room that produces the most
    #    messages and needs the fewest readings.
    rooms.append(
        _fill(rng, cid(), "busy_independent", 60, 0, day, _INDEPENDENT)
    )

    # 3. repeated short follow-ups — every one needs the room.
    rooms.append(_fill(rng, cid(), "short_followups", 24, 0, day, _DEPENDENT))

    # 4. replies and anaphora.
    rooms.append(
        _fill(
            rng,
            cid(),
            "replies_anaphora",
            24,
            0,
            day,
            [m for m in _DEPENDENT if m[2]] or _DEPENDENT,
        )
    )

    # 5. directly addressing Nexus.
    room = Room(chat_id=cid(), shape="addressed")
    for at in _spread(rng, 20, 0, day):
        room.msgs.append(Msg(at=at, text="نکسوس اینو ببین", directed=True, depends=True))
    rooms.append(room)

    # 6. an active task — measured, and labelled not-dependent.
    room = Room(chat_id=cid(), shape="task_state")
    for index, at in enumerate(_spread(rng, 20, 0, day)):
        room.msgs.append(Msg(at=at, text=_TASK[index % len(_TASK)], depends=False))
    rooms.append(room)

    # 7. bursty — five bursts of eight, half of them dependent.
    room = Room(chat_id=cid(), shape="bursty")
    for burst in range(5):
        start = day * burst / 5.0
        for index, at in enumerate(_spread(rng, 8, start, start + 240)):
            text, depends, reply = (_DEPENDENT if index % 2 else _INDEPENDENT)[
                index % len(_DEPENDENT if index % 2 else _INDEPENDENT)
            ]
            room.msgs.append(Msg(at=at, text=text, reply=reply, depends=depends))
    rooms.append(room)

    # 8. many rooms active at once.
    for index in range(8):
        pool = _DEPENDENT if index % 2 else _INDEPENDENT
        rooms.append(_fill(rng, cid(), "many_active", 20, 0, day, pool))

    # 9. one room that never stops — the hog. Every message is
    #    context-independent, so every pass it takes is a pass wasted.
    rooms.append(
        _fill(rng, cid(), "one_constant", 300, 0, day, _INDEPENDENT)
    )

    # 10. many rooms with sparse activity, all dependent.
    for _ in range(12):
        rooms.append(_fill(rng, cid(), "sparse", 3, 0, day, _DEPENDENT))

    return rooms


# ── The orderings ─────────────────────────────────────────────────────────
def baseline_order(rows: list[dict], *, now: float, **_) -> list[dict]:
    """The ordering before U: whatever ``db.group_pending`` returned.

    ``group_pending`` has no ``ORDER BY``, so this is the rows in the order the
    caller built them — which is what the sweep iterated before this increment.
    """
    return list(rows)


def priority_order(rows: list[dict], *, now: float, hint_now=None, **_) -> list[dict]:
    """The *rejected* mechanism: sort the pending list by class, then by wait.

    Kept here rather than in ``app/`` because the measurement is the reason it
    is not in ``app/``: ordering the candidate list changes the outcome by
    exactly nothing (see the module docstring), and shipping inert code is
    worse than recording why it was dropped. It is reproducible with
    ``--compare-ordering``.
    """
    def key(row: dict) -> tuple:
        chat_id = int(row.get("chat_id") or 0)
        oldest = float(row.get("oldest_at") or 0)
        waited = max(0.0, float(now) - oldest) if oldest else 0.0
        rank = awareness_schedule.RANK.get(
            awareness_schedule.priority(chat_id, now=hint_now), 0
        )
        return (-rank, -waited, chat_id)

    return sorted(rows, key=key)


# ── The simulation ────────────────────────────────────────────────────────
@dataclass
class Result:
    order: str
    passes: int = 0
    useful: int = 0
    per_room: dict = field(default_factory=dict)
    served_rooms: int = 0
    starved_rooms: int = 0
    max_wait_s: float = 0.0
    waits: list[float] = field(default_factory=list)
    decide_ms: list[float] = field(default_factory=list)
    calls: int = 0
    dependent_rooms: int = 0
    unread_dependent: int = 0

    def summary(self) -> dict:
        waits = sorted(self.waits)
        return {
            "order": self.order,
            "passes": self.passes,
            "useful_passes": self.useful,
            "useful_pct": round(100.0 * self.useful / self.passes, 1)
            if self.passes
            else 0.0,
            "served_rooms": self.served_rooms,
            "starved_rooms": self.starved_rooms,
            "unread_dependent_msgs": self.unread_dependent,
            "max_wait_s": round(self.max_wait_s, 1),
            "p50_wait_s": round(statistics.median(waits), 1) if waits else 0.0,
            "p95_wait_s": round(_pct(waits, 95), 1) if waits else 0.0,
            "decide_ms_p50": round(statistics.median(self.decide_ms), 3)
            if self.decide_ms
            else 0.0,
            "decide_ms_p95": round(_pct(self.decide_ms, 95), 3)
            if self.decide_ms
            else 0.0,
            "model_calls": self.calls,
        }


def _pct(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int(round(percent / 100.0 * (len(values) - 1))))
    return values[index]


def simulate(
    rooms: list[Room],
    *,
    order_fn,
    defer_fn=None,
    day: float = 86400.0,
    budget: int = 0,
    debounce: float = 0.0,
    max_wait: float = 0.0,
    min_interval: float = 0.0,
    tick: float = 0.0,
    deadline_tick: float = 1.0,
    max_chats_per_tick: int = 0,
    label: str = "",
) -> Result:
    """Run one day of the real policy under one scheduler.

    The loop is ``app/main.py``'s, at the granularity that matters: the
    one-second deadline tick first (a room whose debounce expired), then the
    fifteen-second sweeper (the candidate list, bounded to
    ``max_chats_per_tick`` *runs*). Both share ``last_pass`` and the spent
    allowance, so neither can spend the other's request.

    ``awareness.due`` is the production function, the gap is the production
    formula, and ``defer_fn`` is the production ``awareness_schedule.defer``
    when U is being measured — so the thing under test is the thing that ships.
    With ``defer_fn=None`` this is the baseline: the same loop with no spend
    decision. The only difference between the two runs is the scheduler.

    The workload has no pending admin confirmations, so ``defer`` is called
    with ``waiting`` at its default. That exception — a room the server is
    waiting on is never deferred — is a correctness rule rather than a
    scheduling one, and it is proved on the real path by
    ``tests/test_awareness_schedule_eval.py``.
    """
    budget = budget or max(1, int(config.NEXUS_AWARENESS_DAILY_LIMIT))
    debounce = debounce or float(config.NEXUS_AWARENESS_DEBOUNCE_SECONDS)
    max_wait = max_wait or float(config.NEXUS_AWARENESS_MAX_WAIT_SECONDS)
    min_interval = min_interval or float(config.NEXUS_AWARENESS_MIN_INTERVAL_SECONDS)
    tick = tick or float(config.NEXUS_AWARENESS_TICK_SECONDS)
    max_chats_per_tick = max_chats_per_tick or max(
        1, int(config.NEXUS_AWARENESS_MAX_CHATS_PER_TICK)
    )

    # The enabled gate is part of the policy; the simulation runs the layer on.
    enabled_was = awareness._running
    awareness._running = True
    awareness_schedule.reset()

    result = Result(order=label)
    cursor = {room.chat_id: 0 for room in rooms}
    last_pass: dict[int, float] = {}
    rearm: dict[int, float] = {}
    by_id = {room.chat_id: room for room in rooms}
    arrivals = {room.chat_id: [msg.at for msg in room.msgs] for room in rooms}

    def arrived_count(chat_id: int, now: float) -> int:
        """How many of this room's messages have arrived by ``now``.

        The one thing that makes this a simulation rather than a replay: the
        whole day's arrivals are generated up front, so every read of the room
        must be bounded by the clock or the scheduler would see messages from
        the future and never find a room quiet.
        """
        return bisect.bisect_right(arrivals[chat_id], now)

    def unread_of(room: Room, now: float) -> list[Msg]:
        return room.msgs[cursor[room.chat_id] : arrived_count(room.chat_id, now)]

    def row_of(room: Room, now: float) -> dict:
        unread = unread_of(room, now)
        return {
            "chat_id": room.chat_id,
            "oldest_at": int(unread[0].at),
            "newest_at": int(unread[-1].at),
            "max_id": cursor[room.chat_id] + len(unread),
            "pending": len(unread),
        }

    def deadline_of(room: Room, now: float) -> float | None:
        """The debounce deadline, armed exactly as ``_awareness_schedule`` arms it.

        Newest *arrived* unread message plus the debounce, pushed out by every
        later one — and, once the tick has tried and refused the room, the
        brake's own re-arm on top of it. ``None`` when nothing has arrived that
        is unread, which is the same as "no deadline left to meet".
        """
        unread = unread_of(room, now)
        if not unread:
            return None
        return max(unread[-1].at + debounce, rearm.get(room.chat_id, 0.0))

    def gap(now: float) -> float:
        remaining = budget - result.passes
        left = max(0.0, day - now)
        if remaining <= 0:
            return max(min_interval, left)
        return max(min_interval, left / remaining)

    def affordable(chat_id: int, now: float) -> bool:
        last = last_pass.get(chat_id, 0.0)
        if not last:
            return True
        return (now - last) >= gap(now)

    def run(room: Room, row: dict, now: float, *, urgent: bool = False) -> bool:
        """One pass. Returns whether it ran. Mirrors ``_awareness_run_room``.

        The gates are in the production order, because the order is the policy:
        ``due`` first (timing and safety), then the spend decision (U), then the
        allowance. A deferral returns ``False`` exactly like any other refusal,
        so the caller re-arms the room on its own brake — the same bounded wait
        the deployment gives it — and the room is offered again, still eligible,
        on its next deadline.
        """
        verdict = awareness.due(
            row,
            now=now,
            last_pass_at=last_pass.get(room.chat_id, 0.0),
            urgent=urgent,
        )
        if not verdict:
            return False
        if not urgent and defer_fn is not None:
            oldest = float(row.get("oldest_at") or 0)
            if oldest and defer_fn(room.chat_id, waited=now - oldest, now=now):
                return False
        if not affordable(room.chat_id, now):
            return False
        unread = unread_of(room, now)
        for msg in unread:
            if msg.depends:
                result.waits.append(max(0.0, now - msg.at))
        useful = any(msg.depends for msg in unread)
        cursor[room.chat_id] += len(unread)
        last_pass[room.chat_id] = now
        rearm.pop(room.chat_id, None)
        # The batch is read, so the hint it produced is spent — the same
        # ``forget`` the real pass does in ``_awareness_run_room``.
        awareness_schedule.forget(room.chat_id)
        result.passes += 1
        result.calls += 1
        if useful:
            result.useful += 1
        result.per_room[room.chat_id] = result.per_room.get(room.chat_id, 0) + 1
        return True

    try:
        # The hint store is filled **as the day runs**, not up front: a hint is
        # evidence about messages that have arrived, and seeding the whole day
        # first would let the scheduler see a message from the future. This is
        # the capture path, one message at a time, in arrival order.
        noted = {room.chat_id: 0 for room in rooms}

        def capture(now: float) -> None:
            for room in rooms:
                upto = arrived_count(room.chat_id, now)
                for msg in room.msgs[noted[room.chat_id] : upto]:
                    awareness_schedule.note(
                        room.chat_id,
                        awareness_schedule.read(
                            msg.text,
                            reply=msg.reply,
                            media=msg.media,
                            directed=msg.directed,
                        ),
                        now=msg.at,
                    )
                noted[room.chat_id] = upto

        # The clock. Deadlines are compared against arrival stamps, so the day
        # is walked in one-second steps while only rooms with something unread
        # are ever examined.
        step = min(deadline_tick, tick)
        sweep_every = max(1, int(round(tick / step)))
        ticks = 0
        now = 0.0
        while now <= day and result.passes < budget:
            capture(now)
            active = [c for c in cursor if cursor[c] < arrived_count(c, now)]
            if active:
                # 1. the deadline tick: every expired deadline, oldest first —
                #    then reordered by the scheduler under test. A room that is
                #    refused (including deferred) is re-armed at the brake, the
                #    same bounded wait ``_awareness_deadline_tick`` gives it.
                expired = []
                for chat_id in active:
                    due_at = deadline_of(by_id[chat_id], now)
                    if due_at is not None and due_at <= now:
                        expired.append((due_at, chat_id))
                expired.sort()
                if expired:
                    rows = [row_of(by_id[chat_id], now) for _, chat_id in expired]
                    ordered = order_fn(rows, now=now, hint_now=now)
                    for row in ordered:
                        if result.passes >= budget:
                            break
                        chat_id = int(row["chat_id"])
                        if run(by_id[chat_id], row, now):
                            continue
                        rearm[chat_id] = now + min_interval

                # 2. the sweeper: the ordered list, bounded to N *runs*. It does
                #    not re-arm — the deadline tick owns that, as in production.
                ticks += 1
                if ticks % sweep_every == 0:
                    # Re-read the pending set: the deadline tick above may have
                    # consumed a room entirely in this same second.
                    rows = [
                        row_of(by_id[c], now)
                        for c in active
                        if cursor[c] < arrived_count(c, now)
                    ]
                    started = time.perf_counter()
                    ordered = order_fn(rows, now=now, hint_now=now)
                    result.decide_ms.append(
                        (time.perf_counter() - started) * 1000.0
                    )
                    done = 0
                    for row in ordered:
                        if done >= max_chats_per_tick or result.passes >= budget:
                            break
                        if run(by_id[int(row["chat_id"])], row, now):
                            done += 1
            now += step

        # Starvation is the property the brief names: a room that had a message
        # needing the room and was never read at all. A room still holding a
        # dependent message at the end of the day is counted too, because the
        # allowance running out is exactly the failure being guarded against.
        for room in rooms:
            unread = unread_of(room, day)
            if not any(msg.depends for msg in unread):
                continue
            result.unread_dependent += sum(1 for msg in unread if msg.depends)
            if not result.per_room.get(room.chat_id):
                result.starved_rooms += 1
        result.dependent_rooms = sum(
            1 for room in rooms if any(msg.depends for msg in room.msgs)
        )
        result.served_rooms = len(result.per_room)
        result.max_wait_s = max(result.waits) if result.waits else 0.0
    finally:
        awareness._running = enabled_was
        awareness_schedule.reset()
    return result


def compare(
    *,
    seed: int = 20260924,
    day: float = 86400.0,
    compare_ordering: bool = False,
) -> dict:
    """Run the same workload under baseline and U, and report the difference.

    ``compare_ordering`` additionally runs the rejected mechanism — the same
    loop with the pending list sorted by class and *no* deferral — so the
    negative result that motivated U is reproducible from the same command.
    """
    workload = build_workload(seed=seed, day=day)
    baseline = simulate(workload, order_fn=baseline_order, day=day, label="baseline")
    adaptive = simulate(
        workload,
        # Ordering is not the mechanism: the shipped scheduler spends, or waits,
        # per room, on the order the database returned. Keeping the ordering
        # identical is what makes the comparison a comparison of U alone.
        order_fn=baseline_order,
        defer_fn=awareness_schedule.defer,
        day=day,
        label="U",
    )
    report = {
        "rooms": len(workload),
        "shapes": sorted({room.shape for room in workload}),
        "messages": sum(len(room.msgs) for room in workload),
        "dependent_messages": sum(
            1 for room in workload for msg in room.msgs if msg.depends
        ),
        "dependent_rooms": adaptive.dependent_rooms,
        "seed": seed,
        "day": day,
        "baseline": baseline.summary(),
        "adaptive": adaptive.summary(),
        "per_shape": _per_shape(workload, baseline, adaptive),
    }
    if compare_ordering:
        rejected = simulate(
            workload,
            order_fn=priority_order,
            day=day,
            label="order-only",
        )
        report["order_only"] = rejected.summary()
    return report


def _per_shape(workload: list[Room], baseline: Result, adaptive: Result) -> dict:
    out: dict[str, dict] = {}
    for room in workload:
        entry = out.setdefault(
            room.shape, {"rooms": 0, "baseline": 0, "adaptive": 0}
        )
        entry["rooms"] += 1
        entry["baseline"] += baseline.per_room.get(room.chat_id, 0)
        entry["adaptive"] += adaptive.per_room.get(room.chat_id, 0)
    return out


# ── Multi-seed aggregation ────────────────────────────────────────────────
_MEAN_KEYS = (
    "passes",
    "useful_passes",
    "useful_pct",
    "served_rooms",
    "starved_rooms",
    "unread_dependent_msgs",
    "max_wait_s",
    "p95_wait_s",
)


def aggregate(seeds: list[int], *, day: float = 86400.0, compare_ordering: bool = False) -> dict:
    """Mean of the per-seed summaries, for the report's stability claim.

    A single seed can flatter a scheduler by accident; the claim in the report
    is about the mechanism, so it is measured over several workloads and
    reported as a mean with the worst seed shown alongside it.
    """
    reports = [
        compare(seed=seed, day=day, compare_ordering=compare_ordering) for seed in seeds
    ]
    out: dict = {"seeds": seeds, "count": len(seeds)}
    for name in ("baseline", "adaptive", "order_only"):
        rows = [r[name] for r in reports if name in r]
        if not rows:
            continue
        out[name] = {
            "mean": {k: round(statistics.mean(r[k] for r in rows), 1) for k in _MEAN_KEYS},
            "worst_useful_pct": min(r["useful_pct"] for r in rows),
            "worst_starved_rooms": max(r["starved_rooms"] for r in rows),
        }
    return out


# ── CLI ───────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--day", type=float, default=86400.0)
    parser.add_argument(
        "--seeds",
        type=int,
        default=0,
        help="aggregate over this many seeds (1..N) instead of one",
    )
    parser.add_argument(
        "--compare-ordering",
        action="store_true",
        help="also run the rejected order-only mechanism",
    )
    args = parser.parse_args()

    if args.seeds:
        report = aggregate(
            list(range(1, args.seeds + 1)),
            day=args.day,
            compare_ordering=args.compare_ordering,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        print(f"AWARENESS SCHEDULING — mean of {report['count']} seeds")
        header = f"  {'':22}{'baseline':>12}{'U':>12}"
        if "order_only" in report:
            header += f"{'order-only':>12}"
        print(header)
        columns = ["baseline", "adaptive"] + (
            ["order_only"] if "order_only" in report else []
        )
        for key in _MEAN_KEYS:
            line = f"  {key:22}"
            for name in columns:
                line += f"{report[name]['mean'][key]:>12}"
            print(line)
        for name in columns:
            print(
                f"  {name:22} worst useful%={report[name]['worst_useful_pct']}"
                f"  worst starved={report[name]['worst_starved_rooms']}"
            )
        return 0

    report = compare(
        seed=args.seed, day=args.day, compare_ordering=args.compare_ordering
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    base, adapt = report["baseline"], report["adaptive"]
    print("AWARENESS SCHEDULING — one simulated day, one fixed allowance")
    print(
        f"  rooms {report['rooms']} ({report['dependent_rooms']} with a "
        f"dependent message)  messages {report['messages']}  "
        f"dependent {report['dependent_messages']}  seed {report['seed']}"
    )
    print()
    header = f"  {'':22}{'baseline':>12}{'U':>12}"
    if "order_only" in report:
        header += f"{'order-only':>12}"
    print(header)
    for key, label in (
        ("passes", "passes"),
        ("useful_passes", "useful passes"),
        ("useful_pct", "useful %"),
        ("served_rooms", "rooms served"),
        ("starved_rooms", "rooms starved"),
        ("unread_dependent_msgs", "dependent unread"),
        ("max_wait_s", "max wait (s)"),
        ("p50_wait_s", "p50 wait (s)"),
        ("p95_wait_s", "p95 wait (s)"),
        ("decide_ms_p50", "decide p50 (ms)"),
        ("decide_ms_p95", "decide p95 (ms)"),
        ("model_calls", "model calls"),
    ):
        line = f"  {label:22}{base[key]:>12}{adapt[key]:>12}"
        if "order_only" in report:
            line += f"{report['order_only'][key]:>12}"
        print(line)
    print()
    print("  PASSES BY ROOM SHAPE")
    for shape, entry in sorted(report["per_shape"].items()):
        print(
            f"    {shape:20} rooms={entry['rooms']:<3} "
            f"baseline={entry['baseline']:<5} U={entry['adaptive']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
