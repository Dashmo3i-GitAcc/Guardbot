"""Four AI workloads, four budgets, and no way for one to reach another.

The brief is explicit that the acquisition classifier, the conversational
assistant, the moderation layer and the speech pipeline must not share
histories, quotas, rate limits, circuit breakers, credentials or failure states.
That is not something a reader can verify by skimming, so it is asserted here as
a set of structural properties: separate module state, separate counter tables,
separate breakers, and no cross-module reads.

Where a property is about the *source*, it is asserted against the source. That
is unusual for a test suite and deliberate: "module A never touches module B's
counters" is a claim about the code, and the cheapest way to keep it true is to
fail when someone writes the import.
"""
import asyncio
import inspect

import pytest

from app import (
    ai_intent,
    ai_moderation,
    chat,
    config,
    db,
    mod_policy,
    transcribe,
)

WORKLOADS = {
    "intent": ai_intent,
    "chat": chat,
    "moderation": ai_moderation,
    "transcribe": transcribe,
}

# The state every workload must own a copy of.
SEPARATE_STATE = (
    "_recent_calls",
    "_consecutive_failures",
    "_circuit_open_until",
    "_client",
    "_client_key",
)


@pytest.fixture(autouse=True)
def iso_env(monkeypatch):
    for module in WORKLOADS.values():
        module.reset_state()
    db.init()
    yield
    for module in WORKLOADS.values():
        module.reset_state()


# ── Separate state ────────────────────────────────────────────────────────
def test_every_workload_owns_its_own_rate_window():
    windows = {name: id(getattr(module, "_recent_calls"))
               for name, module in WORKLOADS.items()}
    assert len(set(windows.values())) == len(WORKLOADS), windows


def test_every_workload_owns_its_own_circuit_breaker():
    """Asserted by behaviour, not by identity.

    Small ints and None are interned in CPython, so `id(0)` is the same object
    in every module — an identity check would pass whether the state was shared
    or not, which is worse than no test. Moving one counter and reading the rest
    is the property that actually matters.
    """
    for name, module in WORKLOADS.items():
        module._consecutive_failures = 0
    chat._consecutive_failures = 3

    assert chat._consecutive_failures == 3
    for name, module in WORKLOADS.items():
        if name != "chat":
            assert module._consecutive_failures == 0, name


def test_every_workload_owns_its_own_breaker_deadline():
    for module in WORKLOADS.values():
        module._circuit_open_until = 0.0
    transcribe._circuit_open_until = 12345.0

    assert transcribe._circuit_open_until == 12345.0
    for name, module in WORKLOADS.items():
        if name != "transcribe":
            assert module._circuit_open_until == 0.0, name


def test_every_workload_owns_its_own_client():
    for module in WORKLOADS.values():
        module._client = None
        module._client_key = ""
    chat._client = object()
    chat._client_key = "chat-key"

    assert chat._client is not None
    for name, module in WORKLOADS.items():
        if name != "chat":
            assert module._client is None, name
            assert module._client_key == "", name


def test_the_intent_and_chat_counters_are_different_objects():
    assert chat._recent_calls is not ai_intent._recent_calls
    assert chat._user_calls is not getattr(ai_intent, "_user_calls", None)


def test_no_workload_imports_another():
    """A shared import is how a shared failure state starts."""
    for name, module in WORKLOADS.items():
        source = inspect.getsource(module)
        for other_name, other in WORKLOADS.items():
            if other_name == name:
                continue
            assert f"from . import {other_name}" not in source, (name, other_name)
            assert f"import {other_name}" not in source, (name, other_name)


def _imported_names(module) -> set[str]:
    """The modules a source file actually imports.

    Parsed from the AST rather than grepped, because a comment mentioning
    `chat` is not an import and a test that cannot tell the difference fails on
    prose. This is the precise version of "does A depend on B".
    """
    import ast

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
            if node.module:
                names.add(node.module.split(".")[-1])
    return names


