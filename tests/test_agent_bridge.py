"""The bridge's decisions, tested as decisions.

Everything in ``app/agent_bridge.py`` is pure: no Telegram, no subprocess, no
clock it does not take as an argument. That is deliberate, because the
properties worth testing here are the ones a live experiment cannot show — "a
member cannot start a task", "the model cannot mark its own operation approved",
"«اوکی» with two things pending is a question rather than an approval". Those
are questions about a *function*, and a function can be asked directly.

Four groups, and each corresponds to a sentence in the brief:

* **The allowlist.** A request names a name; the path comes from a table. There
  is no string a model can produce that becomes a directory.
* **The operation vocabulary and the danger classifier.** A closed set, and the
  asymmetry is the point: text can add danger and can never remove it.
* **Confirmation.** Owner-only, nothing-pending is not an approval, and a bare
  confirmation needs exactly one candidate.
* **Transport.** Ordered, lossless, bounded, and redacted on the way out.

Nothing here imports ``app/awareness.py`` or ``app/gemini_pool.py``, and the
last test in the file asserts that by reading the import graph rather than by
trusting the docstring.
"""
import os
import sys

import pytest

from app import agent_bridge, config, db

CHAT = -1001234567890


@pytest.fixture(autouse=True)
def agent_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config,
        "AGENT_REPOSITORIES",
        {"guardbot": "/root/guardbot", "vpn-bot": "/opt/vpn-bot"},
    )
    monkeypatch.setattr(config, "AGENT_MAX_ACTIVE", 2)
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 1)
    monkeypatch.setattr(config, "AGENT_CHUNK_CHARS", 3500)
    monkeypatch.setattr(config, "AGENT_DOCUMENT_CHARS", 3500)
    monkeypatch.setenv("AGENT_SPOOL_DIR", str(tmp_path / "spool"))
    db.init()
    yield


# ── The allowlist ─────────────────────────────────────────────────────────
def test_a_name_resolves_to_the_directory_the_table_names():
    assert agent_bridge.repository_path("guardbot") == "/root/guardbot"
    assert agent_bridge.repository_path("vpn-bot") == "/opt/vpn-bot"


def test_the_name_is_matched_without_regard_to_case_or_spacing():
    assert agent_bridge.parse_repository("  GuardBot ") == "guardbot"


def test_a_path_that_is_exactly_an_allowlisted_root_is_accepted():
    """The model has seen the path in the trusted context and may echo it back.

    Refusing it would produce a confusing refusal for a request that named a
    perfectly allowed repository, so the path is accepted *and converted back to
    the name* — the path never survives into the envelope as a path.
    """
    assert agent_bridge.parse_repository("/root/guardbot") == "guardbot"
    assert agent_bridge.parse_repository("/root/guardbot/") == "guardbot"


def test_a_path_that_is_not_an_allowlisted_root_is_refused():
    for bad in ("/etc", "/root", "/root/guardbot/app", "/opt", "../etc", "~/"):
        assert agent_bridge.parse_repository(bad) == "", bad


def test_an_unknown_name_is_refused_and_the_refusal_names_the_allowed_ones():
    with pytest.raises(agent_bridge.Rejected) as caught:
        agent_bridge.build_request(
            actor_id=1, chat_id=CHAT, repository="nope", task="do a thing"
        )
    assert caught.value.reason == "unknown_repository"
    assert "guardbot" in caught.value.message
    assert "vpn-bot" in caught.value.message


def test_a_request_cannot_produce_a_path_that_is_not_on_the_allowlist():
    """The property, stated once: there is no input that yields a stray path.

    The envelope's ``repo_path`` is only ever assigned from
    ``repository_path(name)``, and ``name`` only ever comes from
    ``parse_repository``. So the test is over the *output* rather than over a
    list of suspicious inputs — a fuzz over the function's own contract.
    """
    allowed = set(agent_bridge.repositories().values())
    for attempt in ("/etc", "/", "guardbot/../../etc", "/root/guardbot/../..", ""):
        try:
            envelope = agent_bridge.build_request(
                actor_id=1, chat_id=CHAT, repository=attempt, task="x"
            )
        except agent_bridge.Rejected:
            continue
        assert envelope.repo_path in allowed


