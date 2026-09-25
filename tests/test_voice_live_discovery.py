"""Finding the active call, and reporting the four ways there isn't one.

The defect this file guards against was not "the join is broken". A real join
against a group with an active call works, and did before this change. The defect
was that *finding* the call was left entirely to ``py-tgcalls``, whose cache wraps
its fallback lookup in ``except Exception: pass`` and so reports every discovery
failure — not a member, forbidden, flood wait, an unresolvable id — as one
``NoActiveGroupCall``. Four problems, one sentence, and the sentence is wrong for
three of them.

So there are two layers of tests here, and they are deliberately separate:

* **The resolver** (``call_discovery``), which asks Telegram itself and returns a
  *kind*. It is exercised against a fake client that returns real Telethon types,
  because the branch that decides "channel or basic group" is a branch on those
  types and a mock of them would not test it.
* **The adapter** (``PytgcallsTransport``), which turns a kind into a machine
  reason, seeds the library's cache with the call it found, and must start the
  library before it plays. It is exercised with the resolver stubbed, so these
  tests run without a Telethon install at all.

The last section asserts, against the source, that the transport and the
resolver never reach into the conversational or acquisition workloads. A live
call is the assistant; it is not a second path into anything else.
"""
from __future__ import annotations

import ast
import asyncio
import datetime
import os
from types import SimpleNamespace

import pytest

from app.voice_live import call_discovery as CD
from app.voice_live import errors
from app.voice_live import telegram_voice as TV

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══ The resolver, against real Telethon types ═════════════════════════════
#
# The resolver's branches are on Telethon *types* (channel vs basic group, an
# ``InputGroupCall`` vs a slug), so those tests need the library and are skipped
# without it. The adapter tests below stub the resolver and need none of it,
# which is why this file does not skip as a whole.
try:
    import telethon  # noqa: F401

    from telethon.errors import ChannelPrivateError, FloodWaitError
    from telethon.tl.types import (
        InputGroupCall,
        InputGroupCallSlug,
        InputPeerChannel,
        InputPeerChat,
    )

    HAVE_TELETHON = True
except Exception:  # noqa: BLE001 - absence is a supported environment
    HAVE_TELETHON = False

needs_telethon = pytest.mark.skipif(
    not HAVE_TELETHON, reason="the resolver reads Telethon types"
)


class _FakeDiscoverClient:
    """A client whose two calls the resolver makes are answered by hand.

    Keyed by request class name rather than by call order, so a test that
    accidentally makes the resolver skip a request fails loudly instead of
    returning the wrong canned answer.
    """

    def __init__(
        self,
        peer,
        *,
        full=None,
        full_error=None,
        group_call=None,
        group_call_error=None,
    ):
        self._peer = peer
        self._full = full
        self._full_error = full_error
        self._group_call = group_call
        self._group_call_error = group_call_error
        self.requests: list[str] = []

    async def get_input_entity(self, chat_id):
        if isinstance(self._peer, BaseException):
            raise self._peer
        return self._peer

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        if name in ("GetFullChannelRequest", "GetFullChatRequest"):
            if self._full_error is not None:
                raise self._full_error
            return self._full
        if name == "GetGroupCallRequest":
            if self._group_call_error is not None:
                raise self._group_call_error
            return self._group_call
        raise AssertionError(f"the resolver made an unexpected request: {name}")


def _channel_peer():
    return InputPeerChannel(channel_id=1112223334, access_hash=987654321012345678)


def _full(call):
    return SimpleNamespace(full_chat=SimpleNamespace(call=call))


def _group_call(schedule_date=None):
    return SimpleNamespace(call=SimpleNamespace(schedule_date=schedule_date))


def _discover(client, chat_id=-1001112223334):
    return asyncio.run(CD.discover(client, chat_id))


@needs_telethon
def test_a_live_call_is_found_and_returned():
    """The happy path: the call is on ``ChannelFull`` and comes back as-is, so
    the caller can hand it to the library rather than let it look again."""
    call = InputGroupCall(id=123456, access_hash=789)
    client = _FakeDiscoverClient(
        _channel_peer(), full=_full(call), group_call=_group_call()
    )
    found = _discover(client)
    assert found.kind == CD.ACTIVE
    assert found.active is True
    assert found.input_call is call
    assert client.requests == ["GetFullChannelRequest", "GetGroupCallRequest"]


