"""One logical Gemini service, backed by many independent accounts.

Why this module exists
----------------------
The bot has four AI workloads (acquisition, conversation, moderation,
transcription) and, until now, one credential each. When a credential's quota
ran out the workload simply stopped, and the only way back was an operator
editing the environment and restarting the container. This module replaces that
with a pool: several credentials, each treated as its own account with its own
state, and a request that survives one of them running out.

The two levels of failover, which are *not* the same thing
----------------------------------------------------------
Google enforces limits at more than one granularity, and conflating them is the
mistake this module is written to avoid:

* **Level 1 — model.** ``gemini-flash-lite-latest`` may be rate-limited while the
  account behind it is perfectly healthy. The right response is another
  compatible model *on the same account*, and abandoning the account here would
  waste a resource that is still good.
* **Level 2 — account/project.** The project's quota may be exhausted, or the
  credential revoked. No amount of model switching helps; the right response is
  the next account.

So a model failure never disables an account, and an account failure is never
treated as a model problem. The two states are tracked separately (``Account``
and ``ModelState``) and persisted separately.

What the provider does not tell us
----------------------------------
Verified against the live API rather than assumed:

* ``models.list`` returns ``name``, ``version``, ``displayName``,
  ``description``, ``inputTokenLimit``, ``outputTokenLimit`` and
  ``supportedGenerationMethods`` — and nothing else. In particular it does
  **not** report input or output modalities.
* No response header or body exposes the Google Cloud project behind a key.

Consequences, and they are deliberate rather than gaps:

* Model **availability** comes from discovery (a model the provider does not
  list is never tried), but model **capability** comes from a curated table
  below, because the provider does not publish it. That table is a statement
  about model families, kept deliberately conservative.
* Remaining quota and reset times are reported as
  ``Not exposed by provider`` unless an error response actually carried them.
  This module never computes or guesses a "requests remaining" figure, because
  there is no honest way to do so.

Secrets
-------
The credential itself never leaves this module. Accounts are identified by a
truncated SHA-256 ``fingerprint`` (to notice that two slots hold the same key)
and a ``masked`` tail for a human to recognise. Neither is reversible, and
nothing here logs, returns or renders the key.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time

from . import config, db

log = logging.getLogger("guardbot.pool")


# ── Capabilities ──────────────────────────────────────────────────────────
# What a workload needs a model to be able to do. A model is only ever offered
# to a workload whose requirements it satisfies in full: this is the rule that
# stops a text-only model being handed an image, or an image model being asked
# for audio.
TEXT = "text"
IMAGE = "image"
VIDEO = "video"
AUDIO_IN = "audio_in"
AUDIO_OUT = "audio_out"

_MULTIMODAL = frozenset({TEXT, IMAGE, VIDEO, AUDIO_IN})
_TEXT_ONLY = frozenset({TEXT})

# Families that exist in ``models.list`` but must never be selected for the
# bot's workloads. Every one of these would either fail outright or answer a
# different question than the one asked:
#
#   veo-*        video generation (predictLongRunning, not generateContent)
#   lyria-*      music generation
#   *embedding*  embeddings (embedContent)
#   aqa          a question-answering endpoint with its own method
#   *computer-use*, *robotics*, *antigravity*  agentic/simulated environments
#   *deep-research*  multi-step research agents, not a single completion
#   *native-audio*, *-live*  bidirectional streaming only, no generateContent
#   *omni*       a separate experimental family; not validated for production
#   nano-banana-*  image generation under an internal codename
_EXCLUDED_MARKERS = (
    "veo",
    "lyria",
    "embedding",
    "computer-use",
    "robotics",
    "antigravity",
    "deep-research",
    "native-audio",
    "omni",
    "nano-banana",
)


def is_experimental(model: str) -> bool:
    """Whether a model is a preview/experimental release.

    Preview models are excluded from the pool by default. They are reachable
    only when an operator names one explicitly in a fallback list, which is the
    one place a deliberate choice can override the default.
    """
    name = (model or "").lower()
    return any(
        marker in name
        for marker in ("preview", "experimental", "-exp", "alpha", "beta")
    )


def capabilities_of(model: str) -> frozenset[str] | None:
    """What a model can do, or None when it is not usable here at all.

    The provider publishes no modality metadata (verified — see the module
    docstring), so this is a curated statement about model *families*. It is
    deliberately conservative: an unrecognised name returns None rather than
    being optimistically assumed to be multimodal.
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    if any(marker in name for marker in _EXCLUDED_MARKERS):
        return None
    if name == "aqa":
        return None
    # Order matters. A TTS model is named ``...-tts``; an image *generation*
    # model is named ``...-image``; and a transcription model is named
    # ``...-transcribe``. Checking TTS before image keeps
    # ``gemini-3.1-flash-tts-preview`` out of the image branch.
    if "-tts" in name or name.endswith("tts"):
        return frozenset({TEXT, AUDIO_OUT})
    if "transcribe" in name:
        return frozenset({AUDIO_IN})
    if name.endswith("-live") or "-live-" in name:
        return None
    if name.startswith("gemma"):
        # Gemma is text-in, text-out. It has no image or audio input, and
        # offering it to the moderation or transcription workloads would
        # produce a 400 on every call.
        return _TEXT_ONLY
    if "-image" in name:
        # An image *output* model. It cannot be used for any workload here.
        return None
    if name.startswith("gemini"):
        return _MULTIMODAL
    return None


# ── Failures ──────────────────────────────────────────────────────────────
# Where a failure belongs. This is the distinction the whole module turns on.
SCOPE_MODEL = "model"        # try another model, keep the account
SCOPE_ACCOUNT = "account"    # abandon the account, try the next
SCOPE_REQUEST = "request"    # the payload is wrong; no account will help
SCOPE_TRANSIENT = "transient"  # retry, then fall through to the next model


class PoolUnavailable(Exception):
    """No configured account/model could answer.

    Never a statement about a message — only about availability. Callers map it
    onto their own failure type so that their existing safe behaviour (the rule
    engine, the review queue, "I could not listen to that") is unchanged.
    """

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class Failure:
    """One classified provider failure."""

    __slots__ = ("kind", "scope", "retryable", "detail", "cooldown", "reset_at")

    def __init__(
        self,
        kind: str,
        scope: str,
        *,
        retryable: bool = False,
        detail: str = "",
        cooldown: int = 0,
        reset_at: int | None = None,
    ):
        self.kind = kind
        self.scope = scope
        self.retryable = retryable
        self.detail = detail
        self.cooldown = cooldown
        # Only ever set from a ``retryDelay`` the provider actually sent.
        self.reset_at = reset_at


_CODE_RE = re.compile(r"\b(4\d\d|5\d\d)\b")

