"""The media builder: every supported Telegram media type, and every refusal.

ffmpeg is not on the test host, so the frame path is monkeypatched exactly the
way the existing media pipeline suite does it. Everything else — the kind table,
the size and duration bounds, the image normalisation, the download failure
paths — runs for real.
"""
import asyncio
import io
import os
from types import SimpleNamespace

from PIL import Image

from app import config, media


def obj(**fields):
    """A stand-in for a Telegram media object. Only the fields we read."""
    fields.setdefault("file_id", "fid")
    fields.setdefault("file_unique_id", "uid")
    fields.setdefault("file_size", 1000)
    return SimpleNamespace(**fields)


def message(**media_fields):
    msg = SimpleNamespace(
        photo=None, video=None, animation=None, video_note=None, sticker=None,
        voice=None, audio=None, document=None,
    )
    for key, value in media_fields.items():
        setattr(msg, key, value)
    return msg


def photo(size=1000):
    return obj(file_id="photo", file_unique_id="u1", file_size=size)


def video(size=1000, duration=5, mime="video/mp4", thumb=None):
    return obj(file_id="video", file_size=size, duration=duration, mime_type=mime,
               thumbnail=thumb)


def sticker(*, animated=False, video_sticker=False, mime="image/webp", thumb=None):
    return obj(file_id="sticker", file_size=500, mime_type=mime,
               is_animated=animated, is_video=video_sticker, thumbnail=thumb)


def voice(size=1000, duration=3, mime="audio/ogg"):
    return obj(file_id="voice", file_size=size, duration=duration, mime_type=mime)


async def downloader(data: bytes):
    async def _dl(file_id):
        return data
    return _dl


def png_bytes(size=(8, 8), colour=(1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def webp_bytes(size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (9, 9, 9)).save(buf, "WEBP")
    return buf.getvalue()


# ── describe(): the kind table ────────────────────────────────────────────
def test_a_photo_is_a_photo():
    ref = media.describe(message(photo=[photo(), photo(2000)]))
    assert ref.kind == "photo"
    assert ref.is_visual is True
    assert ref.is_video_like is False
    assert ref.is_transcribable is False


def test_the_largest_photo_variant_is_chosen():
    small = obj(file_id="small", file_size=100)
    large = obj(file_id="large", file_size=900)
    ref = media.describe(message(photo=[small, large]))
    assert ref.file_id == "large"


def test_a_video_is_video_like():
    ref = media.describe(message(video=video()))
    assert ref.kind == "video"
    assert ref.is_video_like is True


def test_an_animation_is_a_gif():
    ref = media.describe(message(animation=obj(file_id="a", mime_type="video/mp4")))
    assert ref.kind == "gif"
    assert ref.is_video_like is True


def test_a_video_note_is_recognised():
    ref = media.describe(message(video_note=obj(file_id="n", duration=2)))
    assert ref.kind == "video_note"
    assert ref.is_video_like is True


def test_a_static_sticker_is_an_image():
    ref = media.describe(message(sticker=sticker()))
    assert ref.kind == "sticker"
    assert ref.is_visual is True
    assert ref.is_video_like is False


def test_a_video_sticker_is_video_like():
    ref = media.describe(message(sticker=sticker(video_sticker=True, mime="video/webm")))
    assert ref.kind == "video_sticker"
    assert ref.is_video_like is True


def test_an_animated_sticker_uses_its_still_preview():
    """`.tgs` is Lottie: neither ffmpeg nor the model can read it.

    Telegram attaches a static WebP preview, and using that is better than
    dropping the sticker — but the kind says so, so nothing claims the animation
    itself was understood.
    """
    ref = media.describe(message(sticker=sticker(animated=True, thumb=obj(file_id="preview"))))
    assert ref.kind == "animated_sticker"
    assert ref.file_id == "preview"
    assert ref.is_visual is True


def test_an_animated_sticker_with_no_preview_is_not_described():
    """Nothing to read means nothing to claim."""
    assert media.describe(message(sticker=sticker(animated=True))) is None


def test_a_voice_message_is_transcribable():
    ref = media.describe(message(voice=voice()))
    assert ref.kind == "voice"
    assert ref.is_transcribable is True
    assert ref.is_visual is False


def test_an_audio_file_is_recognised():
    ref = media.describe(message(audio=obj(file_id="a", mime_type="audio/mpeg", duration=9)))
    assert ref.kind == "audio"
    assert ref.is_transcribable is True


def test_image_video_and_audio_documents_are_recognised():
    for mime, kind in (
        ("image/png", "image_file"),
        ("video/mp4", "video_file"),
        ("audio/mpeg", "audio"),
    ):
        ref = media.describe(message(document=obj(file_id="d", mime_type=mime)))
        assert ref.kind == kind, mime


def test_an_unsupported_document_is_not_described():
    """A PDF or a zip is not something this bot analyses, and it says so by
    returning nothing rather than by pretending."""
    for mime in ("application/pdf", "application/zip", "text/plain", "application/x-tar"):
        assert media.describe(message(document=obj(mime_type=mime))) is None


def test_a_message_with_no_media_is_not_described():
    assert media.describe(message()) is None
    assert media.describe(None) is None


def test_photo_wins_over_the_other_fields():
    """Telegram allows several fields; only one is what the user meant."""
    msg = message(photo=[photo()], video=video(), document=obj(mime_type="image/png"))
    assert media.describe(msg).kind == "photo"


# ── build(): images ───────────────────────────────────────────────────────
def test_an_image_becomes_one_inline_part():
    ref = media.describe(message(photo=[photo()]))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(png_bytes()),
                                     work_dir="/tmp"))

    assert bundle.ok is True
    assert len(bundle.parts) == 1
    assert bundle.parts[0].mime_type == "image/jpeg"
    assert bundle.parts[0].data


