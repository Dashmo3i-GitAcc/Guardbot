"""Moderation policy: turn detector output into SAFE / REVIEW / EXPLICIT.

The policy is deliberately conservative and lives here (not in the Telegram
handlers) so it can be unit tested and tuned without touching the bot wiring.

    SAFE      - normal / non-explicit. Allow.
    REVIEW    - ambiguous / borderline / uncertain. Allow + log, never punish.
    EXPLICIT  - clearly visible explicit body-region content at high
                confidence. Delete.

Rules:

* Only classes listed in ``EXPLICIT_CLASSES`` (explicit body-region classes)
  can ever produce EXPLICIT.
* A generic NSFW score is only an auxiliary signal. It can raise REVIEW but
  can *never* produce EXPLICIT.
* A detector or decoding error (``MediaAnalysis.ok is False``) fails open to
  SAFE: uncertainty never leads to a deletion.
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
    generic_nsfw: float | None = None
    frames_checked: int = 0


class DecisionEngine:
    """Applies the SAFE / REVIEW / EXPLICIT policy to a MediaAnalysis."""

    def __init__(
        self,
        explicit_classes: frozenset[str],
        explicit_threshold: float,
        review_threshold: float,
        generic_review_threshold: float,
    ) -> None:
        self.explicit_classes = explicit_classes
        self.explicit_threshold = explicit_threshold
        self.review_threshold = review_threshold
        self.generic_review_threshold = generic_review_threshold

    def _explicit_evidence(self, analysis: MediaAnalysis) -> Detection | None:
        """Strongest detection whose class counts as explicit evidence."""
        candidates = [d for d in analysis.detections if d.label in self.explicit_classes]
        return max(candidates, key=lambda d: d.score) if candidates else None

    def decide(self, analysis: MediaAnalysis) -> DecisionResult:
        base = dict(generic_nsfw=analysis.generic_nsfw, frames_checked=analysis.frames_checked)

        if not analysis.ok:
            return DecisionResult(
                Decision.SAFE,
                f"fail-open: {analysis.error or 'analysis error'}",
                **base,
            )

        top = self._explicit_evidence(analysis)

        if top is not None and top.score >= self.explicit_threshold:
            return DecisionResult(
                Decision.EXPLICIT,
                f"{top.label} {top.score:.2f} >= {self.explicit_threshold:.2f}",
                matched=top,
                **base,
            )

        if top is not None and top.score >= self.review_threshold:
            return DecisionResult(
                Decision.REVIEW,
                f"borderline {top.label} {top.score:.2f}",
                matched=top,
                **base,
            )

        if analysis.generic_nsfw is not None and analysis.generic_nsfw >= self.generic_review_threshold:
            return DecisionResult(
                Decision.REVIEW,
                f"generic NSFW {analysis.generic_nsfw:.2f} without explicit-region evidence",
                **base,
            )

        return DecisionResult(Decision.SAFE, "no explicit evidence", **base)


def default_engine() -> DecisionEngine:
    return DecisionEngine(
        explicit_classes=frozenset(config.EXPLICIT_CLASSES),
        explicit_threshold=config.EXPLICIT_DELETE_THRESHOLD,
        review_threshold=config.EXPLICIT_REVIEW_THRESHOLD,
        generic_review_threshold=config.GENERIC_REVIEW_THRESHOLD,
    )
