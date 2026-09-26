"""Voice Context: a Telegram voice note answered as a spoken turn.

What this module is
-------------------
A person sends a voice message that is addressed to Nexus. The **existing**
pipeline understands it first — download, transcription, sender identity, reply
and target resolution, room and awareness context, memory, state, the date, the
web result if one was fetched — and only then, with the server's assembled
context in hand, the turn is handed to the provider's Live API and the answer
comes back as speech. The reply is sent as a Telegram **voice** message that is a
direct reply to the incoming voice note.

The distinction this module exists to hold, in the brief's own terms:

    the same Nexus, with the same context  ≠  a second voice chatbot

The tempting design is to open a live session and let the model talk. That design
is refused, and the reason is the whole feature. A live session on its own has no
idea who is speaking, what they replied to, what the room is doing, what it
remembers about them, or what the last message said — and giving it those things
by *asking the model* would make identity, memory and authority model output.
Here they are assembled server-side, exactly as they are for a text turn, and the
Live session is only a consumer of a context that is already decided. There are
no tools on this session at all: a voice note that contains an instruction is
answered with words like any other message, and every action stays where it
always was, in ``app/admin_service.py``.

How it differs from ordinary transcription
------------------------------------------
A voice message has always been transcribed and answered in text — that path is
untouched and is what runs when this is off, when there is no credential, or when
the turn fails. Voice Context is the *other* answer to the same input: the
transcript still grounds the words, and the audio carries how they were said, so
the model answers the person's own voice rather than a rendering of it. Both
paths share the one download (``main._prepare_conversation_media`` hands the
bytes it already fetched to this module), and both share one conversation lock,
so a voice turn and a text turn in the same room cannot read the same history and
answer each other's context.

Why it is a workload of its own
-------------------------------
It is the same provider capability as the live call and, by default, the same
credential — and it is still its own pool workload. A call is one connection held
for minutes and is rationed by how many a day an account may open; a voice note
is a short, request-shaped turn sent by ordinary members and is rationed by how
many an account may answer. One budget would let a busy room of voice notes spend
the day a call was waiting on, and one breaker would stop both on one bad
afternoon. The credential may be shared; the accounting is not.

What it deliberately does not own
---------------------------------
* **Context.** The blocks a turn is given are the ones ``app/main.py`` already
  composes for a text turn. This module receives that text and passes it on; it
  builds none of it and re-decides none of it.
* **Authority.** Nothing here is imported by ``app/admin_service.py`` or
  ``app/rbac.py``. A switch flip is a typed request that goes to the service like
  every other one; this module's ``set_running`` has no permission check for the
  same reason ``awareness.set_running`` has none — there is one authority model
  and it is not here.
* **The provider's shape.** The connection is ``voice_live.GeminiLiveTransport``
  and the turn is ``voice_live.turn.LiveTurn``; this module leases a credential
  from the pool, wires the two together, and reads the result. It knows no SDK
  type.

Failure is a fallback, never a crash
------------------------------------
Every failure — no credential, a refused configuration, a dropped socket, a
provider that never speaks, audio that cannot be decoded or encoded — returns an
``Answer`` whose ``ok`` is False. The caller then answers the message on the text
path, so a person always gets a reply; the only thing they lose is that it is
typed rather than spoken. The turn closes its transport on every path, so no
session is left open.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from . import chat, config, db, gemini_pool, nexus
from .voice_live import audio as voice_audio
from .voice_live import gemini_live
from .voice_live import turn as turn_module

log = logging.getLogger("guardbot.voicecontext")

# The pool key this module owns. Named once and asked for by that name
# everywhere, so a typo cannot make it read another workload's allowance.
WORKLOAD = "voice_context"

# The two directions, in this module's own vocabulary. Deliberately not
# ``nexus.ONLINE``/``OFFLINE``: those name the assistant's state, and reusing the
# constants would invite a caller to move the wrong switch with the right words.
ON = "on"
OFF = "off"

# Why a turn produced no answer. Machine keys, like ``turn.REASON_*``, and for
# the same reason: the caller branches on them and they are never rendered.
REASON_DISABLED = "disabled"
REASON_UNAVAILABLE = "unavailable"
REASON_NOTHING = "nothing_to_send"
REASON_NO_AUDIO = "no_audio"

# ── The switch ────────────────────────────────────────────────────────────
# The fourth switch beside Nexus, awareness and search, and the same shape: a
# persisted row, a cached read, and a config master. ``configured`` is what the
# deployment asks for; ``running`` is what the owner last said; ``enabled`` is
# the answer both have to agree on. There is deliberately **no permission check**
# in ``set_running`` — the authority for every administrative act lives in
# exactly one place, ``app/admin_service.execute``, and a second check here would
# be a second authority model.
_running: bool | None = None

# The turn limiter. Created lazily inside a running loop, because a semaphore
# built at import time would belong to whichever loop happened to exist then.
_gate: asyncio.Semaphore | None = None

# In-process counters, for the log and for ``status``. Not authoritative — the
# pool's own persisted counters are — this is the view since the last start.
stats: dict = {
    "turns": 0,
    "spoken": 0,
    "failed": 0,
    "skipped": 0,
    "unavailable": 0,
    "unencoded": 0,
}


def reset_state() -> None:
    """Forget the cached switch and the turn limiter. For tests."""
    global _running, _gate
    _running = None
    _gate = None
    for key in stats:
        stats[key] = 0


def configured() -> bool:
    """What the configuration asks for. Never what the owner last said."""
    return bool(config.VOICE_CONTEXT_ENABLED)


def running() -> bool:
    """Whether the owner has left Voice Context switched on.

    Read from the database once and cached, because it is asked on the path of
    every addressed voice message. The default when nothing has ever been
    written is **on**, so a deployment that has never used the switch behaves as
    its configuration asks — which is also why ``None`` is not treated as "off".
    """
    global _running
    if _running is None:
        try:
            row = db.voice_context_control_get()
        except Exception:  # noqa: BLE001 - a switch must never fail a message
            log.exception("could not read the voice context switch")
            return True
        _running = True if row is None else bool(row["enabled"])
    return _running


def set_running(enabled_state: bool, *, actor_id: int = 0, reason: str = "") -> bool:
    """Flip the switch, persist it, and return the state it is now in."""
    global _running
    row = db.voice_context_control_set(
        enabled_state, actor_id=actor_id, reason=reason
    )
    _running = bool(row["enabled"])
    return _running


def reset_switch() -> None:
    """Forget the cached switch, so the next read comes from the database."""
    global _running
    _running = None


def enabled() -> bool:
    """Whether the workload may run at all: config *and* the owner's switch."""
    return configured() and running()


