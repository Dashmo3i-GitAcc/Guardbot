"""The operational read tools: who is offered them, and what they answer.

The security invariants the tool layer rests on are asserted here rather than
only in the older suites, because these tools are new and they read more than
the ones that came before:

* a read tool is offered only to a principal who holds its permission, so a
  member is never given a window into the bot's operational history;
* **no tool has a parameter that names an identity or a room** — the actor and
  the chat come from the server, so a forged one is not rejected, it is
  inexpressible;
* a dispatch never trusts an argument the schema does not declare; and
* nothing a tool returns can carry a credential.
"""
import asyncio

import pytest

from app import admin_tools, config, db, nexus, rbac

OWNER = 999
MODERATOR = 777
HELPER = 888
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999

NEW_TOOLS = (
    "get_identity",
    "search_events",
    "get_nexus_diagnostics",
    "get_service_status",
)


@pytest.fixture(autouse=True)
def tools_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(
        config, "CONFIG_ADMINS", [f"{MODERATOR}:moderator", f"{HELPER}:helper"]
    )
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "ADMIN_CONTEXT_WINDOW", 6 * 3600)
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_REPOSITORIES", {"guardbot": "/root/guardbot"})
    db.init()
    db.admin_reset()
    db.awareness_reset()
    nexus.reset_state()
    yield
    db.admin_reset()
    db.awareness_reset()
    nexus.reset_state()


def run(name, args, *, principal, chat_id=CHAT):
    return asyncio.run(
        admin_tools.run_read_tool(name, args, principal=principal, chat_id=chat_id)
    )


# ── Exposure ──────────────────────────────────────────────────────────────
def test_the_operational_tools_are_declared_read_only():
    for name in NEW_TOOLS:
        assert name in admin_tools.TOOLS, name
        assert admin_tools.TOOLS[name].kind == admin_tools.KIND_READ


def test_a_member_is_offered_none_of_them():
    guest = rbac.resolve(MEMBER)
    offered = set(admin_tools.tool_names_for(guest))
    assert not (offered & set(NEW_TOOLS))


def test_a_moderator_is_offered_them():
    """They are the floor permission — the observational administrator."""
    offered = set(admin_tools.tool_names_for(rbac.resolve(MODERATOR)))
    assert set(NEW_TOOLS) <= offered


def test_a_helper_is_offered_them():
    offered = set(admin_tools.tool_names_for(rbac.resolve(HELPER)))
    assert set(NEW_TOOLS) <= offered


def test_only_the_owner_is_offered_the_agent_task_record():
    assert "get_agent_status" in admin_tools.tool_names_for(rbac.resolve(OWNER))
    assert "get_agent_status" not in admin_tools.tool_names_for(
        rbac.resolve(MODERATOR)
    )


# ── The invariant: no identity or room parameter ──────────────────────────
def test_no_tool_can_name_an_actor_a_room_or_a_permission():
    for spec in admin_tools.TOOLS.values():
        names = {p for p, _, _ in spec.parameters}
        for forbidden in ("actor_id", "actor_user_id", "chat_id", "is_owner",
                          "permissions", "owner"):
            assert forbidden not in names, f"{spec.name} exposes {forbidden}"


def test_the_operational_tools_take_no_room_parameter():
    for name in NEW_TOOLS:
        names = {p for p, _, _ in admin_tools.TOOLS[name].parameters}
        assert "chat_id" not in names, name


# ── Dispatch ──────────────────────────────────────────────────────────────
def test_get_identity_defaults_to_the_person_asking():
    out = run("get_identity", {}, principal=rbac.resolve(MODERATOR))
    assert out["user_id"] == MODERATOR


def test_get_identity_looks_up_another_person_by_id():
    out = run("get_identity", {"user_id": MEMBER}, principal=rbac.resolve(MODERATOR))
    assert out["user_id"] == MEMBER
    assert out["is_admin"] is False


def test_search_events_uses_the_servers_room_not_the_arguments():
    """A ``chat_id`` in the arguments is not declared, so it is not honoured."""
    db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                   chat_id=CHAT, interface="python", role="owner")
    db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                   chat_id=OTHER_CHAT, interface="python", role="owner")

    out = run(
        "search_events",
        {"source": "admin", "chat_id": OTHER_CHAT},
        principal=rbac.resolve(MODERATOR),
        chat_id=CHAT,
    )

    assert all(e["chat_id"] == CHAT for e in out["events"])


def test_search_events_reports_an_unknown_source():
    out = run("search_events", {"source": "nonsense"}, principal=rbac.resolve(MODERATOR))
    assert "error" in out


