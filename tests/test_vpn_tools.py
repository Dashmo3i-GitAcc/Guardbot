"""The VPN reads, and the redactor that makes them safe to hand to a model.

The brief's two requirements sit in tension exactly once here: the assistant
must be able to answer questions about the VPN service, and no credential may
ever reach the model. A VPN service is described by a *connection string*, and
that string is the credential — so this file is mostly about what is absent from
an answer rather than what is in it.

Three properties, and each one is tested in the direction that fails loudly:

* **Authority.** The three reads are gated on ``vpn.read``, which no role bundle
  carries. The check is re-run server-side, so calling a read tool directly as
  somebody who was never offered it is refused rather than answered.
* **Absence.** A field that is never copied cannot be leaked by a redactor that
  misses it. The views copy an allowlist field by field, so the connection
  string is not in the answer to begin with — and the redactor is the second
  line of defence for a value that arrives inside a string.
* **Locality.** The VPN patterns are *not* in the global redactor. Guardbot's own
  identity handle is 32 hex characters and is deliberately a non-secret; a
  global rule for panel client ids would silently rewrite it. That is asserted
  here, because it is the kind of mistake that looks like a security
  improvement.

Nothing here talks to the VPN bot or to Telegram.
"""
import asyncio

import pytest

from app import admin_tools, agent_data, config, db, identity, rbac, vpnbot

OWNER = 999
MODERATOR = 777
MEMBER = 42
CHAT = -1001234567890

CUSTOMER = 424242
SERVICE_ID = 71

# What the VPN bot would send if it did *not* narrow its own output. The point
# of using a deliberately leaky fixture is that the assertions below are about
# guardbot's own narrowing, not about the other service's.
LEAKY_SERVICE = {
    "id": SERVICE_ID,
    "display_name": "کاربر نمونه",
    "plan_name": "ماهانه",
    "status": "active",
    "enable": True,
    "is_trial": False,
    "days_left": 12,
    "days_left_short": "۱۲ روز",
    "expires_at": 1_800_000_000,
    "total_bytes": 100 * 1024**3,
    "used_bytes": 3 * 1024**3,
    "limit_ip": 2,
    # Everything below must not survive the view.
    "sub_url": "https://panel.example/sub/deadbeefdeadbeef",
    "client_email": "71-0f1e2d3c",
    "uuid": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "config": "vless://0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0@panel.example:443?type=ws#user",
}


@pytest.fixture(autouse=True)
def vpn_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{MODERATOR}:moderator"])
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", "shared-secret")
    monkeypatch.setattr(config, "GROUP_TRIAL_ENABLED", True)
    db.init()
    db.admin_reset()
    yield
    db.admin_reset()


def install(monkeypatch, name: str, *, answer=None, error=None):
    """Replace one VPN client wrapper with a stub."""

    async def wrapper(*args, **kwargs):
        if error is not None:
            raise error
        return dict(answer or {})

    monkeypatch.setattr(vpnbot, name, wrapper)


def read(name: str, args: dict, *, actor_id: int = OWNER, chat_id: int = CHAT) -> dict:
    return asyncio.run(
        admin_tools.run_read_tool(
            name, args, principal=rbac.resolve(actor_id), chat_id=chat_id
        )
    )


# ══ AUTHORITY ═════════════════════════════════════════════════════════════
def test_the_reads_are_offered_only_to_the_owner():
    owner = set(admin_tools.tool_names_for(rbac.resolve(OWNER)))
    assert {"vpn_subscription_lookup", "vpn_service_status", "get_vpn_status"} <= owner

    for actor in (MODERATOR, MEMBER):
        names = set(admin_tools.tool_names_for(rbac.resolve(actor)))
        assert "vpn_subscription_lookup" not in names
        assert "vpn_service_status" not in names
        assert "get_vpn_status" not in names


@pytest.mark.parametrize(
    "name, args",
    [
        ("vpn_subscription_lookup", {"telegram_id": CUSTOMER}),
        ("vpn_service_status", {"service_id": SERVICE_ID}),
        ("get_vpn_status", {}),
    ],
)
def test_a_read_is_refused_server_side_for_somebody_who_was_never_offered_it(
    name, args, monkeypatch
):
    """Exposure is not authority. A hallucinated call is refused, not answered."""
    install(monkeypatch, "subscription_lookup", answer={"ok": True, "services": []})
    install(monkeypatch, "service_status", answer={"ok": True})
    install(monkeypatch, "status", answer={"ok": True})

    out = read(name, args, actor_id=MODERATOR)

    assert "error" in out
    assert "not permitted" in out["error"]


