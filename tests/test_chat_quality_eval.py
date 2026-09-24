"""The answer-quality benchmark: the corpus, the scorer, and the harness.

Everything here runs offline with **zero** model calls. The corpus and the
scorer are pure functions, and the two tests that drive the real addressed path
replace the reply seam, so the suite can never spend a request or reach a
provider — the credential gate is asserted rather than assumed.

The point of these tests is not "the tool works". It is that the benchmark
cannot lie in the two ways a benchmark lies: a metric that can never move (a
vacuity), and a run that reports a number it did not measure.
"""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "eval_chat_quality.py"
FIXTURE = ROOT / "tools" / "fixtures" / "chat_quality_transcripts.json"

_CACHE: dict = {}


def _load():
    """Import the tool by path. It is a script, not a package module."""
    if "module" not in _CACHE:
        spec = importlib.util.spec_from_file_location("eval_chat_quality", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CACHE["module"] = module
    return _CACHE["module"]


def _metrics(transcript, arm="a"):
    return _load().score_transcript(transcript)["arms"][arm]["metrics"]


def _mutate(transcript, scenario_id, change, arm="a"):
    """Return a copy of the transcript with one scenario's answers changed."""
    copy = json.loads(json.dumps(transcript))
    for scenario in copy["scenarios"]:
        if scenario["id"] == scenario_id:
            for sample in scenario["arms"][arm]["samples"]:
                sample["text"] = change(sample["text"])
    return copy


# ── The corpus ────────────────────────────────────────────────────────────
def test_the_corpus_is_well_formed():
    module = _load()
    ids = [scenario["id"] for scenario in module.SCENARIOS]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"
    assert len(module.SCENARIOS) >= 12
    for scenario in module.SCENARIOS:
        assert scenario["ask"].strip(), f"{scenario['id']} has no ask"
        assert scenario["expect"], f"{scenario['id']} has no expectation"
        assert scenario["good"].strip(), f"{scenario['id']} has no good answer"
    assert {scenario["category"] for scenario in module.SCENARIOS} == set(
        module.CATEGORIES
    ), "every category must be represented"


def test_every_category_feeds_exactly_one_metric():
    module = _load()
    assert set(module.CATEGORY_METRIC) == set(module.CATEGORIES)
    assert len(set(module.CATEGORY_METRIC.values())) == len(module.CATEGORIES)


def test_the_grounded_set_includes_an_absent_fact():
    """Without it, "always confident" would score full marks."""
    module = _load()
    grounded = [s for s in module.SCENARIOS if s["category"] == "grounded"]
    assert any(s["expect"].get("admits_unknown") for s in grounded)


# ── The scorer, and its non-vacuity ───────────────────────────────────────
def test_the_synthetic_transcript_passes_every_metric():
    module = _load()
    report = module.score_transcript(module.synthetic_transcript(samples=2))
    for arm in ("a", "b"):
        metrics = report["arms"][arm]["metrics"]
        assert metrics["quality_pass_rate"] == 1.0
        assert metrics["answered_rate"] == 1.0
        assert metrics["not_run"] == 0
        assert metrics["room_leaks"] == 0
        assert metrics["admits_unknown_rate"] == 1.0
        for name in module.CATEGORY_METRIC.values():
            assert metrics[name] == 1.0, f"{arm}:{name} is not 1.0"
        assert report["arms"][arm]["failures"] == []


def test_the_committed_fixture_round_trips():
    """The fixture is the corpus's own good answers, not a hand-written copy.

    If this fails, someone edited the corpus (or the fixture) and the two have
    drifted. Regenerate rather than patch the JSON.
    """
    module = _load()
    with FIXTURE.open(encoding="utf-8") as handle:
        committed = json.load(handle)
    assert committed == module.synthetic_transcript(samples=2)


def test_a_dropped_fact_moves_the_grounded_metric():
    module = _load()
    base = module.synthetic_transcript(samples=2)
    mutated = _mutate(base, "G1_room_fact", lambda text: text.replace("فردا", "هفته بعد"))
    before, after = _metrics(base), _metrics(mutated)
    assert after["grounded_answer_rate"] < before["grounded_answer_rate"]
    assert after["quality_pass_rate"] < before["quality_pass_rate"]
    # A control: a category nothing touched must not move.
    assert after["referent_correctness"] == before["referent_correctness"] == 1.0


def test_a_leaked_room_marker_moves_the_self_contained_metric():
    module = _load()
    base = module.synthetic_transcript(samples=2)
    mutated = _mutate(
        base, "S1_general_knowledge", lambda text: text + " ققنوس"
    )
    before, after = _metrics(base), _metrics(mutated)
    assert after["self_contained_no_room"] < before["self_contained_no_room"]
    assert after["room_leaks"] > before["room_leaks"] == 0
    assert after["referent_correctness"] == 1.0


def test_a_missing_question_moves_the_ambiguity_metric():
    module = _load()
    base = module.synthetic_transcript(samples=2)
    mutated = _mutate(base, "A1_two_candidates", lambda text: text.replace("؟", "."))
    before, after = _metrics(base), _metrics(mutated)
    assert after["ambiguity_handling"] < before["ambiguity_handling"]
    assert after["referent_correctness"] == 1.0


def test_a_claimed_action_moves_the_action_safety_metric():
    module = _load()
    base = module.synthetic_transcript(samples=2)
    mutated = _mutate(base, "X1_ban_request", lambda text: text + " انجام دادم.")
    before, after = _metrics(base), _metrics(mutated)
    assert after["action_safety_rate"] < before["action_safety_rate"]
    assert after["referent_correctness"] == 1.0


def test_a_wholly_bad_transcript_moves_every_metric():
    module = _load()
    base = module.synthetic_transcript(samples=2)
    mutated = base
    for scenario_id, change in (
        ("G1_room_fact", lambda text: text.replace("فردا", "هفته بعد")),
        ("S1_general_knowledge", lambda text: text + " ققنوس"),
        ("A1_two_candidates", lambda text: text.replace("؟", ".")),
        ("X1_ban_request", lambda text: text + " انجام دادم."),
    ):
        mutated = _mutate(mutated, scenario_id, change)
    after = _metrics(mutated)
    assert after["quality_pass_rate"] < 1.0
    assert after["grounded_answer_rate"] < 1.0
    assert after["self_contained_no_room"] < 1.0
    assert after["ambiguity_handling"] < 1.0
    assert after["action_safety_rate"] < 1.0


def test_a_skipped_pair_is_not_run_rather_than_failed():
    """The honest treatment of "no answer" is a gap, not a zero."""
    module = _load()
    transcript = json.loads(json.dumps(module.synthetic_transcript(samples=2)))
    for scenario in transcript["scenarios"]:
        if scenario["id"] == "R1_anaphora":
            for sample in scenario["arms"]["a"]["samples"]:
                sample["answered"] = False
                sample["skipped"] = "no_key"
                sample["text"] = ""
    metrics = _metrics(transcript)
    assert metrics["not_run"] == 1
    assert metrics["quality_pass_rate"] == 1.0
    assert metrics["referent_correctness"] == 1.0


def test_the_scorer_makes_no_model_calls(monkeypatch):
    module = _load()
    with FIXTURE.open(encoding="utf-8") as handle:
        transcript = json.load(handle)

    def _explode(*args, **kwargs):
        raise AssertionError("the scorer reached the provider")

    monkeypatch.setattr(module.gemini_pool, "generate", _explode)
    monkeypatch.setattr(module.chat, "_request", _explode)
    report = module.score_transcript(transcript)
    assert report["arms"]["a"]["metrics"]["quality_pass_rate"] == 1.0


# ── The harness ───────────────────────────────────────────────────────────
def _json_tail(output: str) -> dict:
    """The JSON object a ``--json`` run prints, after any NOT RUN line."""
    return json.loads(output[output.index("{"):])


def test_the_credential_gate_reports_not_run(monkeypatch, capsys):
    """With no key the tool must say so, and must not call the model."""
    module = _load()
    called = []

    async def _reply(*args, **kwargs):
        called.append(True)
        raise AssertionError("chat.reply ran without a credential")

    monkeypatch.setattr(module.chat, "api_key", lambda: "")
    monkeypatch.setattr(module.gemini_pool, "has_accounts", lambda workload: False)
    monkeypatch.setattr(module.chat, "reply", _reply)

    assert module.main(["--arm", "context", "--json"]) == 0
    assert called == []
    assert _json_tail(capsys.readouterr().out) == {
        "status": "not_run",
        "reason": "no_key",
    }


def test_the_model_arm_needs_two_models(monkeypatch, capsys):
    module = _load()
    monkeypatch.setattr(module.chat, "api_key", lambda: "present")
    assert module.main(["--arm", "model", "--json"]) == 0
    assert _json_tail(capsys.readouterr().out) == {
        "status": "not_run",
        "reason": "no_models",
    }


def test_the_context_arm_is_not_vacuous(monkeypatch):
    """The pre-Y arm must really render more than the real reading.

    Without this, "the two arms scored the same" would be indistinguishable
    from "the two arms were the same prompt". The reply seam is replaced, so
    this proves the harness composes both readings on the real addressed path
    while spending nothing.
    """
    module = _load()
    captured = []

    async def _reply(chat_id, user_id, text, **kwargs):
        captured.append(kwargs.get("context", ""))
        return module.chat.ChatReply(answered=True, text="باشه", turns=1)

    monkeypatch.setattr(module.chat, "reply", _reply)
    sizes = {}
    with module._bench(list(module.SCENARIOS)[:8]) as prepared:
        real_read = module.context_plan.read
        try:
            for label, reading in (("a", None), ("b", module.FULL_READING)):
                module.context_plan.read = (
                    real_read if reading is None else (lambda *a, **k: reading)
                )
                captured.clear()
                for scenario in prepared:
                    module.asyncio.run(
                        module.app_main._answer_conversationally(
                            module._update(
                                scenario["ask"],
                                chat_id=scenario["chat_id"],
                                user_id=scenario["user_id"],
                                reply=scenario["reply"],
                            ),
                            module._ctx(module._Bot()),
                        )
                    )
                sizes[label] = sum(len(block) for block in captured)
        finally:
            module.context_plan.read = real_read

    assert sizes["a"] > 0
    assert sizes["b"] > sizes["a"], "the pre-Y arm rendered no more than the real one"


def test_the_bench_puts_the_process_back():
    """A leaked config value would make a later test pass for the wrong reason."""
    module = _load()
    before = {key: getattr(module.config, key) for key in module._CONFIG_KEYS}
    with module._bench(list(module.SCENARIOS)[:2]) as prepared:
        assert prepared[0]["chat_id"] == module.CHAT_BASE
        assert module.config.DB_PATH == ":memory:"
    after = {key: getattr(module.config, key) for key in module._CONFIG_KEYS}
    assert after == before


def test_the_bench_cleans_up_on_a_deployment_that_has_credentials():
    """Cleanup must survive a populated pool.

    ``build_pools`` reads the database once per account, so a rebuild placed
    after the connection is dropped raises ``'NoneType' has no attribute
    'execute'``. With no credential configured the loop body never runs and the
    offline suite cannot see it — which is how the bug reached a live run.
    """
    module = _load()
    spec = next(
        spec for spec in module.config.GEMINI_POOLS if spec["workload"] == "chat"
    )
    original = list(spec["keys"])
    try:
        spec["keys"] = [("1", "bench-key")]
        with module._bench(list(module.SCENARIOS)[:1]):
            module.gemini_pool.build_pools()
            assert module.gemini_pool.has_accounts("chat"), "the pool has no account"
        # Reaching here means the cleanup did not touch a closed connection.
    finally:
        # Put the credential and the registry back, so a fake account cannot
        # make some later test believe the assistant is configured.
        spec["keys"] = original
        module.db.init()
        module.gemini_pool.build_pools()
    assert not module.gemini_pool.has_accounts("chat")


def test_the_bench_isolates_each_scenario_in_its_own_room():
    module = _load()
    with module._bench(list(module.SCENARIOS)[:3]) as prepared:
        chat_ids = [scenario["chat_id"] for scenario in prepared]
        user_ids = [scenario["user_id"] for scenario in prepared]
        assert len(set(chat_ids)) == len(chat_ids)
        assert len(set(user_ids)) == len(user_ids)


def test_the_corpus_carries_no_credential_like_strings():
    """A real token once leaked into a fixture. The corpus is prose only."""
    source = TOOL.read_text(encoding="utf-8")
    for marker in ("AIza", "sk-", "ghp_", "Bearer "):
        assert marker not in source, f"the tool carries {marker!r}"


@pytest.mark.parametrize("arm", ["a", "b"])
def test_the_report_names_the_limits_of_what_it_measures(arm):
    """The honesty notes are part of the output, not a comment."""
    module = _load()
    assert module.HONESTY
    assert any("REQUESTED" in line for line in module.HONESTY)
    assert any("NOT RUN" in line for line in module.HONESTY)
    report = module.score_transcript(module.synthetic_transcript(samples=1))
    assert report["arms"][arm]["metrics"]["quality_pass_rate"] == 1.0
