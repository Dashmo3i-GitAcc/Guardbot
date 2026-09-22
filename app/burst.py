"""Instant media-flood (burst) detection.

Pure, bounded, per-user rolling-window tracker. It touches no Telegram API, no
model and no filesystem, so the flood rule can be unit tested exactly.

A burst is **more than** ``max_items`` qualifying media messages from the same
user inside ``window_seconds``. The window is deliberately very short: the
point is to stop an *instant* flood, not to treat normal media sharing over
20-30 seconds as spam.

Only the configured ``kinds`` count. Ordinary photos are never in that set, so
sending several photos quickly is not a flood.

State is bounded on purpose:

* one small ``deque`` per user, itself capped (``max_events_per_key``);
* a hard cap on the number of tracked users (``max_keys``), with stale users
  evicted first.

A spammer therefore cannot grow memory without bound, and the tracker does no
work proportional to history.

This module only reports *that* a burst happened and *which* message ids
belong to it. What to do about it (delete, restrict, warn) is decided by the
caller.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# Hard safety caps. They exist so the tracker can never grow without bound,
# independent of configuration.
DEFAULT_MAX_KEYS = 5000
DEFAULT_MAX_EVENTS_PER_KEY = 64


@dataclass(frozen=True)
class BurstDecision:
    """Result of recording one media message."""

    is_burst: bool
    # Message ids of the messages that make up the burst, oldest first. Empty
    # when ``is_burst`` is False.
    message_ids: list[int] = field(default_factory=list)
    # How many qualifying messages were inside the window at this point.
    count: int = 0


class BurstTracker:
    """Per-user rolling window of qualifying media messages."""

    def __init__(
        self,
        *,
        window_seconds: float,
        max_items: int,
        max_keys: int = DEFAULT_MAX_KEYS,
        max_events_per_key: int = DEFAULT_MAX_EVENTS_PER_KEY,
    ) -> None:
        self.window_seconds = float(window_seconds)
        self.max_items = int(max_items)
        self.max_keys = int(max_keys)
        self.max_events_per_key = int(max_events_per_key)
        self._events: dict[tuple[int, int], deque[tuple[int, float]]] = {}

    @property
    def enabled(self) -> bool:
        """A window of 0 or a negative threshold disables the rule entirely."""
        return self.window_seconds > 0 and self.max_items >= 0

    def _prune(self, key: tuple[int, int], now: float) -> deque[tuple[int, float]]:
        events = self._events.get(key)
        if events is None:
            events = deque(maxlen=self.max_events_per_key)
            self._events[key] = events
        cutoff = now - self.window_seconds
        while events and events[0][1] < cutoff:
            events.popleft()
        return events

    def _evict_if_needed(self, now: float) -> None:
        if len(self._events) <= self.max_keys:
            return
        cutoff = now - self.window_seconds
        # Drop users with no live events first.
        stale = [k for k, ev in self._events.items() if not ev or ev[-1][1] < cutoff]
        for k in stale:
            del self._events[k]
        # Still over the cap: drop the least recently active users.
        if len(self._events) > self.max_keys:
            ordered = sorted(self._events.items(), key=lambda kv: kv[1][-1][1] if kv[1] else 0.0)
            for k, _ in ordered[: len(self._events) - self.max_keys]:
                del self._events[k]

    def record(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        kind: str,
        *,
        now: float | None = None,
        kinds: frozenset[str] | set[str] | None = None,
    ) -> BurstDecision:
        """Record one media message and report whether it completed a burst.

        Non-qualifying kinds (e.g. photos) are ignored and never counted.
        """
        if not self.enabled:
            return BurstDecision(False)
        if kinds is not None and kind not in kinds:
            return BurstDecision(False)

        now = time.time() if now is None else now
        key = (chat_id, user_id)
        events = self._prune(key, now)
        events.append((message_id, now))

        if len(events) <= self.max_items:
            self._evict_if_needed(now)
            return BurstDecision(False, count=len(events))

        message_ids = [mid for mid, _ in events]
        # Clear the window: this burst has been reported. The next flood starts
        # a fresh burst instead of re-reporting the same messages on every
        # subsequent message.
        events.clear()
        self._evict_if_needed(now)
        return BurstDecision(True, message_ids=message_ids, count=len(message_ids))

    def forget(self, chat_id: int, user_id: int) -> None:
        self._events.pop((chat_id, user_id), None)
