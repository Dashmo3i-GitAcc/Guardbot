"""Instant media-flood tracker: rolling-window behaviour and bounding.

These are pure unit tests with an explicit clock, so the window semantics are
pinned exactly rather than inferred from wall-clock timing.
"""
from app.burst import BurstTracker

KINDS = frozenset(
    {"gif", "sticker", "animated_sticker", "video_sticker", "video_note"}
)
CHAT = -1001234567890


def tracker(window=3.0, max_items=5):
    return BurstTracker(window_seconds=window, max_items=max_items)


def feed_all(t, count, *, start=1000.0, step=0.0, kind="gif", user=7, first_id=100):
    """Record ``count`` media messages, returning every decision in order."""
    return [
        t.record(CHAT, user, first_id + i, kind, now=start + i * step, kinds=KINDS)
        for i in range(count)
    ]


def feed(t, count, **kw):
    """Record ``count`` media messages, returning the last decision."""
    return feed_all(t, count, **kw)[-1]


# ------------------------------------------------------- threshold semantics
def test_exactly_five_in_window_is_not_a_burst():
    assert feed(tracker(), 5, step=0.1).is_burst is False


def test_six_in_window_is_a_burst():
    d = feed(tracker(), 6, step=0.1)
    assert d.is_burst is True
    assert d.count == 6
    assert d.message_ids == [100, 101, 102, 103, 104, 105]


def test_ten_in_two_seconds_is_a_burst():
    decisions = feed_all(tracker(), 10, step=0.2)
    burst = next(d for d in decisions if d.is_burst)
    # the flood is stopped the moment it crosses the threshold, not after ten
    assert decisions.index(burst) == 5
    assert burst.count == 6
    assert burst.message_ids == [100, 101, 102, 103, 104, 105]


def test_five_spread_beyond_the_window_is_not_a_burst():
    # one every 2s inside a 3s window: never more than two live at once
    assert feed(tracker(), 5, step=2.0).is_burst is False


def test_a_slow_drip_never_accumulates():
    # 20 messages, one per second, window 3s -> at most 3 live
    assert feed(tracker(), 20, step=1.0).is_burst is False


def test_window_boundary_keeps_the_oldest_inside():
    # last event at +3.0 with a 3.0s window: the first (t=0) is exactly at the
    # cutoff and is still counted
    assert feed(tracker(), 6, step=0.6).is_burst is True


def test_window_boundary_drops_the_oldest():
    # last event at +3.01: the first falls outside and must not be counted
    d = feed(tracker(), 6, step=0.601)
    assert d.is_burst is False


# ------------------------------------------------------- separation and scope
def test_two_separate_bursts_are_reported_separately():
    t = tracker()
    first = feed(t, 6, step=0.1, start=1000.0, first_id=100)
    assert first.is_burst is True
    # the window was cleared, so a later flood is a new burst with new ids
    second = feed(t, 6, step=0.1, start=2000.0, first_id=200)
    assert second.is_burst is True
    assert second.message_ids == [200, 201, 202, 203, 204, 205]


def test_a_burst_is_reported_once_and_does_not_repeat():
    t = tracker()
    assert feed(t, 6, step=0.1, first_id=100).is_burst is True
    # the following message starts a fresh window, it is not a second burst
    assert t.record(CHAT, 7, 106, "gif", now=1000.6, kinds=KINDS).is_burst is False


def test_photos_are_never_counted():
    t = tracker()
    out = None
    for i in range(20):
        out = t.record(CHAT, 7, 100 + i, "photo", now=1000 + i * 0.01, kinds=KINDS)
    assert out.is_burst is False
    assert out.count == 0


def test_non_qualifying_kind_is_ignored():
    t = tracker()
    assert t.record(CHAT, 7, 1, "video_file", now=1000.0, kinds=KINDS).is_burst is False


def test_per_user_windows_are_independent():
    t = tracker()
    for i in range(5):
        t.record(CHAT, 7, 100 + i, "gif", now=1000 + i * 0.1, kinds=KINDS)
    d = t.record(CHAT, 8, 200, "gif", now=1000.5, kinds=KINDS)
    assert d.is_burst is False
    assert d.count == 1


def test_per_chat_windows_are_independent():
    t = tracker()
    for i in range(5):
        t.record(CHAT, 7, 100 + i, "gif", now=1000 + i * 0.1, kinds=KINDS)
    d = t.record(CHAT - 1, 7, 200, "gif", now=1000.5, kinds=KINDS)
    assert d.is_burst is False


def test_disabled_tracker_never_reports_a_burst():
    t = tracker(window=0.0)
    assert t.enabled is False
    assert feed(t, 50, step=0.0).is_burst is False


# ------------------------------------------------------- bounding
def test_events_per_user_are_capped():
    t = BurstTracker(
        window_seconds=1000.0, max_items=100, max_events_per_key=8
    )
    for i in range(30):
        t.record(CHAT, 7, i, "gif", now=1000 + i * 0.01, kinds=KINDS)
    assert len(t._events[(CHAT, 7)]) <= 8


def test_tracked_users_are_capped():
    t = BurstTracker(
        window_seconds=1000.0, max_items=100, max_keys=10, max_events_per_key=8
    )
    for user in range(50):
        t.record(CHAT, user, 1, "gif", now=1000.0, kinds=KINDS)
    assert len(t._events) <= 10


def test_stale_users_are_evicted_before_active_ones():
    t = BurstTracker(
        window_seconds=10.0, max_items=100, max_keys=3, max_events_per_key=8
    )
    t.record(CHAT, 1, 1, "gif", now=1000.0, kinds=KINDS)
    t.record(CHAT, 2, 1, "gif", now=1011.0, kinds=KINDS)
    t.record(CHAT, 3, 1, "gif", now=1011.0, kinds=KINDS)
    t.record(CHAT, 4, 1, "gif", now=1011.0, kinds=KINDS)
    assert len(t._events) <= 3
    # the long-stale user 1 is gone, the recent ones are kept
    assert (CHAT, 1) not in t._events


def test_forget_clears_a_user():
    t = tracker()
    feed(t, 3, step=0.1)
    t.forget(CHAT, 7)
    assert (CHAT, 7) not in t._events