# ── The operation vocabulary ──────────────────────────────────────────────
def test_an_operation_outside_the_vocabulary_is_refused():
    with pytest.raises(agent_bridge.Rejected) as caught:
        agent_bridge.build_request(
            actor_id=1,
            chat_id=CHAT,
            repository="guardbot",
            task="do a thing",
            requested_operation="sudo",
        )
    assert caught.value.reason == "unknown_operation"


def test_a_missing_operation_is_read_from_the_task_and_not_refused():
    envelope = agent_bridge.build_request(
        actor_id=1, chat_id=CHAT, repository="guardbot", task="fix the bug"
    )
    assert envelope.operation == "edit"


def test_the_common_words_a_model_uses_are_mapped_onto_the_vocabulary():
    for written, expected in (
        ("fix", "edit"),
        ("refactor", "edit"),
        ("review", "analyse"),
        ("run-tests", "test"),
        ("git-push", "push"),
        ("release", "deploy"),
    ):
        assert agent_bridge.parse_operation(written, "x") == expected


def test_an_empty_task_is_refused():
    with pytest.raises(agent_bridge.Rejected) as caught:
        agent_bridge.build_request(
            actor_id=1, chat_id=CHAT, repository="guardbot", task="   "
        )
    assert caught.value.reason == "empty_task"


def test_a_task_longer_than_the_bound_is_truncated_rather_than_refused():
    envelope = agent_bridge.build_request(
        actor_id=1, chat_id=CHAT, repository="guardbot", task="x" * 9000
    )
    assert len(envelope.task) == db.AGENT_TASK_MAX_CHARS


# ── Danger ────────────────────────────────────────────────────────────────
def test_the_dangerous_operations_are_dangerous():
    for operation in ("deploy", "migrate", "delete", "reset", "credentials"):
        assert agent_bridge.danger_for(operation, "x"), operation


def test_the_ordinary_operations_are_not_dangerous_on_their_own():
    for operation in ("analyse", "test", "edit", "commit", "push"):
        assert agent_bridge.danger_for(operation, "rename a variable") == ""


def test_the_task_text_can_add_danger_to_an_ordinary_operation():
    """The asymmetry, which is the whole design of the classifier.

    A false positive costs one confirmation; a false negative costs an
    unconfirmed destructive action. So the text scan exists to catch the case
    where the model classified a request as an edit and the request itself says
    to deploy.
    """
    assert agent_bridge.danger_for("edit", "and then deploy it to production")
    assert agent_bridge.danger_for("edit", "force push the branch")
    assert agent_bridge.danger_for("analyse", "drop table users")


def test_the_task_text_can_never_remove_danger():
    assert agent_bridge.danger_for("deploy", "this is completely safe") != ""


def test_a_dangerous_request_is_recorded_as_waiting_and_not_as_queued():
    envelope = agent_bridge.build_request(
        actor_id=1,
        chat_id=CHAT,
        repository="guardbot",
        task="deploy the new build",
        requested_operation="deploy",
    )
    assert envelope.is_dangerous
    assert envelope.status == "waiting_for_owner"


def test_an_ordinary_request_is_queued():
    envelope = agent_bridge.build_request(
        actor_id=1, chat_id=CHAT, repository="guardbot", task="add a test"
    )
    assert not envelope.is_dangerous
    assert envelope.status == "queued"


# ── Confirmation ──────────────────────────────────────────────────────────
def _waiting(*ids):
    return [{"request_id": i, "status": "waiting_for_owner"} for i in ids]


def test_a_non_owner_cannot_confirm_even_with_one_task_waiting():
    decision = agent_bridge.resolve_confirmation(
        actor_id=42, is_owner=False, waiting=_waiting("a")
    )
    assert decision.answer is agent_bridge.Confirm.NOT_OWNER
    assert not decision


def test_a_bare_confirmation_with_nothing_pending_is_refused():
    """The brief's rule, and the failure it prevents.

    «اوکی» with nothing pending is not an approval of anything. Treating it as
    one would be exactly the "blindly guess" behaviour the brief forbids.
    """
    decision = agent_bridge.resolve_confirmation(
        actor_id=1, is_owner=True, waiting=[]
    )
    assert decision.answer is agent_bridge.Confirm.NOTHING_PENDING


def test_a_bare_confirmation_releases_exactly_one_waiting_task():
    decision = agent_bridge.resolve_confirmation(
        actor_id=1, is_owner=True, waiting=_waiting("a")
    )
    assert decision.answer is agent_bridge.Confirm.OK
    assert decision.request_id == "a"
    assert bool(decision)


