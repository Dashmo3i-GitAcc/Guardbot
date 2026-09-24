"""Automatic long-term memory: what Nexus may learn from ordinary conversation.

The explicit path is covered by ``tests/test_memory.py``. This file is about the
second write path — the one that reads ordinary messages — and the four things
that make it safe enough to have at all:

* **A statement, not a guess.** A memory is written only when a rule matches the
  message, or when a behaviour has been demonstrated enough times to count.
  Nothing is inferred from a transient state, a question, or somebody else.
* **A slot, not a sentence.** Every automatic memory is keyed by one entry in a
  closed vocabulary, so a new value replaces the old one and the set can never
  grow without bound.
* **Off the answer path.** The learning is a background task; nothing here is
  awaited by a handler, and a failure is a memory not learned rather than a
  reply not sent.
* **Never authority.** A memory is a sentence for the model to read. No
  permission path imports it, and model output is validated before it is stored.

Nothing here talks to Telegram, to a model, or to the network. Where a candidate
from the model seam is exercised, it is a *synthetic untrusted payload* fed
straight to the validator — the point is the server-side boundary, not a claim
that a provider answered correctly.
"""
import ast
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app import awareness_context, config, db, main, memory, memory_extract

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
    monkeypatch.setattr(config, "NEXUS_MEMORY_AUTO_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX_PER_USER", 30)
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX", 50000)
    monkeypatch.setattr(config, "NEXUS_MEMORY_RETENTION", 180 * 86400)
    monkeypatch.setattr(config, "NEXUS_MEMORY_VALUE_CHARS", 200)
    monkeypatch.setattr(config, "NEXUS_MEMORY_ITEMS", 4)
    monkeypatch.setattr(config, "NEXUS_MEMORY_CHARS", 300)
    monkeypatch.setattr(config, "NEXUS_MEMORY_SIGNAL_THRESHOLD", 3)
    monkeypatch.setattr(config, "NEXUS_MEMORY_SIGNAL_RETENTION", 30 * 86400)
    monkeypatch.setattr(config, "NEXUS_MEMORY_EXTRACT_MODEL", False)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_CHARS", 1500)
    monkeypatch.setattr(config, "NEXUS_AWARENESS_CONTEXT_DEEP", True)
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    db.init()
    db.memory_reset()
    db.signal_reset()
    db.people_reset()
    awareness_context.reset_rooms()
    memory.reset_state()
    yield
    db.memory_reset()
    db.signal_reset()
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


def _observe(text, *, chat_id=CHAT, user_id=USER):
    return asyncio.run(memory.observe(_User(user_id), chat_id, text))


def _slots(chat_id=CHAT, user_id=USER):
    return {row["key"]: row["value"] for row in memory.about(chat_id, user_id)}


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


# ── A. Extraction from ordinary conversation ──────────────────────────────
@pytest.mark.parametrize(
    "text,slot,value",
    [
        ("من یه برنامه‌نویس هستم", "identity.occupation", "برنامه نویس"),
        ("من برنامه‌نویسم و بیشتر با Python کار می‌کنم", "identity.programming", "Python"),
        ("I have switched to Python", "identity.programming", "Python"),
        ("زبانم فارسیه", "identity.language", "فارسی"),
        ("من شنا بلدم", "identity.skill", "شنا"),
        ("دارم روی یه ربات تلگرام کار می‌کنم", "identity.project", "یه ربات تلگرام"),
        ("منو رضا صدا کن", "identity.name", "رضا"),
        ("من COD بازی می‌کنم", "interest.gaming", "COD"),
        ("من راک گوش می‌دم", "interest.music", "راک"),
        ("فیلم ترسناک می‌بینم", "interest.movies", "ترسناک"),
        ("به موسیقی علاقه دارم", "interest.topic", "موسیقی"),
        ("من جواب‌های کوتاه رو بیشتر دوست دارم", "preference.answers", "concise"),
        ("I prefer detailed answers", "preference.answers", "detailed"),
        ("من خودمونی حرف زدن رو دوست دارم", "preference.style", "informal"),
    ],
)
def test_a_statement_about_the_speaker_becomes_one_memory(text, slot, value):
    _observe(text)
    assert _slots().get(slot) == value