def test_get_nexus_diagnostics_uses_the_servers_room():
    out = run("get_nexus_diagnostics", {}, principal=rbac.resolve(MODERATOR))
    assert out["chat_id"] == CHAT
    assert "reasons" in out


def test_get_service_status_reports_the_integrations():
    out = run("get_service_status", {}, principal=rbac.resolve(MODERATOR))
    names = {e["name"] for e in out["integrations"]}
    assert {"vpn_bot", "openvpn", "tqi", "codebuddy_agent"} <= names


def test_get_agent_status_with_an_id_returns_the_full_record():
    db.agent_task_create(
        "task9", actor_id=OWNER, chat_id=CHAT, repository="guardbot",
        repo_path="/root/guardbot", task="do it", operation="edit",
    )
    out = run(
        "get_agent_status", {"request_id": "task9"}, principal=rbac.resolve(OWNER)
    )
    assert out["request_id"] == "task9"
    assert out["status"] == "queued"


def test_get_agent_status_without_an_id_lists_tasks():
    out = run("get_agent_status", {}, principal=rbac.resolve(OWNER))
    assert "recent" in out and "active" in out


# ── Authorization at the boundary, not only at the declaration ────────────
# Being offered a tool is a courtesy to the model; being *served* one is the
# server's decision. These assert the second, because the declarations alone
# would let a hallucinated tool name read the operational history.
def test_a_member_is_refused_at_the_dispatch():
    guest = rbac.resolve(MEMBER)
    assert admin_tools.tool_names_for(guest) == (), "precondition: offered nothing"
    for name, args in (
        ("get_identity", {}),
        ("search_events", {}),
        ("get_nexus_diagnostics", {}),
        ("get_service_status", {}),
        ("get_agent_status", {}),
        ("list_admins", {}),
    ):
        out = run(name, args, principal=guest)
        assert "not permitted" in out.get("error", ""), name
        assert "user_id" not in out and "integrations" not in out, name


def test_an_admin_is_refused_a_tool_above_their_permission():
    """A moderator observes; they do not get the coding-agent's task record."""
    out = run("get_agent_status", {}, principal=rbac.resolve(MODERATOR))
    assert "not permitted" in out["error"]


def test_every_declared_permission_is_enforced_by_the_dispatch():
    """The generic invariant: no tool may be served to a principal the
    exposure rule would not offer it to."""
    for name, spec in admin_tools.TOOLS.items():
        if not spec.permission:
            continue
        # A guest holds nothing, so every permission-gated tool must refuse.
        assert name not in admin_tools.tool_names_for(rbac.resolve(MEMBER))
        out = run(name, {}, principal=rbac.resolve(MEMBER))
        assert "not permitted" in out.get("error", ""), name


def test_a_permitted_actor_still_gets_the_tool():
    """The guard must refuse the unpermitted, not everyone."""
    out = run("get_nexus_diagnostics", {}, principal=rbac.resolve(MODERATOR))
    assert "reasons" in out


def test_an_unknown_tool_is_an_explicit_error():
    out = run("no_such_tool", {}, principal=rbac.resolve(MODERATOR))
    assert "unknown tool" in out["error"]


def test_a_read_tool_never_returns_a_credential():
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    db.audit_write(OWNER, "moderation.ban", outcome="ok", target_id=MEMBER,
                   chat_id=CHAT, detail=f"token {token}", interface="python",
                   role="owner")

    for name, args in (
        ("get_identity", {"user_id": OWNER}),
        ("search_events", {}),
        ("get_nexus_diagnostics", {}),
        ("get_service_status", {}),
        ("get_agent_status", {}),
    ):
        out = run(name, args, principal=rbac.resolve(OWNER))
        assert token not in str(out), name


# ── The tool schema is still closed to invention ──────────────────────────
def test_a_write_tool_still_refuses_an_undeclared_argument():
    request = admin_tools.parse_write_call(
        "ban_member",
        {"target_user_id": MEMBER, "chat_id": OTHER_CHAT},
        actor_id=OWNER,
        chat_id=CHAT,
    )
    assert request is None, "an undeclared argument must refuse the call"


def test_the_write_path_still_takes_the_actor_and_room_from_the_caller():
    request = admin_tools.parse_write_call(
        "ban_member", {"target_user_id": MEMBER}, actor_id=OWNER, chat_id=CHAT
    )
    assert request is not None
    assert request.actor_id == OWNER
    assert request.chat_id == CHAT