def test_a_bare_confirmation_with_two_waiting_tasks_is_a_question():
    decision = agent_bridge.resolve_confirmation(
        actor_id=1, is_owner=True, waiting=_waiting("a", "b")
    )
    assert decision.answer is agent_bridge.Confirm.AMBIGUOUS
    assert decision.candidates == ("a", "b")


def test_a_named_confirmation_releases_the_named_task():
    decision = agent_bridge.resolve_confirmation(
        actor_id=1, is_owner=True, named_request_id="b", waiting=_waiting("a", "b")
    )
    assert decision.answer is agent_bridge.Confirm.OK
    assert decision.request_id == "b"


def test_a_named_confirmation_for_a_task_that_is_not_waiting_is_refused():
    decision = agent_bridge.resolve_confirmation(
        actor_id=1, is_owner=True, named_request_id="zzz", waiting=_waiting("a")
    )
    assert decision.answer is agent_bridge.Confirm.NOT_WAITING
    assert decision.candidates == ("a",)


# ── The lifecycle ─────────────────────────────────────────────────────────
def test_a_terminal_state_is_terminal():
    for status in db.AGENT_TERMINAL_STATUSES:
        for target in db.AGENT_STATUSES:
            if target == status:
                continue
            assert not agent_bridge.transition_allowed(status, target)


def test_a_task_can_be_cancelled_from_any_active_state():
    for status in db.AGENT_ACTIVE_STATUSES:
        assert agent_bridge.transition_allowed(status, "cancelled")


def test_an_unapproved_dangerous_task_cannot_go_straight_to_running():
    """``waiting_for_owner`` → ``running`` is deliberately not legal.

    The only way out of "recorded and not approved" is through ``queued``, which
    is what ``confirm`` produces. Letting it reach ``running`` directly would
    make the approval step optional.
    """
    assert not agent_bridge.transition_allowed("waiting_for_owner", "running")


def test_a_queued_task_can_start_and_cannot_succeed_without_running():
    assert agent_bridge.transition_allowed("queued", "running")
    assert not agent_bridge.transition_allowed("queued", "succeeded")


# ── Identity and idempotency ──────────────────────────────────────────────
def test_the_same_request_in_the_same_minute_has_the_same_id():
    a = agent_bridge.new_request_id(1, "guardbot", "fix it", now=1_000_000)
    b = agent_bridge.new_request_id(1, "guardbot", "fix it", now=1_000_005)
    assert a == b


def test_a_different_actor_asking_the_same_thing_is_a_different_task():
    a = agent_bridge.new_request_id(1, "guardbot", "fix it", now=1_000_000)
    b = agent_bridge.new_request_id(2, "guardbot", "fix it", now=1_000_000)
    assert a != b


def test_a_different_repository_is_a_different_task():
    a = agent_bridge.new_request_id(1, "guardbot", "fix it", now=1_000_000)
    b = agent_bridge.new_request_id(1, "vpn-bot", "fix it", now=1_000_000)
    assert a != b


def test_the_id_is_a_plain_token_that_can_be_a_filename():
    request_id = agent_bridge.new_request_id(1, "guardbot", "fix it")
    assert request_id.startswith("agent-")
    assert all(ch.isalnum() or ch in "-_" for ch in request_id)


# ── Scope ─────────────────────────────────────────────────────────────────
def _envelope(task="fix it", repository="guardbot"):
    return agent_bridge.build_request(
        actor_id=1, chat_id=CHAT, repository=repository, task=task
    )


def test_the_same_request_twice_is_a_duplicate():
    envelope = _envelope()
    assert agent_bridge.scope_check(envelope, active=[envelope.as_row()]) == "duplicate"


def test_a_second_task_on_one_repository_is_refused():
    envelope = _envelope("second thing")
    other = _envelope("first thing").as_row()
    assert agent_bridge.scope_check(envelope, active=[other]) == "repository_busy"


def test_the_global_ceiling_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_ACTIVE", 2)
    monkeypatch.setattr(config, "AGENT_MAX_PER_REPOSITORY", 5)
    active = [
        _envelope("a").as_row(),
        _envelope("b", "vpn-bot").as_row(),
    ]
    assert agent_bridge.scope_check(_envelope("c"), active=active) == "busy"


