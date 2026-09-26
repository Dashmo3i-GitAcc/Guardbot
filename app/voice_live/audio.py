"""PCM in, PCM out, at the three rates this feature has to speak.

Three rates meet in one call, and none of them is negotiable:

* **Telegram** hands the call 48 kHz, 16-bit, mono. This is what the voice-chat
  transport reads and writes, and it is fixed by the transport.
* **The provider's input** is 16 kHz. The Live API's realtime input is
  ``audio/pcm;rate=16000``, and there is no other rate to ask for.
* **The provider's output** is 24 kHz. A Live session answers in 24 kHz PCM, and
  that too is fixed.

So every call resamples in both directions, continuously, for as long as it
lasts. Two things about that are worth knowing before reading the code.

**The ratios are integers, and that is not an accident worth ignoring.**
48000/16000 is 3 and 48000/24000 is 2. A general resampler would need a filter
kernel and a phase accumulator; at these ratios it needs an average over three
samples and a midpoint between two. The module refuses a non-integer ratio
rather than falling back to something approximate, because the only rates it
will ever be asked about are the three above — a mixed ratio here would mean a
rate had been configured that no part of this feature produces, and silently
resampling it would hide that.

**Resampling has memory, so it cannot be a pure function over each chunk.**
Downsampling 48 kHz by 3 only works on groups of three samples; a chunk whose
length is not a multiple of three has one or two samples left over, and dropping
them is a click every 20 ms — a 50 Hz buzz under the entire call. Upsampling by
2 interpolates between *adjacent* input samples, and the first sample of a chunk
is adjacent to the last sample of the previous one. So the resampler carries
state between chunks, and ``StreamResampler`` is the only thing here that is
allowed to: it is per-session, and it is reset when a session's stream restarts.

The averaging used when downsampling is a box filter, which is a crude
low-pass — but it is the *right* crude low-pass here, because decimating without
any filter aliases everything above 8 kHz down into the speech band. Averaging
three samples attenuates the top third of the band before it is folded, which is
enough for speech and costs nothing.

What this module never does: it holds no audio beyond a bounded frame buffer,
writes nothing to disk, and logs no samples. A buffer here is at most one frame
of 20 ms, and the docstring says so because "no persistent raw audio" is a
requirement of this feature rather than an implementation detail.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from array import array
from math import gcd

log = logging.getLogger("guardbot.voice.audio")

# The three rates. Named for *what they are*, not for who sends them, because
# the provider is on both sides of two of them.
RATE_TELEGRAM = 48000
RATE_PROVIDER_IN = 16000
RATE_PROVIDER_OUT = 24000

SAMPLE_BYTES = 2  # 16-bit, everywhere, in both directions
FRAME_MS = 20

# One frame of 20 ms at each rate. Telegram's is the one that matters for
# pacing: the transport accepts a frame at a time and plays them back to back,
# so this is the granularity at which audio is either smooth or stuttering.
FRAME_BYTES = {
    RATE_TELEGRAM: RATE_TELEGRAM * FRAME_MS // 1000 * SAMPLE_BYTES,  # 1920
    RATE_PROVIDER_IN: RATE_PROVIDER_IN * FRAME_MS // 1000 * SAMPLE_BYTES,  # 640
    RATE_PROVIDER_OUT: RATE_PROVIDER_OUT * FRAME_MS // 1000 * SAMPLE_BYTES,  # 960
}

MIME_PROVIDER_IN = f"audio/pcm;rate={RATE_PROVIDER_IN}"

# Silence, in 16-bit terms. Not zero: a real microphone in a quiet room still
# dithers, and treating exact zero as the only silence makes every metric
# report a room that is never quiet.
SILENCE_RMS = 90.0


# ── Sample <-> bytes ──────────────────────────────────────────────────────
def _swap() -> bool:
    """Whether this interpreter's native sample order is big-endian.

    ``array('h')`` uses the platform's byte order, and the wire format is
    little-endian. On every machine this bot runs on the two agree, so this is
    normally False — but a big-endian host would otherwise produce audio that is
    correct in length and noise in content, which is a failure that reads like a
    model problem rather than a byte-order one.
    """
    return sys.byteorder == "big"


def to_samples(data: bytes) -> array:
    """Bytes to samples. Odd trailing bytes are dropped, not guessed at.

    A half-sample is not a sample. It happens only if a transport hands over an
    odd-length buffer, which would itself be a bug in the transport — so it is
    dropped here rather than padded, and the length arithmetic in the callers
    stays honest.
    """
    usable = len(data) - (len(data) % SAMPLE_BYTES)
    out = array("h")
    out.frombytes(data[:usable])
    if _swap():
        out.byteswap()
    return out


def to_bytes(samples: array) -> bytes:
    """Samples to bytes, little-endian, clamped to the 16-bit range.

    Clamping rather than wrapping. Interpolation between two samples cannot
    exceed their range, but a gain applied anywhere upstream can, and a wrapped
    sample is a loud click — the single worst artefact to introduce into a voice
    call, because it is loud, it is periodic, and it sounds like a hardware
    fault.
    """
    out = array("h", samples)
    for index, value in enumerate(out):
        if value > 32767:
            out[index] = 32767
        elif value < -32768:
            out[index] = -32768
    if _swap():
        out.byteswap()
    return out.tobytes()


# ── Resampling ────────────────────────────────────────────────────────────
def ratio(src: int, dst: int) -> tuple[int, int]:
    """``(up, down)`` for a rate pair, reduced. Raises on a mixed ratio.

    Only two pairs are supported, and they are exactly the two the call itself
    needs: **48000 -> 16000** (the call's microphone into the provider) and
    **24000 -> 48000** (the provider's speech back into the call). Both reduce to
    a single integer factor — 3 and 2 — which is what lets this module do the
    conversion with an average and a midpoint instead of a filter kernel.

    Anything else is refused rather than approximated. A mixed ratio such as
    24000 -> 16000 (which reduces to 2/3) is *not* unsupported in principle, it
    is simply not a conversion the call performs — and a caller that needs it can
    compose two supported steps, ``24000 -> 48000 -> 16000``, which is exact.
    Saying that in the error is better than silently resampling with a worse
    method than the caller would have chosen.
    """
    if src <= 0 or dst <= 0:
        raise ValueError(f"bad sample rate: {src} -> {dst}")
    common = gcd(int(src), int(dst))
    up, down = int(dst) // common, int(src) // common
    if up != 1 and down != 1:
        raise ValueError(
            f"unsupported resample ratio {src} -> {dst} ({up}/{down}); the "
            "supported conversions are 48000->16000 and 24000->48000, and a "
            "mixed ratio must be composed from them"
        )
    return up, down


def downsample(samples: array, factor: int) -> array:
    """Average groups of ``factor`` samples. The caller guarantees alignment.

    Only whole groups are converted; a trailing partial group is ignored here
    because ``StreamResampler`` is what carries it, and doing it in both places
    would double-count.
    """
    if factor <= 1:
        return array("h", samples)
    count = len(samples) // factor
    out = array("h", bytes(count * SAMPLE_BYTES))
    for index in range(count):
        base = index * factor
        total = 0
        for offset in range(factor):
            total += samples[base + offset]
        out[index] = int(total / factor)
    return out


def upsample(samples: array, factor: int, *, previous: int = 0) -> array:
    """Linear interpolation by ``factor``, using ``previous`` for the first step.

    ``previous`` is the last input sample of the *preceding* chunk. Without it
    the first output sample of every chunk is interpolated from zero, which is a
    discontinuity at every chunk boundary — again a click, again periodic.
    """
    if factor <= 1:
        return array("h", samples)
    out = array("h", bytes(len(samples) * factor * SAMPLE_BYTES))
    last = int(previous)
    for index, value in enumerate(samples):
        value = int(value)
        step = (value - last) / factor
        for sub in range(factor):
            out[index * factor + sub] = int(last + step * sub)
        last = value
    return out


class StreamResampler:
    """A resampler that carries whatever it could not convert yet.

    One per direction per session, because the state it holds is a property of
    one continuous stream: mixing two streams through one instance would
    interpolate across the boundary between them, and the carried remainder of
    one speaker's audio would become the start of another's.

    Two kinds of remainder are carried, and the second is the one that is easy
    to miss:

    * **Whole samples.** Downsampling by 3 only works on groups of three, so a
      chunk can leave one or two samples over.
    * **A single odd byte.** A chunk whose length is odd ends in half a sample.
      ``to_samples`` is a pure function and drops that byte, which is correct
      for it — but a *stream* that dropped it would lose one byte per odd chunk,
      and a transport that hands over odd-length buffers would quietly degrade
      the audio rather than fail. So the byte is held until the next chunk
      completes it.

    The total held is at most ``down - 1`` samples plus one byte — six bytes at
    the widest ratio here. That bound is why this class can be said to hold no
    audio: it is the remainder of one arithmetic operation, not a buffer.
    """

    def __init__(self, src: int, dst: int) -> None:
        self.src = int(src)
        self.dst = int(dst)
        self.up, self.down = ratio(src, dst)
        self._carry = array("h")
        self._raw = bytearray()
        self._previous = 0

    @property
    def passthrough(self) -> bool:
        return self.up == 1 and self.down == 1

    def reset(self) -> None:
        """Forget the carried samples. Called when a stream restarts, and only
        then — a reset mid-stream is the click this class exists to avoid."""
        self._carry = array("h")
        self._raw.clear()
        self._previous = 0

    def feed(self, data: bytes) -> bytes:
        """Convert one chunk, returning whatever is complete.

        May return ``b""`` when a chunk was too short to yield even one output
        sample, which is normal at the start of a downsampling stream and must
        not be treated as an error by the caller.
        """
        if data:
            self._raw.extend(data)
        usable = len(self._raw) - (len(self._raw) % SAMPLE_BYTES)
        if not usable:
            return b""
        samples = to_samples(bytes(self._raw[:usable]))
        del self._raw[:usable]
        if self.passthrough:
            return to_bytes(samples)
        if self.down > 1:
            return self._feed_down(samples)
        return self._feed_up(samples)

    def _feed_down(self, samples: array) -> bytes:
        buffered = self._carry + samples if self._carry else samples
        whole = len(buffered) // self.down
        if not whole:
            self._carry = array("h", buffered)
            return b""
        consumed = whole * self.down
        self._carry = array("h", buffered[consumed:])
        return to_bytes(downsample(buffered[:consumed], self.down))

    def _feed_up(self, samples: array) -> bytes:
        if not samples:
            return b""
        out = upsample(samples, self.up, previous=self._previous)
        self._previous = int(samples[-1])
        return to_bytes(out)

    def flush(self) -> bytes:
        """Emit whatever is still held, at end of stream.

        For the downsampling direction this converts the last partial group by
        averaging the samples it does have, rather than dropping them: at most
        two samples, so the audible difference is nil, but it keeps the length
        arithmetic exact and stops a stream ending one group short for reasons
        that would look like a bug in the caller.

        Called when a stream ends. Not called between chunks — that is what the
        carrying is for.
        """
        out = b""
        if self.down > 1 and self._carry:
            # Averaged over the samples actually present, which is why this
            # cannot go through ``downsample``: that converts whole groups only,
            # so a two-sample remainder at the end of a stream would convert to
            # nothing and the stream would end a group short.
            count = len(self._carry)
            total = sum(int(value) for value in self._carry)
            out = to_bytes(array("h", [int(total / count)]))
            self._carry = array("h")
        # A trailing odd byte cannot be completed and is dropped here, at end of
        # stream, where there is no next chunk to complete it. This is the one
        # place the loss is real and unavoidable, and it is half a sample.
        self._raw.clear()
        return out


# ── Framing ───────────────────────────────────────────────────────────────
class Framer:
    """Chops a stream of bytes into fixed-size frames.

    The Telegram transport plays a frame at a time, so playback pacing is
    decided by how these are handed over, and a partial frame at the end of a
    turn is padded with silence rather than sent short — a short frame is read
    as a truncated buffer by some transports and as an underrun by others, and
    neither is worth the few milliseconds it saves.

    Holds at most one frame, for the same reason ``StreamResampler`` holds at
    most two samples: the "no persistent raw audio" requirement is a bound on
    what is held, not a promise about what is touched.
    """

    def __init__(self, frame_bytes: int) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame size must be positive")
        self.frame_bytes = int(frame_bytes)
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Return every complete frame this chunk completes."""
        if data:
            self._buffer.extend(data)
        size = self.frame_bytes
        frames: list[bytes] = []
        while len(self._buffer) >= size:
            frames.append(bytes(self._buffer[:size]))
            del self._buffer[:size]
        return frames

    def flush(self, *, pad: bool = True) -> bytes:
        """The remainder, padded to a whole frame. ``b""`` when empty.

        ``pad=False`` is for teardown, where the trailing partial frame is
        silence anyway and sending it would delay the leave.
        """
        if not self._buffer:
            return b""
        rest = bytes(self._buffer)
        self._buffer.clear()
        if not pad:
            return b""
        return rest + bytes(self.frame_bytes - len(rest))

    def reset(self) -> None:
        self._buffer.clear()


# ── Level ─────────────────────────────────────────────────────────────────
def rms(samples: array) -> float:
    """Root-mean-square level, 0..32767. The cheapest honest loudness.

    Used for metrics and for the silence gate in the log, and deliberately
    **not** used for turn detection: the provider's own voice-activity detector
    is what decides when an utterance ends, and a second, cruder one here would
    disagree with it — which is how a barge-in comes to be detected twice, or
    once by each side, with the two answers disagreeing.
    """
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += int(value) * int(value)
    return (total / len(samples)) ** 0.5


def is_silent(data: bytes) -> bool:
    """Whether a buffer is quiet enough to be treated as room tone."""
    return rms(to_samples(data)) <= SILENCE_RMS


def frame_samples(rate: int) -> int:
    """How many samples one 20 ms frame holds at a rate."""
    return int(rate) * FRAME_MS // 1000


# ── Containers ────────────────────────────────────────────────────────────
# Everything above this line is arithmetic on PCM. These two are the other
# direction of the same problem: the *outside* world does not speak raw PCM.
# Telegram's voice notes are OGG/Opus, and a voice note Nexus sends back has to
# be OGG/Opus too, while the provider's input and output are bare PCM at fixed
# rates. So two conversions are needed and neither belongs to the call:
#
#   decode_to_pcm16   OGG/Opus  ->  16 kHz mono s16le   (a voice note coming in)
#   encode_pcm24_to_ogg  24 kHz mono s16le  ->  OGG/Opus (an answer going out)
#
# They live here rather than beside their callers because the encode is now
# used by two features — the text assistant's TTS reply and Voice Context's
# spoken answer — and one ffmpeg invocation with two call sites is one place to
# change the bitrate, not two. ``app/chat.py``'s ``_pcm_to_ogg`` delegates
# here for exactly that reason.
#
# Both are best-effort and neither raises: ffmpeg may be absent, a container
# may be malformed, and a clip may be truncated. The caller is told by the
# return value and falls back — a codec is never worth failing a turn over, and
# a voice reply that cannot be encoded must never cost the answer itself.
OPUS_BITRATE = "32k"


def _ffmpeg(args: list[str], data: bytes, *, timeout: float) -> bytes:
    """Run one ffmpeg pass over ``data``. ``b""`` on any failure.

    The one place a subprocess is started, so the failure handling — missing
    binary, non-zero exit, timeout, empty output — is written once. stderr is
    truncated into the log and never returned: it is ffmpeg's own text and it
    can echo the input path, which for this feature is somebody's audio.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", *args],
            input=data,
            capture_output=True,
            timeout=max(1.0, float(timeout)),
        )
    except Exception as exc:  # noqa: BLE001 - missing binary, timeout, anything
        log.warning("[voice] ffmpeg failed to run: %s", type(exc).__name__)
        return b""
    if proc.returncode != 0 or not proc.stdout:
        log.warning(
            "[voice] ffmpeg returned %d: %s",
            proc.returncode,
            (proc.stderr or b"")[:160],
        )
        return b""
    return bytes(proc.stdout)


def decode_to_pcm16(data: bytes, *, timeout: float = 60.0) -> bytes:
    """A voice note's container as the provider's input: 16 kHz mono s16le.

    ``b""`` means the bytes could not be decoded, and the caller treats that as
    "this clip cannot be sent" rather than as silence — the two are different
    and only one of them is a reason to spend a turn.
    """
    if not data:
        return b""
    return _ffmpeg(
        [
            "-f", "ogg",
            "-i", "pipe:0",
            "-f", "s16le", "-ar", str(RATE_PROVIDER_IN), "-ac", "1",
            "pipe:1",
        ],
        data,
        timeout=timeout,
    )


def encode_pcm24_to_ogg(pcm: bytes, *, timeout: float = 60.0) -> bytes | None:
    """The provider's speech as a Telegram voice note. None when it cannot be.

    None rather than ``b""`` because the caller's decision is different here:
    the audio already exists and is the answer, so "cannot encode" means "send
    the text instead", which is a branch and not an error.
    """
    if not pcm:
        return None
    out = _ffmpeg(
        [
            "-f", "s16le", "-ar", str(RATE_PROVIDER_OUT), "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-f", "ogg", "pipe:1",
        ],
        pcm,
        timeout=timeout,
    )
    return out or None


def pcm_seconds(data: bytes, rate: int) -> float:
    """How long a PCM buffer is, for a ceiling measured in seconds."""
    if not data or rate <= 0:
        return 0.0
    return len(data) / float(SAMPLE_BYTES) / float(rate)
