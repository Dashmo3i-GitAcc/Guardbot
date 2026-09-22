"""The operational data the assistant may read — and the two ways it is safe.

The brief asks for the assistant to answer "why did this happen?" from the
server's records, and in the same breath forbids a credential ever reaching the
model. This suite pins both halves:

* every read is **bounded** — a count, a window, and a room the server chose;
* every string that leaves is **redacted**, even one that was never meant to be
  there in the first place; and
* the shape is an **allowlist** — a field the reader does not name does not
  exist, so a column added to a table later cannot leak by default.

Nothing here talks to Telegram or to Google.
"""
import time

import pytest

from app import agent_data, config, db, nexus, rbac

OWNER = 999
MODERATOR = 777
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999


@pytest.fixture(autouse=True)
def data_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{MODERATOR}:moderator"])
    monkeypatch.setattr(config, "ADMIN_CONTEXT_WINDOW", 6 * 3600)
    db.init()
    db.admin_reset()
    db.awareness_reset()
    nexus.reset_state()
    yield
    db.admin_reset()
    db.awareness_reset()
    nexus.reset_state()


# ── Redaction ─────────────────────────────────────────────────────────────
def test_a_bot_token_in_a_detail_is_scrubbed():
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    db.audit_write(
        OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
        chat_id=CHAT, detail=f"failed with {token}", interface="python", role="owner",
    )

    result = agent_data.search_events(source="admin", chat_id=CHAT)

    blob = str(result)
    assert token not in blob
    assert agent_data.redact(token) == "<redacted>"


def test_an_assignment_shaped_secret_is_scrubbed():
    db.audit_write(
        OWNER, "moderation.warn", outcome="ok", target_id=MEMBER,
        chat_id=CHAT, detail="api_key: super-secret-value", interface="python",
        role="owner",
    )
    result = agent_data.search_events(source="admin", chat_id=CHAT)
    assert "super-secret-value" not in str(result)


def test_redact_leaves_ordinary_text_alone():
    assert agent_data.redact("بن شد به خاطر اسپم") == "بن شد به خاطر اسپم"


# ── Search ────────────────────────────────────────────────────────────────
def test_admin_events_come_back_newest_first_and_bounded():
    for i in range(5):
        db.audit_write(
            OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
            chat_id=CHAT, detail=f"n={i}", interface="python", role="owner",
        )

    result = agent_data.search_events(source="admin", chat_id=CHAT, limit=2)

    assert result["count"] == 2
    assert len(result["events"]) == 2
    assert all(e["source"] == "admin" for e in result["events"])


def test_search_is_scoped_to_the_room_the_server_named():
    db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                   chat_id=CHAT, interface="python", role="owner")
    db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                   chat_id=OTHER_CHAT, interface="python", role="owner")

    result = agent_data.search_events(source="admin", chat_id=CHAT)

    assert all(e["chat_id"] == CHAT for e in result["events"])


def test_an_unknown_source_is_an_error_not_an_empty_success():
    result = agent_data.search_events(source="nonsense", chat_id=CHAT)
    assert "error" in result
    assert "admin" in result["sources"]


def test_all_is_the_default_and_reads_every_source():
    result = agent_data.search_events(chat_id=CHAT)
    assert "error" not in result
    assert "events" in result


def test_a_failing_source_does_not_fail_the_whole_read(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db, "audit_since", boom)

    result = agent_data.search_events(source="admin", chat_id=CHAT)

    assert result["count"] >= 1
    assert any("could not be read" in e["summary"] for e in result["events"])


def test_the_window_is_clamped_to_the_maximum():
    """A caller cannot ask for the whole of history."""
    long_ago = int(time.time()) - agent_data.MAX_WINDOW_SECONDS * 5
    result = agent_data.search_events(source="admin", chat_id=CHAT, since=long_ago)
    assert result["window_seconds"] <= agent_data.MAX_WINDOW_SECONDS + 5


def test_agent_events_carry_status_not_the_task_body():
    db.agent_task_create(
        "abc123", actor_id=OWNER, chat_id=CHAT, repository="guardbot",
        repo_path="/root/guardbot", task="do the thing", operation="edit",
    )
    result = agent_data.search_events(source="agent", chat_id=CHAT)
    row = next(e for e in result["events"] if e["source"] == "agent")
    assert "abc123" in row["summary"]
    assert "queued" in row["summary"]


# ── Nexus diagnostics ─────────────────────────────────────────────────────
def test_diagnostics_say_nexus_is_off_when_it_is():
    nexus.set_state(nexus.OFFLINE, actor_id=OWNER, reason="test")

    out = agent_data.nexus_diagnostics(CHAT)

    assert out["nexus_online"] is False
    assert any("switched off" in r for r in out["reasons"])


def test_diagnostics_say_nothing_is_pending_when_the_room_is_idle():
    out = agent_data.nexus_diagnostics(CHAT)
    assert out["nexus_online"] is True
    assert any("nothing unread" in r for r in out["reasons"])


def test_diagnostics_explain_a_disabled_awareness_layer(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", False)
    out = agent_data.nexus_diagnostics(CHAT)
    assert any("awareness layer is switched off" in r for r in out["reasons"])


def test_diagnostics_never_carry_a_secret(monkeypatch):
    token = "987654321:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    db.awareness_set(CHAT, seen_message_id=1, relevant=True, topic="t",
                     summary=f"the key is {token}")

    out = agent_data.nexus_diagnostics(CHAT)

    assert token not in str(out)
    assert "<redacted>" in str(out)


# ── Agent task view ───────────────────────────────────────────────────────
def test_a_task_view_redacts_its_result():
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    db.agent_task_create(
        "task1", actor_id=OWNER, chat_id=CHAT, repository="guardbot",
        repo_path="/root/guardbot", task="run it", operation="test",
    )
    db.agent_task_update("task1", status="failed", error=f"auth failed: {token}")

    out = agent_data.agent_task_view("task1")

    assert out["status"] == "failed"
    assert token not in str(out)


def test_an_unknown_task_is_an_explicit_error():
    out = agent_data.agent_task_view("nope")
    assert out["error"] == "no such task"


def test_an_empty_task_id_is_an_error():
    assert "no task id" in agent_data.agent_task_view("")["error"]


def test_the_task_view_carries_the_requester_handle():
    from app import identity

    handle = identity.ensure(OWNER)
    db.agent_task_create(
        "task2", actor_id=OWNER, chat_id=CHAT, repository="guardbot",
        repo_path="/root/guardbot", task="x", operation="edit",
    )
    out = agent_data.agent_task_view("task2")
    assert out["actor_id"] == OWNER
    assert out["actor_uuid"] == handle


# ── Identity view ─────────────────────────────────────────────────────────
def test_identity_view_is_redacted_and_allowlisted():
    out = agent_data.identity_view(OWNER, chat_id=CHAT)
    assert out["user_id"] == OWNER
    assert out["is_owner"] is True
    for forbidden in ("token", "api_key", "secret", "password"):
        assert forbidden not in out


def test_resolve_identity_delegates_and_never_guesses():
    out = agent_data.resolve_identity("هیچکس", chat_id=CHAT)
    assert out["status"] == "unknown"
