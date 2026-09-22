"""What this bot is actually connected to, stated honestly.

The brief asks for operational access to the VPN ecosystem — the VPN bot, an
OpenVPN bot, a TQI panel — through narrow, typed adapters rather than a shell.
The first thing a real adapter layer has to do is tell the truth about what
exists, because the failure mode of the alternative is worse than not having the
feature: an assistant that *claims* it can build a configuration, on a
deployment whose upstream API has no such endpoint, will confidently promise an
operation and then fail in front of a customer.

So this module is a **capability registry**, not an integration. It answers one
question — "which of the integrations this bot might have are configured, and
which operations does each actually support?" — and it answers it from
configuration and from the modules that really exist. It never invents an
endpoint, and it never reports an operation that no code implements.

The three states a capability can be in, and they are different:

* **available** — the backend is configured and the operation is implemented.
* **unconfigured** — the operation is implemented, but this deployment has not
  pointed the bot at a backend (no URL, no shared secret).
* **absent** — there is no implementation at all. This is the honest answer for
  OpenVPN and TQI on this deployment, and it is reported rather than hidden so
  that an operator asking "why can't Nexus look up a subscription?" gets "there
  is no such integration" instead of a silent nothing.

Nothing here returns a credential. The VPN bot's shared secret is read only to
decide *whether* the client is configured — a boolean — and never leaves this
module. The same rule the rest of the codebase applies to the model applies
here: a secret must be structurally unable to reach an answer, not merely
instructed not to.
"""
from __future__ import annotations

import logging

from . import config

log = logging.getLogger("guardbot.adapters")

# Capability states.
AVAILABLE = "available"
UNCONFIGURED = "unconfigured"
ABSENT = "absent"
DISABLED = "disabled"

# The VPN bot's internal service API. This is the *complete* set of endpoints
# ``app/vpnbot.py`` implements, read from that module rather than guessed: a
# health probe and a single-purpose acquisition invite. There is deliberately no
# user lookup, no subscription lookup and no configuration generation, because
# the upstream service exposes none of those to this bot — inventing an endpoint
# here would produce a tool that always fails.
_VPN_OPERATIONS = ("health", "acquisition.invite")
# The operations a person might reasonably ask for that this integration does
# *not* support. Listed so the assistant can explain the gap instead of
# improvising around it.
_VPN_UNSUPPORTED = (
    "user.lookup",
    "subscription.lookup",
    "config.generate",
    "service.restart",
)


def _vpn() -> dict:
    """The VPN bot integration, from ``app/vpnbot.py`` and configuration."""
    configured = bool(config.VPNBOT_API_URL and config.VPNBOT_SHARED_SECRET)
    if not config.GROUP_TRIAL_ENABLED:
        state = DISABLED
    elif configured:
        state = AVAILABLE
    else:
        state = UNCONFIGURED
    return {
        "name": "vpn_bot",
        "state": state,
        "configured": configured,
        "operations": list(_VPN_OPERATIONS) if state == AVAILABLE else [],
        "unsupported": list(_VPN_UNSUPPORTED),
        "note": (
            "The VPN bot's internal API exposes a health probe and a one-way "
            "acquisition invite. It does not expose user records, subscriptions "
            "or configuration generation to this bot, so those are not offered."
        ),
    }


def _openvpn() -> dict:
    """The OpenVPN integration. There is none on this deployment.

    Reported as ``absent`` rather than omitted. The brief is explicit that an
    adapter must not be fabricated, and the honest answer to "can you look up
    this OpenVPN client?" is that no such integration exists here.
    """
    return {
        "name": "openvpn",
        "state": ABSENT,
        "configured": False,
        "operations": [],
        "unsupported": [
            "user.lookup",
            "service.status",
            "client.create",
            "client.renew",
            "config.generate",
        ],
        "note": (
            "No OpenVPN integration is implemented or configured on this "
            "deployment. Nothing is claimed for it."
        ),
    }


def _tqi() -> dict:
    """The TQI panel integration. There is none on this deployment.

    ``README``/``AgentMD`` state the boundary this preserves: this bot does not
    talk to the panel. Creating a panel client is an operator decision with its
    own credential and its own review, not something to invent behind a tool.
    """
    return {
        "name": "tqi",
        "state": ABSENT,
        "configured": False,
        "operations": [],
        "unsupported": [
            "status",
            "user.lookup",
            "service.lookup",
            "config.lookup",
        ],
        "note": (
            "No TQI panel integration is implemented or configured on this "
            "deployment. This bot does not hold panel credentials."
        ),
    }


def _agent() -> dict:
    """The coding-agent bridge, as a capability rather than an operation."""
    from . import agent_bridge

    enabled = bool(config.AGENT_ENABLED)
    try:
        repos = list(agent_bridge.repository_names())
    except Exception:  # noqa: BLE001 - a capability report must never raise
        log.exception("could not read the agent repository list")
        repos = []
    return {
        "name": "codebuddy_agent",
        "state": AVAILABLE if (enabled and repos) else DISABLED,
        "configured": bool(repos),
        "operations": ["task.submit", "task.status", "task.confirm", "task.cancel"],
        "unsupported": [],
        "repositories": repos,
        "note": (
            "Engineering work is delegated to the host coding agent through the "
            "spool; the assistant cannot execute code or shell itself."
        ),
    }


# The order the report is presented in: the things a person asks about first.
_REGISTRY = (_vpn, _openvpn, _tqi, _agent)


def capabilities() -> list[dict]:
    """Every integration, and exactly what each can and cannot do.

    Never raises: an integration whose configuration cannot be read is reported
    as ``absent`` with the error noted, because a status report that crashes is
    a status report an operator cannot use to diagnose anything.
    """
    out: list[dict] = []
    for build in _REGISTRY:
        try:
            out.append(build())
        except Exception as exc:  # noqa: BLE001 - a capability report never raises
            log.exception("could not build the capability report for %s", build.__name__)
            out.append(
                {
                    "name": build.__name__.lstrip("_"),
                    "state": ABSENT,
                    "configured": False,
                    "operations": [],
                    "unsupported": [],
                    "note": f"the integration could not be inspected: {type(exc).__name__}",
                }
            )
    return out


def for_name(name: str) -> dict | None:
    """One integration's capability, or ``None``."""
    wanted = (name or "").strip().lower()
    for entry in capabilities():
        if entry.get("name") == wanted:
            return entry
    return None


def summary() -> dict:
    """A compact, log-safe roll-up for the operator's status line."""
    entries = capabilities()
    return {
        "available": [e["name"] for e in entries if e["state"] == AVAILABLE],
        "unconfigured": [e["name"] for e in entries if e["state"] == UNCONFIGURED],
        "absent": [e["name"] for e in entries if e["state"] == ABSENT],
        "disabled": [e["name"] for e in entries if e["state"] == DISABLED],
    }


def summary_line() -> str:
    """One line for the startup log and ``/nexus status``."""
    state = summary()
    parts = []
    if state["available"]:
        parts.append("available=" + ",".join(state["available"]))
    if state["unconfigured"]:
        parts.append("unconfigured=" + ",".join(state["unconfigured"]))
    if state["disabled"]:
        parts.append("disabled=" + ",".join(state["disabled"]))
    if state["absent"]:
        parts.append("absent=" + ",".join(state["absent"]))
    return "INTEGRATIONS: " + (" ".join(parts) or "none")