# The error body is parsed from text, and the quoting is not stable: the SDK
# renders it as a Python dict repr — single quotes — while a body that is still
# JSON is double-quoted. Matching only one of them fails silently, which is the
# worst way for this to fail: every 400 then looks alike, and an invalid key is
# read as a malformed request. Both are matched.
_Q = r"['\"]"
_STATUS_RE = re.compile(rf"{_Q}(?:status|reason){_Q}\s*:\s*{_Q}([A-Z_]+){_Q}")
# The provider's own reset hint, in the two shapes it actually uses. Verified
# against the live API on 2026-09-21: a free-tier 429 answers
#   429 RESOURCE_EXHAUSTED ... * Quota exceeded for metric:
#   generativelanguage.googleapis.com/generate_content_free_tier_requests,
#   limit: 20, model: gemini-3.8-flash
#   Please retry in 26.510175789s.
# The JSON `retryDelay` field the docs describe is not present in that body, so
# a reset time the provider *did* give would have been reported as "not exposed".
_RETRY_DELAY_RE = re.compile(rf"{_Q}retryDelay{_Q}\s*:\s*{_Q}(\d+(?:\.\d+)?)s{_Q}")
_RETRY_IN_RE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_QUOTA_RE = re.compile(rf"{_Q}(?:quotaId|quotaMetric){_Q}\s*:\s*{_Q}([^'\"]+){_Q}")
# The metric is a full resource path — `generativelanguage.googleapis.com/
# generate_content_free_tier_requests` — so the character class has to include
# the slash. Stopping at it would capture only the hostname, which contains
# neither "model" nor "free_tier", and the scope decision below would read a
# per-model limit as a per-project one: the whole account benched for the quota
# cooldown, when a sibling model was available the entire time.
_QUOTA_METRIC_RE = re.compile(r"Quota exceeded for metric:\s*([\w./]+)", re.IGNORECASE)

# Text that means "this credential will never work", whatever code carried it.
# An invalid key answers 400, not 401 (verified live), and reading that as a bad
# request would abandon the whole request instead of moving to the next account.
_AUTH_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "api key expired",
    "api key is invalid",
    "unauthenticated",
    "permission_denied",
    "access_token_type_unsupported",
)


def _text_of(exc: BaseException) -> str:
    return f"{type(exc).__name__} {exc}"


def _status_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        raw = getattr(exc, attr, None)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if 100 <= value <= 599:
            return value
    match = _CODE_RE.search(_text_of(exc))
    return int(match.group(1)) if match else None


def classify_error(exc: BaseException) -> Failure:
    """Decide which level a provider failure belongs to.

    Read from the response text rather than the exception class, because the
    SDK's exception types have moved between versions and the JSON body has not.
    The shapes below were captured from the live API on 2026-09-21, not guessed:

    * an unknown model answers ``404 NOT_FOUND`` — *"is not found for API
      version v1beta, or is not supported for generateContent"*
    * a model retired for new users answers ``404 NOT_FOUND`` too — *"is no
      longer available to new users"*
    * **an invalid key answers ``400 INVALID_ARGUMENT``**, with
      ``reason: API_KEY_INVALID`` and *"API key not valid"* — not the 401 the
      documentation implies. Read as a bad request it would abandon the request
      instead of moving to the next account, which is the bug this branch
      exists to prevent.
    * a free-tier 429 names the model and the limit in prose and carries the
      reset as *"Please retry in 26.5s"*, not in a ``retryDelay`` field
    """
    text = _text_of(exc)
    lowered = text.lower()
    code = _status_code(exc)
    status_match = _STATUS_RE.search(text)
    status = status_match.group(1) if status_match else ""

    # A reset time, but only when the provider actually sent one. This is the
    # single source of truth for "when will this work again", and it is read
    # from either shape the provider uses.
    reset_at = None
    delay = _RETRY_DELAY_RE.search(text) or _RETRY_IN_RE.search(text)
    if delay:
        try:
            reset_at = int(time.time() + float(delay.group(1)))
        except (TypeError, ValueError):
            reset_at = None

    # ── authentication / authorisation: the account is finished ──
    #
    # Checked before the 400 branch, because an invalid key arrives as a 400.
    if (
        code in (401, 403)
        or status in ("UNAUTHENTICATED", "PERMISSION_DENIED")
        or any(marker in lowered for marker in _AUTH_MARKERS)
    ):
        # 403 splits two ways, and they mean different things: a revoked or
        # unpermitted key is INVALID and must not be retried, while a billing
        # or quota refusal is the account being out of allowance, which is
        # recoverable once it resets.
        if "quota" in lowered or "billing" in lowered or "exceeded" in lowered:
            return Failure(
                "quota_exhausted",
                SCOPE_ACCOUNT,
                detail=status or str(code),
                cooldown=int(config.GEMINI_POOL_QUOTA_COOLDOWN),
                reset_at=reset_at,
            )
        return Failure(
            "invalid_credential", SCOPE_ACCOUNT, detail=status or str(code)
        )

    # ── the model does not exist, or cannot do this ──
    if code == 404 or status == "NOT_FOUND":
        return Failure("unsupported_model", SCOPE_MODEL, detail=status or "404")

    # ── rate limiting: the interesting one ──
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        quota = _QUOTA_RE.search(text)
        quota_id = (quota.group(1) if quota else "").lower()
        if not quota_id:
            # The live free-tier body names the metric in prose rather than in a
            # `quotaId` field. Read it, so the model-versus-project decision
            # below is made on what the provider said rather than on a default.
            metric = _QUOTA_METRIC_RE.search(text)
            quota_id = (metric.group(1) if metric else "").lower()
        # Google's per-model limits name the model in the quota id; the
        # project-wide ones talk about the free tier or the project. When the
        # provider names neither, the conservative reading is a *model* limit:
        # it costs one wasted call on a sibling model, where the opposite
        # mistake costs the whole account for the cooldown.
        if quota_id and not any(
            token in quota_id for token in ("model", "generate_content_free_tier")
        ):
            return Failure(
                "quota_exhausted",
                SCOPE_ACCOUNT,
                detail=quota_id[:60],
                cooldown=int(config.GEMINI_POOL_QUOTA_COOLDOWN),
                reset_at=reset_at,
            )
        if "per_project" in quota_id or "per project" in lowered:
            return Failure(
                "quota_exhausted",
                SCOPE_ACCOUNT,
                detail=quota_id[:60],
                cooldown=int(config.GEMINI_POOL_QUOTA_COOLDOWN),
                reset_at=reset_at,
            )
        return Failure(
            "rate_limited",
            SCOPE_MODEL,
            retryable=True,
            detail=quota_id[:60] or "429",
            cooldown=int(config.GEMINI_POOL_MODEL_COOLDOWN),
            reset_at=reset_at,
        )

    # ── the request itself is malformed ──
    if code == 400 or status in ("INVALID_ARGUMENT", "FAILED_PRECONDITION"):
        # A capability mismatch arrives as a 400 that names the input. That is
        # a model problem — a sibling model may well accept the payload — and
        # treating it as a bad request would abort a request that was fine.
        if any(
            marker in lowered
            for marker in (
                "unable to process input",
                "not supported",
                "does not support",
                "unsupported",
                "invalid image",
                "invalid argument: image",
                "audio",
                "mime",
            )
        ):
            return Failure("unsupported_input", SCOPE_MODEL, detail="400")
        return Failure("bad_request", SCOPE_REQUEST, detail="400")

    # ── the provider is briefly unwell ──
    if code is not None and code >= 500:
        return Failure("provider_error", SCOPE_TRANSIENT, retryable=True,
                       detail=str(code))
    if status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "ABORTED"):
        return Failure("provider_error", SCOPE_TRANSIENT, retryable=True,
                       detail=status)
    if any(
        marker in lowered
        for marker in ("timeout", "timed out", "deadline", "connection",
                       "reset by peer", "temporarily", "network", "unreachable")
    ):
        return Failure("network_error", SCOPE_TRANSIENT, retryable=True,
                       detail=type(exc).__name__)

    # Unrecognised. Transient is the safe reading: it costs one retry and then
    # falls through to the next model, where "request" would abandon a request
    # that might have succeeded.
    return Failure("unknown_error", SCOPE_TRANSIENT, retryable=True,
                   detail=type(exc).__name__)


