"""The outbound AI path: prefer IPv6, and never lose IPv4 doing it.

The property that matters here is not "IPv6 is used" — that depends on the host
and on DNS. It is that the preference is a *reorder*, so a broken or absent IPv6
path degrades to IPv4 instead of breaking the AI integration. Every test below
is about that, or about the module declining cleanly when it cannot help.

Nothing here opens a socket: the resolver is replaced at the module's own
``_original_getaddrinfo`` seam, which is exactly the function the wrapper calls.
"""
import socket

import pytest

from app import config, net


def _fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """IPv4 first, IPv6 last — the worst case for a preference to fix."""
    port = port or 443
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("5.6.7.8", port)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
    ]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """The real resolver back afterwards, whatever a test installed."""
    net.reset_for_tests()
    yield
    net.reset_for_tests()


# ── Telling a usable address from a decorative one ────────────────────────
def test_link_local_and_loopback_are_not_global():
    """Every interface here has a fe80:: address; none of them route anywhere."""
    assert net._is_global_ipv6("fe80::1") is False
    assert net._is_global_ipv6("fe80::1%eth0") is False
    assert net._is_global_ipv6("fec0::1") is False
    assert net._is_global_ipv6("::1") is False
    assert net._is_global_ipv6("::") is False
    assert net._is_global_ipv6("") is False
    assert net._is_global_ipv6("1.2.3.4") is False


def test_a_global_address_is_recognised():
    assert net._is_global_ipv6("2a14:7c0:1742:3be0::1") is True
    assert net._is_global_ipv6("2001:4860:4841:400::") is True


# ── The reorder, which is the whole point ─────────────────────────────────
def test_the_preference_reorders_without_dropping_ipv4(monkeypatch):
    """IPv6 first, and every IPv4 address still there to fall back to."""
    monkeypatch.setattr(net, "_original_getaddrinfo", _fake_getaddrinfo)

    out = net._ordered_getaddrinfo("generativelanguage.googleapis.com", 443)
    families = [info[0] for info in out]

    assert families[0] == socket.AF_INET6
    assert families.count(socket.AF_INET) == 2, "IPv4 must never be dropped"
    assert len(out) == 3, "the preference is a reorder, not a filter"


def test_a_non_ai_host_is_left_untouched(monkeypatch):
    """The wrapper is process-wide, so its scope has to be real."""
    monkeypatch.setattr(net, "_original_getaddrinfo", _fake_getaddrinfo)

    out = net._ordered_getaddrinfo("example.com", 443)

    assert [info[0] for info in out] == [
        socket.AF_INET,
        socket.AF_INET,
        socket.AF_INET6,
    ]


def test_an_explicit_family_request_is_respected(monkeypatch):
    """A caller that asked for IPv4 has already made the decision."""
    monkeypatch.setattr(net, "_original_getaddrinfo", _fake_getaddrinfo)

    out = net._ordered_getaddrinfo(
        "generativelanguage.googleapis.com", 443, socket.AF_INET
    )

    assert [info[0] for info in out] == [
        socket.AF_INET,
        socket.AF_INET,
        socket.AF_INET6,
    ]


def test_a_bytes_hostname_is_understood(monkeypatch):
    """getaddrinfo accepts bytes, so the scope check has to as well."""
    monkeypatch.setattr(net, "_original_getaddrinfo", _fake_getaddrinfo)

    out = net._ordered_getaddrinfo(b"generativelanguage.googleapis.com", 443)

    assert out[0][0] == socket.AF_INET6


# ── Declining, which is the other half of being safe ──────────────────────
def test_it_declines_when_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_PREFER_IPV6", False)
    monkeypatch.setattr(net, "ipv6_usable", lambda: True)

    assert net.install_preference() == "disabled"
    assert socket.getaddrinfo is net._original_getaddrinfo


def test_it_declines_without_a_global_ipv6_address(monkeypatch):
    """A link-local-only host cannot reach the IPv6 internet, so do nothing."""
    monkeypatch.setattr(config, "AI_PREFER_IPV6", True)
    monkeypatch.setattr(net, "ipv6_usable", lambda: False)

    assert net.install_preference() == "no_global_ipv6"
    assert socket.getaddrinfo is net._original_getaddrinfo


def test_it_installs_once_and_stays_installed(monkeypatch):
    monkeypatch.setattr(config, "AI_PREFER_IPV6", True)
    monkeypatch.setattr(net, "ipv6_usable", lambda: True)

    assert net.install_preference() == "installed"
    assert socket.getaddrinfo is net._ordered_getaddrinfo
    assert net.install_preference() == "already_installed"


# ── The report an operator reads ──────────────────────────────────────────
def test_the_report_has_a_stable_shape_and_no_secret(monkeypatch):
    monkeypatch.setattr(net, "ipv6_usable", lambda: True)
    monkeypatch.setattr(net, "family_order", lambda host: [net.IPV6, net.IPV4])
    monkeypatch.setattr(net, "local_ipv6_addresses", lambda: ["2a14:7c0::1"])

    state = net.describe()

    assert set(state) == {"ipv6_usable", "local_ipv6", "order", "preferred"}
    blob = repr(state).lower()
    for word in ("key", "token", "secret", "password"):
        assert word not in blob


def test_the_preference_is_installed_before_anything_can_connect():
    """It has to run at startup, before a handler can open a socket."""
    import inspect

    from app import main

    source = inspect.getsource(main.main)
    assert "net.install_preference()" in source
