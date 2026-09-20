"""Decision-engine policy tests.

No model is loaded here: we feed the engine synthetic detector output, which
is exactly the interface the real detector produces.
"""
from app.decision import Decision, DecisionEngine, default_engine
from app.detector import Detection, MediaAnalysis

EXPLICIT = frozenset(
    {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
)


def engine(**over) -> DecisionEngine:
    kw = dict(
        explicit_classes=EXPLICIT,
        explicit_threshold=0.80,
        review_threshold=0.40,
        generic_review_threshold=0.90,
    )
    kw.update(over)
    return DecisionEngine(**kw)


def analysis(detections=(), ok=True, generic=None, frames=1, error=""):
    return MediaAnalysis(
        ok=ok,
        detections=list(detections),
        frames_checked=frames,
        generic_nsfw=generic,
        error=error,
    )


# 1. clearly safe media -> SAFE
def test_safe_media_is_safe():
    assert engine().decide(analysis()).decision is Decision.SAFE


# 2. borderline score -> REVIEW
def test_borderline_score_is_review():
    dets = [Detection("FEMALE_GENITALIA_EXPOSED", 0.55)]
    assert engine().decide(analysis(dets)).decision is Decision.REVIEW


# 3. high-confidence explicit genital detection -> EXPLICIT
def test_high_confidence_genitalia_is_explicit():
    for label in ("FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"):
        res = engine().decide(analysis([Detection(label, 0.95)]))
        assert res.decision is Decision.EXPLICIT
        assert res.matched.label == label


# 4. high generic NSFW score without explicit genital evidence -> never EXPLICIT
def test_generic_nsfw_alone_never_explicit():
    res = engine().decide(analysis(generic=0.999))
    assert res.decision is Decision.REVIEW
    assert res.decision is not Decision.EXPLICIT


def test_generic_nsfw_below_threshold_is_safe():
    assert engine().decide(analysis(generic=0.5)).decision is Decision.SAFE


# covered / non-explicit classes must never be treated as explicit evidence
def test_covered_classes_are_safe():
    dets = [
        Detection("FEMALE_GENITALIA_COVERED", 0.99),
        Detection("FEMALE_BREAST_COVERED", 0.98),
        Detection("BUTTOCKS_COVERED", 0.97),
    ]
    assert engine().decide(analysis(dets)).decision is Decision.SAFE


# exposed non-genital classes are outside the configured evidence set
def test_exposed_but_not_configured_class_is_safe():
    dets = [Detection("FEMALE_BREAST_EXPOSED", 0.99)]
    assert engine().decide(analysis(dets)).decision is Decision.SAFE


# 7. detector error -> fail open (SAFE)
def test_detector_error_fails_open():
    res = engine().decide(analysis(ok=False, error="boom"))
    assert res.decision is Decision.SAFE
    assert "fail-open" in res.reason


# 13. ambiguous media is not automatically deleted
def test_ambiguous_media_is_review_not_explicit():
    dets = [Detection("MALE_GENITALIA_EXPOSED", 0.62)]
    assert engine().decide(analysis(dets)).decision is Decision.REVIEW


def test_threshold_boundaries():
    e = engine()
    # exactly at delete threshold -> EXPLICIT
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.80)])).decision is Decision.EXPLICIT
    # just below delete, above review -> REVIEW
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.79)])).decision is Decision.REVIEW
    # exactly at review threshold -> REVIEW
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.40)])).decision is Decision.REVIEW
    # just below review -> SAFE
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.39)])).decision is Decision.SAFE


def test_strongest_detection_wins():
    dets = [
        Detection("FEMALE_GENITALIA_EXPOSED", 0.42),
        Detection("MALE_GENITALIA_EXPOSED", 0.93),
        Detection("ANUS_EXPOSED", 0.30),
    ]
    res = engine().decide(analysis(dets))
    assert res.decision is Decision.EXPLICIT
    assert res.matched.label == "MALE_GENITALIA_EXPOSED"


def test_default_engine_uses_config_classes_and_thresholds():
    res = default_engine().decide(analysis([Detection("FEMALE_GENITALIA_EXPOSED", 0.97)]))
    assert res.decision is Decision.EXPLICIT
    # a class not in config.EXPLICIT_CLASSES can never delete
    res2 = default_engine().decide(analysis([Detection("FEMALE_BREAST_EXPOSED", 0.99)]))
    assert res2.decision is Decision.SAFE


# --- regression: live test on 2026-09-20 sent confirmed explicit media and
# --- everything landed in REVIEW because the delete threshold was 0.80.
def test_live_confirmed_explicit_samples_are_now_explicit():
    samples = [
        ("FEMALE_GENITALIA_EXPOSED", 0.50),
        ("MALE_GENITALIA_EXPOSED", 0.67),
        ("ANUS_EXPOSED", 0.56),
        ("MALE_GENITALIA_EXPOSED", 0.51),
    ]
    for label, score in samples:
        res = default_engine().decide(analysis([Detection(label, score)]))
        assert res.decision is Decision.EXPLICIT, f"{label} {score} -> {res.decision}"


def test_live_non_explicit_sample_stays_safe():
    """Real live sample with no explicit-region class must not be touched."""
    dets = [
        Detection("BELLY_EXPOSED", 0.82),
        Detection("FACE_FEMALE", 0.78),
        Detection("FEMALE_BREAST_EXPOSED", 0.77),
    ]
    assert default_engine().decide(analysis(dets)).decision is Decision.SAFE


def test_default_threshold_boundaries():
    e = default_engine()
    assert e.explicit_threshold == 0.45
    assert e.review_threshold == 0.25
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.45)])).decision is Decision.EXPLICIT
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.44)])).decision is Decision.REVIEW
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.25)])).decision is Decision.REVIEW
    assert e.decide(analysis([Detection("ANUS_EXPOSED", 0.24)])).decision is Decision.SAFE
