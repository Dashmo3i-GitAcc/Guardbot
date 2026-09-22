"""The bridge's container half, through the one authority model.

``app/agent_service.py`` is thin on purpose, and what it is thin *about* is the
thing worth testing: it decides nothing that it was not handed. So these tests
are mostly about what reaches it and what leaves it —

* a member's request is refused, and refused by ``rbac`` rather than by a check
  in this module;
* the repository and the operation the model named are resolved against closed
  tables before anything is stored;
* a dangerous request is *recorded and not published*, so no runner can see it;
* an approval is the owner's, is server-side, and is unambiguous or is a
  question.

Two of them go through the real path rather than calling the service directly:
``admin_service.execute`` with a ``codebuddy_task`` request, and
``admin_tools.parse_write_call`` followed by the same. Those are the ones that
would catch a change to the operation table or the tool schema, which is exactly
where a mistake would be invisible from this module's own tests.
"""
import asyncio
import os

import pytest

from app import (
    admin_service,
    admin_tools,
    agent_bridge,
    agent_service,
    agent_spool,
    config,
    db,
    rbac,
)

OWNER = 999
ADMIN = 555
MEMBER = 42
OTHER = 43
CHAT = -1001234567890
BOT_ID = 1


@pytest.fixture(autouse=True)
def agent_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin"])
    monkeypatch.setattr(
        config,
        "AGENT_REPOSITORIES",
        {"guardbot": "/root/guardbot", "vpn-bot": "/opt/vpn-bot"},
    )
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_MAX_ACTIVE", 2)
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 1)
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "ADMIN_REQUEST_REPLAY_WINDOW", 120)
    monkeypatch.setenv("AGENT_SPOOL_DIR", str(tmp_path / "spool"))
    db.init()
    db.admin_reset()
    db.agent_reset()
    agent_spool.ensure()
    yield
    db.agent_reset()
    db.admin_reset()


class FakeGateway:
    """The narrowest gateway that satisfies the protocol. Records every call."""

    def __init__(self):
        self.calls = []

    async def bot_right(self, chat_id, right):
        return True

    async def promote(self, chat_id, user_id, rights):
        self.calls.append(("promote", user_id))

    async def demote(self, chat_id, user_id):
        self.calls.append(("demote", user_id))

    async def mute(self, chat_id, user_id):
        self.calls.append(("mute", user_id))

    async def unmute(self, chat_id, user_id):
        self.calls.append(("unmute", user_id))

    async def ban(self, chat_id, user_id):
        self.calls.append(("ban", user_id))

    async def unban(self, chat_id, user_id):
        self.calls.append(("unban", user_id))

    async def delete(self, chat_id, message_id):
        self.calls.append(("delete", message_id))

    async def warn(self, chat_id, user_id, reason):
        self.calls.append(("warn", user_id))

    async def member(self, chat_id, user_id):
        return {}


def _submit(
    *, actor=OWNER, repository="guardbot", task="fix the captcha bug",
    operation="edit", reply_mode="", chat=CHAT,
):
    return asyncio.run(
        agent_service.submit(
            admin_service.AdminRequest(
                operation="codebuddy_task",
                chat_id=chat,
                actor_id=actor,
                repository=repository,
                task=task,
                agent_operation=operation,
                reply_mode=reply_mode,
                interface=admin_service.INTERFACE_AI,
            )
        )
    )


def _requests():
    return sorted(os.listdir(agent_spool.requests_dir()))


# ── Submitting ────────────────────────────────────────────────────────────
def test_the_owner_gets_a_queued_task_and_a_published_request():
    result = _submit()
    assert result.ok
    assert result.outcome == admin_service.OUTCOME_OK
    row = db.agent_task_get(result.detail)
    assert row["status"] == "queued"
    assert row["repository"] == "guardbot"
    assert row["repo_path"] == "/root/guardbot"
    assert row["actor_id"] == OWNER
    assert f"{result.detail}.json" in _requests()


def test_the_published_envelope_carries_the_resolved_path_and_the_prompt():
    result = _submit(task="rename the parser")
    payload = agent_spool.read_request(result.detail)
    assert payload["repo_path"] == "/root/guardbot"
    assert payload["repository"] == "guardbot"
    assert "rename the parser" in payload["prompt"]
    assert "rename the parser" in payload["task"]


