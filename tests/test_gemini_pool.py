"""One logical AI service, many independent accounts.

The brief this module answers asks for a lot of properties at once, and most of
them are only visible under failure. So the suite is built around a scripted
provider: a fake Google that can be told "this key, this model, answers with
this" or "this key, this model, raises this" — and that records every call it
received. Nothing here touches the network, and no test spends real quota.

Three ideas organise the file, because they are the three the implementation
turns on:

* **An account is not a model.** A 429 on one model must not disable the
  account; a project-level quota failure must not be treated as a model
  problem. Those are the two levels of failover and they are tested separately.
* **A key is never rendered.** Not in a log line, not in an owner message, not
  in the status report. Tested by asserting the literal secret is absent from
  every string the module can produce.
* **Workloads stay isolated.** A pool per workload, rows keyed by workload, and
  one workload's exhaustion must not silence another.
"""
import asyncio
import inspect
import json
import logging
import pathlib
import time

import pytest

from app import config, db, gemini_pool, main

# The credentials used throughout. Obviously fake, and shaped like the real
# thing only in length. They are literals in a test file on purpose: a test that
# reads a credential from the environment would silently stop testing anything
# the moment the environment changed.
KEY_A = "AIzaSyFAKE000000000000000000000000000000000A"
KEY_B = "AIzaSyFAKE000000000000000000000000000000000B"
KEY_C = "AIzaSyFAKE000000000000000000000000000000000C"

TEXT_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash"]
MEDIA_MODELS = ["gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-pro-latest"]
AUDIO_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-3.5-transcribe"]


# ── The scripted provider ─────────────────────────────────────────────────
class ProviderError(Exception):
    """Stands in for the SDK's own error types.

    A real SDK error carries the HTTP code in a body it renders into ``str()``,
    which is exactly how ``classify_error`` reads it — so these carry a ``code``
    attribute *and* a JSON body, and both routes are exercised.
    """

    def __init__(self, code: int, status: str, message: str = "", extra: dict | None = None):
        self.code = code
        self.status = status
        body = {"error": {"code": code, "status": status, "message": message}}
        body["error"].update(extra or {})
        super().__init__(json.dumps(body))


def rate_limited(model: str) -> ProviderError:
    """A per-model 429. The account behind it is still perfectly healthy."""
    return ProviderError(
        429,
        "RESOURCE_EXHAUSTED",
        "Quota exceeded",
        {"quotaId": f"GenerateRequestsPerMinutePerProjectPerModel-FreeTier-{model}"},
    )


def project_quota_exhausted() -> ProviderError:
    """A project-wide 429. No model switch can help; the next account is the fix."""
    return ProviderError(
        429,
        "RESOURCE_EXHAUSTED",
        "Quota exceeded for the project",
        {"quotaId": "GenerateRequestsPerProjectPerDay"},
    )


def invalid_key() -> ProviderError:
    return ProviderError(401, "UNAUTHENTICATED", "API key not valid")


def unsupported_model() -> ProviderError:
    return ProviderError(
        404,
        "NOT_FOUND",
        "is not found for API version v1beta, or is not supported for generateContent",
    )


def overloaded() -> ProviderError:
    return ProviderError(503, "UNAVAILABLE", "The model is overloaded")


def network_failure() -> ProviderError:
    return ProviderError(0, "CONNECTION_RESET", "connection reset by peer")


def bad_request() -> ProviderError:
    return ProviderError(400, "INVALID_ARGUMENT", "Request contains an invalid argument")


class _FakeModels:
    def __init__(self, provider, key):
        self._provider = provider
        self._key = key

    async def generate_content(self, *, model, contents, config):  # noqa: A002 - SDK's own name
        return await self._provider._generate(self._key, model, contents, config)

    async def list(self):
        return await self._provider._list(self._key)


class _FakeAio:
    def __init__(self, provider, key):
        self.models = _FakeModels(provider, key)


class FakeClient:
    def __init__(self, provider, key):
        self._provider = provider
        self._key = key
        self.aio = _FakeAio(provider, key)


class Response:
    def __init__(self, text="", pcm=b""):
        self.text = text
        self._pcm = pcm

    @property
    def candidates(self):
        if not self._pcm:
            return []
        part = type("Part", (), {"inline_data": type("D", (), {"data": self._pcm})()})()
        content = type("Content", (), {"parts": [part]})()
        return [type("Candidate", (), {"content": content})()]


class Provider:
    """A scripted stand-in for the Gemini API.

    ``script`` maps a credential to a list of outcomes applied in order. An
    outcome is either a response string or an exception to raise; the last entry
    repeats once the list is exhausted, so a script can say "this model always
    answers" without knowing how many times it will be asked.
    """

    def __init__(self):
        self.script: dict[str, dict[str, list]] = {}
        self.listed: dict[str, list | Exception] = {}
        self.calls: list[tuple[str, str]] = []
        self.listed_calls = 0

    # -- scripting --
    def always(self, key: str, model: str, text: str) -> "Provider":
        self.script.setdefault(key, {})[model] = [text]
        return self

    def then(self, key: str, model: str, *outcomes) -> "Provider":
        self.script.setdefault(key, {})[model] = list(outcomes)
        return self

    def answers(self, key: str, text: str) -> "Provider":
        """Every model on this credential answers with ``text``."""
        self.script[key] = {m: [text] for m in
                            set(TEXT_MODELS + MEDIA_MODELS + AUDIO_MODELS)}
        return self

    def models(self, key: str, names) -> "Provider":
        self.listed[key] = [
            {"name": f"models/{n}", "supported_generation_methods": ["generateContent"]}
            for n in names
        ]
        return self

    # -- the API --
    def client(self, key: str, timeout: float | None = None) -> FakeClient:
        """The pool's client seam. The deadline is accepted and ignored: these
        calls answer instantly, and the timeout path is tested separately."""
        return FakeClient(self, key)

    async def _generate(self, key: str, model: str, contents, config):  # noqa: A002
        self.calls.append((key, model))
        queue = self.script.get(key, {}).get(model)
        if not queue:
            raise AssertionError(f"unscripted call key={gemini_pool.mask(key)} model={model}")
        outcome = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(text=outcome)

    async def _list(self, key: str):
        self.listed_calls += 1
        result = self.listed.get(key)
        if isinstance(result, BaseException):
            raise result
        return result or []

    # -- assertions --
    def models_used(self, key: str) -> list[str]:
        return [m for k, m in self.calls if k == key]

    @property
    def total_calls(self) -> int:
        return len(self.calls)


@pytest.fixture
def provider(monkeypatch):
    """Install the fake provider over the pool's SDK seam.

    The real ``google.genai.types`` is kept, because the payload builders the
    workloads pass in are the real ones — ``_wire``, ``_contents``,
    ``_generation_config`` — and replacing them would mean the test no longer
    covers the thing that has actually broken before. Only the transport is
    faked. Nothing here opens a socket.

    Discovery is off by default: it is a real extra network call, and turning it
    on in every test would make "which model was chosen" depend on two
    mechanisms at once. ``test_discovery_*`` turns it on deliberately.
    """
    from google import genai
    from google.genai import types

    p = Provider()
    monkeypatch.setattr(gemini_pool, "_load_sdk", lambda: (genai, types))
    monkeypatch.setattr(gemini_pool, "_client_for", p.client)
    monkeypatch.setattr(config, "GEMINI_POOL_DISCOVERY_ENABLED", False)
    gemini_pool.reset_clients()
    return p


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    db.init()
    db.pool_reset()
    gemini_pool.reset_clients()
    monkeypatch.setattr(gemini_pool, "_pools", {})
    yield
    db.pool_reset()
    gemini_pool.reset_clients()


