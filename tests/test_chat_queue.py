"""The turn queue: ordered per conversation, bounded overall, never a scolding.

The behaviour this suite pins down is the fix for the defect the owner reported:
Nexus used to answer a burst by **dropping** what did not fit its own rate window
and telling the person off for it — «یه کم سریع داری پیام می‌دی». Three things
must now be true, and each is a test below:

* **Our own brake is silent.** `chat._MESSAGES` has no sentence for
  `rate_limit`/`user_rate_limit`, so a throttled turn can never become a message
  that blames the sender. A real quota (`daily_cap`) still speaks.
* **A full window is waited out, not dropped.** The queue retries a throttled
  turn with a bounded backoff and serves it when the window frees.
* **A conversation is ordered and the pool is bounded.** Two turns for one
  `(chat_id, user_id)` never overlap — which is what stops them reading the same
  history and answering each other's context — while different conversations run
  together, up to `GEMINI_CHAT_MAX_CONCURRENCY`.

No Telegram, no Google: `chat.reply` is replaced, so "how many calls, and in
what order" is asserted exactly rather than inferred.
"""
import asyncio

import pytest

from app import chat, chat_queue, config


def _stub(monkeypatch, fn):
    """Replace the model seam the queue calls."""
    monkeypatch.setattr(chat, "reply", fn)


def _answered(text="ok"):
    return chat.ChatReply(answered=True, text=text)


# ── The brake is silent ────────────────────────────────────────────────────
def test_our_own_brake_has_no_sentence():
    """A limit the deployment set is never reported as the sender's fault."""
    assert chat.ChatReply(skipped="rate_limit").message == ""
    assert chat.ChatReply(skipped="user_rate_limit").message == ""
    # A real, exhausted quota is a different thing and still speaks.
    assert chat.ChatReply(skipped="daily_cap").message != ""


def test_a_rate_limit_reply_is_not_a_message_to_send():
    """The whole point: a throttled turn produces nothing to send."""
    assert chat.ChatReply(answered=False, skipped="rate_limit").message == ""


# ── Waiting instead of dropping ────────────────────────────────────────────
def test_a_full_window_is_waited_out_and_then_served(monkeypatch):
    calls = {"n": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return chat.ChatReply(answered=False, skipped="rate_limit")
        return _answered()

    _stub(monkeypatch, _reply)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_MAX_WAIT", 5.0)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_BACKOFF", 0.01)

    result = asyncio.run(chat_queue.reply(1, 2, "hi"))
    assert result.answered and result.text == "ok"
    assert calls["n"] == 3
    assert chat_queue.stats["waited"] >= 1


def test_a_persons_window_is_also_waited_out(monkeypatch):
    calls = {"n": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            return chat.ChatReply(answered=False, skipped="user_rate_limit")
        return _answered()

    _stub(monkeypatch, _reply)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_MAX_WAIT", 5.0)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_BACKOFF", 0.01)

    assert asyncio.run(chat_queue.reply(1, 2, "hi")).answered


def test_a_window_that_never_frees_is_left_silent(monkeypatch):
    """The deadline is a give-up, not a scolding: nothing is sent."""
    calls = {"n": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        calls["n"] += 1
        return chat.ChatReply(answered=False, skipped="user_rate_limit")

    _stub(monkeypatch, _reply)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_MAX_WAIT", 0.05)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_BACKOFF", 0.01)

    result = asyncio.run(chat_queue.reply(1, 2, "hi"))
    assert not result.answered
    assert result.skipped == "user_rate_limit"
    assert result.message == ""  # silence, never a sentence
    assert calls["n"] >= 1
    assert chat_queue.stats["abandoned"] == 1