def test_the_published_envelope_does_not_name_an_executable():
    """Which binary runs is the host's decision — see ``tools/agent_runner.py``."""
    result = _submit()
    payload = agent_spool.read_request(result.detail)
    assert "cli" not in payload
    assert "cli_args" not in payload


def test_a_member_is_refused_and_nothing_is_recorded():
    result = _submit(actor=MEMBER)
    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_NOT_ADMIN
    assert db.agent_task_active() == []
    assert _requests() == []


def test_an_administrator_is_refused_because_the_permission_is_owner_only():
    """The structural half of "only the owner may ask".

    ``agent.request`` is in no role bundle, so an administrator does not hold it
    and the tool is not even offered to them. This is the same property, checked
    at the service rather than at the tool set.
    """
    assert not rbac.resolve(ADMIN).can("agent.request")
    result = _submit(actor=ADMIN)
    assert result.outcome == admin_service.OUTCOME_DENIED
    assert db.agent_task_active() == []


def test_a_repository_that_is_not_on_the_allowlist_is_refused_by_name():
    result = _submit(repository="somewhere-else")
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED
    assert "guardbot" in result.message


def test_a_path_that_is_not_an_allowlisted_root_is_refused():
    result = _submit(repository="/etc")
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED
    assert db.agent_task_active() == []


def test_an_operation_outside_the_vocabulary_is_refused():
    result = _submit(operation="sudo")
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED
    assert db.agent_task_active() == []


def test_an_empty_task_is_refused():
    result = _submit(task="   ")
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED


def test_the_bridge_being_switched_off_refuses_everything(monkeypatch):
    monkeypatch.setattr(config, "AGENT_ENABLED", False)
    result = _submit()
    assert result.outcome == admin_service.OUTCOME_AGENT_DISABLED
    assert db.agent_task_active() == []


def test_the_reply_mode_reaches_the_row():
    result = _submit(reply_mode="document")
    assert db.agent_task_get(result.detail)["reply_mode"] == "document"


def test_an_unknown_reply_mode_falls_back_to_text():
    result = _submit(reply_mode="telepathy")
    assert db.agent_task_get(result.detail)["reply_mode"] == "text"


# ── Idempotency and concurrency ───────────────────────────────────────────
def test_the_same_request_twice_is_a_duplicate_and_not_a_second_task():
    first = _submit(task="fix the same bug")
    second = _submit(task="fix the same bug")
    assert second.outcome == admin_service.OUTCOME_AGENT_DUPLICATE
    assert second.extra["task"]["request_id"] == first.detail
    assert len(db.agent_task_active()) == 1
    assert len(_requests()) == 1


def test_a_second_task_on_one_repository_is_refused_while_the_first_is_active():
    _submit(task="the first thing")
    result = _submit(task="a completely different thing")
    assert result.outcome == admin_service.OUTCOME_AGENT_BUSY
    assert result.detail == "repository_busy"


def test_a_different_repository_is_allowed_while_one_is_busy():
    _submit(task="the first thing", repository="guardbot")
    result = _submit(task="a different thing", repository="vpn-bot")
    assert result.ok


def test_the_global_ceiling_refuses_the_third_task(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 5)
    _submit(task="one", repository="guardbot")
    _submit(task="two", repository="vpn-bot")
    result = _submit(task="three", repository="guardbot")
    assert result.outcome == admin_service.OUTCOME_AGENT_BUSY
    assert result.detail == "busy"


def test_a_finished_task_does_not_block_a_new_one():
    first = _submit(task="the first thing")
    db.agent_task_update(first.detail, status="succeeded", finished_at=1)
    result = _submit(task="a completely different thing")
    assert result.ok


# ── Dangerous operations ──────────────────────────────────────────────────
def test_a_dangerous_operation_is_recorded_and_not_published():
    """The central safety property of the whole bridge.

    The task exists, so the owner can see it and approve it; the request file
    does not exist, so no runner can see it and nothing can start. The two facts
    together are what "waiting for the owner" means.
    """
    result = _submit(task="deploy the new build", operation="deploy")
    assert result.outcome == admin_service.OUTCOME_AGENT_WAITING
    row = db.agent_task_get(result.detail)
    assert row["status"] == "waiting_for_owner"
    assert row["danger"]
    assert _requests() == []


