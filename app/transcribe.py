"""Speech to text: the fourth workload, and the only one with a right answer.

A transcription is a mechanical operation. It has one correct output, it is
cheap, it is cacheable, and a wrong one is a *different kind* of wrong from a
bad chat reply — which is why it is its own module with its own key, model,
rate window, daily cap, circuit breaker, counters and client, rather than a
function inside ``app/chat.py``.

Three reasons that separation is load-bearing rather than tidy:

* **Budgets.** A voice conversation spends two requests per turn — one to
  transcribe, one to reply. Sharing a window between them would mean a busy
  voice chat could silence the assistant entirely, and it would make "the
  transcript was wrong" indistinguishable from "the reply was wrong" in the
  counters.
* **Failure domains.** Transcription failing must degrade to "answer the text
  that was there, or say you could not hear it" without touching the reply
  path, and vice versa.
* **The brief's rule about ordinary voice messages.** Nothing here runs
  automatically. There is no handler that transcribes a group voice note on
  arrival, so a voice message in a group goes to the monitoring pipeline (where
  it is simply not a text lead) and never to acquisition, moderation or the
  assistant. Transcription happens only when something explicitly asks for it:
  an addressed voice message, or the transcription-only command.

What it will not do: it does not summarise, translate, interpret or answer. The
prompt asks for the words, and the words are what comes back. Anything else
would be a second, undeclared AI workload hiding inside this one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from . import config, db, gemini_pool

log = logging.getLogger("guardbot.transcribe")

# The instruction. Short, and specific about the two things a speech model gets
# wrong when left to its own devices: it answers the speaker instead of
# transcribing them, and it tidies the words into what it thinks they meant.
SYSTEM_INSTRUCTION = (
    "You are a speech-to-text function. You return the words that were spoken "
    "and nothing else.\n"
    "\n"
    "Rules:\n"
    "* Transcribe verbatim, in the language that was spoken. Do not translate.\n"
    "* Do not answer the speaker, do not summarise, do not comment, do not "
    "explain. If the speaker asked a question, your output is the question — "
    "not an answer to it.\n"
    "* Do not add punctuation the speaker did not imply, and do not correct "
    "grammar or word choice. Keep filler words.\n"
    "* If the audio contains no speech — music, noise, silence — return exactly "
    "the word NOSPEECH and nothing else.\n"
    "* If the speech is not intelligible enough to transcribe, return exactly "
    "the word UNINTELLIGIBLE and nothing else.\n"
    "* Never output anything except the transcription or one of those two "
    "markers."
)

# The two markers the prompt may return. They are matched case-insensitively and
# only when they are the *whole* answer, so a transcript that happens to contain
# the word "unintelligible" is not mistaken for the marker.
MARKER_NO_SPEECH = "NOSPEECH"
MARKER_UNINTELLIGIBLE = "UNINTELLIGIBLE"


@dataclass(frozen=True)
class Transcript:
    """One transcription, or the reason there wasn't one."""

    ok: bool = False
    text: str = ""
    skipped: str = ""
    error: str = ""
    model: str = ""
    # True when the audio held no speech at all, as opposed to failing. The
    # caller says something different for "I heard nothing" than for "I could
    # not listen", and those are genuinely different to a person.
    no_speech: bool = False

    def __bool__(self) -> bool:
        return self.ok


# ── State ─────────────────────────────────────────────────────────────────
_recent_calls: list[float] = []
_consecutive_failures = 0
_circuit_open_until = 0.0
_sdk_missing_logged = False
_client = None
_client_key = ""

stats = {"consulted": 0, "transcripts": 0, "malformed": 0, "errors": 0, "skipped": 0}


def reset_state() -> None:
    """Forget the rate window, the breaker and the cached client. For tests."""
    global _consecutive_failures, _circuit_open_until, _client, _client_key
    global _sdk_missing_logged
    _recent_calls.clear()
    _consecutive_failures = 0
    _circuit_open_until = 0.0
    _sdk_missing_logged = False
    _client = None
    _client_key = ""
    for key in stats:
        stats[key] = 0


def api_key() -> str:
    """Its own key when configured, the classifier's only when explicitly allowed."""
    if config.TRANSCRIBE_API_KEY:
        return config.TRANSCRIBE_API_KEY
    if config.TRANSCRIBE_ALLOW_SHARED_KEY:
        return config.GEMINI_API_KEY
    return ""


def shares_google_project() -> bool:
    return bool(
        not config.TRANSCRIBE_API_KEY
        and config.TRANSCRIBE_ALLOW_SHARED_KEY
        and config.GEMINI_API_KEY
    )