# ══ ABSENCE ═══════════════════════════════════════════════════════════════
def test_a_subscription_lookup_strips_the_connection_string(monkeypatch):
    install(
        monkeypatch,
        "subscription_lookup",
        answer={
            "ok": True,
            "found": True,
            "telegram_id": CUSTOMER,
            "has_service": True,
            "services": [LEAKY_SERVICE],
        },
    )

    out = read("vpn_subscription_lookup", {"telegram_id": CUSTOMER})
    blob = str(out)

    for leaked in ("sub_url", "client_email", "vless://", "panel.example", "0f1e2d3c"):
        assert leaked not in blob, f"{leaked} reached the model"
    assert out["count"] == 1
    assert out["services"][0]["id"] == SERVICE_ID
    assert out["services"][0]["days_left"] == 12


def test_a_service_lookup_strips_the_connection_string(monkeypatch):
    install(
        monkeypatch,
        "service_status",
        answer={"ok": True, "found": True, "service_id": SERVICE_ID, "service": LEAKY_SERVICE},
    )

    out = read("vpn_service_status", {"service_id": SERVICE_ID})
    blob = str(out)

    assert "sub_url" not in blob
    assert "vless://" not in blob
    assert out["found"] is True
    assert out["service"]["plan_name"] == "ماهانه"


def test_a_service_lookup_that_finds_nothing_is_a_decision_not_an_error(monkeypatch):
    """An unknown id is an answer. Reporting it as a failure would make the
    model apologise for a problem that does not exist."""
    install(monkeypatch, "service_status", answer={"ok": True, "found": False, "service_id": SERVICE_ID})

    out = read("vpn_service_status", {"service_id": SERVICE_ID})

    assert out["found"] is False
    assert "error" not in out


def test_a_user_who_has_never_used_the_bot_is_an_answer(monkeypatch):
    install(
        monkeypatch,
        "subscription_lookup",
        answer={"ok": True, "found": False, "telegram_id": CUSTOMER, "services": []},
    )

    out = read("vpn_subscription_lookup", {"telegram_id": CUSTOMER})

    assert out == {
        "found": False,
        "telegram_id": CUSTOMER,
        "has_service": False,
        "count": 0,
        "services": [],
    }


def test_the_status_read_maps_the_booleans_and_nothing_else(monkeypatch):
    install(
        monkeypatch,
        "status",
        answer={
            "ok": True,
            "service": "vpn-bot",
            "acquisition": True,
            "admin_writes": False,
            "panel_configured": True,
            "bot_username_configured": True,
            "shared_secret": "must-not-appear",
        },
    )

    out = read("get_vpn_status", {})

    assert out == {
        "service": "vpn-bot",
        "acquisition_enabled": True,
        "admin_writes_enabled": False,
        "panel_configured": True,
        "bot_username_configured": True,
        # What this room has recorded and not yet approved, from our own table
        # rather than from the VPN bot. Empty here.
        "waiting_for_confirmation": [],
    }


def test_the_status_read_names_what_is_waiting_for_the_owner(monkeypatch):
    """The second half of "what is waiting for me?", from our own record."""
    from app import vpn_service

    install(monkeypatch, "status", answer={"ok": True, "service": "vpn-bot"})
    monkeypatch.setattr(
        vpn_service, "pending_lines", lambda *, chat_id=0, limit=5: ["abc vpn_balance ۵۰٬۰۰۰"]
    )

    out = read("get_vpn_status", {})

    assert out["waiting_for_confirmation"] == ["abc vpn_balance ۵۰٬۰۰۰"]


def test_an_unreachable_vpn_bot_does_not_report_an_empty_pending_list(monkeypatch):
    """No integration status means no claim about what is waiting either."""
    install(monkeypatch, "status", error=vpnbot.VpnBotError(vpnbot.ERR_UNREACHABLE, "down"))

    out = read("get_vpn_status", {})

    assert "error" in out
    assert "waiting_for_confirmation" not in out


@pytest.mark.parametrize(
    "name, args, wrapper",
    [
        ("vpn_subscription_lookup", {"telegram_id": CUSTOMER}, "subscription_lookup"),
        ("vpn_service_status", {"service_id": SERVICE_ID}, "service_status"),
        ("get_vpn_status", {}, "status"),
    ],
)
def test_an_unreachable_vpn_bot_is_an_explicit_error(name, args, wrapper, monkeypatch):
    """Not an empty success: a model told "no data" will fill the gap in itself."""
    install(monkeypatch, wrapper, error=vpnbot.VpnBotError(vpnbot.ERR_UNREACHABLE, "down"))

    out = read(name, args)

    assert "error" in out
    assert out["code"] == vpnbot.ERR_UNREACHABLE
    assert "down" not in str(out), "the transport detail is not the model's business"