def test_the_task_text_can_make_an_ordinary_operation_dangerous():
    result = _submit(task="clean up the old branches and force push", operation="push")
    assert result.outcome == admin_service.OUTCOME_AGENT_WAITING
    assert _requests() == []


def test_a_dangerous_task_is_in_the_waiting_list_and_an_ordinary_one_is_not():
    waiting = _submit(task="deploy it", operation="deploy")
    _submit(task="add a test", repository="vpn-bot")
    ids = [r["request_id"] for r in db.agent_task_waiting()]
    assert ids == [waiting.detail]


def test_a_member_cannot_confirm_a_waiting_task():
    waiting = _submit(task="deploy it", operation="deploy")
    result = agent_service.confirm(actor_id=MEMBER, chat_id=CHAT)
    assert result.outcome == admin_service.OUTCOME_DENIED
    assert db.agent_task_get(waiting.detail)["status"] == "waiting_for_owner"
    assert _requests() == []


def test_an_administrator_cannot_confirm_either():
    _submit(task="deploy it", operation="deploy")
    result = agent_service.confirm(actor_id=ADMIN, chat_id=CHAT)
    assert result.outcome == admin_service.OUTCOME_DENIED


def test_confirming_with_nothing_pending_is_refused():
    result = agent_service.confirm(actor_id=OWNER, chat_id=CHAT)
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED
    assert result.detail == "nothing_pending"


def test_the_owner_confirming_one_waiting_task_releases_it():
    waiting = _submit(task="deploy it", operation="deploy")
    result = agent_service.confirm(actor_id=OWNER, chat_id=CHAT)
    assert result.ok
    row = db.agent_task_get(waiting.detail)
    assert row["status"] == "queued"
    assert row["confirmed_by"] == OWNER
    assert row["confirmed_at"] > 0
    assert f"{waiting.detail}.json" in _requests()


def test_a_bare_confirmation_with_two_waiting_tasks_is_a_question(monkeypatch):
    """The brief's rule about vague language, at the service boundary.

    «اوکی» with two dangerous tasks pending must not pick one. The result names
    both so the next message can be specific, and neither task moves.
    """
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 5)
    first = _submit(task="deploy the first", repository="guardbot", operation="deploy")
    second = _submit(task="deploy the second", repository="vpn-bot", operation="deploy")
    result = agent_service.confirm(actor_id=OWNER, chat_id=CHAT)
    assert result.outcome == admin_service.OUTCOME_AGENT_BUSY
    assert set(result.extra["candidates"]) == {first.detail, second.detail}
    assert db.agent_task_get(first.detail)["status"] == "waiting_for_owner"
    assert db.agent_task_get(second.detail)["status"] == "waiting_for_owner"
    assert _requests() == []


def test_naming_one_of_two_waiting_tasks_releases_that_one(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 5)
    first = _submit(task="deploy the first", repository="guardbot", operation="deploy")
    second = _submit(task="deploy the second", repository="vpn-bot", operation="deploy")
    result = agent_service.confirm(
        actor_id=OWNER, chat_id=CHAT, request_id=second.detail
    )
    assert result.ok
    assert db.agent_task_get(second.detail)["status"] == "queued"
    assert db.agent_task_get(first.detail)["status"] == "waiting_for_owner"


def test_naming_a_task_that_is_not_waiting_is_refused():
    _submit(task="deploy it", operation="deploy")
    result = agent_service.confirm(
        actor_id=OWNER, chat_id=CHAT, request_id="agent-does-not-exist"
    )
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED
    assert result.detail.startswith("not_waiting")


def test_confirming_twice_does_not_release_anything_the_second_time():
    _submit(task="deploy it", operation="deploy")
    assert agent_service.confirm(actor_id=OWNER, chat_id=CHAT).ok
    again = agent_service.confirm(actor_id=OWNER, chat_id=CHAT)
    assert not again.ok
    assert again.detail == "nothing_pending"


def test_a_confirmation_cannot_release_a_task_that_is_not_dangerous():
    """A bare «اوکی» must not start something the owner never saw described.

    The waiting list is what a confirmation resolves against, and a queued task
    is not in it — so an approval with nothing waiting is refused rather than
    applied to whatever happens to be running.
    """
    ordinary = _submit(task="add a test")
    result = agent_service.confirm(actor_id=OWNER, chat_id=CHAT)
    assert result.detail == "nothing_pending"
    assert db.agent_task_get(ordinary.detail)["status"] == "queued"


