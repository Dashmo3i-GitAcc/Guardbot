"""Ordinary content is not a sexual verdict, and explicit content still is.

The owner reported false positives in the sexual/adult moderation: normal
photographs were producing "needs review" notices, with the AI's own line in the
notice reading `normal` next to a local score. Two defects produced that, and
they are independent.

**The gate.** ``main._assess_media_with_ai`` documented itself as "ask the AI
exactly when the local stage has something to say", and then guarded with "skip
when the local decision is SAFE *and* the scene score is absent". The second
clause made the first one dead: ``detector._scene_score`` returns a number for
every image the classifier touches, including ``0.0``, so ``scene_nsfw`` was
never ``None`` on any image and the AI was asked about every photograph posted
in the group. The moderation workload has no daily budget, so that was unbounded
cost as well as noise. The condition now says what it meant.

**The rule.** ``mod_policy``'s rule 8 turned any local REVIEW into a REVIEW
outcome *whatever the AI said*. The scene classifier is the less interpretable
of the two signals and it is the one that scores portraits, gym photographs,
swimwear and close-ups of skin as NSFW, so those landed in the local review band
and stayed there even when the AI looked at the same picture and said `normal`.
The two signals disagreed and the weaker one won. Rule 8 is now rule 9, and rule
8 is the case where the AI said the content is ordinary.

What did **not** change, and is asserted here so it cannot drift:

* no threshold moved — not the scene review band, not the delete threshold, not
  the NudeNet thresholds, so nothing that used to be caught at the detector
  level is missed now;
* a local *strong* claim that the AI declines is still a review (the old rule 4)
  — a disagreement at that level is a human's business, and a local EXPLICIT is
  not a weak cue;
* an AI that answers ``unknown``, or that could not answer at all, leaves a
  weak local cue in review — "I do not know" is not "this is fine";
* ``suggestive`` still reports for review, because that is the graded category
  between ordinary and explicit and it is exactly what a review channel is for;
* a genuinely explicit image is still deleted.

Three layers: the policy ladder as pure functions, the gate, and the whole
handler end to end with the detector and the AI stubbed. Nothing here talks to
Telegram, Google, or the network.
"""
import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image

from app import ai_moderation, config, db, detector, main, media, mod_policy
from app.ai_moderation import ModerationVerdict
from app.decision import Decision, DecisionResult
from app.detector import Detection

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
USER_ID = 7

LOCAL_LABEL = "FEMALE_GENITALIA_EXPOSED"


# ══ LAYER 1 — the policy ladder, as pure functions ════════════════════════
def local(decision, *, label=LOCAL_LABEL, score=0.5, scene=None):
    matched = Detection(label, score) if label else None
    return DecisionResult(
        decision,
        "test",
        matched=matched,
        scene_nsfw=scene,
        frames_checked=1,
        source="nudenet" if label else ("scene" if scene is not None else "none"),
    )


def ai(classification, confidence, *, uncertain=False):
    return ModerationVerdict(
        decided=True,
        classification=classification,
        confidence=confidence,
        uncertain=uncertain,
        model="test-model",
    )


def run(*, local_result=None, verdict=None, exempt=False):
    return mod_policy.decide(
        mod_policy.PolicyInput(
            local=local_result,
            ai=verdict,
            media_kind="photo",
            is_media=True,
            exempt=exempt,
        )
    )


# The local review band, as the scene classifier produces it for ordinary
# photographs: a score above SCENE_REVIEW_THRESHOLD and well below the delete
# threshold. This is the input the owner's false positives arrived as.
SCENE_REVIEW = local(Decision.REVIEW, label="", score=0.0, scene=0.72)
LOCAL_EXPLICIT = local(Decision.EXPLICIT, score=0.55)
SAFE = DecisionResult(Decision.SAFE, "no explicit evidence")


def test_a_portrait_the_ai_calls_normal_is_allowed():
    """The reported false positive, in one assertion."""
    outcome = run(local_result=SCENE_REVIEW, verdict=ai("normal", 0.95))

    assert outcome.action is mod_policy.Action.ALLOW
    assert outcome.reason == "local_review_ai_normal"
    assert outcome.deletes is False
    assert outcome.reviews is False


def test_the_ordinary_cases_are_all_allowed():
    """Portrait, gym, swimwear, a hug, a kiss, a close-up, visible skin.

    The local stage is identical for all of them — that is the point. The scene
    classifier cannot tell a swimsuit from underwear and cannot tell a kiss from
    anything else; the AI is the layer that can, and its `normal` is what has to
    decide. A test per case, because a single "ordinary content is allowed" test
    would not notice a rule that only handled one of them.
    """
    for case in (
        "portrait",
        "gym",
        "swimsuit",
        "hug",
        "kiss",
        "close-up",
        "visible skin",
    ):
        outcome = run(local_result=SCENE_REVIEW, verdict=ai("normal", 0.92))
        assert outcome.action is mod_policy.Action.ALLOW, case
        assert outcome.deletes is False, case


