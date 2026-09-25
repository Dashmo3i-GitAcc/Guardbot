"""What can go wrong in a live call, and what each kind of wrongness licenses.

A live voice call is the least forgiving thing this bot does. Every other
subsystem is request-shaped: something asks, the bot answers, and a failure is
one answer that did not happen. A call is a *session* — a socket held open for
minutes, two media streams in flight, a person waiting to be spoken to — and a
failure inside it has to be answered while it is happening, by a process that
must decide, in the moment, whether to retry, to give up, or to stop and say so.

That decision cannot be made from an exception's type alone and it must not be
made from its message. So every failure here carries two machine-readable facts:

* ``reason`` — a stable key. It is never a sentence, and it is never shown to
  anyone: the caller maps it to copy, exactly as ``app/admin_service.py`` maps
  an outcome key. A reason that leaked into a group would be an internal string
  in a public place, and a reason that was *derived* from a message would be
  whatever the provider felt like saying.

* ``retryable`` — whether trying the same thing again could plausibly work.

The second is the load-bearing one, and the rule it encodes is the whole reason
this module exists rather than a single ``VoiceLiveError``:

    **A failure that is retryable is retried with backoff, and a failure that is
    not ends the session.**

Getting that backwards is expensive in both directions. Retrying a rejected
language code loops for ever and holds a voice channel open while doing it.
Giving up on a dropped socket loses a call that would have survived a two-second
reconnect. And there is a third case the boolean cannot express, which is why
``fatal`` exists alongside it — see below.

What this module deliberately does **not** do
---------------------------------------------
It does not import ``app/config.py``, ``app/db.py`` or anything else from the
application, and it holds no credentials, no ids and no text. It is the one
module in this package that could be read in full to answer "what can fail
here" without reading the rest, and that is the point of keeping it free of
dependencies.
"""
from __future__ import annotations

# ── Reasons ───────────────────────────────────────────────────────────────
# Machine keys. Grouped by what a caller should *do* about them, because that
# is the only question anyone asks of a reason.
#
# Retryable: the same call, tried again, may work. A dropped socket, a provider
# hiccup, a stream that went away. These are the ordinary weather of a long-lived
# connection and none of them means anything is wrong with the configuration.
REASON_CONNECT_FAILED = "connect_failed"
REASON_CONNECTION_LOST = "connection_lost"
REASON_STREAM_ENDED = "stream_ended"
REASON_PROVIDER_BUSY = "provider_busy"
REASON_TIMEOUT = "timeout"
# The provider said the session is ending and offered a handle to resume it.
# Distinct from a plain disconnect because the *right* response is different:
# resuming reuses the provider's own session state instead of starting over,
# which is what keeps the conversation's context across a provider-side
# migration rather than replaying it.
REASON_GO_AWAY = "go_away"

# Not retryable, and not fatal to the deployment either: the thing asked for is
# not available right now, and asking again in a second will not change that.
REASON_QUOTA_EXHAUSTED = "quota_exhausted"
REASON_NO_CREDENTIAL = "no_credential"
REASON_TRANSPORT_UNAVAILABLE = "transport_unavailable"
REASON_NOT_IN_CALL = "not_in_call"
REASON_JOIN_REJECTED = "join_rejected"

# ── Why a join was refused, in enough detail to fix it ────────────────────
# ``join_rejected`` alone is the answer to "was the join refused", and it is not
# the answer to "what do I change". Telegram offers four distinct reasons a voice
# chat cannot be joined, each with a different fix, and collapsing them is how an
# operator ends up hunting the wrong one:
#
# * ``no_active_call`` — the group has no voice chat right now. Nothing to fix;
#   start one, or ask again later. This is the *normal* case, not a fault.
# * ``scheduled_call`` — a voice chat exists but is scheduled, not live. Joining
#   it is not what "come into the call" means, and PyTgCalls deliberately refuses
#   a call whose ``schedule_date`` is set.
# * ``call_not_visible`` — the account cannot see the call: it is not a member,
#   or was removed, or the peer is forbidden. A membership/permission fix.
# * ``discovery_failed`` — asking Telegram failed (network, flood wait, an
#   unexpected reply). Transient or environmental; retrying later may work.
#
# These are the four the *resolver* returns. The library's own
# ``NoActiveGroupCall`` cannot tell them apart, because its cache swallows the
# exception that would have said which one it was — see
# ``app/voice_live/call_discovery.py``.
REASON_NO_ACTIVE_CALL = "no_active_call"
REASON_SCHEDULED_CALL = "scheduled_call"
REASON_CALL_NOT_VISIBLE = "call_not_visible"
REASON_DISCOVERY_FAILED = "discovery_failed"

# Fatal to the session and a configuration problem: something the operator has
# to change. Retrying these is a loop that cannot terminate.
REASON_SETUP_REJECTED = "setup_rejected"
REASON_UNSUPPORTED_MODEL = "unsupported_model"
REASON_UNSUPPORTED_LANGUAGE = "unsupported_language"

# The feature itself is off, or the caller is not allowed to turn it on.
REASON_DISABLED = "disabled"
REASON_NOT_OWNER = "not_owner"
REASON_BUSY = "busy"
REASON_NOT_CONFIGURED = "not_configured"
# A hard resource ceiling was reached — too many concurrent calls, or a session
# that outlived its maximum. Distinct from ``busy`` because it is a limit this
# process imposed on itself and the operator can see it coming.
REASON_LIMIT_REACHED = "limit_reached"

