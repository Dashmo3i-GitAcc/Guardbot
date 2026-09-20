"""Local explicit-content detection. Nothing leaves the server.

Primary detector: NudeNet (YOLOv8-based ONNX model, 320px, CPU-only). It
reports explicit *body-region* classes, including:

    FEMALE_GENITALIA_EXPOSED
    MALE_GENITALIA_EXPOSED
    ANUS_EXPOSED

Those classes are the actual model outputs - they are not synthesised here.

- Images are classified directly.
- Video / GIF / animated video stickers (mp4/webm) are decoded with ffmpeg,
  N frames are sampled and every frame is scored. The strongest detection per
  class across all frames is kept, so explicit content that only appears in
  one frame is still seen, while a single weak frame cannot dominate.

This module only produces raw detections. The moderation *policy* (which
classes and which confidence count as EXPLICIT) lives in decision.py, so the
detector can be swapped or extended without touching the policy.

Second stage (optional, REVIEW-only)
------------------------------------
NudeNet sees explicit *body regions* only. It cannot see a sexual act when no
genitalia are visible, which is a real gap. When ``GENERIC_NSFW_ENABLED`` is
on, a local scene-level NSFW classifier scores the media on top of NudeNet and
the result is put in ``MediaAnalysis.generic_nsfw``.

That score is an auxiliary signal: decision.py can raise REVIEW with it but it
can **never** produce EXPLICIT, so the second stage cannot cause a deletion.
It is scored on exactly one frame per media item to bound the CPU cost, and it
fails open - a missing model, a missing dependency or an inference error simply
leaves ``generic_nsfw`` as None.
"""
import logging
import os
import subprocess
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("detector")

_detector = None
_load_error: str | None = None
_generic_pipe = None
_generic_error: str | None = None


@dataclass(frozen=True)
class Detection:
    """A single raw detection from the model."""

    label: str
    score: float
    box: tuple[int, int, int, int] | None = None


@dataclass
class FrameAnalysis:
    """Detections for one decoded frame (or the single image)."""

    path: str
    detections: list[Detection]


@dataclass
class MediaAnalysis:
    """Aggregated detector output for one media item.

    ``ok`` is False when the media could not be decoded or the detector
    errored. Callers must treat ``ok=False`` as "unknown" and fail open.
    """

    ok: bool
    detections: list[Detection] = field(default_factory=list)
    frames_checked: int = 0
    # Per-frame detail, kept so the caller can pick a representative frame as
    # evidence for the moderation report. Frame files are owned by the caller.
    frames: list[FrameAnalysis] = field(default_factory=list)
    # Optional auxiliary signal. It is never a deletion trigger (see
    # decision.py) and is None when no generic classifier is wired in.
    generic_nsfw: float | None = None
    error: str = ""
    note: str = ""

    def strongest(self) -> Detection | None:
        return max(self.detections, key=lambda d: d.score) if self.detections else None

    def evidence_frame(self, label: str) -> str | None:
        """Path of the frame with the highest score for ``label``.

        This is the frame the decision was actually based on, so it is the
        most useful still image to attach to a moderation report.
        """
        best_path: str | None = None
        best_score = -1.0
        for frame in self.frames:
            for d in frame.detections:
                if d.label == label and d.score > best_score:
                    best_score, best_path = d.score, frame.path
        return best_path

    def detections_summary(self) -> str:
        """Compact metadata string of every detected class + score, for logging.

        Only class names and scores are included - never media.

            "n/a"   - analysis failed, so there is no detection data
            "none"  - analysis ran and the detector returned nothing
            "CLS:0.87,OTHER:0.42" - all detections, strongest first

        This makes "no detections" distinguishable from "detections existed but
        none belonged to the explicit classes".
        """
        if not self.ok:
            return "n/a"
        if not self.detections:
            return "none"
        ordered = sorted(self.detections, key=lambda d: d.score, reverse=True)
        return ",".join(f"{d.label}:{d.score:.2f}" for d in ordered)


def load_model() -> None:
    """Load the detectors once at startup so the first media is not slow."""
    global _detector, _load_error
    from nudenet import NudeDetector

    log.info("Loading explicit-content detector (NudeNet 320n, CPU) ...")
    try:
        _detector = NudeDetector()
        _load_error = None
        log.info("Detector ready.")
    except Exception as e:  # pragma: no cover - depends on environment
        _detector = None
        _load_error = str(e)
        log.exception("Detector failed to load; media checks will fail open")

    _load_generic()


def _load_generic() -> None:
    """Load the optional second-stage scene classifier (REVIEW-only).

    Never fatal: any failure leaves ``_generic_pipe`` as None and the stage is
    skipped, exactly like a detector error failing open.
    """
    global _generic_pipe, _generic_error
    if not config.GENERIC_NSFW_ENABLED:
        _generic_pipe = None
        _generic_error = None
        log.info("Second-stage scene classifier disabled (GENERIC_NSFW_ENABLED=false)")
        return
    try:
        import torch  # noqa: F401  (imported for the thread setting below)
        from transformers import pipeline

        # One thread per inference. With MEDIA_WORKERS workers this bounds the
        # total CPU the auxiliary stage can take instead of letting every call
        # spawn its own intra-op pool on a 2-core box.
        try:
            torch.set_num_threads(1)
        except Exception:  # pragma: no cover - depends on build
            pass

        log.info(
            "Loading second-stage scene classifier (%s, CPU) ...",
            config.GENERIC_NSFW_MODEL,
        )
        _generic_pipe = pipeline(
            "image-classification", model=config.GENERIC_NSFW_MODEL, device=-1
        )
        _generic_error = None
        log.info("Scene classifier ready (REVIEW-only, never deletes).")
    except Exception as e:
        _generic_pipe = None
        _generic_error = str(e)
        log.exception(
            "Scene classifier failed to load; the REVIEW-only stage will be skipped"
        )