def test_a_zero_deadline_means_one_attempt(monkeypatch):
    """The switch tests use so the queue never waits out a window in a suite."""
    calls = {"n": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        calls["n"] += 1
        return chat.ChatReply(answered=False, skipped="rate_limit")

    _stub(monkeypatch, _reply)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_MAX_WAIT", 0.0)

    asyncio.run(chat_queue.reply(1, 2, "hi"))
    assert calls["n"] == 1


def test_a_plain_refusal_is_not_retried(monkeypatch):
    """Waiting cannot fix a switched-off feature or a missing key."""
    calls = {"n": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        calls["n"] += 1
        return chat.ChatReply(answered=False, skipped="disabled")

    _stub(monkeypatch, _reply)
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_MAX_WAIT", 5.0)

    result = asyncio.run(chat_queue.reply(1, 2, "hi"))
    assert result.skipped == "disabled"
    assert calls["n"] == 1


# ── Ordering and the bound ─────────────────────────────────────────────────
def test_one_conversations_turns_never_overlap(monkeypatch):
    """The interleave that let two turns read the same history and answer each
    other's context is what the per-conversation lock removes."""
    live = {"now": 0, "max": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return _answered()

    _stub(monkeypatch, _reply)

    async def run():
        await asyncio.gather(
            chat_queue.reply(1, 2, "a"),
            chat_queue.reply(1, 2, "b"),
            chat_queue.reply(1, 2, "c"),
        )

    asyncio.run(run())
    assert live["max"] == 1


def test_different_conversations_run_together(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_CONCURRENCY", 4)
    live = {"now": 0, "max": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.03)
        live["now"] -= 1
        return _answered()

    _stub(monkeypatch, _reply)

    async def run():
        await asyncio.gather(*[chat_queue.reply(1, uid, "x") for uid in range(4)])

    asyncio.run(run())
    assert live["max"] >= 2


def test_global_concurrency_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_CONCURRENCY", 2)
    live = {"now": 0, "max": 0}

    async def _reply(chat_id, user_id, text, **kwargs):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return _answered()

    _stub(monkeypatch, _reply)

    async def run():
        await asyncio.gather(*[chat_queue.reply(1, uid, "x") for uid in range(8)])

    asyncio.run(run())
    assert live["max"] <= 2


def test_the_lock_map_is_bounded(monkeypatch):
    """The key is user input, so the map of locks must not grow without bound."""
    monkeypatch.setattr(chat_queue, "_LOCK_MAX", 10)

    async def _reply(chat_id, user_id, text, **kwargs):
        return _answered()

    _stub(monkeypatch, _reply)

    async def run():
        for uid in range(200):
            await chat_queue.reply(1, uid, "x")

    asyncio.run(run())
    assert len(chat_queue._locks) <= 10


# ── Failure shapes ─────────────────────────────────────────────────────────
def test_cancellation_propagates(monkeypatch):
    """A shutdown must not be swallowed into a silent refusal."""

    async def _reply(chat_id, user_id, text, **kwargs):
        raise asyncio.CancelledError()

    _stub(monkeypatch, _reply)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chat_queue.reply(1, 2, "hi"))


def test_an_unexpected_error_from_the_model_seam_propagates(monkeypatch):
    """`chat.reply` promises never to raise; if it ever does, the queue must not
    hide it behind a retry loop."""

    async def _reply(chat_id, user_id, text, **kwargs):
        raise RuntimeError("boom")

    _stub(monkeypatch, _reply)

    with pytest.raises(RuntimeError):
        asyncio.run(chat_queue.reply(1, 2, "hi"))


def test_kwargs_are_passed_through_unchanged(monkeypatch):
    seen = {}

    async def _reply(chat_id, user_id, text, **kwargs):
        seen.update(chat_id=chat_id, user_id=user_id, text=text, kwargs=kwargs)
        return _answered()

    _stub(monkeypatch, _reply)
    asyncio.run(
        chat_queue.reply(7, 9, "body", parts=["p"], kind="photo", context="ctx")
    )
    assert seen["chat_id"] == 7 and seen["user_id"] == 9 and seen["text"] == "body"
    assert seen["kwargs"] == {"parts": ["p"], "kind": "photo", "context": "ctx"}


def test_reset_state_clears_the_gate_and_locks(monkeypatch):
    async def _reply(chat_id, user_id, text, **kwargs):
        return _answered()

    _stub(monkeypatch, _reply)
    asyncio.run(chat_queue.reply(1, 2, "hi"))
    assert chat_queue._locks
    chat_queue.reset_state()
    assert not chat_queue._locks
    assert chat_queue._semaphore is None
    assert chat_queue.stats["waited"] == 0
