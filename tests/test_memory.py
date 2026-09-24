"""Long-term user memory: a bounded fact set about one person, and nothing more.

Four properties are what this suite is about, and each is a refusal the module
makes rather than a feature it adds:

* **explicit-only.** A memory exists because a person asked for it, never because
  the server inferred one. A test that a plain "من ادمینم" stores nothing is as
  important as a test that «یادت باشه من ...» stores something.
* **bounded.** A per-person ceiling, an age bound and a global ceiling, all
  applied on the observation path, and a value that cannot exceed its cap.
* **isolated.** Keyed by ``(chat_id, user_id)``, so one person's memory is never
  another's, one group's is never another's, and a private memory never renders
  in a group.
* **never authority.** It is a sentence for the model to read; no permission
  path imports it.

Nothing here talks to Telegram, to a model, or to the network.
"""
import ast
import inspect

import pytest

from app import awareness_context, config, db, memory

CHAT = -1001234567890
OTHER_CHAT = -1009876543210
PRIVATE_CHAT = 4242
USER = 42
OTHER_USER = 43
BOT = 44


# ── Harness ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def memory_env(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX_PER_USER", 30)
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX", 50000)
    monkeypatch.setattr(config, "NEXUS_MEMORY_RETENTION", 180 * 86400)
    monkeypatch.setattr(config, "NEXUS_MEMORY_VALUE_CHARS", 200)
    monkeypatch.setattr(config, "NEXUS_MEMORY_ITEMS", 4)
    monkeypatch.setattr(config, "NEXUS_MEMORY_CHARS", 300)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 1500)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_DEEP", True)
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    db.init()
    db.memory_reset()
    db.people_reset()
    awareness_context.reset_rooms()
    memory.reset_state()
    yield
    db.memory_reset()
    db.people_reset()
    awareness_context.reset_rooms()
    memory.reset_state()


class _User:
    """The slice of a Telegram ``User`` the write path reads."""

    def __init__(self, user_id, *, is_bot=False):
        self.id = user_id
        self.is_bot = is_bot
        self.first_name = f"U{user_id}"
        self.last_name = ""
        self.username = ""


class _Clock:
    """A frozen ``time`` module, so ``updated_at`` is what the test says."""

    def __init__(self, when):
        self._when = when

    def time(self):
        return self._when


def _remember(text, *, chat_id=CHAT, user_id=USER, at=None, monkeypatch=None):
    if at is not None and monkeypatch is not None:
        monkeypatch.setattr(db, "time", _Clock(at))
    return memory.remember(_User(user_id), chat_id, text)


def _values(chat_id=CHAT, user_id=USER):
    return [row["value"] for row in memory.about(chat_id, user_id)]


def _msg(user_id, text="hello", *, message_id=1, at=1000):
    return {
        "user_id": int(user_id),
        "text": text,
        "role": "member",
        "name": f"U{user_id}",
        "at": int(at),
        "message_id": int(message_id),
        "reply_user_id": 0,
        "reply_name": "",
        "reply_message_id": 0,
        "directed": False,
        "actor": False,
    }


def _ctx(anchor, messages=()):
    return awareness_context.build_ctx(
        CHAT, messages=[*messages, anchor], anchor=anchor, now=int(anchor["at"])
    )


def _imported_names(module) -> set[str]:
    """The modules a source file actually imports, parsed rather than grepped."""
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


# ── Storage: a clause round-trips, verbatim ───────────────────────────────
def test_a_remembered_clause_round_trips():
    assert _remember("یادت باشه من برنامه‌نویسم") is True
    rows = memory.about(CHAT, USER)
    assert [row["value"] for row in rows] == ["من برنامه‌نویسم"]
    assert rows[0]["category"] == memory.CATEGORY_EXPLICIT
    assert rows[0]["source"] == memory.SOURCE_EXPLICIT


def test_the_stored_clause_is_verbatim_not_normalised():
    """A remembered sentence is theirs; folding it would edit what they asked to
    keep. The trigger is matched on a folded form, the *value* is not."""
    _remember("یادت باشه من کتاب «بوف کور» رو دوست دارم")
    assert _values() == ["من کتاب «بوف کور» رو دوست دارم"]