# ── Answering a question ──────────────────────────────────────────────────
def test_a_task_that_asked_a_question_is_not_in_the_confirmation_list():
    """The two meanings of ``waiting_for_owner``, told apart by ``started_at``.

    Confirming a task that had already begun would re-run work that was under
    way, which is the opposite of what the owner asked for. The filter is in the
    query, so it is not something a caller can get wrong.
    """
    asked = _submit(task="refactor the parser")
    db.agent_task_update(asked.detail, status="running", started_at=100)
    db.agent_task_update(asked.detail, status="waiting_for_owner")
    assert db.agent_task_waiting() == []
    assert agent_service.confirm(actor_id=OWNER, chat_id=CHAT).detail == "nothing_pending"


def test_answering_a_question_appends_it_and_requeues_the_task():
    asked = _submit(task="refactor the parser")
    db.agent_task_update(asked.detail, status="running", started_at=100)
    db.agent_task_update(asked.detail, status="waiting_for_owner")
    result = agent_service.resume(
        actor_id=OWNER, request_id=asked.detail, text="use the strict parser"
    )
    assert result.ok
    row = db.agent_task_get(asked.detail)
    assert row["status"] == "queued"
    assert "use the strict parser" in row["task"]
    assert "refactor the parser" in row["task"]


def test_answering_an_unapproved_dangerous_task_is_not_an_approval():
    waiting = _submit(task="deploy it", operation="deploy")
    result = agent_service.resume(
        actor_id=OWNER, request_id=waiting.detail, text="yes go ahead"
    )
    assert result.outcome == admin_service.OUTCOME_AGENT_WAITING
    assert db.agent_task_get(waiting.detail)["status"] == "waiting_for_owner"
    assert _requests() == []


def test_an_answer_that_makes_the_work_dangerous_does_not_start_it():
    """The hole this closes: turning a question into a backdoor approval.

    The answer is appended to the task and the danger is recomputed, so "yes,
    and deploy it afterwards" is caught rather than inheriting the permission
    the original question did not have.
    """
    asked = _submit(task="check the config")
    db.agent_task_update(asked.detail, status="running", started_at=100)
    db.agent_task_update(asked.detail, status="waiting_for_owner")
    result = agent_service.resume(
        actor_id=OWNER, request_id=asked.detail, text="yes, and then deploy it"
    )
    assert result.outcome == admin_service.OUTCOME_AGENT_WAITING
    row = db.agent_task_get(asked.detail)
    assert row["status"] == "waiting_for_owner"
    assert row["danger"]
    # The request file from the first run is still on disk — that is normal, it
    # is only removed by the retention prune — so what matters is that the task
    # did not go back to ``queued`` and that no *new* run can be claimed for it.
    assert not agent_bridge.transition_allowed(row["status"], "running")


def test_a_member_cannot_answer_a_question():
    asked = _submit(task="refactor the parser")
    db.agent_task_update(asked.detail, status="running", started_at=100)
    db.agent_task_update(asked.detail, status="waiting_for_owner")
    result = agent_service.resume(
        actor_id=MEMBER, request_id=asked.detail, text="do it my way"
    )
    assert result.outcome == admin_service.OUTCOME_DENIED


def test_an_empty_answer_is_refused():
    asked = _submit(task="refactor the parser")
    db.agent_task_update(asked.detail, status="running", started_at=100)
    db.agent_task_update(asked.detail, status="waiting_for_owner")
    result = agent_service.resume(
        actor_id=OWNER, request_id=asked.detail, text="   "
    )
    assert result.detail == "empty_answer"


# ── Cancelling ────────────────────────────────────────────────────────────
def test_the_owner_can_cancel_a_queued_task():
    task = _submit()
    result = agent_service.cancel(actor_id=OWNER, request_id=task.detail)
    assert result.ok
    assert db.agent_task_get(task.detail)["status"] == "cancelled"
    assert agent_spool.cancel_requested(task.detail)


def test_cancelling_something_that_already_finished_is_refused():
    task = _submit()
    db.agent_task_update(task.detail, status="succeeded", finished_at=1)
    result = agent_service.cancel(actor_id=OWNER, request_id=task.detail)
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED
    assert result.detail == "already_finished"


def test_a_member_cannot_cancel_the_owners_task():
    task = _submit()
    result = agent_service.cancel(actor_id=MEMBER, request_id=task.detail)
    assert result.outcome == admin_service.OUTCOME_DENIED
    assert db.agent_task_get(task.detail)["status"] == "queued"