@needs_telethon
def test_no_call_is_none_and_is_not_probed_further():
    """``call=None`` is Telegram's answer, not a failure. There is nothing to ask
    a follow-up question about, so the resolver must not ask one."""
    client = _FakeDiscoverClient(_channel_peer(), full=_full(None))
    found = _discover(client)
    assert found.kind == CD.NONE
    assert found.input_call is None
    assert found.active is False
    assert client.requests == ["GetFullChannelRequest"]


@needs_telethon
def test_a_scheduled_call_is_distinguished_from_a_live_one():
    """PyTgCalls refuses a call whose ``schedule_date`` is set, and it is right
    to — but "scheduled" is not "there is no call", and an operator told the
    wrong one looks in the wrong place."""
    call = InputGroupCall(id=1, access_hash=2)
    client = _FakeDiscoverClient(
        _channel_peer(),
        full=_full(call),
        group_call=_group_call(schedule_date=datetime.datetime(2026, 10, 1, 12, 0)),
    )
    found = _discover(client)
    assert found.kind == CD.SCHEDULED
    assert found.active is False


@needs_telethon
def test_failing_to_read_the_schedule_does_not_hide_the_call():
    """We already know a call exists. A failed schedule probe is a missing
    detail, not a reason to pretend the call is absent — the join itself is the
    better judge of whether it can be entered."""
    call = InputGroupCall(id=1, access_hash=2)
    client = _FakeDiscoverClient(
        _channel_peer(),
        full=_full(call),
        group_call_error=RuntimeError("nope"),
    )
    found = _discover(client)
    assert found.kind == CD.ACTIVE
    assert found.input_call is call
    assert found.detail == "schedule_unknown"


@needs_telethon
def test_a_private_channel_is_reported_as_not_visible():
    """The error the library swallows. It must arrive as a visibility problem,
    because the fix is membership and not "start a call"."""
    client = _FakeDiscoverClient(
        _channel_peer(), full_error=ChannelPrivateError(request=None)
    )
    found = _discover(client)
    assert found.kind == CD.NO_ACCESS
    assert found.detail == "ChannelPrivateError"


@needs_telethon
def test_an_unresolvable_chat_is_not_visible_and_not_a_transient_failure():
    """Telethon raises ``ValueError`` for an id it cannot resolve. Grouping it
    with the visibility errors is the point: a retry will not make a chat the
    account cannot see appear."""
    client = _FakeDiscoverClient(ValueError("cannot find any entity"))
    found = _discover(client)
    assert found.kind == CD.NO_ACCESS
    assert found.detail == "ValueError"


@needs_telethon
def test_a_flood_wait_is_a_discovery_failure_not_a_visibility_one():
    """A flood wait is transient and environmental. Reporting it as "you are not
    a member" would send the operator to fix permissions that are fine."""
    client = _FakeDiscoverClient(
        _channel_peer(), full_error=FloodWaitError(request=None)
    )
    found = _discover(client)
    assert found.kind == CD.ERROR
    assert found.detail == "FloodWaitError"


@needs_telethon
def test_a_basic_group_is_discovered_through_the_chat_api():
    """A migrated or plain basic group answers on ``GetFullChat``, not
    ``GetFullChannel``. The branch is on the peer type, so it is asserted with
    both types rather than assumed."""
    call = InputGroupCall(id=9, access_hash=8)
    client = _FakeDiscoverClient(
        InputPeerChat(chat_id=444555666),
        full=_full(call),
        group_call=_group_call(),
    )
    found = _discover(client, -444555666)
    assert found.kind == CD.ACTIVE
    assert client.requests[0] == "GetFullChatRequest"


@needs_telethon
def test_a_peer_that_is_neither_a_channel_nor_a_chat_is_unsupported():
    """A user id, for instance. Not a fault in Telegram and not a call: say it is
    unsupported rather than "no active call"."""
    client = _FakeDiscoverClient(SimpleNamespace())
    found = _discover(client, 12345)
    assert found.kind == CD.UNSUPPORTED
    assert found.input_call is None


@needs_telethon
def test_a_call_object_we_do_not_understand_is_not_reported_as_active():
    """A future layer could return a call type this build does not know. Handing
    it to the library would fail there; saying "none" with a detail is honest."""
    client = _FakeDiscoverClient(_channel_peer(), full=_full(object()))
    found = _discover(client)
    assert found.kind == CD.NONE
    assert found.detail == "object"


def test_a_missing_library_is_a_discovery_failure_not_a_crash(monkeypatch):
    """The resolver is the last thing before a join, and it runs in a process
    that may not have Telethon. That must be a reportable kind, not a traceback."""
    monkeypatch.setattr(CD, "_telethon", lambda: (_ for _ in ()).throw(ImportError("no")))
    found = _discover(_FakeDiscoverClient(None))
    assert found.kind == CD.ERROR
    assert found.detail == "telethon:ImportError"


