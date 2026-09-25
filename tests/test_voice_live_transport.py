"""The real transport's availability, and the dependency it is built on.

Three questions, all of them about deployability rather than behaviour:

* **Is the dependency declared?** ``py-tgcalls``, ``telethon`` and ``ntgcalls``
  must be in ``requirements.txt``. A transport whose library is not declared is
  a transport that works on the machine where somebody installed it by hand and
  nowhere else, which is the exact failure this file exists to prevent. Telethon
  is asserted separately because ``py-tgcalls`` offers it only as an *extra* —
  ``pip install py-tgcalls`` does not install it — and the adapter imports it
  directly.
* **Can the image carry the bootstrap?** The ``tools/`` directory has to be
  copied by the Dockerfile, or the documented one-time command cannot run inside
  the container, where ``/data`` is.
* **When it cannot hold a call, does it say which thing is missing?** An operator
  told "not configured" for a missing wheel and an operator told it for a missing
  credential go to two different places, and only one of them is right.

The availability checks are exercised with the library import stubbed, so they
run everywhere. The real import is asserted when the libraries are present and
skipped when they are not, because a fast unit-test environment is allowed not
to have a 41 MB native package installed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re

import pytest

from app.voice_live import telegram_voice as TV

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The three packages the real transport needs, in the order a reader would
# install them.
REQUIRED = ("py-tgcalls", "telethon", "ntgcalls")


def _libs():
    """A stand-in bundle: the availability logic only tests it for truthiness."""
    return TV._Libs(
        pytgcalls=object(),
        filters=object(),
        Device=object(),
        Direction=object(),
        Frame=object(),
        StreamFrames=object(),
        GroupCallConfig=object(),
        telethon=object(),
    )


def _transport(**overrides):
    settings = dict(
        api_id=12345, api_hash="not-a-real-hash", session_path="/tmp/x.session"
    )
    settings.update(overrides)
    return TV.PytgcallsTransport(**settings)


def _installed() -> bool:
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("pytgcalls", "telethon", "ntgcalls")
    )


# ══ The dependency is declared ════════════════════════════════════════════
def test_the_transport_dependencies_are_declared():
    """Pinned, not merely mentioned: the native wheel is the thing that decides
    whether the image can run at all, and an unpinned requirement is a wheel
    this deployment was never tested against."""
    text = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    for name in REQUIRED:
        pattern = rf"^{re.escape(name)}\s*[<>=~!]"
        assert re.search(pattern, text, re.M), f"{name} is not declared in requirements.txt"


def test_telethon_is_declared_in_its_own_right():
    """``py-tgcalls`` lists Telethon as an optional extra, so the line that
    installs the transport does not install the module the adapter imports.
    Asserting it separately is what stops the two from being confused."""
    text = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    assert re.search(r"^telethon\s*[<>=~!]", text, re.M)
    # ...and it is not relied upon through the extra. Only active lines count:
    # the comment above the declaration mentions the extra by name on purpose.
    active = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any("py-tgcalls[" in line for line in active)


def test_the_image_copies_the_session_bootstrap():
    """The bootstrap must be *in* the image: it writes to /data, which is the
    mounted volume, and it needs the libraries installed here rather than on the
    host. A Dockerfile that copies only app/ cannot run it."""
    text = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    assert re.search(r"^COPY\s+tools\b", text, re.M), "the Dockerfile does not copy tools/"


def test_the_image_proves_the_transport_imports_at_build_time():
    """Declaring the packages is not the same as the image being able to load
    them. The native wheel can fail for a reason a declaration cannot catch — a
    wheel built for the wrong ABI, or a missing libstdc++ — and the adapter
    imports it lazily, so that failure would arrive at the first join, mid-call,
    rather than at build time. The Dockerfile imports all three in a RUN step so
    it fails the build instead; this asserts that guard is still there."""
    text = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    run_lines = [
        line for line in text.splitlines() if line.lstrip().startswith("RUN")
    ]
    assert any(
        "import" in line
        and all(name in line for name in ("telethon", "pytgcalls", "ntgcalls"))
        for line in run_lines
    ), "the Dockerfile does not import the voice transport at build time"


# ══ Availability, decided without touching the network ════════════════════
def test_a_missing_library_is_reported_as_a_missing_library(monkeypatch):
    transport = _transport()
    monkeypatch.setattr(transport, "_import", lambda: None)
    assert transport.available is False
    assert transport.unavailable_reason == TV.UNAVAILABLE_LIBRARY


def test_missing_credentials_are_reported_as_missing_credentials(monkeypatch):
    transport = _transport(api_id=0, api_hash="")
    monkeypatch.setattr(transport, "_import", _libs)
    assert transport.available is False
    assert transport.unavailable_reason == TV.UNAVAILABLE_CREDENTIALS


def test_a_missing_session_path_is_also_a_credential_problem(monkeypatch):
    """The session *is* the credential, so a missing path and a missing api_hash
    are one answer: there is nothing here to log in with."""
    transport = _transport(session_path="")
    monkeypatch.setattr(transport, "_import", _libs)
    assert transport.unavailable_reason == TV.UNAVAILABLE_CREDENTIALS


def test_the_library_is_checked_before_the_credential(monkeypatch):
    """The order is the point. An operator missing both should be told to
    install the dependency first — sending them to my.telegram.org for a
    credential they cannot use yet is a wasted trip, and a confusing one."""
    transport = _transport(api_id=0, api_hash="", session_path="")
    monkeypatch.setattr(transport, "_import", lambda: None)
    assert transport.unavailable_reason == TV.UNAVAILABLE_LIBRARY


def test_a_configured_transport_reports_itself_available(monkeypatch):
    transport = _transport()
    monkeypatch.setattr(transport, "_import", _libs)
    assert transport.available is True
    assert transport.unavailable_reason == ""


def test_availability_is_decided_without_touching_the_network(monkeypatch):
    """A transport with a library, credentials and a path reports available even
    though the session file has not been checked — because checking it is a
    network round trip and a property is not the place for one. The session's
    authorisation is discovered at the first join, and reported as
    ``not_authorised`` there. Stated as a test so the optimism is deliberate and
    visible rather than accidental.
    """
    transport = _transport(session_path="/nonexistent/never-created.session")
    monkeypatch.setattr(transport, "_import", _libs)
    assert transport.available is True


def test_a_closed_transport_is_not_available(monkeypatch):
    transport = _transport()
    monkeypatch.setattr(transport, "_import", _libs)
    assert transport.available is True

    asyncio.run(transport.close())
    assert transport.available is False


def test_the_real_adapter_implements_the_whole_interface():
    """The protocol is the complete surface this subsystem may use on a call, so
    the real adapter has to satisfy all of it — not most of it."""
    assert isinstance(_transport(), TV.TelegramVoiceTransport)


def test_the_availability_reasons_are_distinct_machine_keys():
    """They are branched on, so they must be distinct and stable. A collision
    here would merge two fixes into one message."""
    reasons = {
        TV.UNAVAILABLE_LIBRARY,
        TV.UNAVAILABLE_CREDENTIALS,
        TV.UNAVAILABLE_NOT_AUTHORISED,
        TV.UNAVAILABLE_DISABLED,
    }
    assert len(reasons) == 4
    for reason in reasons:
        assert reason and reason == reason.lower() and " " not in reason


# ══ The real libraries, when they are installed ═══════════════════════════
@pytest.mark.skipif(
    not _installed(), reason="the voice-chat transport libraries are not installed here"
)
def test_the_real_libraries_import_and_expose_what_the_adapter_calls():
    """The adapter is written against these exact names. If a future release
    renames one, this fails at the name rather than at the first join."""
    libs = _transport()._import()
    assert libs is not None
    for name in (
        "pytgcalls",
        "filters",
        "Device",
        "Direction",
        "Frame",
        "StreamFrames",
        "GroupCallConfig",
        "telethon",
    ):
        assert getattr(libs, name) is not None, name
    assert hasattr(libs.pytgcalls, "PyTgCalls")
    assert hasattr(libs.telethon, "TelegramClient")
    # The two members speaker identity is built from, checked by name.
    assert hasattr(libs.Device, "MICROPHONE")
    for attribute in ("stream_frame", "chat_update", "call_participant", "stream_end"):
        assert hasattr(libs.filters, attribute), attribute


@pytest.mark.skipif(
    not _installed(), reason="the voice-chat transport libraries are not installed here"
)
def test_the_native_layer_imports_on_this_interpreter():
    """``ntgcalls`` is the native half, and the wheel tag is what decides whether
    this image can run at all. Importing it is the whole assertion: a wheel built
    for the wrong ABI or linked against a missing libstdc++ fails here, at build
    verification time, rather than in the middle of a call.
    """
    import ntgcalls

    assert ntgcalls.__file__, "ntgcalls imported without a file, which cannot be right"
    assert _transport()._import() is not None