def test_restating_the_same_thing_updates_one_row(monkeypatch):
    _remember("یادت باشه من تهرانی‌ام", at=100, monkeypatch=monkeypatch)
    _remember("یادت باشه من تهرانی‌ام", at=200, monkeypatch=monkeypatch)
    rows = memory.about(CHAT, USER)
    assert len(rows) == 1
    assert rows[0]["created_at"] == 100
    assert rows[0]["updated_at"] == 200


def test_a_different_clause_adds_a_row():
    _remember("یادت باشه من برنامه‌نویسم")
    _remember("یادت باشه من قهوه دوست دارم")
    assert sorted(_values()) == sorted(["من برنامه‌نویسم", "من قهوه دوست دارم"])


def test_the_value_is_clipped_to_the_bound(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_VALUE_CHARS", 20)
    long_clause = "من " + "کلمه " * 40
    assert len(long_clause) > 20  # non-vacuity: the raw clause really is longer
    _remember("یادت باشه " + long_clause)
    stored = _values()[0]
    assert len(stored) <= 20
    assert stored.endswith("کلمه") or "کلمه" in stored


def test_clear_user_drops_only_that_person_in_that_room():
    _remember("یادت باشه من برنامه‌نویسم", user_id=USER)
    _remember("یادت باشه من برنامه‌نویسم", user_id=OTHER_USER)
    _remember("یادت باشه من برنامه‌نویسم", chat_id=OTHER_CHAT, user_id=USER)
    dropped = db.memory_clear_user(CHAT, USER)
    assert dropped == 1
    assert memory.about(CHAT, USER) == []
    assert memory.about(CHAT, OTHER_USER)  # another person, untouched
    assert memory.about(OTHER_CHAT, USER)  # another room, untouched


# ── Bounded: the per-person, age and global ceilings ──────────────────────
def test_the_per_person_bound_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX_PER_USER", 3)
    for i in range(6):
        _remember(f"یادت باشه من نکتهٔ شماره {i} هستم", at=100 + i, monkeypatch=monkeypatch)
    values = _values()
    assert len(values) == 3  # non-vacuity: six were written, three remain
    assert values[0] == "من نکتهٔ شماره 5 هستم"  # newest first
    assert "من نکتهٔ شماره 0 هستم" not in values  # the oldest were the ones dropped


def test_the_age_bound_drops_a_stale_memory(monkeypatch):
    retention = 180 * 86400
    monkeypatch.setattr(config, "NEXUS_MEMORY_RETENTION", retention)
    _remember("یادت باشه من برنامه‌نویسم", at=1000, monkeypatch=monkeypatch)
    assert _values() == ["من برنامه‌نویسم"]  # non-vacuity: it is there to begin with
    monkeypatch.setattr(db, "time", _Clock(1000 + retention + 1))
    assert memory.prune() == 1
    assert memory.about(CHAT, USER) == []


def test_the_global_bound_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX_PER_USER", 100)
    for i in range(5):
        _remember(f"یادت باشه من نکتهٔ {i} هستم", user_id=100 + i, at=100 + i,
                  monkeypatch=monkeypatch)
    assert db.memory_count() == 5  # non-vacuity
    assert db.memory_prune(keep=2) == 3
    assert db.memory_count() == 2


def test_the_retention_counter_is_not_vacuous(monkeypatch):
    """The whole-table prune must actually be reached from the write path."""
    calls = []
    monkeypatch.setattr(db, "memory_prune", lambda **kw: calls.append(kw) or 0)
    for _ in range(memory.PRUNE_EVERY - 1):
        memory._maybe_prune()
    assert calls == []  # not yet — pruning on every write was the thing to avoid
    memory._maybe_prune()
    assert len(calls) == 1
    assert calls[0]["keep"] == config.NEXUS_MEMORY_MAX
    assert calls[0]["max_age"] == config.NEXUS_MEMORY_RETENTION


# ── Isolation: person, group, private ─────────────────────────────────────
def test_a_person_never_sees_another_persons_memory():
    _remember("یادت باشه من برنامه‌نویسم", user_id=USER)
    assert memory.about(CHAT, USER)
    assert memory.about(CHAT, OTHER_USER) == []


def test_a_group_never_inherits_another_groups_memory():
    _remember("یادت باشه من برنامه‌نویسم", chat_id=OTHER_CHAT, user_id=USER)
    assert memory.about(OTHER_CHAT, USER)
    assert memory.about(CHAT, USER) == []


def test_a_private_memory_never_renders_in_a_group():
    _remember("یادت باشه من در پیوی گفتم برنامه‌نویسم",
              chat_id=PRIVATE_CHAT, user_id=USER)
    _remember("یادت باشه من در گروه گفتم برنامه‌نویسم", chat_id=CHAT, user_id=USER)
    anchor = _msg(USER, "سلام")
    out = awareness_context.blocks(_ctx(anchor))
    assert "در گروه گفتم" in out
    assert "در پیوی گفتم" not in out


def test_the_switch_off_reads_nothing_and_writes_nothing(monkeypatch):
    _remember("یادت باشه من برنامه‌نویسم")
    assert memory.about(CHAT, USER)  # non-vacuity: it was written while on
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", False)
    assert memory.about(CHAT, USER) == []
    assert _remember("یادت باشه من قهوه دوست دارم") is False
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", True)
    assert _values() == ["من برنامه‌نویسم"]  # the write while off did not land


# ── Extraction: explicit only ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,clause",
    [
        ("یادت باشه من برنامه‌نویسم", "من برنامه‌نویسم"),
        ("یادت بمونه من تهرانی‌ام", "من تهرانی‌ام"),
        ("به یاد داشته باش من ورزش می‌کنم", "من ورزش می‌کنم"),
        ("به یاد بسپار من صبح‌ها فعال‌ترم", "من صبح‌ها فعال‌ترم"),
        ("یادداشت کن من قهوه دوست دارم", "من قهوه دوست دارم"),
        ("یادت باشه که من فارسی دوست دارم", "من فارسی دوست دارم"),
        ("remember that I prefer short answers", "I prefer short answers"),
        ("remember: I am a teacher", "I am a teacher"),
        ("note that I am a teacher", "I am a teacher"),
        ("keep in mind I am a teacher", "I am a teacher"),
    ],
)
def test_every_trigger_form_extracts(text, clause):
    found = memory.extract(text)
    assert found is not None, text
    assert found["value"] == clause