def test_a_missing_argument_is_refused_before_the_call(monkeypatch):
    install(monkeypatch, "subscription_lookup", answer={"ok": True})

    assert "error" in read("vpn_subscription_lookup", {})
    assert "error" in read("vpn_service_status", {})


# ══ THE REDACTOR, AND WHY IT IS LOCAL ═════════════════════════════════════
@pytest.mark.parametrize(
    "text",
    [
        "vless://uuid@host:443?type=ws#name",
        "vmess://eyJhZGQiOiJob3N0In0=",
        "trojan://pass@host:443",
        "ss://YWVzOnBhc3M@host:8388",
        "hysteria2://pass@host:443",
        "tuic://uuid:pass@host:443",
        "sub_url=https://panel.example/sub/abc",
        "https://panel.example/subscription/abc123",
        "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    ],
)
def test_every_shape_of_vpn_credential_is_removed(text):
    out = agent_data.redact_vpn(text)

    assert agent_bridge_redacted() in out
    for fragment in ("panel.example", "0f1e2d3c", "vless://", "vmess://", "sub_url="):
        assert fragment not in out


def agent_bridge_redacted() -> str:
    from app import agent_bridge

    return agent_bridge.REDACTED


def test_the_vpn_redactor_is_not_the_global_one():
    """The mistake this guards against looks like a security improvement.

    Guardbot's own identity handle is 32 lowercase hex characters — the same
    shape as a panel client id — and it is *deliberately* not a secret, because
    it is how a person is addressed. Adding the VPN patterns to the global
    redactor would rewrite it everywhere and quietly break the identity layer,
    so the patterns live with the VPN views instead. This asserts both halves:
    the VPN redactor removes such a handle, and the identity view still returns
    it.
    """
    handle = identity.ensure(MEMBER)
    assert len(handle) == 32, "the premise of this test is a 32-hex handle"

    # The VPN redactor does remove it — it is a panel-id shape.
    assert handle not in agent_data.redact_vpn(handle)

    # And the identity view, which uses the global redactor, still returns it.
    view = agent_data.identity_view(MEMBER)
    assert view["uuid"] == handle

    # The global redactor on its own leaves it alone, which is the property that
    # makes the identity layer work.
    assert agent_data.redact(handle) == handle


# ══ THE BOUNDARY, AND THE REPORT ══════════════════════════════════════════
def test_no_vpn_tool_can_name_an_actor_a_room_or_a_permission():
    for name in (
        "vpn_subscription_lookup",
        "vpn_service_status",
        "get_vpn_status",
        "vpn_admin",
        "confirm_vpn_operation",
    ):
        names = {p for p, _, _ in admin_tools.TOOLS[name].parameters}
        for forbidden in ("actor_id", "actor_user_id", "chat_id", "is_owner",
                          "permissions", "owner"):
            assert forbidden not in names, f"{name} exposes {forbidden}"


def test_the_capability_report_lists_the_new_operations_and_the_gaps(monkeypatch):
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", "shared-secret")

    out = agent_data.service_status()
    vpn = next(e for e in out["integrations"] if e["name"] == "vpn_bot")

    assert "subscription.lookup" in vpn["operations"]
    assert "admin.balance" in vpn["operations"]
    assert "config.generate" in vpn["unsupported"]
    # The acquisition flow being on means nothing is reported as switched off.
    assert vpn["disabled_operations"] == []


def test_the_read_tool_never_calls_a_write_wrapper(monkeypatch):
    """A read tool is a read. The wrappers are separate, and this pins it."""
    called: list[str] = []

    async def record(name, *args, **kwargs):
        called.append(name)
        return {"ok": True, "code": "ok"}

    for name in (
        "set_service_enabled",
        "set_notifications_enabled",
        "set_plan_active",
        "adjust_balance",
        "reject_stale_orders",
        "set_transaction_status",
    ):
        monkeypatch.setattr(vpnbot, name, lambda *a, _n=name, **k: record(_n))

    install(monkeypatch, "status", answer={"ok": True, "service": "vpn-bot"})
    read("get_vpn_status", {})

    assert called == []