def available() -> bool:
    """Whether a voice turn could actually be made: switched on, with a key.

    The gate the conversational path asks before it takes the voice road. It is
    about *presence* of a credential rather than its validity — whether the key
    works is answered by using it — and it makes no network call, so asking it
    costs nothing on the path of a message that will not use it.
    """
    return bool(enabled() and gemini_pool.has_accounts(WORKLOAD))


def state_label() -> str:
    """The label a confirmation prints, read from the live gate."""
    return config.VOICE_CONTEXT_ON_LABEL if enabled() else config.VOICE_CONTEXT_OFF_LABEL


# ── Being named, and the spoken command ───────────────────────────────────
def named(text: str) -> bool:
    """Whether the message names the *Voice Context layer*.

    Whole-word and case-insensitive, matching ``awareness.named`` and
    ``web_search.named``: the functions are asked about the same sentence and
    have to agree about what a word is. It grants nothing — the speaker is
    checked against the owner id separately.

    The name list deliberately excludes the bare word «voice», which belongs to
    the live call: a name two layers answer to is a name that moves the wrong
    switch.
    """
    if not text:
        return False
    for name in config.VOICE_CONTEXT_NAMES:
        if not name:
            continue
        try:
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
                return True
        except re.error:  # pragma: no cover - re.escape makes this unreachable
            continue
    return False


