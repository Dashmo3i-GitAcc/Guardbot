"""Local NSFW detection. Nothing leaves the server.

- Images: classified directly.
- Video / GIF / animated stickers (webm/tgs-converted): N frames are pulled
  with ffmpeg and the WORST (highest) score wins.
"""
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass

from PIL import Image

from . import config

log = logging.getLogger("detector")

_pipe = None


def load_model() -> None:
    """Load once at startup so the first media isn't slow."""
    global _pipe
    from transformers import pipeline

    log.info("Loading NSFW model %s ...", config.NSFW_MODEL)
    _pipe = pipeline("image-classification", model=config.NSFW_MODEL, device=-1)
    log.info("Model ready.")


@dataclass
class Verdict:
    score: float          # 0..1, higher = more likely NSFW
    frames_checked: int
    note: str = ""


def _score_image(path: str) -> float:
    img = Image.open(path).convert("RGB")
    results = _pipe(img, top_k=None)
    for r in results:
        if r["label"].lower() == "nsfw":
            return float(r["score"])
    return 0.0


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


def analyze_image(path: str) -> Verdict:
    try:
        return Verdict(_score_image(path), 1)
    except Exception as e:
        log.exception("image analysis failed")
        return Verdict(0.0, 0, note=f"error: {e}")


def analyze_video(path: str) -> Verdict:
    """Also used for GIFs (Telegram sends them as mp4) and webm stickers."""
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as d:
        frames = extract_frames(path, d, config.VIDEO_FRAMES)
        if not frames:
            return Verdict(0.0, 0, note="no frames extracted")
        worst = 0.0
        for f in frames:
            try:
                worst = max(worst, _score_image(f))
            except Exception:
                log.exception("frame scoring failed")
        return Verdict(worst, len(frames))
