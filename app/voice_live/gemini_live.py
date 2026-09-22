"""The real provider connection: one bidirectional Live session, held open.

This module is the only place in the package that imports the provider SDK, and
it is deliberately the only place that knows the provider's event names. Above
it, the session works in ``LiveEvent`` — a small closed vocabulary of things that
can happen in a call — so that the whole lifecycle can be tested without a
network and without a credential.

What was measured before this was written
----------------------------------------
Every number and constraint below came from probing the live API with this
project's own key class, on real Persian speech synthesised by this project's own
TTS. Three findings changed the design rather than being worked around:

* **The feed must stay continuous.** The provider's automatic voice-activity
  detector needs to hear the *end* of an utterance, and it finds it in the
  trailing silence. A caller that sends one burst of speech and waits gets
  nothing back: the session connects, emits ``setupComplete``, and hangs. So
  audio is forwarded as a continuous stream, silence included, and that is a
  property of the caller rather than of this class — but it is the reason the
  transport has no "send an utterance" method.

* **Explicit activity control is rejected while automatic detection is on.**
  Sending ``activity_start`` / ``activity_end`` alongside auto-detection fails
  with ``1007 Explicit activity control is not supported when automatic activity
  detection is enabled``. So this class never sends them, and the VAD is left to
  the provider — which is also the right place for it, because a second detector
  here would disagree with the one deciding the turn.

* **The model choice is not the obvious one.** ``gemini-2.5-flash-native-audio``
  is purpose-built for this and measured *twice as slow* on Persian (2.21 s from
  end of utterance to first audio byte, against 1.12 s for ``gemini-3.8-live``),
  and it rejects every explicit Persian language code. The default lives in
  ``app/config.py`` with the numbers.

Failure classification
----------------------
A provider failure has to be answered *while the call is happening*, and the only
question the caller has is whether to try again. So every exception raised here
is mapped onto ``app/voice_live/errors.py``, which is where retryability is
decided — a rejected language code is not retryable and a dropped socket is, and
the difference is not in the exception's type but in what it means.

Nothing here logs a credential. The key is held as an attribute, never rendered,
and every detail string that leaves this module is scrubbed of anything that
looks like one — because a provider error message is provider-authored text and
this is the boundary where it stops.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from . import audio, errors

log = logging.getLogger("guardbot.voice.gemini")

# ── The event vocabulary ──────────────────────────────────────────────────
# What can happen in a live session, as far as anything above this module is
# concerned. Deliberately small: each one is a thing the session has to *decide*
# about, and an event nobody branches on would be a second copy of the provider's
# message type.
SETUP = "setup"
AUDIO = "audio"
TRANSCRIPT_IN = "transcript_in"
TRANSCRIPT_OUT = "transcript_out"
TURN_COMPLETE = "turn_complete"
INTERRUPTED = "interrupted"
TOOL_CALL = "tool_call"
GO_AWAY = "go_away"
RESUMPTION = "resumption"
CLOSED = "closed"


@dataclass(frozen=True)
class LiveEvent:
    """One thing that happened, in provider-neutral terms.

    A frozen record rather than a class hierarchy, because the session's handling
    of these is a dispatch on ``kind`` and a hierarchy would only move the
    dispatch somewhere less visible. ``audio`` and ``text`` are populated only
    for the kinds that carry them, and neither is ever logged by this module or
    by the session.
    """

    kind: str
    audio: bytes = b""
    text: str = ""
    calls: tuple = ()
    handle: str = ""
    detail: str = ""
    at: float = field(default_factory=time.monotonic)

    @property
    def is_audio(self) -> bool:
        return self.kind == AUDIO and bool(self.audio)

    def describe(self) -> dict:
        """A safe summary. No audio, no transcript, no credential."""
        return {
            "kind": self.kind,
            "bytes": len(self.audio),
            "calls": len(self.calls),
            "has_handle": bool(self.handle),
        }


def classify(exc: BaseException) -> errors.ProviderError:
    """Map a provider or transport exception onto this package's taxonomy.

    The classification is by *meaning*, not by type, and it is written out
    rather than derived, because the two mistakes here are expensive in opposite
    directions: calling a rejected configuration retryable loops for ever and
    holds a voice channel while it does, and calling a dropped socket fatal ends
    calls that a two-second reconnect would have saved.

    A setup rejection is recognised by the websocket close codes the provider
    uses for a bad request — 1007 (invalid payload), 1008 (policy), and the HTTP
    codes a rejected key or model produce. Everything else is treated as weather:
    retryable, with backoff.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return errors.ProviderTimeout("the provider did not answer in time")
    if isinstance(exc, errors.ProviderError):
        return exc

    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    # A rejected configuration, never retried. These are the strings the provider
    # actually produced while this feature was being measured.
    for marker in (
        "1007",
        "1008",
        "unsupported language",
        "not supported",
        "invalid argument",
        "api key not valid",
        "permission denied",
        "not found",
        "400",
        "403",
        "404",
    ):
        if marker in low:
            return errors.SetupRejected(_scrub(text))
    for marker in ("1000", "1001", "1011", "1012", "1013", "connection", "closed"):
        if marker in low:
            return errors.ConnectionLost(_scrub(text))
    # Unknown, and therefore *not* retryable — see ``errors.is_retryable``. The
    # safe direction for an unrecognised failure is to end the call and say so,
    # not to loop.
    return errors.ProviderError(_scrub(text))