def command_from(text: str) -> str | None:
    """The direction a Voice Context phrase asks for, or ``None``.

    Purely textual, like ``nexus.command_from``: this says what the words ask
    for, never whether the person saying them may have it. Two vocabularies are
    consulted, and the order is the safety property:

    * the layer's **own** phrases first — «باز», «بسته» — which are unambiguous
      *because* the layer is named, and which are deliberately not in the shared
      list: «باز» is one of the commonest words in Persian, and a shared
      vocabulary that read it as "turn on" would make «نکسوس باز خراب شد» a
      command.
    * then the shared switch vocabulary, through ``nexus.command_from`` with
      ``names_layer=True`` — which is exactly the fact that makes «روشن»,
      «خاموش» and «فعال شو» unambiguous here.

    The layer has to be named for either vocabulary to be read, and the check is
    inside this function rather than left to the caller: every phrase here —
    the layer's «باز»/«بسته» and the shared «بیا پایین»/«راه بنداز» — is only a
    command because the sentence says which switch it means, and a reader that
    trusted a caller to have asked that would be one edit away from moving this
    switch on a sentence about something else.

    A negation anywhere cancels the whole thing, and a message that asks for
    both directions resolves to nothing rather than to a guess: this switch
    decides whether a person is answered in their own voice, and guessing at a
    contradiction is how that ends up on when it was meant off.
    """
    low = (text or "").lower()
    if not low:
        return None
    if nexus.negated(low):
        return None
    # Not named, not this layer's command. The shared vocabulary is asked with
    # ``names_layer=False`` below for the same reason — the ambiguous half of it
    # is only about a layer when the message names one.
    layer_named = named(text)
    if layer_named:
        wants_on = _hit(low, config.VOICE_CONTEXT_ON_PHRASES)
        wants_off = _hit(low, config.VOICE_CONTEXT_OFF_PHRASES)
        if wants_on != wants_off:
            return ON if wants_on else OFF
        if wants_on and wants_off:
            # Both: a contradiction, and ``nexus.command_from`` refuses it too.
            return None
    shared = nexus.command_from(text, names_layer=layer_named)
    if shared == nexus.ONLINE:
        return ON
    if shared == nexus.OFFLINE:
        return OFF
    return None


def _hit(low: str, phrases) -> bool:
    """Whole-word match of any phrase, using the one matcher ``nexus`` exposes."""
    return any(nexus.mentions(low, phrase) for phrase in (phrases or ()))


# ── What the spoken turn is told ──────────────────────────────────────────
# Appended to ``chat.SYSTEM_INSTRUCTION`` — the same persona, in the same system
# instruction, not a second personality — for the one reason it is needed: the
# persona was written for text, and a few of its rules read differently when the
# answer is heard once and cannot be re-read. It changes the *medium*, never the
# voice, the warmth, the length policy or the rules about facts.
SPOKEN_ADDENDUM = (
    "\n"
    "── For this turn, your answer is spoken aloud ──\n"
    "This answer is turned into speech and sent as a voice message, so it will "
    "be heard once and cannot be re-read. Everything above still holds — the "
    "same voice, the same warmth, the same length policy — and these points are "
    "about the medium:\n"
    "* Write what a person would say out loud, in the language they spoke. No "
    "headings, no numbered steps, no bullet points, no Markdown, no emoji, no "
    "code, no tables and no links. Ordinary sentences, in order.\n"
    "* Never read out a numeric id, a username, a file name or a raw identifier. "
    "Say who or what you mean in words; an id only means something on a screen.\n"
    "* Do not describe the medium and do not narrate yourself. Never say that "
    "you are sending a voice message, that you are answering by voice, or that "
    "something cannot be shown — just say the answer.\n"
    "* The length still follows the request, exactly as above: a greeting gets a "
    "few words, a question gets a complete answer, and an explicit request to "
    "explain fully gets the full explanation, out loud. Do not shorten a real "
    "answer because it is spoken, and do not pad a short one.\n"
    "* If something is genuinely better shown than said, give the shortest "
    "spoken version that is still useful instead of reading out what cannot be "
    "spoken — and never invent a link or a figure to fill the gap.\n"
)


def instruction() -> str:
    """The system instruction for one spoken turn.

    Built from the conversational persona rather than beside it, so the two can
    never drift: Voice Context is the same Nexus answering the same message, and
    the only thing this adds is what changes when the answer is heard.
    """
    return chat.SYSTEM_INSTRUCTION + SPOKEN_ADDENDUM


