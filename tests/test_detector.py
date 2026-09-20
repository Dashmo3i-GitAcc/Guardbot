"""Detector-level tests: parsing, fail-open behaviour, media-type allow rules."""
import asyncio

from app import detector
from app.decision import Decision, default_engine
from app.detector import Detection, MediaAnalysis
from app.moderation import enforce


class StubDetector:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def detect(self, path):
        if self.error is not None:
            raise self.error
        return self.result


def run(coro):
    return asyncio.run(coro)


# 8. media decoding failure -> fail open
def test_media_decode_failure_fails_open(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(error=ValueError("cannot decode")))
    result = detector.analyze_image("/tmp/not-an-image")
    assert result.ok is False
    assert default_engine().decide(result).decision is Decision.SAFE


# 7. detector exception -> fail open
def test_detector_exception_fails_open(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(error=RuntimeError("onnx boom")))
    result = detector.analyze_image("/tmp/x.jpg")
    assert result.ok is False
    assert default_engine().decide(result).decision is Decision.SAFE


def test_detector_not_loaded_fails_open(monkeypatch):
    monkeypatch.setattr(detector, "_detector", None)
    result = detector.analyze_image("/tmp/x.jpg")
    assert result.ok is False
    assert default_engine().decide(result).decision is Decision.SAFE


def test_raw_detections_are_parsed(monkeypatch):
    raw = [{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.91, "box": [1, 2, 3, 4]}]
    monkeypatch.setattr(detector, "_detector", StubDetector(result=raw))
    result = detector.analyze_image("/tmp/x.jpg")
    assert result.ok is True
    assert result.frames_checked == 1
    assert result.detections == [Detection("FEMALE_GENITALIA_EXPOSED", 0.91, (1, 2, 3, 4))]


def test_merge_best_keeps_highest_per_class():
    best = {}
    detector._merge_best(best, [Detection("ANUS_EXPOSED", 0.30)])
    detector._merge_best(best, [Detection("ANUS_EXPOSED", 0.70), Detection("FACE_MALE", 0.5)])
    detector._merge_best(best, [Detection("ANUS_EXPOSED", 0.50)])
    assert best["ANUS_EXPOSED"].score == 0.70
    assert best["FACE_MALE"].score == 0.5


def test_video_with_no_frames_fails_open(monkeypatch):
    monkeypatch.setattr(detector, "extract_frames", lambda *a, **k: [])
    result = detector.analyze_video("/tmp/x.mp4")
    assert result.ok is False
    assert default_engine().decide(result).decision is Decision.SAFE


def test_video_uses_strongest_frame(monkeypatch, tmp_path):
    frames = []
    for i in range(3):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"x")
        frames.append(str(p))
    monkeypatch.setattr(detector, "extract_frames", lambda *a, **k: frames)

    class Seq:
        def __init__(self):
            self.n = 0

        def detect(self, path):
            self.n += 1
            if self.n == 2:  # only the middle frame is explicit
                return [{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.88, "box": [0, 0, 1, 1]}]
            return []

    monkeypatch.setattr(detector, "_detector", Seq())
    result = detector.analyze_video("/tmp/x.mp4")
    assert result.ok is True
    assert result.frames_checked == 3
    assert default_engine().decide(result).decision is Decision.EXPLICIT


# 9. normal sticker -> allowed
def test_normal_sticker_allowed(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(result=[]))
    analysis = detector.analyze_image("/tmp/sticker.webp")
    assert default_engine().decide(analysis).decision is Decision.SAFE


# 10. normal GIF -> allowed
def test_normal_gif_allowed(monkeypatch, tmp_path):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"x")
    monkeypatch.setattr(detector, "extract_frames", lambda *a, **k: [str(frame)])
    monkeypatch.setattr(detector, "_detector", StubDetector(result=[]))
    analysis = detector.analyze_video("/tmp/cat.gif")
    assert default_engine().decide(analysis).decision is Decision.SAFE


# 11. normal swimsuit image -> allowed
def test_swimsuit_image_allowed(monkeypatch):
    raw = [{"class": "FEMALE_GENITALIA_COVERED", "score": 0.95, "box": [0, 0, 1, 1]}]
    monkeypatch.setattr(detector, "_detector", StubDetector(result=raw))
    analysis = detector.analyze_image("/tmp/beach.jpg")
    assert default_engine().decide(analysis).decision is Decision.SAFE


def test_detections_summary_none_when_no_detections():
    assert MediaAnalysis(ok=True).detections_summary() == "none"


def test_detections_summary_na_when_analysis_failed():
    assert MediaAnalysis(ok=False, error="boom").detections_summary() == "n/a"


def test_detections_summary_lists_all_classes_strongest_first():
    analysis = MediaAnalysis(
        ok=True,
        detections=[
            Detection("FACE_MALE", 0.42, None),
            Detection("FEMALE_GENITALIA_COVERED", 0.87, None),
            Detection("FEMALE_BREAST_COVERED", 0.55, None),
        ],
        frames_checked=1,
    )
    assert (
        analysis.detections_summary()
        == "FEMALE_GENITALIA_COVERED:0.87,FEMALE_BREAST_COVERED:0.55,FACE_MALE:0.42"
    )


def test_non_explicit_detections_are_distinguishable_from_no_detections():
    """class=- / confidence=0.00 must not hide non-explicit detections."""
    empty = MediaAnalysis(ok=True, detections=[], frames_checked=1)
    covered = MediaAnalysis(
        ok=True,
        detections=[Detection("FEMALE_GENITALIA_COVERED", 0.71, None)],
        frames_checked=1,
    )
    # both produce class=- / confidence=0.00 in the log line ...
    assert empty.strongest() is None
    assert default_engine().decide(covered).matched is None
    # ... but the detections field tells them apart
    assert empty.detections_summary() == "none"
    assert covered.detections_summary() == "FEMALE_GENITALIA_COVERED:0.71"


# end-to-end for the explicit case: detector -> decision -> delete
def test_explicit_media_is_deleted_end_to_end(monkeypatch):
    raw = [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.97, "box": [0, 0, 5, 5]}]
    monkeypatch.setattr(detector, "_detector", StubDetector(result=raw))
    analysis = detector.analyze_image("/tmp/x.jpg")
    result = default_engine().decide(analysis)
    assert result.decision is Decision.EXPLICIT

    deleted = {"n": 0}

    async def delete():
        deleted["n"] += 1

    outcome = run(enforce(result, delete_media=delete, record_confirmed=lambda: 1))
    assert outcome.action == "deleted"
    assert deleted["n"] == 1