# ── Identity ──────────────────────────────────────────────────────────────
def fingerprint(key: str) -> str:
    """A stable, non-reversible id for one credential.

    Used only to notice that two configured slots resolved to the same key —
    which means one project, and therefore one shared quota, however many slots
    it was written into.
    """
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]


def mask(key: str) -> str:
    """The last four characters, for a human to recognise. Never the key."""
    tail = (key or "")[-4:]
    return f"****{tail}" if tail else "****"


# ── Account ───────────────────────────────────────────────────────────────
class Account:
    """One credential's state within one workload.

    In-memory mirrors of the persisted row. Every mutation writes through, so a
    container restart resumes with the cooldowns and counters it had.
    """

    def __init__(self, workload: str, slot: str, key: str, label: str = ""):
        self.workload = workload
        self.slot = slot
        self.key = key
        self.label = label or f"API #{slot}"
        self.fingerprint = fingerprint(key)
        self.masked = mask(key)

        self.state = "ACTIVE"
        self.cooldown_until = 0
        self.last_error = ""
        self.last_error_at = 0
        self.last_request = 0
        self.last_success = 0
        self.last_failure = 0
        self.requests = 0
        self.successes = 0
        self.failures = 0
        self.rate_limits = 0
        self.quota_events = 0
        self.model_states: dict[str, "ModelState"] = {}
        self._persisted = False

        # The per-account daily allowance, set by the pool after construction.
        # 0 means unlimited, which is what every workload except chat uses.
        self.daily_budget = 0
        # Cached count for `_daily_day`. Read lazily and refreshed whenever the
        # day turns over, so a long-running process picks up the new day without
        # anything having to fire at midnight.
        self._daily_day = ""
        self._daily_calls = 0

    # -- persistence --
    def load(self) -> "Account":
        """Adopt the persisted rows, if there are any.

        Cooldowns are honoured across restarts; a state of RECOVERING becomes
        ACTIVE, because the process that was going to prove recovery is gone and
        the next request is itself the proof.

        Every model row for this workload is adopted in one read rather than one
        read per model. ``models_for`` asks about each candidate model on every
        request, so loading lazily would be a full table scan per candidate per
        request — nine queries where one will do, on the busiest path in the bot.
        """
        for row in db.pool_accounts(self.workload):
            if row["slot"] != self.slot:
                continue
            self.state = row["state"] if row["state"] in db.ACCOUNT_STATES else "ACTIVE"
            if self.state == "RECOVERING":
                self.state = "ACTIVE"
            self.cooldown_until = int(row["cooldown_until"] or 0)
            self.last_error = row["last_error"] or ""
            self.last_error_at = int(row["last_error_at"] or 0)
            self.last_request = int(row["last_request"] or 0)
            self.last_success = int(row["last_success"] or 0)
            self.last_failure = int(row["last_failure"] or 0)
            self.requests = int(row["requests"] or 0)
            self.successes = int(row["successes"] or 0)
            self.failures = int(row["failures"] or 0)
            self.rate_limits = int(row["rate_limits"] or 0)
            self.quota_events = int(row["quota_events"] or 0)
            self._persisted = True
            break
        for row in db.pool_models(self.workload):
            if row["slot"] != self.slot:
                continue
            state = ModelState(self, row["model"])
            state.adopt(row)
            self.model_states[row["model"]] = state
        self.save()
        return self

    def save(self) -> None:
        db.pool_account_save(
            self.workload,
            self.slot,
            fingerprint=self.fingerprint,
            masked=self.masked,
            state=self.state,
            cooldown_until=self.cooldown_until,
            last_error=self.last_error[:200],
            last_error_at=self.last_error_at,
            last_request=self.last_request,
            last_success=self.last_success,
            last_failure=self.last_failure,
            requests=self.requests,
            successes=self.successes,
            failures=self.failures,
            rate_limits=self.rate_limits,
            quota_events=self.quota_events,
        )

    # -- state --
    def usable(self, now: float) -> bool:
        """Whether this account may be tried at all.

        INVALID is terminal: a revoked credential is not going to start working,
        and hammering it wastes the request budget of every message that needs
        an answer.

        A spent daily allowance is *not* terminal — it comes back when the day
        turns over — so it is checked last and reported separately by
        :meth:`daily_exhausted`, which is what lets the caller say "out of
        allowance" rather than "out of accounts".
        """
        if self.state == "INVALID":
            return False
        if self.state in ("DISABLED",):
            return False
        if self.cooldown_until > int(now):
            return False
        # No argument on purpose: the allowance is a calendar fact and reads the
        # wall clock itself. See the note above ``daily_calls``.
        return not self.daily_exhausted()

    def in_cooldown(self, now: float) -> bool:
        return self.cooldown_until > int(now)

    # -- the per-account daily allowance --
    #
    # These deliberately take **no** clock from the caller, and default to
    # ``time.time()``. A day is a calendar fact and can only be read from the
    # wall clock, whereas the cooldown logic around them is about intervals and
    # is happy with a monotonic one. Passing a caller's ``now`` in here is how
    # the two get mixed, and a monotonic reading through ``ai_day`` lands in
    # 1970 — so the allowance was written under one day and read under another,
    # and the cap never fired. Keeping the calendar behind this boundary is what
    # makes that impossible rather than merely unlikely.
    def daily_calls(self, now: float | None = None) -> int:
        """Provider requests this account has spent on the current API day.

        Cached per day rather than per call: this is asked on every candidate
        account for every request, and the day only changes once. The cache is
        keyed by the day itself, so it cannot go stale — reading a new day is
        what refreshes it.
        """
        if not self.daily_budget:
            return 0
        day = db.ai_day(time.time() if now is None else now)
        if day != self._daily_day:
            self._daily_day = day
            self._daily_calls = db.daily_for(self.workload, day).get(self.slot, 0)
        return self._daily_calls

    def daily_exhausted(self, now: float | None = None) -> bool:
        """Whether this account has spent its whole allowance for the day.

        False when no allowance is configured, which is the case for every
        workload but chat.
        """
        if not self.daily_budget:
            return False
        return self.daily_calls(now) >= self.daily_budget

    def note_request(self, now: float) -> None:
        self.requests += 1
        self.last_request = int(now)
        db.pool_account_bump(self.workload, self.slot, "requests")
        db.pool_account_save(self.workload, self.slot, last_request=int(now))
        if self.daily_budget:
            # Counted here rather than at the call site because this is the one
            # place that already means "a provider request is about to be spent
            # on this account", and a second place would eventually disagree.
            # The day comes from the wall clock, never from ``now``.
            self._daily_day = db.ai_day()
            self._daily_calls = db.daily_add(self.workload, self.slot, self._daily_day)

    def note_success(self, now: float) -> bool:
        """Record a success. Returns True when this was a recovery.

        A recovery is worth telling the owner about: it is the moment an account
        comes back, and it is the only way they learn the pool grew again
        without asking.
        """
        was_down = self.state in ("RATE_LIMITED", "QUOTA_EXHAUSTED", "UNAVAILABLE")
        self.successes += 1
        self.last_success = int(now)
        self.state = "ACTIVE"
        self.cooldown_until = 0
        self.last_error = ""
        db.pool_account_bump(self.workload, self.slot, "successes")
        db.pool_account_save(
            self.workload,
            self.slot,
            state=self.state,
            cooldown_until=0,
            last_error="",
            last_success=int(now),
        )
        return was_down

    def note_failure(self, failure: Failure, now: float) -> None:
        self.failures += 1
        self.last_failure = int(now)
        self.last_error = f"{failure.kind}:{failure.detail}"[:200]
        self.last_error_at = int(now)
        db.pool_account_bump(self.workload, self.slot, "failures")
        db.pool_account_save(
            self.workload,
            self.slot,
            last_failure=int(now),
            last_error=self.last_error,
            last_error_at=int(now),
        )
        if failure.kind == "rate_limited":
            self.rate_limits += 1
            db.pool_account_bump(self.workload, self.slot, "rate_limits")
        elif failure.kind == "quota_exhausted":
            self.quota_events += 1
            db.pool_account_bump(self.workload, self.slot, "quota_events")

    def trip(self, failure: Failure, now: float) -> None:
        """Take the whole account out of rotation after an account-level fault."""
        if failure.kind == "invalid_credential":
            self.state = "INVALID"
            self.cooldown_until = 0
        elif failure.kind == "quota_exhausted":
            self.state = "QUOTA_EXHAUSTED"
            self.cooldown_until = failure.reset_at or (
                int(now) + max(1, failure.cooldown)
            )
        else:
            self.state = "UNAVAILABLE"
            self.cooldown_until = int(now) + max(1, failure.cooldown)
        self.save()

    def mark(self, state: str, *, reason: str, now: float, cooldown: int = 0) -> None:
        self.state = state
        self.last_error = reason[:200]
        self.last_error_at = int(now)
        self.cooldown_until = int(now) + cooldown if cooldown else 0
        self.save()

    # -- models --
    def model(self, name: str) -> "ModelState":
        """This account's state for one model, created on first use.

        No database read: ``load()`` already adopted every persisted model row
        for this workload, so a state that is not in the dictionary genuinely
        has no history rather than merely not having been read yet.
        """
        state = self.model_states.get(name)
        if state is None:
            state = ModelState(self, name)
            self.model_states[name] = state
        return state

    def model_usable(self, name: str, now: float) -> bool:
        return self.model(name).usable(now)

    # -- description --
    def describe(self) -> dict:
        """Everything safe to log or show the owner. No credential, ever."""
        return {
            "slot": self.slot,
            "label": self.label,
            "masked": self.masked,
            "state": self.state,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limits": self.rate_limits,
            "quota_events": self.quota_events,
            "cooldown_until": self.cooldown_until,
            "last_error": self.last_error,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
        }


