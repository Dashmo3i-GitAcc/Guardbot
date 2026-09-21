"""End-to-end tests for the media pipeline in app/main.on_media.

The Telegram layer is faked; the detector is stubbed. This exercises the real
handler: download -> detect -> decide -> delete -> report -> cleanup.
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
from PIL import Image
from telegram.error import TelegramError

from app import config, db, detector, main

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999


# --------------------------------------------------------------- fakes
class FakeFile:
    async def download_to_drive(self, path):
        # a real (tiny) PNG: the scene stage opens the downloaded file with PIL,
        # so the fake download must produce something decodable
        Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "PNG")


class FakeBot:
    """Records every outgoing call. `photo_fails` forces the document fallback."""

    def __init__(self, photo_fails=False, document_fails=False):
        self.messages: list[str] = []
        self.photos: list[str] = []
        self.documents: list[str] = []
        self.photo_fails = photo_fails
        self.document_fails = document_fails

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")  # never an admin

    async def get_file(self, file_id):
        return FakeFile()

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        if self.photo_fails:
            raise TelegramError("photo rejected")
        self.photos.append(caption)

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        if self.document_fails:
            raise TelegramError("document rejected")
        self.documents.append(caption)


class FakeMessage:
    def __init__(self, **media):
        self.message_id = 55
        self.photo = None
        self.video = None
        self.animation = None
        self.video_note = None
        self.sticker = None
        self.document = None
        for key, value in media.items():
            setattr(self, key, value)
        self.delete_calls = 0
        self.delete_error = None

    async def delete(self):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error


class StubDetector:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def detect(self, path):
        if self.error is not None:
            raise self.error
        return self.result


def photo_obj(size=1000):
    return SimpleNamespace(file_id="photo", file_size=size, thumbnail=None, thumb=None)


def sticker_obj(is_animated=False, is_video=False, thumbnail=None):
    return SimpleNamespace(
        file_id="sticker", file_size=900, is_animated=is_animated,
        is_video=is_video, thumbnail=thumbnail, thumb=None,
    )


def install_frames(monkeypatch, n=3):
    """Make ffmpeg frame extraction deterministic without real video."""

    def _extract(video_path, out_dir, count):
        paths = []
        for i in range(n):
            p = os.path.join(out_dir, f"f{i}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"frame")
            paths.append(p)
        return paths

    monkeypatch.setattr(detector, "extract_frames", _extract)


def explicit_raw(label="FEMALE_GENITALIA_EXPOSED", score=0.67):
    return [{"class": label, "score": score, "box": [0, 0, 1, 1]}]


def run_media(bot, **media):
    msg = FakeMessage(**media)
    user = SimpleNamespace(id=7, full_name="Tester", username="tester", is_bot=False)
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=user,
    )
    asyncio.run(main.on_media(update, SimpleNamespace(bot=bot)))
    return msg, bot


@pytest.fixture(autouse=True)
def pipeline_env(monkeypatch, tmp_path):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_dir))
    monkeypatch.setattr(config, "MAX_DOWNLOAD_MB", 20)
    # These tests are about the *pipeline* — download, detect, delete, report,
    # cleanup — not about the policy that decides whether to delete. So they run
    # in the local-only mode, which is the deployment configuration in which the
    # local detector is allowed to act on its own, and the threshold is set to
    # the fixture score so the plumbing is what is being exercised.
    #
    # The policy itself has its own suites: tests/test_mod_policy.py for the
    # rules and tests/test_moderation_ai.py for the AI layer. The default mode
    # (MODERATION_REQUIRE_AI_CONFIRM=True) is deliberately *not* what these
    # tests run under, because in that mode a local-only signal never deletes
    # and there would be no deletion path left to test here.
    monkeypatch.setattr(config, "MODERATION_REQUIRE_AI_CONFIRM", False)
    monkeypatch.setattr(config, "MODERATION_LOCAL_HARD_THRESHOLD", 0.45)
    # The moderation AI is not configured in tests, and must not be reached:
    # nothing here may make a network call.
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", False)
    main._admin_cache.clear()
    main._recently_deleted.clear()
    yield tmp_dir


def assert_clean(tmp_dir):
    assert os.listdir(tmp_dir) == [], "temp media was not cleaned up"


def assert_no_admin_message(bot):
    assert bot.messages == [] and bot.photos == [] and bot.documents == []


def assert_review_notice(bot):
    """A REVIEW now tells the operator. It carries no media and no message text."""
    assert bot.photos == [] and bot.documents == []
    assert len(bot.messages) == 1, bot.messages
    assert "نیازمند بررسی دستی" in bot.messages[0]


# ------------------------------------------------- explicit media: deleted
def test_explicit_photo_is_deleted_and_reported(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    msg, bot = run_media(FakeBot(), photo=[photo_obj()])
    assert msg.delete_calls == 1
    assert len(bot.photos) == 1  # evidence + report
    assert_clean(pipeline_env)


def test_explicit_gif_is_deleted(monkeypatch, pipeline_env):
    install_frames(monkeypatch)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw(score=0.50)))
    msg, bot = run_media(FakeBot(), animation=photo_obj())
    assert msg.delete_calls == 1
    assert len(bot.photos) == 1
    assert_clean(pipeline_env)


def test_explicit_video_is_deleted(monkeypatch, pipeline_env):
    install_frames(monkeypatch)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw("MALE_GENITALIA_EXPOSED", 0.67)))
    msg, bot = run_media(FakeBot(), video=photo_obj())
    assert msg.delete_calls == 1
    assert len(bot.photos) == 1
    assert_clean(pipeline_env)


def test_explicit_video_note_is_deleted(monkeypatch, pipeline_env):
    install_frames(monkeypatch)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw("ANUS_EXPOSED", 0.56)))
    msg, bot = run_media(FakeBot(), video_note=photo_obj())
    assert msg.delete_calls == 1
    assert_clean(pipeline_env)


def test_explicit_static_sticker_is_deleted(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw("ANUS_EXPOSED", 0.56)))
    msg, bot = run_media(FakeBot(), sticker=sticker_obj())
    assert msg.delete_calls == 1
    assert len(bot.photos) == 1
    assert_clean(pipeline_env)


def test_explicit_video_sticker_is_deleted(monkeypatch, pipeline_env):
    install_frames(monkeypatch)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw("FEMALE_GENITALIA_EXPOSED", 0.61)))
    msg, bot = run_media(FakeBot(), sticker=sticker_obj(is_video=True))
    assert msg.delete_calls == 1
    assert_clean(pipeline_env)


def test_explicit_animated_sticker_uses_preview_thumbnail(monkeypatch, pipeline_env):
    thumb = SimpleNamespace(file_id="thumb", file_size=400, thumbnail=None, thumb=None)
    sticker = sticker_obj(is_animated=True, thumbnail=thumb)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw("MALE_GENITALIA_EXPOSED", 0.70)))
    msg, bot = run_media(FakeBot(), sticker=sticker)
    assert msg.delete_calls == 1
    assert len(bot.photos) == 1
    assert_clean(pipeline_env)


# ------------------------------------------------- allowed media: untouched
def test_normal_photo_is_allowed(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    msg, bot = run_media(FakeBot(), photo=[photo_obj()])
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)
    assert_clean(pipeline_env)


def test_swimsuit_photo_is_allowed(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(
        [{"class": "FEMALE_GENITALIA_COVERED", "score": 0.95, "box": [0, 0, 1, 1]}]
    ))
    msg, bot = run_media(FakeBot(), photo=[photo_obj()])
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)


def test_normal_sticker_is_allowed(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(
        [{"class": "FACE_FEMALE", "score": 0.86, "box": [0, 0, 1, 1]}]
    ))
    msg, bot = run_media(FakeBot(), sticker=sticker_obj())
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)


def test_normal_gif_is_allowed(monkeypatch, pipeline_env):
    install_frames(monkeypatch)
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    msg, bot = run_media(FakeBot(), animation=photo_obj())
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)


def test_borderline_media_is_not_deleted_but_is_reported_for_review(
    monkeypatch, pipeline_env
):
    # REVIEW band (0.25 <= score < 0.45): nothing is deleted, and the operator
    # is told so they can look. The report carries no media and no message text.
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw(score=0.30)))
    msg, bot = run_media(FakeBot(), photo=[photo_obj()])
    assert msg.delete_calls == 0
    assert_review_notice(bot)
    assert_clean(pipeline_env)


def test_detector_failure_fails_open(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(error=RuntimeError("onnx boom")))
    msg, bot = run_media(FakeBot(), photo=[photo_obj()])
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)
    assert_clean(pipeline_env)


def test_unsupported_media_is_skipped(monkeypatch, pipeline_env):
    # .tgs without a preview thumbnail cannot be analysed
    msg, bot = run_media(FakeBot(), sticker=sticker_obj(is_animated=True, thumbnail=None))
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)
    assert_clean(pipeline_env)


# ------------------------------------------------- delete failure
def test_delete_failure_applies_no_punishment_and_no_report(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    strikes = []
    monkeypatch.setattr(db, "add_strike", lambda *a, **k: strikes.append(a) or 1)

    msg = FakeMessage(photo=[photo_obj()])
    msg.delete_error = TelegramError("not enough rights")
    user = SimpleNamespace(id=7, full_name="Tester", username="tester", is_bot=False)
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=user,
    )
    bot = FakeBot()
    asyncio.run(main.on_media(update, SimpleNamespace(bot=bot)))

    assert msg.delete_calls == 1
    assert_no_admin_message(bot)   # DELETE_FAILED is not reported
    assert strikes == []            # no strike on failure
    assert_clean(pipeline_env)


# ------------------------------------------------- evidence fallbacks
def test_evidence_falls_back_to_document_then_text(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    msg, bot = run_media(FakeBot(photo_fails=True), photo=[photo_obj()])
    assert msg.delete_calls == 1
    assert len(bot.documents) == 1  # document fallback used
    assert bot.messages == []


def test_report_still_sent_when_evidence_upload_fails(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    msg, bot = run_media(FakeBot(photo_fails=True, document_fails=True), photo=[photo_obj()])
    assert msg.delete_calls == 1
    assert len(bot.messages) == 1  # text-only report still delivered
    assert_clean(pipeline_env)


def test_oversized_media_without_thumbnail_is_skipped(monkeypatch, pipeline_env):
    monkeypatch.setattr(config, "MAX_DOWNLOAD_MB", 0)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    msg, bot = run_media(FakeBot(), photo=[photo_obj(size=10_000_000)])
    assert msg.delete_calls == 0
    assert_no_admin_message(bot)
    assert_clean(pipeline_env)


# ------------------------------------------------- scene-level detection
class FakeScenePipe:
    """Stands in for the local scene classifier (labels: normal / nsfw)."""

    def __init__(self, score=0.99, error=None):
        self.score = score
        self.error = error

    def __call__(self, img, top_k=None):
        if self.error is not None:
            raise RuntimeError(self.error)
        return [
            {"label": "normal", "score": 1.0 - self.score},
            {"label": "nsfw", "score": self.score},
        ]


def test_scene_only_explicit_media_is_reviewed_not_deleted(monkeypatch, pipeline_env):
    """NudeNet finds nothing; only the scene score is high.

    This used to delete, and it deliberately no longer does. The scene
    classifier is the less interpretable of the two local signals, and the case
    it was added for — a sexual act with no exposed anatomy — is now handled by
    the moderation AI, which can attach a reason. A scene score is therefore
    evidence for a human, not a verdict, in every mode.
    """
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakeScenePipe(score=0.99))

    msg, bot = run_media(FakeBot(), photo=[photo_obj()])

    assert msg.delete_calls == 0
    assert_review_notice(bot)
    assert_clean(pipeline_env)


def test_scene_only_media_records_no_confirmed_violation(monkeypatch, pipeline_env):
    """Nothing was deleted, so nothing may count as a violation."""
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakeScenePipe(score=0.99))
    recorded = []
    monkeypatch.setattr(db, "add_strike", lambda *a, **k: recorded.append(a) or 1)

    run_media(FakeBot(), photo=[photo_obj()])

    assert recorded == []


def test_borderline_scene_media_is_not_deleted_and_is_reported(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakeScenePipe(score=0.75))

    msg, bot = run_media(FakeBot(), photo=[photo_obj()])

    assert msg.delete_calls == 0
    assert_review_notice(bot)
    assert_clean(pipeline_env)


def test_low_scene_score_is_allowed(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakeScenePipe(score=0.05))

    msg, bot = run_media(FakeBot(), photo=[photo_obj()])

    assert msg.delete_calls == 0
    assert_no_admin_message(bot)


def test_scene_stage_failure_fails_open(monkeypatch, pipeline_env):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakeScenePipe(error="scene boom"))

    msg, bot = run_media(FakeBot(), photo=[photo_obj()])

    assert msg.delete_calls == 0
    assert_no_admin_message(bot)
    assert_clean(pipeline_env)