def make_pool(
    workload="intent",
    keys=(("1", KEY_A),),
    models=TEXT_MODELS,
    capabilities=frozenset({gemini_pool.TEXT}),
    *,
    retries=0,
    allow_experimental=False,
    backoff=0.0,
    rotate_models=False,
):
    return gemini_pool.Pool(
        workload,
        list(keys),
        list(models),
        capabilities,
        allow_experimental=allow_experimental,
        retries=retries,
        backoff=backoff,
        timeout=5.0,
        rotate_models=rotate_models,
    )


def call(pool, text="hello"):
    """One request through the pool, with a trivial payload builder."""
    return asyncio.run(
        gemini_pool.generate(
            pool,
            build_contents=lambda types: text,
            build_config=lambda types: {"cfg": True},
        )
    )


# ══ ACCOUNT TESTS ═════════════════════════════════════════════════════════
def test_one_account_answers(provider):
    provider.answers(KEY_A, "one")
    pool = make_pool()

    assert call(pool) == "one"
    assert pool.health()["accounts"] == 1
    assert pool.accounts[0].successes == 1


def test_multiple_accounts_load_as_separate_accounts(provider):
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B), ("3", KEY_C)))

    assert [a.slot for a in pool.accounts] == ["1", "2", "3"]
    assert {a.masked for a in pool.accounts} == {
        gemini_pool.mask(KEY_A),
        gemini_pool.mask(KEY_B),
        gemini_pool.mask(KEY_C),
    }
    assert len({a.fingerprint for a in pool.accounts}) == 3


def test_the_same_key_in_two_slots_is_one_account(provider):
    """Two slots holding one key are one project, so counting them twice would
    invent a quota that does not exist."""
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_A)))

    assert len(pool.accounts) == 1


def test_account_state_is_independent(provider):
    provider.answers(KEY_A, "a")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))
    first, second = pool.accounts

    call(pool)

    assert first.requests == 1
    assert second.requests == 0
    assert second.state == "ACTIVE"


def test_selection_prefers_the_least_recently_successful(provider):
    """Five configured accounts must not leave four of them unused."""
    provider.answers(KEY_A, "a").answers(KEY_B, "b").answers(KEY_C, "c")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B), ("3", KEY_C)))

    call(pool)
    call(pool)
    call(pool)

    assert [a.slot for a in pool.accounts if a.requests == 1] == ["1", "2", "3"]


def test_account_failover_when_the_project_quota_is_gone(provider):
    provider.then(KEY_A, TEXT_MODELS[0], project_quota_exhausted())
    provider.then(KEY_A, TEXT_MODELS[1], project_quota_exhausted())
    provider.then(KEY_A, TEXT_MODELS[2], project_quota_exhausted())
    provider.answers(KEY_B, "from B")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "from B"
    assert pool.accounts[0].state == "QUOTA_EXHAUSTED"
    assert pool.accounts[1].successes == 1


def test_an_invalid_credential_is_never_retried(provider):
    provider.then(KEY_A, TEXT_MODELS[0], invalid_key())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "b"

    first = pool.accounts[0]
    assert first.state == "INVALID"
    assert provider.models_used(KEY_A) == [TEXT_MODELS[0]]

    # A second request must not touch it at all: a revoked key does not start
    # working, and every attempt costs the request budget of a real message.
    before = provider.total_calls
    call(pool)
    assert provider.models_used(KEY_A) == [TEXT_MODELS[0]]
    assert provider.total_calls == before + 1