# ── One turn ──────────────────────────────────────────────────────────────
@dataclass
class Answer:
    """One voice turn's outcome. Plain data, safe to log.

    ``voice`` is OGG/Opus bytes ready for Telegram's ``sendVoice`` and ``said``
    is the model's own transcript of the speech, which is what the caller falls
    back to when the audio cannot be delivered. Neither is ever logged here.
    """

    ok: bool = False
    voice: bytes | None = None
    said: str = ""
    heard: str = ""
    reason: str = ""
    detail: str = ""
    attempts: int = 0
    capped: bool = False
    # How long the *speech* is, in seconds. Carried rather than derived, because
    # ``voice`` is already OGG/Opus and the length of the speech is only known
    # before it is encoded. It is what the log line and the caller report.
    seconds: float = 0.0
    timing: dict = field(default_factory=dict)

    def describe(self) -> dict:
        """A safe summary. No audio, no transcript, no credential."""
        return {
            "ok": self.ok,
            "bytes": len(self.voice or b""),
            "seconds": round(self.seconds, 2),
            "said_chars": len(self.said),
            "reason": self.reason,
            "attempts": self.attempts,
            "capped": self.capped,
            "timing": dict(self.timing),
        }


def _semaphore() -> asyncio.Semaphore:
    """The turn limiter, created on first use inside the running loop."""
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(
            max(1, int(config.VOICE_CONTEXT_MAX_CONCURRENCY))
        )
    return _gate


def _lease() -> tuple[str, str]:
    """One model and one credential for this turn, from the pool.

    The same shape ``app/voice_live/session.py`` uses for a call, and for the
    same reason: the pool is asked rather than the environment read directly, so
    the allowance accounting and the owner's key dashboard both see this turn.
    One turn is one request against ``voice_context``; the turn's *length* is
    bounded by ``VOICE_CONTEXT_TURN_TIMEOUT_SECONDS`` rather than by the pool.

    Raises ``_Unavailable`` when the pool cannot serve, which the caller turns
    into a fallback rather than a failure.
    """
    pool = gemini_pool.pool_for(WORKLOAD)
    if pool is None or not pool.enabled:
        raise _Unavailable("no voice_context pool is configured")
    now = time.time()
    accounts = pool.ordered_accounts(now)
    if not accounts:
        raise _Unavailable("every voice_context credential is unusable")
    account = accounts[0]
    models = pool.models_for(account, now)
    if not models:
        raise _Unavailable("no live model is available to this credential")
    account.note_request(now)
    return models[0], account.key


class _Unavailable(Exception):
    """The pool could not serve a turn. Never rendered; only the reason is used."""


def _note_success() -> None:
    """Tell the pool this turn worked. Best effort, never fatal.

    Mirrors ``app/voice_live/session.py``: the pool's success note is what
    rotates the credential and clears a cooldown, and a turn that is already
    answered must not be taken down because a counter could not be written.
    """
    try:
        pool = gemini_pool.pool_for(WORKLOAD)
        if pool is None:
            return
        accounts = pool.ordered_accounts(time.time())
        if accounts:
            accounts[0].note_success(time.time())
    except Exception:  # noqa: BLE001 - never the reason a turn fails
        log.debug("[voicecontext] pool success note failed; ignoring", exc_info=True)


def _transport_factory(model: str, key: str):
    """A callable returning a *fresh* transport, which is what a retry needs.

    A provider session's state is the conversation, so a second attempt has to
    open a new session — a factory rather than a transport is what makes that
    true instead of a rule the retry loop has to remember. There are no tools:
    a voice turn cannot ask for an action, so the authority model is not
    weakened here, it is simply absent.
    """

    def build():
        return gemini_live.GeminiLiveTransport(
            model,
            key,
            language=config.VOICE_CONTEXT_LANGUAGE,
            voice=config.VOICE_CONTEXT_VOICE,
            system_instruction=instruction(),
            tools=None,
            timeout=config.VOICE_CONTEXT_CONNECT_TIMEOUT_SECONDS,
        )

    return build