def test_nothing_active_means_no_objection():
    assert agent_bridge.scope_check(_envelope(), active=[]) == ""


# ── The prompt ────────────────────────────────────────────────────────────
def test_the_prompt_names_the_directory_and_forbids_leaving_it():
    prompt = agent_bridge.build_prompt(_envelope())
    assert "/root/guardbot" in prompt
    assert "Work only inside" in prompt


def test_the_prompt_carries_the_task_verbatim():
    prompt = agent_bridge.build_prompt(_envelope("rename the thing in foo.py"))
    assert "rename the thing in foo.py" in prompt


def test_the_prompt_forbids_printing_a_credential():
    prompt = agent_bridge.build_prompt(_envelope())
    assert "Never print a credential" in prompt


def test_the_prompt_states_the_marker_the_runner_looks_for():
    prompt = agent_bridge.build_prompt(_envelope())
    assert agent_bridge.QUESTION_MARKER in prompt


# ── Reading the answer ────────────────────────────────────────────────────
# The credential-shaped strings below are **test vectors, not credentials**.
# They are what ``redact`` has to match, so they have to look like the real
# thing; the bot token is the example Telegram prints in its own API
# documentation and the rest are obviously-fake filler. No live key appears in
# this file, and none may be added — see ``AgentMD.md`` §39.12.
def test_a_question_is_found_by_its_marker():
    text = "I read the files.\nQUESTION: should I push the branch?\n"
    assert agent_bridge.question_in(text) == "should I push the branch?"


def test_a_finding_that_is_not_marked_is_not_read_as_a_question():
    assert agent_bridge.question_in("Should I push the branch?") == ""