def test_a_webp_sticker_is_converted_to_png():
    """The container the API is sure about, for the format Telegram always uses."""
    ref = media.describe(message(sticker=sticker()))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(webp_bytes()),
                                     work_dir="/tmp"))

    assert bundle.ok is True
    assert bundle.parts[0].mime_type == "image/png"


def test_an_undecodable_webp_is_still_sent_as_webp():
    """The conversion is best-effort; a failure must not lose the media."""
    ref = media.describe(message(sticker=sticker()))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(b"not an image"),
                                     work_dir="/tmp"))

    assert bundle.ok is True
    assert bundle.parts[0].mime_type == "image/webp"


# ── build(): size and duration bounds ─────────────────────────────────────
def test_an_oversized_file_is_refused_before_it_is_downloaded(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_MB", 1.0)
    called = []

    async def _dl(file_id):
        called.append(file_id)
        return b"x"

    ref = media.describe(message(video=video(size=50 * 1024 * 1024)))
    bundle = asyncio.run(media.build(ref, download=_dl, work_dir="/tmp"))

    assert bundle.ok is False
    assert called == [], "a file we already know is too big must not be fetched"


def test_an_oversized_image_with_a_thumbnail_falls_back_to_it(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_MB", 1.0)
    ref = media.describe(
        message(video=video(size=50 * 1024 * 1024, thumb=obj(file_id="thumb")))
    )
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(png_bytes()),
                                     work_dir="/tmp"))

    assert bundle.ok is True
    assert bundle.thumbnail_only is True
    assert "thumbnail" in bundle.note


def test_an_oversized_file_with_no_thumbnail_is_refused(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_MB", 1.0)
    ref = media.describe(message(video=video(size=50 * 1024 * 1024)))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(b"x"),
                                     work_dir="/tmp"))

    assert bundle.ok is False


def test_a_long_audio_clip_is_refused_rather_than_truncated(monkeypatch):
    """Half a sentence is a wrong sentence, so this refuses instead."""
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_SECONDS", 10.0)
    ref = media.describe(message(voice=voice(duration=600)))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(b"x"),
                                     work_dir="/tmp"))

    assert bundle.ok is False
    assert "too long" in bundle.note


def test_a_long_video_falls_back_to_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_SECONDS", 10.0)
    _install_frames(monkeypatch, tmp_path, 3)
    ref = media.describe(message(video=video(duration=600)))

    bundle = asyncio.run(
        media.build(ref, download=lambda f: _ret(b"\x00\x00\x00\x18ftypmp42"),
                    work_dir=str(tmp_path))
    )

    assert bundle.ok is True
    assert bundle.reduced_to_frames is True
    assert len(bundle.parts) == 3
    assert all(p.mime_type == "image/jpeg" for p in bundle.parts)


# ── build(): failures ─────────────────────────────────────────────────────
def test_a_download_failure_is_reported_not_raised():
    async def _boom(file_id):
        raise RuntimeError("network")

    ref = media.describe(message(photo=[photo()]))
    bundle = asyncio.run(media.build(ref, download=_boom, work_dir="/tmp"))

    assert bundle.ok is False
    assert bundle.note == "download failed"


