"""The speech-to-text workload: its own key, its own brakes, its own failures."""
import asyncio

import pytest

from app import config, db, transcribe


@pytest.fixture(autouse=True)
def tr_env(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "test-transcribe-key")
    monkeypatch.setattr(config, "TRANSCRIBE_ALLOW_SHARED_KEY", False)
    monkeypatch.setattr(config, "TRANSCRIBE_RATE_LIMIT", 100)
    monkeypatch.setattr(config, "TRANSCRIBE_DAILY_LIMIT", 1000)
    monkeypatch.setattr(config, "TRANSCRIBE_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "TRANSCRIBE_MAX_SECONDS", 300.0)
    transcribe.reset_state()
    db.init()
    yield
    transcribe.reset_state()


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, data, mime_type):
        self.calls.append((data, mime_type))
        if not self.responses:
            raise AssertionError("more calls than responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def count(self):
        return len(self.calls)


def install(monkeypatch, *responses):
    rec = Recorder(*responses)
    monkeypatch.setattr(transcribe, "_request", rec)
    return rec


def run(data=b"audio-bytes", mime="audio/ogg", duration=None):
    return asyncio.run(transcribe.transcribe(data, mime, duration=duration))


# ── Enablement ────────────────────────────────────────────────────────────
def test_without_its_own_key_it_does_nothing(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "")
    rec = install(monkeypatch)

    result = run()

    assert result.ok is False
    assert result.skipped == "no_key"
    assert rec.count == 0


def test_the_shared_key_is_an_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "")
    monkeypatch.setattr(config, "TRANSCRIBE_ALLOW_SHARED_KEY", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "classifier-key")

    assert transcribe.api_key() == "classifier-key"
    assert transcribe.shares_google_project() is True


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_ENABLED", False)
    rec = install(monkeypatch)

    assert run().skipped == "disabled"
    assert rec.count == 0


def test_status_never_contains_the_key():
    assert "test-transcribe-key" not in repr(transcribe.status())


# ── The transcript ────────────────────────────────────────────────────────
def test_a_transcript_comes_back_as_text(monkeypatch):
    install(monkeypatch, "سلام، حالت چطوره؟")

    result = run()

    assert result.ok is True
    assert result.text == "سلام، حالت چطوره؟"


def test_the_marker_for_silence_is_reported_as_no_speech(monkeypatch):
    """`I heard nothing` is a different sentence from `I could not listen`."""
    install(monkeypatch, "NOSPEECH")

    result = run()

    assert result.ok is False
    assert result.no_speech is True
    assert result.error == ""


def test_the_marker_is_matched_case_insensitively_and_alone(monkeypatch):
    install(monkeypatch, "  nospeech.  ")

    assert run().no_speech is True


def test_a_transcript_containing_the_marker_word_is_not_a_marker(monkeypatch):
    """Only the whole answer counts, so ordinary speech is never swallowed."""
    install(monkeypatch, "اون گفت unintelligible ولی من نفهمیدم چی گفت")

    result = run()

    assert result.ok is True


def test_an_unintelligible_marker_is_an_error_not_silence(monkeypatch):
    install(monkeypatch, "UNINTELLIGIBLE")

    result = run()

    assert result.ok is False
    assert result.no_speech is False
    assert result.error == "unintelligible"


def test_an_empty_answer_is_a_failure(monkeypatch):
    install(monkeypatch, "")

    result = run()

    assert result.ok is False
    assert result.error == "empty_response"


def test_control_and_bidi_characters_are_stripped(monkeypatch):
    """A transcript becomes the user's turn, so it is treated like typed text."""
    install(monkeypatch, "سلام\u202edlrow\u2066 دنیا\x07")

    result = run()

    assert "\u202e" not in result.text
    assert "\u2066" not in result.text
    assert "\x07" not in result.text


def test_the_transcript_is_bounded(monkeypatch):
    install(monkeypatch, "کلمه " * 5000)

    assert len(run().text) <= transcribe.MAX_TRANSCRIPT_CHARS


# ── Refusals before the call ──────────────────────────────────────────────
def test_a_clip_that_is_too_long_is_refused(monkeypatch):
    rec = install(monkeypatch, "x")

    result = run(duration=9999)

    assert result.skipped == "too_long"
    assert rec.count == 0


