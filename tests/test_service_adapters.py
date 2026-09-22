"""The capability registry: the honest answer to "can this bot do X?".

The brief asks for VPN, OpenVPN and TQI adapters. Two of those integrations do
not exist on this deployment, and the point of these tests is that the bot says
so rather than improvising around the gap. The third — the VPN bot — exposes two
endpoints and nothing else, and the registry reports exactly those.

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
    assert set(entry["operations"]) == {"health", "acquisition.invite"}


def test_the_vpn_bot_reports_what_it_cannot_do(monkeypatch):
    """The gap is stated, so the assistant explains it instead of inventing."""
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)

    entry = by_name("vpn_bot")

    assert "config.generate" in entry["unsupported"]
    assert "subscription.lookup" in entry["unsupported"]


def test_the_vpn_bot_is_disabled_when_the_acquisition_flow_is_off(monkeypatch):
    monkeypatch.setattr(config, "GROUP_TRIAL_ENABLED", False)
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", SECRET)
    assert by_name("vpn_bot")["state"] == service_adapters.DISABLED


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
