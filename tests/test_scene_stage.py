"""The second-stage scene classifier.

It exists to widen detection beyond NudeNet's body regions, so its contract is
now graded rather than REVIEW-only:

    score <  SCENE_REVIEW_THRESHOLD  -> SAFE
    score >= SCENE_REVIEW_THRESHOLD  -> REVIEW   (logged, never deletes)
    score >= SCENE_DELETE_THRESHOLD  -> EXPLICIT (deletes)

It must stay bounded (it scores only a few of the already-extracted frames) and
it must fail open: a missing model, a missing dependency, an undecodable file or
an inference error leaves the score absent, and an absent score never deletes.
"""
import os

from PIL import Image

from app import config, detector
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

    def __init__(self, score=0.97, error=None, scores=None):
        self.score = score
        self.error = error
        self.scores = list(scores) if scores is not None else None
        self.calls = 0

    def __call__(self, img, top_k=None):
        self.calls += 1
        if self.error is not None:
            raise RuntimeError(self.error)
        if self.scores:
            s = self.scores[min(self.calls - 1, len(self.scores) - 1)]
        else:
            s = self.score
        return [
            {"label": "normal", "score": 1.0 - s},
            {"label": "nsfw", "score": s},
        ]


def png(path):
    Image.new("RGB", (8, 8), (10, 20, 30)).save(str(path), "PNG")
    return str(path)


# ------------------------------------------------------- the score itself
def test_no_pipeline_means_no_score(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_scene_pipe", None)
    assert detector._scene_score(png(tmp_path / "a.png")) is None


def test_score_is_read_from_the_nsfw_label(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.97))
    assert detector._scene_score(png(tmp_path / "a.png")) == 0.97


def test_score_is_fail_open_on_inference_error(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(error="onnx boom"))
    assert detector._scene_score(png(tmp_path / "a.png")) is None


def test_score_is_fail_open_on_undecodable_file(monkeypatch, tmp_path):
    bad = tmp_path / "not-an-image.bin"
    bad.write_bytes(b"definitely not an image")
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.99))
    assert detector._scene_score(str(bad)) is None


# ------------------------------------------------------- wiring into analysis
def test_analyze_image_attaches_the_score_and_its_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.97))
    path = png(tmp_path / "a.png")
    a = detector.analyze_image(path)
    assert a.ok is True
    assert a.scene_nsfw == 0.97
    assert a.scene_frame == path  # usable as the admin evidence frame
    assert a.scene_frames == 1