def is_enabled() -> bool:
    return bool(
        config.TRANSCRIBE_ENABLED
        and (api_key() or gemini_pool.has_accounts("transcribe"))
    )


def status() -> dict:
    """Safe to log. The key is never in here."""
    pool = gemini_pool.pool_for("transcribe")
    return {
        "enabled": bool(config.TRANSCRIBE_ENABLED),
        "configured": bool(api_key() or gemini_pool.has_accounts("transcribe")),
        "active": is_enabled(),
        "shares_google_project": shares_google_project(),
        "model": config.TRANSCRIBE_MODEL,
        "pool": pool.status() if pool is not None else None,
        "daily_limit": int(config.TRANSCRIBE_DAILY_LIMIT),
        "used_today": db.transcript_calls_today(),
        "max_seconds": float(config.TRANSCRIBE_MAX_SECONDS),
    }


def _rate_limited(now: float) -> bool:
    window = max(1.0, float(config.TRANSCRIBE_RATE_WINDOW))
    limit = max(1, int(config.TRANSCRIBE_RATE_LIMIT))
    cutoff = now - window
    while _recent_calls and _recent_calls[0] < cutoff:
        _recent_calls.pop(0)
    return len(_recent_calls) >= limit


def _circuit_open(now: float) -> bool:
    return now < _circuit_open_until


def _note_failure(now: float) -> None:
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures += 1
    threshold = max(1, int(config.TRANSCRIBE_CIRCUIT_FAILURES))
    if _consecutive_failures >= threshold:
        _circuit_open_until = now + max(0.0, float(config.TRANSCRIBE_CIRCUIT_SECONDS))
        log.warning(
            "[transcribe] circuit_open failures=%d cooldown=%ss",
            _consecutive_failures,
            int(config.TRANSCRIBE_CIRCUIT_SECONDS),
        )


def _note_success() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


MIN_DEADLINE_SECONDS = 10.0


def timeout_seconds() -> float:
    return max(MIN_DEADLINE_SECONDS, float(config.TRANSCRIBE_TIMEOUT_SECONDS))