class ModelState:
    """One model's state inside one account.

    Separate from the account on purpose: "Model A is limited, but Account #1 is
    still healthy" has to be representable, or a single rate-limited model takes
    a perfectly good account out of the pool.
    """

    def __init__(self, account: Account, name: str):
        self.account = account
        self.name = name
        self.state = "ACTIVE"
        self.cooldown_until = 0
        self.last_use = 0
        self.requests = 0
        self.successes = 0
        self.failures = 0
        self.rate_limits = 0
        self.quota_events = 0
        self._persisted = False

    def adopt(self, row: dict) -> None:
        """Take the state and counters from one persisted row."""
        self.state = row["state"] if row["state"] in db.ACCOUNT_STATES else "ACTIVE"
        if self.state == "RECOVERING":
            self.state = "ACTIVE"
        self.cooldown_until = int(row["cooldown_until"] or 0)
        self.last_use = int(row["last_use"] or 0)
        self.requests = int(row["requests"] or 0)
        self.successes = int(row["successes"] or 0)
        self.failures = int(row["failures"] or 0)
        self.rate_limits = int(row["rate_limits"] or 0)
        self.quota_events = int(row["quota_events"] or 0)
        self._persisted = True

    def save(self) -> None:
        db.pool_model_save(
            self.account.workload,
            self.account.slot,
            self.name,
            state=self.state,
            cooldown_until=self.cooldown_until,
            last_use=self.last_use,
            requests=self.requests,
            successes=self.successes,
            failures=self.failures,
            rate_limits=self.rate_limits,
            quota_events=self.quota_events,
        )

    def usable(self, now: float) -> bool:
        if self.state == "DISABLED":
            return False
        return self.cooldown_until <= int(now)

    def note_request(self, now: float) -> None:
        self.requests += 1
        self.last_use = int(now)
        db.pool_model_bump(self.account.workload, self.account.slot, self.name,
                           "requests")
        db.pool_model_save(self.account.workload, self.account.slot, self.name,
                           last_use=int(now))

    def note_success(self, now: float) -> None:
        self.successes += 1
        self.state = "ACTIVE"
        self.cooldown_until = 0
        self.last_use = int(now)
        db.pool_model_bump(self.account.workload, self.account.slot, self.name,
                           "successes")
        db.pool_model_save(self.account.workload, self.account.slot, self.name,
                           state="ACTIVE", cooldown_until=0, last_use=int(now))

    def note_failure(self, failure: Failure, now: float) -> None:
        self.failures += 1
        self.last_use = int(now)
        db.pool_model_bump(self.account.workload, self.account.slot, self.name,
                           "failures")
        if failure.kind == "rate_limited":
            self.rate_limits += 1
            db.pool_model_bump(self.account.workload, self.account.slot,
                               self.name, "rate_limits")
        elif failure.kind == "quota_exhausted":
            self.quota_events += 1
            db.pool_model_bump(self.account.workload, self.account.slot,
                               self.name, "quota_events")

        if failure.kind == "unsupported_model":
            # A model the provider says does not exist is not going to start
            # existing. Disabling it outright stops it being tried on every
            # request for the rest of the process's life.
            self.state = "DISABLED"
            self.cooldown_until = 0
        elif failure.scope == SCOPE_TRANSIENT:
            # A brief provider wobble says nothing about this model. Keeping the
            # state ACTIVE with a short cooldown means the next request tries it
            # again, instead of the model being wrongly benched for minutes.
            self.cooldown_until = int(now) + max(
                1, int(config.GEMINI_POOL_TRANSIENT_COOLDOWN)
            )
        else:
            self.state = "RATE_LIMITED"
            self.cooldown_until = failure.reset_at or (
                int(now) + max(1, failure.cooldown)
            )
        self.save()

    def describe(self) -> dict:
        return {
            "model": self.name,
            "state": self.state,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limits": self.rate_limits,
            "quota_events": self.quota_events,
            "cooldown_until": self.cooldown_until,
        }


