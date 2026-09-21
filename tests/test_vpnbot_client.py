"""The signed client to the VPN bot's service API.

The scheme is mirrored, not shared — the two bots are separate projects on
different frameworks. ``test_signing_vector_matches_the_vpn_bot`` pins a fixed
vector that the VPN bot's ``tests/test_service_auth.py`` asserts too. If either
implementation drifts, one of the two tests fails, which is the only thing
keeping the mirror honest.
"""
import asyncio
import json

import pytest

from app import config, vpnbot

# Fixed inputs, shared with the VPN bot's test suite. Do not change one side.
VECTOR_SECRET = "test-shared-secret"
VECTOR_METHOD = "POST"
VECTOR_PATH = "/internal/acquisition/invite"
VECTOR_TIMESTAMP = 1758326400
VECTOR_NONCE = "0123456789abcdef0123456789abcdef"
VECTOR_BODY = b'{"telegram_id":123,"source":"group"}'
VECTOR_SIGNATURE = "34bfe93c19f199b1e9d20199845cea664b0f0da554e0f16b170563d4f7176950"


def test_signing_vector_matches_the_vpn_bot():
    assert vpnbot.body_digest(VECTOR_BODY) == (
        "fad3d88022cb8f0bc0343243dbb9872300e4d788eca04a01dbe42f059de3924a"
    )
    assert (
        vpnbot.signature(
            VECTOR_SECRET,
            VECTOR_METHOD,
            VECTOR_PATH,
            VECTOR_TIMESTAMP,
            VECTOR_NONCE,
            VECTOR_BODY,
        )
        == VECTOR_SIGNATURE
    )


def test_signing_string_is_exactly_the_documented_shape():
    assert vpnbot.signing_string(
        VECTOR_METHOD, VECTOR_PATH, VECTOR_TIMESTAMP, VECTOR_NONCE, VECTOR_BODY
    ) == "\n".join(
        [
            "POST",
            "/internal/acquisition/invite",
            "1758326400",
            "0123456789abcdef0123456789abcdef",
            "fad3d88022cb8f0bc0343243dbb9872300e4d788eca04a01dbe42f059de3924a",
        ]
    )


def test_sign_headers_are_complete_and_use_the_fixed_nonce():
    headers = vpnbot.sign_headers(
        VECTOR_SECRET,
        VECTOR_METHOD,
        VECTOR_PATH,
        VECTOR_BODY,
        timestamp=VECTOR_TIMESTAMP,
        nonce=VECTOR_NONCE,
    )
    assert headers[vpnbot.HEADER_SERVICE] == "guardbot"
    assert headers[vpnbot.HEADER_TIMESTAMP] == str(VECTOR_TIMESTAMP)
    assert headers[vpnbot.HEADER_NONCE] == VECTOR_NONCE
    assert headers[vpnbot.HEADER_SIGNATURE] == VECTOR_SIGNATURE


def test_nonce_is_unique_per_call():
    seen = {
        vpnbot.sign_headers(VECTOR_SECRET, "GET", "/internal/health")[
            vpnbot.HEADER_NONCE
        ]
        for _ in range(50)
    }
    assert len(seen) == 50


def test_a_different_path_produces_a_different_signature():
    """So a signature harvested from /health cannot be replayed on /invite."""
    health = vpnbot.signature(
        VECTOR_SECRET, "POST", "/internal/health", VECTOR_TIMESTAMP, VECTOR_NONCE
    )
    invite = vpnbot.signature(
        VECTOR_SECRET, "POST", VECTOR_PATH, VECTOR_TIMESTAMP, VECTOR_NONCE
    )
    assert health != invite


def test_editing_the_body_invalidates_the_signature():
    edited = json.dumps(
        {"telegram_id": 999, "source": "group"}, separators=(",", ":")
    ).encode()
    assert vpnbot.signature(
        VECTOR_SECRET, VECTOR_METHOD, VECTOR_PATH, VECTOR_TIMESTAMP, VECTOR_NONCE, edited
    ) != VECTOR_SIGNATURE


# ── Configuration guard ───────────────────────────────────────────────────
def test_an_unconfigured_client_refuses_to_send(monkeypatch):
    monkeypatch.setattr(config, "VPNBOT_API_URL", "")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", "")

    assert not vpnbot.is_configured()
    with pytest.raises(vpnbot.VpnBotError) as excinfo:
        asyncio.run(vpnbot.request_invite(123))
    assert excinfo.value.code == vpnbot.ERR_NOT_CONFIGURED


def test_a_transport_failure_is_reported_as_unreachable(monkeypatch):
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", "secret")
    monkeypatch.setattr(config, "VPNBOT_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(vpnbot.VpnBotError) as excinfo:
        asyncio.run(vpnbot.request_invite(123))
    assert excinfo.value.code == vpnbot.ERR_UNREACHABLE
