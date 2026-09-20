"""Moderation policy: turn detector output into SAFE / REVIEW / EXPLICIT.

The policy is deliberately conservative and lives here (not in the Telegram
handlers) so it can be unit tested and tuned without touching the bot wiring.

    SAFE      - normal / non-explicit. Allow.
    REVIEW    - ambiguous / borderline / uncertain. Allow + log, never punish.
    EXPLICIT  - clearly sexual content at high confidence. Delete.

There are two independent evidence sources, and either can produce EXPLICIT:

1. NudeNet anatomical evidence - an explicit body-region class from
   ``EXPLICIT_CLASSES`` at or above ``EXPLICIT_DELETE_THRESHOLD``.
2. Scene-level sexual content - the second-stage classifier's NSFW score at or
   above ``SCENE_DELETE_THRESHOLD``, even when NudeNet found nothing. This is
   what catches a sexual act whose anatomical class is not detected.

Rules:

* Only classes listed in ``EXPLICIT_CLASSES`` can produce EXPLICIT from
  NudeNet. No other NudeNet class is ever evidence.
* The scene score is graded: below ``SCENE_REVIEW_THRESHOLD`` it is SAFE,
  at/above it the media is REVIEW (logged, never deleted), and at/above
  ``SCENE_DELETE_THRESHOLD`` it is EXPLICIT.
* A detector or decoding error (``MediaAnalysis.ok is False``) fails open to
  SAFE: uncertainty never leads to a deletion. The same holds for a missing
  scene score - it is ``None``, not zero, and ``None`` never deletes.
* ``DecisionResult.source`` records which signal decided, so operators can see
  whether a deletion came from NudeNet or from the scene stage.
"""
import logging
from dataclasses import dataclass
from enum import Enum

from . import config
from .detector import Detection, MediaAnalysis

log = logging.getLogger("decision")


class Decision(str, Enum):
    SAFE = "SAFE"
    REVIEW = "REVIEW"
    EXPLICIT = "EXPLICIT"


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    reason: str
    matched: Detection | None = None
    scene_nsfw: float | None = None
    frames_checked: int = 0
    # Which signal drove the decision: "nudenet", "scene" or "none".
    source: str = "none"


class DecisionEngine:
    """Applies the SAFE / REVIEW / EXPLICIT policy to a MediaAnalysis."""

    def __init__(
        self,
        explicit_classes: frozenset[str],
        explicit_threshold: float,
        review_threshold: float,
        scene_delete_threshold: float,
        scene_review_threshold: float,
    ) -> None:
        self.explicit_classes = explicit_classes
        self.explicit_threshold = explicit_threshold
        self.review_threshold = review_threshold
        self.scene_delete_threshold = scene_delete_threshold
        self.scene_review_threshold = scene_review_threshold

    def _explicit_evidence(self, analysis: MediaAnalysis) -> Detection | None:
        """Strongest detection whose class counts as explicit evidence."""
        candidates = [d for d in analysis.detections if d.label in self.explicit_classes]
        return max(candidates, key=lambda d: d.score) if candidates else None

    def decide(self, analysis: MediaAnalysis) -> DecisionResult:
        base = dict(
            scene_nsfw=analysis.scene_nsfw, frames_checked=analysis.frames_checked
        )

        if not analysis.ok:
            return DecisionResult(
                Decision.SAFE,
                f"fail-open: {analysis.error or 'analysis error'}",
                **base,
            )

        top = self._explicit_evidence(analysis)

        # 1. anatomical evidence (NudeNet) - strongest, most explainable signal
        if top is not None and top.score >= self.explicit_threshold:
            return DecisionResult(
                Decision.EXPLICIT,
                f"{top.label} {top.score:.2f} >= {self.explicit_threshold:.2f}",
                matched=top,
                source="nudenet",
                **base,
            )

        # 2. scene-level sexual content - deletes even with no anatomical match
        if (
            analysis.scene_nsfw is not None
            and analysis.scene_nsfw >= self.scene_delete_threshold
        ):
            return DecisionResult(
                Decision.EXPLICIT,
                f"scene NSFW {analysis.scene_nsfw:.2f} >= "
                f"{self.scene_delete_threshold:.2f} without explicit-region evidence",
                source="scene",
                **base,
            )

        # 3. anatomical evidence in the borderline band
        if top is not None and top.score >= self.review_threshold:
            return DecisionResult(
                Decision.REVIEW,
                f"borderline {top.label} {top.score:.2f}",
                matched=top,
                source="nudenet",
                **base,
            )

        # 4. mildly suggestive / ambiguous scene
        if (
            analysis.scene_nsfw is not None
            and analysis.scene_nsfw >= self.scene_review_threshold
        ):
            return DecisionResult(
                Decision.REVIEW,
                f"scene NSFW {analysis.scene_nsfw:.2f} in review band",
                source="scene",
                **base,
            )

        return DecisionResult(Decision.SAFE, "no explicit evidence", **base)


def default_engine() -> DecisionEngine:
    return DecisionEngine(
        explicit_classes=frozenset(config.EXPLICIT_CLASSES),
        explicit_threshold=config.EXPLICIT_DELETE_THRESHOLD,
        review_threshold=config.EXPLICIT_REVIEW_THRESHOLD,
        scene_delete_threshold=config.SCENE_DELETE_THRESHOLD,
        scene_review_threshold=config.SCENE_REVIEW_THRESHOLD,
    )