def test_a_rate_limited_account_is_not_the_same_as_a_dead_one(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.then(KEY_A, TEXT_MODELS[1], rate_limited(TEXT_MODELS[1]))
    provider.then(KEY_A, TEXT_MODELS[2], "a")
    pool = make_pool()

    assert call(pool) == "a"
    assert pool.accounts[0].state == "ACTIVE"
    assert pool.accounts[0].rate_limits == 2


def test_an_account_recovers_and_reports_it(provider):
    provider.then(KEY_A, TEXT_MODELS[0], project_quota_exhausted())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    call(pool)

    first = pool.accounts[0]
    assert first.state == "QUOTA_EXHAUSTED"

    # The cooldown passes, and the account answers again.
    first.cooldown_until = 0
    provider.answers(KEY_A, "a again")
    assert call(pool) == "a again"

    assert first.state == "ACTIVE"
    assert [e["kind"] for e in events("account_recovered")] == ["account_recovered"]


def events(kind: str = "", limit: int = 50) -> list[dict]:
    """The recorded pool events, oldest first, optionally of one kind.

    There is no ``_collect`` notifier helper any more. The pool used to take an
    async callback that a test could hand a list to, and that callback was the
    Telegram send; with it gone, the only way to observe what the pool did is to
    read what it wrote down — which is also the only way an operator does it.
    """
    rows = [e for e in db.pool_events(limit) if not kind or e["kind"] == kind]
    return sorted(rows, key=lambda e: e["id"])


def test_persistence_across_a_restart(provider):
    """Counters and cooldowns are database facts, not process facts."""
    provider.answers(KEY_A, "a")
    pool = make_pool()
    call(pool)
    pool.accounts[0].cooldown_until = int(time.time()) + 600
    pool.accounts[0].state = "RATE_LIMITED"
    pool.accounts[0].save()

    reborn = make_pool()
    account = reborn.accounts[0]

    assert account.requests == 1
    assert account.successes == 1
    assert account.state == "RATE_LIMITED"
    assert account.cooldown_until > int(time.time())
    assert account.usable(time.time()) is False


def test_a_recovering_account_comes_back_active(provider):
    """A state of RECOVERING is a promise the dead process cannot keep."""
    db.pool_account_save("intent", "1", fingerprint=gemini_pool.fingerprint(KEY_A),
                         masked=gemini_pool.mask(KEY_A), state="RECOVERING")

    assert make_pool().accounts[0].state == "ACTIVE"


# ══ MODEL TESTS ═══════════════════════════════════════════════════════════
def test_the_primary_model_is_preferred(provider):
    provider.answers(KEY_A, "primary")
    pool = make_pool()

    call(pool)

    assert provider.models_used(KEY_A) == [TEXT_MODELS[0]]


def test_a_model_rate_limit_falls_back_to_a_sibling_model(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "sibling")
    pool = make_pool()

    assert call(pool) == "sibling"
    assert provider.models_used(KEY_A) == [TEXT_MODELS[0], TEXT_MODELS[1]]
    # The account is untouched by a model-level failure. This is the whole
    # distinction: one model hitting a limit is not an account going away.
    assert pool.accounts[0].state == "ACTIVE"
    assert pool.accounts[0].successes == 1


def test_the_limited_model_is_benched_not_the_account(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    pool = make_pool()

    call(pool)

    state = pool.accounts[0].model_states[TEXT_MODELS[0]]
    assert state.state == "RATE_LIMITED"
    assert state.usable(time.time()) is False
    assert pool.accounts[0].model_states[TEXT_MODELS[1]].state == "ACTIVE"


def test_a_missing_model_is_disabled_outright(provider):
    provider.then(KEY_A, TEXT_MODELS[0], unsupported_model())
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    pool = make_pool()

    call(pool)

    assert pool.accounts[0].model_states[TEXT_MODELS[0]].state == "DISABLED"
    # And it is not tried again on the next request.
    call(pool)
    assert provider.models_used(KEY_A).count(TEXT_MODELS[0]) == 1


def test_a_model_the_provider_does_not_list_is_skipped(provider):
    provider.models(KEY_A, [TEXT_MODELS[1]])
    provider.always(KEY_A, TEXT_MODELS[1], "listed")
    pool = make_pool()
    monkeypatch_enabled = True
    config.GEMINI_POOL_DISCOVERY_ENABLED = True
    try:
        assert call(pool) == "listed"
    finally:
        config.GEMINI_POOL_DISCOVERY_ENABLED = False

    assert provider.models_used(KEY_A) == [TEXT_MODELS[1]]


def test_discovery_failure_does_not_block_the_request(provider):
    """Discovery is an optimisation. Losing it must mean "do not filter"."""
    provider.listed[KEY_A] = RuntimeError("discovery exploded")
    provider.answers(KEY_A, "still answered")
    config.GEMINI_POOL_DISCOVERY_ENABLED = True
    try:
        assert call(make_pool()) == "still answered"
    finally:
        config.GEMINI_POOL_DISCOVERY_ENABLED = False


def test_a_text_only_model_is_never_offered_to_a_media_workload():
    pool = make_pool(
        workload="moderation",
        models=["gemma-3-27b-it", *MEDIA_MODELS],
        capabilities=frozenset({gemini_pool.TEXT, gemini_pool.IMAGE, gemini_pool.VIDEO}),
    )

    offered = pool.models_for(pool.accounts[0], time.time())

    assert "gemma-3-27b-it" not in offered
    assert offered == MEDIA_MODELS


def test_an_audio_workload_is_only_offered_audio_models():
    pool = make_pool(
        workload="transcribe",
        models=["gemma-3-27b-it", *AUDIO_MODELS],
        capabilities=frozenset({gemini_pool.AUDIO_IN}),
    )

    offered = pool.models_for(pool.accounts[0], time.time())

    # A text-only model here would not error — it would invent a transcript,
    # which is the worst available failure for this workload.
    assert "gemma-3-27b-it" not in offered
    assert offered == ["gemini-flash-latest", "gemini-2.5-flash", "gemini-3.5-transcribe"]


def test_an_image_generation_model_is_never_used():
    assert gemini_pool.capabilities_of("gemini-2.5-flash-image") is None
    assert gemini_pool.capabilities_of("imagen-4.0-generate-001") is None


def test_generation_families_are_excluded():
    for name in ("veo-3.0-generate-001", "lyria-3-pro", "text-embedding-005",
                 "gemini-2.5-computer-use-preview", "aqa"):
        assert gemini_pool.capabilities_of(name) is None, name


def test_preview_models_are_excluded_unless_opted_in():
    assert gemini_pool.is_experimental("gemini-2.5-pro-preview-tts") is True
    pool = make_pool(models=["gemini-2.5-pro-preview-tts", TEXT_MODELS[0]])
    assert pool.models_for(pool.accounts[0], time.time()) == [TEXT_MODELS[0]]

    opted = make_pool(models=["gemini-2.5-pro-preview-tts"], allow_experimental=True)
    assert opted.models_for(opted.accounts[0], time.time()) == [
        "gemini-2.5-pro-preview-tts"
    ]


def test_the_tts_workload_gets_only_speech_models():
    pool = make_pool(
        workload="tts",
        models=["gemini-flash-latest", "gemini-3.1-flash-tts-preview"],
        capabilities=frozenset({gemini_pool.AUDIO_OUT}),
        allow_experimental=True,
    )

    assert pool.models_for(pool.accounts[0], time.time()) == [
        "gemini-3.1-flash-tts-preview"
    ]


# ══ FAILOVER TESTS ════════════════════════════════════════════════════════
def test_a_project_failure_moves_to_the_next_account(provider):
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "b"
    assert pool.accounts[0].state == "QUOTA_EXHAUSTED"
    assert pool.accounts[0].quota_events >= 1


def test_a_transient_provider_error_is_retried_then_succeeds(provider):
    provider.then(KEY_A, TEXT_MODELS[0], overloaded(), "recovered")
    pool = make_pool(retries=1)

    assert call(pool) == "recovered"
    assert provider.models_used(KEY_A) == [TEXT_MODELS[0], TEXT_MODELS[0]]


def test_a_network_failure_falls_through_to_the_next_model(provider):
    provider.then(KEY_A, TEXT_MODELS[0], network_failure())
    provider.always(KEY_A, TEXT_MODELS[1], "next model")
    pool = make_pool()

    assert call(pool) == "next model"


def test_a_503_exhausts_the_models_then_the_accounts(provider):
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, overloaded())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "b"


def test_a_bad_request_stops_immediately_and_spends_nothing_else(provider):
    """The payload is wrong; every account would answer the same way."""
    provider.then(KEY_A, TEXT_MODELS[0], bad_request())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    with pytest.raises(gemini_pool.PoolUnavailable) as caught:
        call(pool)

    assert caught.value.kind == "bad_request"
    assert provider.total_calls == 1
    assert provider.models_used(KEY_B) == []


def test_the_attempt_budget_is_bounded(provider, monkeypatch):
    """No infinite loop, and no unbounded spend on one logical request."""
    monkeypatch.setattr(config, "GEMINI_POOL_MAX_ATTEMPTS", 4)
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, overloaded())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)), retries=3)

    with pytest.raises(gemini_pool.PoolUnavailable):
        call(pool)

    assert provider.total_calls == 4


def test_retries_are_bounded_per_model(provider):
    provider.then(KEY_A, TEXT_MODELS[0], overloaded(), overloaded(), overloaded())
    provider.always(KEY_A, TEXT_MODELS[1], "second")
    pool = make_pool(retries=1)

    assert call(pool) == "second"
    assert provider.models_used(KEY_A) == [TEXT_MODELS[0], TEXT_MODELS[0], TEXT_MODELS[1]]


def test_backoff_is_exponential_and_jittered():
    pool = make_pool(backoff=1.0)
    values = [gemini_pool._backoff(pool, attempt) for attempt in range(4)]

    assert values[0] >= 1.0
    assert values[1] >= 2.0
    assert values[2] >= 4.0
    # Jitter means no two waits in one process are identical, which is what
    # stops four workloads retrying in lockstep against a rate-limited provider.
    assert len({round(v, 6) for v in values}) == len(values)