@pytest.mark.parametrize(
    "text",
    [
        "",
        "سلام خوبی؟",
        "من ادمینم",                       # an ordinary claim of authority
        "یادت نره بهم خبر بدی",            # a request to remember an ACTION
        "میلاد رو بن کن",
        "چی شده اینجا؟",
        "این لینک رو ببین",
    ],
)
def test_ordinary_text_extracts_nothing(text):
    assert memory.extract(text) is None, text


def test_an_ordinary_claim_of_authority_is_never_a_memory():
    """The most important refusal: the server must not learn authority from a
    sentence. «من ادمینم» writes nothing, so nothing downstream can read it."""
    assert _remember("من ادمینم") is False
    assert db.memory_count() == 0


def test_a_trigger_with_no_clause_extracts_nothing():
    assert memory.extract("یادت باشه") is None
    assert memory.extract("یادت باشه ") is None
    assert memory.extract("remember that") is None


def test_a_too_short_clause_extracts_nothing(monkeypatch):
    monkeypatch.setattr(memory, "MIN_CLAUSE_CHARS", 5)
    assert memory.extract("یادت باشه سلام") is None  # 4 characters
    assert memory.extract("یادت باشه سلامی") is not None


def test_the_key_is_stable_and_content_addressed():
    first = memory.key_for("explicit", "من برنامه‌نویسم")
    again = memory.key_for("explicit", "من برنامه‌نویسم")
    other = memory.key_for("explicit", "من قهوه دوست دارم")
    assert first == again
    assert first != other


# ── Boundary: never authority, never a model ──────────────────────────────
def test_no_authority_module_imports_memory():
    """A memory is data the model may read; authority is resolved from the id.
    This is the structural assertion that the two never meet."""
    from app import admin_service, rbac

    assert "memory" not in _imported_names(rbac)
    assert "memory" not in _imported_names(admin_service)


