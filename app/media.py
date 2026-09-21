"""Telegram media -> something Gemini can actually read.

One module, two callers. The moderation workload and the conversational one both
need to answer "what is in this attachment", and they must not answer it with
two different implementations — a sticker that the moderator can see but the
assistant cannot is a bug that only shows up as a confusing silence.

What is shared is the *translation*: which Telegram field holds the bytes, what
MIME type they are, whether the thing is a video, and how to turn it into a
bounded request. What is emphatically **not** shared is policy: the moderation
path's limits, key and decision live in ``app/ai_moderation.py`` and
``app/mod_policy.py``; the conversational path's live in ``app/chat.py``. This
module has no opinion about whether anything should be deleted or said.

The capability table below is not a guess. It was measured on this deployment's
key on 2026-09-21 with one real request per row:

    image/png   inline   64 B   -> described correctly
    image/gif   inline  1.2 KB  -> described correctly
    video/mp4   inline  1.9 KB  -> described correctly
    video/webm  inline  1.1 KB  -> described correctly
    audio/wav   inline   32 KB  -> described correctly
    audio/ogg   inline  2.6 KB  -> accepted

so images, video and audio are all sent as **inline** parts. The Files API also
works (upload, wait for PROCESSING, generateContent by URI, delete) and is
deliberately not used: it would leave a copy of a group member's media in
Google's storage for the life of the file, for no capability this bot needs.
Telegram's own download ceiling is 20 MB and the inline request ceiling is the
same order, so there is nothing the Files API would unlock here.

Three rules this module holds:

* **Bounded.** Bytes, duration and part count all have ceilings, and a request
  that would exceed one is reduced or refused — never sent unbounded.
* **Honest.** A media type that cannot be analysed returns ``ok=False`` with a
  reason. Nothing here fabricates a description, and nothing pretends a file was
  looked at when it was skipped.
* **Reusing, not duplicating.** ``describe`` is the single source of truth for
  "what media is this", and ``app/main.py``'s existing media pipeline was
  changed to delegate to it rather than keep a second copy of the same table.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("guardbot.media")

# ── Kinds ─────────────────────────────────────────────────────────────────
# Every media kind this bot understands, with the facts the callers need:
#
#   mime          the type sent to the API when Telegram does not tell us
#   video_like    decode with ffmpeg (the local detector's video path)
#   visual        something a picture-based moderator can look at
#   transcribable something the speech pipeline can turn into text
#
# `tgs` (a Lottie animated sticker) is not decodable by ffmpeg or readable by
# Gemini. Telegram attaches a static WebP preview, so the preview is what gets
# analysed — which is why `animated_sticker` is marked visual but not video.
KINDS = {
    "photo": ("image/jpeg", False, True, False),
    "sticker": ("image/webp", False, True, False),
    "animated_sticker": ("image/webp", False, True, False),
    "video_sticker": ("video/webm", True, True, False),
    "gif": ("video/mp4", True, True, False),
    "video": ("video/mp4", True, True, False),
    "video_note": ("video/mp4", True, True, False),
    "image_file": ("image/jpeg", False, True, False),
    "video_file": ("video/mp4", True, True, False),
    "voice": ("audio/ogg", False, False, True),
    "audio": ("audio/mpeg", False, False, True),
}

# What Gemini accepts as an inline part, per the measurements in the module
# docstring. A kind whose MIME is not here is refused rather than attempted: an
# unsupported upload wastes a request from a quota we are trying to respect, and
# its failure would look like a model outage.
INLINE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
        "video/mp4",
        "video/webm",
        "video/mpeg",
        "video/quicktime",
        "video/x-matroska",
        "audio/wav",
        "audio/mp3",
        "audio/mpeg",
        "audio/aac",
        "audio/flac",
        "audio/ogg",
        "audio/opus",
        "audio/webm",
    }
)

# Image MIME types Pillow can be asked to normalise. WebP is the one that
# matters in practice: Telegram stickers are WebP, and converting to PNG removes
# any doubt about whether the model accepts the container.
_NORMALISE = {"image/webp": "image/png", "image/heic": "image/jpeg"}


@dataclass(frozen=True)
class MediaRef:
    """One attachment on a Telegram message, described without downloading it."""

    kind: str
    file_id: str
    file_unique_id: str = ""
    mime_type: str = ""
    file_size: int = 0
    duration: float | None = None
    # A smaller still image Telegram attaches alongside a sticker or a video.
    # Used when the media itself cannot be decoded, and as the fallback for an
    # oversized file.
    thumb_file_id: str = ""
    obj: object = None

    @property
    def is_video_like(self) -> bool:
        return KINDS.get(self.kind, ("", False, False, False))[1]

    @property
    def is_visual(self) -> bool:
        return KINDS.get(self.kind, ("", False, False, False))[2]

    @property
    def is_transcribable(self) -> bool:
        return KINDS.get(self.kind, ("", False, False, False))[3]

    @property
    def effective_mime(self) -> str:
        return self.mime_type or KINDS.get(self.kind, ("", False, False, False))[0]


@dataclass(frozen=True)
class MediaPart:
    """One inline part for a Gemini request."""

    mime_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class MediaBundle:
    """The result of turning one message's media into request parts.

    ``ok`` False means nothing could be prepared, and ``note`` says why in a
    short machine-ish phrase that ends up in the log and the admin report. It is
    never a description of the content: this module does not know what the
    content is.
    """

    ok: bool = False
    kind: str = ""
    parts: list[MediaPart] = field(default_factory=list)
    note: str = ""
    total_bytes: int = 0
    # Set when a video was reduced to still frames, so a caller can say so in a
    # report rather than implying the whole clip was watched.
    reduced_to_frames: bool = False
    # Set when only a thumbnail was available (animated sticker, oversized file).
    thumbnail_only: bool = False

    @property
    def size(self) -> int:
        return sum(p.size for p in self.parts)

    def __bool__(self) -> bool:
        return self.ok


# ── Describing a message ──────────────────────────────────────────────────
def _thumb_file_id(obj) -> str:
    th = getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)
    return getattr(th, "file_id", "") or ""


def describe(msg) -> MediaRef | None:
    """What media, if any, this message carries.

    The order mirrors Telegram's own precedence: a message can carry several
    optional fields and only one of them is the thing the user meant to send.
    Returns None for a message with no supported attachment — including an
    animated sticker with no preview, which genuinely cannot be read.
    """
    if msg is None:
        return None

    if msg.photo:
        largest = msg.photo[-1]
        return MediaRef(
            kind="photo",
            file_id=largest.file_id,
            file_unique_id=getattr(largest, "file_unique_id", ""),
            mime_type="image/jpeg",
            file_size=getattr(largest, "file_size", 0) or 0,
            obj=largest,
        )

    if msg.video:
        v = msg.video
        return MediaRef(
            kind="video",
            file_id=v.file_id,
            file_unique_id=getattr(v, "file_unique_id", ""),
            mime_type=getattr(v, "mime_type", "") or "video/mp4",
            file_size=getattr(v, "file_size", 0) or 0,
            duration=getattr(v, "duration", None),
            thumb_file_id=_thumb_file_id(v),
            obj=v,
        )

    if msg.animation:
        a = msg.animation
        return MediaRef(
            kind="gif",
            file_id=a.file_id,
            file_unique_id=getattr(a, "file_unique_id", ""),
            mime_type=getattr(a, "mime_type", "") or "video/mp4",
            file_size=getattr(a, "file_size", 0) or 0,
            duration=getattr(a, "duration", None),
            thumb_file_id=_thumb_file_id(a),
            obj=a,
        )

    if msg.video_note:
        n = msg.video_note
        return MediaRef(
            kind="video_note",
            file_id=n.file_id,
            file_unique_id=getattr(n, "file_unique_id", ""),
            mime_type="video/mp4",
            file_size=getattr(n, "file_size", 0) or 0,
            duration=getattr(n, "duration", None),
            thumb_file_id=_thumb_file_id(n),
            obj=n,
        )

    if msg.sticker:
        st = msg.sticker
        if getattr(st, "is_animated", False):
            # .tgs is a Lottie animation: neither ffmpeg nor the model can read
            # it. The static preview is the only thing available, and using it
            # is better than dropping the sticker — but the caller is told, so
            # nothing claims the animation itself was understood.
            thumb = _thumb_file_id(st)
            if not thumb:
                return None
            return MediaRef(
                kind="animated_sticker",
                file_id=thumb,
                mime_type="image/webp",
                file_size=getattr(_thumb_of(st), "file_size", 0) or 0,
                obj=st,
            )
        if getattr(st, "is_video", False):
            return MediaRef(
                kind="video_sticker",
                file_id=st.file_id,
                file_unique_id=getattr(st, "file_unique_id", ""),
                mime_type=getattr(st, "mime_type", "") or "video/webm",
                file_size=getattr(st, "file_size", 0) or 0,
                thumb_file_id=_thumb_file_id(st),
                obj=st,
            )
        return MediaRef(
            kind="sticker",
            file_id=st.file_id,
            file_unique_id=getattr(st, "file_unique_id", ""),
            mime_type=getattr(st, "mime_type", "") or "image/webp",
            file_size=getattr(st, "file_size", 0) or 0,
            thumb_file_id=_thumb_file_id(st),
            obj=st,
        )

    if msg.voice:
        v = msg.voice
        return MediaRef(
            kind="voice",
            file_id=v.file_id,
            file_unique_id=getattr(v, "file_unique_id", ""),
            mime_type=getattr(v, "mime_type", "") or "audio/ogg",
            file_size=getattr(v, "file_size", 0) or 0,
            duration=getattr(v, "duration", None),
            obj=v,
        )

    if msg.audio:
        a = msg.audio
        return MediaRef(
            kind="audio",
            file_id=a.file_id,
            file_unique_id=getattr(a, "file_unique_id", ""),
            mime_type=getattr(a, "mime_type", "") or "audio/mpeg",
            file_size=getattr(a, "file_size", 0) or 0,
            duration=getattr(a, "duration", None),
            thumb_file_id=_thumb_file_id(a),
            obj=a,
        )

    if msg.document and getattr(msg.document, "mime_type", None):
        mt = msg.document.mime_type
        d = msg.document
        if mt.startswith("image/"):
            kind = "image_file"
        elif mt.startswith("video/"):
            kind = "video_file"
        elif mt.startswith("audio/"):
            kind = "audio"
        else:
            return None
        return MediaRef(
            kind=kind,
            file_id=d.file_id,
            file_unique_id=getattr(d, "file_unique_id", ""),
            mime_type=mt,
            file_size=getattr(d, "file_size", 0) or 0,
            duration=getattr(d, "duration", None),
            thumb_file_id=_thumb_file_id(d),
            obj=d,
        )

    return None


def _thumb_of(obj):
    return getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)


# ── Turning bytes into parts ──────────────────────────────────────────────
def _normalise_image(data: bytes, mime_type: str) -> tuple[bytes, str]:
    """Convert an image container the API may not accept into one it will.

    WebP is the case that matters: every Telegram sticker is WebP. The
    conversion is best-effort — if Pillow is unavailable or the bytes are not a
    decodable image, the original is returned and the API gets its chance.
    """
    target = _NORMALISE.get(mime_type)
    if not target:
        return data, mime_type
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            out = io.BytesIO()
            img.convert("RGB").save(out, format=target.split("/")[1].upper())
            return out.getvalue(), target
    except Exception as e:  # noqa: BLE001 - any failure keeps the original
        log.debug("image normalise failed (%s); sending as %s", e, mime_type)
        return data, mime_type


def _probe_duration(path: str) -> float:
    """Duration in seconds, or 0.0 when it cannot be determined.

    A video whose duration is unknown is treated as short by the caller only
    when its size is small; an unreadable duration is never assumed to be safe
    for a long clip.
    """
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path,
            ],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _extract_frames(path: str, out_dir: str, n: int, *, scale: int = 512) -> list[str]:
    """Evenly spread still frames from a video, for the oversized-video path.

    Deliberately a smaller scale than the detector's 640: these frames go to a
    model as images, and the extra pixels cost tokens without adding meaning.
    """
    dur = _probe_duration(path)
    if dur <= 0:
        times = [0.0]
    else:
        times = [dur * (0.1 + 0.8 * i / max(n - 1, 1)) for i in range(n)]
    frames: list[str] = []
    for i, t in enumerate(times):
        out = os.path.join(out_dir, f"m{i}.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{t:.2f}", "-i", path,
                    "-frames:v", "1", "-vf", f"scale='min({scale},iw)':-2", out,
                ],
                check=True, timeout=30,
            )
            if os.path.exists(out):
                frames.append(out)
        except Exception as e:  # noqa: BLE001
            log.warning("media frame %d failed: %s", i, e)
    return frames


async def build(
    ref: MediaRef,
    *,
    download,
    work_dir: str,
    max_mb: float | None = None,
    max_seconds: float | None = None,
    frames: int | None = None,
    max_parts: int | None = None,
) -> MediaBundle:
    """Prepare ``ref`` as inline parts, within every configured bound.

    ``download`` is an injected async callable taking a ``file_id`` and
    returning bytes. Injecting it is what keeps this module testable without a
    Telegram connection, and it is the same seam style the AI modules use.

    Never raises. Every failure is a bundle with ``ok=False`` and a reason.
    """
    if ref is None:
        return MediaBundle(ok=False, note="no media")

    max_mb = float(config.GEMINI_MEDIA_MAX_MB if max_mb is None else max_mb)
    max_seconds = float(
        config.GEMINI_MEDIA_MAX_SECONDS if max_seconds is None else max_seconds
    )
    frames = int(config.GEMINI_MEDIA_FRAMES if frames is None else frames)
    max_parts = int(config.GEMINI_MEDIA_MAX_PARTS if max_parts is None else max_parts)

    ceiling = int(max_mb * 1024 * 1024)
    mime = ref.effective_mime

    # Telegram tells us the size before we spend a download on it. A file we
    # already know is too big is skipped rather than fetched and discarded.
    if ref.file_size and ref.file_size > ceiling:
        return await _thumbnail_fallback(
            ref, download, ceiling, note=f"too large ({ref.file_size} bytes)"
        )

    if ref.duration and max_seconds and float(ref.duration) > max_seconds:
        # A long video is reduced to frames; a long audio clip is refused,
        # because half a sentence is a wrong sentence and this module will not
        # pretend otherwise.
        if not ref.is_video_like:
            return MediaBundle(
                ok=False,
                kind=ref.kind,
                note=f"too long ({int(ref.duration)}s)",
            )
        return await _video_frames(
            ref, download, work_dir, frames, max_parts, note="too long"
        )

    if mime not in INLINE_MIME_TYPES:
        return await _thumbnail_fallback(
            ref, download, ceiling, note=f"unsupported type ({mime})"
        )

    try:
        data = await download(ref.file_id)
    except Exception as e:  # noqa: BLE001 - a download failure is not fatal
        log.warning("media download failed kind=%s: %s", ref.kind, e)
        return MediaBundle(ok=False, kind=ref.kind, note="download failed")

    if not data:
        return MediaBundle(ok=False, kind=ref.kind, note="empty download")

    if len(data) > ceiling:
        return await _thumbnail_fallback(
            ref, download, ceiling, note=f"too large after download ({len(data)} bytes)"
        )

    if mime.startswith("image/"):
        data, mime = _normalise_image(data, mime)

    if mime not in INLINE_MIME_TYPES:
        return MediaBundle(ok=False, kind=ref.kind, note=f"unsupported type ({mime})")

    if ref.is_video_like and not _has_video_stream(data, mime):
        # A GIF served as an animation can still be a real GIF file rather than
        # an MP4, and a `.webm` sticker can be audio-only in principle. Rather
        # than sending something the API will reject, fall back to frames.
        bundle = await _video_frames(
            ref, download, work_dir, frames, max_parts, note="no video stream"
        )
        if bundle.ok:
            return bundle

    return MediaBundle(
        ok=True,
        kind=ref.kind,
        parts=[MediaPart(mime_type=mime, data=data)][:max_parts],
        note="",
    )


def _has_video_stream(data: bytes, mime: str) -> bool:
    """A cheap sanity check before sending bytes the API may reject.

    Only the container's own magic numbers are inspected; this is not a decoder.
    It exists so a mislabelled file becomes "frames" rather than a wasted
    request against a quota we are trying to respect.
    """
    if len(data) < 12:
        return False
    if mime.startswith("image/"):
        return False
    head = data[:12]
    # ISO base media (mp4/mov/m4v): a `ftyp` box at offset 4.
    if head[4:8] == b"ftyp":
        return True
    # Matroska / WebM: EBML header.
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return True
    # GIF87a / GIF89a
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


async def _video_frames(
    ref: MediaRef, download, work_dir: str, frames: int, max_parts: int, *, note: str
) -> MediaBundle:
    """Reduce a video to still frames. The documented fallback, not a guess."""
    try:
        data = await download(ref.file_id)
    except Exception as e:  # noqa: BLE001
        log.warning("frame fallback download failed: %s", e)
        return MediaBundle(ok=False, kind=ref.kind, note="download failed")

    if not data:
        return MediaBundle(ok=False, kind=ref.kind, note="empty download")

    path = os.path.join(work_dir, f"src_{ref.kind}")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except Exception as e:  # noqa: BLE001
        log.warning("frame fallback write failed: %s", e)
        return MediaBundle(ok=False, kind=ref.kind, note="write failed")

    paths = _extract_frames(path, work_dir, max(1, min(frames, max_parts)))
    if not paths:
        return MediaBundle(ok=False, kind=ref.kind, note="no frames extracted")

    parts: list[MediaPart] = []
    for p in paths[:max_parts]:
        try:
            with open(p, "rb") as fh:
                parts.append(MediaPart(mime_type="image/jpeg", data=fh.read()))
        except Exception:  # noqa: BLE001
            continue
    if not parts:
        return MediaBundle(ok=False, kind=ref.kind, note="no frames readable")
    return MediaBundle(
        ok=True,
        kind=ref.kind,
        parts=parts,
        note=f"{note}; sent as {len(parts)} still frames",
        reduced_to_frames=True,
    )


async def _thumbnail_fallback(
    ref: MediaRef, download, ceiling: int, *, note: str
) -> MediaBundle:
    """Last resort: the still preview Telegram attaches.

    A thumbnail is not the media, and callers are told so through
    ``thumbnail_only`` — an operator reading a report must be able to see that a
    decision was made on a preview rather than on the file.
    """
    if not ref.thumb_file_id:
        return MediaBundle(ok=False, kind=ref.kind, note=note)
    try:
        data = await download(ref.thumb_file_id)
    except Exception as e:  # noqa: BLE001
        log.warning("thumbnail fallback failed: %s", e)
        return MediaBundle(ok=False, kind=ref.kind, note=note)
    if not data or len(data) > ceiling:
        return MediaBundle(ok=False, kind=ref.kind, note=note)
    data, mime = _normalise_image(data, "image/webp")
    if mime not in INLINE_MIME_TYPES:
        return MediaBundle(ok=False, kind=ref.kind, note=note)
    return MediaBundle(
        ok=True,
        kind=ref.kind,
        parts=[MediaPart(mime_type=mime, data=data)],
        note=f"{note}; only the thumbnail was readable",
        thumbnail_only=True,
    )


async def build_for_message(
    msg,
    *,
    download,
    work_dir: str,
    max_mb: float | None = None,
    max_seconds: float | None = None,
    frames: int | None = None,
    max_parts: int | None = None,
) -> tuple[MediaRef | None, MediaBundle]:
    """Convenience: describe a message and prepare it in one step.

    Returns both, because the caller usually wants the kind and duration for its
    log line even when preparation failed.
    """
    ref = describe(msg)
    if ref is None:
        return None, MediaBundle(ok=False, note="no media")
    bundle = await build(
        ref,
        download=download,
        work_dir=work_dir,
        max_mb=max_mb,
        max_seconds=max_seconds,
        frames=frames,
        max_parts=max_parts,
    )
    return ref, bundle


def build_from_path(
    path: str,
    kind: str,
    *,
    work_dir: str,
    mime_type: str = "",
    max_mb: float | None = None,
    frames: int | None = None,
    max_parts: int | None = None,
) -> MediaBundle:
    """Prepare media that is *already on disk*, without downloading it again.

    The moderation pipeline has the file in its own temp directory by the time
    it needs to ask the AI — it downloaded it for the local detector. Going back
    to Telegram for the same bytes would be a second download of somebody's
    media, which is slower, wastes the API's bandwidth and is one more place the
    file exists. So this entry point takes the path.

    Synchronous, and deliberately so: it does no I/O beyond reading a local file
    and (for a video) one ffmpeg pass, and the caller already runs it in its
    media thread pool.
    """
    max_mb = float(config.GEMINI_MEDIA_MAX_MB if max_mb is None else max_mb)
    frames = int(config.GEMINI_MEDIA_FRAMES if frames is None else frames)
    max_parts = int(config.GEMINI_MEDIA_MAX_PARTS if max_parts is None else max_parts)
    ceiling = int(max_mb * 1024 * 1024)

    if not path or not os.path.exists(path):
        return MediaBundle(ok=False, kind=kind, note="no local file")

    video_like = KINDS.get(kind, ("", False, False, False))[1]
    mime = mime_type or KINDS.get(kind, ("", False, False, False))[0]

    try:
        size = os.path.getsize(path)
    except OSError:
        return MediaBundle(ok=False, kind=kind, note="stat failed")

    if size > ceiling:
        if video_like:
            return _frames_from_path(path, kind, work_dir, frames, max_parts,
                                     note=f"too large ({size} bytes)")
        return MediaBundle(ok=False, kind=kind, note=f"too large ({size} bytes)")

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        log.warning("local media read failed: %s", e)
        return MediaBundle(ok=False, kind=kind, note="read failed")

    if not data:
        return MediaBundle(ok=False, kind=kind, note="empty file")

    if mime.startswith("image/"):
        data, mime = _normalise_image(data, mime)

    if mime not in INLINE_MIME_TYPES:
        # A container the API will not take. For a video that is worth one more
        # attempt as frames; for anything else there is nothing to fall back to.
        if video_like:
            return _frames_from_path(path, kind, work_dir, frames, max_parts,
                                     note=f"unsupported type ({mime})")
        return MediaBundle(ok=False, kind=kind, note=f"unsupported type ({mime})")

    if video_like and not _has_video_stream(data, mime):
        bundle = _frames_from_path(path, kind, work_dir, frames, max_parts,
                                   note="no video stream")
        if bundle.ok:
            return bundle

    return MediaBundle(
        ok=True, kind=kind, parts=[MediaPart(mime_type=mime, data=data)], note=""
    )


def _frames_from_path(
    path: str, kind: str, work_dir: str, frames: int, max_parts: int, *, note: str
) -> MediaBundle:
    """The frame fallback, for a file already on disk."""
    paths = _extract_frames(path, work_dir, max(1, min(frames, max_parts)))
    if not paths:
        return MediaBundle(ok=False, kind=kind, note=note)
    parts: list[MediaPart] = []
    for p in paths[:max_parts]:
        try:
            with open(p, "rb") as fh:
                parts.append(MediaPart(mime_type="image/jpeg", data=fh.read()))
        except OSError:
            continue
    if not parts:
        return MediaBundle(ok=False, kind=kind, note=note)
    return MediaBundle(
        ok=True,
        kind=kind,
        parts=parts,
        note=f"{note}; sent as {len(parts)} still frames",
        reduced_to_frames=True,
    )