def test_a_bot_token_in_the_answer_is_redacted():
    out = agent_bridge.redact("token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    assert agent_bridge.REDACTED in out


def test_a_google_key_in_the_answer_is_redacted():
    out = agent_bridge.redact("key=AIzaSyD-1234567890abcdefghijklmnopqrstu")
    assert "AIzaSyD" not in out


def test_an_api_key_assignment_is_redacted():
    out = agent_bridge.redact("OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuvwx")
    assert "abcdefghijklmnopqrstuvwx" not in out


def test_a_private_key_block_is_redacted():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in agent_bridge.redact(text)


def test_ordinary_prose_is_left_alone():
    text = "I changed the parser and the tests pass."
    assert agent_bridge.redact(text) == text


def test_the_summary_keeps_the_end_of_a_long_answer():
    body = "x" * 500 + "THE CONCLUSION"
    assert "THE CONCLUSION" in agent_bridge.summarise(body, limit=100)


# ── Transport ─────────────────────────────────────────────────────────────
def test_a_short_answer_is_one_piece():
    assert agent_bridge.chunk_text("short") == ["short"]


def test_chunking_is_lossless():
    body = "\n".join(f"line {i} of the transcript" for i in range(500))
    pieces = agent_bridge.chunk_text(body, limit=300)
    assert "".join(pieces).replace("\n", "") == body.replace("\n", "")


def test_chunking_preserves_order():
    body = "\n".join(f"line {i}" for i in range(200))
    pieces = agent_bridge.chunk_text(body, limit=200)
    joined = "\n".join(pieces)
    assert joined.index("line 0") < joined.index("line 199")


def test_chunking_splits_on_a_line_boundary_when_it_can():
    body = "a" * 100 + "\n" + "b" * 100
    pieces = agent_bridge.chunk_text(body, limit=150)
    assert len(pieces) >= 2
    assert not pieces[0].endswith("b")


def test_chunking_never_produces_a_piece_over_the_limit():
    body = "word " * 3000
    for piece in agent_bridge.chunk_text(body, limit=500):
        assert len(piece) <= 500


def test_every_character_of_a_long_answer_survives():
    body = "".join(chr(0x600 + (i % 40)) for i in range(20000))
    pieces = agent_bridge.chunk_text(body, limit=1000)
    assert sum(len(p) for p in pieces) >= len(body) - len(pieces)


def test_a_long_answer_prefers_a_document():
    assert agent_bridge.needs_document("x" * 5000, limit=3500)


def test_a_short_answer_does_not():
    assert not agent_bridge.needs_document("short", limit=3500)


def test_reply_mode_text_produces_chunks_and_no_document():
    plan = agent_bridge.reply_plan("x" * 9000, "text")
    assert plan["chunks"] and not plan["document"]


def test_reply_mode_document_produces_a_document():
    plan = agent_bridge.reply_plan("x" * 9000, "document")
    assert plan["document"] and not plan["chunks"]


def test_reply_mode_both_sends_a_document_for_a_long_answer():
    plan = agent_bridge.reply_plan("x" * 9000, "both")
    assert plan["document"]


def test_there_is_no_mode_that_drops_the_answer():
    """The brief's rule, asserted as a property over every mode and length."""
    for mode in ("text", "document", "both", "", "nonsense"):
        for length in (0, 10, 4000, 40000):
            body = "y" * length
            plan = agent_bridge.reply_plan(body, mode)
            if not body:
                continue
            assert plan["chunks"] or plan["document"], (mode, length)


def test_an_empty_answer_is_reported_as_empty_rather_than_planned():
    assert agent_bridge.reply_plan("", "text")["mode"] == "empty"


# ── Isolation from the awareness allowance ────────────────────────────────
def _imports(path: str) -> set[str]:
    """The modules a file actually imports, from its real import statements.

    A substring search over the source would be answered by the docstrings —
    and these modules *talk* about the awareness layer and the pool precisely
    because they are explaining why they do not use them. So the check is over
    parsed import lines, which is the property that matters.
    """
    found: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("import "):
                for name in stripped[len("import "):].split(","):
                    found.add(name.strip().split(" as ")[0].split(".")[0])
            elif stripped.startswith("from "):
                rest = stripped[len("from "):]
                module = rest.split(" import ")[0].strip()
                if module == "." or module.startswith("."):
                    # ``from . import a, b`` — record the names as ``app``-local.
                    names = rest.split(" import ", 1)[-1]
                    for name in names.split(","):
                        found.add(name.strip().split(" as ")[0])
                else:
                    found.add(module.split(".")[0])
    return found


def test_a_coding_task_spends_its_own_account_and_not_the_assistants():
    assert agent_bridge.allowance_account() == "agent"


def test_no_agent_module_imports_the_awareness_layer_or_the_pool():
    """The property behind ``allowance_account``, checked rather than asserted.

    A coding task must not consume the assistant's daily allowance. The
    mechanism is that the bridge never reaches the pool at all — the agent is a
    host process authenticated by the owner's own credential — and this test is
    what keeps that true if somebody later decides the bridge should "just call
    Gemini".
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in (
        "agent_bridge.py",
        "agent_service.py",
        "agent_poller.py",
        "agent_spool.py",
    ):
        imported = _imports(os.path.join(root, "app", name))
        assert "awareness" not in imported, name
        assert "gemini_pool" not in imported, name


def test_the_runner_does_not_import_the_database():
    """The runner must not be a second SQLite writer.

    ``app/agent_spool.py`` is the only project module it imports, and that
    module is written to the standard library. This asserts both halves: the
    runner does not import ``db``, and the spool imports nothing but the
    standard library.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner = open(os.path.join(root, "tools", "agent_runner.py"), encoding="utf-8").read()
    assert "from app import db" not in runner
    assert "import db" not in runner

    assert _imports(os.path.join(root, "app", "agent_spool.py")) <= {
        "json",
        "os",
        "time",
        "__future__",
    }


def test_the_runner_does_not_take_the_executable_from_the_request():
    """Which binary runs is the host's decision, not the container's.

    A container that could name an executable could name one that is not a
    coding agent, and that is a trust edge with nothing on the other side of it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner = open(os.path.join(root, "tools", "agent_runner.py"), encoding="utf-8").read()
    assert 'payload.get("cli")' not in runner
    assert 'payload.get("cli_args")' not in runner
    assert 'os.getenv("AGENT_CLI"' in runner


# ── Status lines ──────────────────────────────────────────────────────────
def test_the_status_lines_are_short_and_carry_no_task_bodies():
    db.agent_task_create(
        "agent-abc",
        actor_id=1,
        chat_id=CHAT,
        repository="guardbot",
        repo_path="/root/guardbot",
        task="a very secret thing nobody should see",
        operation="edit",
    )
    lines = agent_bridge.status_lines(limit=5)
    joined = "\n".join(lines)
    assert "agent-abc" in joined
    assert "a very secret thing" not in joined


def test_a_status_label_exists_for_every_state():
    for status in db.AGENT_STATUSES:
        assert agent_bridge.status_label(status) != status or status == ""