def test_the_request_completes_after_two_levels_of_failover(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.then(KEY_A, TEXT_MODELS[1], rate_limited(TEXT_MODELS[1]))
    provider.then(KEY_A, TEXT_MODELS[2], project_quota_exhausted())
    provider.answers(KEY_B, "the answer")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "the answer"
    assert [c[0] for c in provider.calls] == [KEY_A] * 3 + [KEY_B]


def test_an_empty_pool_raises_rather_than_hanging(provider):
    pool = make_pool(keys=())

    with pytest.raises(gemini_pool.PoolUnavailable) as caught:
        call(pool)

    assert caught.value.kind == "no_account"


def test_a_fully_exhausted_pool_reports_itself_empty(provider):
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    pool = make_pool()

    with pytest.raises(gemini_pool.PoolUnavailable) as caught:
        call(pool)

    # The exception carries the *reason* the last attempt failed, which is what
    # makes a log line useful; the pool's own emptiness is a separate fact and
    # is what the owner's warning is built from.
    assert caught.value.kind == "quota_exhausted"
    assert pool.health()["empty"] is True
    assert pool.health()["usable"] == 0


# ══ SECURITY TESTS ════════════════════════════════════════════════════════
def test_a_key_never_reaches_a_log_line(provider, caplog):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.then(KEY_A, TEXT_MODELS[1], project_quota_exhausted())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    with caplog.at_level(logging.DEBUG):
        call(pool)

    for key in (KEY_A, KEY_B):
        assert key not in caplog.text


def test_a_transient_failure_logs_what_the_provider_said(provider, caplog):
    """``kind`` alone cannot tell a 503 from a deadline; the detail can.

    Both arrive as ``provider_error``/``transient`` with the same scope, and the
    reading is opposite: one is the provider being briefly unwell, the other is
    it accepting the request and running out of its own time. Without the status
    in the line an operator can see only that something failed, which is how a
    provider-wide degradation gets mistaken for a fault in this code.
    """
    provider.answers(KEY_A, "ok")
    provider.then(KEY_A, TEXT_MODELS[0], overloaded())
    pool = make_pool()

    with caplog.at_level(logging.WARNING):
        assert call(pool) == "ok"

    assert "kind=provider_error" in caplog.text
    assert "detail=503" in caplog.text


def test_a_key_never_reaches_a_recorded_event(provider):
    """The events table is read by operators, so it is a place a key must not be.

    This used to assert the same thing about a Telegram message. The message is
    gone; the events are not, and they are now the only artefact a failover
    leaves behind — which makes this check more load-bearing than it was, not
    less.
    """
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    call(pool)

    rows = events()
    assert rows
    blob = "\n".join(
        f"{e['kind']} {e['slot']} {e['model']} {e['reason']} {e['detail']}"
        for e in rows
    )
    assert KEY_A not in blob and KEY_B not in blob
    # The slot is what an operator gets instead, and it is enough to tell one
    # configured key from another without being enough to use one.
    assert any(e["slot"] == "1" for e in rows)


def test_a_key_never_reaches_the_status_report(provider):
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))
    gemini_pool._pools = {"intent": pool}
    try:
        report = gemini_pool.status_report("intent")
    finally:
        gemini_pool._pools = {}

    assert KEY_A not in report and KEY_B not in report
    assert gemini_pool.mask(KEY_A) in report


def test_a_key_never_reaches_describe_or_the_startup_lines(provider):
    pool = make_pool(keys=(("1", KEY_A),))
    gemini_pool._pools = {"intent": pool}
    try:
        described = json.dumps(pool.accounts[0].describe())
        lines = "\n".join(gemini_pool.startup_lines())
    finally:
        gemini_pool._pools = {}

    assert KEY_A not in described
    assert KEY_A not in lines
    assert gemini_pool.mask(KEY_A) in lines


def test_the_client_cache_is_keyed_by_fingerprint_not_by_key(monkeypatch):
    """A stray repr of the cache must not be a credential leak."""
    monkeypatch.setattr(gemini_pool, "_load_sdk", lambda: (object(), object()))
    monkeypatch.setattr(
        gemini_pool, "build_client", lambda key, timeout: (object(), object())
    )
    gemini_pool.reset_clients()

    gemini_pool.client_for(KEY_A, 5.0)

    assert gemini_pool._clients, "the client should have been cached"
    for cache_key in gemini_pool._clients:
        assert KEY_A not in repr(cache_key)
        assert gemini_pool.fingerprint(KEY_A) in repr(cache_key)


def test_the_masked_form_never_reveals_more_than_four_characters():
    masked = gemini_pool.mask(KEY_A)

    assert masked == "****000A"
    assert KEY_A not in masked


# ══ EVENT TESTS ═══════════════════════════════════════════════════════════
# What used to be "does the owner get told" is now "is it written down". The
# observable behaviour of a failover is a row in `gemini_events` and a counter
# on the account, and those are what these assert.
def test_a_model_failover_is_recorded(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    pool = make_pool()

    call(pool)

    rows = events("model_failover")
    assert len(rows) == 1
    assert rows[0]["model"] == TEXT_MODELS[0]
    assert rows[0]["reason"] == "rate_limited"
    assert rows[0]["slot"] == "1"
    # The action taken is kept, because "what did the pool do about it" is the
    # question the row exists to answer.
    assert TEXT_MODELS[1] in rows[0]["detail"]


def test_an_account_failover_is_recorded(provider):
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    call(pool)

    rows = events("account_failover")
    assert len(rows) == 1
    assert rows[0]["slot"] == "1"
    assert rows[0]["reason"] == "quota_exhausted"
    # How much pool is left, which is the fact that makes the row worth having.
    assert "usable=" in rows[0]["detail"]


def test_the_pool_degrading_is_recorded(provider):
    provider.answers(KEY_A, "a")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))
    pool.accounts[1].mark("INVALID", reason="revoked", now=time.time())

    call(pool)

    rows = events("pool_critical")
    assert len(rows) == 1
    assert rows[0]["reason"] == "one usable account"


def test_the_pool_emptying_is_recorded(provider):
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    pool = make_pool()

    with pytest.raises(gemini_pool.PoolUnavailable):
        call(pool)

    assert [e["kind"] for e in events("pool_empty")] == ["pool_empty"]


def test_events_are_deduplicated(provider):
    """A hundred consecutive 429s are one row, not a hundred."""
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    pool = make_pool()

    for _ in range(20):
        call(pool)

    assert len(events("model_failover")) == 1


def test_deduplication_is_per_account_not_global(provider):
    """The second account failing is the one that says the pool is shrinking."""
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
        provider.then(KEY_B, model, project_quota_exhausted())
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    with pytest.raises(gemini_pool.PoolUnavailable):
        call(pool)

    assert len(events("account_failover")) == 2


def test_recording_an_event_never_calls_out_to_anything(provider):
    """The pool's event recorder is synchronous and touches nothing external.

    This is the structural half of "no automatic Telegram notifications". A test
    that only asserted "no message was sent" would pass against an implementation
    that *could* send one; this asserts the shape — ``Pool.record`` is not a
    coroutine, so there is no ``await`` inside it, so there is nothing it can
    call that would reach a network.
    """
    pool = make_pool()

    assert not inspect.iscoroutinefunction(gemini_pool.Pool.record)
    assert pool.record("model_failover", slot="1", reason="test") is True
    assert len(events("model_failover")) == 1


def test_the_pool_has_no_way_to_reach_telegram():
    """There is no notifier to register, and no module-level hook to set.

    The old design gave every pool a callback that reached a chat, and the
    registry kept the last one so a rebuild would not lose it. Both are gone.
    This test is the guard against them coming back by accident: if somebody
    re-adds a delivery path, these attributes reappear and this fails.
    """
    assert not hasattr(gemini_pool, "set_notifier")
    assert not hasattr(gemini_pool, "_notifier")
    assert not hasattr(gemini_pool.Pool, "set_notifier")
    assert not hasattr(gemini_pool.Pool, "notify")


