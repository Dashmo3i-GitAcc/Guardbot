"""Execute a moderation decision.

Kept free of any Telegram dependency so the delete-failure safety rules can be
unit tested. The Telegram handler injects the real actions.

Safety contract (this is the important part):

    EXPLICIT  -> attempt deletion
                 -> deletion succeeded: record the confirmed moderation action
                 -> deletion failed:  log it, apply NO strike / NO ban
    REVIEW    -> allow, no side effects
    SAFE      -> allow, no side effects

A member is never punished because of an internal error or a failed delete.
In this stage there is intentionally no ban/mute escalation at all: the only
automatic action is removing the media.
"""
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .decision import Decision, DecisionResult

log = logging.getLogger("moderation")


@dataclass
class Outcome:
    decision: Decision
    action: str            # "allowed" | "deleted" | "delete_failed"
    deleted: bool
    strike: int | None
    reason: str


async def enforce(
    result: DecisionResult,
    *,
    delete_media: Callable[[], Awaitable[None]],
    record_confirmed: Callable[[], int] | None = None,
) -> Outcome:
    """Apply the action for ``result``.

    ``delete_media`` must raise if the deletion did not happen. ``record_confirmed``
    is only called after a *successful* deletion.
    """
    if result.decision is not Decision.EXPLICIT:
        return Outcome(result.decision, "allowed", False, None, result.reason)

    try:
        await delete_media()
    except Exception as e:
        # TelegramError, network error, missing permission, ... all land here.
        log.warning("delete failed; no strike/ban applied: %s", e)
        return Outcome(
            result.decision, "delete_failed", False, None, f"delete failed: {e}"
        )

    strike = None
    if record_confirmed is not None:
        try:
            strike = record_confirmed()
        except Exception:
            log.exception("failed to record confirmed moderation action")

    matched = result.matched
    log.info(
        "confirmed explicit media deleted class=%s score=%.2f strike=%s",
        matched.label if matched else "?",
        matched.score if matched else 0.0,
        strike,
    )
    return Outcome(result.decision, "deleted", True, strike, result.reason)