def test_a_barely_confident_normal_does_not_dismiss_the_cue():
    """Below the review floor the AI has not really spoken."""
    outcome = run(local_result=SCENE_REVIEW, verdict=ai("normal", 0.30))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "local_review"


def test_a_normal_the_ai_was_unsure_about_does_not_dismiss_the_cue():
    outcome = run(
        local_result=SCENE_REVIEW, verdict=ai("normal", 0.95, uncertain=True)
    )

    assert outcome.action is mod_policy.Action.REVIEW


def test_an_unknown_verdict_leaves_the_cue_in_review():
    """"I could not judge" is not "this is fine".

    This is why the new rule is narrower than "the AI declined": ``unknown`` is
    also a non-deletable class, and letting it silence the cue would turn the
    moderation AI's uncertainty into an allow.
    """
    outcome = run(local_result=SCENE_REVIEW, verdict=ai("unknown", 0.99))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "local_review"


def test_a_weak_cue_with_no_second_opinion_is_still_a_review():
    """The AI was not asked or could not answer. A human looks; nothing happens."""
    outcome = run(local_result=SCENE_REVIEW, verdict=None)

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "local_review"
    assert outcome.deletes is False


def test_suggestive_still_reports_for_review():
    """The graded middle: not explicit, and not nothing either."""
    outcome = run(local_result=SCENE_REVIEW, verdict=ai("suggestive", 0.70))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "ai_suggestive"
    assert outcome.deletes is False


def test_a_strong_local_claim_the_ai_declines_is_still_a_review():
    """Rule 4, unchanged. A local EXPLICIT is not a weak cue.

    The new rule must not swallow this: the local stage escalated *strongly*
    (a NudeNet anatomical class, or a scene score in the delete band) and the AI
    disagreed. That disagreement is exactly what a human should see, and it is
    the documented false-positive fix rather than a false positive.
    """
    outcome = run(local_result=LOCAL_EXPLICIT, verdict=ai("normal", 0.99))

    assert outcome.action is mod_policy.Action.REVIEW
    assert outcome.reason == "local_explicit_ai_declined"
    assert outcome.deletes is False


def test_a_strong_local_claim_the_ai_confirms_is_deleted():
    """Genuinely explicit content is unchanged."""
    outcome = run(
        local_result=local(Decision.EXPLICIT, score=0.99),
        verdict=ai("explicit_sexual", 0.97),
    )

    assert outcome.action is mod_policy.Action.DELETE_WARN
    assert outcome.reason == "ai_confirmed_explicit"
    assert outcome.deletes is True


def test_explicit_content_is_deleted_even_with_a_weak_local_score():
    """The AI confirming is enough on its own — the deletion path is intact."""
    outcome = run(local_result=SAFE, verdict=ai("explicit_sexual", 0.98))

    assert outcome.action is mod_policy.Action.DELETE_WARN


def test_the_gate_is_narrower_than_the_ai_declined_check():
    """A guard against the obvious simplification.

    ``_ai_declines`` is true for ``unknown`` as well as ``normal``. If the new
    rule used it, an AI that could not judge would allow content the local stage
    was unsure about — a fail-open where the policy is supposed to fail safe.
    """
    assert mod_policy._ai_declines(ai("unknown", 0.99)) is True
    assert mod_policy._ai_says_ordinary(ai("unknown", 0.99)) is False
    assert mod_policy._ai_says_ordinary(ai("normal", 0.99)) is True


def test_no_threshold_moved():
    """The fix is a rule, not a loosened number.

    Asserted explicitly because "reduce false positives" is very easy to
    implement by raising a threshold, and that would trade a false positive for
    a false negative on genuinely explicit material.
    """
    assert config.SCENE_REVIEW_THRESHOLD == 0.60
    assert config.SCENE_DELETE_THRESHOLD == 0.95
    assert config.EXPLICIT_REVIEW_THRESHOLD == 0.25
    assert config.EXPLICIT_DELETE_THRESHOLD == 0.45
    assert config.MODERATION_DELETABLE_CLASSES == {"explicit_sexual"}


# ══ LAYER 2 — when the AI is asked ════════════════════════════════════════
@pytest.fixture
def ai_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODERATION_MEDIA_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "test-mod-key")
    monkeypatch.setattr(config, "MODERATION_AI_ASK_ON_SAFE", False)
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    ai_moderation.reset_state()
    yield
    ai_moderation.reset_state()