def test_the_pool_module_does_not_import_telegram():
    """The pool cannot message a chat, and does not know what one is."""
    source = pathlib.Path(gemini_pool.__file__).read_text(encoding="utf-8")
    assert "import telegram" not in source
    assert "from telegram" not in source
    assert "send_message" not in source


def test_admin_log_chat_is_never_a_pool_destination(provider, monkeypatch):
    """Configuring an admin log chat changes nothing a failover does.

    The old design read ``ADMIN_LOG_CHAT`` first and the owner's private chat as
    a fallback, so a deployment with a log group received every failover notice
    whether or not anybody wanted them. Both destinations are checked here: the
    pool module must not read either setting, and a complete failover with both
    configured must produce nothing but an event row.
    """
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", -1009999999999)
    monkeypatch.setattr(config, "OWNER_USER_ID", 424242)

    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "b"

    # It did the work...
    assert events("account_failover")
    assert pool.accounts[0].state == "QUOTA_EXHAUSTED"
    assert pool.accounts[1].successes == 1

    # ...and there was nowhere for a message to go, because the pool has no
    # notion of a chat. This is a structural fact, not a coincidence of
    # configuration: the module does not import telegram, and no function in
    # the application hands it anything that could send.
    source = pathlib.Path(gemini_pool.__file__).read_text(encoding="utf-8")
    assert "ADMIN_LOG_CHAT" not in source
    assert "owner_id" not in source
    assert not hasattr(main, "notify_owner")


def test_the_owner_private_chat_is_not_a_pool_fallback(provider, monkeypatch):
    """The owner's private chat is not a notification destination either.

    The brief is explicit that removing the group message must not be answered
    by sending it somewhere quieter. There is no fallback, no digest and no
    "only the important ones" filter — the event is written down and that is
    all that happens.
    """
    monkeypatch.setattr(config, "OWNER_USER_ID", 424242)
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", None)

    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    pool = make_pool()

    call(pool)

    assert events("model_failover")
    assert not hasattr(main, "notify_owner")
    assert not hasattr(gemini_pool, "_notifier")
    # The owner id is not even a name the pool module knows.
    source = pathlib.Path(gemini_pool.__file__).read_text(encoding="utf-8")
    assert "OWNER_USER_ID" not in source


def test_the_application_registers_no_pool_notifier():
    """``main`` has no owner-notification helper and no bot handle to give it.

    Checked against the imported module rather than its text, so a comment
    mentioning the old design does not fail the test and a live attribute
    cannot hide behind one. If a delivery path were reintroduced in
    ``post_init`` it would need a function to call and a bot to call it with,
    and both would have to exist here.
    """
    assert not hasattr(main, "notify_owner")
    assert not hasattr(main, "_pool_bot")


def test_the_status_report_names_the_pool_and_never_a_credential(provider):
    provider.answers(KEY_A, "a")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))
    gemini_pool._pools = {"intent": pool}
    try:
        call(pool)
        report = gemini_pool.status_report()
    finally:
        gemini_pool._pools = {}

    assert "GEMINI API POOL" in report
    assert "intent" in report
    assert "Successful: 1" in report
    assert "Remaining: Not exposed by provider" in report
    assert "Reset: Not exposed by provider" in report


def test_the_status_report_counts_every_account_state(provider):
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B), ("3", KEY_C)))
    now = time.time()
    pool.accounts[0].mark("QUOTA_EXHAUSTED", reason="quota", now=now)
    pool.accounts[1].mark("INVALID", reason="revoked", now=now)

    health = pool.health()

    assert health["accounts"] == 3
    assert health["exhausted"] == 1
    assert health["invalid"] == 1
    assert health["active"] == 1
    assert health["critical"] is True
    assert health["degraded"] is True


def test_a_pool_of_one_is_critical_but_not_degraded(provider):
    """A one-account pool is always at one usable account.

    Warning about it would be a message that never means anything, which is how
    an operator learns to ignore the message that does. "Degraded" is the
    version that is news: the pool shrank from more than one to exactly one.
    """
    pool = make_pool(keys=(("1", KEY_A),))

    health = pool.health()

    assert health["accounts"] == 1
    assert health["critical"] is True
    assert health["degraded"] is False


def test_a_pool_of_one_records_no_degradation(provider):
    """A one-account pool is not degraded, so nothing is written down.

    The point is unchanged from when this asserted silence: a pool of one is
    *always* at one usable account, and recording that on every request would
    fill the events table with a fact that was true when it was configured. The
    row has to mean something for the table to be worth reading.
    """
    provider.answers(KEY_A, "a")
    pool = make_pool(keys=(("1", KEY_A),))

    call(pool)

    assert not events("pool_critical")
    assert not events("pool_empty")


# ══ ISOLATION TESTS ═══════════════════════════════════════════════════════
def test_two_workloads_keep_separate_rows_for_one_credential(provider):
    """The same key serving two workloads is two independent budgets.

    This is a property of the schema, not a promise about how the code calls
    things: a key exhausted for moderation must not silence the classifier.
    """
    provider.answers(KEY_A, "a")
    intent = make_pool(workload="intent")
    moderation = make_pool(
        workload="moderation",
        models=MEDIA_MODELS,
        capabilities=frozenset({gemini_pool.TEXT, gemini_pool.IMAGE, gemini_pool.VIDEO}),
    )

    call(intent)
    call(moderation)

    rows = {r["workload"]: r for r in db.pool_accounts()}
    assert set(rows) == {"intent", "moderation"}
    assert rows["intent"]["requests"] == 1
    assert rows["moderation"]["requests"] == 1


def test_one_workload_exhausting_does_not_disable_another(provider):
    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    provider.answers(KEY_B, "b")
    intent = make_pool(workload="intent", keys=(("1", KEY_A),))
    chat = make_pool(workload="chat", keys=(("1", KEY_B),))

    with pytest.raises(gemini_pool.PoolUnavailable):
        call(intent)

    assert call(chat) == "b"
    assert chat.accounts[0].state == "ACTIVE"