async def answer(
    *,
    context: str,
    transcript: str = "",
    audio: bytes = b"",
    chat_id: int = 0,
    user_id: int = 0,
) -> Answer:
    """Answer one voice note with one spoken turn, or say why it could not.

    ``context`` is the server's assembled record for this message — the same
    text a text turn puts in its system instruction — and ``transcript`` is the
    server's own reading of the voice note. ``audio`` is the note's own bytes as
    Telegram delivered them (OGG/Opus); it is decoded here and the model hears
    the person's voice rather than a rendering of it. With
    ``VOICE_CONTEXT_SEND_AUDIO`` off, the turn is text-only against the same
    context and the block itself is the message.

    Never raises except ``CancelledError``, which is re-raised so a shutdown is
    not swallowed. Every other failure comes back as an ``Answer`` whose ``ok``
    is False, because the caller's next move — answer on the text path — is a
    branch and not an exception.
    """
    started = time.monotonic()
    if not available():
        stats["skipped"] += 1
        return Answer(reason=REASON_DISABLED)

    text = (transcript or "").strip()
    pcm = b""
    decode_ms = 0.0
    if config.VOICE_CONTEXT_SEND_AUDIO and audio:
        # ffmpeg is a subprocess, so it runs off the event loop. A clip that
        # cannot be decoded is not a failure: the transcript still closes the
        # turn, which is why the decode result is allowed to be empty.
        mark = time.monotonic()
        try:
            pcm = await asyncio.to_thread(voice_audio.decode_to_pcm16, audio)
        except Exception:  # noqa: BLE001 - an undecodable clip falls back to text
            log.warning("[voicecontext] could not decode the voice note")
            pcm = b""
        decode_ms = (time.monotonic() - mark) * 1000.0

    if not context and not text and not pcm:
        stats["skipped"] += 1
        return Answer(reason=REASON_NOTHING)

    try:
        model, key = _lease()
    except _Unavailable as exc:
        stats["unavailable"] += 1
        log.info("[voicecontext] no credential: %s", exc)
        return Answer(reason=REASON_UNAVAILABLE, detail=str(exc)[:120])

    live = turn_module.LiveTurn(
        transport_factory=_transport_factory(model, key),
        context=context,
        transcript=text,
        send_audio=bool(config.VOICE_CONTEXT_SEND_AUDIO),
        silence_ms=int(config.VOICE_CONTEXT_SILENCE_MS),
        turn_timeout=float(config.VOICE_CONTEXT_TURN_TIMEOUT_SECONDS),
        max_reply_seconds=float(config.VOICE_CONTEXT_MAX_REPLY_SECONDS),
        max_attempts=int(config.VOICE_CONTEXT_MAX_ATTEMPTS),
        retry_backoff=float(config.VOICE_CONTEXT_RETRY_BACKOFF_SECONDS),
    )
    # The limiter bounds how many provider connections one busy moment may hold.
    # The text queue's own gate is separate and untouched: a voice turn never
    # waits on it and never holds it.
    async with _semaphore():
        result = await live.run(pcm)
    gemini_ms = (time.monotonic() - started) * 1000.0

    if not result.ok:
        stats["failed"] += 1
        log.info(
            "[voicecontext] turn failed chat=%s user=%s reason=%s detail=%s "
            "attempts=%d decode_ms=%.0f gemini_ms=%.0f",
            chat_id,
            user_id,
            result.reason or "unknown",
            (result.detail or "-")[:80],
            result.attempts,
            decode_ms,
            gemini_ms,
        )
        return Answer(
            reason=result.reason or "unknown",
            detail=result.detail,
            attempts=result.attempts,
            said=result.said,
            heard=result.heard,
            timing=dict(result.timing),
        )

    _note_success()
    stats["turns"] += 1
    timing = dict(result.timing)
    timing["decode_ms"] = round(decode_ms, 1)
    timing["gemini_ms"] = round(gemini_ms, 1)

    mark = time.monotonic()
    try:
        ogg = await asyncio.to_thread(voice_audio.encode_pcm24_to_ogg, result.audio)
    except Exception:  # noqa: BLE001 - the transcript is still the answer
        log.warning("[voicecontext] could not encode the spoken answer")
        ogg = None
    timing["encode_ms"] = round((time.monotonic() - mark) * 1000.0, 1)
    timing["total_ms"] = round((time.monotonic() - started) * 1000.0, 1)

    if not ogg:
        # The speech could not be packaged. The words are still the answer, so
        # they are handed back for the caller to send as text rather than lost.
        stats["unencoded"] += 1
        log.info(
            "[voicecontext] no audio to send chat=%s user=%s said_chars=%d",
            chat_id,
            user_id,
            len(result.said),
        )
        return Answer(
            ok=bool(result.said),
            said=result.said,
            heard=result.heard,
            reason=REASON_NO_AUDIO,
            capped=result.capped,
            attempts=result.attempts,
            seconds=result.seconds,
            timing=timing,
        )

    stats["spoken"] += 1
    log.info(
        "[voicecontext] spoke chat=%s user=%s bytes=%d seconds=%.1f capped=%s "
        "attempts=%d decode_ms=%.0f connect_ms=%.0f send_ms=%.0f "
        "first_audio_ms=%.0f reply_ms=%.0f encode_ms=%.0f total_ms=%.0f",
        chat_id,
        user_id,
        len(ogg),
        result.seconds,
        result.capped,
        result.attempts,
        decode_ms,
        timing.get("connect_ms", 0.0),
        timing.get("send_ms", 0.0),
        timing.get("first_audio_ms", 0.0),
        timing.get("reply_ms", 0.0),
        timing["encode_ms"],
        timing["total_ms"],
    )
    return Answer(
        ok=True,
        voice=ogg,
        said=result.said,
        heard=result.heard,
        capped=result.capped,
        attempts=result.attempts,
        seconds=result.seconds,
        timing=timing,
    )