def test_cancelling_a_task_that_does_not_exist_is_refused():
    result = agent_service.cancel(actor_id=OWNER, request_id="agent-nope")
    assert result.outcome == admin_service.OUTCOME_AGENT_REJECTED


# ── Status ────────────────────────────────────────────────────────────────
def test_the_status_report_is_owner_only():
    text = agent_service.status_text(actor_id=MEMBER)
    assert "مالک" in text or "⛔" in text
    assert "guardbot" not in text


def test_the_status_report_names_the_allowlist_and_the_ceiling():
    text = agent_service.status_text(actor_id=OWNER)
    assert "guardbot" in text
    assert "vpn-bot" in text


def test_the_status_report_shows_a_waiting_task_and_how_to_release_it():
    _submit(task="deploy it", operation="deploy")
    text = agent_service.status_text(actor_id=OWNER)
    assert "منتظر تأیید" in text
    assert "/agent confirm" in text


def test_the_status_report_never_shows_a_task_body():
    _submit(task="a very private instruction nobody should read")
    text = agent_service.status_text(actor_id=OWNER)
    assert "a very private instruction" not in text


def test_the_status_report_says_so_when_the_bridge_is_off(monkeypatch):
    monkeypatch.setattr(config, "AGENT_ENABLED", False)
    text = agent_service.status_text(actor_id=OWNER)
    assert "خاموش" in text


# ── Recovery ──────────────────────────────────────────────────────────────
def test_a_queued_task_with_no_request_file_is_republished_at_startup():
    """The restart case the brief names: no duplicate execution, no stranding.

    A process that died between the database write and the spool write leaves a
    task that is queued and invisible. ``recover`` publishes it, and because the
    request file is the runner's only source of work, publishing it once means
    running it once.
    """
    task = _submit()
    agent_spool.forget(task.detail)
    assert _requests() == []
    assert agent_service.recover() == 1
    assert f"{task.detail}.json" in _requests()


def test_recovery_does_not_touch_a_running_task():
    task = _submit()
    agent_spool.forget(task.detail)
    db.agent_task_update(task.detail, status="running", started_at=1)
    assert agent_service.recover() == 0
    assert _requests() == []


def test_recovery_does_not_republish_an_unapproved_dangerous_task():
    waiting = _submit(task="deploy it", operation="deploy")
    assert agent_service.recover() == 0
    assert _requests() == []
    assert db.agent_task_get(waiting.detail)["status"] == "waiting_for_owner"


def test_recovery_is_idempotent():
    task = _submit()
    agent_spool.forget(task.detail)
    assert agent_service.recover() == 1
    assert agent_service.recover() == 0


# ── Through the real path ─────────────────────────────────────────────────
def test_the_operation_runs_through_admin_service_and_not_around_it():
    """The integration that matters: the bridge is an operation, not a front door.

    If this passes, then every check ``admin_service.execute`` performs —
    the replay window, the idempotency table, the audit row, the actor's
    authority — applies to a coding request exactly as it applies to a ban.
    """
    request = admin_service.AdminRequest(
        operation="codebuddy_task",
        chat_id=CHAT,
        actor_id=OWNER,
        repository="guardbot",
        task="add a test for the parser",
        agent_operation="edit",
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_AI,
        at=int(__import__("time").time()),
    )
    result = asyncio.run(
        admin_service.execute(request, FakeGateway(), bot_id=BOT_ID)
    )
    assert result.ok
    assert result.operation == "codebuddy_task"
    assert db.agent_task_get(result.detail)


def test_a_member_going_through_admin_service_is_refused_and_audited():
    request = admin_service.AdminRequest(
        operation="codebuddy_task",
        chat_id=CHAT,
        actor_id=MEMBER,
        repository="guardbot",
        task="add a test for the parser",
        agent_operation="edit",
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_AI,
        at=int(__import__("time").time()),
    )
    result = asyncio.run(
        admin_service.execute(request, FakeGateway(), bot_id=BOT_ID)
    )
    assert not result.ok
    assert result.outcome == admin_service.OUTCOME_DENIED
    rows = db.audit_recent(5)
    assert any(r["action"] == "agent.task" for r in rows)


