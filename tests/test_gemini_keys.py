"""The owner's Gemini credential dashboard: storage, pool wiring, and access.

The brief asks for a control plane rather than a report, which means this suite
has to pin three different kinds of property, and they are worth keeping apart:

* **Storage.** A credential added from Telegram survives a rebuild, is readable
  only by the process that owns it, and never appears in the database, the audit
  trail, a log line or a rendered screen. The one place it *is* readable — the
  0600 file — is asserted explicitly, because "no plaintext anywhere" would be a
  false claim and a test that pretended otherwise would be worse than none.
* **Pool wiring.** Adding a credential changes the pool the running bot uses,
  without a restart, and a workload that is not open to management cannot be
  widened by a crafted callback.
* **Access.** Only the owner, re-checked on every press from the Telegram id, and
  only for the three workloads the configuration names. A credential typed into
  a private chat must never reach the conversational layer.

Everything is driven through the real handlers with fake updates, so what is
pinned is the whole chain rather than the pieces.
"""
import asyncio
import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop

from app import config, db, gemini_keys, gemini_pool, key_store, main

OWNER = 999
STRANGER = 42
CHAT = -1001234567890
PRIVATE = 555

# Shaped like the deployment's real short-lived tokens rather than like an
# ``AIza...`` key, because that is the shape the store has to accept — but
# synthetic. A test constant that is a working credential would be published the
# moment this repository is, and the shape is the only thing the code cares
# about.
KEY_NEW = "AQ.Ab8RN6FAKEKEY000000000000000000000000000000000000001"
KEY_TWO = "AQ.Ab8RN6FAKEKEY000000000000000000000000000000000000002"
KEY_ENV = "AIzaSyFAKE000000000000000000000000000000000A"

TEXT_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def keys_env(monkeypatch, tmp_path):
    """An owner, an empty database, an empty pool registry, a private store.

    The store path is per-test. A shared path would make "add a key" in one test
    visible to the next, and the failures that produces read as security bugs
    rather than as test isolation.
    """
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", None)
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(
        config, "GEMINI_KEY_STORE_PATH", str(tmp_path / "gemini_keys.json")
    )
    db.init()
    gemini_pool.reset_clients()
    monkeypatch.setattr(gemini_pool, "_pools", {})
    gemini_keys.reset_pending()
    yield
    gemini_pool.reset_clients()


def _pools_config(monkeypatch, *, chat_keys=(("1", KEY_ENV),), extra=()):
    """A small, deterministic pool configuration.

    The real one is built from the environment, which is empty in tests — so a
    test that asserted "chat has one account" would be asserting nothing. This
    states the configuration outright instead.
    """
    specs = [
        {
            "workload": "chat",
            "keys": [(slot, key) for slot, key in chat_keys],
            "models": list(TEXT_MODELS),
            "capabilities": frozenset({"text"}),
            "allow_experimental": False,
            "retries": 0,
            "backoff": 0.0,
            "timeout": 10.0,
        },
        {
            "workload": "moderation",
            "keys": [("1", "AIzaSyFAKE000000000000000000000000000000000M")],
            "models": list(TEXT_MODELS),
            "capabilities": frozenset({"text"}),
            "allow_experimental": False,
            "retries": 0,
            "backoff": 0.0,
            "timeout": 10.0,
        },
        {
            "workload": "awareness",
            "keys": [],
            "models": list(TEXT_MODELS),
            "capabilities": frozenset({"text"}),
            "allow_experimental": False,
            "retries": 0,
            "backoff": 0.0,
            "timeout": 10.0,
        },
    ]
    specs.extend(extra)
    monkeypatch.setattr(config, "GEMINI_POOLS", specs)
    return specs


# ── The scripted provider ─────────────────────────────────────────────────
# Only the credential probe is faked. The store, the pool, the handlers and the
# screens are the real ones, because they are what the feature is.
class FakeClient:
    def __init__(self, outcome):
        self._outcome = outcome
        self.aio = SimpleNamespace(models=SimpleNamespace(list=self._list))

    async def _list(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class ProviderError(Exception):
    def __init__(self, code, status, message=""):
        self.code = code
        self.status = status
        super().__init__(json.dumps({"error": {"code": code, "status": status,
                                               "message": message}}))


class Probe:
    """Records every credential handed to the probe, and what it answers."""

    def __init__(self, outcome=None):
        self.outcome = outcome if outcome is not None else [
            {"name": f"models/{m}", "supported_generation_methods": ["generateContent"]}
            for m in TEXT_MODELS
        ]
        self.keys = []

    def build(self, key, timeout):
        self.keys.append(key)
        return FakeClient(self.outcome), None


@pytest.fixture
def probe(monkeypatch):
    p = Probe()
    monkeypatch.setattr(gemini_pool, "build_client", p.build)
    return p


# ── Telegram doubles ──────────────────────────────────────────────────────
class FakeBot:
    def __init__(self, *, delete_fails=False, set_commands_fails=False):
        self.id = 1
        self.username = "guardbot"
        self.sent = []
        self.edits = []
        self.deleted = []
        self.answers = []
        self.delete_fails = delete_fails
        self.set_commands_fails = set_commands_fails
        # Every keyboard handed to `send_message`, so a test can assert that the
        # owner was given a way in and a stranger was not.
        self.markups = []
        # `(commands, scope)` for each `set_my_commands` call, in order.
        self.commands = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def set_my_commands(self, commands, scope=None, **kwargs):
        if self.set_commands_fails:
            raise TelegramError("cannot set commands")
        self.commands.append(
            (tuple((c.command, c.description) for c in commands), scope)
        )

    async def delete_message(self, chat_id, message_id, **kwargs):
        if self.delete_fails:
            raise TelegramError("cannot delete")
        self.deleted.append((chat_id, message_id))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        return SimpleNamespace(message_id=kwargs.get("message_id", 0))

    async def send_chat_action(self, *args, **kwargs):
        return None


class FakeApplication:
    """Collects the deferred work so a test can await it deliberately."""

    def __init__(self):
        self.tasks = []

    def create_task(self, coro, update=None):
        self.tasks.append(coro)
        return coro

    def drain(self):
        for coro in self.tasks:
            asyncio.run(coro)
        self.tasks.clear()


class FakeCallbackQuery:
    def __init__(self, data, *, message_id=7):
        self.data = data
        self.message_id = message_id
        self.answers = []
        self.edits = []
        self.deleted = 0
        self.answer_error = None
        self.edit_error = None

    async def answer(self, text=None, **kwargs):
        if self.answer_error is not None:
            raise self.answer_error
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(text)

    async def edit_message_reply_markup(self, **kwargs):
        return None

    async def delete_message(self):
        self.deleted += 1


def message_update(
    bot, *, actor=OWNER, text="hello", chat_id=PRIVATE, chat_type="private",
    message_id=11,
):
    msg = SimpleNamespace(
        message_id=message_id,
        text=text,
        from_user=SimpleNamespace(id=actor, full_name=f"user{actor}", is_bot=False),
    )
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=actor, full_name=f"user{actor}",
                                       first_name=f"user{actor}",
                                       username=None, is_bot=False),
        callback_query=None,
    )


