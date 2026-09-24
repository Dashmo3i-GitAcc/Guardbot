"""The Admin Control Center's own audit trail.

Every panel event goes through :func:`record`, and it never raises: an audit row
that cannot be written must not be the reason a login fails. The exception is
logged, which is where every other failure in this project goes.

The rows live in ``dashboard_audit`` — the panel's **own** table, not
``admin_audit``. The two trails are separate on purpose (AgentMD §53.13):

* the panel's actor is a configured operator with a password, not a Telegram
  user, so the bot's ``actor_id INTEGER NOT NULL`` does not describe it;
* the panel's events are logins, refusals and logouts — not administrative
  actions inside a chat;
* and writing them into ``admin_audit`` would put them in front of the bot's own
  audit view, which would be a change to the bot's behaviour. The panel is
  additive.

Retention is applied here rather than by the bot's prune loop, for the reason
``db.audit_prune`` gives: a rule that only runs when somebody remembers is not a
rule. The counter is in-process, so a restart simply resets when the next sweep
happens — which is harmless, because the sweep is idempotent.
"""
from __future__ import annotations

import logging

from app import config, db

log = logging.getLogger("guardbot.dashboard.audit")

# ── Actions ───────────────────────────────────────────────────────────────
# Stable strings, because they are the keys the panel's activity page filters on
# (M7) and the values an operator greps for. They are never built from input.
#
# There is deliberately no action for "the panel is unconfigured": that state
# refuses *every* request, so a row per attempt would be a way to grow the table
# from outside, and there is no event to investigate — the fix is a setting.
ACTION_LOGIN = "login"
ACTION_LOGIN_FAILED = "login.failed"
ACTION_LOGIN_THROTTLED = "login.throttled"
ACTION_LOGOUT = "logout"
ACTION_AUTHZ_REFUSED = "authz.refused"

OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"

# How many writes between retention sweeps. Low enough that a busy panel still
# bounds the table, high enough that the sweep is not on the login path's cost.
PRUNE_EVERY = 200

_writes_since_prune = 0


def record(
    action: str,
    *,
    outcome: str,
    actor: str = "",
    actor_id: int = 0,
    permission: str = "",
    role: str = "",
    detail: str = "",
    client_ip: str = "",
) -> None:
    """Append one panel event. Never raises."""
    global _writes_since_prune
    try:
        db.dashboard_audit_write(
            action,
            outcome=outcome,
            actor=actor,
            actor_id=actor_id,
            permission=permission,
            role=role,
            detail=detail,
            client_ip=client_ip,
        )
    except Exception:  # noqa: BLE001 - auditing must never break the panel
        log.exception("dashboard audit write failed action=%s", action)

    _writes_since_prune += 1
    if _writes_since_prune >= PRUNE_EVERY:
        _writes_since_prune = 0
        try:
            db.dashboard_audit_prune(config.DASHBOARD_AUDIT_RETENTION_SECONDS)
        except Exception:  # noqa: BLE001 - same reason
            log.exception("dashboard audit prune failed")


def reset_state() -> None:
    """Forget the in-process prune counter. For tests and for a fresh start."""
    global _writes_since_prune
    _writes_since_prune = 0


__all__ = [
    "ACTION_AUTHZ_REFUSED",
    "ACTION_LOGIN",
    "ACTION_LOGIN_FAILED",
    "ACTION_LOGIN_THROTTLED",
    "ACTION_LOGOUT",
    "OUTCOME_OK",
    "OUTCOME_REFUSED",
    "record",
    "reset_state",
]