def test_a_clip_that_is_too_large_is_refused(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_MAX_MB", 0.00001)
    rec = install(monkeypatch, "x")

    result = run(data=b"x" * 5000)

    assert result.skipped == "too_large"
    assert rec.count == 0


def test_an_empty_clip_is_refused(monkeypatch):
    rec = install(monkeypatch, "x")

    assert run(data=b"").skipped == "empty"
    assert rec.count == 0


# ── Failures ──────────────────────────────────────────────────────────────
def test_a_timeout_is_reported(monkeypatch):
    install(monkeypatch, asyncio.TimeoutError())

    assert run().error == "timeout"


def test_a_transient_failure_is_retried(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "TRANSCRIBE_BACKOFF_SECONDS", 0.0)
    rec = install(monkeypatch, RuntimeError("503 unavailable"), "سلام")

    result = run()

    assert result.ok is True
    assert rec.count == 2


def test_a_permanent_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_MAX_RETRIES", 3)
    rec = install(monkeypatch, transcribe.TranscribeUnavailable("sdk_missing"))

    assert run().ok is False
    assert rec.count == 1


def test_it_never_raises(monkeypatch):
    install(monkeypatch, RuntimeError("something exotic"))

    assert run().ok is False


# ── The brakes ────────────────────────────────────────────────────────────
def test_the_rate_window_stops_calls(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_RATE_LIMIT", 2)
    rec = install(monkeypatch, "a", "b", "c")

    run()
    run()
    third = run()

    assert third.skipped == "rate_limit"
    assert rec.count == 2


def test_the_daily_cap_stops_calls(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_DAILY_LIMIT", 1)
    rec = install(monkeypatch, "a", "b")

    run()
    second = run()

    assert second.skipped == "daily_cap"
    assert rec.count == 1


def test_the_circuit_breaker_opens(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_CIRCUIT_FAILURES", 2)
    monkeypatch.setattr(config, "TRANSCRIBE_CIRCUIT_SECONDS", 300.0)
    rec = install(monkeypatch, RuntimeError("boom"), RuntimeError("boom"))

    run()
    run()
    third = run()

    assert third.skipped == "circuit_open"
    assert rec.count == 2


def test_the_counters_are_recorded(monkeypatch):
    install(monkeypatch, "سلام")

    run()

    usage = db.transcript_usage()
    assert usage["calls"] == 1
    assert usage["transcripts"] == 1


def test_a_skip_is_not_a_spend(monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBE_DAILY_LIMIT", 1)
    install(monkeypatch, "a", "b")

    run()
    run()

    usage = db.transcript_usage()
    assert usage["calls"] == 1
    assert usage["skipped"] == 1


# ── The MediaRef convenience ──────────────────────────────────────────────
def test_a_non_audio_ref_is_refused_without_a_call(monkeypatch):
    from app import media
    from types import SimpleNamespace

    rec = install(monkeypatch, "x")
    ref = media.describe(
        SimpleNamespace(
            photo=[SimpleNamespace(file_id="p", file_unique_id="u", file_size=10)],
            video=None, animation=None, video_note=None, sticker=None, voice=None,
            audio=None, document=None,
        )
    )

    result = asyncio.run(transcribe.transcribe_ref(ref, download=lambda f: _ret(b"x")))

    assert result.skipped == "not_audio"
    assert rec.count == 0


def test_a_voice_ref_is_transcribed(monkeypatch):
    from app import media
    from types import SimpleNamespace

    install(monkeypatch, "سلام")
    ref = media.describe(
        SimpleNamespace(
            photo=None, video=None, animation=None, video_note=None, sticker=None,
            voice=SimpleNamespace(file_id="v", file_unique_id="u", file_size=100,
                                  duration=3, mime_type="audio/ogg"),
            audio=None, document=None,
        )
    )

    result = asyncio.run(transcribe.transcribe_ref(ref, download=lambda f: _ret(b"x")))

    assert result.ok is True
    assert result.text == "سلام"


def test_a_download_failure_in_the_convenience_path_is_reported(monkeypatch):
    from app import media
    from types import SimpleNamespace

    async def _boom(file_id):
        raise RuntimeError("network")

    ref = media.describe(
        SimpleNamespace(
            photo=None, video=None, animation=None, video_note=None, sticker=None,
            voice=SimpleNamespace(file_id="v", file_unique_id="u", file_size=100,
                                  duration=3, mime_type="audio/ogg"),
            audio=None, document=None,
        )
    )

    result = asyncio.run(transcribe.transcribe_ref(ref, download=_boom))

    assert result.ok is False
    assert result.error == "download_failed"


# ── What reaches the model ────────────────────────────────────────────────
def test_the_audio_bytes_and_mime_reach_the_seam(monkeypatch):
    rec = install(monkeypatch, "سلام")

    run(data=b"raw-audio", mime="audio/ogg")

    assert rec.calls[0] == (b"raw-audio", "audio/ogg")


def test_the_instruction_forbids_answering_the_speaker():
    """The failure mode of a speech model asked to transcribe a question."""
    text = transcribe.SYSTEM_INSTRUCTION
    assert "Do not answer the speaker" in text
    assert "not an answer to it" in text


def test_the_instruction_forbids_translating():
    assert "Do not translate" in transcribe.SYSTEM_INSTRUCTION


async def _ret(value):
    return value