def test_an_empty_download_is_a_failure():
    ref = media.describe(message(photo=[photo()]))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(b""),
                                     work_dir="/tmp"))

    assert bundle.ok is False
    assert bundle.note == "empty download"


def test_an_unsupported_mime_is_refused():
    ref = media.MediaRef(kind="image_file", file_id="x", mime_type="image/tiff",
                         file_size=10)
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(b"x"),
                                     work_dir="/tmp"))

    assert bundle.ok is False
    assert "unsupported type" in bundle.note


def test_a_missing_file_id_is_a_failure_not_a_crash():
    ref = media.describe(message(photo=[obj(file_id="", file_size=10)]))
    bundle = asyncio.run(media.build(ref, download=lambda f: _ret(b""),
                                     work_dir="/tmp"))
    assert bundle.ok is False


def test_build_with_no_ref_is_a_failure():
    bundle = asyncio.run(media.build(None, download=lambda f: _ret(b"x"),
                                     work_dir="/tmp"))
    assert bundle.ok is False
    assert bundle.note == "no media"


def test_the_part_count_is_capped(monkeypatch, tmp_path):
    """A pathological message must not become an unbounded upload."""
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_PARTS", 2)
    _install_frames(monkeypatch, tmp_path, 9)
    ref = media.describe(message(video=video(duration=600)))
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_SECONDS", 1.0)

    bundle = asyncio.run(
        media.build(ref, download=lambda f: _ret(b"\x00\x00\x00\x18ftypmp42"),
                    work_dir=str(tmp_path))
    )

    assert len(bundle.parts) <= 2


# ── build_from_path(): the moderation path ────────────────────────────────
def test_a_local_image_is_read_without_downloading_it(tmp_path):
    path = tmp_path / "media"
    path.write_bytes(png_bytes())

    bundle = media.build_from_path(str(path), "photo", work_dir=str(tmp_path))

    assert bundle.ok is True
    assert bundle.parts[0].data == path.read_bytes()


def test_a_local_oversized_video_falls_back_to_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GEMINI_MEDIA_MAX_MB", 0.0001)
    _install_frames(monkeypatch, tmp_path, 2)
    path = tmp_path / "media"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000)

    bundle = media.build_from_path(str(path), "video", work_dir=str(tmp_path))

    assert bundle.ok is True
    assert bundle.reduced_to_frames is True


def test_a_local_file_that_does_not_exist_is_a_failure(tmp_path):
    bundle = media.build_from_path(str(tmp_path / "nope"), "photo",
                                   work_dir=str(tmp_path))
    assert bundle.ok is False
    assert bundle.note == "no local file"


def test_a_local_empty_file_is_a_failure(tmp_path):
    path = tmp_path / "media"
    path.write_bytes(b"")
    bundle = media.build_from_path(str(path), "photo", work_dir=str(tmp_path))
    assert bundle.ok is False


# ── The capability table itself ───────────────────────────────────────────
def test_every_kind_the_describe_function_returns_is_in_the_table():
    """A kind that `describe` can produce but the table does not know would be
    treated as having no properties at all, which is how a video stops being a
    video."""
    described = {
        "photo", "sticker", "animated_sticker", "video_sticker", "gif", "video",
        "video_note", "image_file", "video_file", "voice", "audio",
    }
    assert described <= set(media.KINDS)


def test_every_kind_maps_to_a_mime_type_the_api_accepts():
    """Except the two that are deliberately normalised first."""
    normalised = set(media._NORMALISE)
    for kind, (mime, _video, _visual, _audio) in media.KINDS.items():
        assert mime in media.INLINE_MIME_TYPES or mime in normalised, kind


def test_the_capability_table_records_what_was_measured():
    """The MIME types the module docstring says were verified against the live
    API must be in the accepted set, or the measurement is not what the code
    implements."""
    for mime in ("image/png", "image/gif", "video/mp4", "video/webm", "audio/wav",
                 "audio/ogg"):
        assert mime in media.INLINE_MIME_TYPES


# ── helpers ───────────────────────────────────────────────────────────────
async def _ret(value):
    return value


def _install_frames(monkeypatch, tmp_path, count):
    def _extract(video_path, out_dir, n, **kwargs):
        paths = []
        for index in range(count):
            path = os.path.join(out_dir, f"m{index}.jpg")
            with open(path, "wb") as handle:
                handle.write(png_bytes())
            paths.append(path)
        return paths

    monkeypatch.setattr(media, "_extract_frames", _extract)