# ── The pool ──────────────────────────────────────────────────────────────
class Pool:
    """Every account one workload may use, plus its model preference order."""

    def __init__(
        self,
        workload: str,
        keys: list[tuple[str, str]],
        models: list[str],
        capabilities: frozenset[str],
        *,
        allow_experimental: bool = False,
        retries: int = 0,
        backoff: float = 1.5,
        timeout: float = 10.0,
        daily_budget: int = 0,
    ):
        """``keys`` is an ordered list of ``(slot, credential)`` pairs.

        ``daily_budget`` is a per-account allowance of provider requests per API
        day, and 0 means unlimited. It is per *account* rather than per pool on
        purpose: a single shared allowance for the whole workload is reached
        while a second configured account still has a full day available, which
        makes the second account worth nothing. Per account, the pool spends one
        account's day and then fails over to the next exactly as it does for a
        429 — so the allowance scales with the pool instead of capping it.
        """
        self.workload = workload
        self.capabilities = capabilities
        self.models = [m for m in models if m]
        self.allow_experimental = allow_experimental
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, float(backoff))
        self.timeout = max(1.0, float(timeout))
        self.daily_budget = max(0, int(daily_budget))
        self.accounts: list[Account] = []
        self._discovery: dict[str, list[str] | None] = {}
        seen: set[str] = set()
        for slot, key in keys:
            if not key:
                continue
            fp = fingerprint(key)
            if fp in seen:
                # The same credential twice is one account. Keeping both would
                # invent a quota that does not exist.
                continue
            seen.add(fp)
            account = Account(workload, str(slot), key).load()
            account.daily_budget = self.daily_budget
            self.accounts.append(account)

    @property
    def enabled(self) -> bool:
        return bool(self.accounts)

    # -- selection --
    def ordered_accounts(self, now: float) -> list[Account]:
        """Accounts to try, healthiest-and-least-used first.

        Least-recently-succeeded ordering rather than "stay on #1 until it
        dies". The requirement is explicit that five configured accounts must
        not leave four of them unused: spreading the load is what makes the
        pool's total capacity available instead of only its first account's.
        """
        usable = [a for a in self.accounts if a.usable(now)]
        usable.sort(key=lambda a: (a.last_success or 0, a.slot))
        return usable

    def daily_exhausted(self, now: float | None = None) -> bool:
        """Whether *every* account has spent its allowance for the day.

        This is the only question that may produce a "your daily quota is used
        up" message, and it is deliberately asked of the accounts rather than of
        a single counter. One shared counter reached zero while a second account
        with a full day sat unused, and the bot told the group its quota was
        gone — which was false.

        Cooldowns are ignored here on purpose. An account that has allowance but
        is briefly cooling down is a transient failure, not an exhausted quota,
        and conflating the two would send people away for a day over a minute.
        """
        if not self.daily_budget or not self.accounts:
            return False
        return all(a.daily_exhausted(now) for a in self.accounts)

    def daily_remaining(self, now: float | None = None) -> int:
        """Provider requests left across the pool today. For the owner report.

        An account with no budget configured contributes nothing, so a workload
        without an allowance reports 0 rather than a misleading infinity.
        """
        if not self.daily_budget:
            return 0
        return sum(
            max(0, self.daily_budget - a.daily_calls(now)) for a in self.accounts
        )

    def models_for(self, account: Account, now: float) -> list[str]:
        """The models to try, in preference order, for one account.

        Filtered three ways: the model must be capable of this workload, it must
        not be an experimental release unless that was opted into, and — when
        discovery has answered for this credential — the provider must actually
        list it.
        """
        discovered = self._discovery.get(account.fingerprint, "unknown")
        out: list[str] = []
        for name in self.models:
            if not account.model_usable(name, now):
                continue
            caps = capabilities_of(name)
            if caps is None or not self.capabilities <= caps:
                continue
            if is_experimental(name) and not self.allow_experimental:
                continue
            if isinstance(discovered, list) and name not in discovered:
                continue
            out.append(name)
        return out

    # -- discovery --
    async def discover(self, account: Account) -> list[str] | None:
        """Ask the provider which models this credential can see.

        Availability only. The provider publishes no modality metadata, so this
        cannot answer "can it do audio" — that stays with ``capabilities_of``.
        Returns None when discovery is off or failed, which means "do not
        filter", never "no models".
        """
        if not config.GEMINI_POOL_DISCOVERY_ENABLED:
            return None
        cached = self._discovery.get(account.fingerprint)
        if isinstance(cached, list):
            return cached
        stored = db.discovery_get(account.fingerprint,
                                  config.GEMINI_POOL_DISCOVERY_TTL)
        if stored is not None:
            self._discovery[account.fingerprint] = stored
            return stored
        try:
            genai, _types = _load_sdk()
            client = _client_for(account.key, self.timeout)
            names = await _list_models(client)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - discovery is best-effort
            log.info(
                "[pool] model discovery unavailable workload=%s reason=%s",
                self.workload,
                type(exc).__name__,
            )
            self._discovery[account.fingerprint] = None
            return None
        if not names:
            self._discovery[account.fingerprint] = None
            return None
        db.discovery_put(account.fingerprint, sorted(names))
        self._discovery[account.fingerprint] = sorted(names)
        log.info(
            "[pool] discovery workload=%s account=%s models=%d",
            self.workload,
            account.masked,
            len(names),
        )
        return self._discovery[account.fingerprint]

    # -- health --
    def health(self) -> dict:
        """Counts of accounts by state, and whether the pool is in trouble."""
        counts = {"active": 0, "limited": 0, "exhausted": 0, "invalid": 0,
                  "unavailable": 0}
        now = time.time()
        for account in self.accounts:
            if account.state == "INVALID":
                counts["invalid"] += 1
            elif account.state == "QUOTA_EXHAUSTED":
                counts["exhausted"] += 1
            elif account.state in ("RATE_LIMITED", "UNAVAILABLE"):
                counts["limited"] += 1
            elif account.in_cooldown(now):
                counts["limited"] += 1
            elif account.daily_exhausted(now):
                # Out of allowance is a kind of limited, not a kind of dead: it
                # comes back on its own when the day turns over. Counting it as
                # active would overstate what the pool can serve right now, and
                # counting it as invalid would be a lie about the credential.
                counts["limited"] += 1
            else:
                counts["active"] += 1
        usable = counts["active"]
        accounts = len(self.accounts)
        return {
            "workload": self.workload,
            "accounts": accounts,
            "usable": usable,
            **counts,
            # Exactly one usable account left. Literally what the brief asks to
            # be warned about.
            "critical": accounts > 0 and usable == 1,
            # ...and the version of that which is actually news. A pool of one
            # is *always* at one usable account, so warning about it on every
            # boot would be a message that never means anything — which is how
            # an operator learns to ignore the message that does. Degraded means
            # the pool has shrunk from more than one to exactly one, which is a
            # change they can act on.
            "degraded": accounts > 1 and usable == 1,
            "empty": accounts > 0 and usable == 0,
            # The daily allowance, which is a different question from whether
            # the accounts are healthy: an account can be perfectly valid and
            # still have spent its day.
            "daily_budget": self.daily_budget,
            "daily_remaining": self.daily_remaining(now),
            "daily_exhausted": self.daily_exhausted(now),
        }

    # -- events --
    def record(
        self,
        kind: str,
        *,
        slot: str = "",
        model: str = "",
        reason: str = "",
        detail: str = "",
        now: float | None = None,
    ) -> bool:
        """Write one structured pool event. Returns whether a row was written.

        Deduplicated on ``(workload, kind, slot, model)`` against a cooldown, so
        a hundred consecutive 429s on one model are one row rather than a
        hundred. That is what keeps this table a record of *transitions*: the
        quantitative history — how many requests, how many failures, how many
        rate limits — already lives on the account and model rows, and a table
        that duplicated it would be both larger and less readable.

        **This never contacts Telegram, and there is no way to make it.** There
        is no notifier to register, no callback to install, and no message text
        to send: the method records a row and returns. Pool state reaches a
        human when a human asks for it — `/pool`, or this table — and never
        because the pool decided to speak.

        This is not ``async`` any more, and that is the point: it was async only
        because it awaited a delivery, and the ``await`` was the thing that made
        "the pool can send a message" look like an ordinary consequence of
        recording an event. Removing it removes the shape of the mistake.
        """
        moment = int(time.time() if now is None else now)
        last = db.pool_last_event(self.workload, kind, slot=slot, model=model)
        if last and moment - last < max(0, int(config.GEMINI_POOL_EVENT_COOLDOWN)):
            return False
        db.pool_event_add(
            self.workload,
            kind,
            slot=slot,
            model=model,
            reason=reason,
            detail=detail,
            at=moment,
        )
        return True

    # -- status --
    def status(self) -> dict:
        """A description safe to log and to show the owner."""
        health = self.health()
        return {
            "workload": self.workload,
            "enabled": self.enabled,
            "accounts": len(self.accounts),
            "usable": health["usable"],
            "critical": health["critical"],
            "empty": health["empty"],
            "primary_model": self.models[0] if self.models else "",
            "models": list(self.models),
            "capabilities": sorted(self.capabilities),
            "discovery": bool(config.GEMINI_POOL_DISCOVERY_ENABLED),
            "daily_budget": health["daily_budget"],
            "daily_remaining": health["daily_remaining"],
            "daily_exhausted": health["daily_exhausted"],
        }