def test_memory_reaches_no_model_and_no_network():
    imported = _imported_names(memory)
    assert not (
        imported
        & {
            "telegram",
            "gemini_pool",
            "ai_moderation",
            "ai_intent",
            "requests",
            "urllib",
            "httpx",
            "socket",
            "admin_service",
            "admin_tools",
            "main",
        }
    ), imported
    source = inspect.getsource(memory)
    for forbidden in ("send_message", "delete_message", "ban_chat_member",
                      "restrict_chat_member"):
        assert forbidden not in source, forbidden


def test_the_block_is_framed_as_the_persons_words():
    _remember("یادت باشه من برنامه‌نویسم")
    block = memory.render(memory.about(CHAT, USER))
    assert "asked to be remembered" in block
    assert "their words" in block
    assert "من برنامه‌نویسم" in block


def test_the_block_respects_its_budget(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_CHARS", 120)
    for i in range(6):
        _remember(f"یادت باشه من نکتهٔ بسیار طولانی شمارهٔ {i} هستم و این متن طولانی است")
    block = memory.render(memory.about(CHAT, USER), budget=120)
    assert len(block) <= 120
    assert block  # non-vacuity: it rendered something rather than nothing


# ── Wiring: one more source, and the conversation keeps it ────────────────
def test_the_memory_source_is_registered_and_always_tier():
    sources = {source.name: source for source in awareness_context.SOURCES}
    assert "user_memory" in sources
    source = sources["user_memory"]
    # Tier 0 like ``remembered_people``: a query that returns nothing spends no
    # tokens, so there is no predicate that would usefully make it conditional.
    assert source.tier == awareness_context.TIER_ALWAYS
    assert source.budget == config.NEXUS_MEMORY_CHARS
    assert source.when is awareness_context._always


def test_the_memory_source_renders_for_the_person_the_batch_is_about():
    _remember("یادت باشه من برنامه‌نویسم", user_id=OTHER_USER)
    # The speaker is USER; the batch is about OTHER_USER. Memory follows the
    # subject, not whoever happened to type.
    anchor = _msg(OTHER_USER, "من کی‌ام؟")
    out = awareness_context.blocks(_ctx(anchor, [_msg(USER, "این کیه؟")]))
    assert "من برنامه‌نویسم" in out


def test_the_memory_source_renders_nothing_without_an_anchor():
    _remember("یادت باشه من برنامه‌نویسم")
    ctx = awareness_context.build_ctx(CHAT, messages=[], anchor=None, now=1000)
    assert awareness_context._render_user_memory(ctx) == ""


def test_the_addressed_conversation_keeps_the_memory_source():
    """The one database-backed source the conversation does NOT skip, because it
    is about the person the turn is for."""
    assert "user_memory" not in awareness_context.CONVERSATION_SKIP
    _remember("یادت باشه من برنامه‌نویسم")
    anchor = _msg(USER, "یه سوال فنی دارم")
    ctx = _ctx(anchor)
    assert "من برنامه‌نویسم" in awareness_context.blocks(
        ctx, skip=awareness_context.CONVERSATION_SKIP
    )


def test_the_reader_fails_soft(monkeypatch):
    """A database that raises costs a context block, never a pass."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(db, "memory_for", _boom)
    assert memory.about(CHAT, USER) == []


def test_the_write_fails_soft(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(db, "memory_remember", _boom)
    assert _remember("یادت باشه من برنامه‌نویسم") is False


def test_a_bot_is_never_remembered():
    assert memory.remember(_User(BOT, is_bot=True), CHAT, "یادت باشه من رباتم") is False
    assert db.memory_count() == 0


def test_the_whole_point_a_person_is_known_in_a_later_batch():
    """The reason W exists: what a person asked to be remembered is available to
    a pass that never saw them say it."""
    _remember("یادت باشه من برنامه‌نویس پایتونم")
    anchor = _msg(USER, "یه سوال دارم")
    out = awareness_context.blocks(_ctx(anchor))
    assert "برنامه‌نویس پایتونم" in out