def test_a_non_numeric_chat_id_is_unsupported():
    found = _discover(_FakeDiscoverClient(None), "not-a-chat")
    assert found.kind == CD.UNSUPPORTED


@needs_telethon
def test_the_slug_form_of_a_call_is_accepted():
    """Telegram can return ``InputGroupCallSlug`` instead of ``InputGroupCall``.
    Both are joinable, so both are active."""
    call = InputGroupCallSlug(slug="abc123")
    client = _FakeDiscoverClient(
        _channel_peer(), full=_full(call), group_call=_group_call()
    )
    found = _discover(client)
    assert found.kind == CD.ACTIVE
    assert found.input_call is call


# ══ Outcome → reason ══════════════════════════════════════════════════════
def test_the_four_refusal_outcomes_have_four_distinct_reasons():
    """Distinct reasons are the whole point: a log line that says which of the
    four it was is the difference between a fix and a guess. ``unsupported``
    shares ``discovery_failed`` with ``error`` on purpose — a peer this build
    cannot read is not a *fifth* problem, it is a lookup that did not produce a
    call — so it is asserted as the same reason rather than a unique one."""
    reasons = {
        kind: CD.reason_for(kind)
        for kind in (CD.NONE, CD.SCHEDULED, CD.NO_ACCESS, CD.ERROR)
    }
    assert len(set(reasons.values())) == len(reasons), reasons
    assert CD.reason_for(CD.NONE) == errors.REASON_NO_ACTIVE_CALL
    assert CD.reason_for(CD.SCHEDULED) == errors.REASON_SCHEDULED_CALL
    assert CD.reason_for(CD.NO_ACCESS) == errors.REASON_CALL_NOT_VISIBLE
    assert CD.reason_for(CD.ERROR) == errors.REASON_DISCOVERY_FAILED
    assert CD.reason_for(CD.UNSUPPORTED) == errors.REASON_DISCOVERY_FAILED
    for reason in reasons.values():
        assert reason and reason == reason.lower() and " " not in reason


def test_an_active_call_has_no_refusal_reason():
    assert CD.reason_for(CD.ACTIVE) == ""


def test_the_discovery_reasons_are_not_retryable():
    """A join refusal must not be auto-retried: two of the four are permanent
    (no call, not a member) and looping on them holds the join path open."""
    for kind in (CD.NONE, CD.SCHEDULED, CD.NO_ACCESS, CD.UNSUPPORTED, CD.ERROR):
        assert errors.is_retryable(CD.reason_for(kind)) is False


# ══ The adapter, with the resolver stubbed ════════════════════════════════
class _FakeCache:
    def __init__(self, *, explode=False):
        self.store: dict[int, object] = {}
        self.dropped: list[int] = []
        self.explode = explode

    def set_cache(self, chat_id, call):
        if self.explode:
            raise RuntimeError("the private API moved")
        self.store[int(chat_id)] = call

    def drop_cache(self, chat_id):
        self.dropped.append(int(chat_id))
        self.store.pop(int(chat_id), None)


class _FakeCall:
    def __init__(self, *, cache=None):
        self.started = 0
        self.plays: list[tuple] = []
        self.leaves: list[int] = []
        self._app = SimpleNamespace(
            _bind_client=SimpleNamespace(_cache=cache or _FakeCache())
        )

    async def start(self):
        self.started += 1

    async def play(self, chat_id, stream, config):
        self.plays.append((int(chat_id), config))

    async def leave_call(self, chat_id):
        self.leaves.append(int(chat_id))

    def on_update(self, _filter):
        def decorate(func):
            return func

        return decorate


class _FakeClient:
    def __init__(self, *, authorized=True):
        self.connected = False
        self._authorized = authorized
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def disconnect(self):
        self.disconnected = True


def _adapter(call=None, client=None, cache=None):
    call = call or _FakeCall(cache=cache)
    client = client or _FakeClient()
    transport = TV.PytgcallsTransport(
        api_id=12345, api_hash="not-a-real-hash", session_path="/tmp/x.session"
    )
    transport._libs = TV._Libs(
        pytgcalls=SimpleNamespace(PyTgCalls=lambda _client: call),
        filters=SimpleNamespace(
            stream_frame="stream_frame",
            chat_update="chat_update",
            call_participant="call_participant",
            stream_end="stream_end",
        ),
        Device=SimpleNamespace(MICROPHONE="mic"),
        Direction=object(),
        Frame=object(),
        StreamFrames=object(),
        GroupCallConfig=lambda **kwargs: ("config", kwargs),
        telethon=SimpleNamespace(TelegramClient=lambda *a, **k: client),
    )
    return transport, call, client


