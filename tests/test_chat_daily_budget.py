"""The conversational daily allowance belongs to an account, not to the bot.

The bug this file pins down is specific and it happened in production: one
shared counter for the whole deployment reached its ceiling while a second
configured chat key with a full day's allowance sat unused, and the group was
told "سهم امروز چت تموم شده" — today's chat share is used up — which was simply
false. Two keys bought nothing, because the cap was not a property of a key.

So the allowance now lives on each account, and the pool spends one account's
day and then fails over to the next exactly as it does for a 429. The property
that matters, and the one most of this file is about:

    a user is told the quota is gone only when *every* account is out.

Everything here runs against the real pool code with the network seam replaced.
No test reaches Google or Telegram.
"""
import asyncio
import time

import pytest

from app import chat, config, db, gemini_pool

CHAT = -1001234567890
USER = 42


@pytest.fixture(autouse=True)
def layer(monkeypatch):
    """A fresh database, a configured chat key, and an empty pool registry."""
    db.init()
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "chat-key-not-a-real-one")
    monkeypatch.setattr(config, "GEMINI_CHAT_MODEL", "test-chat-model")
    monkeypatch.setattr(config, "GEMINI_CHAT_RATE_LIMIT", 1000)
    monkeypatch.setattr(config, "GEMINI_CHAT_USER_RATE_LIMIT", 1000)
    monkeypatch.setattr(config, "GEMINI_CHAT_DAILY_LIMIT", 100)
    monkeypatch.setattr(config, "GEMINI_CHAT_CIRCUIT_FAILURES", 1000)
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "GEMINI_CHAT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(config, "GEMINI_CHAT_HISTORY_TURNS", 0)
    monkeypatch.setattr(gemini_pool, "_pools", {})
    chat.reset_state()
    yield
    chat.reset_state()


def _pool(*slots, budget=3):
    """A chat pool with one account per slot, and no network behind it.

    The model name is a real one from the capability table rather than a
    placeholder, because ``models_for`` refuses a model it cannot find
    capabilities for — a made-up name would make every pool look empty and the
    tests would pass for the wrong reason.
    """
    pool = gemini_pool.Pool(
        "chat",
        [(slot, f"key-for-slot-{slot}") for slot in slots],
        ["gemini-flash-lite-latest"],
        frozenset({"text"}),
        daily_budget=budget,
    )
    return pool


def _use(pool, slot, times, now=None):
    """Spend ``times`` provider requests on one account of a pool."""
    now = time.time() if now is None else now
    account = next(a for a in pool.accounts if a.slot == str(slot))
    for _ in range(times):
        account.note_request(now)


class Recorder:
    def __init__(self, answer="باشه."):
        self.answer = answer
        self.calls = []

    async def __call__(self, contents):
        self.calls.append(contents)
        return self.answer

    @property
    def count(self):
        return len(self.calls)