# ── SDK plumbing ──────────────────────────────────────────────────────────
# Imported lazily, and kept lazy: ``google-genai`` is an optional dependency, and
# a deployment without it must still run moderation, acquisition and the whole
# test suite.
_sdk_missing_logged = False
_clients: dict[tuple[str, int], object] = {}


def _load_sdk():
    global _sdk_missing_logged
    try:
        from google import genai
        from google.genai import types
    except BaseException as exc:  # noqa: BLE001 - any import failure is one fact
        if not _sdk_missing_logged:
            _sdk_missing_logged = True
            log.warning(
                "[pool] google-genai is not installed: %s "
                "(the rule engine and local moderation are unaffected)",
                exc,
            )
        raise PoolUnavailable("sdk_missing", str(exc)[:120]) from exc
    return genai, types


def build_client(key: str, timeout: float):
    """Construct an SDK client for one credential. Returns ``(client, types)``.

    The single place a client is built, so every workload gets the same
    deadline handling. The import stays lazy: ``google-genai`` is optional, and
    a host without it must still run the rule engine and local moderation.
    """
    genai, types = _load_sdk()
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=int(max(1.0, timeout) * 1000)),
    )
    return client, types


def client_for(key: str, timeout: float):
    """A cached client for one credential, plus the ``types`` module.

    Cached per ``(fingerprint, timeout)`` because building one per request would
    open a connection pool per request. The key itself is never a dictionary key
    — only its fingerprint — so a stray ``repr`` of the cache cannot leak it.
    """
    _genai, types = _load_sdk()
    cache_key = (fingerprint(key), int(max(1.0, timeout) * 1000))
    client = _clients.get(cache_key)
    if client is None:
        client, _types = build_client(key, timeout)
        _clients[cache_key] = client
    return client, types


def _client_for(key: str, timeout: float):
    """The client used by the request path."""
    return client_for(key, timeout)[0]


async def _list_models(client) -> set[str]:
    """Every model name this credential can use for ``generateContent``.

    The method filter is not decoration. ``models.list`` also returns streaming
    and long-running models — ``gemini-3.5-transcribe-live`` speaks only
    ``bidiGenerateContent``, and the Veo and Lyria families only
    ``predictLongRunning``. Those would be accepted by a name-only filter and
    then rejected on every call, so they are dropped here instead.
    """
    names: set[str] = set()
    pager = await client.aio.models.list()
    if hasattr(pager, "__aiter__"):
        async for item in pager:
            _collect_model(item, names)
    else:
        for item in pager or []:
            _collect_model(item, names)
    return names


def _attr(item, *names):
    for name in names:
        value = getattr(item, name, None)
        if value is None and isinstance(item, dict):
            value = item.get(name)
        if value is not None:
            return value
    return None


def _collect_model(item, out: set[str]) -> None:
    """Add one listed model to ``out`` if it can serve ``generateContent``.

    The attribute name differs between SDK versions (``supported_actions`` on
    the typed model, ``supported_generation_methods`` on the raw dict), so both
    are read. When neither is present the model is *kept*: refusing to filter on
    missing metadata would be worse than trying a model the provider listed.
    """
    raw = _attr(item, "name")
    if not raw:
        return
    name = str(raw).split("/")[-1]
    if not name:
        return
    methods = _attr(item, "supported_actions", "supported_generation_methods",
                    "supportedGenerationMethods")
    if methods:
        flat = [str(m).split(".")[-1] for m in methods]
        if not any("generateContent" in m for m in flat):
            return
    out.add(name)


def reset_clients() -> None:
    """Forget cached clients. For tests and for a credential rotation."""
    _clients.clear()


