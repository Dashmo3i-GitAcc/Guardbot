"""The optional second-stage scene classifier.

It exists to widen detection beyond NudeNet's body regions, and its contract is
strict: it may raise REVIEW and it may never delete, and it fails open.
"""
import os

from PIL import Image

from app import detector
from app.decision import Decision, default_engine
from app.detector import MediaAnalysis


class StubDetector:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def detect(self, path):
        if self.error is not None:
            raise self.error
        return self.result


class FakePipe:
    """Stands in for the transformers image-classification pipeline."""

    def __init__(self, score=0.97, error=None):
        self.score = score
        self.error = error
        self.calls = 0

    def __call__(self, img, top_k=None):
        self.calls += 1
        if self.error is not None:
            raise RuntimeError(self.error)
        return [
            {"label": "normal", "score": 1.0 - self.score},
            {"label": "nsfw", "score": self.score},
        ]


def png(path):
    Image.new("RGB", (8, 8), (10, 20, 30)).save(str(path), "PNG")
    return str(path)


# ------------------------------------------------------- the score itself
def test_no_pipeline_means_no_score(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_generic_pipe", None)
    assert detector._generic_score(png(tmp_path / "a.png")) is None


def test_score_is_read_from_the_nsfw_label(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_generic_pipe", FakePipe(score=0.97))
    assert detector._generic_score(png(tmp_path / "a.png")) == 0.97


def test_score_is_fail_open_on_inference_error(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_generic_pipe", FakePipe(error="onnx boom"))
    assert detector._generic_score(png(tmp_path / "a.png")) is None


def test_score_is_fail_open_on_undecodable_file(monkeypatch, tmp_path):
    bad = tmp_path / "not-an-image.bin"
    bad.write_bytes(b"definitely not an image")
    monkeypatch.setattr(detector, "_generic_pipe", FakePipe(score=0.99))
    assert detector._generic_score(str(bad)) is None


# ------------------------------------------------------- wiring into analysis
def test_analyze_image_attaches_the_score(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_generic_pipe", FakePipe(score=0.97))
    a = detector.analyze_image(png(tmp_path / "a.png"))
    assert a.ok is True
    assert a.generic_nsfw == 0.97


def test_analyze_image_without_the_stage_has_no_score(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_generic_pipe", None)
    a = detector.analyze_image(png(tmp_path / "a.png"))
    assert a.ok is True
    assert a.generic_nsfw is None


def test_analyze_video_scores_exactly_one_frame(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()

    def frames(path, out_dir, n):
        return [png(os.path.join(out_dir, f"f{i}.png")) for i in range(n)]

    pipe = FakePipe(score=0.5)
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_generic_pipe", pipe)
    monkeypatch.setattr(detector, "extract_frames", frames)

    res = detector.analyze_video("ignored", str(work))
    assert res.ok is True
    assert res.frames_checked == 4
    assert res.generic_nsfw == 0.5
    assert pipe.calls == 1  # one inference per media item, not per frame


# ------------------------------------------------------- it can never delete
def test_a_high_scene_score_is_review_not_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_generic_pipe", FakePipe(score=0.999))
    analysis = detector.analyze_image(png(tmp_path / "a.png"))
    result = default_engine().decide(analysis)
    assert result.decision is Decision.REVIEW
    assert result.decision is not Decision.EXPLICIT


def test_a_failed_scene_stage_leaves_a_clean_safe_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_generic_pipe", FakePipe(error="boom"))
    analysis = detector.analyze_image(png(tmp_path / "a.png"))
    assert analysis.generic_nsfw is None
    assert default_engine().decide(analysis).decision is Decision.SAFE


def test_an_analysis_without_the_stage_is_unchanged():
    # the pre-existing shape of MediaAnalysis must still behave identically
    res = default_engine().decide(MediaAnalysis(ok=True, detections=[], frames_checked=1))
    assert res.decision is Decision.SAFE
    assert res.generic_nsfw is None