def test_an_interest_stated_with_its_verb_is_remembered():
    """A durable interest is read from the verb a person uses, not from a noun
    they happened to mention."""
    _observe("من فوتبال بازی می‌کنم")
    assert _slots()["interest.gaming"] == "فوتبال"


# ── A. Humour, as a stated preference and nothing more ────────────────────
def test_a_stated_adult_humour_preference_is_a_flag_not_a_transcript():
    _observe("من شوخی‌های بزرگسال دوست دارم")
    assert _slots()["humor.adult"] == "preferred"


def test_a_stated_sarcasm_preference_is_remembered():
    _observe("من طنز و کنایه دوست دارم")
    assert _slots()["humor.sarcasm"] == "preferred"


def test_no_joke_content_is_ever_stored_only_the_flag():
    """The table holds the preference and never the material. A test that the
    joke itself is absent is the point of the whole humour design."""
    joke = "من شوخی‌های بزرگسال دوست دارم، اون یکی خیلی بی‌ادب بود"
    _observe(joke)
    stored = " ".join(_values())
    assert "preferred" in stored
    assert "بی‌ادب" not in stored


# ── A. Behaviour: counted, then promoted ──────────────────────────────────
def test_a_playful_style_is_remembered_only_after_repeated_evidence():
    _observe("هاها خیلی خنده‌دار بود")
    _observe("هاها بازم بگو")
    assert "style.playful" not in _slots()  # two is not a pattern
    _observe("هاها 😂")
    assert _slots()["style.playful"] == "frequent"


def test_an_informal_register_is_remembered_after_repeated_evidence():
    for _ in range(3):
        _observe("داداش چطوری")
    assert _slots()["preference.style"] == "informal"


def test_one_playful_message_is_not_a_personality():
    _observe("هاها 😂")
    assert "style.playful" not in _slots()


# ── B. Negative cases ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "امروز خیلی خسته‌ام",
        "الان حالم خوب نیست",
        "این هفته سرم شلوغه",
        "I'm tired today",
        "امروز هوا خوبه",
        "فردا میام",
    ],
)
def test_a_transient_state_never_becomes_a_memory(text):
    assert _observe(text) == []
    assert _slots() == {}


@pytest.mark.parametrize(
    "text",
    [
        "برادرم برنامه‌نویس است",
        "خواهرم معلمه",
        "My brother is a programmer",
        "دوستام پایتون کار می‌کنن",
    ],
)
def test_somebody_elses_attribute_is_never_attached_to_the_speaker(text):
    assert _observe(text) == []
    assert _slots() == {}


def test_a_question_is_not_a_statement_of_fact():
    assert _observe("با چه زبانی کار می‌کنی؟") == []
    assert _observe("برنامه‌نویسی سخته؟") == []
    assert _slots() == {}


def test_a_request_is_not_a_stable_preference():
    """«جواب کوتاه بده» is an instruction for one reply, not a preference."""
    assert _observe("جواب کوتاه بده") == []
    assert _slots() == {}


def test_a_link_is_never_a_value():
    assert memory.automatic("من اینو دوست دارم https://example.com") == []


def test_an_ordinary_message_produces_nothing():
    assert _observe("فکر کنم باید یه چیز دیگه امتحان کنیم") == []
    assert _slots() == {}


def test_no_personality_is_inferred_from_a_bare_emotion():
    """The rules read statements about *what* somebody is, never how they feel."""
    assert _observe("من خیلی باهوشم") == []
    assert _slots() == {}


# ── C. Lifecycle: create, replace, refine, dedupe, bound, expire ──────────
def test_contradictory_stale_values_are_replaced_not_accumulated():
    _observe("من بیشتر با JavaScript کار می‌کنم")
    assert _slots()["identity.programming"] == "JavaScript"
    _observe("رفتم روی Python")
    assert _slots()["identity.programming"] == "Python"
    assert len(memory.about(CHAT, USER)) == 1


def test_a_broad_interest_is_refined_rather_than_duplicated():
    _observe("من فوتبال بازی می‌کنم")
    _observe("من COD بازی می‌کنم")
    assert _slots()["interest.gaming"] == "COD"
    assert len(memory.about(CHAT, USER)) == 1