def test_each_workload_has_its_own_pool_in_the_registry(monkeypatch):
    monkeypatch.setattr(
        config,
        "GEMINI_POOLS",
        [
            {"workload": "intent", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
            {"workload": "chat", "keys": [("1", KEY_B)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
        ],
    )
    gemini_pool.build_pools()

    assert gemini_pool.pool_for("intent") is not gemini_pool.pool_for("chat")
    assert gemini_pool.pool_for("intent").accounts[0].fingerprint != (
        gemini_pool.pool_for("chat").accounts[0].fingerprint
    )


def test_the_tts_workload_shares_the_chat_credential_by_design(monkeypatch):
    """Speech synthesis is a mode of the conversation, not a peer workload."""
    monkeypatch.setattr(
        config,
        "GEMINI_POOLS",
        [
            {"workload": "chat", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
            {"workload": "tts", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"audio_out"}), "allow_experimental": True,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
        ],
    )
    gemini_pool.build_pools()

    # Reported only when two *independent* workloads collide. chat/tts is a
    # pairing the operator configured on purpose, and crying wolf about it
    # would train them to ignore the warning that matters.
    assert gemini_pool.shared_credentials() == []


def test_shared_credentials_across_independent_workloads_are_reported(monkeypatch):
    monkeypatch.setattr(
        config,
        "GEMINI_POOLS",
        [
            {"workload": "intent", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
            {"workload": "moderation", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
        ],
    )
    gemini_pool.build_pools()

    shared = gemini_pool.shared_credentials()

    assert shared == [(gemini_pool.mask(KEY_A), ["intent", "moderation"])]


def test_awareness_sharing_the_chat_key_is_reported(monkeypatch):
    """Awareness is not a mode of the conversation, and must not be treated as one.

    ``tts`` is: it runs inside a turn that already happened, so it cannot take an
    allowance from a request nobody has made yet. Awareness runs on its own timer
    in its own rooms, whether or not anybody is talking to the assistant, so the
    pairing is a genuine collision — and on the deployment that produced this
    test it cost 26% of conversational turns to rate limits before anybody
    noticed. The boot log is where the operator can find out.
    """
    monkeypatch.setattr(
        config,
        "GEMINI_POOLS",
        [
            {"workload": "chat", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
            {"workload": "awareness", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
        ],
    )
    gemini_pool.build_pools()

    assert gemini_pool.shared_credentials() == [
        (gemini_pool.mask(KEY_A), ["awareness", "chat"])
    ]


def test_awareness_with_its_own_key_is_not_reported(monkeypatch):
    """The report is actionable: an operator who separated them stops seeing it."""
    monkeypatch.setattr(
        config,
        "GEMINI_POOLS",
        [
            {"workload": "chat", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
            {"workload": "awareness", "keys": [("1", KEY_B)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
        ],
    )
    gemini_pool.build_pools()

    assert gemini_pool.shared_credentials() == []

# ══ PERSISTENCE TESTS ═════════════════════════════════════════════════════
def test_model_counters_survive_a_restart(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    call(make_pool())

    reborn = make_pool()
    account = reborn.accounts[0]

    assert account.requests == 2
    assert account.successes == 1
    assert account.rate_limits == 1
    assert account.model(TEXT_MODELS[1]).successes == 1
    assert account.model(TEXT_MODELS[0]).rate_limits == 1


def test_a_cooldown_survives_a_restart(provider):
    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "ok")
    call(make_pool())

    reborn = make_pool()
    benched = reborn.accounts[0].model(TEXT_MODELS[0])

    assert benched.state == "RATE_LIMITED"
    assert benched.usable(time.time()) is False
    offered = reborn.models_for(reborn.accounts[0], time.time())
    assert TEXT_MODELS[0] not in offered
    assert offered == TEXT_MODELS[1:]


def test_discovery_is_cached_per_credential_not_per_slot(provider):
    provider.models(KEY_A, TEXT_MODELS)
    provider.answers(KEY_A, "a")
    config.GEMINI_POOL_DISCOVERY_ENABLED = True
    try:
        pool = make_pool(keys=(("1", KEY_A),))
        call(pool)
        call(pool)
    finally:
        config.GEMINI_POOL_DISCOVERY_ENABLED = False

    # Asked once, then answered from the database for the TTL.
    assert provider.listed_calls == 1


def test_concurrent_requests_do_not_corrupt_the_counters(provider):
    provider.answers(KEY_A, "a").answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    async def burst():
        return await asyncio.gather(
            *[
                gemini_pool.generate(
                    pool,
                    build_contents=lambda types: "x",
                    build_config=lambda types: {},
                )
                for _ in range(12)
            ]
        )

    results = asyncio.run(burst())

    assert len(results) == 12
    total = sum(a.requests for a in pool.accounts)
    assert total == 12
    assert sum(a.successes for a in pool.accounts) == 12
    # And the persisted rows agree with the in-memory mirrors.
    assert db.pool_counts("intent")["requests"] == 12


def test_the_counters_are_the_database_and_not_a_process_local_number(provider):
    provider.answers(KEY_A, "a")
    pool = make_pool()
    call(pool)

    counts = db.pool_counts("intent")

    assert counts["accounts"] == 1
    assert counts["requests"] == 1
    assert counts["successes"] == 1


# ══ WIRING TESTS ══════════════════════════════════════════════════════════
# The pool is only worth anything if the four workloads actually call it. These
# go through each module's real request seam, with the real payload builders, so
# that "the caller does not know about failover" is a property of the code
# rather than of a mock.
def install(monkeypatch, workload, keys, models, capabilities):
    pool = make_pool(
        workload=workload, keys=keys, models=models, capabilities=capabilities
    )
    monkeypatch.setitem(gemini_pool._pools, workload, pool)
    return pool


def test_the_classifier_calls_through_the_pool(monkeypatch, provider):
    from app import ai_intent

    provider.always(KEY_A, TEXT_MODELS[0], "pooled")
    install(monkeypatch, "intent", (("1", KEY_A),), TEXT_MODELS,
            frozenset({gemini_pool.TEXT}))

    assert asyncio.run(ai_intent._request("سلام")) == "pooled"
    assert provider.calls == [(KEY_A, TEXT_MODELS[0])]


def test_the_classifier_fails_over_without_the_caller_knowing(monkeypatch, provider):
    from app import ai_intent

    provider.then(KEY_A, TEXT_MODELS[0], rate_limited(TEXT_MODELS[0]))
    provider.always(KEY_A, TEXT_MODELS[1], "second model")
    install(monkeypatch, "intent", (("1", KEY_A),), TEXT_MODELS,
            frozenset({gemini_pool.TEXT}))

    # One call in, one answer out. The handler that asked never learns that two
    # provider calls happened.
    assert asyncio.run(ai_intent._request("سلام")) == "second model"


def test_the_moderation_workload_never_reaches_a_text_only_model(monkeypatch, provider):
    from app import ai_moderation

    provider.always(KEY_A, MEDIA_MODELS[1], "verdict")
    install(monkeypatch, "moderation", (("1", KEY_A),),
            ["gemma-3-27b-it", *MEDIA_MODELS],
            frozenset({gemini_pool.TEXT, gemini_pool.IMAGE, gemini_pool.VIDEO}))

    asyncio.run(
        ai_moderation._request([{"mime_type": "image/png", "data": b"bytes"}, "what is this"])
    )

    assert "gemma-3-27b-it" not in provider.models_used(KEY_A)


def test_the_moderation_workload_fails_over_across_accounts(monkeypatch, provider):
    from app import ai_moderation

    for model in MEDIA_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    provider.answers(KEY_B, "verdict from B")
    install(monkeypatch, "moderation", (("1", KEY_A), ("2", KEY_B)), MEDIA_MODELS,
            frozenset({gemini_pool.TEXT, gemini_pool.IMAGE, gemini_pool.VIDEO}))

    result = asyncio.run(
        ai_moderation._request([{"mime_type": "image/png", "data": b"bytes"}, "what is this"])
    )

    assert result == "verdict from B"


def test_the_conversation_calls_through_the_pool(monkeypatch, provider):
    from app import chat

    provider.always(KEY_A, TEXT_MODELS[0], "سلام!")
    install(monkeypatch, "chat", (("1", KEY_A),), TEXT_MODELS,
            frozenset({gemini_pool.TEXT}))

    result = asyncio.run(
        chat._request([{"role": "user", "parts": [{"text": "سلام"}]}])
    )

    assert result == "سلام!"


def test_speech_synthesis_uses_its_own_pool(monkeypatch, provider):
    from app import chat

    provider.always(KEY_A, "gemini-3.1-flash-tts-preview", "unused")
    pool = make_pool(
        workload="tts",
        keys=(("1", KEY_A),),
        models=["gemini-3.1-flash-tts-preview"],
        capabilities=frozenset({gemini_pool.AUDIO_OUT}),
        allow_experimental=True,
    )
    monkeypatch.setitem(gemini_pool._pools, "tts", pool)

    # The response shape is audio, not text, so the pool's extract hook is what
    # decides the return type. An empty PCM is the honest answer here.
    assert asyncio.run(chat._tts_request("سلام")) == b""


def test_transcription_calls_through_the_pool(monkeypatch, provider):
    from app import transcribe

    provider.always(KEY_A, AUDIO_MODELS[0], "متن")
    install(monkeypatch, "transcribe", (("1", KEY_A),), AUDIO_MODELS,
            frozenset({gemini_pool.AUDIO_IN}))

    assert asyncio.run(transcribe._request(b"audio", "audio/ogg")) == "متن"


def test_transcription_never_reaches_a_text_only_model(monkeypatch, provider):
    from app import transcribe

    provider.always(KEY_A, AUDIO_MODELS[0], "متن")
    install(monkeypatch, "transcribe", (("1", KEY_A),),
            ["gemma-3-27b-it", *AUDIO_MODELS], frozenset({gemini_pool.AUDIO_IN}))

    asyncio.run(transcribe._request(b"audio", "audio/ogg"))

    assert "gemma-3-27b-it" not in provider.models_used(KEY_A)


def test_a_pool_key_alone_makes_a_workload_report_itself_enabled(monkeypatch, provider):
    """An operator who moves a workload onto a pool key must not find it inert."""
    from app import ai_intent, ai_moderation, chat, transcribe

    for workload in ("intent", "chat", "moderation", "transcribe"):
        install(monkeypatch, workload, (("1", KEY_A),), TEXT_MODELS,
                frozenset({gemini_pool.TEXT}))

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "")
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "")
    # The switches stay on; only the *credentials* move to the pool. That is the
    # configuration an operator reaches by adding pool keys and clearing the old
    # single-key variables, and it must not silently disable a workload.
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", True)
    monkeypatch.setattr(config, "TRANSCRIBE_ENABLED", True)

    assert ai_intent.is_enabled() is True
    assert chat.is_enabled() is True
    assert ai_moderation.is_enabled() is True
    assert transcribe.is_enabled() is True


def test_a_workload_with_no_credential_anywhere_stays_inert(monkeypatch):
    from app import ai_intent

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    assert ai_intent.is_enabled() is False


def test_a_pool_unavailable_becomes_the_modules_own_failure_type(monkeypatch, provider):
    """Callers must see their existing vocabulary, not the pool's."""
    from app import ai_intent

    for model in TEXT_MODELS:
        provider.then(KEY_A, model, project_quota_exhausted())
    install(monkeypatch, "intent", (("1", KEY_A),), TEXT_MODELS,
            frozenset({gemini_pool.TEXT}))

    with pytest.raises(ai_intent.AiUnavailable):
        asyncio.run(ai_intent._request("سلام"))


# ══ THE SHAPES THE LIVE API ACTUALLY SENDS ════════════════════════════════
# Every string below was captured from the live API on 2026-09-21. They are
# here because the first version of this module classified against the shapes
# the *documentation* implies, and two of them were wrong in ways a mock could
# never have shown:
#
#   * an invalid key answers 400 INVALID_ARGUMENT, not 401 UNAUTHENTICATED —
#     read as a bad request it abandoned the request instead of failing over
#   * the free-tier 429 carries its reset as "Please retry in 26.5s", not in a
#     `retryDelay` field, so a reset the provider *did* give was reported as
#     "not exposed by provider"
#
# They are also quoted the way the SDK renders them: a Python dict repr, single
# quotes, which a double-quote-only regex silently fails to match.
LIVE_404_UNKNOWN = (
    "ClientError 404 NOT_FOUND. {'error': {'code': 404, 'message': "
    "'models/gemini-model-that-does-not-exist-xyz is not found for API version "
    "v1beta, or is not supported for generateContent. Call ModelService.ListModels "
    "to see the list of available models and their supported methods.', "
    "'status': 'NOT_FOUND'}}"
)
LIVE_404_RETIRED = (
    "ClientError 404 NOT_FOUND. {'error': {'code': 404, 'message': "
    "'This model models/gemini-2.5-flash is no longer available to new users. "
    "Please update your code to use models/gemini-3.6-flash for the latest "
    "features and improvements.', 'status': 'NOT_FOUND'}}"
)
LIVE_429_FREE_TIER = (
    "ClientError 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
    "'You exceeded your current quota, please check your plan and billing "
    "details.\\n* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\\n"
    "Please retry in 26.510175789s.', 'status': 'RESOURCE_EXHAUSTED'}}"
)
LIVE_400_INVALID_KEY = (
    "ClientError 400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
    "'API key not valid. Please pass a valid API key.', 'status': "
    "'INVALID_ARGUMENT', 'details': [{'@type': "
    "'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'API_KEY_INVALID', "
    "'domain': 'googleapis.com'}]}}"
)


class LiveError(Exception):
    """An exception whose text is exactly what the SDK produced."""

    def __init__(self, text):
        self.code = int(text.split()[1])
        super().__init__(text.split(" ", 1)[1] if " " in text else text)


def classify(text):
    return gemini_pool.classify_error(LiveError(text))


def test_an_unknown_model_is_a_model_problem():
    failure = classify(LIVE_404_UNKNOWN)

    assert failure.kind == "unsupported_model"
    assert failure.scope == gemini_pool.SCOPE_MODEL


def test_a_model_retired_for_new_users_is_also_a_model_problem():
    """The live API words this differently from an unknown name."""
    failure = classify(LIVE_404_RETIRED)

    assert failure.kind == "unsupported_model"
    assert failure.scope == gemini_pool.SCOPE_MODEL


def test_the_free_tier_429_is_a_model_limit_with_a_real_reset():
    """The metric names the model, and the reset is in prose, not in a field."""
    failure = classify(LIVE_429_FREE_TIER)

    assert failure.kind == "rate_limited"
    assert failure.scope == gemini_pool.SCOPE_MODEL
    assert failure.retryable is True
    assert failure.reset_at is not None
    assert 20 <= failure.reset_at - time.time() <= 30


def test_an_invalid_key_answers_400_and_must_still_abandon_the_account():
    """The one that mattered: a revoked key is not a malformed request."""
    failure = classify(LIVE_400_INVALID_KEY)

    assert failure.kind == "invalid_credential"
    assert failure.scope == gemini_pool.SCOPE_ACCOUNT


def test_an_invalid_key_fails_over_to_the_next_account(provider):
    """End to end, with the real error text rather than a synthetic 401."""
    provider.then(KEY_A, TEXT_MODELS[0], LiveError(LIVE_400_INVALID_KEY))
    provider.answers(KEY_B, "b")
    pool = make_pool(keys=(("1", KEY_A), ("2", KEY_B)))

    assert call(pool) == "b"
    assert pool.accounts[0].state == "INVALID"


def test_a_free_tier_429_benches_the_model_for_the_providers_own_reset(provider):
    provider.then(KEY_A, TEXT_MODELS[0], LiveError(LIVE_429_FREE_TIER))
    provider.always(KEY_A, TEXT_MODELS[1], "sibling")
    pool = make_pool()

    assert call(pool) == "sibling"

    benched = pool.accounts[0].model(TEXT_MODELS[0])
    # The provider said 26 seconds; the default model cooldown is 120. Believing
    # the provider is the difference between a model back in half a minute and
    # one wrongly written off for two.
    assert benched.state == "RATE_LIMITED"
    assert benched.cooldown_until - int(time.time()) <= 30
    # And the account is untouched: this was a model limit.
    assert pool.accounts[0].state == "ACTIVE"


def test_the_classifier_reads_double_quoted_json_too():
    """A body that is still JSON must classify the same way as the repr."""
    text = (
        'ClientError 400 INVALID_ARGUMENT. {"error": {"code": 400, "message": '
        '"API key not valid. Please pass a valid API key.", "status": '
        '"INVALID_ARGUMENT"}}'
    )

    failure = classify(text)

    assert failure.kind == "invalid_credential"
    assert failure.scope == gemini_pool.SCOPE_ACCOUNT


def test_a_plain_bad_request_is_still_the_requests_fault():
    """Widening the auth branch must not swallow a genuinely malformed payload."""
    failure = classify(
        "ClientError 400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
        "'Invalid JSON payload received. Unknown name \\\"foo\\\".', 'status': "
        "'INVALID_ARGUMENT'}}"
    )

    assert failure.kind == "bad_request"
    assert failure.scope == gemini_pool.SCOPE_REQUEST


def test_a_capability_mismatch_is_a_model_problem_not_a_bad_request():
    failure = classify(
        "ClientError 400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
        "'Unable to process input image. Please retry or report in "
        "https://developers.generativeai.google/guide/troubleshooting', "
        "'status': 'INVALID_ARGUMENT'}}"
    )

    assert failure.kind == "unsupported_input"
    assert failure.scope == gemini_pool.SCOPE_MODEL


# ══ Model rotation ════════════════════════════════════════════════════════
# The pool has always spread *accounts* least-recently-succeeded-first, so a
# pool of five does not leave four unused. The model list was left in strict
# preference order, so the leading model absorbed every request until the
# provider rate-limited it. Under a per-model daily allowance that means the
# leading model is spent first while the rest sit idle, and the workload's
# capacity is one model's rather than the list's.
def test_models_are_tried_in_preference_order_when_rotation_is_off():
    """The default, unchanged: a workload that has not opted in is unaffected."""
    pool = make_pool(models=TEXT_MODELS)
    account = pool.accounts[0]
    assert pool.models_for(account, time.time()) == list(TEXT_MODELS)


def test_rotation_never_reuses_the_model_that_just_served():
    """The point of the feature: load moves on instead of staying on one name."""
    pool = make_pool(models=TEXT_MODELS, rotate_models=True)
    account = pool.accounts[0]
    now = time.time()

    # Nothing has been used, so the preference list still decides.
    assert pool.models_for(account, now)[0] == TEXT_MODELS[0]

    account.model(TEXT_MODELS[0]).note_request(now)
    assert pool.models_for(account, now)[0] == TEXT_MODELS[1]

    account.model(TEXT_MODELS[1]).note_request(now + 1)
    assert pool.models_for(account, now)[0] == TEXT_MODELS[2]


def test_rotation_cycles_back_to_the_least_recently_used():
    """After a full pass the order is the original preference order again."""
    pool = make_pool(models=TEXT_MODELS, rotate_models=True)
    account = pool.accounts[0]
    now = time.time()
    for index, name in enumerate(TEXT_MODELS):
        account.model(name).note_request(now + index)

    assert pool.models_for(account, now) == list(TEXT_MODELS)


def test_rotation_still_respects_a_benched_model():
    """Rotating must not resurrect a model the pool has just taken out."""
    pool = make_pool(models=TEXT_MODELS, rotate_models=True)
    account = pool.accounts[0]
    now = time.time()
    account.model(TEXT_MODELS[0]).note_failure(
        gemini_pool.Failure("rate_limited", gemini_pool.SCOPE_MODEL, cooldown=600),
        now,
    )

    order = pool.models_for(account, now)
    assert TEXT_MODELS[0] not in order
    assert order[0] == TEXT_MODELS[1]


def test_only_the_configured_workloads_rotate(monkeypatch):
    """Rotation is opt-in per workload, not a change to every pool at once."""
    monkeypatch.setattr(
        config,
        "GEMINI_POOLS",
        [
            {"workload": "awareness", "keys": [("1", KEY_A)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
            {"workload": "chat", "keys": [("1", KEY_B)], "models": TEXT_MODELS,
             "capabilities": frozenset({"text"}), "allow_experimental": False,
             "retries": 0, "backoff": 0.0, "timeout": 10.0},
        ],
    )
    monkeypatch.setattr(config, "GEMINI_POOL_ROTATE_MODELS", frozenset({"awareness"}))
    gemini_pool.build_pools()

    assert gemini_pool.pool_for("awareness").rotate_models is True
    assert gemini_pool.pool_for("chat").rotate_models is False


def test_the_shipped_default_rotates_the_two_high_volume_workloads():
    """The default the deployment actually gets, asserted so it cannot drift.

    Both were measured hitting a ceiling: awareness is the highest-volume
    workload and the one whose leading model was rate-limited most, and chat
    spent 500/500 and 481/500 of its two accounts on 2026-09-21. The workloads
    that are not listed — intent, moderation, transcribe, tts — keep the strict
    preference order, which is the behaviour an operator already knows.
    """
    assert config.GEMINI_POOL_ROTATE_MODELS == frozenset({"awareness", "chat"})


def test_a_model_that_has_never_answered_is_tried_last():
    """Spreading the load must not mean volunteering for a broken model.

    Measured on the deployment, and the reason this rule exists: a fresh
    least-recently-used rotation over the whole list put every model at the
    front in turn — including the ones this credential cannot actually use — and
    one awareness pass took 104 s, against 21 s before rotation existed. The
    model is not disabled and not forgotten; it is simply reached after the
    models that have demonstrated they answer.
    """
    pool = make_pool(models=TEXT_MODELS, rotate_models=True)
    account = pool.accounts[0]
    now = time.time()

    account.model(TEXT_MODELS[0]).note_request(now)        # asked, never answered
    account.model(TEXT_MODELS[1]).note_request(now)
    account.model(TEXT_MODELS[1]).note_success(now)        # asked, answered

    order = pool.models_for(account, now)
    assert order[-1] == TEXT_MODELS[0]
    assert order.index(TEXT_MODELS[1]) < order.index(TEXT_MODELS[0])


def test_a_never_asked_model_still_counts_as_proven():
    """The first pass through a new list learns in the configured order.

    Otherwise a fresh deployment would skip past every name it had not used yet
    and rotate only among whichever one it happened to try first.
    """
    pool = make_pool(models=TEXT_MODELS, rotate_models=True)
    account = pool.accounts[0]
    assert pool.models_for(account, time.time()) == list(TEXT_MODELS)