def test_analyze_image_without_the_stage_has_no_score(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", None)
    a = detector.analyze_image(png(tmp_path / "a.png"))
    assert a.ok is True
    assert a.scene_nsfw is None
    assert a.scene_frame is None
    assert a.scene_frames == 0


# ------------------------------------------------------- bounded frame use
def test_scene_sample_is_bounded_and_spreads_across_the_frames():
    paths = [f"f{i}" for i in range(4)]
    assert detector._scene_sample(paths, 2) == ["f0", "f3"]
    assert detector._scene_sample(paths, 1) == ["f0"]
    assert detector._scene_sample(paths, 4) == paths
    assert detector._scene_sample(paths, 9) == paths
    assert detector._scene_sample(paths, 0) == []


def test_scene_sample_always_keeps_the_strongest_nudenet_frame():
    paths = [f"f{i}" for i in range(4)]
    assert "f1" in detector._scene_sample(paths, 2, preferred="f1")


def video_frames(monkeypatch, n=4):
    def frames(path, out_dir, _n):
        return [png(os.path.join(out_dir, f"f{i}.png")) for i in range(n)]

    monkeypatch.setattr(detector, "extract_frames", frames)


def test_analyze_video_reuses_the_sampled_frames_and_stays_bounded(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    video_frames(monkeypatch, 4)
    pipe = FakePipe(score=0.5)
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", pipe)
    monkeypatch.setattr(config, "SCENE_MAX_FRAMES", 2)

    res = detector.analyze_video("ignored", str(work))

    assert res.ok is True
    assert res.frames_checked == 4       # NudeNet still sees every frame
    assert res.scene_frames == 2         # the scene stage is capped
    assert pipe.calls == 2               # ... and costs exactly that many


def test_analyze_video_scores_all_frames_when_the_cap_allows(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    video_frames(monkeypatch, 3)
    pipe = FakePipe(score=0.5)
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", pipe)
    monkeypatch.setattr(config, "SCENE_MAX_FRAMES", 5)

    res = detector.analyze_video("ignored", str(work))

    assert pipe.calls == 3
    assert res.scene_frames == 3


def test_analyze_video_takes_the_highest_scene_score(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    video_frames(monkeypatch, 4)
    pipe = FakePipe(scores=[0.05, 0.99, 0.10, 0.20])
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", pipe)
    monkeypatch.setattr(config, "SCENE_MAX_FRAMES", 4)

    res = detector.analyze_video("ignored", str(work))

    assert res.scene_nsfw == 0.99
    assert res.scene_frame is not None


def test_one_bad_frame_does_not_hide_a_good_one(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    video_frames(monkeypatch, 2)

    real = detector._scene_score
    seen = []

    def flaky(path):
        seen.append(path)
        if len(seen) == 1:
            return None  # first frame fails to score
        return real(path)

    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.99))
    monkeypatch.setattr(config, "SCENE_MAX_FRAMES", 4)
    monkeypatch.setattr(detector, "_scene_score", flaky)

    res = detector.analyze_video("ignored", str(work))
    assert res.scene_nsfw == 0.99
    assert res.scene_frames == 1


# ------------------------------------------------------- the graded policy
def test_a_very_high_scene_score_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.999))
    analysis = detector.analyze_image(png(tmp_path / "a.png"))
    res = default_engine().decide(analysis)
    assert res.decision is Decision.EXPLICIT
    assert res.source == "scene"
    assert res.matched is None  # no anatomical evidence, yet it deletes


def test_a_borderline_scene_score_is_review(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.75))
    analysis = detector.analyze_image(png(tmp_path / "a.png"))
    assert default_engine().decide(analysis).decision is Decision.REVIEW


def test_a_low_scene_score_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(score=0.05))
    analysis = detector.analyze_image(png(tmp_path / "a.png"))
    assert default_engine().decide(analysis).decision is Decision.SAFE


def test_a_failed_scene_stage_leaves_a_clean_safe_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    monkeypatch.setattr(detector, "_scene_pipe", FakePipe(error="boom"))
    analysis = detector.analyze_image(png(tmp_path / "a.png"))
    assert analysis.scene_nsfw is None
    assert default_engine().decide(analysis).decision is Decision.SAFE


def test_an_analysis_without_the_stage_is_unchanged():
    # the pre-existing shape of MediaAnalysis must still behave identically
    res = default_engine().decide(MediaAnalysis(ok=True, detections=[], frames_checked=1))
    assert res.decision is Decision.SAFE
    assert res.scene_nsfw is None


# ------------------------------------------------------- startup safety
def break_transformers(monkeypatch):
    """Make ``from transformers import pipeline`` raise.

    The local ``import transformers`` name and ``sys.modules["transformers"]``
    are not always the same object for this lazily-imported package, so the
    import system's view (``sys.modules``) is what has to be replaced.
    """
    import sys
    import types

    def boom(*a, **k):
        raise RuntimeError("no model for you")

    fake = types.ModuleType("transformers")
    fake.pipeline = boom
    monkeypatch.setitem(sys.modules, "transformers", fake)


def test_scene_stage_disabled_does_not_load_anything(monkeypatch):
    monkeypatch.setattr(config, "SCENE_ENABLED", False)
    detector._load_scene()  # must not raise
    assert detector._scene_pipe is None


def test_scene_load_failure_is_not_fatal(monkeypatch):
    break_transformers(monkeypatch)
    monkeypatch.setattr(config, "SCENE_ENABLED", True)

    detector._load_scene()  # must not raise

    assert detector._scene_pipe is None
    assert detector._scene_error


def test_load_model_survives_a_scene_stage_failure(monkeypatch):
    break_transformers(monkeypatch)
    monkeypatch.setattr(config, "SCENE_ENABLED", True)
    before = detector._detector
    try:
        detector.load_model()  # must not raise: startup stays up
    finally:
        detector._detector = before
    assert detector._scene_pipe is None
