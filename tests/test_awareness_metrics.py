"""Awareness metrics and the operator's status line.

The brief asks for measurable awareness quality exposed through the existing
operational status tools. The metrics here are *derived* from the records a pass
already writes — the per-room understanding row, the captured window, the
pending query — rather than from a second counter store, so there is no
bookkeeping that can drift from the behaviour it describes.

The tests below pin two things: that the numbers move when the behaviour does,
and that ``/nexus status`` reports them and the integration registry without
ever failing.
"""
import pytest

from app import awareness, config, db, main, nexus, rbac

OWNER = 999
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999


@pytest.fixture(autouse=True)
def metrics_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    db.init()
    db.awareness_reset()
    nexus.reset_state()
    yield
    db.awareness_reset()
    nexus.reset_state()


def capture(chat_id, user_id, role, text):
    return awareness.capture(chat_id, user_id, role, "name", text)


# ── The metrics themselves ────────────────────────────────────────────────
def test_an_empty_deployment_reports_zeroes():
    m = awareness.metrics()
    assert m["rooms"] == 0
    assert m["passes"] == 0
    assert m["replies"] == 0
    assert m["pending_rooms"] == 0


def test_a_pass_is_counted_per_room():
    db.awareness_set(CHAT, seen_message_id=1, relevant=True, topic="t", summary="s")
    db.awareness_set(OTHER_CHAT, seen_message_id=1, relevant=False, topic="u", summary="s")

    m = awareness.metrics()

    assert m["rooms"] == 2
    assert m["passes"] == 2
    assert m["relevant"] == 1


def test_a_reply_is_counted_from_the_window():
    capture(CHAT, OWNER, awareness.ROLE_OWNER, "سلام")
    capture(CHAT, OWNER, awareness.ROLE_NEXUS, "سلام داداش")

    m = awareness.metrics()

    assert m["replies"] == 1
    assert m["window_messages"] == 2


def test_pending_counts_only_human_messages():
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "یکی هست؟")

    m = awareness.metrics()

    assert m["pending_rooms"] == 1
    assert m["pending_messages"] == 1


def test_metrics_report_whether_the_layer_is_on(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)
    assert awareness.metrics()["enabled"] is False


def test_the_metrics_line_is_counts_only():
    capture(CHAT, MEMBER, awareness.ROLE_MEMBER, "یک پیام محرمانه")
    line = awareness.metrics_line()
    assert "awareness[" in line
    assert "یک پیام محرمانه" not in line


# ── The operator's status line ────────────────────────────────────────────
def test_the_status_text_reports_metrics_and_integrations():
    text = main._nexus_status_text()
    assert "awareness[" in text
    assert "INTEGRATIONS:" in text


def test_the_status_text_survives_a_broken_metric_read(monkeypatch):
    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(awareness, "metrics_line", boom)

    text = main._nexus_status_text()

    assert "INTEGRATIONS:" in text, "one bad line must not lose the whole report"


def test_the_status_text_names_an_absent_integration():
    text = main._nexus_status_text()
    assert "openvpn" in text