def install(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(chat, "_request", recorder)
    return recorder


def ask(text, chat_id=CHAT, user_id=USER):
    return asyncio.run(chat.reply(chat_id, user_id, text))


# ── The allowance is per account ──────────────────────────────────────────
def test_each_account_gets_its_own_allowance_not_a_shared_one():
    """Two accounts mean two allowances. This is the whole point."""
    pool = _pool("1", "2", budget=3)

    _use(pool, "1", 3)

    assert pool.accounts[0].daily_calls(time.time()) == 3
    assert pool.accounts[0].daily_exhausted(time.time()) is True
    assert pool.accounts[1].daily_calls(time.time()) == 0
    assert pool.accounts[1].daily_exhausted(time.time()) is False
    # Three spent on one account must not have cost the other anything.
    assert pool.daily_remaining(time.time()) == 3


def test_the_pool_stops_offering_an_account_that_spent_its_day():
    pool = _pool("1", "2", budget=2)
    now = time.time()

    _use(pool, "1", 2)

    assert [a.slot for a in pool.ordered_accounts(now)] == ["2"]


def test_the_allowance_is_counted_per_provider_request():
    pool = _pool("1", budget=10)
    now = time.time()

    _use(pool, "1", 4)

    assert pool.accounts[0].daily_calls(now) == 4
    assert pool.daily_remaining(now) == 6


def test_the_total_spendable_scales_with_the_number_of_accounts():
    """One account is one allowance; three accounts are three."""
    one = _pool("1", budget=5)
    three = _pool("1", "2", "3", budget=5)
    now = time.time()

    assert one.daily_remaining(now) == 5
    assert three.daily_remaining(now) == 15


def test_the_pool_reports_exhausted_only_when_every_account_is_out():
    pool = _pool("1", "2", budget=2)
    now = time.time()

    _use(pool, "1", 2)
    assert pool.daily_exhausted(now) is False

    _use(pool, "2", 2)
    assert pool.daily_exhausted(now) is True


# ── The day turns over on its own ─────────────────────────────────────────
def test_the_allowance_comes_back_on_the_next_day_with_no_restart(monkeypatch):
    """Recovery is a property of the clock, not of a process restart.

    The day is the API day, derived from the wall clock on every read, so
    nothing has to fire at midnight for the allowance to return — and a
    container that has been up for a week gets a fresh allowance every day
    without anybody touching it.

    The clock is moved rather than a future timestamp passed in, because that is
    the mechanism: the allowance is not stored with an expiry, it is *read* from
    whatever day the clock is in.
    """
    pool = _pool("1", "2", budget=2)
    _use(pool, "1", 2)
    _use(pool, "2", 2)
    assert pool.daily_exhausted() is True
    assert pool.ordered_accounts(time.time()) == []

    tomorrow = time.time() + 86400
    monkeypatch.setattr(gemini_pool.time, "time", lambda: tomorrow)

    assert pool.daily_exhausted() is False
    assert [a.slot for a in pool.ordered_accounts(tomorrow)] == ["1", "2"]
    assert pool.daily_remaining() == 4


def test_yesterdays_spend_is_still_on_the_record_after_the_rollover(monkeypatch):
    """A new day forgives the allowance; it does not erase the history."""
    pool = _pool("1", budget=2)
    now = time.time()
    _use(pool, "1", 2)
    spent_day = db.ai_day(now)

    monkeypatch.setattr(gemini_pool.time, "time", lambda: now + 86400)

    assert pool.daily_remaining() == 2
    assert db.daily_for("chat", spent_day) == {"1": 2}


# ── Transient trouble is not an exhausted quota ───────────────────────────
def test_an_account_cooling_down_with_allowance_is_not_an_exhausted_quota():
    """A minute-long cooldown must not send anybody away for a day.

    ``daily_exhausted`` deliberately ignores cooldowns. If it did not, a single
    429 would produce the "come back tomorrow" message, which is both wrong and
    the kind of wrong that costs a user for a whole day.
    """
    pool = _pool("1", budget=5)
    now = time.time()
    pool.accounts[0].cooldown_until = int(now) + 60

    assert pool.daily_exhausted(now) is False
    # It is out of rotation, though — those are different facts.
    assert pool.ordered_accounts(now) == []


def test_a_revoked_account_does_not_make_the_quota_look_exhausted():
    pool = _pool("1", "2", budget=5)
    now = time.time()
    pool.accounts[0].state = "INVALID"

    assert pool.daily_exhausted(now) is False
    assert [a.slot for a in pool.ordered_accounts(now)] == ["2"]


# ── What the user is actually told ────────────────────────────────────────
def test_the_user_is_not_told_the_quota_is_gone_while_an_account_remains(
    monkeypatch,
):
    """The production bug, as a test.

    The first account's day is spent. There is a second account with a full day
    available. The user must get an answer, not a "quota exhausted" message.
    """
    pool = _pool("1", "2", budget=2)
    monkeypatch.setattr(gemini_pool, "_pools", {"chat": pool})
    recorder = install(monkeypatch)
    _use(pool, "1", 2)

    result = ask("سلام")

    assert result.skipped != "daily_cap"
    assert result.answered is True
    assert recorder.count == 1


def test_the_user_is_told_only_once_every_account_is_out(monkeypatch):
    pool = _pool("1", "2", budget=2)
    monkeypatch.setattr(gemini_pool, "_pools", {"chat": pool})
    recorder = install(monkeypatch)
    _use(pool, "1", 2)
    _use(pool, "2", 2)

    result = ask("سلام")

    assert result.skipped == "daily_cap"
    assert result.answered is False
    assert recorder.count == 0


def test_the_answer_still_arrives_while_an_account_has_allowance(monkeypatch):
    """Not just "not skipped" — the reply is actually produced."""
    pool = _pool("1", "2", budget=1)
    monkeypatch.setattr(gemini_pool, "_pools", {"chat": pool})
    install(monkeypatch)
    _use(pool, "1", 1)

    result = ask("سلام")

    assert result.answered is True
    assert result.text


# ── The fallback, and the isolation ───────────────────────────────────────
def test_a_deployment_with_no_pool_still_uses_the_single_counter(monkeypatch):
    """The old behaviour is unchanged where there is no account to attribute to.

    Without a pool there is no per-account allowance to spend, so one counter
    really is the whole truth — and that path must keep working exactly as it
    did, including its floor of one.
    """
    monkeypatch.setattr(gemini_pool, "_pools", {})
    monkeypatch.setattr(config, "GEMINI_CHAT_DAILY_LIMIT", 2)
    recorder = install(monkeypatch)

    ask("یک")
    ask("دو")
    third = ask("سه")

    assert recorder.count == 2
    assert third.skipped == "daily_cap"


def test_the_other_workloads_have_no_daily_allowance():
    """Only the conversational workload has one, and this is what keeps it so.

    A per-account allowance on moderation or transcription would silently
    change how much of the provider those workloads may use, which is not what
    the conversational fix was about.
    """
    for spec in config.GEMINI_POOLS:
        if spec["workload"] == "chat":
            assert spec["daily_budget"] >= 1
        else:
            assert spec.get("daily_budget", 0) == 0


def test_a_pool_without_a_budget_never_reports_an_exhausted_quota():
    """0 means unlimited, which is what the other four workloads rely on."""
    pool = gemini_pool.Pool(
        "moderation", [("1", "k")], ["m"], frozenset({"text"}), daily_budget=0
    )
    now = time.time()
    _use(pool, "1", 1000)

    assert pool.daily_exhausted(now) is False
    assert pool.accounts[0].daily_exhausted(now) is False
    assert pool.ordered_accounts(now)


def test_the_chat_allowance_does_not_touch_the_other_counters():
    """Spending the chat day must not move moderation's or acquisition's."""
    pool = _pool("1", budget=2)
    now = time.time()

    _use(pool, "1", 2)

    assert db.daily_for("moderation", db.ai_day(now)) == {}
    assert db.daily_for("intent", db.ai_day(now)) == {}
    assert db.daily_for("transcribe", db.ai_day(now)) == {}


# ── The allowance composed with the failover it rides on ──────────────────
def test_a_spent_account_is_skipped_and_the_request_is_served_by_the_next(
    monkeypatch,
):
    """The whole promise, through ``generate`` rather than around it.

    Account 1 has spent its day. The request must be served by account 2, with
    no waiting for account 1 to recover — which is the behaviour the brief asks
    for and the behaviour the single shared counter made impossible.
    """
    from types import SimpleNamespace

    pool = _pool("1", "2", budget=2)
    monkeypatch.setattr(gemini_pool, "_pools", {"chat": pool})

    async def fake_call(pool_, account, model, types, build_contents, build_config):
        return SimpleNamespace(text=f"served-by-{account.slot}")

    monkeypatch.setattr(gemini_pool, "_call", fake_call)
    _use(pool, "1", 2)

    text = asyncio.run(
        gemini_pool.generate(
            pool,
            build_contents=lambda types: [],
            build_config=lambda types: None,
        )
    )

    assert text == "served-by-2"


def test_the_request_is_refused_only_when_every_account_is_out(monkeypatch):
    """...and the other half of the same promise."""
    pool = _pool("1", "2", budget=1)
    monkeypatch.setattr(gemini_pool, "_pools", {"chat": pool})
    _use(pool, "1", 1)
    _use(pool, "2", 1)

    with pytest.raises(gemini_pool.PoolUnavailable):
        asyncio.run(
            gemini_pool.generate(
                pool,
                build_contents=lambda types: [],
                build_config=lambda types: None,
            )
        )