def callback_update(bot, *, actor=OWNER, data="gk:home", chat_id=PRIVATE,
                    chat_type="private"):
    query = FakeCallbackQuery(data)
    return SimpleNamespace(
        effective_message=None,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=actor, full_name=f"user{actor}",
                                       username=None, is_bot=False),
        callback_query=query,
    ), query


def ctx_for(bot, *, args=None):
    return SimpleNamespace(
        bot=bot, args=list(args or []), application=FakeApplication()
    )


def run_handler(handler, update, ctx):
    """Run one handler, absorbing ``ApplicationHandlerStop`` like the dispatcher.

    Returns whether the handler stopped the update — which is the property the
    isolation test is about, so it is the return value rather than a detail.
    """
    async def go():
        try:
            await handler(update, ctx)
            return False
        except ApplicationHandlerStop:
            return True

    return asyncio.run(go())


# ── The store: shape and identity ─────────────────────────────────────────
def test_a_credential_is_accepted_in_the_shape_the_deployment_actually_uses():
    assert key_store.looks_like_key(KEY_NEW)
    assert key_store.looks_like_key(KEY_ENV)


@pytest.mark.parametrize(
    "text",
    ["", "hello", "two words", "a" * 19, "a" * 201, "has space in it 1234567890"],
)
def test_something_that_is_not_one_token_is_refused(text):
    assert not key_store.looks_like_key(text)


def test_a_pasted_credential_may_carry_a_trailing_newline():
    assert key_store.normalise(f"  {KEY_NEW}\n") == KEY_NEW


def test_the_slot_is_derived_from_the_credential_and_is_stable():
    first = key_store.slot_for(KEY_NEW)
    assert first == key_store.slot_for(KEY_NEW)
    assert first.startswith(key_store.SLOT_PREFIX)
    assert first != key_store.slot_for(KEY_TWO)


def test_an_entry_does_not_render_its_credential():
    """The one line of defence against a future ``log.info("%s", entry)``."""
    entry, _created = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert KEY_NEW not in repr(entry)
    assert KEY_NEW not in str(entry.describe())
    assert entry.masked.endswith(KEY_NEW[-4:])


# ── The store: writing ────────────────────────────────────────────────────
def test_a_stored_credential_is_readable_only_by_its_owner():
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    mode = stat.S_IMODE(os.stat(config.GEMINI_KEY_STORE_PATH).st_mode)
    assert mode == 0o600


def test_the_credential_is_plaintext_in_the_store_file_and_that_is_the_choice():
    """Stated, not hidden: the boundary is the file mode, not encryption.

    This is the honest half of the brief's "no plaintext secrets". The credential
    is never in the database, the audit trail, a log or a message; it *is* in one
    root-only file, and a test that asserted otherwise would be testing a
    fiction.
    """
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    with open(config.GEMINI_KEY_STORE_PATH, encoding="utf-8") as handle:
        assert KEY_NEW in handle.read()