def _scrub(text: str) -> str:
    """Remove anything credential-shaped from a string on its way to a log.

    A backstop rather than a control: this module never puts a key into a message
    it builds. It exists because provider error text is provider-authored and
    sometimes echoes the request, and the one place that must never happen is a
    log line.
    """
    out = str(text or "")
    for marker in ("AQ.", "AIza"):
        if marker in out:
            head, _, rest = out.partition(marker)
            # Keep the prefix, drop the secret, keep the length as a hint.
            tail = rest.split()[0] if rest.split() else ""
            out = f"{head}{marker}[redacted:{len(tail)}]"
    return out[:500]


class GeminiLiveTransport:
    """One Live session: open it, feed it, read it, close it.

    Not reusable. A transport is bound to one call for its whole life, because
    the provider's session state *is* the conversation — the turns so far, the
    pending tool call, the resumption handle. A transport that could be reopened
    would be a second way to have a conversation, and the session already has one.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        language: str = "",
        voice: str = "",
        system_instruction: str = "",
        tools=None,
        handle: str = "",
        timeout: float | None = None,
    ) -> None:
        if not model:
            raise ValueError("a live transport needs a model")
        if not api_key:
            # Refused here rather than at connect time, so that a pool that
            # handed out an empty credential is caught by the caller that built
            # this object and not by a socket error ten seconds later.
            raise ValueError("a live transport needs a credential")
        self.model = model
        self._api_key = api_key
        self.language = language
        self.voice = voice
        self.system_instruction = system_instruction
        self.tools = list(tools or ())
        self.timeout = float(timeout if timeout is not None else 30.0)
        # The resumption handle, if this session is being reopened after a
        # provider-side migration. Carrying it is what keeps the conversation
        # rather than replaying it.
        self.handle = str(handle or "")
        self._client = None
        self._session = None
        self._cm = None
        self._closed = False
        self.connected_at = 0.0

    # -- lifecycle --
    async def connect(self) -> LiveEvent:
        """Open the session and wait for the provider to accept it.

        Returns the ``SETUP`` event, which is synthesised here rather than read
        off the stream — and that is a fact about the SDK rather than a choice.
        The provider sends ``setupComplete`` as the handshake response, and
        ``google-genai`` consumes it *inside* the connect context manager: it
        stores it on the session and never yields it to ``receive()``. Verified
        against the installed SDK, where the first message a caller ever sees is
        a resumption update. So waiting for a setup event on the stream waits for
        ever, which is exactly the bug this docstring exists to prevent somebody
        reintroducing.

        The consequence is that entering the context manager *is* the
        confirmation, and the timeout below is the connect timeout. Waiting for
        it here rather than leaving it to the receive loop is what makes
        "connected" mean something: a socket that is open but has not been
        accepted can still fail, and a session that reported itself ready before
        the provider agreed would promise audio it could not deliver.
        """
        genai, types = _load_sdk()
        try:
            self._client = genai.Client(api_key=self._api_key)
            self._cm = self._client.aio.live.connect(
                model=self.model, config=self._config(types)
            )
            self._session = await asyncio.wait_for(
                self._cm.__aenter__(), timeout=self.timeout
            )
        except BaseException as exc:  # noqa: BLE001 - classified, then re-raised
            await self.close()
            raise classify(exc) from None

        self.connected_at = time.monotonic()
        session_id = str(getattr(self._session, "session_id", "") or "")
        if getattr(self._session, "setup_complete", None) is None:
            # The socket opened and the context manager returned, but the
            # handshake response was not what the SDK expects. Not fatal — the
            # session is usable — but it means the SDK's contract has moved, and
            # the next thing to break would be silent.
            log.warning(
                "[voice] provider accepted the connection without a setup "
                "response; the SDK's handshake may have changed"
            )
        log.info(
            "[voice] provider session ready model=%s session=%s resumed=%s",
            self.model,
            session_id or "-",
            bool(self.handle),
        )
        return LiveEvent(SETUP, handle=self.handle, detail=session_id)

    async def close(self) -> None:
        """Close the session. Safe to call twice, and safe to call on a failure.

        Idempotent because it is called from teardown paths that cannot know
        whether the connection got far enough to exist — and a second close that
        raised would turn a clean shutdown into a traceback in a log, which is
        how a real failure gets missed.
        """
        if self._closed:
            return
        self._closed = True
        session, cm = self._session, self._cm
        self._session = self._cm = None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                log.debug("[voice] session close raised; ignoring", exc_info=True)
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.debug("[voice] connect context exit raised; ignoring", exc_info=True)

    @property
    def ready(self) -> bool:
        return bool(self._session) and not self._closed

    # -- sending --
    async def send_audio(self, pcm: bytes) -> None:
        """Forward one chunk of 16 kHz PCM.

        Forwarded as it arrives and never batched: the provider's end-of-speech
        detection works on the stream's *timing*, and a caller that accumulated
        audio and sent it in bursts would be sending the provider a conversation
        with the pauses removed.
        """
        if not pcm or not self.ready:
            return
        _, types = _load_sdk()
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=audio.MIME_PROVIDER_IN)
            )
        except BaseException as exc:  # noqa: BLE001
            raise classify(exc) from None

    async def send_context(self, text: str) -> None:
        """Inject a block of server-built text into the session.

        This is how a refreshed awareness snapshot reaches a running call. It
        goes in as a *client turn* rather than as part of the system instruction,
        because the system instruction is fixed when the session opens and the
        whole point of a refresh is that it arrives later.

        The text is context, never an instruction: it is assembled by
        ``awareness_bridge`` from the room's own records, and the model is told
        what the room looks like, not what to do about it.
        """
        if not text or not self.ready:
            return
        _, types = _load_sdk()
        try:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user", parts=[types.Part(text=text)]
                ),
                turn_complete=False,
            )
        except BaseException as exc:  # noqa: BLE001
            raise classify(exc) from None

    async def send_tool_results(self, responses) -> None:
        """Answer the provider's tool call so the turn can finish.

        A live session that never answers a tool call waits for ever, and the
        caller hears silence. The results here are the *outcome sentences* of
        requests that were already authorised and executed elsewhere — this
        module never executes anything and has no way to.
        """
        if not responses or not self.ready:
            return
        _, types = _load_sdk()
        payload = [
            types.FunctionResponse(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                response={"result": str(item.get("result") or "")},
            )
            for item in responses
        ]
        try:
            await self._session.send_tool_response(function_responses=payload)
        except BaseException as exc:  # noqa: BLE001
            raise classify(exc) from None

    # -- receiving --
    async def receive(self) -> AsyncIterator[LiveEvent]:
        """Yield events until the stream ends.

        Ends by yielding a ``CLOSED`` event rather than by returning quietly, so
        that the session has one place to notice a stream that stopped — a loop
        that simply finished would leave the call looking connected while nothing
        was arriving, which is the failure mode this whole class exists to make
        visible.

        Note what never arrives on this stream: the setup message. The SDK
        consumes it inside the connect context manager and stores it on the
        session, so ``connect`` synthesises that event instead — see there.

        Cancellation and generator shutdown are re-raised rather than turned into
        a ``CLOSED`` event. An async generator that catches ``GeneratorExit`` and
        then yields is a runtime error — found by exactly that happening while
        this was being tested — and a session that read "somebody cancelled me"
        as "the provider went away" would begin a reconnect for a call that is
        deliberately being torn down.
        """
        if not self.ready:
            yield LiveEvent(CLOSED, detail="not connected")
            return
        try:
            async for message in self._session.receive():
                for event in self._translate(message):
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except BaseException as exc:  # noqa: BLE001
            failure = classify(exc)
            log.info("[voice] provider stream ended reason=%s", failure.reason)
            yield LiveEvent(CLOSED, detail=failure.reason)
            return
        yield LiveEvent(CLOSED, detail="stream ended")

    # -- translation --
    def _translate(self, message) -> list[LiveEvent]:
        """One provider message to zero or more events.

        A list rather than a single event, and that is a correction rather than a
        convenience. The provider is free to put more than one thing in one
        message — speech *and* its transcript being the obvious pair — and an
        earlier version of this method returned the first match it found, which
        silently dropped the rest. It was caught by watching a live call produce
        three and a half seconds of audio and report an empty answer: the audio
        was translated and the transcript that came with it was thrown away.

        Written against attributes rather than against the SDK's types, because
        the provider sends a union and the SDK's own discrimination has changed
        between releases. Reading ``hasattr`` is stable across those changes; an
        ``isinstance`` chain is not.

        There is deliberately no branch for the setup message: see ``receive``.
        """
        if message is None:
            return []
        events: list[LiveEvent] = []

        # The resumption handle, stored before anything else in the same message
        # is acted on: if the session drops while we are handling the content,
        # the handle is what makes it recoverable.
        update = getattr(message, "session_resumption_update", None)
        if update is not None and getattr(update, "resumable", False):
            handle = str(getattr(update, "new_handle", "") or "")
            if handle:
                self.handle = handle
                events.append(LiveEvent(RESUMPTION, handle=handle))

        go_away = getattr(message, "go_away", None)
        if go_away is not None:
            events.append(
                LiveEvent(
                    GO_AWAY, detail=str(getattr(go_away, "time_left", "") or "")
                )
            )

        tool_call = getattr(message, "tool_call", None)
        if tool_call is not None:
            calls = tuple(
                _call_tuple(c)
                for c in (getattr(tool_call, "function_calls", None) or ())
            )
            if calls:
                events.append(LiveEvent(TOOL_CALL, calls=calls))

        content = getattr(message, "server_content", None)
        if content is not None:
            events.extend(self._content_events(content))
        return events

    @staticmethod
    def _content_events(content) -> list[LiveEvent]:
        """Everything one ``server_content`` message carries.

        Audio first, because it is the field that must never be lost. The markers
        and the transcripts follow, and all of them can be present at once — which
        is the whole reason this returns a list.
        """
        events: list[LiveEvent] = []
        chunk = _inline_audio(content)
        if chunk:
            events.append(LiveEvent(AUDIO, audio=chunk))
        if getattr(content, "interrupted", False):
            events.append(LiveEvent(INTERRUPTED))
        if getattr(content, "turn_complete", False):
            events.append(
                LiveEvent(
                    TURN_COMPLETE,
                    detail=str(getattr(content, "turn_complete_reason", "") or ""),
                )
            )
        heard = getattr(content, "input_transcription", None)
        if heard is not None and str(getattr(heard, "text", "") or ""):
            events.append(LiveEvent(TRANSCRIPT_IN, text=str(heard.text)))
        said = getattr(content, "output_transcription", None)
        if said is not None and str(getattr(said, "text", "") or ""):
            events.append(LiveEvent(TRANSCRIPT_OUT, text=str(said.text)))
        return events

    # -- configuration --
    def _config(self, types):
        """The session's configuration, assembled from the measured facts.

        Every field here is one the probe showed matters. ``response_modalities``
        is AUDIO and only AUDIO: the transcription family refuses that
        combination outright, which is why the capability table in
        ``app/gemini_pool.py`` gives a live transcription model no ``audio_out``
        and the pool can therefore never hand one to this code.
        """
        kwargs = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.system_instruction or None,
            "input_audio_transcription": types.AudioTranscriptionConfig(),
            "output_audio_transcription": types.AudioTranscriptionConfig(),
            # Automatic detection, and never the explicit signals: the provider
            # rejects ``activity_start``/``activity_end`` while this is on, and
            # its detector is the one deciding the turn anyway.
            "realtime_input_config": types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False
                )
            ),
        }
        if self.language:
            kwargs["speech_config"] = types.SpeechConfig(
                language_code=self.language,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.voice or "Puck"
                    )
                ),
            )
        if self.tools:
            kwargs["tools"] = [
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(**decl) for decl in self.tools
                ])
            ]
        if self.handle:
            # Only when resuming. A handle on a fresh session is meaningless, and
            # ``transparent`` resumption without a handle would let the provider
            # silently start a *new* conversation that looks continuous.
            kwargs["session_resumption"] = types.SessionResumptionConfig(
                handle=self.handle
            )
        return types.LiveConnectConfig(**kwargs)


def _call_tuple(call) -> dict:
    """One function call as plain data.

    Copied out of the SDK's object rather than kept as a reference, because the
    args are passed on to ``actions.parse`` and a plain dict there is what makes
    the action bridge testable without the SDK.
    """
    args = getattr(call, "args", None) or {}
    return {
        "id": str(getattr(call, "id", "") or ""),
        "name": str(getattr(call, "name", "") or ""),
        "args": dict(args) if isinstance(args, dict) else {},
    }


def _inline_audio(content) -> bytes:
    """The audio bytes out of a server content message, or ``b""``.

    Walks ``model_turn.parts`` looking for inline data, which is how a Live
    session delivers speech. Anything that is not inline audio — a text part, a
    thought part — is skipped rather than concatenated, because mixing them into
    the playback buffer would put non-audio into the call.
    """
    turn = getattr(content, "model_turn", None)
    if turn is None:
        return b""
    chunks: list[bytes] = []
    for part in getattr(turn, "parts", None) or ():
        inline = getattr(part, "inline_data", None)
        if inline is None:
            continue
        data = getattr(inline, "data", None)
        if isinstance(data, (bytes, bytearray)) and data:
            chunks.append(bytes(data))
    return b"".join(chunks)


def _load_sdk():
    """Import the provider SDK on demand.

    Lazily, so that the package imports — and its tests run — on a machine
    without the provider library, and so that a deployment that has the feature
    switched off never pays for the import. The failure is raised as this
    package's own error type so a caller has one taxonomy to catch.
    """
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # noqa: BLE001 - reported as a transport problem
        raise errors.ProviderError(
            f"the provider SDK is unavailable ({type(exc).__name__})",
            reason=errors.REASON_CONNECT_FAILED,
        ) from None
    return genai, types