RETRYABLE_REASONS = frozenset(
    {
        REASON_CONNECT_FAILED,
        REASON_CONNECTION_LOST,
        REASON_STREAM_ENDED,
        REASON_PROVIDER_BUSY,
        REASON_TIMEOUT,
        REASON_GO_AWAY,
    }
)


def is_retryable(reason: str) -> bool:
    """Whether a reason is one a fresh attempt could plausibly survive.

    A function rather than a membership test at each call site so that the
    answer has one home, and so that a reason added later is *unretryable by
    default*. That default is the safe direction: an unknown failure retried is
    a loop, while an unknown failure abandoned is a call that ends and says so.
    """
    return reason in RETRYABLE_REASONS


# ── The hierarchy ─────────────────────────────────────────────────────────
class VoiceLiveError(Exception):
    """Base for everything this package raises.

    ``reason`` is the machine key; ``detail`` is for the log only and is never
    rendered to a person. Both are keyword-only so that a subclass cannot
    accidentally transpose them — a detail string where a reason belongs would
    put provider text into a branch that decides whether to retry.
    """

    reason = "error"

    def __init__(self, detail: str = "", *, reason: str = "") -> None:
        self.detail = str(detail or "")
        if reason:
            self.reason = reason
        super().__init__(self.reason)

    @property
    def retryable(self) -> bool:
        return is_retryable(self.reason)

    def __str__(self) -> str:
        """The reason, and the detail only when there is one.

        Deliberately not the detail alone. A log line that reads
        ``"1007 Unsupported language code"`` names the symptom; one that reads
        ``unsupported_language: 1007 ...`` names the decision the caller took.
        """
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


class FeatureDisabled(VoiceLiveError):
    """Voice Live is switched off, or was never switched on.

    The default state of the feature, and not an error condition: a deployment
    that has not opted in reaches this on every attempt and should log it once
    and quietly, not as a failure.
    """

    reason = REASON_DISABLED


class NotAuthorised(VoiceLiveError):
    """The actor may not do this. Owner-only, like every other control here."""

    reason = REASON_NOT_OWNER


class NotConfigured(VoiceLiveError):
    """The feature is on but this deployment has no way to carry a call.

    The honest state today for the real Telegram transport, and the missing
    piece is a *credential*, not a library. ``ntgcalls 2.2.5`` publishes
    ``cp312-manylinux_2_28_x86_64`` wheels, so ``py-tgcalls`` installs on this
    deployment's interpreter; what is absent is the ``api_id``/``api_hash`` pair
    and the logged-in MTProto session that joining a voice chat requires (the
    Bot API has no method for it). An earlier version of this docstring said the
    library published no wheel, which was wrong and sent the fix in the wrong
    direction. Saying so as its own reason is what lets the command answer
    "not available on this build" instead of failing as though the request were
    malformed.
    """

    reason = REASON_NOT_CONFIGURED


class SessionConflict(VoiceLiveError):
    """A session already exists for this chat, or the ceiling is reached."""

    reason = REASON_BUSY


class SessionNotActive(VoiceLiveError):
    """There is nothing to stop, or nothing to interrupt."""

    reason = REASON_NOT_IN_CALL


class LimitReached(VoiceLiveError):
    """A resource ceiling this process imposes on itself was reached."""

    reason = REASON_LIMIT_REACHED


class TransportUnavailable(VoiceLiveError):
    """The voice-chat transport could not be built or could not join."""

    reason = REASON_TRANSPORT_UNAVAILABLE


class JoinRejected(VoiceLiveError):
    """The transport was available and the join was refused."""

    reason = REASON_JOIN_REJECTED


class ProviderError(VoiceLiveError):
    """The model provider refused, dropped or timed out.

    The base for the three provider failures below, and the only place the
    retryability of a provider failure is decided. A caller that catches this
    gets the right answer for all three without knowing which it caught, which
    is what a reconnect loop wants: it does not care *why* the socket went
    away, only whether to reopen it.
    """


class ConnectFailed(ProviderError):
    reason = REASON_CONNECT_FAILED


class ConnectionLost(ProviderError):
    reason = REASON_CONNECTION_LOST


class ProviderTimeout(ProviderError):
    reason = REASON_TIMEOUT


class GoAway(ProviderError):
    """The provider is migrating the session and offered a resume handle.

    Retryable, but *not* the same retry as the others: the session must be
    reopened with the stored handle rather than from scratch, or the
    conversation loses everything said so far.
    """

    reason = REASON_GO_AWAY


class SetupRejected(ProviderError):
    """The provider refused the session's configuration.

    Not retryable, and deliberately the place a rejected language code or an
    unsupported model lands. Both were hit while measuring this feature: the
    native-audio family refuses every explicit Persian code, and the
    transcription family refuses the AUDIO response modality. Neither gets
    better on a second attempt, and a loop that kept asking would hold a voice
    channel open while failing.
    """

    reason = REASON_SETUP_REJECTED


class QuotaExhausted(VoiceLiveError):
    """No credential in the ``live_voice`` pool can serve a call.

    Fails *closed*. A call is a stream, not a request, so there is no partial
    answer and no failover mid-sentence: the only honest outcomes are "start,
    on a working credential" and "do not start".
    """

    reason = REASON_QUOTA_EXHAUSTED