def test_no_workload_imports_another_module_of_the_set():
    for name, module in WORKLOADS.items():
        imported = _imported_names(module)
        for other in WORKLOADS:
            if other == name:
                continue
            assert other not in imported, (name, other, imported)


def _db_attributes(module) -> set[str]:
    """Every `db.<name>` the module actually evaluates.

    Read from the AST so that a docstring explaining the separation does not
    count as violating it. Only attribute access on the name `db` is collected,
    which is exactly the surface that can move a counter.
    """
    import ast

    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "db"
        ):
            found.add(node.attr)
    return found


COUNTER_PREFIXES = ("ai_", "chat_", "mod_", "transcript_")


def _counters_touched(module) -> set[str]:
    return {
        name for name in _db_attributes(module)
        if name.startswith(COUNTER_PREFIXES)
    }


def test_the_conversational_module_reads_only_its_own_counters():
    touched = _counters_touched(chat)
    assert touched, "the module must touch its own counters"
    assert all(name.startswith("chat_") for name in touched), touched


def test_the_classifier_reads_only_its_own_counters():
    touched = _counters_touched(ai_intent)
    assert touched, "the module must touch its own counters"
    assert all(name.startswith("ai_") for name in touched), touched


def test_the_moderation_module_reads_only_its_own_counters():
    touched = _counters_touched(ai_moderation)
    assert touched, "the module must touch its own counters"
    assert all(name.startswith("mod_") for name in touched), touched


def test_the_transcription_module_reads_only_its_own_counters():
    touched = _counters_touched(transcribe)
    assert touched, "the module must touch its own counters"
    assert all(name.startswith("transcript_") for name in touched), touched


# ── Separate counter tables ───────────────────────────────────────────────
def test_the_four_counter_tables_are_distinct():
    assert ai_intent.stats is not chat.stats
    assert chat.stats is not ai_moderation.stats
    assert ai_moderation.stats is not transcribe.stats


def test_recording_in_one_table_does_not_move_another():
    before = (db.ai_usage(), db.chat_usage(), db.mod_usage(), db.transcript_usage())

    db.record_mod_attempt("flagged")

    after = (db.ai_usage(), db.chat_usage(), db.mod_usage(), db.transcript_usage())
    assert after[0] == before[0], "the classifier's counters moved"
    assert after[1] == before[1], "the chat counters moved"
    assert after[3] == before[3], "the transcription counters moved"
    assert after[2]["calls"] == before[2]["calls"] + 1


def test_a_chat_call_does_not_spend_the_moderation_allowance():
    before = db.mod_usage()["calls"]
    db.record_chat_attempt("replies")
    assert db.mod_usage()["calls"] == before


def test_a_transcription_does_not_spend_the_chat_allowance():
    before = db.chat_usage()["calls"]
    db.record_transcript_attempt("transcripts")
    assert db.chat_usage()["calls"] == before


# ── Separate credentials ──────────────────────────────────────────────────
def test_every_workload_reads_its_own_key_setting(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k-intent")
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k-chat")
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "k-mod")
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "k-tr")

    # The classifier reads GEMINI_API_KEY inline rather than through an
    # accessor, so it is asserted structurally; the other three each have their
    # own accessor and their own setting.
    assert "GEMINI_API_KEY" in inspect.getsource(ai_intent)
    assert chat.api_key() == "k-chat"
    assert ai_moderation.api_key() == "k-mod"
    assert transcribe.api_key() == "k-tr"