# ── The one entry point ───────────────────────────────────────────────────
async def generate(
    pool: Pool,
    *,
    build_contents,
    build_config,
    extract=None,
) -> object:
    """Answer one request, failing over internally until something works.

    The caller sees one call. It does not know which account or model served it,
    does not retry, and does not handle a 429 — all of that is here, which is
    what keeps failover out of the Telegram handlers.

    Raises ``PoolUnavailable`` when no compatible resource could answer. Callers
    translate that into their existing safe behaviour.
    """
    if not pool.enabled:
        raise PoolUnavailable("no_account")

    _genai, types = _load_sdk()
    now = time.time()

    # Discovery is best-effort and must never block a request that would
    # otherwise succeed, so a failure here simply means "do not filter".
    for account in pool.accounts:
        await pool.discover(account)

    attempts_per_model = pool.retries + 1
    # A hard ceiling on total provider calls for one logical request. Without it
    # a pathological pool (many accounts, many models, retries on each) could
    # spend a minute of wall clock and dozens of calls on a single message.
    budget = max(1, int(config.GEMINI_POOL_MAX_ATTEMPTS))

    last: PoolUnavailable = PoolUnavailable("no_attempt")
    tried_any = False

    for account in pool.ordered_accounts(now):
        candidates = pool.models_for(account, now)
        if not candidates:
            account.mark(
                "UNAVAILABLE", reason="no_compatible_model", now=now,
                cooldown=int(config.GEMINI_POOL_MODEL_COOLDOWN),
            )
            pool.record(
                "no_compatible_model",
                slot=account.slot,
                reason="no compatible model",
                detail="account out of rotation until the configuration changes",
                now=now,
            )
            continue

        account_dead = False
        for model in candidates:
            if not account.model_usable(model, now):
                continue
            state = account.model(model)
            for attempt in range(attempts_per_model):
                if budget <= 0:
                    last = PoolUnavailable("attempt_budget", last.kind)
                    account_dead = True
                    break
                budget -= 1
                tried_any = True
                now = time.time()
                account.note_request(now)
                state.note_request(now)
                try:
                    response = await _call(
                        pool, account, model, types, build_contents, build_config
                    )
                except asyncio.CancelledError:
                    raise
                except PoolUnavailable as exc:
                    # Raised by the call itself: a timeout we imposed, or the
                    # SDK being absent. The latter is not worth retrying.
                    if exc.kind == "sdk_missing":
                        raise
                    failure = Failure("timeout", SCOPE_TRANSIENT, retryable=True,
                                      detail=exc.detail)
                    last = PoolUnavailable(failure.kind, failure.detail)
                    account.note_failure(failure, now)
                    state.note_failure(failure, now)
                    if attempt + 1 < attempts_per_model:
                        await asyncio.sleep(_backoff(pool, attempt))
                        continue
                    break
                except BaseException as exc:  # noqa: BLE001 - the SDK raises widely
                    failure = classify_error(exc)
                    account.note_failure(failure, now)
                    state.note_failure(failure, now)
                    last = PoolUnavailable(failure.kind, failure.detail)
                    log.warning(
                        "[pool] error workload=%s account=%s model=%s kind=%s "
                        "scope=%s failures=%d",
                        pool.workload,
                        account.masked,
                        model,
                        failure.kind,
                        failure.scope,
                        account.failures,
                    )

                    if failure.scope == SCOPE_MODEL:
                        _record_model_failure(
                            pool, account, model, failure, candidates, now
                        )
                        break  # next model, same account
                    if failure.scope == SCOPE_ACCOUNT:
                        account.trip(failure, now)
                        _record_account_failure(pool, account, model, failure, now)
                        account_dead = True
                        break
                    if failure.scope == SCOPE_REQUEST:
                        # The payload is wrong; every account would answer the
                        # same way, and spending the rest of the pool on it
                        # would be pure waste.
                        raise last
                    # transient
                    if attempt + 1 < attempts_per_model and failure.retryable:
                        await asyncio.sleep(_backoff(pool, attempt))
                        continue
                    break  # next model
                else:
                    was_down = account.note_success(now)
                    state.note_success(now)
                    if was_down:
                        pool.record(
                            "account_recovered",
                            slot=account.slot,
                            reason="recovered",
                            detail="returned to the pool",
                            now=now,
                        )
                    _record_pool_health(pool, now)
                    return _extract(response, extract)
            if account_dead:
                break

    if not tried_any:
        health = pool.health()
        raise PoolUnavailable(
            "pool_empty" if health["empty"] else "no_compatible_model",
            f"usable={health['usable']}/{health['accounts']}",
        )
    raise last


def _backoff(pool: Pool, attempt: int) -> float:
    """Exponential backoff with jitter.

    Jitter matters here for a reason that has nothing to do with politeness: the
    four workloads share this process, and without it a rate-limited provider
    would get every workload's retries in lockstep.
    """
    base = pool.backoff * (2 ** attempt)
    return base + random.uniform(0, max(0.05, base * 0.25))


async def _call(pool, account, model, types, build_contents, build_config):
    """One provider call, bounded by our own deadline.

    The timeout is applied with ``asyncio.wait_for`` rather than trusted to the
    transport, because that is what actually bounds the handler when a socket
    stalls. ``wait_for`` raising TimeoutError is translated to PoolUnavailable so
    that the retry loop has one exception vocabulary.
    """
    timeout = pool.timeout
    client = _client_for(account.key, timeout)
    contents = build_contents(types)
    cfg = build_config(types)

    async def _run():
        return await client.aio.models.generate_content(
            model=model, contents=contents, config=cfg
        )

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise PoolUnavailable("timeout") from exc


def _extract(response, extract):
    if extract is None:
        return getattr(response, "text", "") or ""
    return extract(response)


# The three recorders below used to build a Telegram message and hand it to the
# pool to deliver. They now write a structured row and stop there. What is kept
# is the *reason* and the *detail* — the parts an operator reads in the events
# table — and what is gone is the presentation, because presentation with no
# audience is just a string nobody reads.
def _record_model_failure(pool, account, model, failure, candidates, now) -> None:
    """Note that a model failed on one account, and what was tried next."""
    remaining = [m for m in candidates if m != model]
    if failure.kind == "unsupported_model":
        action = "model disabled on this account"
    elif remaining:
        action = f"tried {remaining[0]}"
    else:
        action = "no compatible model left on this account; next account"
    pool.record(
        "model_failover",
        slot=account.slot,
        model=model,
        reason=failure.kind,
        detail=action,
        now=now,
    )


def _record_account_failure(pool, account, model, failure, now) -> None:
    """Note that an account left the pool, and how many are left."""
    health = pool.health()
    reset = (
        time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(failure.reset_at))
        if failure.reset_at
        else "not exposed by provider"
    )
    pool.record(
        "account_failover",
        slot=account.slot,
        model=model,
        reason=failure.kind,
        detail=(
            f"usable={health['usable']}/{health['accounts']} reset={reset}"
        ),
        now=now,
    )
    _record_pool_health(pool, now)


def _record_pool_health(pool, now) -> None:
    """Note that the pool is nearly or entirely out.

    Deduplicated like everything else, and keyed on the state rather than the
    event, so the table gains one row when the pool degrades and not one per
    request. The state itself is always available from ``health()`` and from
    ``/pool`` — this is the history, not the status.
    """
    health = pool.health()
    if not pool.accounts:
        return
    if health["empty"]:
        pool.record(
            "pool_empty",
            reason="no usable accounts",
            detail=f"usable=0/{health['accounts']}",
            now=now,
        )
    elif health["degraded"]:
        pool.record(
            "pool_critical",
            reason="one usable account",
            detail=f"usable={health['usable']}/{health['accounts']}",
            now=now,
        )


# ── The registry ──────────────────────────────────────────────────────────
# There is deliberately no notifier here, and no way to add one. This registry
# used to carry a callback that every pool was handed so it could reach the
# owner's chat; that callback is gone, and with it the only path from a pool
# event to Telegram. A future change that wants the pool to speak will have to
# add that path back deliberately, in the open, rather than find a hook already
# waiting for it.
_pools: dict[str, Pool] = {}