def _generic_score(path: str) -> float | None:
    """Scene-level NSFW probability for one frame, or None.

    Auxiliary only: decision.py may raise REVIEW with it but it can never
    produce EXPLICIT. Any failure returns None (fail open).
    """
    if _generic_pipe is None:
        return None
    try:
        from PIL import Image

        with Image.open(path) as img:
            results = _generic_pipe(img.convert("RGB"), top_k=None)
        for r in results:
            if str(r.get("label", "")).lower() == "nsfw":
                return float(r["score"])
        return 0.0
    except Exception as e:
        log.warning("scene classifier failed: %s", e)
        return None


def _detect_file(path: str) -> list[Detection]:
    if _detector is None:
        raise RuntimeError(_load_error or "detector not loaded")
    raw = _detector.detect(path)
    detections = []
    for d in raw:
        box = d.get("box")
        detections.append(
            Detection(
                label=str(d["class"]),
                score=float(d["score"]),
                box=tuple(int(v) for v in box) if box else None,
            )
        )
    return detections


def _merge_best(best: dict[str, Detection], found: list[Detection]) -> None:
    """Keep only the highest-scoring detection per class."""
    for d in found:
        cur = best.get(d.label)
        if cur is None or d.score > cur.score:
            best[d.label] = d


def _duration(path: str) -> float:
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


def extract_frames(video_path: str, out_dir: str, n: int) -> list[str]:
    """Pull n frames spread evenly across the video."""
    dur = _duration(video_path)
    if dur <= 0:
        times = [0.0]
    else:
        # sample at 10%..90% so we skip black intro/outro frames
        times = [dur * (0.1 + 0.8 * i / max(n - 1, 1)) for i in range(n)]

    frames = []
    for i, t in enumerate(times):
        out = os.path.join(out_dir, f"f{i}.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{t:.2f}", "-i", video_path,
                    "-frames:v", "1", "-vf", "scale='min(640,iw)':-2", out,
                ],
                check=True, timeout=30,
            )
            if os.path.exists(out):
                frames.append(out)
        except Exception as e:
            log.warning("frame %d failed: %s", i, e)
    return frames


def analyze_image(path: str) -> MediaAnalysis:
    """Classify a still image (photo, static sticker, image document)."""
    try:
        found = _detect_file(path)
    except Exception as e:
        log.warning("image analysis failed: %s", e)
        return MediaAnalysis(ok=False, error=str(e), note="image decode/detect failed")
    return MediaAnalysis(
        ok=True,
        detections=found,
        frames_checked=1,
        frames=[FrameAnalysis(path, found)],
        generic_nsfw=_generic_score(path),
    )


def _representative_frame(frames: list[FrameAnalysis], paths: list[str]) -> str:
    """The single frame worth spending the auxiliary stage on.

    The frame with the strongest NudeNet detection is the most informative; if
    no frame produced any detection, the first frame is used. Only one frame is
    ever scored, so the second stage costs one inference per media item.
    """
    best_path, best_score = None, -1.0
    for frame in frames:
        top = max((d.score for d in frame.detections), default=0.0)
        if top > best_score:
            best_score, best_path = top, frame.path
    return best_path or paths[0]


def analyze_video(path: str, work_dir: str) -> MediaAnalysis:
    """Classify a video / GIF / animated video sticker (mp4 or webm).

    Frames are written into ``work_dir``, which is owned by the caller: this
    function never deletes them, so the caller can use one as evidence before
    removing the whole directory.
    """
    try:
        frames = extract_frames(path, work_dir, config.VIDEO_FRAMES)
        if not frames:
            return MediaAnalysis(
                ok=False, error="no frames extracted", note="video decode failed"
            )
        best: dict[str, Detection] = {}
        frame_results: list[FrameAnalysis] = []
        failures = 0
        for f in frames:
            try:
                found = _detect_file(f)
            except Exception as e:
                failures += 1
                log.warning("frame scoring failed: %s", e)
                continue
            _merge_best(best, found)
            frame_results.append(FrameAnalysis(f, found))
        if failures == len(frames):
            return MediaAnalysis(
                ok=False, error="all frames failed", note="detector error"
            )
        return MediaAnalysis(
            ok=True,
            detections=list(best.values()),
            frames_checked=len(frames),
            frames=frame_results,
            generic_nsfw=_generic_score(_representative_frame(frame_results, frames)),
        )
    except Exception as e:
        log.warning("video analysis failed: %s", e)
        return MediaAnalysis(ok=False, error=str(e), note="video analysis failed")
