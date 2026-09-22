"""Signed client for the VPN bot's internal service API.

The two bots are separate projects — this one runs python-telegram-bot on
Python 3.12, the other aiogram on 3.10 — so they cannot share code. The signing
scheme is therefore mirrored from the VPN bot's ``app/services/service_auth.py``
rather than imported, and ``tests/test_vpnbot_client.py`` pins a fixed vector
that both implementations must reproduce. If one drifts, that test fails.

This module holds no credentials beyond the shared secret, and the secret buys
exactly one capability: asking the VPN bot whether it wants to invite someone.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time

import httpx

from . import config

log = logging.getLogger("guardbot.vpnbot")

HEADER_SERVICE = "X-QS-Service"
HEADER_TIMESTAMP = "X-QS-Timestamp"
HEADER_NONCE = "X-QS-Nonce"
HEADER_SIGNATURE = "X-QS-Signature"

SERVICE_NAME = "guardbot"

INVITE_PATH = "/internal/acquisition/invite"
HEALTH_PATH = "/internal/health"

# The operational surface. Reads and writes both travel as POST with a signed
# body, including the reads: the signature covers the method, the path, the
# timestamp, the nonce and the body hash, and it is verified against the path
# *without* the query string. A parameter sent as ``?telegram_id=`` would
# therefore sit outside the signature and a captured request could be replayed
# with a different id, so no parameter is ever sent that way.
STATUS_PATH = "/internal/status"
SUBSCRIPTION_LOOKUP_PATH = "/internal/subscription/lookup"
SERVICE_STATUS_PATH = "/internal/service/status"
SERVICE_ENABLED_PATH = "/internal/admin/service/enabled"
NOTIFICATIONS_PATH = "/internal/admin/notifications"
PLAN_ACTIVE_PATH = "/internal/admin/plan/active"
BALANCE_PATH = "/internal/admin/balance"
ORDERS_SWEEP_PATH = "/internal/admin/orders/sweep"
TRANSACTION_STATUS_PATH = "/internal/admin/transaction/status"

# Refusal codes that describe *our* side of the wire, as opposed to a decision
# the VPN bot made about the user.
ERR_NOT_CONFIGURED = "not_configured"
ERR_UNREACHABLE = "unreachable"
ERR_BAD_RESPONSE = "bad_response"
ERR_REFUSED = "refused"


class VpnBotError(Exception):
    """The VPN bot could not be asked. Never a decision about a user."""

    def __init__(self, code: str, detail: str = "", status: int | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.status = status


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def signing_string(
    method: str, path: str, timestamp: int | str, nonce: str, body: bytes = b""
) -> str:
    return "\n".join(
        [
            (method or "").upper(),
            path or "",
            str(timestamp),
            nonce or "",
            body_digest(body),
        ]
    )


def signature(
    secret: str,
    method: str,
    path: str,
    timestamp: int | str,
    nonce: str,
    body: bytes = b"",
) -> str:
    return hmac.new(
        (secret or "").encode("utf-8"),
        signing_string(method, path, timestamp, nonce, body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_headers(
    secret: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict:
    stamp = int(timestamp if timestamp is not None else time.time())
    token = nonce or secrets.token_hex(16)
    return {
        HEADER_SERVICE: SERVICE_NAME,
        HEADER_TIMESTAMP: str(stamp),
        HEADER_NONCE: token,
        HEADER_SIGNATURE: signature(secret, method, path, stamp, token, body),
    }


def is_configured() -> bool:
    return bool(config.VPNBOT_API_URL and config.VPNBOT_SHARED_SECRET)


async def _call(method: str, path: str, payload: dict | None = None) -> dict:
    """Sign and send one request, returning the decoded JSON body."""
    if not is_configured():
        raise VpnBotError(
            ERR_NOT_CONFIGURED,
            "VPNBOT_API_URL / VPNBOT_SHARED_SECRET are not set",
        )

    body = b""
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    headers = sign_headers(
        config.VPNBOT_SHARED_SECRET, method, path, body
    )
    headers["Content-Type"] = "application/json"

    url = f"{config.VPNBOT_API_URL}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=config.VPNBOT_TIMEOUT_SECONDS
        ) as client:
            response = await client.request(
                method, url, content=body, headers=headers
            )
    except httpx.HTTPError as exc:
        raise VpnBotError(ERR_UNREACHABLE, str(exc)) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise VpnBotError(
            ERR_BAD_RESPONSE, f"HTTP {response.status_code}", response.status_code
        ) from exc

    if response.status_code >= 400 or not isinstance(data, dict):
        raise VpnBotError(
            ERR_REFUSED,
            str(data.get("reason") if isinstance(data, dict) else data),
            response.status_code,
        )
    return data


async def request_invite(
    telegram_id: int,
    *,
    chat_id: int | None = None,
    message_id: int | None = None,
    source: str = "group",
) -> dict:
    """Ask the VPN bot for an invitation link.

    Returns its answer verbatim: ``{"ok": True, "deep_link": …}`` or
    ``{"ok": False, "reason": "already_used" | "already_invited" | …}``. A
    refusal is a decision, not an error — only an unreachable or misconfigured
    VPN bot raises.
    """
    payload = {"telegram_id": int(telegram_id), "source": source}
    if chat_id is not None:
        payload["chat_id"] = int(chat_id)
    if message_id is not None:
        payload["message_id"] = int(message_id)
    return await _call("POST", INVITE_PATH, payload)


async def health() -> dict:
    return await _call("GET", HEALTH_PATH)


async def status() -> dict:
    """What the integration can currently do, and whether its panel is wired."""
    return await _call("GET", STATUS_PATH)


async def subscription_lookup(telegram_id: int) -> dict:
    """Every VPN service belonging to one Telegram account.

    The answer never contains a subscription link or a panel client id — the
    VPN bot drops those fields before serialising, rather than trusting this
    side to redact them.
    """
    return await _call(
        "POST", SUBSCRIPTION_LOOKUP_PATH, {"telegram_id": int(telegram_id)}
    )


async def service_status(service_id: int) -> dict:
    return await _call("POST", SERVICE_STATUS_PATH, {"service_id": int(service_id)})


# ── Writes ────────────────────────────────────────────────────────────────
# Every write carries ``operator_id``: the Telegram id of the person on whose
# behalf this bot is acting. The VPN bot records it as ``guardbot:<id>`` in its
# own audit table, the way the dashboard records ``dashboard:<user>``. That id
# is *asserted* by us and not independently proven by the other side — the HMAC
# proves which service asked, and the owner-only check happened here. Recording
# it anyway is what makes "who changed this" answerable from either side.
async def set_service_enabled(
    service_id: int, enabled: bool, *, operator_id: int, interface: str = ""
) -> dict:
    return await _call(
        "POST",
        SERVICE_ENABLED_PATH,
        {
            "service_id": int(service_id),
            "enabled": bool(enabled),
            "operator_id": int(operator_id),
            "interface": interface or "",
        },
    )


async def set_notifications_enabled(
    telegram_id: int, enabled: bool, *, operator_id: int, interface: str = ""
) -> dict:
    return await _call(
        "POST",
        NOTIFICATIONS_PATH,
        {
            "telegram_id": int(telegram_id),
            "enabled": bool(enabled),
            "operator_id": int(operator_id),
            "interface": interface or "",
        },
    )


async def set_plan_active(
    plan_id: int, active: bool, *, operator_id: int, interface: str = ""
) -> dict:
    return await _call(
        "POST",
        PLAN_ACTIVE_PATH,
        {
            "plan_id": int(plan_id),
            "active": bool(active),
            "operator_id": int(operator_id),
            "interface": interface or "",
        },
    )


async def adjust_balance(
    telegram_id: int,
    delta: int,
    reason: str,
    *,
    operator_id: int,
    interface: str = "",
) -> dict:
    return await _call(
        "POST",
        BALANCE_PATH,
        {
            "telegram_id": int(telegram_id),
            "delta": int(delta),
            "reason": str(reason),
            "operator_id": int(operator_id),
            "interface": interface or "",
        },
    )


async def reject_stale_orders(
    days: int, reason: str, *, operator_id: int, interface: str = ""
) -> dict:
    return await _call(
        "POST",
        ORDERS_SWEEP_PATH,
        {
            "days": int(days),
            "reason": str(reason),
            "operator_id": int(operator_id),
            "interface": interface or "",
        },
    )


async def set_transaction_status(
    transaction_id: int,
    status: str,
    reason: str,
    *,
    operator_id: int,
    compensate: bool = False,
    interface: str = "",
) -> dict:
    return await _call(
        "POST",
        TRANSACTION_STATUS_PATH,
        {
            "transaction_id": int(transaction_id),
            "status": str(status),
            "reason": str(reason),
            "compensate": bool(compensate),
            "operator_id": int(operator_id),
            "interface": interface or "",
        },
    )
