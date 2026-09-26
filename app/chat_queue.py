"""One turn's place in the queue: ordered per conversation, bounded overall.

The conversational path used to answer a burst by **dropping** what did not fit
its own brakes and telling the person off for it («یه کم سریع داری پیام می‌دی»).
That is wrong twice over: the limit is the deployment's, not the sender's, and a
dropped message is a message that was never answered. This module is the fix.

Three properties, and each is load-bearing:

* **Per-conversation ordering.** One `asyncio.Lock` per `(chat_id, user_id)` —
  the same key the conversation history uses — so a person's messages are
  answered one at a time and every turn reads the history the previous turn
  wrote. Without it two messages sent together both read the old history and
  both append, and the two turns answer each other's context. The lock is held
  across the model call, which is the suspension point, so the interleave cannot
  happen.

* **Bounded concurrency.** One `asyncio.Semaphore` bounds how many model calls
  run at once, so ten people talking at the same moment cannot stampede the
  provider. It is acquired only around the call itself, never across a wait, so
  a queued turn holds nothing that another room needs.

* **Wait, never drop.** When `chat.reply` refuses a turn because *our* window is
  full — `rate_limit` or `user_rate_limit` — the queue waits and retries with a
  bounded backoff until the window frees or `GEMINI_CHAT_QUEUE_MAX_WAIT`
  expires. A turn that outlasts its deadline returns the refusal, which now
  carries **no sentence** (`chat._MESSAGES` has no entry for either reason), so
  the outcome is silence rather than a scolding. The provider's own rate limits
  are a different thing and stay in the pool: this module never sees a 429, and
  `gemini_pool` never sees these reasons.

The queue is deliberately thin — it owns ordering, the gate and the wait, and
nothing else. Every policy decision stays in `chat.reply`; the queue only
decides *when* a turn is allowed to reach it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

from . import chat, config

log = logging.getLogger("guardbot.chatqueue")

# The reasons *our* brakes refused a turn. They are the only ones worth waiting
# on: a switched-off feature, a missing key, an exhausted daily allowance or an
# open circuit cannot be fixed by waiting, and retrying them would spend the
# queue's time on a turn that cannot succeed. Waiting is exactly right for the
# two windows, which free themselves.
_THROTTLE = frozenset({"rate_limit", "user_rate_limit"})

# Per-conversation ordering, keyed by ``(chat_id, user_id)`` — the same key the
# conversation history uses. Bounded, because the key comes from user input and
# an unbounded dict of locks is a memory leak wearing a queue's clothes.
_locks: dict[tuple[int, int], asyncio.Lock] = {}
_LOCK_MAX = 5000

# The global gate. Created lazily so it belongs to the running loop rather than
# to import time, and reset by tests.
_semaphore: asyncio.Semaphore | None = None

stats = {
    # Turns that had to wait for a window and were served afterwards.
    "waited": 0,
    # Turns that outlasted the deadline and were left silent.
    "abandoned": 0,
}


def reset_state() -> None:
    """Forget the locks and the gate. For tests."""
    global _semaphore
    _locks.clear()
    _semaphore = None
    for key in stats:
        stats[key] = 0


def _lock_for(chat_id: int, user_id: int) -> asyncio.Lock:
    """The lock for one conversation, created on first use.

    Eviction prefers an *unlocked* lock, because deleting one a turn is holding
    would let a second turn create a fresh lock and run beside the first — the
    exact interleave this module exists to prevent. When every candidate is held
    the map simply grows past its soft ceiling rather than trading correctness
    for a bound; the next call that finds a free one will trim it.
    """
    key = (int(chat_id), int(user_id))
    lock = _locks.get(key)
    if lock is None:
        if len(_locks) >= _LOCK_MAX:
            for stale in list(_locks)[: _LOCK_MAX // 5]:
                if stale != key and not _locks[stale].locked():
                    del _locks[stale]
        lock = _locks[key] = asyncio.Lock()
    return lock


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, int(config.GEMINI_CHAT_MAX_CONCURRENCY)))
    return _semaphore


async def reply(chat_id: int, user_id: int, text: str, **kwargs) -> chat.ChatReply:
    """Answer one message, ordered behind that conversation's earlier turn.

    Same signature and same return as ``chat.reply``, so a caller can swap one
    for the other. It never raises beyond what ``chat.reply`` raises
    (``CancelledError``, which is re-raised untouched so a shutdown is not
    swallowed), and it makes no policy decision of its own.
    """
    async with serialized(chat_id, user_id):
        deadline = time.monotonic() + max(
            0.0, float(config.GEMINI_CHAT_QUEUE_MAX_WAIT)
        )
        delay = max(0.0, float(config.GEMINI_CHAT_QUEUE_BACKOFF))
        while True:
            # The gate is held only across the call. A turn waiting for a window
            # holds its own conversation's order and nothing global, so a slow
            # window in one room cannot block another room's call.
            async with _gate():
                result = await chat.reply(chat_id, user_id, text, **kwargs)
            if result.answered or result.skipped not in _THROTTLE:
                return result
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                # Outlasted the deadline. The refusal carries no sentence, so
                # this is silence: nobody is told they were too fast.
                stats["abandoned"] += 1
                log.info(
                    "[chatqueue] window still full at the deadline chat=%s user=%s",
                    chat_id,
                    user_id,
                )
                return result
            stats["waited"] += 1
            # Jitter so a burst does not wake every waiter on the same tick and
            # stampede the window it is waiting for. The sleep is bounded by the
            # time left, so it can never run past the deadline.
            sleep_for = min(delay, remaining)
            await asyncio.sleep(sleep_for * (0.5 + random.random()))
            delay = min(max(delay, 0.25) * 2.0, 8.0)


@contextlib.asynccontextmanager
async def serialized(chat_id: int, user_id: int):
    """Hold one conversation's order without making a model call.

    The same lock ``reply`` takes, exposed so that a turn which does *not* go
    through ``chat.reply`` — Voice Context's spoken turn — is ordered behind the
    conversation's earlier turns exactly as a text turn is. Without it two voice
    notes sent together would both read the same history and answer each other's
    context, which is the interleave this module exists to prevent; with it, a
    voice note and a typed message from one person queue behind each other
    whichever order they arrive in.

    The lock is *not* reentrant, and that is the reason this is a context
    manager rather than something ``reply`` could nest inside: a caller must
    release it before it reaches ``chat.reply``, so a fallback from the spoken
    path to the text one happens after this block and not inside it.
    """
    async with _lock_for(chat_id, user_id):
        yield


__all__ = ["reply", "reset_state", "serialized", "stats"]
