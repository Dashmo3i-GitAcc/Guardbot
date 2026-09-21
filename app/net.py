"""Which IP family the AI calls leave by, and how we know.

This server has a global IPv6 address and the AI endpoint publishes AAAA
records, so the *operating system* already prefers IPv6 here — the RFC 6724
default. That is the right behaviour and it is also entirely implicit: nothing
in this project said it wanted IPv6, nothing records that it got it, and if the
preference ever flipped to IPv4 there would be no evidence except a vague report
that the AI calls got slow or flaky.

This module makes that decision explicit in three steps, and deliberately stops
there:

  1. **Report.** ``describe()`` says whether IPv6 is actually usable (a global
     address, not just a link-local one), what each family costs to reach, and
     which family a connector will try first. It is logged once at startup, so
     "the AI calls are failing" can be diagnosed as an address-family problem
     rather than guessed at.
  2. **Prefer.** ``install_preference()`` orders resolved addresses IPv6-first
     for the AI hosts. This is a *reorder*, never a filter — every IPv4 address
     stays in the list — so a connector that walks the list gets IPv6 when it
     works and IPv4 when it does not. That is the "safe IPv4 fallback" the
     requirement asks for, and it is why this cannot break a working IPv4 path.
  3. **Scope.** ``getaddrinfo`` has no per-call hook, so the wrapper is
     process-wide; it is therefore restricted to a hard-coded set of AI host
     names and returns everything else untouched. It also defers to an explicit
     family request: if a caller asks for ``AF_INET`` it gets IPv4, unsorted.

What this module deliberately does not do: bind a source address, disable IPv4,
or reach for a third-party resolver. Each of those would turn a preference into
a dependency, and the failure mode of a wrong IPv6 preference should be a slower
call, never a call that cannot be made.
"""
from __future__ import annotations

import logging
import socket
import time

from . import config

log = logging.getLogger("guardbot.net")

# The hosts the AI SDKs talk to. Narrow on purpose — the wrapper below is
# process-wide, so it covers the smallest set of names that need it.
AI_HOSTS = frozenset({"generativelanguage.googleapis.com"})

IPV6 = "IPv6"
IPV4 = "IPv4"

_original_getaddrinfo = socket.getaddrinfo
_installed = False


def _family_name(family: int) -> str:
    return IPV6 if family == socket.AF_INET6 else IPV4


def _is_global_ipv6(address: str) -> bool:
    """A globally routable IPv6 address, not a link-local or loopback one.

    The distinction matters: every interface here has a ``fe80::`` address, so a
    naive "do we have IPv6?" check would say yes on a host that cannot reach the
    IPv6 internet at all.
    """
    text = (address or "").split("%")[0].strip().lower()
    if ":" not in text or text in ("::1", "::"):
        return False
    # Link-local (fe80::/10) and deprecated site-local (fec0::/10).
    return not (text.startswith("fe80:") or text.startswith("fec0:"))


def local_ipv6_addresses() -> list[str]:
    """Global IPv6 addresses this host holds. Empty when it has none."""
    found: list[str] = []
    try:
        infos = _original_getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
    except OSError:
        return []
    for info in infos:
        address = info[4][0]
        if _is_global_ipv6(address) and address not in found:
            found.append(address)
    return found


def ipv6_usable() -> bool:
    """Whether preferring IPv6 could possibly help on this host."""
    return bool(local_ipv6_addresses())


def resolve(host: str, port: int = 443) -> list[tuple[str, str]]:
    """``(family, address)`` for ``host``, in the order a connector would try.

    Uses the *original* resolver so the report describes the real world rather
    than our own wrapper — otherwise "is the preference working?" would be
    answered by the thing being questioned.
    """
    infos = _original_getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    out: list[tuple[str, str]] = []
    for info in infos:
        family, address = _family_name(info[0]), info[4][0]
        if (family, address) not in out:
            out.append((family, address))
    return out


def family_order(host: str) -> list[str]:
    """The distinct families for ``host``, in connection order."""
    order: list[str] = []
    for family, _ in resolve(host):
        if family not in order:
            order.append(family)
    return order


def probe(host: str, port: int = 443, timeout: float = 3.0) -> dict:
    """Time a TCP connect per family. ``None`` for a family that fails.

    A diagnostic rather than something on the request path: it opens real
    sockets, so it belongs in an operator's hands and in tests, not in a message
    handler. The first address of each family is used, which is what a connector
    would try first anyway.
    """
    result: dict = {}
    for family, address in resolve(host, port):
        if family in result:
            continue
        started = time.perf_counter()
        sock = socket.socket(socket.AF_INET6 if family == IPV6 else socket.AF_INET,
                             socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect((address, port))
            result[family] = round((time.perf_counter() - started) * 1000, 1)
        except OSError:
            result[family] = None
        finally:
            sock.close()
    return result


def describe() -> dict:
    """A one-shot description of the AI egress path, safe to log.

    Every value is a fact about this host or about DNS, never a credential.
    """
    host = next(iter(sorted(AI_HOSTS)))
    return {
        "ipv6_usable": ipv6_usable(),
        "local_ipv6": local_ipv6_addresses(),
        "order": family_order(host),
        "preferred": _installed,
    }


def _ordered_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """``getaddrinfo`` with IPv6 first for the AI hosts, IPv4 kept behind it.

    A reorder and not a filter: dropping the IPv4 entries would mean a broken
    IPv6 path becomes a broken AI integration, which is the opposite of what a
    *preference* should do. An explicit family request is passed straight
    through, because a caller asking for ``AF_INET`` has already decided.
    """
    infos = _original_getaddrinfo(host, port, family, type, proto, flags)
    if family in (socket.AF_INET, socket.AF_INET6):
        return infos
    name = host.decode() if isinstance(host, bytes) else host
    if name not in AI_HOSTS:
        return infos
    v6 = [info for info in infos if info[0] == socket.AF_INET6]
    rest = [info for info in infos if info[0] != socket.AF_INET6]
    return v6 + rest


def install_preference() -> str:
    """Order AI addresses IPv6-first, if that can help. Returns what happened.

    Idempotent, and it declines rather than guesses: with the switch off, or on
    a host with no global IPv6 address, it does nothing and says so. The return
    value is a short token so the caller can log one line without a branch.
    """
    global _installed
    if _installed:
        return "already_installed"
    if not config.AI_PREFER_IPV6:
        return "disabled"
    if not ipv6_usable():
        return "no_global_ipv6"
    socket.getaddrinfo = _ordered_getaddrinfo
    _installed = True
    return "installed"


def reset_for_tests() -> None:
    """Put the real resolver back. Never called on a running bot."""
    global _installed
    if _installed:
        socket.getaddrinfo = _original_getaddrinfo
        _installed = False