def test_a_workload_without_its_own_key_does_not_borrow_one_by_default(monkeypatch):
    """The shared-key fallback is opt-in per workload, and off everywhere."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k-intent")
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "")
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_CHAT_ALLOW_SHARED_KEY", False)
    monkeypatch.setattr(config, "GEMINI_MOD_ALLOW_SHARED_KEY", False)
    monkeypatch.setattr(config, "TRANSCRIBE_ALLOW_SHARED_KEY", False)

    assert chat.api_key() == ""
    assert ai_moderation.api_key() == ""
    assert transcribe.api_key() == ""


def test_the_shared_key_fallback_is_reported_per_workload(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k-intent")
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_CHAT_ALLOW_SHARED_KEY", True)
    monkeypatch.setattr(config, "GEMINI_MOD_ALLOW_SHARED_KEY", False)

    assert chat.shares_google_project() is True
    assert ai_moderation.shares_google_project() is False


def test_no_status_ever_contains_a_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k-intent")
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k-chat")
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "k-mod")
    monkeypatch.setattr(config, "TRANSCRIBE_API_KEY", "k-tr")

    blob = " ".join(
        repr(module.status()) for module in WORKLOADS.values()
    )
    for secret in ("k-intent", "k-chat", "k-mod", "k-tr"):
        assert secret not in blob


# ── Separate failure states ───────────────────────────────────────────────
def test_opening_one_breaker_leaves_the_others_closed(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_MOD_API_KEY", "k")
    monkeypatch.setattr(config, "MODERATION_TEXT_ENABLED", True)
    monkeypatch.setattr(config, "MODERATION_MEDIA_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_MOD_CIRCUIT_FAILURES", 1)
    monkeypatch.setattr(config, "GEMINI_MOD_CIRCUIT_SECONDS", 300.0)
    monkeypatch.setattr(config, "GEMINI_MOD_MAX_RETRIES", 0)

    async def _boom(parts):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_moderation, "_request", _boom)
    asyncio.run(ai_moderation.assess_text("a message long enough to be sent"))

    assert ai_moderation._circuit_open_until > 0
    assert chat._circuit_open_until == 0.0
    assert ai_intent._circuit_open_until == 0.0
    assert transcribe._circuit_open_until == 0.0


def test_a_chat_failure_does_not_touch_the_classifier(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_CHAT_CIRCUIT_FAILURES", 1)
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_RETRIES", 0)

    async def _boom(contents):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat, "_request", _boom)
    asyncio.run(chat.reply(-100, 1, "سلام"))

    assert chat._consecutive_failures >= 1
    assert ai_intent._consecutive_failures == 0
    assert ai_moderation._consecutive_failures == 0
    assert transcribe._consecutive_failures == 0


def test_resetting_one_workload_leaves_the_others_alone(monkeypatch):
    ai_intent._recent_calls.append(1.0)
    chat._recent_calls.append(1.0)
    ai_moderation._recent_calls.append(1.0)
    transcribe._recent_calls.append(1.0)

    chat.reset_state()

    assert chat._recent_calls == []
    assert ai_intent._recent_calls == [1.0]
    assert ai_moderation._recent_calls == [1.0]
    assert transcribe._recent_calls == [1.0]


# ── Separate daily caps ───────────────────────────────────────────────────
def test_every_workload_has_its_own_daily_limit_setting():
    limits = {
        "intent": config.GEMINI_DAILY_LIMIT,
        "chat": config.GEMINI_CHAT_DAILY_LIMIT,
        "moderation": config.GEMINI_MOD_DAILY_LIMIT,
        "transcribe": config.TRANSCRIBE_DAILY_LIMIT,
    }
    assert len(set(limits.values())) == len(limits), (
        "two workloads sharing a default cap is how one starves another"
    )


def test_every_workload_has_its_own_circuit_settings():
    for prefix in ("GEMINI_", "GEMINI_CHAT_", "GEMINI_MOD_", "TRANSCRIBE_"):
        assert hasattr(config, f"{prefix}CIRCUIT_FAILURES"), prefix
        assert hasattr(config, f"{prefix}CIRCUIT_SECONDS"), prefix


def test_every_workload_has_its_own_model_setting():
    models = {
        "intent": config.GEMINI_MODEL,
        "chat": config.GEMINI_CHAT_MODEL,
        "moderation": config.GEMINI_MOD_MODEL,
        "transcribe": config.TRANSCRIBE_MODEL,
    }
    assert all(models.values())
    for name, model in models.items():
        assert isinstance(model, str) and model, name


# ── The routing that keeps them apart ─────────────────────────────────────
def test_moderation_cannot_trigger_acquisition():
    """The policy engine has no route to the acquisition path at all."""
    imported = _imported_names(mod_policy)
    assert "vpnbot" not in imported
    assert "ai_intent" not in imported
    assert "classifier" not in imported


def test_acquisition_cannot_trigger_a_conversation():
    """The classifier has no import of the assistant, and no route to it."""
    imported = _imported_names(ai_intent)
    assert "chat" not in imported
    assert "rbac" not in imported


def test_conversation_cannot_grant_administrative_privileges():
    """The assistant has no reachable reference to the authorization engine."""
    imported = _imported_names(chat)
    assert "rbac" not in imported
    source = inspect.getsource(chat)
    assert "admin_set" not in source
    assert "promote_chat_member" not in source


def test_the_moderation_workload_cannot_execute_a_telegram_action():
    imported = _imported_names(ai_moderation)
    assert not {name for name in imported if name.startswith("telegram")}
    source = inspect.getsource(ai_moderation)
    for forbidden in (
        "delete_message", "restrict_chat_member", "ban_chat_member",
        "promote_chat_member", "send_message", "ctx.bot",
    ):
        assert forbidden not in source, forbidden


def test_the_transcription_workload_cannot_execute_a_telegram_action():
    imported = _imported_names(transcribe)
    assert not {name for name in imported if name.startswith("telegram")}
    source = inspect.getsource(transcribe)
    for forbidden in ("ctx.bot", "send_message", "delete_message"):
        assert forbidden not in source, forbidden


def test_ordinary_group_voice_is_not_routed_into_any_workload():
    """Nothing transcribes a group voice note on arrival.

    Asserted structurally: `transcribe` is called from exactly the two places
    that explicitly ask for it, and neither is a general group handler.
    """
    from app import main

    source = inspect.getsource(main)
    callers = [
        line for line in source.splitlines()
        if "transcribe.transcribe" in line and not line.strip().startswith("#")
    ]
    assert len(callers) == 2, callers
    # One is the conversational path, one is the transcription-only command.
    assert any("transcribe_ref" in line for line in callers)


def test_the_assistant_does_not_run_on_ordinary_group_text():
    """The boundary, asserted where it lives.

    Two gates, and both are required. `_nexus_directed` decides whether the
    message is aimed at Nexus; `nexus.accepts` decides whether the sender may
    reach it at all. A group message that is neither addressed nor an
    instruction from an administrator is recorded as context and answered with
    silence — the "watch without replying" requirement.
    """
    from app import main

    source = inspect.getsource(main.on_group_chat)
    assert "_nexus_directed" in source
    assert "nexus.accepts" in source
    # The gate must come before the answer, and the answer before any model call.
    assert source.index("nexus.accepts") < source.index("_answer_conversationally")
    assert source.index("_nexus_directed") < source.index("_answer_conversationally")


def test_the_assistant_is_gated_on_the_sender_before_anything_is_spent():
    """Identity and role are resolved before the relevance gate and the model."""
    from app import main

    source = inspect.getsource(main.on_group_chat)
    resolve = source.index("rbac.resolve")
    accepts = source.index("nexus.accepts")
    actionable = source.index("nexus.looks_actionable")
    answer = source.index("_answer_conversationally")
    assert resolve < accepts < actionable < answer


def test_the_four_workloads_have_four_separate_switches():
    switches = (
        config.GEMINI_ENABLED,
        config.GEMINI_CHAT_ENABLED,
        config.GEMINI_MOD_ENABLED,
        config.TRANSCRIBE_ENABLED,
    )
    assert len(switches) == 4
    for name in ("GEMINI_ENABLED", "GEMINI_CHAT_ENABLED", "GEMINI_MOD_ENABLED",
                 "TRANSCRIBE_ENABLED"):
        assert isinstance(getattr(config, name), bool)
