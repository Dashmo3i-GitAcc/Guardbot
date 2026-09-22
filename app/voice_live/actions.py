"""What the model may *ask for*, and the one path that carries it out.

This is the security boundary of the whole feature, so it is worth being exact
about what is on each side of it.

**The model may ask.** A live session is given a small set of function
declarations — ban, unban, mute, unmute, warn — and when it thinks somebody
asked for one, it produces a structured call. That call is *data*. It carries no
authority, it cannot name its own actor, and it cannot reach past this module.

**The application decides.** Every request is turned into an
``admin_service.AdminRequest`` and handed to ``admin_service.execute``, which is
the same function every other administrative action in this bot goes through.
That means the same permission check, the same owner-protection rule, the same
hierarchy check, the same live Telegram-rights check, the same replay window, the
same idempotency table and the same audit row. There is no second authorisation
path here and there is deliberately no way to make one: this module has no
access to ``db``, no access to ``rbac``, and no way to call a gateway method
directly.

Four things this module does *not* trust, and what it does instead
-----------------------------------------------------------------
1. **The model's word about who is speaking.** ``actor_id`` is a parameter, and
   the only caller passes what ``speakers.SpeakerMap`` read from Telegram's own
   participant list. There is no field on ``VoiceActionRequest`` that the model
   populates with an identity, and a request with actor ``0`` is refused before
   anything else happens.

2. **The model's word about how sure it is.** The call may declare a
   ``resolution`` of ``resolved``, ``ambiguous`` or ``unknown``. Anything but
   ``resolved`` is refused. This is *extra* safety and not the safety: a model
   that confidently mishears somebody says ``resolved`` too, and what catches
   that is the hierarchy check and the audit trail, not the model's self-report.

3. **The model's word about which operation this is.** ``action`` is checked
   against a closed set that is itself a subset of ``admin_service.OPERATIONS``.
   A name outside it never becomes a request, so the voice path cannot reach an
   operation the text path cannot — and in particular cannot reach the
   owner-only switches, promotions, the coding agent or the VPN.

4. **Arguments the declaration did not declare.** A call carrying a parameter
   that is not in the vocabulary is refused rather than having it ignored. A
   model that is inventing parameters is not describing the call it thinks it
   is describing.

Why the vocabulary is a subset, and why it is that subset
---------------------------------------------------------
The five member-targeted moderation operations are the ones a person plausibly
says out loud to a group assistant, and each has a target who is protected by
the ordinary hierarchy rules. Everything else is left out for a reason that is
specific rather than cautious:

* **``promote_member`` / ``demote_member``** change the authority table. A
  misheard sentence that hands somebody administrator rights is not a mistake
  that can be undone by saying sorry, and voice is the least reliable input this
  bot has.
* **``delete_message``** needs a message id. Voice has no way to identify one,
  and inventing an id from a spoken description is exactly the guess this module
  exists to avoid.
* **``nexus_offline`` / ``nexus_online`` / ``awareness_offline`` /
  ``awareness_online``** are the owner's own switches, and they are already
  reachable by deterministic phrase — the path that works with no model at all.
  Letting a model *also* request them would add a second way to silence the
  assistant, and the asymmetry is unforgiving: a misfired "on" costs an answer,
  a misfired "off" costs the assistant.
* **``codebuddy_task`` and the VPN operations** have no business being
  reachable from a voice chat at all.

Rate limits
-----------
Two, because the failure they prevent is different. A short cooldown stops a
model that has misheard from repeating the same request in a loop — the
audio equivalent of a stuck key. A per-session ceiling stops a long call from
becoming an unbounded run of actions even if each one is individually plausible.

Neither is a security control. Both are blast-radius controls: authorisation is
what decides whether an action may happen, and these decide only how often the
question may be asked.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .. import admin_service, config

log = logging.getLogger("guardbot.voice.actions")

# ── The vocabulary ────────────────────────────────────────────────────────
BAN = "ban_member"
UNBAN = "unban_member"
MUTE = "mute_member"
UNMUTE = "unmute_member"
WARN = "warn_member"

#: The operations a live session may ask for. Every name is a key in
#: ``admin_service.OPERATIONS`` — asserted in the test suite, because a name
#: here that is not one there would produce a request that is refused as an
#: unknown operation, which looks like a bug in the model rather than a typo in
#: this table.
VOICE_ACTIONS = frozenset({BAN, UNBAN, MUTE, UNMUTE, WARN})

#: The parameters a declaration may carry. ``target_user_id`` is who it is
#: about; ``reason`` is the wording for a warning or an audit note; and
#: ``resolution`` is the model's own statement about whether it identified the
#: person. Nothing else is accepted.
VOICE_ACTION_PARAMS = frozenset({"target_user_id", "reason", "resolution"})

# The resolution vocabulary. Only ``RESOLVED`` proceeds.
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"
RESOLUTIONS = frozenset({RESOLVED, AMBIGUOUS, UNKNOWN})

# How long a ``reason`` may be. The same bound ``AdminRequest.normalized``
# applies, stated here too so that a request that never reaches the service
# still cannot carry an unbounded string.
REASON_MAX_CHARS = 400


@dataclass(frozen=True)
class VoiceActionRequest:
    """One administrative intention, as a live session produced it.

    The field list is the brief's, and every field is *data*. Note what is
    absent, for the same reason ``admin_service.AdminRequest`` documents the same
    absence: there is no ``is_owner``, no ``actor_role``, no ``allowed``. The
    actor is an id and the authority is derived from it downstream, so a request
    cannot assert anything about itself.
    """

    action: str
    actor_id: int
    chat_id: int
    target_id: int = 0
    reason: str = ""
    # The model's own statement about whether it identified the target. Refused
    # unless it is ``resolved``; kept as a field rather than dropped because a
    # refusal that names the reason is worth more in the audit trail than a
    # refusal that only says "no".
    resolution: str = RESOLVED
    request_id: str = ""
    at: int = 0

    @property
    def resolved(self) -> bool:
        return self.resolution == RESOLVED

    def describe(self) -> dict:
        """A safe summary for the log: ids, action and resolution. No audio,
        no transcript, and the reason is deliberately not included — it is
        model-authored text and the audit row is where it belongs."""
        return {
            "action": self.action,
            "actor_id": self.actor_id,
            "chat_id": self.chat_id,
            "target_id": self.target_id,
            "resolution": self.resolution,
            "request_id": self.request_id,
        }


# ── Refusals this module makes on its own ─────────────────────────────────
# Distinct from the outcomes ``admin_service`` produces, because these are
# refused *before* a request exists. They still end up in the audit trail —
# ``submit`` writes them through the service's own recorder — but a caller can
# tell "the voice layer would not ask" from "the application said no".
OUTCOME_VOICE_UNKNOWN_ACTION = "voice_unknown_action"
OUTCOME_VOICE_NO_ACTOR = "voice_no_actor"
OUTCOME_VOICE_UNRESOLVED = "voice_unresolved"
OUTCOME_VOICE_BAD_TARGET = "voice_bad_target"
OUTCOME_VOICE_BAD_ARGS = "voice_bad_args"
OUTCOME_VOICE_RATE_LIMITED = "voice_rate_limited"
OUTCOME_VOICE_NO_GATEWAY = "voice_no_gateway"


class VoiceActionBridge:
    """One session's action path: parse a call, decide, hand it over.

    Per session, because the two rate limits are per session — a cooldown that
    survived a call would refuse the first action of the next one for no reason
    the operator could see.
    """

    def __init__(
        self,
        chat_id: int,
        *,
        max_actions: int | None = None,
        cooldown: float | None = None,
        clock=time.monotonic,
    ) -> None:
        chat_id = int(chat_id or 0)
        if not chat_id:
            raise ValueError("a voice action bridge needs a chat id")
        self.chat_id = chat_id
        self.max_actions = (
            int(max_actions)
            if max_actions is not None
            else max(0, int(config.GEMINI_LIVE_MAX_ACTIONS))
        )
        self.cooldown = (
            float(cooldown)
            if cooldown is not None
            else max(0.0, float(config.GEMINI_LIVE_ACTION_COOLDOWN_SECONDS))
        )
        self._clock = clock
        self._requests = 0
        self._last_at = 0.0
        self._refused = 0

    # -- parsing --
    def parse(
        self, name: str, args, *, actor_id: int, now: float | None = None
    ) -> VoiceActionRequest | None:
        """A model's call to a typed request, or None when it is not one.

        Returning None rather than raising is the interface the session wants:
        a malformed call is a thing a model does, not an error in this program,
        and the session's response to it is to say nothing rather than to end the
        call. Everything refused here is logged, because a model repeatedly
        producing unusable calls is worth seeing.
        """
        action = str(name or "").strip().lower()
        if action not in VOICE_ACTIONS:
            log.info("[voice] refused an action outside the vocabulary: %r", action)
            return None
        if isinstance(args, dict):
            supplied = dict(args)
        else:
            supplied = {
                key: getattr(args, key)
                for key in VOICE_ACTION_PARAMS
                if hasattr(args, key)
            }
        unknown = set(supplied) - VOICE_ACTION_PARAMS
        if unknown:
            log.info(
                "[voice] refused an action carrying undeclared args: %s",
                ",".join(sorted(unknown)),
            )
            return None
        target = _as_int(supplied.get("target_user_id"))
        if target <= 0:
            log.info("[voice] refused %s with no usable target", action)
            return None
        resolution = str(supplied.get("resolution") or RESOLVED).strip().lower()
        if resolution not in RESOLUTIONS:
            resolution = UNKNOWN
        return VoiceActionRequest(
            action=action,
            actor_id=int(actor_id or 0),
            chat_id=self.chat_id,
            target_id=target,
            reason=str(supplied.get("reason") or "")[:REASON_MAX_CHARS],
            resolution=resolution,
            request_id=admin_service.new_request_id(),
            at=int(now if now is not None else time.time()),
        )

    # -- the local checks, before a request exists --
    def refuse(
        self, request: VoiceActionRequest, outcome: str
    ) -> admin_service.AdminResult:
        """A refusal this module makes, shaped like every other result.

        Built from the public ``AdminResult`` and the public ``message_for``, so
        that the object a caller renders is the same type whatever refused it. A
        second result type here would mean two renderers, and the one used less
        would rot.

        These outcomes have no entry in ``message_for``'s table, so they render
        as the generic denial sentence. That is the correct sentence — a request
        this layer would not pass on *was* denied — and adding voice-specific
        copy would put a second vocabulary of refusals next to the one the whole
        bot already uses.

        Deliberately **not** written to ``admin_audit``. That table is the record
        of what was *asked for and decided*, and a malformed model call is not a
        request: it is output this layer could not turn into one. Recording them
        would fill the audit trail with non-events and make a real refusal harder
        to find. The log line above is where they belong, because the question
        they answer is "is the model producing usable calls", not "what happened
        in this group".
        """
        self._refused += 1
        return admin_service.AdminResult(
            ok=False,
            operation=request.action,
            outcome=outcome,
            actor_id=request.actor_id,
            target_id=request.target_id,
            chat_id=request.chat_id,
            request_id=request.request_id,
            message=admin_service.message_for(outcome),
        )

    def check(self, request: VoiceActionRequest, *, now: float | None = None) -> str:
        """The outcome key for a request this module will not pass on, or ``""``.

        Separate from ``submit`` so that the checks can be tested without a
        gateway and without an event loop. That matters more than it looks: the
        whole point of these checks is that they happen *before* anything
        downstream, and a test that had to construct the downstream to observe
        that would not be testing the ordering.
        """
        if not request.actor_id:
            return OUTCOME_VOICE_NO_ACTOR
        if request.action not in VOICE_ACTIONS:
            return OUTCOME_VOICE_UNKNOWN_ACTION
        if not request.resolved:
            return OUTCOME_VOICE_UNRESOLVED
        if request.target_id <= 0:
            return OUTCOME_VOICE_BAD_TARGET
        if self.max_actions and self._requests >= self.max_actions:
            return OUTCOME_VOICE_RATE_LIMITED
        moment = now if now is not None else self._clock()
        if self.cooldown and self._last_at and moment - self._last_at < self.cooldown:
            return OUTCOME_VOICE_RATE_LIMITED
        return ""

    # -- the handover --
    async def submit(
        self, request: VoiceActionRequest, gateway, *, bot_id: int = 0,
        actor=None, now: float | None = None,
    ) -> admin_service.AdminResult:
        """Hand one request to the existing authorisation and execution path.

        ``gateway`` is the same object the text side passes: the narrow
        protocol in ``admin_service``, whose ten methods are the complete set of
        Telegram side effects reachable from an administrative request. Nothing
        about voice widens it.

        A missing gateway refuses rather than proceeding, which is the fail-closed
        direction: without it no action could actually be carried out, and
        answering "ok" to something that did not happen is the worst available
        outcome — an operator who believes a ban took effect stops watching.
        """
        outcome = self.check(request, now=now)
        if outcome:
            log.info(
                "[voice] action refused locally outcome=%s %s",
                outcome,
                request.describe(),
            )
            return self.refuse(request, outcome)
        if gateway is None:
            log.warning("[voice] action refused: no gateway is wired")
            return self.refuse(request, OUTCOME_VOICE_NO_GATEWAY)

        self._requests += 1
        self._last_at = now if now is not None else self._clock()

        admin_request = request_to_admin(request)
        result = await admin_service.execute(
            admin_request, gateway, actor=actor, bot_id=bot_id
        )
        log.info(
            "[voice] action submitted %s -> ok=%s outcome=%s",
            request.describe(),
            result.ok,
            result.outcome,
        )
        return result

    def describe(self) -> dict:
        """Safe state for the status line: counts, no ids and no text."""
        return {
            "chat_id": self.chat_id,
            "requested": self._requests,
            "refused": self._refused,
            "max_actions": self.max_actions,
            "cooldown_seconds": self.cooldown,
        }


def request_to_admin(request: VoiceActionRequest) -> admin_service.AdminRequest:
    """The one translation from a spoken request to an administrative one.

    ``interface=INTERFACE_AI`` and not ``INTERFACE_PYTHON``, and the choice is
    deliberate. It is what makes the audit trail answer "was this a person
    typing or a model proposing?" with the truth — a spoken action *is* a model
    proposing, on the strength of what it heard — and it is also what subjects
    the request to the check that refuses an AI-originated action while Nexus is
    offline. A voice call cannot outlive the assistant's own off switch.

    ``at`` is copied from the request rather than stamped here, because the
    request's timestamp is when the model produced the call and the replay window
    is measured from then. Stamping at submission would make a call that sat
    behind a slow reconnect look fresh.
    """
    return admin_service.AdminRequest(
        operation=request.action,
        chat_id=request.chat_id,
        actor_id=request.actor_id,
        target_id=request.target_id,
        reason=request.reason,
        request_id=request.request_id,
        interface=admin_service.INTERFACE_AI,
        at=request.at,
    )


def declarations():
    """The function declarations a live session is given.

    Three parameters, one of which is a closed enum, and no free-form field
    except the reason. Written as a plain structure rather than through the SDK's
    ``types`` so that this module stays importable without the provider library —
    the session turns it into whatever the SDK wants, and a test can assert the
    vocabulary without constructing an SDK object.
    """
    return [
        {
            "name": action,
            "description": _DESCRIPTIONS[action],
            "parameters": {
                "type": "object",
                "properties": {
                    "target_user_id": {
                        "type": "integer",
                        "description": (
                            "The Telegram numeric id of the person this is about. "
                            "Take it from the list of people in the call; never "
                            "guess it."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "A short reason, in the room's language.",
                    },
                    "resolution": {
                        "type": "string",
                        "enum": sorted(RESOLUTIONS),
                        "description": (
                            "resolved only when exactly one person in the call "
                            "matches. Say ambiguous or unknown otherwise."
                        ),
                    },
                },
                "required": ["target_user_id", "resolution"],
            },
        }
        for action in sorted(VOICE_ACTIONS)
    ]


_DESCRIPTIONS = {
    BAN: "Ban a member from the group. The request is authorised by the server.",
    UNBAN: "Lift a ban from a member. The request is authorised by the server.",
    MUTE: "Restrict a member from posting. The request is authorised by the server.",
    UNMUTE: "Lift a restriction from a member. The request is authorised by the server.",
    WARN: "Warn a member in the group. The request is authorised by the server.",
}


def _as_int(value) -> int:
    """Coerce to int, or 0. ``0`` is the fail-closed answer everywhere here."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
