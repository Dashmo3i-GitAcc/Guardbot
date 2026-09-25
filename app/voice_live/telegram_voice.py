"""Carrying the call: the interface, a working double, and the real adapter.

The interface exists so the session, the audio pipeline, the speaker map and the
barge-in logic can be tested without a voice channel, and so the native transport
can be swapped later without touching any of them.

What is true now
----------------
``py-tgcalls`` (2.3.3) over ``ntgcalls`` (2.2.5) is the transport. It joins a
voice chat, hands over incoming PCM tagged with an ``ssrc``, accepts outgoing PCM
for the microphone, and lists participants with the ``user_id`` each ``ssrc``
belongs to — which is the whole of what this feature needs, including speaker
identity. It installs on this deployment's Python 3.12 (``ntgcalls 2.2.5``
publishes ``cp312-manylinux_2_28_x86_64`` wheels; an earlier note that claimed
otherwise was wrong). The MTProto credential — an ``api_id``/``api_hash`` pair
and a logged-in user session — now exists, and this adapter has held a real call
with it.

Finding the call is done here, not left to the library
------------------------------------------------------
``py-tgcalls`` discovers an active call by reading ``ChannelFull.call`` and by
caching ``UpdateGroupCall``. Its cache wraps the fallback lookup in a bare
``except Exception: pass``, so every discovery error — not a member, forbidden,
flood wait, an unresolvable id — is flattened into ``None`` and reported by the
caller as one ``NoActiveGroupCall``. That single sentence covers four different
problems with four different fixes, so this adapter finds the call itself with
:mod:`app.voice_live.call_discovery`, keeps the outcomes apart, and hands the
found call to the library so it does not look it up a second time and miss. The
library's own finder remains the fallback when handing it over is not possible.

The session is connected with ``connect()`` and checked with
``is_user_authorized()`` rather than started with ``start()``, because
``start()`` prompts interactively for a phone number when the session is not
logged in, and a bot process that blocks on a phone-number prompt is a bot
process that has stopped moderating.

One thing still unverified, and said so
---------------------------------------
The native layer's contract for an outgoing microphone frame — 16-bit PCM at
48 kHz, mono, 20 ms — is what ``ntgcalls`` documents and what ``audio.py``
implements, but it has not been confirmed against a live call's audio. It is one
constant in ``audio.py`` if it turns out to be wrong, and stating that is better
than presenting an unverified number as a measured one.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable

from .. import config
from . import audio, call_discovery, errors

log = logging.getLogger("guardbot.voice.transport")

# A sentinel on the incoming queue meaning "the participant list changed, go and
# re-read it" rather than "here is audio". A tuple rather than a separate queue
# because the two are ordered with respect to each other: a roster change that
# arrived *after* a frame must not be applied before it, or the frame would be
# attributed with the wrong map.
_ROSTER_CHANGED: tuple[int, bytes] = (0, b"")


@dataclass(frozen=True)
class _Libs:
    """The transport libraries, imported once and held by name.

    A bundle rather than a tuple: the tuple this replaced had eight positions and
    two call sites unpacked it with placeholders, which is how the wrong class
    gets bound to the wrong name the first time the order changes.
    """

    pytgcalls: object
    filters: object
    Device: object
    Direction: object
    Frame: object
    StreamFrames: object
    GroupCallConfig: object
    telethon: object


# Why a transport is unusable, as machine keys. Distinct because they need
# different fixes: one is an operator adding a dependency, the other an operator
# adding a credential.
UNAVAILABLE_LIBRARY = "library_missing"
UNAVAILABLE_CREDENTIALS = "credentials_missing"
UNAVAILABLE_NOT_AUTHORISED = "session_not_authorised"
UNAVAILABLE_DISABLED = "transport_disabled"


@runtime_checkable
class TelegramVoiceTransport(Protocol):
    """Everything this package needs from a voice chat, and nothing else.

    Deliberately narrow, for the same reason ``admin_service.Gateway`` is: the
    complete set of things this subsystem can do to a call is these seven
    methods, so reviewing that set is reviewing the whole surface. There is no
    ``call`` method, no way to pass a raw MTProto request, and no access to the
    underlying client.
    """

    @property
    def available(self) -> bool:
        """Whether a call could actually be joined right now."""

    @property
    def unavailable_reason(self) -> str:
        """Why not, as a machine key. ``""`` when available."""

    async def join(self, chat_id: int, *, invite_hash: str = "") -> None:
        """Join the voice chat in this group."""

    async def leave(self, chat_id: int) -> None:
        """Leave it."""

    async def participants(self, chat_id: int) -> list:
        """Everyone currently in the call, with their ``ssrc``."""

    def incoming(self) -> AsyncIterator[tuple[int, bytes]]:
        """Incoming audio as ``(ssrc, pcm48)``, until the call ends.

        One protocol subtlety, and it is the only one: a frame whose ``ssrc`` is
        ``0`` and whose data is empty is **not audio** — it means "the participant
        list has changed, re-read it". It shares the same stream rather than
        having a queue of its own because the two are ordered with respect to
        each other, and a roster change that arrived after a frame must not be
        applied before it, or that frame gets attributed with the wrong map.
        """

    async def play(self, chat_id: int, pcm48: bytes) -> None:
        """Send one frame of audio into the call."""

    async def stop(self, chat_id: int) -> None:
        """Stop playing. Called on a barge-in, and it must be immediate."""

    async def close(self) -> None:
        """Release everything. Idempotent."""


# ── The double ────────────────────────────────────────────────────────────
class FakeTelegramVoice:
    """A transport that joins nothing, for tests and for dry runs.

    Fully functional as an *interface*: it records what it was asked to do,
    lets a test push incoming audio and read what was played, and can be told
    to fail at any step. That is what makes the session, the audio pipeline, the
    speaker map and the barge-in logic testable without a voice channel — and
    those are where every interesting behaviour of this feature lives.

    It is not a way to run the feature. ``available`` is True so that a test can
    drive the happy path, but ``unavailable_reason`` reports ``transport_disabled``
    when the configured transport is ``fake``, and the session refuses to start a
    call in that configuration outside a test. A fake transport that could be
    mistaken for a real one would be the worst possible thing to ship.
    """

    def __init__(self, *, fail_join: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail_join = fail_join
        self._participants: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self.playing = False

    # -- the interface --
    @property
    def available(self) -> bool:
        return not self._closed

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else UNAVAILABLE_DISABLED

    async def join(self, chat_id: int, *, invite_hash: str = "") -> None:
        self.calls.append(("join", int(chat_id), invite_hash))
        if self.fail_join:
            raise errors.JoinRejected("the double was told to refuse the join")
        if self._closed:
            raise errors.TransportUnavailable("the double is closed")

    async def leave(self, chat_id: int) -> None:
        self.calls.append(("leave", int(chat_id)))
        self.playing = False

    async def participants(self, chat_id: int) -> list:
        self.calls.append(("participants", int(chat_id)))
        return list(self._participants)

    async def play(self, chat_id: int, pcm48: bytes) -> None:
        self.calls.append(("play", int(chat_id), len(pcm48)))
        self.playing = True

    async def stop(self, chat_id: int) -> None:
        self.calls.append(("stop", int(chat_id)))
        self.playing = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.calls.append(("close",))
        # Unblock anything waiting on incoming audio, so a test that is
        # iterating does not hang when the call ends.
        await self._incoming.put(None)

    def incoming(self) -> AsyncIterator[tuple[int, bytes]]:
        return self._incoming_frames()

    async def _incoming_frames(self) -> AsyncIterator[tuple[int, bytes]]:
        while True:
            item = await self._incoming.get()
            if item is None:
                return
            yield item

    # -- the test controls --
    def feed(self, ssrc: int, pcm48: bytes) -> None:
        """Push a frame of incoming audio, as the real handler would."""
        self._incoming.put_nowait((int(ssrc), pcm48))

    def end_stream(self) -> None:
        """Close the incoming stream without closing the transport."""
        self._incoming.put_nowait(None)

    def set_participants(self, participants) -> None:
        self._participants = list(participants or ())

    def note_roster_change(self) -> None:
        """Signal that the participant list changed, as the real handler does.

        Pushed onto the same stream as the audio, in order, so a test can prove
        that a frame arriving before a roster change is attributed with the old
        map and one arriving after is attributed with the new one.
        """
        self._incoming.put_nowait(_ROSTER_CHANGED)

    def played_frames(self) -> int:
        """How many frames were played. For assertions about barge-in."""
        return sum(1 for call in self.calls if call[0] == "play")

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


# ── The real adapter ──────────────────────────────────────────────────────
class PytgcallsTransport:
    """The real transport: ``py-tgcalls`` over a Telethon MTProto session.

    Constructed lazily and never at import time, so that a deployment without
    the dependency pays nothing and a deployment with the feature switched off
    never touches it. Every failure to become usable is reported as a *reason*
    rather than raised from the constructor, because the caller's question is
    "can I hold a call" and the answer "no, and here is which of the three
    things is missing" is more useful than an exception at import.

    The Telethon client is created with ``connect()`` and then checked for
    authorisation rather than started with ``start()``. ``start()`` prompts
    interactively for a phone number when the session is not authorised, and a
    bot process that blocks on a phone-number prompt is a bot process that has
    stopped moderating.
    """

    def __init__(self, *, api_id: int = 0, api_hash: str = "", session_path: str = ""):
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "")
        self.session_path = str(session_path or "")
        self._client = None
        self._call = None
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._reason = ""
        self._closed = False
        self._frames_seen = 0
        self._libs = None
        # The chats this adapter is currently in. Kept so that a second join is
        # a no-op and so that ``close`` knows which calls to leave — the
        # library's ``leave_call`` needs a chat id, and without this there is
        # nothing to pass it.
        self._joined: set[int] = set()

    # -- availability, decided without touching the network --
    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    @property
    def unavailable_reason(self) -> str:
        """Why this transport cannot be used, checked in cheapest-first order.

        The order is the point: a deployment missing the library should be told
        that, not told to go and fetch credentials it cannot use yet. And an
        operator who has added credentials but not the dependency would
        otherwise be sent to my.telegram.org for nothing.
        """
        if self._closed:
            return UNAVAILABLE_LIBRARY
        libs = self._import()
        if libs is None:
            return UNAVAILABLE_LIBRARY
        if not self.api_id or not self.api_hash:
            return UNAVAILABLE_CREDENTIALS
        if not self.session_path:
            return UNAVAILABLE_CREDENTIALS
        return self._reason

    def _import(self):
        """Import the transport libraries, once, remembering the answer.

        Returns a small named bundle rather than a tuple, because the tuple had
        eight positions and two of the call sites unpacked it with placeholders —
        which is a way to silently bind the wrong class to the wrong name the
        first time the order changes. Names cannot be reordered by accident.
        """
        if self._libs is not None:
            return self._libs or None
        try:
            import pytgcalls
            from pytgcalls import filters
            from pytgcalls.types import (
                Device,
                Direction,
                Frame,
                GroupCallConfig,
                StreamFrames,
            )
            import telethon
        except Exception as exc:  # noqa: BLE001 - absence is an expected state
            log.info(
                "[voice] the voice-chat transport library is unavailable (%s)",
                type(exc).__name__,
            )
            self._libs = False
            return None
        self._libs = _Libs(
            pytgcalls=pytgcalls,
            filters=filters,
            Device=Device,
            Direction=Direction,
            Frame=Frame,
            StreamFrames=StreamFrames,
            GroupCallConfig=GroupCallConfig,
            telethon=telethon,
        )
        return self._libs

    # -- the interface --
    async def join(self, chat_id: int, *, invite_hash: str = "") -> None:
        """Connect the MTProto session and join the group's voice chat.

        Both halves are required and the failure modes are distinct: a session
        that cannot be authorised is ``not_configured``, and a join the server
        refuses is ``join_rejected`` — carrying a reason that says *which*
        refusal, so the log names the fix rather than the symptom.
        """
        chat_id = int(chat_id)
        reason = self.unavailable_reason
        if reason == UNAVAILABLE_LIBRARY:
            raise errors.NotConfigured(
                "the voice-chat transport library is not installed",
                reason=errors.REASON_NOT_CONFIGURED,
            )
        if reason == UNAVAILABLE_CREDENTIALS:
            raise errors.NotConfigured(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured",
                reason=errors.REASON_NOT_CONFIGURED,
            )
        libs = self._libs

        if self._client is None:
            self._client = libs.telethon.TelegramClient(
                self.session_path, self.api_id, self.api_hash
            )
            await self._client.connect()
            if not await self._client.is_user_authorized():
                # Deliberately not ``start()``: it would block on a phone-number
                # prompt, and a bot that stops moderating to ask a question in a
                # log nobody is reading is worse than one that says it cannot.
                self._reason = UNAVAILABLE_NOT_AUTHORISED
                raise errors.NotConfigured(
                    "the voice-live MTProto session is not logged in",
                    reason=errors.REASON_NOT_CONFIGURED,
                )

        if chat_id in self._joined:
            # A second join for a call we are already in is not an error, and
            # re-running discovery for it is pure cost. ``play`` is idempotent
            # underneath, so this only skips the round trips.
            log.info("[voice] already in the voice chat chat=%s", chat_id)
            return

        if self._call is None:
            self._call = libs.pytgcalls.PyTgCalls(self._client)
            self._register(libs)
            # ``play`` is wrapped in ``@mtproto_required``, which raises
            # ``ClientNotStarted`` until ``start`` has wired the library's
            # handlers. Without this the very first join fails before Telegram
            # is ever asked.
            await self._call.start()

        # Find the call here rather than letting the library report every
        # failure as ``NoActiveGroupCall``. A discovery that raises rather than
        # answering is itself a failure worth naming, so it is caught and
        # reported as one instead of escaping as an unrelated exception.
        try:
            found = await call_discovery.discover(self._client, chat_id)
        except BaseException as exc:  # noqa: BLE001 - a discovery is not a crash
            raise errors.JoinRejected(
                f"discovery:{type(exc).__name__}",
                reason=errors.REASON_DISCOVERY_FAILED,
            ) from None

        if not found.active:
            raise errors.JoinRejected(
                f"{found.kind}:{found.detail}" if found.detail else found.kind,
                reason=call_discovery.reason_for(found.kind),
            )

        # Hand the library exactly the call that was found, so its own lookup —
        # and the swallowed exception inside it — is not consulted at all.
        self._seed_call(chat_id, found.input_call)

        try:
            await self._call.play(
                chat_id,
                None,
                libs.GroupCallConfig(
                    invite_hash=invite_hash or None, auto_start=False
                ),
            )
        except BaseException as exc:  # noqa: BLE001 - a join is not a crash
            raise errors.JoinRejected(f"{type(exc).__name__}") from None
        self._joined.add(chat_id)
        log.info("[voice] joined the voice chat chat=%s", chat_id)

    def _seed_call(self, chat_id: int, input_call) -> bool:
        """Give PyTgCalls the call we already found, through one guarded seam.

        The library keeps its own ``InputGroupCall`` cache and consults it before
        asking Telegram. Filling that cache from outside is private API, so it is
        reached in exactly one place, feature-detected, and allowed to fail: if a
        future release renames it the join still works, because the library then
        falls back to looking the call up itself. Returns whether the seed took.

        This is the adapter's own instance cache, not module or global state, and
        nothing outside this adapter's call can observe it.
        """
        try:
            cache = self._call._app._bind_client._cache
            cache.set_cache(int(chat_id), input_call)
            return True
        except BaseException as exc:  # noqa: BLE001 - the fallback is the library's
            log.debug(
                "[voice] could not seed the call cache (%s); "
                "the library will look the call up itself",
                type(exc).__name__,
            )
            return False

    def _drop_call(self, chat_id: int) -> None:
        """Forget the call in the library's cache after leaving it.

        ``leave_call`` clears the library's peer state but not the call cache, so
        a seeded call would linger there for the cache's lifetime and be handed
        to a later join that should have looked for a new one.
        """
        if self._call is None:
            return
        try:
            self._call._app._bind_client._cache.drop_cache(int(chat_id))
        except BaseException:  # noqa: BLE001 - dropping a cache is best effort
            log.debug("[voice] dropping the call cache raised; ignoring", exc_info=True)

    def _register(self, libs: _Libs) -> None:
        """Wire the library's events onto this adapter's queue.

        Only one event carries anything this package uses: incoming frames. The
        others are registered because they are the call's lifecycle and the
        session needs to know when somebody joins or leaves — speaker identity
        depends on it — but none of them is allowed to raise into the library's
        dispatcher, because an exception there takes the call down.
        """
        @self._call.on_update(libs.filters.stream_frame)
        async def _on_frames(_, update):
            for frame in getattr(update, "frames", None) or ():
                data = getattr(frame, "frame", None)
                ssrc = int(getattr(frame, "ssrc", 0) or 0)
                if isinstance(data, (bytes, bytearray)) and data:
                    self._frames_seen += 1
                    self._incoming.put_nowait((ssrc, bytes(data)))

        @self._call.on_update(libs.filters.chat_update)
        async def _on_chat_update(_, update):
            # A participant list change. The roster is re-read rather than
            # trusting the delta, because the delta describes one person and the
            # speaker map has to be right about all of them.
            self._incoming.put_nowait(_ROSTER_CHANGED)

        @self._call.on_update(libs.filters.call_participant)
        async def _on_participant(_, update):
            self._incoming.put_nowait(_ROSTER_CHANGED)

        @self._call.on_update(libs.filters.stream_end)
        async def _on_stream_end(_, update):
            log.info("[voice] a stream ended")

    async def leave(self, chat_id: int) -> None:
        chat_id = int(chat_id)
        self._joined.discard(chat_id)
        if self._call is None:
            return
        try:
            await self._call.leave_call(chat_id)
        except BaseException as exc:  # noqa: BLE001 - teardown is best effort
            log.info("[voice] leaving the call raised (%s)", type(exc).__name__)
        # Drop the seeded call *after* leaving, because ``leave_call`` reads it.
        self._drop_call(chat_id)

    async def participants(self, chat_id: int) -> list:
        if self._call is None:
            return []
        try:
            people = await self._call.get_participants(int(chat_id))
        except BaseException as exc:  # noqa: BLE001
            log.info("[voice] participant list unavailable (%s)", type(exc).__name__)
            return []
        # ``GroupCallParticipant`` carries ``user_id`` and ``source``; ``source``
        # is the ssrc, and that pairing is the whole of speaker identity.
        return [
            {
                "user_id": int(getattr(p, "user_id", 0) or 0),
                "ssrc": int(getattr(p, "source", 0) or 0),
            }
            for p in (people or ())
        ]

    def incoming(self) -> AsyncIterator[tuple[int, bytes]]:
        return self._incoming_frames()

    async def _incoming_frames(self) -> AsyncIterator[tuple[int, bytes]]:
        while True:
            item = await self._incoming.get()
            if item is None:
                return
            yield item

    async def play(self, chat_id: int, pcm48: bytes) -> None:
        """Send one frame into the call's microphone.

        The format is the one ``audio.py`` produces: 16-bit PCM, 48 kHz, mono.
        See the module docstring — this is the one number in this file that has
        not been confirmed against a live call.
        """
        if self._call is None or not pcm48:
            return
        try:
            await self._call.send_frame(
                int(chat_id), self._libs.Device.MICROPHONE, pcm48
            )
        except BaseException as exc:  # noqa: BLE001
            raise errors.TransportUnavailable(
                f"could not send a frame ({type(exc).__name__})"
            ) from None

    async def stop(self, chat_id: int) -> None:
        """Stop playback.

        Implemented by sending a single frame of silence rather than by pausing
        the stream. A pause is a state the native layer owns and would have to be
        resumed; the session already tracks whether it is speaking, and a
        transport that also tracked it would be a second answer to the same
        question. The queued audio is dropped by the session, which is where the
        queue lives.
        """
        if self._call is None:
            return
        try:
            await self._call.send_frame(
                int(chat_id),
                self._libs.Device.MICROPHONE,
                bytes(audio.FRAME_BYTES[audio.RATE_TELEGRAM]),
            )
        except BaseException:  # noqa: BLE001 - a barge-in must never raise
            log.debug("[voice] silencing the call raised; ignoring", exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._call is not None:
            # ``leave_call`` takes a chat id. This used to be called with no
            # argument, which raised ``TypeError`` into the handler below and was
            # swallowed — so shutting the adapter down left the account sitting
            # in the voice chat until the socket happened to die. Leave each call
            # we know about, by id.
            for chat_id in sorted(self._joined):
                try:
                    await self._call.leave_call(chat_id)
                except BaseException:  # noqa: BLE001
                    log.debug("[voice] final leave raised; ignoring", exc_info=True)
                self._drop_call(chat_id)
            self._joined.clear()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except BaseException:  # noqa: BLE001
                log.debug("[voice] disconnect raised; ignoring", exc_info=True)
        await self._incoming.put(None)

    def describe(self) -> dict:
        """A safe summary: availability and counters, never a credential."""
        return {
            "kind": "pytgcalls",
            "available": self.available,
            "reason": self.unavailable_reason,
            "frames_seen": self._frames_seen,
            "configured": bool(self.api_id and self.api_hash),
        }


def build(*, transport: str = "") -> TelegramVoiceTransport:
    """The configured transport, or the double.

    ``auto`` — the default — means the real one when it is usable and the double
    otherwise, and the *session* refuses to hold a call on a double unless it was
    explicitly asked for. So ``auto`` cannot silently turn a real deployment into
    a pretend one: it can only produce an adapter that reports why it cannot
    work, or a double that the session will not use in production.
    """
    choice = (transport or config.GEMINI_LIVE_TRANSPORT or "auto").strip().lower()
    if choice == "fake":
        return FakeTelegramVoice()
    if choice in ("pytgcalls", "auto"):
        return PytgcallsTransport(
            api_id=config.TELEGRAM_API_ID,
            api_hash=config.TELEGRAM_API_HASH,
            session_path=config.GEMINI_LIVE_SESSION_PATH,
        )
    log.warning("[voice] unknown transport %r; using the real adapter", choice)
    return PytgcallsTransport(
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
        session_path=config.GEMINI_LIVE_SESSION_PATH,
    )


def is_double(transport) -> bool:
    """Whether a transport is the test double.

    Asked by the session before it joins anything. A double is fine in a test and
    is not fine in a deployment, and the only way to tell them apart is to ask —
    which is why the double is a named type rather than a mock that happens to
    behave like one.
    """
    return isinstance(transport, FakeTelegramVoice)