class TranscribeUnavailable(Exception):
    """The model could not be asked. Never a statement about the audio."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


def _build_client():
    global _sdk_missing_logged
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # noqa: BLE001
        if not _sdk_missing_logged:
            _sdk_missing_logged = True
            log.warning("[transcribe] google-genai is not installed: %s", exc)
        raise TranscribeUnavailable("sdk_missing", str(exc)[:120]) from exc
    return (
        genai.Client(
            api_key=api_key(),
            http_options=types.HttpOptions(timeout=int(timeout_seconds() * 1000)),
        ),
        types,
    )


def _client_or_raise():
    global _client, _client_key
    if _client is not None and _client_key == api_key():
        return _client
    client, _types = _build_client()
    _client = client
    _client_key = api_key()
    return _client


def _generation_config(types):
    return types.GenerateContentConfig(
        # Temperature 0: this is transcription, and variance is error.
        temperature=0.0,
        max_output_tokens=1024,
        system_instruction=SYSTEM_INSTRUCTION,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _contents(data: bytes, mime_type: str, types) -> list:
    return [
        types.Part.from_bytes(data=data, mime_type=mime_type),
        "Transcribe this audio.",
    ]


async def _pooled_request(pool, data: bytes, mime_type: str) -> str:
    """One transcription, through the pool.

    The workload demands ``audio_in`` of every model it is offered. That is the
    requirement that must never be relaxed: a text-only model handed audio would
    not error, it would invent a plausible transcript, and an invented
    transcript is indistinguishable from a real one downstream.
    """
    try:
        raw = await gemini_pool.generate(
            pool,
            build_contents=lambda types: _contents(data, mime_type, types),
            build_config=_generation_config,
        )
    except gemini_pool.PoolUnavailable as exc:
        raise TranscribeUnavailable(exc.kind, exc.detail) from exc
    return raw or ""


async def _request(data: bytes, mime_type: str) -> str:
    """The single network seam. Tests replace exactly this."""
    pool = gemini_pool.pool_for("transcribe")
    if pool is not None and pool.enabled:
        return await _pooled_request(pool, data, mime_type)

    from google.genai import types

    client = _client_or_raise()
    config_ = _generation_config(types)

    async def _call():
        return await client.aio.models.generate_content(
            model=config.TRANSCRIBE_MODEL,
            contents=_contents(data, mime_type, types),
            config=config_,
        )

    response = await asyncio.wait_for(_call(), timeout=timeout_seconds())
    return getattr(response, "text", "") or ""


def _is_transient(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        marker in text
        for marker in ("429", "500", "502", "503", "504", "timeout", "deadline",
                       "unavailable", "resource_exhausted", "connection", "reset")
    )


_PERMANENT = frozenset({"sdk_missing", "empty_response"})

# The longest transcript we will pass on. A voice note that produced a wall of
# text is either a long recording or a model that decided to write an essay;
# either way it is truncated before it reaches anything else.
MAX_TRANSCRIPT_CHARS = 4000


def _clean(raw: str) -> str:
    """Strip control and bidi characters, collapse whitespace, bound the length.

    The transcript becomes the *user's* turn in a conversation, so it is treated
    exactly like text the user typed: anything invisible is removed, because an
    invisible reordering character in a transcript would let a spoken message
    read as something other than what was said.
    """
    text = raw or ""
    text = "".join(
        ch
        for ch in text
        if ch in "\n\t" or (ord(ch) >= 0x20 and not (0x202A <= ord(ch) <= 0x202E)
                            and not (0x2066 <= ord(ch) <= 0x2069) and ord(ch) != 0xFEFF)
    )
    text = " ".join(text.split())
    return text[:MAX_TRANSCRIPT_CHARS]


def _skipped(reason: str) -> Transcript:
    stats["skipped"] += 1
    db.record_transcript_skip()
    log.info("[transcribe] skipped reason=%s", reason)
    return Transcript(skipped=reason, model=config.TRANSCRIBE_MODEL)


async def transcribe(
    data: bytes, mime_type: str, *, duration: float | None = None
) -> Transcript:
    """Turn audio into text. Never raises.

    ``duration`` is Telegram's own metadata and is used only to refuse a clip
    that is too long *before* downloading it — a caller that has already fetched
    the bytes may pass None.
    """
    if not config.TRANSCRIBE_ENABLED:
        return _skipped("disabled")
    if not (api_key() or gemini_pool.has_accounts("transcribe")):
        return Transcript(skipped="no_key", model=config.TRANSCRIBE_MODEL)

    if duration is not None and float(duration) > float(config.TRANSCRIBE_MAX_SECONDS):
        return _skipped("too_long")

    if not data:
        return Transcript(skipped="empty", model=config.TRANSCRIBE_MODEL)

    if len(data) > int(float(config.TRANSCRIBE_MAX_MB) * 1024 * 1024):
        return _skipped("too_large")

    now = time.monotonic()
    if _circuit_open(now):
        return _skipped("circuit_open")
    if _rate_limited(now):
        return _skipped("rate_limit")
    if db.transcript_calls_today() >= max(1, int(config.TRANSCRIBE_DAILY_LIMIT)):
        return _skipped("daily_cap")

    # The pool owns retries when it is in use. A voice note is the most
    # expensive call in the bot, so re-walking an exhausted pool here would be
    # the worst place to double the budget.
    pooled = gemini_pool.has_accounts("transcribe")
    attempts = 1 if pooled else max(0, int(config.TRANSCRIBE_MAX_RETRIES)) + 1
    backoff = max(0.0, float(config.TRANSCRIBE_BACKOFF_SECONDS))
    last: TranscribeUnavailable | None = None
    # The one extra call an *empty* answer may spend. Empty is a separate fault
    # from a transport error: the request succeeded and the model said nothing,
    # which is almost always a transient hiccup rather than a clip with no speech
    # in it, so it gets one re-ask of its own rather than being declared
    # unreadable on the spot.
    retried_empty = False

    for attempt in range(attempts):
        _recent_calls.append(time.monotonic())
        try:
            raw = await _request(data, mime_type or "audio/ogg")
        except asyncio.CancelledError:
            raise
        except TranscribeUnavailable as exc:
            last = exc
            db.record_transcript_attempt("errors")
            if exc.kind in _PERMANENT:
                break
        except (asyncio.TimeoutError, TimeoutError):
            last = TranscribeUnavailable("timeout")
            db.record_transcript_attempt("errors")
        except BaseException as exc:  # noqa: BLE001
            last = TranscribeUnavailable(type(exc).__name__, str(exc)[:160])
            db.record_transcript_attempt("errors")
            if not _is_transient(exc):
                break
        else:
            text = _clean(raw)
            if not text and not retried_empty:
                # An empty answer is usually a transient provider hiccup, not a
                # clip with nothing in it. Ask once more before declaring the
                # file unreadable; a second empty answer is a failure, exactly as
                # before. The re-ask is counted first: it is a second real
                # request, and the daily ceiling is computed from the same
                # counter, so an uncounted retry would spend quota the ceiling
                # cannot see.
                retried_empty = True
                _recent_calls.append(time.monotonic())
                db.record_transcript_attempt("errors")
                try:
                    raw = await _request(data, mime_type or "audio/ogg")
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    log.warning(
                        "[transcribe] empty-retry failed: %s", type(exc).__name__
                    )
                    raw = ""
                text = _clean(raw)
            if not text:
                # An empty answer is a failure to transcribe, not a silent clip.
                stats["errors"] += 1
                db.record_transcript_attempt("errors")
                _note_failure(time.monotonic())
                return Transcript(error="empty_response", model=config.TRANSCRIBE_MODEL)

            marker = text.strip().upper().strip(".!")
            if marker == MARKER_NO_SPEECH:
                stats["consulted"] += 1
                db.record_transcript_attempt("transcripts")
                _note_success()
                log.info("[transcribe] no speech detected")
                return Transcript(
                    ok=False, no_speech=True, model=config.TRANSCRIBE_MODEL
                )
            if marker == MARKER_UNINTELLIGIBLE:
                stats["consulted"] += 1
                stats["malformed"] += 1
                db.record_transcript_attempt("malformed")
                _note_success()
                log.info("[transcribe] unintelligible")
                return Transcript(
                    error="unintelligible", model=config.TRANSCRIBE_MODEL
                )

            stats["consulted"] += 1
            stats["transcripts"] += 1
            db.record_transcript_attempt("transcripts")
            _note_success()
            # The transcript itself is never logged. It is the content of
            # somebody's voice message, and a log line is a copy of it.
            log.info("[transcribe] ok chars=%d", len(text))
            return Transcript(ok=True, text=text, model=config.TRANSCRIBE_MODEL)

        if attempt + 1 < attempts:
            await asyncio.sleep(backoff * (2**attempt))

    stats["errors"] += 1
    _note_failure(time.monotonic())
    log.warning(
        "[transcribe] error kind=%s failures=%d",
        last.kind if last else "unknown",
        _consecutive_failures,
    )
    return Transcript(
        error=last.kind if last else "unknown", model=config.TRANSCRIBE_MODEL
    )


async def transcribe_ref(ref, *, download) -> Transcript:
    """Convenience: fetch a ``media.MediaRef`` and transcribe it.

    Refuses anything that is not transcribable rather than attempting it, so a
    caller that hands over a photo gets a clear "not audio" instead of a wasted
    request against the quota.
    """
    result = await _transcribe_ref(ref, download=download)
    # One record per voice note, whatever it became: the words, or the reason
    # there were none. This is the seam every caller shares, so recording it
    # here means no caller has to remember to.
    _observe_transcript(ref, result)
    return result


def _observe_transcript(ref, result) -> None:
    """Record one transcription, as evidence. Never raises."""
    try:
        from . import observe

        if not observe.started():
            return
        observe.emit(
            observe.schema.KIND_VOICE,
            event="transcribe",
            ok=bool(result.ok),
            text=result.text if result.ok else "",
            error="" if result.ok else (result.error or result.skipped or "failed"),
            data={
                "duration": float(getattr(ref, "duration", 0) or 0),
                "file_size": int(getattr(ref, "file_size", 0) or 0),
                "mime": str(getattr(ref, "effective_mime", "") or ""),
                "model": result.model,
                "no_speech": bool(getattr(result, "no_speech", False)),
            },
        )
    except Exception:  # noqa: BLE001
        pass


async def _transcribe_ref(ref, *, download) -> Transcript:
    if ref is None:
        return Transcript(skipped="no_media", model=config.TRANSCRIBE_MODEL)
    if not getattr(ref, "is_transcribable", False):
        return Transcript(skipped="not_audio", model=config.TRANSCRIBE_MODEL)
    if ref.duration is not None and float(ref.duration) > float(
        config.TRANSCRIBE_MAX_SECONDS
    ):
        return _skipped("too_long")
    if ref.file_size and ref.file_size > int(
        float(config.TRANSCRIBE_MAX_MB) * 1024 * 1024
    ):
        return _skipped("too_large")
    try:
        data = await download(ref.file_id)
    except Exception as e:  # noqa: BLE001
        log.warning("[transcribe] download failed: %s", e)
        return Transcript(error="download_failed", model=config.TRANSCRIBE_MODEL)
    return await transcribe(data, ref.effective_mime, duration=ref.duration)


__all__ = [
    "MARKER_NO_SPEECH",
    "MARKER_UNINTELLIGIBLE",
    "Transcript",
    "is_enabled",
    "reset_state",
    "shares_google_project",
    "status",
    "transcribe",
    "transcribe_ref",
    "timeout_seconds",
]