def accepts(ref) -> bool:
    """Whether a media ref is within this workload's input bounds.

    The same shape ``transcribe.transcribe_ref`` uses, with this workload's own
    numbers: this path spends two provider calls per message rather than one, so
    the point at which it stops being worth answering is its own decision. A ref
    outside the bounds is not refused with a sentence — it simply takes the
    ordinary text path.
    """
    if ref is None or not getattr(ref, "is_transcribable", False):
        return False
    duration = getattr(ref, "duration", None)
    if duration is not None and float(duration) > float(config.VOICE_CONTEXT_MAX_SECONDS):
        return False
    size = int(getattr(ref, "file_size", 0) or 0)
    if size and size > int(float(config.VOICE_CONTEXT_MAX_MB) * 1024 * 1024):
        return False
    return True


# ── Status ────────────────────────────────────────────────────────────────
def status() -> dict:
    """A description safe to log or show an operator. No key, no transcript."""
    pool = gemini_pool.pool_for(WORKLOAD)
    return {
        "enabled": enabled(),
        "configured": configured(),
        "switch_on": running(),
        "active": available(),
        "model": config.VOICE_CONTEXT_MODEL,
        "language": config.VOICE_CONTEXT_LANGUAGE,
        "voice": config.VOICE_CONTEXT_VOICE,
        "send_audio": bool(config.VOICE_CONTEXT_SEND_AUDIO),
        "pool": pool.status() if pool is not None else None,
        "daily_limit": int(config.VOICE_CONTEXT_DAILY_LIMIT),
        "daily_remaining": pool.daily_remaining() if pool is not None else 0,
        "max_concurrency": max(1, int(config.VOICE_CONTEXT_MAX_CONCURRENCY)),
        "max_seconds": float(config.VOICE_CONTEXT_MAX_SECONDS),
        "max_reply_seconds": float(config.VOICE_CONTEXT_MAX_REPLY_SECONDS),
        "counters": dict(stats),
    }


__all__ = [
    "OFF",
    "ON",
    "REASON_DISABLED",
    "REASON_NO_AUDIO",
    "REASON_NOTHING",
    "REASON_UNAVAILABLE",
    "SPOKEN_ADDENDUM",
    "WORKLOAD",
    "Answer",
    "accepts",
    "answer",
    "available",
    "command_from",
    "configured",
    "enabled",
    "instruction",
    "named",
    "reset_state",
    "reset_switch",
    "running",
    "set_running",
    "state_label",
    "status",
]
