"""The capability registry: the honest answer to "can this bot do X?".

The brief asks for VPN, OpenVPN and TQI adapters. Two of those integrations do
not exist on this deployment, and the point of these tests is that the bot says
so rather than improvising around the gap. The third — the VPN bot — exposes a
known set of endpoints, and the registry reports exactly those and no more.

The other property under test is the one the whole tool layer rests on: no
credential leaves this module. The shared secret is read only to decide whether
the client is configured, and it must be impossible for it to appear in the
report.
"""
import pytest

from app import config, service_adapters

SECRET = "vpnbot-shared-secret-do-not-leak"


@pytest.fixture(autouse=True)
def adapter_env(monkeypatch):
    monkeypatch.setattr(config, "GROUP_TRIAL_ENABLED", True)
    monkeypatch.setattr(config, "VPNBOT_API_URL", "")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", "")
    monkeypatch.setattr(config, "AGENT_ENABLED", False)
    monkeypatch.setattr(config, "AGENT_REPOSITORIES", [])
    yield


def by_name(name):
    return next(e for e in service_adapters.capabilities() if e["name"] == name)


# ── The VPN bot ───────────────────────────────────────────────────────────
def test_the_vpn_bot_is_unconfigured_without_a_url_and_secret():
    entry = by_name("vpn_bot")
    assert entry["state"] == service_adapters.UNCONFIGURED
    assert entry["operations"] == []


def test_the_vpn_bot_is_available_with_both(monkeypatch):
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)

    entry = by_name("vpn_bot")

    assert entry["state"] == service_adapters.AVAILABLE
    # The complete set, and it is asserted exactly rather than as a subset so
    # that an operation the code does not implement cannot be added to the
    # report without this test being changed on purpose.
    assert set(entry["operations"]) == {
        "health",
        "status",
        "acquisition.invite",
        "subscription.lookup",
        "service.status",
        "admin.service.enabled",
        "admin.notifications",
        "admin.plan.active",
        "admin.balance",
        "admin.orders.sweep",
        "admin.transaction.status",
    }


def test_every_reported_vpn_operation_is_one_this_bot_can_actually_call():
    """The registry names endpoints; ``app/vpnbot.py`` implements them.

    This is the test that keeps the report from becoming a wish list. Every
    dotted name in the registry has to correspond to a real wrapper and a real
    path constant in the client, so "the assistant can look up a subscription"
    is a claim with code behind it.
    """
    from app import vpnbot

    implemented = {
        "health": vpnbot.HEALTH_PATH,
        "status": vpnbot.STATUS_PATH,
        "acquisition.invite": vpnbot.INVITE_PATH,
        "subscription.lookup": vpnbot.SUBSCRIPTION_LOOKUP_PATH,
        "service.status": vpnbot.SERVICE_STATUS_PATH,
        "admin.service.enabled": vpnbot.SERVICE_ENABLED_PATH,
        "admin.notifications": vpnbot.NOTIFICATIONS_PATH,
        "admin.plan.active": vpnbot.PLAN_ACTIVE_PATH,
        "admin.balance": vpnbot.BALANCE_PATH,
        "admin.orders.sweep": vpnbot.ORDERS_SWEEP_PATH,
        "admin.transaction.status": vpnbot.TRANSACTION_STATUS_PATH,
    }

    assert set(implemented) == set(service_adapters._VPN_OPERATIONS)
    for path in implemented.values():
        assert path.startswith("/internal/")


def test_the_vpn_bot_reports_what_it_cannot_do(monkeypatch):
    """The gap is stated, so the assistant explains it instead of inventing."""
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)

    entry = by_name("vpn_bot")

    assert "config.generate" in entry["unsupported"]
    assert "service.restart" in entry["unsupported"]
    # A lookup the API does implement is no longer reported as a gap. The list
    # is the honest answer to "what can this not do", so it has to shrink when
    # that changes.
    assert "subscription.lookup" not in entry["unsupported"]
    assert "subscription.lookup" in entry["operations"]


def test_switching_off_acquisition_does_not_report_the_whole_integration_as_dead(
    monkeypatch,
):
    """The semantics fix: one operation off is not the integration off.

    The acquisition flow is what this integration was originally built for, so
    it used to be that switching it off reported the whole entry as
    ``disabled``. That stopped being true when the reads and the writes were
    added — they do not go through the acquisition path and they work either
    way. Reporting the integration as dead would send an operator looking for a
    fault that does not exist, so the operation is named instead.
    """
    monkeypatch.setattr(config, "GROUP_TRIAL_ENABLED", False)
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)

    entry = by_name("vpn_bot")

    assert entry["state"] == service_adapters.AVAILABLE
    assert entry["acquisition_enabled"] is False
    assert entry["disabled_operations"] == ["acquisition.invite"]
    # Still offered, because the code behind it is still there.
    assert "subscription.lookup" in entry["operations"]


def test_nothing_is_reported_disabled_when_acquisition_is_on(monkeypatch):
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)

    entry = by_name("vpn_bot")

    assert entry["acquisition_enabled"] is True
    assert entry["disabled_operations"] == []


def test_the_secret_never_appears_in_the_report(monkeypatch):
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)

    blob = str(service_adapters.capabilities()) + str(service_adapters.summary())

    assert SECRET not in blob


# ── The integrations that do not exist ────────────────────────────────────
def test_openvpn_is_reported_absent_and_claims_nothing():
    entry = by_name("openvpn")
    assert entry["state"] == service_adapters.ABSENT
    assert entry["configured"] is False
    assert entry["operations"] == []
    assert entry["unsupported"], "an absent integration must say what is missing"


def test_tqi_is_reported_absent_and_claims_nothing():
    entry = by_name("tqi")
    assert entry["state"] == service_adapters.ABSENT
    assert entry["operations"] == []


# ── The coding agent ──────────────────────────────────────────────────────
def test_the_agent_is_disabled_when_the_bridge_is_switched_off(monkeypatch):
    monkeypatch.setattr(config, "AGENT_ENABLED", False)
    monkeypatch.setattr(config, "AGENT_REPOSITORIES", {"guardbot": "/root/guardbot"})
    assert by_name("codebuddy_agent")["state"] == service_adapters.DISABLED


def test_the_agent_reports_its_allowlisted_repositories(monkeypatch):
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_REPOSITORIES", {"guardbot": "/root/guardbot"})

    entry = by_name("codebuddy_agent")

    assert entry["state"] == service_adapters.AVAILABLE
    assert entry["repositories"] == ["guardbot"]


# ── Robustness ────────────────────────────────────────────────────────────
def test_a_broken_builder_is_reported_not_raised(monkeypatch):
    def boom():
        raise RuntimeError("no")

    monkeypatch.setattr(service_adapters, "_REGISTRY", (boom,))

    entries = service_adapters.capabilities()

    assert entries[0]["state"] == service_adapters.ABSENT
    assert "RuntimeError" in entries[0]["note"]


def test_for_name_finds_one_entry_and_none_for_an_unknown():
    assert service_adapters.for_name("vpn_bot")["name"] == "vpn_bot"
    assert service_adapters.for_name("nope") is None


def test_the_summary_line_names_every_state():
    line = service_adapters.summary_line()
    assert line.startswith("INTEGRATIONS:")
    assert "openvpn" in line
    assert "tqi" in line
    assert "absent=" in line