def _stub_discovery(monkeypatch, result=None, *, raises=None, seen=None):
    async def fake(client, chat_id):
        if seen is not None:
            seen.append(int(chat_id))
        if raises is not None:
            raise raises
        if callable(result):
            return result(int(chat_id))
        return result

    monkeypatch.setattr(TV.call_discovery, "discover", fake)


#: An opaque stand-in for a discovered call. The adapter never inspects it — it
#: seeds it and plays — so a sentinel keeps the adapter tests free of Telethon.
_A_CALL = object()


def _active(chat_id):
    return CD.Discovery(CD.ACTIVE, input_call=_A_CALL)


def test_the_library_is_started_before_it_is_asked_to_play(monkeypatch):
    """``play`` is guarded by ``@mtproto_required`` and raises ``ClientNotStarted``
    until ``start`` has run. The order is the assertion, not merely that both
    happened — starting after the first join is starting too late."""
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, _active(0))
    asyncio.run(transport.join(-1001112223334))
    assert call.started == 1
    assert len(call.plays) == 1


def test_the_discovered_call_is_seeded_into_the_library(monkeypatch):
    """Handing over the call we found is what stops the library consulting its
    own finder — and the swallowed exception inside it."""
    cache = _FakeCache()
    transport, call, _ = _adapter(cache=cache)
    found_call = object()
    _stub_discovery(monkeypatch, CD.Discovery(CD.ACTIVE, input_call=found_call))
    asyncio.run(transport.join(-1001112223334))
    assert cache.store == {-1001112223334: found_call}
    assert len(call.plays) == 1


def test_a_seed_that_fails_does_not_fail_the_join(monkeypatch):
    """The seed is private API. If a release moves it, the join must still work:
    the library falls back to looking the call up itself."""
    transport, call, _ = _adapter(cache=_FakeCache(explode=True))
    _stub_discovery(monkeypatch, _active(0))
    asyncio.run(transport.join(-1001112223334))
    assert len(call.plays) == 1


def test_no_active_call_is_refused_with_its_own_reason(monkeypatch):
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, CD.Discovery(CD.NONE))
    with pytest.raises(errors.JoinRejected) as caught:
        asyncio.run(transport.join(-1001112223334))
    assert caught.value.reason == errors.REASON_NO_ACTIVE_CALL
    assert call.plays == []


def test_a_scheduled_call_is_refused_as_scheduled(monkeypatch):
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, CD.Discovery(CD.SCHEDULED, input_call=object()))
    with pytest.raises(errors.JoinRejected) as caught:
        asyncio.run(transport.join(-1001112223334))
    assert caught.value.reason == errors.REASON_SCHEDULED_CALL
    assert call.plays == []


def test_a_call_the_account_cannot_see_is_refused_as_not_visible(monkeypatch):
    transport, _, _ = _adapter()
    _stub_discovery(monkeypatch, CD.Discovery(CD.NO_ACCESS, detail="ChannelPrivateError"))
    with pytest.raises(errors.JoinRejected) as caught:
        asyncio.run(transport.join(-1001112223334))
    assert caught.value.reason == errors.REASON_CALL_NOT_VISIBLE


def test_a_discovery_that_raises_is_named_rather_than_escaping(monkeypatch):
    """A resolver bug must not surface as an unrelated exception type. It is a
    discovery failure, and it says so."""
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, raises=RuntimeError("boom"))
    with pytest.raises(errors.JoinRejected) as caught:
        asyncio.run(transport.join(-1001112223334))
    assert caught.value.reason == errors.REASON_DISCOVERY_FAILED
    assert "RuntimeError" in caught.value.detail
    assert call.plays == []


def test_a_second_join_is_a_no_op(monkeypatch):
    """Joining twice must not run discovery or play twice. The session guards
    against two sessions in one room; this is the adapter's own guard, so a
    repeated join is harmless rather than an error."""
    transport, call, _ = _adapter()
    seen: list[int] = []
    _stub_discovery(monkeypatch, _active(0), seen=seen)
    asyncio.run(transport.join(-1001112223334))
    asyncio.run(transport.join(-1001112223334))
    assert seen == [-1001112223334]
    assert call.started == 1
    assert len(call.plays) == 1