def test_the_store_directory_is_created_when_it_does_not_exist(tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "deeper" / "keys.json"
    monkeypatch.setattr(config, "GEMINI_KEY_STORE_PATH", str(nested))
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert nested.exists()


def test_adding_the_same_credential_twice_does_not_duplicate_it():
    first, created_first = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    second, created_second = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert created_first is True
    assert created_second is False
    assert first.slot == second.slot
    assert len(key_store.entries()) == 1


def test_adding_the_same_credential_to_a_second_workload_is_a_second_account():
    """One key, two workloads: the pool keeps them separate on purpose."""
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    key_store.add("awareness", KEY_NEW, actor_id=OWNER)
    assert len(key_store.entries()) == 2


def test_a_workload_that_is_not_managed_cannot_be_written_to():
    assert not key_store.is_managed("moderation")
    with pytest.raises(key_store.StoreError) as caught:
        key_store.add("moderation", KEY_NEW, actor_id=OWNER)
    assert caught.value.reason == "not_managed"


def test_a_workload_that_is_not_managed_cannot_be_removed_from():
    with pytest.raises(key_store.StoreError) as caught:
        key_store.remove("moderation", "1", actor_id=OWNER)
    assert caught.value.reason == "not_managed"


def test_a_value_that_is_not_a_credential_is_refused():
    with pytest.raises(key_store.StoreError) as caught:
        key_store.add("chat", "definitely not a key", actor_id=OWNER)
    assert caught.value.reason == "bad_shape"


def test_the_per_workload_ceiling_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_KEY_MAX_PER_WORKLOAD", 2)
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    key_store.add("chat", KEY_TWO, actor_id=OWNER)
    with pytest.raises(key_store.StoreError) as caught:
        key_store.add("chat", KEY_ENV, actor_id=OWNER)
    assert caught.value.reason == "too_many"


def test_an_unreadable_store_is_never_overwritten():
    """The failure mode that would lose every other credential in the file."""
    with open(config.GEMINI_KEY_STORE_PATH, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    with pytest.raises(key_store.StoreError) as caught:
        key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert caught.value.reason == "corrupt"
    with open(config.GEMINI_KEY_STORE_PATH, encoding="utf-8") as handle:
        assert "this is not json" in handle.read()


def test_an_unreadable_store_reads_as_empty_for_the_pool_and_says_so(caplog):
    with open(config.GEMINI_KEY_STORE_PATH, "w", encoding="utf-8") as handle:
        handle.write("[]")
    found, error = key_store.entries_or_empty()
    assert found == []
    assert error == "corrupt"


def test_removing_something_that_was_never_there_returns_nothing():
    assert key_store.remove("chat", "kdeadbeef", actor_id=OWNER) is None


def test_removing_an_environment_slot_returns_nothing():
    """The dashboard must not be able to reach a credential it did not add."""
    assert key_store.remove("chat", "1", actor_id=OWNER) is None
    assert key_store.remove("chat", "shared1", actor_id=OWNER) is None


def test_a_removed_credential_is_gone_from_the_file():
    entry, _ = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    key_store.remove("chat", entry.slot, actor_id=OWNER)
    assert key_store.entries() == []
    with open(config.GEMINI_KEY_STORE_PATH, encoding="utf-8") as handle:
        assert KEY_NEW not in handle.read()


def test_slots_for_only_answers_for_managed_workloads():
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert key_store.slots_for("chat")
    assert key_store.slots_for("moderation") == []


def test_concurrent_adds_do_not_lose_each_other():
    """Two owners pressing at once is two writes to one file."""
    keys = [KEY_NEW, KEY_TWO, KEY_ENV]
    errors: list = []

    def worker(key):
        try:
            key_store.add("chat", key, actor_id=OWNER)
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(key_store.entries()) == 3


# ── The pool wiring ───────────────────────────────────────────────────────
def test_a_runtime_credential_joins_the_pool_the_bot_actually_uses(monkeypatch):
    _pools_config(monkeypatch)
    assert len(gemini_pool.build_pools()["chat"].accounts) == 1
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    pools = gemini_pool.reload()
    assert len(pools["chat"].accounts) == 2


def test_a_removed_credential_leaves_the_pool(monkeypatch):
    _pools_config(monkeypatch)
    entry, _ = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert len(gemini_pool.build_pools()["chat"].accounts) == 2
    key_store.remove("chat", entry.slot, actor_id=OWNER)
    assert len(gemini_pool.reload()["chat"].accounts) == 1


def test_the_environment_key_stays_first(monkeypatch):
    """The operator's written-down choice is not demoted by a phone."""
    _pools_config(monkeypatch)
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    pool = gemini_pool.build_pools()["chat"]
    assert pool.accounts[0].key == KEY_ENV
    assert pool.accounts[1].key == KEY_NEW


def test_a_store_row_cannot_widen_a_workload_that_is_not_managed(monkeypatch):
    """Belt and braces: even a hand-edited file cannot open moderation up."""
    _pools_config(monkeypatch)
    # Written straight to the file, as an operator with shell access could.
    with open(config.GEMINI_KEY_STORE_PATH, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": 1,
                "keys": [
                    {"workload": "moderation", "slot": "kx", "label": "x",
                     "added_at": 0, "added_by": 0, "key": KEY_NEW}
                ],
            },
            handle,
        )
    pool = gemini_pool.build_pools()["moderation"]
    assert [a.key for a in pool.accounts] == [
        "AIzaSyFAKE000000000000000000000000000000000M"
    ]


def test_an_unreadable_store_does_not_stop_the_pool_from_building(monkeypatch):
    _pools_config(monkeypatch)
    with open(config.GEMINI_KEY_STORE_PATH, "w", encoding="utf-8") as handle:
        handle.write("{ broken")
    pools = gemini_pool.build_pools()
    assert len(pools["chat"].accounts) == 1


def test_the_registry_is_swapped_atomically_rather_than_cleared_in_place(monkeypatch):
    """A rebuild must not open a window in which a workload has no pool.

    The registry is read on every request. If it were cleared and refilled, a
    message arriving mid-rebuild would find its pool missing and report an
    outage that the owner caused by adding a key.
    """
    _pools_config(monkeypatch)
    previous = gemini_pool.build_pools()
    seen: list = []
    real_pool = gemini_pool.Pool

    def spy(*args, **kwargs):
        # What a concurrent reader would have seen at this instant.
        seen.append(dict(gemini_pool._pools))
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(gemini_pool, "Pool", spy)
    gemini_pool.build_pools()

    assert seen, "the spy was never called"
    assert all(snapshot == previous for snapshot in seen)
    assert gemini_pool._pools is not previous


def test_reload_keeps_the_counters_of_an_account_it_already_had(monkeypatch):
    """A rebuild re-adopts persisted state; it does not start over."""
    _pools_config(monkeypatch)
    pool = gemini_pool.build_pools()["chat"]
    pool.accounts[0].note_request(1000.0)
    assert pool.accounts[0].requests == 1
    again = gemini_pool.reload()["chat"]
    assert again.accounts[0].requests == 1


# ── The credential probe ──────────────────────────────────────────────────
def test_a_working_credential_verifies(probe):
    ok, kind, detail = asyncio.run(gemini_pool.probe_credential(KEY_NEW))
    assert ok is True
    assert kind == ""
    assert "2 models" in detail
    assert probe.keys == [KEY_NEW]


def test_an_invalid_credential_is_refused_with_the_providers_reason(probe):
    probe.outcome = ProviderError(400, "INVALID_ARGUMENT", "API key not valid")
    ok, kind, detail = asyncio.run(gemini_pool.probe_credential(KEY_NEW))
    assert ok is False
    assert kind == "invalid_credential"


def test_a_provider_that_cannot_be_reached_is_a_refusal_not_a_pass(probe):
    probe.outcome = ProviderError(503, "UNAVAILABLE", "backend down")
    ok, kind, _detail = asyncio.run(gemini_pool.probe_credential(KEY_NEW))
    assert ok is False
    assert kind == "provider_error"


def test_a_credential_that_can_serve_no_model_is_refused(probe):
    probe.outcome = []
    ok, kind, _detail = asyncio.run(gemini_pool.probe_credential(KEY_NEW))
    assert ok is False
    assert kind == "unsupported_model"


def test_an_empty_credential_is_never_sent_anywhere(probe):
    ok, kind, _detail = asyncio.run(gemini_pool.probe_credential(""))
    assert ok is False
    assert kind == "invalid_credential"
    assert probe.keys == []


def test_a_probe_detail_never_contains_the_credential(probe):
    """A body that quotes the key must not reach the stored detail.

    ``classify_error`` reads the status enum rather than the message, so the
    credential is usually absent by construction. This pins the other half too:
    whatever *does* come back goes through ``redact`` before it is returned, so
    even a body that echoed the key verbatim cannot store it.
    """
    probe.outcome = ProviderError(
        400, "INVALID_ARGUMENT", f"API key not valid: {KEY_NEW}"
    )
    ok, kind, detail = asyncio.run(gemini_pool.probe_credential(KEY_NEW))
    assert ok is False
    assert kind == "invalid_credential"
    assert KEY_NEW not in detail


def test_redact_removes_a_credential_from_arbitrary_text():
    assert gemini_pool.redact(f"boom {KEY_NEW} bang", KEY_NEW) == (
        "boom [redacted] bang"
    )
    assert gemini_pool.redact("nothing to do", KEY_NEW) == "nothing to do"
    assert gemini_pool.redact("empty key", "") == "empty key"


def test_a_rejected_credential_does_not_get_a_cached_client(probe, monkeypatch):
    """A rejected key must not live in the process's client cache."""
    gemini_pool.reset_clients()
    probe.outcome = ProviderError(400, "INVALID_ARGUMENT", "API key not valid")
    asyncio.run(gemini_pool.probe_credential(KEY_NEW))
    assert gemini_pool._clients == {}


# ── The screens ───────────────────────────────────────────────────────────
def _populate(pool) -> None:
    """Give the accounts real counters and real per-model state.

    This exists because the empty-pool render test was not enough. A screen that
    reads a field the state does not carry only fails once there *is* state to
    read — `models_view` read `last_use` for a while and every test passed,
    because no test ever had a model row. The live container, whose accounts have
    been answering for a day, found it on the first render.
    """
    for account in pool.accounts:
        account.note_request(1000.0)
        account.note_failure(
            gemini_pool.Failure("rate_limited", gemini_pool.SCOPE_MODEL,
                                retryable=True, detail="429", cooldown=60),
            1001.0,
        )
        account.note_success(1002.0)
        state = account.model(pool.models[0])
        state.note_request(1000.0)
        state.note_failure(
            gemini_pool.Failure("rate_limited", gemini_pool.SCOPE_MODEL,
                                retryable=True, detail="429", cooldown=60),
            1001.0,
        )


def test_every_screen_renders_with_populated_state(monkeypatch):
    """Every screen, every workload, against accounts that have actually worked.

    The counters are the interesting part: a screen that reads a key the state
    does not carry renders fine on an untouched account and raises on a real one.
    """
    _pools_config(monkeypatch)
    pools = gemini_pool.build_pools()
    for pool in pools.values():
        _populate(pool)
    for workload in pools:
        slots = [a.slot for a in pools[workload].accounts]
        screens = [
            gemini_keys.overview(),
            gemini_keys.workload_view(workload),
            gemini_keys.usage_view(workload),
            gemini_keys.events_view(workload),
            gemini_keys.daily_view(workload),
            gemini_keys.remove_prompt(workload, slots[0] if slots else "1"),
            gemini_keys.add_prompt(workload, private=True),
        ]
        for slot in slots:
            screens.append(gemini_keys.account_view(workload, slot))
            screens.append(gemini_keys.models_view(workload, slot))
        for text, _rows in screens:
            assert text
            assert len(text) <= 4096


def test_every_screen_renders_for_every_workload(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    for workload in ("chat", "moderation", "awareness"):
        for text, rows in (
            gemini_keys.overview(),
            gemini_keys.workload_view(workload),
            gemini_keys.usage_view(workload),
            gemini_keys.events_view(workload),
            gemini_keys.daily_view(workload),
            gemini_keys.account_view(workload, "1"),
            gemini_keys.models_view(workload, "1"),
            gemini_keys.remove_prompt(workload, "1"),
            gemini_keys.add_prompt(workload, private=True),
        ):
            assert text
            assert len(text) <= 4096


def _many_accounts(monkeypatch, count=12):
    keys = [
        (str(index), f"AIzaSyFAKE00000000000000000000000000000{index:03d}")
        for index in range(1, count + 1)
    ]
    _pools_config(monkeypatch, chat_keys=tuple(keys))
    gemini_pool.build_pools()


def test_a_full_pool_renders_under_telegrams_limit(monkeypatch):
    """Telegram rejects a message over 4096 characters outright.

    A dozen accounts, each with a label and several counter lines, is the
    realistic worst case for a deployment that has been adding keys.
    """
    _many_accounts(monkeypatch)
    for text, _rows in (
        gemini_keys.overview(),
        gemini_keys.workload_view("chat"),
        gemini_keys.daily_view("chat"),
    ):
        assert len(text) <= 4096


def test_a_screen_that_would_not_fit_is_cut_and_says_so(monkeypatch):
    """The cap is exercised directly, because the realistic pool still fits."""
    _many_accounts(monkeypatch)
    monkeypatch.setattr(gemini_keys, "MAX_CHARS", 600)
    text, _rows = gemini_keys.workload_view("chat")
    assert len(text) <= 600
    assert gemini_keys.TRUNCATED_NOTE in text


def test_a_screen_that_fits_is_not_truncated(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    text, _rows = gemini_keys.workload_view("chat")
    assert gemini_keys.TRUNCATED_NOTE not in text


def test_the_render_cap_is_under_telegrams_hard_limit():
    assert gemini_keys.MAX_CHARS < 4096


def test_no_screen_renders_a_credential(monkeypatch):
    _pools_config(monkeypatch)
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    gemini_pool.reload()
    for text, _rows in (
        gemini_keys.overview(),
        gemini_keys.workload_view("chat"),
        gemini_keys.account_view("chat", key_store.slot_for(KEY_NEW)),
        gemini_keys.models_view("chat", key_store.slot_for(KEY_NEW)),
        gemini_keys.usage_view("chat"),
        gemini_keys.events_view("chat"),
        gemini_keys.daily_view("chat"),
        gemini_keys.remove_prompt("chat", key_store.slot_for(KEY_NEW)),
    ):
        assert KEY_NEW not in text
        assert KEY_ENV not in text


def test_a_runtime_account_is_labelled_as_one_and_an_environment_one_is_not(
    monkeypatch,
):
    _pools_config(monkeypatch)
    entry, _ = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    gemini_pool.reload()
    text, _rows = gemini_keys.workload_view("chat")
    assert gemini_keys.SOURCE_LABELS["runtime"] in text
    assert gemini_keys.SOURCE_LABELS["env"] in text
    assert entry.masked in text


def test_the_usage_screen_says_what_it_does_not_measure(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    text, _rows = gemini_keys.usage_view("chat")
    assert "توکن" in text
    assert "درخواست" in text


def test_an_unknown_workload_or_account_is_a_sentence_not_a_crash(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    assert gemini_keys.workload_view("nope")[0] == gemini_keys.TEXT_UNKNOWN_WORKLOAD
    assert (
        gemini_keys.account_view("chat", "nope")[0] == gemini_keys.TEXT_UNKNOWN_ACCOUNT
    )


def test_only_a_runtime_credential_offers_a_remove_button(monkeypatch):
    _pools_config(monkeypatch)
    entry, _ = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    gemini_pool.reload()
    prompt = f"{gemini_keys.PREFIX}-:chat:"
    env_rows = gemini_keys.account_view("chat", "1")[1]
    runtime_rows = gemini_keys.account_view("chat", entry.slot)[1]
    assert not any(prompt in data for row in env_rows for _label, data in row)
    assert any(prompt in data for row in runtime_rows for _label, data in row)


def test_the_remove_confirmation_warns_when_it_would_empty_the_workload(monkeypatch):
    _pools_config(monkeypatch, chat_keys=())
    entry, _ = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    gemini_pool.reload()
    text, _rows = gemini_keys.remove_prompt("chat", entry.slot)
    assert "⚠️" in text


# ── The callback payload ──────────────────────────────────────────────────
def test_parse_splits_a_payload_into_three_short_parts():
    assert gemini_keys.parse("gk:a:awareness:shared1") == ("a", "awareness", "shared1")
    assert gemini_keys.parse("gk:home") == ("home", "", "")


def test_parse_refuses_a_foreign_namespace():
    assert gemini_keys.parse("adm:c:1:2:m:0") is None
    assert gemini_keys.parse("report_delete") is None
    assert gemini_keys.parse("") is None


def test_parse_truncates_an_absurdly_long_payload():
    verb, first, second = gemini_keys.parse("gk:w:" + "x" * 500 + ":y")
    assert len(first) == 40


# ── The pending prompt ────────────────────────────────────────────────────
def test_an_armed_prompt_is_returned_and_can_be_cleared():
    assert gemini_keys.begin_add(OWNER, "chat") is True
    assert gemini_keys.pending_for(OWNER) == "chat"
    gemini_keys.clear_pending(OWNER)
    assert gemini_keys.pending_for(OWNER) == ""


def test_an_armed_prompt_expires_on_its_own(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_KEY_ADD_TTL_SECONDS", 60)
    gemini_keys.begin_add(OWNER, "chat", now=1000.0)
    assert gemini_keys.pending_for(OWNER, now=1030.0) == "chat"
    assert gemini_keys.pending_for(OWNER, now=1061.0) == ""


def test_a_prompt_cannot_be_armed_for_a_workload_that_is_not_managed():
    assert gemini_keys.begin_add(OWNER, "moderation") is False
    assert gemini_keys.pending_for(OWNER) == ""


def test_the_add_prompt_refuses_to_offer_itself_in_a_group():
    text, _rows = gemini_keys.add_prompt("chat", private=False)
    assert text == gemini_keys.TEXT_ADD_NEEDS_PRIVATE.format(label="گفتگو")


# ── /keys, the command ────────────────────────────────────────────────────
def test_the_owner_gets_the_dashboard(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    run_handler(main.cmd_keys, message_update(bot, actor=OWNER), ctx_for(bot))
    assert bot.sent
    assert "کلیدهای Gemini" in bot.sent[0]


def test_a_stranger_gets_nothing_but_a_refusal(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    run_handler(main.cmd_keys, message_update(bot, actor=STRANGER), ctx_for(bot))
    assert bot.sent == [gemini_keys.TEXT_DENIED]
    rows = db.audit_recent()
    assert rows[0]["action"] == "keys.view"
    assert rows[0]["outcome"] != "ok"


# ── The callbacks ─────────────────────────────────────────────────────────
def test_a_stranger_pressing_a_button_is_refused_and_audited(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(bot, actor=STRANGER, data="gk:w:chat")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert query.edits == []
    assert gemini_keys.TEXT_DENIED in query.answers
    assert db.audit_recent()[0]["action"] == "keys.view"


def test_the_owner_can_walk_the_screens(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    for data in ("gk:home", "gk:w:chat", "gk:a:chat:1", "gk:m:chat:1",
                 "gk:u:chat", "gk:e:chat", "gk:h:chat"):
        update, query = callback_update(bot, data=data)
        run_handler(main.on_key_callback, update, ctx_for(bot))
        assert query.edits, data


def test_a_crafted_payload_cannot_open_a_workload_that_is_not_managed(monkeypatch):
    """The screen is shown — nothing is hidden — but it offers no write button."""
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(bot, data="gk:w:moderation")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert query.edits
    assert "افزودن کلید" not in query.edits[0]


def test_an_unknown_verb_is_a_stale_button(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    update, query = callback_update(bot, data="gk:zzz:chat")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert query.answers == [gemini_keys.TEXT_STALE]


def test_an_add_press_in_a_group_never_arms_the_prompt(monkeypatch):
    """A key typed into a group is already published, so the prompt is refused."""
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(
        bot, data="gk:+:chat", chat_id=CHAT, chat_type="supergroup"
    )
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert gemini_keys.pending_for(OWNER) == ""
    assert query.edits
    assert "چت خصوصی" in query.edits[0]


def test_an_add_press_for_an_unmanaged_workload_is_refused(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(bot, data="gk:+:moderation")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert gemini_keys.pending_for(OWNER) == ""
    assert gemini_keys.TEXT_NOT_MANAGED in query.answers


# ── The key message: the isolation requirement ────────────────────────────
def test_a_key_sent_in_private_is_consumed_and_stops_the_update(monkeypatch, probe):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    update = message_update(bot, actor=OWNER, text=KEY_NEW)
    ctx = ctx_for(bot)
    stopped = run_handler(main.on_key_message, update, ctx)
    assert stopped is True
    assert (PRIVATE, 11) in bot.deleted
    ctx.application.drain()
    assert KEY_NEW not in " ".join(bot.sent)


def test_a_key_message_from_a_stranger_is_left_to_the_dispatcher(monkeypatch, probe):
    _pools_config(monkeypatch)
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    update = message_update(bot, actor=STRANGER, text=KEY_NEW)
    stopped = run_handler(main.on_key_message, update, ctx_for(bot))
    assert stopped is False
    assert bot.deleted == []


def test_a_key_in_a_group_is_never_consumed(monkeypatch, probe):
    """Group text belongs to acquisition and moderation, not to this."""
    _pools_config(monkeypatch)
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    update = message_update(
        bot, actor=OWNER, text=KEY_NEW, chat_id=CHAT, chat_type="supergroup"
    )
    stopped = run_handler(main.on_key_message, update, ctx_for(bot))
    assert stopped is False
    assert bot.deleted == []


def test_an_ordinary_message_while_a_prompt_is_armed_is_left_alone(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    update = message_update(bot, actor=OWNER, text="سلام، امروز چطوری؟")
    stopped = run_handler(main.on_key_message, update, ctx_for(bot))
    assert stopped is False
    assert gemini_keys.pending_for(OWNER) == "chat"


def test_a_long_token_that_is_not_a_key_is_reported_without_being_consumed(
    monkeypatch,
):
    _pools_config(monkeypatch)
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    update = message_update(bot, actor=OWNER, text="!" * 40)
    stopped = run_handler(main.on_key_message, update, ctx_for(bot))
    assert stopped is False
    assert bot.sent == [gemini_keys.TEXT_ADD_BAD_SHAPE]
    assert gemini_keys.pending_for(OWNER) == "chat"


def test_a_message_with_no_armed_prompt_is_never_consumed(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    update = message_update(bot, actor=OWNER, text=KEY_NEW)
    stopped = run_handler(main.on_key_message, update, ctx_for(bot))
    assert stopped is False
    assert bot.deleted == []


# ── The add flow, end to end ──────────────────────────────────────────────
def test_the_whole_add_flow_stores_the_key_and_rebuilds_the_pool(
    monkeypatch, probe
):
    _pools_config(monkeypatch)
    assert len(gemini_pool.build_pools()["chat"].accounts) == 1
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(
        main.on_key_message,
        message_update(bot, actor=OWNER, text=KEY_NEW),
        ctx,
    )
    ctx.application.drain()

    entry = key_store.entry_for("chat", key_store.slot_for(KEY_NEW))
    assert entry is not None
    assert len(gemini_pool.pool_for("chat").accounts) == 2
    assert bot.edits  # the notice was edited with the result
    assert entry.slot in bot.edits[-1]


def test_the_add_flow_records_the_slot_and_never_the_credential(
    monkeypatch, probe
):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(main.on_key_message, message_update(bot, actor=OWNER, text=KEY_NEW), ctx)
    ctx.application.drain()

    rows = db.audit_recent()
    actions = [row["action"] for row in rows]
    assert "keys.add" in actions
    assert all(KEY_NEW not in json.dumps(row) for row in rows)
    entry = key_store.entry_for("chat", key_store.slot_for(KEY_NEW))
    assert entry.masked in json.dumps(rows)


def test_a_failed_verification_stores_nothing(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    failing = Probe(ProviderError(400, "INVALID_ARGUMENT", "API key not valid"))
    monkeypatch.setattr(gemini_pool, "build_client", failing.build)
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(main.on_key_message, message_update(bot, actor=OWNER, text=KEY_NEW), ctx)
    ctx.application.drain()

    assert key_store.entries() == []
    assert len(gemini_pool.pool_for("chat").accounts) == 1
    assert bot.edits
    assert "نامعتبر" in bot.edits[-1]
    assert KEY_NEW not in bot.edits[-1]


def test_the_add_flow_reports_a_duplicate_rather_than_a_second_account(
    monkeypatch, probe
):
    _pools_config(monkeypatch)
    key_store.add("chat", KEY_NEW, actor_id=OWNER)
    gemini_pool.build_pools()
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(main.on_key_message, message_update(bot, actor=OWNER, text=KEY_NEW), ctx)
    ctx.application.drain()
    assert len(key_store.entries()) == 1
    assert bot.edits
    assert "از قبل" in bot.edits[-1]


def test_a_message_that_cannot_be_deleted_is_reported(monkeypatch, probe):
    """Silence would be wrong: the credential is still in the chat history."""
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot(delete_fails=True)
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(main.on_key_message, message_update(bot, actor=OWNER, text=KEY_NEW), ctx)
    ctx.application.drain()
    assert bot.edits
    assert "حذف نشد" in bot.edits[-1]


def test_the_add_flow_leaves_no_credential_in_the_database(monkeypatch, probe):
    """The whole point: the database is copyable, the store file is not."""
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(main.on_key_message, message_update(bot, actor=OWNER, text=KEY_NEW), ctx)
    ctx.application.drain()

    dumped = json.dumps(db.audit_recent())
    assert KEY_NEW not in dumped
    assert KEY_NEW not in json.dumps(gemini_pool.status_report())
    for row in db.pool_accounts():
        assert KEY_NEW not in json.dumps(row)


# ── The remove flow ───────────────────────────────────────────────────────
def test_the_owner_can_remove_a_runtime_credential(monkeypatch):
    _pools_config(monkeypatch)
    entry, _ = key_store.add("chat", KEY_NEW, actor_id=OWNER)
    assert len(gemini_pool.build_pools()["chat"].accounts) == 2
    bot = FakeBot()
    update, query = callback_update(bot, data=f"gk:!:{'chat'}:{entry.slot}")
    run_handler(main.on_key_callback, update, ctx_for(bot))

    assert key_store.entries() == []
    assert len(gemini_pool.pool_for("chat").accounts) == 1
    assert query.edits
    assert entry.slot in query.edits[-1]
    assert db.audit_recent()[0]["action"] == "keys.remove"
    assert db.audit_recent()[0]["outcome"] == "ok"


def test_an_environment_slot_cannot_be_removed_through_the_callback(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(bot, data="gk:!:chat:1")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert query.edits == [gemini_keys.TEXT_REMOVE_MISSING]
    assert len(gemini_pool.pool_for("chat").accounts) == 1


def test_removing_something_already_gone_says_so(monkeypatch):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(bot, data="gk:!:chat:kdeadbeef")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert query.edits == [gemini_keys.TEXT_REMOVE_MISSING]


# ── The store breaking must not break the bot ────────────────────────────
def test_a_broken_store_does_not_stop_the_dashboard_rendering(monkeypatch):
    """A credential file must not be able to take the screens down with it."""
    _pools_config(monkeypatch)
    with open(config.GEMINI_KEY_STORE_PATH, "w", encoding="utf-8") as handle:
        handle.write("{ broken")
    gemini_pool.build_pools()
    bot = FakeBot()
    update, query = callback_update(bot, data="gk:w:chat")
    run_handler(main.on_key_callback, update, ctx_for(bot))
    assert query.edits
    assert "گفتگو" in query.edits[0]


def test_a_broken_store_refuses_an_actual_add(monkeypatch, probe):
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    with open(config.GEMINI_KEY_STORE_PATH, "w", encoding="utf-8") as handle:
        handle.write("{ broken")
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    ctx = ctx_for(bot)
    run_handler(main.on_key_message, message_update(bot, actor=OWNER, text=KEY_NEW), ctx)
    ctx.application.drain()
    assert bot.edits
    assert bot.edits[-1].startswith(gemini_keys.TEXT_STORE_BROKEN)
    with open(config.GEMINI_KEY_STORE_PATH, encoding="utf-8") as handle:
        assert "broken" in handle.read()


# ── Regression: nothing else changed ─────────────────────────────────────
def test_pool_events_can_still_be_read_without_a_workload():
    db.init()
    db.pool_event_add("chat", "model_failover", slot="1", model="m")
    assert len(db.pool_events(5)) == 1
    assert len(db.pool_events(5, workload="chat")) == 1
    assert db.pool_events(5, workload="awareness") == []


def test_the_key_store_is_not_a_second_pool():
    """The module that stores credentials must not be able to make a call."""
    assert not hasattr(key_store, "generate")
    assert not hasattr(key_store, "Pool")
    assert not hasattr(key_store, "pool_for")


def test_the_dashboard_never_reaches_telegram_by_itself():
    """Rendering is pure; only the handlers in main.py send anything."""
    import inspect

    source = inspect.getsource(gemini_keys)
    assert "send_message" not in source
    assert "edit_message_text" not in source


# ── Discoverability: the command menu and the entry button ────────────────
# The feature worked and the owner could not find it. That is a defect in its own
# right — "it is there if you already know the name" is not a user interface —
# and these tests pin the two things that make it findable: the menu Telegram is
# told about, and the button on the two screens a person actually lands on.
#
# The menu is derived from the same functions that register the handlers, so the
# central invariant is not "the menu has a fixed list" but "the menu and the
# registrations are the same set". A hard-coded expectation would pass while the
# two drifted.
def _registered_names() -> set[str]:
    return {
        name
        for source in (
            main.admin_command_handlers(),
            main.chat_command_handlers(),
            main.transcribe_command_handlers(),
        )
        for name, _handler in source
    }


def test_the_menu_advertises_exactly_the_commands_that_are_registered(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    public, owner = main.command_menu()
    assert {name for name, _label in owner} == _registered_names()


def test_the_public_menu_is_a_subset_of_the_owner_menu(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    public, owner = main.command_menu()
    assert set(public) <= set(owner)


def test_a_stranger_is_not_told_the_bot_has_a_credential_dashboard(monkeypatch):
    """The public list is what every chat sees. It must not name `/keys`."""
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    public, owner = main.command_menu()
    names = {name for name, _label in public}
    assert config.GEMINI_KEYS_COMMAND not in names
    assert "ban" not in names and "promote" not in names
    # …and the owner's list does have it, or the feature is still unfindable.
    assert config.GEMINI_KEYS_COMMAND in {name for name, _label in owner}


def test_the_conversational_commands_disappear_with_the_assistant(monkeypatch):
    """A menu entry for a command that was never registered is a dead button."""
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", False)
    public, _owner = main.command_menu()
    names = {name for name, _label in public}
    assert "start" not in names and "reset" not in names
    assert main.chat_command_handlers() == ()


def test_the_voice_command_follows_its_setting(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_COMMAND", "transcribe")
    assert [name for name, _ in main.transcribe_command_handlers()] == ["transcribe"]
    public, _owner = main.command_menu()
    assert "transcribe" in {name for name, _label in public}

    monkeypatch.setattr(config, "TRANSCRIBE_COMMAND", "")
    assert main.transcribe_command_handlers() == ()
    public, _owner = main.command_menu()
    assert "transcribe" not in {name for name, _label in public}


def test_no_menu_label_is_empty_or_absurdly_long():
    """Telegram rejects a description over 256 characters, silently at best."""
    public, owner = main.command_menu()
    for name, label in (*public, *owner):
        assert name and label
        assert len(label) <= 256, name


def test_publishing_the_menu_tells_telegram_both_scopes(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    bot = FakeBot()
    asyncio.run(main._publish_command_menu(SimpleNamespace(bot=bot)))
    assert len(bot.commands) == 2
    public, owner = main.command_menu()
    assert bot.commands[0] == (public, None)
    commands, scope = bot.commands[1]
    assert commands == owner
    # A chat-scoped list, aimed at the owner's own chat and nobody else's.
    assert scope is not None and scope.chat_id == OWNER


def test_publishing_the_menu_does_not_fail_the_start_when_telegram_is_down(caplog):
    """A missing menu is cosmetic; refusing to boot over one would be an outage."""
    bot = FakeBot(set_commands_fails=True)
    with caplog.at_level("WARNING"):
        asyncio.run(main._publish_command_menu(SimpleNamespace(bot=bot)))
    assert "could not publish the command menu" in caplog.text
    assert bot.commands == []


def test_publishing_the_menu_does_not_fail_the_start_on_an_unexpected_error(caplog):
    """This runs inside `post_init`, so anything escaping it stops the bot.

    A bot object without `set_my_commands` is the cheapest stand-in for "some
    failure nobody predicted": the point is that *no* failure here is allowed to
    take the bot down, and that it is still logged rather than swallowed.
    """
    with caplog.at_level("WARNING"):
        asyncio.run(main._publish_command_menu(SimpleNamespace(bot=SimpleNamespace())))
    assert "could not publish the command menu" in caplog.text


def test_the_menu_is_not_published_when_there_is_no_owner(monkeypatch):
    """Without an owner there is no chat to scope the administrative list to."""
    monkeypatch.setattr(config, "OWNER_USER_ID", 0)
    bot = FakeBot()
    asyncio.run(main._publish_command_menu(SimpleNamespace(bot=bot)))
    assert len(bot.commands) == 1


def test_the_owner_gets_a_button_to_the_dashboard_on_start(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    run_handler(main.on_chat_start, message_update(bot, actor=OWNER), ctx_for(bot))
    keyboard = bot.markups[0]
    assert keyboard is not None
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [f"{gemini_keys.PREFIX}home"]
    assert buttons[0].text == gemini_keys.TEXT_BUTTON_OPEN


def test_a_stranger_gets_no_button_on_start(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    run_handler(main.on_chat_start, message_update(bot, actor=STRANGER), ctx_for(bot))
    assert bot.sent, "the greeting is still sent"
    assert bot.markups == [None]


def test_whoami_offers_the_button_to_the_owner_and_to_nobody_else(monkeypatch):
    _pools_config(monkeypatch)
    bot = FakeBot()
    run_handler(main.cmd_whoami, message_update(bot, actor=OWNER), ctx_for(bot))
    assert bot.markups[0] is not None
    run_handler(main.cmd_whoami, message_update(bot, actor=STRANGER), ctx_for(bot))
    assert bot.markups[1] is None


def test_the_button_payload_opens_a_real_screen(monkeypatch):
    """The button must not be a dead end, so its payload goes through the parser."""
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    from app import rbac

    keyboard = main._owner_menu_keyboard(rbac.resolve(OWNER))
    payload = keyboard.inline_keyboard[0][0].callback_data
    assert payload == f"{gemini_keys.PREFIX}home"
    parsed = gemini_keys.parse(payload)
    assert parsed is not None
    verb, _first, _second = parsed
    assert verb in main._KEY_SCREENS


def test_the_owner_button_is_absent_for_a_principal_who_is_not_the_owner():
    from app import rbac

    assert main._owner_menu_keyboard(rbac.resolve(STRANGER)) is None


# ── The entry filter: a command is never key material ─────────────────────
def _ptb_update(text, *, private=True, edited=False):
    from telegram import Chat, Message, MessageEntity, Update, User

    user = User(id=OWNER, first_name="Owner", is_bot=False)
    chat = Chat(id=PRIVATE if private else CHAT,
                type=Chat.PRIVATE if private else Chat.SUPERGROUP)
    entities = None
    if text.startswith("/"):
        command = text.split()[0]
        entities = [
            MessageEntity(
                type=MessageEntity.BOT_COMMAND, offset=0, length=len(command)
            )
        ]
    message = Message(
        message_id=1, date=None, chat=chat, from_user=user, text=text,
        entities=entities,
    )
    if edited:
        return Update(update_id=1, edited_message=message)
    return Update(update_id=1, message=message)


def test_the_entry_filter_accepts_a_private_key_message():
    accepts = main.key_entry_filter()
    assert accepts.check_update(_ptb_update(KEY_NEW))


def test_the_entry_filter_refuses_a_command():
    """The registered filter is the thing under test, not a copy of it."""
    accepts = main.key_entry_filter()
    assert not accepts.check_update(_ptb_update("/keys"))
    assert not accepts.check_update(_ptb_update(f"/keys {KEY_NEW}"))


def test_the_entry_filter_refuses_a_group_and_an_edit():
    accepts = main.key_entry_filter()
    assert not accepts.check_update(_ptb_update(KEY_NEW, private=False))
    assert not accepts.check_update(_ptb_update(KEY_NEW, edited=True))


def test_the_entry_filter_refuses_media():
    """A caption is not a credential, and this handler must not see one."""
    from telegram import Chat, Message, Update, User

    message = Message(
        message_id=1,
        date=None,
        chat=Chat(id=PRIVATE, type=Chat.PRIVATE),
        from_user=User(id=OWNER, first_name="Owner", is_bot=False),
        photo=[SimpleNamespace(file_id="x", file_unique_id="y", width=1, height=1)],
        caption=KEY_NEW,
    )
    assert not main.key_entry_filter().check_update(
        Update(update_id=1, message=message)
    )


def test_a_command_with_a_prompt_armed_reaches_the_command_handler(monkeypatch):
    """End to end: `/keys` while armed opens the dashboard, it is not swallowed.

    The filter is asserted directly above; this is the behaviour the owner cares
    about, driven through the real handler so the two cannot diverge.
    """
    _pools_config(monkeypatch)
    gemini_pool.build_pools()
    bot = FakeBot()
    gemini_keys.begin_add(OWNER, "chat")
    stopped = run_handler(
        main.on_key_message, message_update(bot, actor=OWNER, text="/keys"),
        ctx_for(bot),
    )
    assert stopped is False
    assert bot.deleted == []
    assert gemini_keys.pending_for(OWNER) == "chat"