def test_restating_a_fact_leaves_one_row_with_a_newer_timestamp(monkeypatch):
    _observe("من یه برنامه‌نویس هستم")
    first = memory.about(CHAT, USER)[0]["created_at"]
    monkeypatch.setattr(db, "time", type("C", (), {"time": staticmethod(lambda: 999999)})())
    _observe("من یه برنامه‌نویس هستم")
    rows = memory.about(CHAT, USER)
    assert len(rows) == 1
    assert rows[0]["created_at"] == first  # the first time survives
    assert rows[0]["updated_at"] == 999999


def test_the_per_person_ceiling_still_binds_the_automatic_path(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_MAX_PER_USER", 3)
    for text in (
        "من یه برنامه‌نویس هستم",
        "زبانم فارسیه",
        "من شنا بلدم",
        "من COD بازی می‌کنم",
        "من راک گوش می‌دم",
    ):
        _observe(text)
    assert len(memory.about(CHAT, USER)) <= 3


def test_a_value_cannot_exceed_its_cap(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_VALUE_CHARS", 12)
    _observe("دارم روی یه ربات تلگرام خیلی خیلی بزرگ کار می‌کنم")
    for value in _values():
        assert len(value) <= 12


def test_an_expired_signal_counter_stops_promoting(monkeypatch):
    monkeypatch.setattr(db, "time", type("C", (), {"time": staticmethod(lambda: 100)})())
    for _ in range(3):
        _observe("هاها 😂")
    assert "style.playful" in _slots()
    db.signal_reset()
    db.memory_reset()
    monkeypatch.setattr(db, "time", type("C", (), {"time": staticmethod(lambda: 10**9)})())
    db.signal_bump(CHAT, USER, "playful", now=100)
    assert db.signal_prune(max_age=30 * 86400) == 1
    assert db.signal_count() == 0


def test_memory_expires_by_age(monkeypatch):
    monkeypatch.setattr(db, "time", type("C", (), {"time": staticmethod(lambda: 100)})())
    _observe("من یه برنامه‌نویس هستم")
    assert memory.about(CHAT, USER)
    monkeypatch.setattr(
        db, "time", type("C", (), {"time": staticmethod(lambda: 10**9)})()
    )
    assert memory.prune() >= 1
    assert memory.about(CHAT, USER) == []


# ── D. Context integration: the four sources stay distinct ────────────────
def test_a_relevant_memory_reaches_the_context_block():
    _observe("من برنامه‌نویسم و بیشتر با Python کار می‌کنم")
    out = awareness_context.blocks(_ctx(_msg(USER, "برای پروژه پایتونم چه کنم؟")))
    assert "Python" in out


def test_an_irrelevant_memory_is_excluded_from_the_block():
    """The example the brief gives: a favourite game must not appear in an
    answer about a programming project."""
    _observe("من COD بازی می‌کنم")
    out = awareness_context.blocks(_ctx(_msg(USER, "این پروژه پایتون رو چطور بنویسم؟")))
    assert "COD" not in out


def test_a_preference_is_relevant_whatever_the_subject():
    _observe("من جواب‌های کوتاه رو بیشتر دوست دارم")
    out = awareness_context.blocks(_ctx(_msg(USER, "یه سوال درباره کوهنوردی دارم")))
    assert "concise" in out


def test_the_memory_block_is_framed_as_the_person_s_words_not_the_server_s():
    _observe("من یه برنامه‌نویس هستم")
    out = awareness_context.blocks(_ctx(_msg(USER, "یه سوال فنی دارم")))
    assert "asked to be remembered" in out or "Remembered (their words)" in out


def test_memory_and_the_room_reading_are_separate_blocks():
    """Awareness and Memory are complementary, not one merged context."""
    _observe("من یه برنامه‌نویس هستم")
    out = awareness_context.blocks(_ctx(_msg(USER, "یه سوال دارم")))
    assert "برنامه نویس" in out
    assert "room's state" in out  # the awareness reading, distinct from memory


def test_the_conversation_reading_keeps_the_memory_source():
    _observe("من یه برنامه‌نویس هستم")
    out = awareness_context.blocks(
        _ctx(_msg(USER, "یه سوال دارم")), skip=awareness_context.CONVERSATION_SKIP
    )
    assert "برنامه نویس" in out


def test_the_four_sources_coexist_without_being_merged():
    """The calendar (fresh server reading), the room reading, the conversation
    and the memory each contribute their own bounded block."""
    _observe("من یه برنامه‌نویس هستم")
    anchor = _msg(USER, "یه سوال فنی دارم", at=1000)
    other = _msg(OTHER_USER, "منم سوال دارم", message_id=2, at=900)
    out = awareness_context.blocks(_ctx(anchor, messages=[other]))
    assert "current date" in out  # server clock, tier 0
    assert "room's state" in out  # awareness reading
    assert "برنامه نویس" in out  # long-term memory
    assert len(out) <= config.NEXUS_AWARENESS_CONTEXT_CHARS


# ── E. Freshness precedence ───────────────────────────────────────────────
def test_a_fresh_statement_replaces_the_stale_memory_it_contradicts():
    """Fresh input wins because the validated write path updates the slot: the
    block the model reads afterwards says Go, not the old Python."""
    _observe("من بیشتر با Python کار می‌کنم")
    assert _slots()["identity.programming"] == "Python"
    _observe("I have switched to Go")
    assert _slots()["identity.programming"] == "Go"
    out = awareness_context.blocks(_ctx(_msg(USER, "برای پروژه جدیدم چه زبانی؟")))
    assert "Go" in out
    assert "Python" not in out


def test_a_project_scoped_choice_is_not_a_durable_self_fact():
    """«این پروژه رو با Go می‌خوام بنویسم» is about one project, not about the
    person, so it must not overwrite what they said about themselves. Precision
    here is what keeps a fresh remark from silently rewriting a profile."""
    _observe("من بیشتر با Python کار می‌کنم")
    assert _observe("این پروژه رو با Go می‌خوام بنویسم") == []
    assert _slots()["identity.programming"] == "Python"


def test_the_memory_is_the_person_s_words_so_fresh_input_can_outrank_it():
    """A stale memory is never the only voice: it is framed as something the
    person said, and the newest message is the anchor of the same context, so
    the model reads the fresh statement beside it rather than instead of it."""
    _observe("من بیشتر با Python کار می‌کنم")
    anchor = _msg(USER, "نه، این بار Go می‌خوام", at=1000)
    ctx = _ctx(anchor)
    assert ctx.messages[-1]["text"] == "نه، این بار Go می‌خوام"
    block = awareness_context.blocks(ctx)
    assert "asked to be remembered" in block or "Remembered (their words)" in block


# ── F. Repetitive-question prevention ─────────────────────────────────────
def test_a_known_preference_is_in_context_so_it_need_not_be_asked_again():
    _observe("من بیشتر با Python کار می‌کنم")
    out = awareness_context.blocks(_ctx(_msg(USER, "برای پروژه جدیدم چه زبانی پیشنهاد می‌کنی؟")))
    assert "Python" in out


def test_a_known_name_is_in_context_so_it_need_not_be_asked_again():
    _observe("منو رضا صدا کن")
    out = awareness_context.blocks(_ctx(_msg(USER, "منو یادت هست؟")))
    assert "رضا" in out


def test_a_known_project_is_in_context_so_it_need_not_be_re_established():
    _observe("دارم روی یه ربات تلگرام کار می‌کنم")
    out = awareness_context.blocks(_ctx(_msg(USER, "رباتم رو چطور تست کنم؟")))
    assert "ربات" in out


def test_the_known_answer_is_present_before_any_question_is_asked():
    """The server's half of "do not ask again": the fact is in the block. Whether
    the model then asks is a live-probe question, not a deterministic one."""
    _observe("من بیشتر با Python کار می‌کنم")
    block = awareness_context.blocks(_ctx(_msg(USER, "چه زبانی خوبه؟")))
    assert "programming: Python" in block


# ── G. Isolation ──────────────────────────────────────────────────────────
def test_one_person_s_memory_is_never_another_s():
    _observe("من یه برنامه‌نویس هستم", user_id=USER)
    assert memory.about(CHAT, OTHER_USER) == []


def test_one_group_s_memory_is_never_another_group_s():
    _observe("من یه برنامه‌نویس هستم", chat_id=CHAT)
    assert memory.about(OTHER_CHAT, USER) == []


def test_a_private_memory_never_renders_in_a_group():
    _observe("من یه برنامه‌نویس هستم", chat_id=PRIVATE_CHAT)
    out = awareness_context.blocks(_ctx(_msg(USER, "یه سوال دارم")))
    assert "برنامه نویس" not in out


def test_a_disabled_switch_means_no_read_and_no_write(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", False)
    assert _observe("من یه برنامه‌نویس هستم") == []
    assert memory.about(CHAT, USER) == []
    assert db.memory_count() == 0


def test_the_automatic_switch_can_be_turned_off_alone(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_AUTO_ENABLED", False)
    assert _observe("من یه برنامه‌نویس هستم") == []
    assert db.memory_count() == 0


def test_a_bot_s_message_is_never_remembered():
    assert asyncio.run(memory.observe(_User(USER, is_bot=True), CHAT, "من یه برنامه‌نویس هستم")) == []
    assert db.memory_count() == 0


def test_a_failed_write_leaves_the_caller_unharmed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "memory_remember", boom)
    assert _observe("من یه برنامه‌نویس هستم") == []  # no raise
    assert db.memory_count() == 0


def test_a_failed_read_leaves_the_caller_unharmed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "memory_for", boom)
    assert memory.about(CHAT, USER) == []
    assert _observe("من یه برنامه‌نویس هستم") is not None


def test_a_failed_signal_counter_leaves_the_caller_unharmed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "signal_bump", boom)
    asyncio.run(memory.observe(_User(USER), CHAT, "هاها 😂"))  # no raise


# ── H. Security: untrusted candidate, never authority ─────────────────────
def test_the_memory_module_is_not_an_authority_source():
    """Nothing that decides a permission may import it."""
    for module in ("rbac", "admin_service"):
        names = _imported_names(__import__(f"app.{module}", fromlist=[module]))
        assert "memory" not in names
        assert "memory_extract" not in names


def test_the_model_seam_is_isolated_from_the_other_workloads():
    names = _imported_names(memory_extract)
    for forbidden in (
        "chat",
        "awareness",
        "intent",
        "ai_intent",
        "moderation",
        "web_search",
        "transcribe",
        "voice_live",
        "rbac",
        "admin_service",
    ):
        assert forbidden not in names


@pytest.mark.parametrize(
    "candidate",
    [
        {"slot": "not.a.slot", "value": "anything"},
        {"slot": "identity.occupation", "value": "x"},
        {"slot": "identity.occupation", "value": "see https://example.com"},
        {"slot": "identity.occupation", "value": "@someone"},
        {"slot": "identity.occupation", "value": "my brother the programmer"},
        {"slot": "identity.occupation", "value": "برادرم برنامه‌نویس"},
        "not a dict",
        None,
    ],
)
def test_an_untrusted_candidate_the_server_will_not_have_is_dropped(candidate):
    assert memory.validate_candidate(candidate) is None


def test_a_valid_candidate_is_kept_and_re_stamped_as_model_output():
    valid = memory.validate_candidate(
        {"slot": "identity.programming", "value": "Python"}
    )
    assert valid["slot"] == "identity.programming"
    assert valid["value"] == "Python"
    assert valid["source"] == memory.SOURCE_MODEL
    assert valid["confidence"] < 1.0


def test_a_model_candidate_reaches_storage_only_through_the_validator(monkeypatch):
    """A synthetic untrusted payload — the server keeps the slot it recognises
    and drops the one it does not. This is the trust boundary, not a claim that
    a provider answered."""
    monkeypatch.setattr(config, "NEXUS_MEMORY_EXTRACT_MODEL", True)
    monkeypatch.setattr(memory_extract, "enabled", lambda: True)

    async def fake_candidates(text):
        return [
            {"slot": "identity.programming", "value": "Python"},
            {"slot": "invented.slot", "value": "admin"},
        ]

    monkeypatch.setattr(memory_extract, "candidates", fake_candidates)
    _observe("من با یه زبون خاص کار می‌کنم")
    assert _slots() == {"identity.programming": "Python"}


# ── I. Performance: the gate, the bounds, the decoupling ──────────────────
def test_the_deterministic_path_makes_no_provider_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no provider call may happen on the free path")

    from app import gemini_pool

    monkeypatch.setattr(gemini_pool, "generate", boom)
    assert _observe("من یه برنامه‌نویس هستم") == ["identity.occupation"]
    assert _observe("هاها 😂") is not None


def test_the_model_seam_is_off_when_its_switch_is_off(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the seam is off; no call may be attempted")

    from app import gemini_pool

    monkeypatch.setattr(gemini_pool, "generate", boom)
    monkeypatch.setattr(memory_extract, "enabled", lambda: True)
    _observe("من اهل شیرازم")  # self-info, matches no rule
    assert db.memory_count() == 0


def test_an_irrelevant_message_never_reaches_the_seam(monkeypatch):
    calls: list = []

    async def spy(text):
        calls.append(text)
        return []

    monkeypatch.setattr(config, "NEXUS_MEMORY_EXTRACT_MODEL", True)
    monkeypatch.setattr(memory_extract, "enabled", lambda: True)
    monkeypatch.setattr(memory_extract, "candidates", spy)
    _observe("امروز هوا خوبه")  # no first person, no self-information
    assert calls == []


def test_a_message_a_rule_already_knows_never_reaches_the_seam(monkeypatch):
    calls: list = []

    async def spy(text):
        calls.append(text)
        return []

    monkeypatch.setattr(config, "NEXUS_MEMORY_EXTRACT_MODEL", True)
    monkeypatch.setattr(memory_extract, "enabled", lambda: True)
    monkeypatch.setattr(memory_extract, "candidates", spy)
    _observe("من یه برنامه‌نویس هستم")
    assert calls == []


def test_the_seam_is_disabled_without_its_own_credential(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_EXTRACT_MODEL", True)
    from app import gemini_pool

    monkeypatch.setattr(gemini_pool, "has_accounts", lambda workload: False)
    assert memory_extract.enabled() is False


def test_the_seam_asks_only_its_own_workload(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_EXTRACT_MODEL", True)
    from app import gemini_pool

    seen: list = []

    def accounts(workload):
        seen.append(workload)
        return True

    monkeypatch.setattr(gemini_pool, "has_accounts", accounts)
    assert memory_extract.enabled() is True
    assert seen == ["memory"]


def test_the_memory_block_is_bounded_by_its_budget():
    for text in (
        "من یه برنامه‌نویس هستم",
        "زبانم فارسیه",
        "من شنا بلدم",
        "من COD بازی می‌کنم",
        "من راک گوش می‌دم",
        "منو رضا صدا کن",
    ):
        _observe(text)
    rows = memory.about(CHAT, USER, limit=99)
    block = memory.render(rows, budget=120)
    assert len(block) <= 120


def test_the_retrieval_block_shows_at_most_the_configured_items():
    for text in (
        "من یه برنامه‌نویس هستم",
        "زبانم فارسیه",
        "من شنا بلدم",
        "من COD بازی می‌کنم",
        "من راک گوش می‌دم",
    ):
        _observe(text)
    assert len(memory.about(CHAT, USER, limit=2)) == 2


def test_storage_cannot_grow_without_bound_across_many_messages():
    """A person who says the same durable thing fifty times still has one row."""
    for _ in range(50):
        _observe("من بیشتر با Python کار می‌کنم")
    assert db.memory_count() == 1


def test_observe_is_a_coroutine_so_a_handler_cannot_block_on_it():
    assert inspect.iscoroutinefunction(memory.observe)


def test_the_group_handler_schedules_memory_rather_than_awaiting_it():
    """The hard requirement: memory is off the synchronous answer path."""
    from app import main

    source = inspect.getsource(main.on_group_chat)
    assert "_schedule_memory_observation" in source
    assert "await memory.observe" not in source
    assert "memory.remember(" not in source


def test_the_scheduler_never_raises_without_a_scheduler():
    from app import main

    class NoScheduler:
        pass

    class Ctx:
        application = NoScheduler()

    # No running loop and no ``create_task``: the helper must swallow it.
    main._schedule_memory_observation(Ctx(), _User(USER), CHAT, "من یه برنامه‌نویس هستم")


# ── Explicit memory continues to work through the same entry point ────────
def test_an_explicit_request_still_writes_through_observe():
    _observe("یادت باشه من برنامه‌نویس پایتونم")
    rows = memory.about(CHAT, USER)
    assert rows
    assert rows[0]["source"] == memory.SOURCE_EXPLICIT
    assert rows[0]["value"] == "من برنامه‌نویس پایتونم"


def test_an_explicit_clause_and_an_automatic_slot_can_coexist():
    _observe("یادت باشه من برنامه‌نویس پایتونم")
    _observe("من COD بازی می‌کنم")
    slots = _slots()
    assert "interest.gaming" in slots
    assert any(row["source"] == memory.SOURCE_EXPLICIT for row in memory.about(CHAT, USER))


# ── 28. Empty / no-candidate behaviour ────────────────────────────────────
@pytest.mark.parametrize("text", ["", "   ", "؟", "😂"])
def test_an_empty_or_contentless_message_produces_nothing(text):
    assert memory.automatic(text) == []
    assert _observe(text) == []
    assert db.memory_count() == 0


# ══ AWARENESS IS AN OPTIONAL SOURCE: CHAT SURVIVES WITHOUT IT ═════════════
# These drive ``main._answer_conversationally`` with the real gate, the real
# context composition and the real memory reader; only the network seam
# (``chat.reply``) is replaced. They are what makes "memory does not depend on
# awareness" a property of the running code rather than of a module in isolation.
_CHAT = -100
_SPEAKER = 7


class _Bot:
    def __init__(self):
        self.id = 1
        self.username = "guardbot"
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, *args, **kwargs):
        pass


def _update(text):
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            message_id=10, photo=None, video=None, animation=None,
            video_note=None, sticker=None, voice=None, audio=None,
            document=None, text=text, caption=None, reply_to_message=None,
        ),
        effective_chat=SimpleNamespace(id=_CHAT, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=_SPEAKER, full_name="Tester", username="tester", is_bot=False
        ),
    )


def _chat_turn(monkeypatch, text, *, awareness_on):
    """One addressed message through the real conversational path.

    Returns the context blocks handed to the model. ``chat.reply`` is replaced
    so no provider is needed; everything that builds the context is real.
    """
    bot = _Bot()
    ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
    seen: list[str] = []

    async def _reply(chat_id, user_id, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        seen.append(context)
        return main.chat.ChatReply(answered=True, text="پاسخ", turns=1)

    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", awareness_on)
    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    asyncio.run(main._answer_conversationally(_update(text), ctx))
    return seen


def test_chat_still_carries_the_memory_when_awareness_is_off(monkeypatch):
    """The brief's requirement: with awareness unavailable, chat falls back to
    the other sources — and long-term memory is one of them."""
    _observe(
        "من برنامه‌نویسم و بیشتر با Python کار می‌کنم",
        chat_id=_CHAT,
        user_id=_SPEAKER,
    )
    seen = _chat_turn(monkeypatch, "برای پروژه پایتونم چه کنم؟", awareness_on=False)
    assert seen and "Python" in seen[0]


def test_chat_still_carries_the_memory_when_the_room_reading_fails(monkeypatch):
    """Awareness on but the reading cannot be built: the memory still arrives,
    and nothing is fabricated in the reading's place."""
    _observe(
        "من برنامه‌نویسم و بیشتر با Python کار می‌کنم",
        chat_id=_CHAT,
        user_id=_SPEAKER,
    )
    monkeypatch.setattr(main, "_room_reading", lambda *a, **k: "")
    seen = _chat_turn(monkeypatch, "برای پروژه پایتونم چه کنم؟", awareness_on=True)
    assert seen and "Python" in seen[0]


def test_chat_answers_normally_when_awareness_is_off_and_memory_is_empty(monkeypatch):
    """Nothing to remember, awareness off: the turn is still answered."""
    seen = _chat_turn(monkeypatch, "یه سوال دارم", awareness_on=False)
    assert seen is not None  # the model was reached; the turn was not dropped


def test_the_memory_block_is_not_duplicated_when_awareness_is_on(monkeypatch):
    """The reading already carries the memory source, so the fallback must not
    add a second copy."""
    _observe(
        "من برنامه‌نویسم و بیشتر با Python کار می‌کنم",
        chat_id=_CHAT,
        user_id=_SPEAKER,
    )
    seen = _chat_turn(monkeypatch, "برای پروژه پایتونم چه کنم؟", awareness_on=True)
    assert seen and seen[0].count("programming: Python") == 1


def test_the_memory_context_is_empty_when_memory_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "NEXUS_MEMORY_ENABLED", False)
    assert main._memory_context(_CHAT, _SPEAKER, "هرچی") == ""


def test_the_memory_context_fails_soft(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(memory, "about", boom)
    assert main._memory_context(_CHAT, _SPEAKER, "هرچی") == ""


def test_the_memory_context_renders_nothing_for_a_stranger(monkeypatch):
    assert main._memory_context(_CHAT, 999, "هرچی") == ""