def test_leaving_forgets_the_seeded_call(monkeypatch):
    """A seeded call outlives ``leave_call`` in the library's cache, so it is
    dropped explicitly — otherwise a later join in the same group would be
    handed a call that has ended."""
    cache = _FakeCache()
    transport, call, _ = _adapter(cache=cache)
    _stub_discovery(monkeypatch, _active(0))
    asyncio.run(transport.join(-1001112223334))
    asyncio.run(transport.leave(-1001112223334))
    assert call.leaves == [-1001112223334]
    assert cache.dropped == [-1001112223334]
    assert cache.store == {}


def test_close_leaves_every_joined_call_by_id(monkeypatch):
    """The disconnect path. ``leave_call`` needs a chat id; calling it without
    one raised ``TypeError`` into a swallowing handler and left the account in
    the call. This asserts the id is passed, for every call held."""
    transport, call, client = _adapter()
    _stub_discovery(monkeypatch, _active(0))
    asyncio.run(transport.join(-1001112223334))
    asyncio.run(transport.join(-1005556667778))
    asyncio.run(transport.close())
    assert set(call.leaves) == {-1001112223334, -1005556667778}
    assert client.disconnected is True


def test_close_is_idempotent(monkeypatch):
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, _active(0))
    asyncio.run(transport.join(-1001112223334))
    asyncio.run(transport.close())
    asyncio.run(transport.close())
    assert call.leaves == [-1001112223334]


def test_close_without_a_join_is_safe():
    """Closing an adapter that never joined must not raise and must not invent a
    call to leave — there is no client to disconnect either, because the client is
    only built by a join."""
    transport, call, _ = _adapter()
    asyncio.run(transport.close())
    assert call.leaves == []
    assert transport._client is None


def test_an_unauthorised_session_is_not_configured(monkeypatch):
    """A session file that exists but is not logged in is a configuration
    problem, distinct from a refused join."""
    transport, _, _ = _adapter(client=_FakeClient(authorized=False))
    _stub_discovery(monkeypatch, _active(0))
    with pytest.raises(errors.NotConfigured) as caught:
        asyncio.run(transport.join(-1001112223334))
    assert caught.value.reason == errors.REASON_NOT_CONFIGURED


# ══ The boundary this file must not cross ═════════════════════════════════
def _imported_names(path: str) -> set[str]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            names.update(alias.name for alias in node.names)
    return names


def test_the_transport_and_resolver_never_reach_another_ai_workload():
    """Voice Live is the assistant through a microphone, not a second path into
    the conversational, acquisition, moderation or transcription layers. A live
    call must not start an acquisition pass or a conversational answer, and the
    cheapest way to keep that true is to fail when the import is written."""
    forbidden = {
        "chat",
        "ai_intent",
        "ai_moderation",
        "transcribe",
        "web_search",
        "acquisition",
        "awareness",
        "awareness_context",
    }
    for name in ("telegram_voice.py", "call_discovery.py"):
        path = os.path.join(ROOT, "app", "voice_live", name)
        crossed = _imported_names(path) & forbidden
        assert not crossed, f"{name} imports {sorted(crossed)}"


def test_the_resolver_does_not_import_telethon_at_module_load():
    """The package is imported where the transport library is absent, and that
    has to keep working. The Telethon import is inside ``discover``, reached only
    after the transport has confirmed the library exists."""
    path = os.path.join(ROOT, "app", "voice_live", "call_discovery.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name != "telethon" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "telethon"


def test_the_transport_still_satisfies_its_own_protocol():
    transport, _, _ = _adapter()
    assert isinstance(transport, TV.TelegramVoiceTransport)


def test_the_join_path_never_asks_for_a_new_call(monkeypatch):
    """``auto_start`` must stay false. A bot that creates a voice chat because
    none was open is a bot that rings a room nobody asked it to ring."""
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, _active(0))
    asyncio.run(transport.join(-1001112223334))
    (_chat, config) = call.plays[0]
    assert config[1]["auto_start"] is False


def test_the_adapter_does_not_claim_a_join_it_did_not_make(monkeypatch):
    """A refusal must leave the adapter out of the call, so ``close`` has nothing
    to leave and the session cannot believe it is in a room it is not."""
    transport, call, _ = _adapter()
    _stub_discovery(monkeypatch, CD.Discovery(CD.NONE))
    with pytest.raises(errors.JoinRejected):
        asyncio.run(transport.join(-1001112223334))
    assert transport._joined == set()
    asyncio.run(transport.close())
    assert call.leaves == []