def test_the_tool_call_becomes_the_request_the_service_expects():
    request = admin_tools.parse_write_call(
        "codebuddy_task",
        {"repository": "guardbot", "task": "add a test", "operation": "edit"},
        actor_id=OWNER,
        chat_id=CHAT,
        request_id="rid",
    )
    assert request is not None
    assert request.operation == "codebuddy_task"
    assert request.repository == "guardbot"
    assert request.agent_operation == "edit"
    result = asyncio.run(
        admin_service.execute(request, FakeGateway(), bot_id=BOT_ID)
    )
    assert result.ok


def test_a_tool_call_cannot_smuggle_a_field_the_schema_does_not_declare():
    """No ``owner=true``, no ``approved=true``, no ``is_admin``.

    The brief names this explicitly. It is enforced by ``parse_write_call``
    refusing an undeclared argument, which means the field cannot even reach
    ``AdminRequest`` — and ``AdminRequest`` has nowhere to put it anyway.
    """
    for smuggled in ("owner", "is_owner", "approved", "is_admin", "allowed"):
        request = admin_tools.parse_write_call(
            "codebuddy_task",
            {"repository": "guardbot", "task": "x", smuggled: True},
            actor_id=OWNER,
            chat_id=CHAT,
        )
        assert request is None, smuggled


def test_the_owner_is_offered_the_tools_and_an_administrator_is_not():
    owner_tools = admin_tools.tool_names_for(rbac.resolve(OWNER))
    admin_tools_names = admin_tools.tool_names_for(rbac.resolve(ADMIN))
    for name in (
        "codebuddy_task",
        "confirm_agent_task",
        "cancel_agent_task",
        "answer_agent_task",
        "get_agent_status",
    ):
        assert name in owner_tools, name
        assert name not in admin_tools_names, name


def test_a_guest_is_offered_no_agent_tool_even_when_guest_tools_are_on(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", True)
    names = admin_tools.tool_names_for(rbac.guest(MEMBER))
    assert "get_agent_status" not in names
    assert "codebuddy_task" not in names


def test_the_context_block_is_the_owners_and_carries_the_waiting_list():
    """The antecedent for «اوکی», which is the brief's conversational rule.

    Without the pending list in the context, a vague approval has nothing to be
    unambiguous *about* — and the rule the brief states is precisely that it is
    approval only when the context is unambiguous.
    """
    waiting = _submit(task="deploy it", operation="deploy")
    text = admin_tools.build_context(principal=rbac.resolve(OWNER), chat_id=CHAT)
    assert waiting.detail in text
    assert "confirm_agent_task" in text


def test_the_context_block_is_absent_for_an_administrator():
    _submit(task="deploy it", operation="deploy")
    text = admin_tools.build_context(principal=rbac.resolve(ADMIN), chat_id=CHAT)
    assert "coding agent" not in text


def test_the_context_block_does_not_contain_a_task_body():
    _submit(task="a very private instruction")
    text = admin_tools.build_context(principal=rbac.resolve(OWNER), chat_id=CHAT)
    assert "a very private instruction" not in text


def test_the_agent_status_tool_answers_with_the_three_lists():
    _submit(task="add a test")
    answer = admin_tools.agent_status(chat_id=CHAT)
    assert answer["enabled"]
    assert answer["allowed_repositories"] == ["guardbot", "vpn-bot"]
    assert len(answer["active"]) == 1
    assert answer["who_may_confirm"] == "the owner only"


def test_the_agent_status_tool_does_not_leak_a_task_body():
    _submit(task="a very private instruction")
    answer = admin_tools.agent_status(chat_id=CHAT)
    assert "a very private instruction" not in str(answer)


# ── Pruning ───────────────────────────────────────────────────────────────
def test_a_finished_task_older_than_the_window_is_pruned(monkeypatch):
    monkeypatch.setattr(config, "AGENT_RETENTION_SECONDS", 1)
    task = _submit()
    db.agent_task_update(task.detail, status="succeeded", finished_at=1)
    assert agent_service.prune() >= 1
    assert db.agent_task_get(task.detail) is None
    assert _requests() == []


def test_an_active_task_is_never_pruned(monkeypatch):
    monkeypatch.setattr(config, "AGENT_RETENTION_SECONDS", 1)
    task = _submit()
    db.agent_task_update(task.detail, started_at=1)
    agent_service.prune()
    assert db.agent_task_get(task.detail) is not None
