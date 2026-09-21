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