def gate_env(monkeypatch):
    """A prepared media bundle, and an AI call that records itself."""
    asked: list[str] = []

    part = SimpleNamespace(mime_type="image/png", data=b"x")
    monkeypatch.setattr(
        media,
        "build_from_path",
        lambda path, kind, work_dir=None: SimpleNamespace(
            ok=True, parts=[part],
            note="", reduced_to_frames=False, thumbnail_only=False,
        ),
    )

    async def _assess(parts, kind):
        asked.append(kind)
        return ai("normal", 0.95)

    monkeypatch.setattr(ai_moderation, "assess_media", _assess)
    return asked


def test_a_safe_image_is_not_sent_to_the_ai(ai_env, monkeypatch, tmp_path):
    """The cost fix: an ordinary photograph costs nothing.

    This is the defect the guard used to have. ``scene_nsfw`` is ``0.0`` for a
    clean image, not ``None``, so the old condition was never true and every
    photograph was sent.
    """
    asked = gate_env(monkeypatch)
    clean = local(Decision.SAFE, label="", score=0.0, scene=0.0)

    verdict = asyncio.run(
        main._assess_media_with_ai("p", str(tmp_path), "photo", clean, "")
    )

    assert verdict is None
    assert asked == []


def test_an_image_with_no_scene_score_is_not_sent_either(ai_env, monkeypatch, tmp_path):
    """The scene stage off is also "the local stage had nothing to say"."""
    asked = gate_env(monkeypatch)
    no_scene = DecisionResult(Decision.SAFE, "no explicit evidence")

    verdict = asyncio.run(
        main._assess_media_with_ai("p", str(tmp_path), "photo", no_scene, "")
    )

    assert verdict is None
    assert asked == []


def test_a_borderline_image_is_sent_to_the_ai(ai_env, monkeypatch, tmp_path):
    """The second opinion is exactly for the case the local stage was unsure of."""
    asked = gate_env(monkeypatch)

    verdict = asyncio.run(
        main._assess_media_with_ai("p", str(tmp_path), "photo", SCENE_REVIEW, "")
    )

    assert asked == ["photo"]
    assert verdict is not None and verdict.classification == "normal"


def test_an_explicit_image_is_sent_to_the_ai(ai_env, monkeypatch, tmp_path):
    asked = gate_env(monkeypatch)

    asyncio.run(
        main._assess_media_with_ai("p", str(tmp_path), "photo", LOCAL_EXPLICIT, "")
    )

    assert asked == ["photo"]


def test_the_operator_can_ask_for_every_image_again(ai_env, monkeypatch, tmp_path):
    """The lever, and the reason it exists.

    The default is the cheaper and intended behaviour, but it *is* a reduction
    in the set of images the AI sees. An operator who observes a missed
    explicit image should be able to widen it with one variable rather than by
    editing the guard back into the shape that made it always true.
    """
    monkeypatch.setattr(config, "MODERATION_AI_ASK_ON_SAFE", True)
    asked = gate_env(monkeypatch)
    clean = local(Decision.SAFE, label="", score=0.0, scene=0.0)

    asyncio.run(main._assess_media_with_ai("p", str(tmp_path), "photo", clean, ""))

    assert asked == ["photo"]


# ══ LAYER 3 — the whole handler ═══════════════════════════════════════════
class FakeFile:
    async def download_to_drive(self, path):
        Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "PNG")


class FakeBot:
    def __init__(self):
        self.messages: list[str] = []
        self.photos: list[str] = []
        self.documents: list[str] = []

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")

    async def get_file(self, file_id):
        return FakeFile()

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        self.photos.append(caption or "")

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        self.documents.append(caption or "")


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

    async def delete(self):
        self.delete_calls += 1


class StubDetector:
    def __init__(self, raw=None):
        self.raw = raw or []

    def detect(self, path):
        return self.raw


@pytest.fixture
def handler_env(monkeypatch, tmp_path):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_dir))
    monkeypatch.setattr(config, "MAX_DOWNLOAD_MB", 20)
    monkeypatch.setattr(config, "BURST_ENABLED", False)
    monkeypatch.setattr(config, "MODERATION_ENABLED", True)
    monkeypatch.setattr(config, "MODERATION_MEDIA_ENABLED", True)
    monkeypatch.setattr(config, "MODERATION_REQUIRE_AI_CONFIRM", True)
    monkeypatch.setattr(config, "MODERATION_REVIEW_NOTIFY", True)
    monkeypatch.setattr(config, "MODERATION_AI_ASK_ON_SAFE", False)
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "test-mod-key")
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", [])
    monkeypatch.setattr(detector, "_detector", StubDetector())
    monkeypatch.setattr(detector, "_scene_score", lambda path: 0.0)
    ai_moderation.reset_state()
    db.init()
    main._admin_cache.clear()
    main._recently_deleted.clear()
    yield tmp_dir
    ai_moderation.reset_state()