def build_pools() -> dict[str, Pool]:
    """Build every workload's pool from configuration.

    Called once at startup and once per reload. Safe to call again: it rebuilds
    from configuration, and the persisted state is re-adopted by each account.
    """
    global _pools
    _pools = {}
    for spec in config.GEMINI_POOLS:
        pool = Pool(
            spec["workload"],
            spec["keys"],
            spec["models"],
            spec["capabilities"],
            allow_experimental=spec["allow_experimental"],
            retries=spec["retries"],
            backoff=spec["backoff"],
            timeout=spec["timeout"],
            # Per account, and 0 for every workload that has not asked for one.
            # Only the conversational workload sets it today: it is the one with
            # a user-facing daily budget that has to scale with the pool.
            daily_budget=spec.get("daily_budget", 0),
        )
        _pools[spec["workload"]] = pool
    return _pools


def pool_for(workload: str) -> Pool | None:
    """The pool for one workload, building the registry on first use.

    Lazy on purpose. The four workload modules ask for their pool at import
    time, from ``is_enabled()``, and the bot's entry point has not run by then.
    Building on first access means a workload reports itself enabled when its
    pool has accounts, instead of depending on who imported what first.
    """
    if not _pools:
        build_pools()
    return _pools.get(workload)


def has_accounts(workload: str) -> bool:
    """Whether this workload has any credential at all, pool or legacy.

    The single question ``is_enabled()`` needs, and it is deliberately about
    *presence* rather than about validity: whether the credential works is
    answered by using it, not by inspecting it.
    """
    pool = pool_for(workload)
    return bool(pool is not None and pool.enabled)


def pools() -> list[Pool]:
    """Every built pool, in configuration order. For the startup report."""
    if not _pools:
        build_pools()
    return list(_pools.values())


def shared_credentials() -> list[tuple[str, list[str]]]:
    """Credentials that appear in more than one workload's pool.

    This is the honest reading of requirement three. Two *slots* holding the
    same key are collapsed to one account inside a pool, but the same key
    reaching two different workloads is still one Google project, and therefore
    one allowance — however separately the two workloads count their own usage.

    Returned as ``(masked, [workload, ...])`` so it can be logged and shown to
    the owner without a credential appearing anywhere. Empty when every
    workload has its own credentials, which is the configuration the brief asks
    for.

    ``tts`` and ``awareness`` are excluded, and deliberately. Both are *modes* of
    the conversation feature rather than peers of it: speech synthesis is the
    same exchange spoken aloud, and awareness is the same assistant reading the
    room instead of a message. Both are configured to use the chat credential on
    purpose (``GEMINI_CHAT_TTS_MODEL`` and ``GEMINI_CHAT_API_KEY`` are one
    feature's settings, and ``GEMINI_AWARENESS_API_KEY`` defaults to the same
    key). Reporting those pairings as a surprise would train the operator to
    ignore the warning that actually matters, which is two independent workloads
    quietly drawing on one project.

    Note what is *not* claimed here: sharing a credential means sharing a Google
    project, and therefore a provider-side rate limit. What stays separate — and
    what the brief requires separate — is everything this application controls:
    each workload keeps its own accounts, daily allowance, model preference,
    breaker and counters, because all of those are keyed by workload.
    """
    shared_modes = {"tts", "awareness"}
    seen: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for pool in pools():
        if pool.workload in shared_modes:
            continue
        for account in pool.accounts:
            seen.setdefault(account.fingerprint, [])
            if pool.workload not in seen[account.fingerprint]:
                seen[account.fingerprint].append(pool.workload)
            labels[account.fingerprint] = account.masked
    return [
        (labels[fp], sorted(workloads))
        for fp, workloads in seen.items()
        if len(workloads) > 1
    ]


def status_report(workload: str | None = None) -> str:
    """The owner-facing pool report.

    Built from the persisted rows, so it is accurate even for an account this
    process has not yet used, and honest about what the provider does not
    publish: remaining quota and reset times read ``Not exposed by provider``
    unless an error response actually carried them.
    """
    lines: list[str] = ["🧠 <b>GEMINI API POOL</b>", ""]
    pools = [pool_for(workload)] if workload else list(_pools.values())
    pools = [p for p in pools if p is not None]
    if not pools:
        return "🧠 <b>GEMINI API POOL</b>\n\nNo pools configured."
    total = {"accounts": 0, "requests": 0, "successes": 0, "failures": 0}
    for pool in pools:
        health = pool.health()
        lines.append(f"<b>{pool.workload}</b>")
        lines.append(
            f"Accounts: {health['accounts']}   "
            f"Active: {health['active']}   "
            f"Limited: {health['limited']}   "
            f"Exhausted: {health['exhausted']}   "
            f"Invalid: {health['invalid']}"
        )
        if pool.models:
            lines.append(f"Model preference: {' → '.join(pool.models[:4])}")
        if health["daily_budget"]:
            # The daily allowance is per account, so the number worth printing is
            # what is left across the pool — and the per-account figure beside it,
            # because that is the setting the operator actually wrote.
            lines.append(
                f"Daily allowance: {health['daily_remaining']} of "
                f"{health['daily_budget'] * health['accounts']} left "
                f"({health['daily_budget']} per account)"
            )
        now = time.time()
        for account in pool.accounts:
            row = account.describe()
            lines.append("")
            lines.append(f"{account.label} ({row['masked']})")
            lines.append(f"Status: {row['state']}")
            lines.append(f"Requests: {row['requests']}")
            lines.append(f"Successful: {row['successes']}")
            lines.append(f"Failed: {row['failures']}")
            lines.append(f"Rate limits: {row['rate_limits']}")
            lines.append(f"Quota events: {row['quota_events']}")
            if account.in_cooldown(now):
                lines.append(
                    f"Cooldown: {max(0, int(account.cooldown_until - now))} seconds"
                )
            else:
                lines.append("Cooldown: none")
            if pool.daily_budget:
                lines.append(
                    f"Today: {account.daily_calls()} of {pool.daily_budget} used"
                )
            lines.append("Remaining: Not exposed by provider")
            lines.append("Reset: Not exposed by provider")
            if row["last_error"]:
                lines.append(f"Last error: {row['last_error']}")
        lines.append("")
        totals = db.pool_counts(pool.workload)
        total["accounts"] += totals["accounts"]
        total["requests"] += totals["requests"]
        total["successes"] += totals["successes"]
        total["failures"] += totals["failures"]
    lines.append("")
    lines.append("<b>Totals</b>")
    lines.append(f"Accounts: {total['accounts']}")
    lines.append(f"Requests observed: {total['requests']}")
    lines.append(f"Successful: {total['successes']}")
    lines.append(f"Failed: {total['failures']}")
    lines.append("")
    lines.append(
        "<i>Local counters are this bot's own observations. Google does not "
        "publish remaining quota or reset times for these keys.</i>"
    )
    return "\n".join(lines)


def startup_lines() -> list[str]:
    """One line per workload, for the boot log. Never contains a credential."""
    out: list[str] = []
    for workload, pool in _pools.items():
        health = pool.health()
        out.append(
            f"[pool] {workload}: accounts={health['accounts']} "
            f"usable={health['usable']} models={len(pool.models)} "
            f"caps={','.join(sorted(pool.capabilities))} "
            f"accounts_detail={'; '.join(a.masked for a in pool.accounts) or 'none'}"
        )
    return out