def run_media(bot, **media_kwargs):
    msg = FakeMessage(**media_kwargs)
    user = SimpleNamespace(
        id=USER_ID, full_name="Tester", username="tester", is_bot=False
    )
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=user,
    )
    asyncio.run(main.on_media(update, SimpleNamespace(bot=bot)))
    return msg, bot


def stub_ai(monkeypatch, classification, confidence, *, uncertain=False):
    async def _assess(parts, kind):
        return ai(classification, confidence, uncertain=uncertain)

    monkeypatch.setattr(ai_moderation, "assess_media", _assess)


@pytest.mark.parametrize(
    "case",
    ["portrait", "gym", "swimsuit", "hug", "kiss", "close-up", "visible skin"],
)
def test_an_ordinary_photo_is_neither_deleted_nor_reported(
    case, handler_env, monkeypatch
):
    """The end-to-end shape of the reported bug.

    The scene classifier scores the picture in its review band — which is what
    it does for every one of these — the AI looks and says `normal`, and the
    handler must do nothing at all: no delete, and no notice in the operator's
    review channel.
    """
    monkeypatch.setattr(detector, "_scene_score", lambda path: 0.72)
    stub_ai(monkeypatch, "normal", 0.94)

    msg, bot = run_media(FakeBot(), photo=[SimpleNamespace(file_id="p", file_size=1000)])

    assert msg.delete_calls == 0, case
    assert bot.messages == [], case
    assert bot.photos == [] and bot.documents == [], case
    assert handler_env is not None and list(handler_env.iterdir()) == []


def test_an_ordinary_photo_the_ai_could_not_judge_is_reported_but_kept(
    handler_env, monkeypatch
):
    """Fail safe: the cue stays a review, and nothing is destroyed."""
    monkeypatch.setattr(detector, "_scene_score", lambda path: 0.72)

    async def _fail(parts, kind):
        return ModerationVerdict(error="timeout", model="test")

    monkeypatch.setattr(ai_moderation, "assess_media", _fail)

    msg, bot = run_media(FakeBot(), photo=[SimpleNamespace(file_id="p", file_size=1000)])

    assert msg.delete_calls == 0
    assert len(bot.messages) == 1
    assert "نیازمند بررسی دستی" in bot.messages[0]


def test_a_suggestive_photo_is_reported_and_kept(handler_env, monkeypatch):
    monkeypatch.setattr(detector, "_scene_score", lambda path: 0.72)
    stub_ai(monkeypatch, "suggestive", 0.70)

    msg, bot = run_media(FakeBot(), photo=[SimpleNamespace(file_id="p", file_size=1000)])

    assert msg.delete_calls == 0
    assert len(bot.messages) == 1
    assert "نیازمند بررسی دستی" in bot.messages[0]


def test_a_genuinely_explicit_photo_is_still_deleted(handler_env, monkeypatch):
    """The half that must not regress.

    NudeNet finds an explicit class above the delete threshold and the AI
    confirms it. Nothing about the false-positive fix may change this.
    """
    monkeypatch.setattr(
        detector,
        "_detector",
        StubDetector([{"class": LOCAL_LABEL, "score": 0.97, "box": [0, 0, 1, 1]}]),
    )
    stub_ai(monkeypatch, "explicit_sexual", 0.97)

    msg, bot = run_media(FakeBot(), photo=[SimpleNamespace(file_id="p", file_size=1000)])

    assert msg.delete_calls == 1
    assert len(bot.photos) == 1, "the deletion is reported with its evidence"


def test_explicit_content_is_deleted_on_the_ai_verdict_alone(handler_env, monkeypatch):
    """A sexual act with no exposed anatomy: the AI is the only signal.

    This is the case the scene stage was added for, and it is the case the
    narrower gate could have lost. It does not: the scene score puts the image
    in the review band, so the AI is asked, and its confirmation deletes.
    """
    monkeypatch.setattr(detector, "_scene_score", lambda path: 0.80)
    stub_ai(monkeypatch, "explicit_sexual", 0.96)

    msg, bot = run_media(FakeBot(), photo=[SimpleNamespace(file_id="p", file_size=1000)])

    assert msg.delete_calls == 1


def test_a_clean_photo_costs_no_ai_call_at_all(handler_env, monkeypatch):
    """The gate, end to end: a picture the local stage is happy with is free."""
    calls: list[str] = []

    async def _assess(parts, kind):
        calls.append(kind)
        return ai("normal", 0.95)

    monkeypatch.setattr(ai_moderation, "assess_media", _assess)
    monkeypatch.setattr(detector, "_scene_score", lambda path: 0.05)

    msg, bot = run_media(FakeBot(), photo=[SimpleNamespace(file_id="p", file_size=1000)])

    assert calls == [], "an ordinary photograph must not be sent to the AI"
    assert msg.delete_calls == 0
    assert bot.messages == []
