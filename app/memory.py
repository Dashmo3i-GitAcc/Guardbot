"""Long-term user memory: what the server may remember about one person.

The brief's goal is that Nexus knows something durable about a member it has not
met this hour — a preference, a role they hold elsewhere, a fact they asked to be
remembered by. That is a different thing from everything already stored:

* the **room window** (``group_messages``) is a recent view of a conversation,
  kept for an hour and thrown away;
* **Awareness** is what the room is doing, summarised per chat;
* **Intent** is what one message is trying to do;
* **identity memory** (``app/people``) is a name and a message *count*.

None of them answers "what durable thing do I know about this person". So this
module owns one narrow thing: a bounded set of remembered clauses, keyed by
``(chat_id, user_id)``.

Four rules shape it, and each is a refusal.

**It is explicit-only.** A memory is written when, and only when, a person asks
to be remembered — «یادت باشه من ...». The clause they typed is stored, bounded
and verbatim. Nothing is inferred from ordinary conversation, because inferring a
durable fact from a passing sentence is exactly the guessing this codebase
refuses everywhere else, and because doing it well would need a model call the
evidence rule forbids on the ordinary path. A memory the server *guessed* is a
memory that can be wrong about a real person, and being wrong about a real person
is the failure this project is built to avoid.

**It grants nothing.** A memory is a sentence for the model to read, never a
permission, an authorisation or a gate. There is no function here that returns an
authority, and nothing in ``app/rbac.py`` or ``app/admin_service.py`` imports this
module. "یادت باشه من ادمینم" makes Nexus *say* the person said so; it does not
make the server believe it, because authority is resolved from the Telegram id and
from nothing a person typed.

**It is bounded, and by measurement.** ``NEXUS_MEMORY_MAX_PER_USER`` is 30, chosen
from the benchmark recorded in the roadmap rather than from the brief's "20–50":
storage is 208 bytes a row, so the ceiling that actually matters is not disk (30
items across 3000 members is 17.9 MB against a 200 MB budget) but how much can
ever be *read* — the retrieval block surfaces at most about four items — so 30
leaves a wide recall margin while keeping the table a fact set rather than a log.
Retention is applied on the observation path, because this process has no
scheduler.

**It never crosses a boundary.** The key is ``(chat_id, user_id)``, so a group
cannot inherit another group's memory and a private-chat memory can never render
in a group. Isolation is by construction — there is no code path that reads a
person's memory without naming the room — rather than by a check that could be
forgotten.

The write is a deterministic trigger match over the message the person typed, in
the same "evidence, never authority" shape as the other readers. The clause is
stored as the person wrote it: no normalisation is applied to the *value*, because
a remembered sentence is theirs and folding it would edit what they asked to keep.
"""
from __future__ import annotations

import hashlib
import logging
import re

from . import config, db, people

log = logging.getLogger("guardbot.memory")

# The one category this version writes. Named rather than implied so a later
# version can add categories without a migration: the column already exists.
CATEGORY_EXPLICIT = "explicit"

# Where a row came from. Kept as its own column so a reviewer can always tell a
# clause the person typed from anything a future version might derive, and so a
# future "only trust what they said" filter is a ``WHERE`` clause rather than a
# rewrite.
SOURCE_EXPLICIT = "explicit"

# Below this a clause is not a memory. «باشه» after a trigger is an acknowledgement,
# not a fact, and storing it would fill the table with noise.
MIN_CLAUSE_CHARS = 3

# Every how many recordings the whole-table retention bounds are applied. The
# per-person bound runs on every write — it is the indexed, cheap statement — but
# the age and global bounds are whole-table scans, so they run rarely. Same shape
# and same reason as ``app/people.PRUNE_EVERY``.
PRUNE_EVERY = 200
_since_prune = 0

# The explicit request to be remembered, as the person might type it. Written as
# patterns rather than plain strings because Persian is typed with two letters for
# the same sound («ی»/«ي») and the trigger must match either. Each alternative is
# anchored on a word boundary by the surrounding ``\b``-like behaviour of the
# Persian letter classes; English triggers carry ``\b`` explicitly.
_TRIGGER_PATTERNS = (
    r"[یي]ادت\s+باشه",
    r"[یي]ادت\s+بمونه",
    r"[یي]ادت\s+هست",
    r"به\s+[یي]اد\s+داشته?\s+باش",
    r"به\s+[یي]اد\s+بسپار",
    r"[یي]ادداشت\s+کن",
    r"\bremember\s+that\b",
    r"\bremember\s*:",
    r"\bnote\s+that\b",
    r"\bkeep\s+in\s+mind\b",
)
_TRIGGER_RE = re.compile(
    "|".join(f"(?:{pattern})" for pattern in _TRIGGER_PATTERNS),
    re.IGNORECASE | re.UNICODE,
)

# Punctuation and whitespace a clause may open with once the trigger is removed.
# «یادت باشه: من ...» and «یادت باشه که من ...» both leave the fact itself.
_LEADING = re.compile(r"^[\s:،,.;؛\-–—]+")
# Persian's «که» is a complementiser, not part of the fact: «یادت باشه که من ...».
# Stripped only when it is the first token, so a fact that legitimately starts
# with the word keeps it.
_LEADING_KEH = re.compile(r"^که\s+")


def reset_state() -> None:
    """Forget the prune counter. For tests."""
    global _since_prune
    _since_prune = 0


def _clip(text: str, cap: int) -> str:
    """Bound one clause to ``cap`` characters, on a word boundary where there is
    one. A memory is short by design; this is the guard that keeps a person from
    writing an essay into a context block."""
    text = " ".join(str(text or "").split())
    if cap <= 0 or len(text) <= cap:
        return text
    cut = text.rfind(" ", 0, max(1, cap - 1))
    if cut <= 0:
        cut = max(1, cap - 1)
    return text[:cut].rstrip()


def extract(text: str) -> dict | None:
    """The clause a person explicitly asked to be remembered, or ``None``.

    Deterministic and conservative: no trigger means no memory, and a trigger
    followed by nothing substantial means no memory. Returns a candidate dict
    rather than writing, so the decision and the storage can be tested apart.
    """
    if not text:
        return None
    match = _TRIGGER_RE.search(str(text))
    if not match:
        return None
    rest = str(text)[match.end():]
    rest = _LEADING.sub("", rest)
    rest = _LEADING_KEH.sub("", rest)
    value = _clip(rest, int(config.NEXUS_MEMORY_VALUE_CHARS))
    if len(value) < MIN_CLAUSE_CHARS:
        return None
    return {"category": CATEGORY_EXPLICIT, "value": value}


def key_for(category: str, value: str) -> str:
    """A stable key for a clause, so restating it updates one row.

    Derived from the normalised clause and not from a timestamp, which is what
    makes the write an upsert rather than a growing log: saying the same thing
    twice leaves one memory with a newer ``updated_at``. ``people.normalize`` is
    reused so «یادت باشه» folded one way and the other produce the same key.
    """
    material = f"{category}\x00{people.normalize(value)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _get(obj, key: str, default=""):
    """One field from a ``User`` object or a dict, without assuming which."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def remember(user, chat_id: int, text: str) -> bool:
    """Record a person's explicit request to be remembered. Never raises.

    Called from the observation path for every group message, beside
    ``people.remember``. It is a regex over text the handler already holds, so an
    ordinary message that asks for nothing costs one failed match and no write.
    A missing id, a bot, or the switch being off is a no-op rather than an error:
    a memory is never worth failing a handler over.
    """
    if not config.NEXUS_MEMORY_ENABLED:
        return False
    user_id = int(_get(user, "id", 0) or 0)
    if not user_id or not chat_id:
        return False
    if getattr(user, "is_bot", False):
        return False
    found = extract(text)
    if not found:
        return False
    try:
        db.memory_remember(
            chat_id,
            user_id,
            key_for(found["category"], found["value"]),
            category=found["category"],
            value=found["value"],
            source=SOURCE_EXPLICIT,
            confidence=1.0,
        )
        # The per-person bound, right where it is needed and nowhere else: this
        # is the indexed delete (0.02 ms measured), and pruning only the person
        # who just overflowed keeps it off the whole-table path.
        db.memory_prune_user(
            chat_id, user_id, keep=int(config.NEXUS_MEMORY_MAX_PER_USER)
        )
    except Exception:  # noqa: BLE001 - never the reason a handler fails
        log.exception("could not record a memory")
        return False
    _maybe_prune()
    return True


def _maybe_prune() -> None:
    global _since_prune
    _since_prune += 1
    if _since_prune < PRUNE_EVERY:
        return
    _since_prune = 0
    prune()


def prune() -> int:
    """Apply the age and global bounds, and decay the signal counters.

    Best effort; never raises. The counters decay on the same sweep as the
    memories because they answer the same question — "is this still true?" — and
    a behaviour that has not been demonstrated inside the retention window
    should not keep a style label alive.
    """
    dropped = 0
    try:
        dropped = db.memory_prune(
            keep=max(0, int(config.NEXUS_MEMORY_MAX)),
            max_age=max(0, int(config.NEXUS_MEMORY_RETENTION)),
        )
    except Exception:  # noqa: BLE001
        log.exception("memory retention prune failed")
    try:
        db.signal_prune(max_age=max(0, int(config.NEXUS_MEMORY_SIGNAL_RETENTION)))
    except Exception:  # noqa: BLE001
        log.exception("memory signal decay failed")
    return dropped


def _rank(rows: list[dict], topic: str) -> list[dict]:
    """The rows that bear on ``topic``, most relevant first.

    Relevance is a deterministic overlap between the topic's words and the
    row's own label, value and slot hints, plus a constant for the rows that are
    always worth having. Those are the rows that describe **the person** rather
    than a subject — an explicit memory (they asked for it to be kept), an
    identity (who they are), a preference, a style, a humour signal. Knowing
    somebody is a programmer is relevant to almost any question they ask, so
    those rows do not have to be mentioned to be shown. An **interest** is a
    subject, so it does: a favourite game must not appear in an answer about a
    programming project.

    Only rows that score above zero are returned, so an unrelated memory is
    *excluded* rather than merely ranked last. The caller's limit still bounds
    the result.
    """
    tokens = {t for t in people.normalize(topic).split() if len(t) >= 3}
    if not tokens:
        return list(rows)
    scored: list[tuple[int, int, dict]] = []
    for row in rows:
        slot = str(row.get("key") or "")
        label = SLOTS.get(slot, ("", ""))[1]
        hints = " ".join(_SLOT_HINTS.get(slot, ()))
        hay = set(people.normalize(f"{label} {row.get('value', '')} {hints}").split())
        overlap = len(tokens & hay)
        always = row.get("category") in _ALWAYS_CATEGORIES
        score = overlap * 2 + (1 if always else 0)
        scored.append((score, int(row.get("updated_at") or 0), row))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [row for score, _at, row in scored if score > 0]


def about(
    chat_id: int, user_id: int, *, limit: int = 0, topic: str = ""
) -> list[dict]:
    """One person's memories in one room, most relevant first.

    The retrieval reader, and it is a *read*: it returns rows for the caller to
    render, grants nothing, and takes both ids so no caller can ask across rooms.
    A failure returns an empty list — a context block is never worth failing a
    pass — and the switch being off returns empty rather than raising.

    ``topic`` is the text the block is being built for. When it is given, only
    the memories that bear on it are returned (see ``_rank``); when it is empty
    the most recent are returned, which is the behaviour the explicit path had.
    The whole set is read first and then bounded, because the set is already
    bounded by the per-person ceiling.
    """
    if not config.NEXUS_MEMORY_ENABLED:
        return []
    if not chat_id or not user_id:
        return []
    limit = int(limit or config.NEXUS_MEMORY_ITEMS)
    if limit <= 0:
        return []
    try:
        rows = db.memory_for(int(chat_id), int(user_id), limit=0)
    except Exception:  # noqa: BLE001 - a block is never worth a pass
        log.exception("could not read a person's memory")
        return []
    # The relationship row is a server observation, not something the person
    # asked to be remembered, so it is never returned here: it is rendered by
    # ``relationship`` in its own framing. Filtering at the reader is what makes
    # that a property of the reader rather than of every renderer.
    rows = [
        row for row in rows if row.get("category") != CATEGORY_RELATIONSHIP
    ]
    if topic:
        rows = _rank(rows, topic)
    return rows[:limit]


def render(rows: list[dict], *, budget: int = 0) -> str:
    """The memories as one bounded block, or ``""`` when there is nothing.

    Stated as what the person *asked to be remembered*, never as a fact the
    server asserts, so the model treats it as something a member said rather than
    as a statement from the server — the same distinction the room block makes.

    The budget covers the framing line as well as the memories, so the guarantee
    is ``len(block) <= budget``. A memory that does not fit the room left is
    clipped rather than dropped: losing the whole block because the header is
    large would turn a bounded block into a missing one.
    """
    cap = int(budget or config.NEXUS_MEMORY_CHARS)
    if cap <= 0:
        return ""
    header = "What this person asked to be remembered (their words, not the server's):\n"
    if len(header) >= cap:
        # A shorter frame beats no frame: a memory with no framing reads as the
        # server's own claim rather than as something a person said.
        header = "Remembered (their words):\n"
    if len(header) >= cap:
        return ""
    lines: list[str] = []
    used = len(header)
    for row in rows or []:
        value = " ".join(str(row.get("value") or "").split())
        if not value:
            continue
        # An automatic memory is stored as a slot and a value, so it is shown
        # with its label ("programming: Python"). An explicit one is the person's
        # own clause and is shown as they wrote it — adding a label to their
        # words would be the server editing what they asked to keep.
        label = SLOTS.get(str(row.get("key") or ""), ("", ""))[1]
        text = f"{label}: {value}" if label else value
        line = f"- {text}\n"
        if used + len(line) > cap:
            room = cap - used
            if room > len("- ") + 8:
                clipped = _clip(text, room - len("- ") - 1)
                if clipped:
                    lines.append(f"- {clipped}\n")
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return header + "".join(lines)


# ── Automatic extraction: categories, slots and the gate ──────────────────
#
# The explicit path stores a clause the person asked to keep. This path learns
# from ordinary conversation — and the design problem is not "what can be
# extracted" but "what must never be".
#
# Three rules shape it, and each is a refusal:
#
# **A slot, not a sentence.** Every automatic memory is written against one entry
# in ``SLOTS``, a closed vocabulary. The slot is the memory's identity, so a new
# value for the same slot *replaces* the old one: "I program in JavaScript" then
# "I program in Python" is one row, not two contradictory ones. It also bounds a
# person's automatic memories by the size of the vocabulary rather than by how
# much they talk — an unbounded log is impossible by construction.
#
# **Deterministic first.** The gate and the structurer are rules over the message
# the person typed. Most messages produce no candidate and no write. The model
# seam (``app/memory_extract.py``) is off unless an operator enables it *and*
# gives the isolated ``memory`` workload a credential.
#
# **Nothing inferred.** Every value is either a closed-vocabulary token
# (a language, a role) or a phrase the person actually wrote next to a first-person
# marker. A transient state, a question, or a statement about somebody else is
# refused outright — see ``_transient`` and ``_third_party``.

CATEGORY_IDENTITY = "identity"
CATEGORY_INTEREST = "interest"
CATEGORY_PREFERENCE = "preference"
CATEGORY_STYLE = "style"
CATEGORY_HUMOR = "humor"
# How this person has treated Nexus. A category of its own because it is not a
# fact *about* them that they told us — it is the server's own count of their
# behaviour toward the assistant, and it is rendered by ``relationship`` in a
# framing of its own rather than through ``render``. Keeping it out of
# ``about``/``render`` is what stops a server observation from being shown to
# the model as something the person said about themselves.
CATEGORY_RELATIONSHIP = "relationship"

SOURCE_AUTO = "auto"
SOURCE_MODEL = "model"

# slot -> (category, label). The label is how the value is shown in the context
# block, so the model reads "programming: Python" rather than a bare "Python".
SLOTS: dict[str, tuple[str, str]] = {
    "identity.occupation": (CATEGORY_IDENTITY, "occupation"),
    "identity.programming": (CATEGORY_IDENTITY, "programming"),
    "identity.language": (CATEGORY_IDENTITY, "language"),
    "identity.skill": (CATEGORY_IDENTITY, "skill"),
    "identity.project": (CATEGORY_IDENTITY, "project"),
    "identity.name": (CATEGORY_IDENTITY, "preferred name"),
    "interest.gaming": (CATEGORY_INTEREST, "gaming"),
    "interest.music": (CATEGORY_INTEREST, "music"),
    "interest.movies": (CATEGORY_INTEREST, "movies"),
    "interest.topic": (CATEGORY_INTEREST, "interest"),
    "preference.answers": (CATEGORY_PREFERENCE, "answers"),
    "preference.language": (CATEGORY_PREFERENCE, "explanation language"),
    "preference.style": (CATEGORY_PREFERENCE, "tone"),
    "style.playful": (CATEGORY_STYLE, "playful humour"),
    "style.teasing": (CATEGORY_STYLE, "teasing"),
    "humor.adult": (CATEGORY_HUMOR, "adult humour"),
    "humor.sarcasm": (CATEGORY_HUMOR, "sarcasm"),
    # The one slot that is not a fact about the person but a reading of how they
    # have treated the assistant. Its value is a closed token (``hostile`` or
    # ``friendly``) so the renderer can state it as the server's observation.
    "relationship.tone": (CATEGORY_RELATIONSHIP, "how they treat you"),
}

# The categories that describe **the person** rather than a subject, and so are
# relevant whatever the conversation is about. See ``_rank``: these are shown
# without having to be mentioned, while an interest must be mentioned to appear.
# An identity is on this list deliberately — the brief's own example is a Persian
# question about a Python project, and no lexical overlap can bridge a Persian
# topic to an English value, so a relevance test alone would hide exactly the
# memory that makes the feature worth having.
_ALWAYS_CATEGORIES = frozenset(
    {
        CATEGORY_EXPLICIT,
        CATEGORY_IDENTITY,
        CATEGORY_PREFERENCE,
        CATEGORY_STYLE,
        CATEGORY_HUMOR,
    }
)

# Extra words that make an interest relevant, so a Persian question about a game
# can reach an interest stored under its English label. Only the interest slots
# need these — every other category is always relevant. Small and hand-written on
# purpose: this is a lexical hint, not an inference, and it is auditable.
_SLOT_HINTS: dict[str, frozenset[str]] = {
    "interest.gaming": frozenset({"بازی", "گیم", "game", "gaming"}),
    "interest.music": frozenset({"موسیقی", "آهنگ", "music", "song"}),
    "interest.movies": frozenset({"فیلم", "سریال", "movie", "film", "series"}),
    "interest.topic": frozenset({"علاقه", "سرگرمی", "interest", "hobby"}),
}

# Closed vocabularies. A value is only accepted when it is one of these, which is
# what makes "I am a programmer" safe to store while "I am tired" is not: the
# rule is not "the sentence looked like a role", it is "the last word is a role".
_ROLE_WORDS = frozenset(
    {
        "برنامه‌نویس", "برنامه نویس", "توسعه‌دهنده", "مهندس", "طراح", "معلم",
        "دبیر", "استاد", "دانشجو", "دانش‌آموز", "پزشک", "پرستار", "مدیر",
        "نویسنده", "هنرمند", "حسابدار", "وکیل", "راننده", "آشپز", "کارمند",
        "programmer", "developer", "engineer", "designer", "teacher", "student",
        "doctor", "nurse", "manager", "writer", "artist", "accountant", "lawyer",
        "driver", "cook", "researcher", "scientist",
    }
)
_LANGUAGES = frozenset(
    {
        "python", "javascript", "js", "typescript", "ts", "java", "go", "golang",
        "rust", "php", "ruby", "kotlin", "swift", "c", "c++", "c#", "csharp",
        "sql", "html", "css", "dart", "scala", "perl", "matlab", "bash", "lua",
        "elixir", "haskell", "r",
    }
)
_HUMAN_LANGUAGES = frozenset(
    {
        "فارسی", "انگلیسی", "عربی", "ترکی", "کردی", "آلمانی", "فرانسوی",
        "اسپانیایی", "روسی", "چینی",
        "persian", "farsi", "english", "arabic", "turkish", "kurdish", "german",
        "french", "spanish", "russian", "chinese",
    }
)

# A transient state is not a fact about a person. A message carrying one of these
# is refused whole, so "I'm tired today" can never become "tired: yes" — and the
# refusal is a word list rather than a model judgement because it must be
# auditable.
_TRANSIENT_RE = re.compile(
    r"(?:امروز|امشب|الان|فعلا|فعلاً|این\s+هفته|این\s+روزها|حالم|خسته\s*ام|خسته‌ام"
    r"|عصبانی|ناراحتم|حوصل"
    r"|\btoday\b|\btonight\b|\bright\s+now\b|\bcurrently\b|\bat\s+the\s+moment\b"
    r"|\bthis\s+week\b|\bthese\s+days\b|\bi\s+feel\b|\bi'?m\s+tired\b"
    r"|\bi'?m\s+angry\b|\bi'?m\s+annoyed\b)",
    re.IGNORECASE | re.UNICODE,
)

# Somebody else's attribute is not this person's. Checked on the captured value
# and on the opening of the message, so «بازی برادرم رو دوست دارم» cannot become
# an interest of the speaker.
_THIRD_PARTY_RE = re.compile(
    r"(?:برادرم|برادرم|خواهرم|دوستم|دوستام|رفیقم|پدرم|مادرم|همکارم|همسرم|زنم"
    r"|شوهرم|بچه‌ام|پسرعمو|دخترعمو"
    r"|\bmy\s+(?:brother|sister|friend|father|mother|dad|mom|colleague|coworker"
    r"|wife|husband|son|daughter|cousin|partner|boss)\b"
    r"|\bhe\s+is\b|\bshe\s+is\b|\bthey\s+are\b)",
    re.IGNORECASE | re.UNICODE,
)

# A question is not a statement of fact. Conservative: a message that ends in a
# question mark contributes no candidate at all.
_QUESTION_TAIL = re.compile(r"[?؟]\s*$")

# The speaker's own marker, stripped from the front of a captured value. It is
# the person, not the thing they are describing, and a capture that begins
# before the verb can otherwise carry it in.
_LEADING_SELF = re.compile(
    r"^(?:من|منم|منو|خودم|ما|I|I'?m|I'?ve|my|myself|we|our)\s+",
    re.IGNORECASE | re.UNICODE,
)

# A value that carries a link, a mention or a handle is not a stable trait.
_NOT_A_VALUE_RE = re.compile(r"(?:https?://|www\.|@\w|t\.me/|[\x00-\x1f])", re.IGNORECASE)


def _norm_value(text: str) -> str:
    """One candidate value, folded to the form it is stored in.

    A leading first-person marker is stripped, because a capture that starts
    before the verb can otherwise carry it into the value: «من COD بازی می‌کنم»
    would be remembered as gaming «من COD». The marker is the speaker, not the
    thing, so it is removed here rather than in every pattern.
    """
    value = " ".join(str(text or "").split()).strip(" .,،:;-–—")
    value = _LEADING_SELF.sub("", value).strip(" .,،:;-–—")
    return value


def _fold(text: str) -> str:
    """The text the rules match against: ZWNJ replaced by a space.

    Persian is written both with and without the zero-width non-joiner —
    «میکنم» and «می کنم» are the same word — so a rule that spells one form
    misses half the people who type the other. Folding to a space before
    matching is what makes one rule cover both. The refusals still run on the
    raw text, where the joined form («خستهام») is the one that matters.
    """
    return str(text or "").replace("\u200c", " ")


def _tokens(text: str) -> set[str]:
    return {t for t in people.normalize(text).split() if len(t) >= 2}


def _last_word(text: str) -> str:
    words = people.normalize(text).split()
    return words[-1] if words else ""


def _in_roles(value: str) -> bool:
    """Whether a captured phrase ends in a role word.

    A suffix test rather than equality, because Persian roles are written with
    and without a ZWNJ or a space («برنامه‌نویس» / «برنامه نویس») and the
    normaliser folds both to the spaced form. A phrase like «برنامه نویس ارشد»
    does not pass, and that is deliberate: the closed vocabulary is what makes
    "I am a programmer" safe to store while "I am tired" is not.
    """
    norm = people.normalize(value)
    if not norm:
        return False
    for word in _ROLE_WORDS:
        folded = people.normalize(word)
        if folded and (norm == folded or norm.endswith(" " + folded)):
            return True
    return False


def _in_languages(value: str) -> bool:
    return people.normalize(value).replace(" ", "") in _LANGUAGES


def _in_human_languages(value: str) -> bool:
    return people.normalize(value) in {people.normalize(w) for w in _HUMAN_LANGUAGES}


def _any(_value: str) -> bool:
    return True


# The rules. Each is (slot, compiled pattern with one capture group, validator).
# The validator is what keeps a broad pattern honest: «من X هستم» matches almost
# anything, so the captured X must end in a role word before it is stored.
_RULES: tuple[tuple[str, re.Pattern, object], ...] = (
    # ── identity ──
    (
        "identity.programming",
        re.compile(
            r"(?:بیشتر\s+)?با\s+(?P<v>[\w+#.]{2,20})\s+کار\s+می\s?کنم"
            r"|\bI\s+(?:mostly\s+)?(?:program|code|work)\s+(?:in|with)\s+(?P<v2>[\w+#.]{2,20})"
            r"|\bI(?:'ve| have)?\s+switched\s+to\s+(?P<v3>[\w+#.]{2,20})"
            r"|(?:رفتم|سوییچ\s+کردم)\s+(?:روی|به)\s+(?P<v4>[\w+#.]{2,20})",
            re.IGNORECASE | re.UNICODE,
        ),
        _in_languages,
    ),
    (
        "identity.language",
        re.compile(
            r"زبان(?:م)?\s+(?P<v>[\w\u0600-\u06ff]{2,20})\s*(?:است|ه)\b"
            r"|\bI\s+speak\s+(?P<v2>[\w ]{2,20})"
            r"|\bmy\s+(?:native\s+)?language\s+is\s+(?P<v3>[\w ]{2,20})",
            re.IGNORECASE | re.UNICODE,
        ),
        _in_human_languages,
    ),
    (
        "identity.occupation",
        re.compile(
            r"من\s+(?:یه\s+|یک\s+)?(?P<v>[\w\u0600-\u06ff +]{2,40}?)\s+هستم"
            r"|\bI(?:'m| am)\s+(?:a|an)\s+(?P<v2>[\w +]{2,40})",
            re.IGNORECASE | re.UNICODE,
        ),
        _in_roles,
    ),
    (
        "identity.skill",
        re.compile(
            r"من\s+(?P<v>[\w\u0600-\u06ff +]{2,30}?)\s+بلدم"
            r"|\bI\s+know\s+how\s+to\s+(?P<v2>[\w +]{2,30})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "identity.project",
        re.compile(
            r"دارم\s+روی\s+(?P<v>[\w\u0600-\u06ff ]{2,40}?)\s+کار\s+می\s?کنم"
            r"|\bI(?:'m| am)\s+working\s+on\s+(?P<v2>[\w ]{2,40})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "identity.name",
        re.compile(
            r"(?:منو|من\s+رو)\s+(?P<v>[\w\u0600-\u06ff]{2,20})\s+صدا\s+(?:کن|بزن)"
            r"|\bcall\s+me\s+(?P<v2>[\w ]{2,20})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    # ── interest ──
    (
        "interest.gaming",
        re.compile(
            r"(?P<v>[\w\u0600-\u06ff ]{2,30}?)\s+بازی\s+می\s?کنم"
            r"|\bI\s+play\s+(?P<v2>[\w ]{2,30})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "interest.music",
        re.compile(
            r"(?P<v>[\w\u0600-\u06ff ]{2,30}?)\s+(?:گوش\s+می\s?دم|گوش\s+می\s?کنم)"
            r"|\bI\s+listen\s+to\s+(?P<v2>[\w ]{2,30})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "interest.movies",
        re.compile(
            r"(?:فیلم|سریال)\s+(?P<v>[\w\u0600-\u06ff ]{2,30}?)\s+(?:می\s?بینم|تماشا)"
            r"|\bI\s+watch\s+(?P<v2>[\w ]{2,30})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "interest.topic",
        re.compile(
            r"به\s+(?P<v>[\w\u0600-\u06ff ]{2,30}?)\s+علاقه\s+دارم"
            r"|\bI(?:'m| am)\s+interested\s+in\s+(?P<v2>[\w ]{2,30})",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    # ── preference ──
    (
        "preference.answers",
        re.compile(
            r"(?:جواب|پاسخ)\s*(?:‌های|های|ا)?\s*(?:رو\s*)?(?:کوتاه|مختصر|کامل|مفصل|بلند)"
            r"[^\n؟?]{0,24}(?:دوست\s+دارم|ترجیح\s+می\s?دم|می\s?پسندم|بیشتر)"
            r"|\bI\s+prefer\s+(?:short|concise|detailed|long)\s+answers?\b"
            r"|\b(?:short|concise|detailed|long)\s+answers?\s+(?:are\s+)?(?:better|preferred)\b",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "preference.language",
        re.compile(
            r"(?P<v>فارسی|انگلیسی|عربی|ترکی)\s+(?:توضیح|جواب|پاسخ|بنویس|بگو)"
            r"|\b(?:explain|answer|reply|write)\s+in\s+(?P<v2>[\w ]{2,20})",
            re.IGNORECASE | re.UNICODE,
        ),
        _in_human_languages,
    ),
    (
        "preference.style",
        re.compile(
            r"(?:خودمونی|دوستانه)\s+(?:حرف|صحبت|جواب)"
            r"|\b(?:informal|casual)\s+(?:tone|style|answers?)\b",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
)

# The rules whose match *is* the value are handled by the rule itself (see
# ``_answer_length_value``); every other rule captures a noun phrase.


def _rule_value(slot: str, match: re.Match) -> str:
    """The captured value from whichever alternative matched."""
    for name in ("v", "v2", "v3", "v4"):
        try:
            got = match.group(name)
        except IndexError:  # this alternative did not capture; try the next
            got = None
        if got:
            return _norm_value(got)
    return ""


def automatic(text: str) -> list[dict]:
    """The automatic candidates in one message, or an empty list.

    Deterministic, conservative and cheap: a handful of regexes over the text the
    handler already holds. A transient message, a question, or one that opens
    with somebody else is refused whole before any rule runs.
    """
    if not config.NEXUS_MEMORY_ENABLED or not config.NEXUS_MEMORY_AUTO_ENABLED:
        return []
    raw = str(text or "")
    if not raw.strip():
        return []
    if _TRANSIENT_RE.search(raw):
        return []
    if _QUESTION_TAIL.search(raw):
        return []
    if _THIRD_PARTY_RE.match(raw.strip()):
        return []
    # The refusals above read the message as typed; the rules read it folded, so
    # one pattern covers «می‌کنم» and «می کنم».
    folded = _fold(raw)
    out: list[dict] = []
    seen: set[str] = set()
    for slot, pattern, validator in _ALL_RULES:
        match = pattern.search(folded)
        if not match:
            continue
        value = _rule_value(slot, match) or _FIXED_VALUES.get(slot, "")
        if slot == "preference.answers":
            value = _answer_length_value(raw)
        if len(value) < 2 or slot in seen:
            continue
        if _THIRD_PARTY_RE.search(value) or _NOT_A_VALUE_RE.search(value):
            continue
        try:
            if not validator(value):
                continue
        except Exception:  # noqa: BLE001 - a validator is never worth a handler
            log.exception("memory validator failed for slot %s", slot)
            continue
        seen.add(slot)
        out.append(
            {
                "slot": slot,
                "category": SLOTS[slot][0],
                "value": _clip(value, int(config.NEXUS_MEMORY_VALUE_CHARS)),
                "source": SOURCE_AUTO,
                "confidence": 0.7,
            }
        )
    return out


def _answer_length_value(raw: str) -> str:
    """``concise`` or ``detailed``, from whichever half of the pattern matched."""
    folded = people.normalize(raw)
    if any(word in folded for word in ("کوتاه", "مختصر", "short", "concise")):
        return "concise"
    if any(word in folded for word in ("کامل", "مفصل", "بلند", "detailed", "long")):
        return "detailed"
    return ""


# ── Explicit humour preferences (a statement, never a judgement) ───────────
#
# The brief allows a compact *style* memory for humour — "adult_humor =
# frequently_used". There is one honest way to learn that without classifying
# the content of anybody's jokes: read the preference the person *states*. So
# this is a statement rule like every other rule here, and never a detector run
# over the jokes themselves. What is stored is the flag and nothing else: a
# style preference is not permission, it does not touch content policy, and it
# grants nothing. The chat layer's own safety rules are unaffected by it.
_HUMOUR_RULES: tuple[tuple[str, re.Pattern, object], ...] = (
    (
        "humor.adult",
        re.compile(
            r"(?:شوخی|جوک|طنز)\s*(?:‌های|های|ه)?\s*(?:بزرگسال|رکیک|جنسی|سکسی)"
            r"|\b(?:adult|dirty|explicit|sexual)\s+jokes?\b"
            r"|\bI\s+(?:like|prefer|love)\s+(?:adult|dirty|explicit)\s+humou?r\b",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
    (
        "humor.sarcasm",
        re.compile(
            r"(?:طنز|کنایه|طعنه)\s*(?:رو\s*)?(?:دوست\s+دارم|بیشتر)"
            r"|\bI\s+(?:like|prefer)\s+sarcas(?:m|tic)\b",
            re.IGNORECASE | re.UNICODE,
        ),
        _any,
    ),
)

# Every rule, in one tuple: the noun-phrase rules above plus the fixed-value
# humour statements. A module-level join rather than a literal so a later
# version can add a rule group without editing the loop in ``automatic``.
_ALL_RULES = _RULES + _HUMOUR_RULES

# The slots whose *match is the value*: there is nothing to capture because the
# sentence itself states the value. A fixed table rather than a special case in
# the loop, so ``automatic`` stays one uniform pass over the rules.
_FIXED_VALUES: dict[str, str] = {
    "preference.style": "informal",
    "humor.adult": "preferred",
    "humor.sarcasm": "preferred",
}


# ── The write path: one slot, one row ─────────────────────────────────────
def _store(
    chat_id: int,
    user_id: int,
    slot: str,
    *,
    value: str,
    source: str,
    confidence: float,
) -> bool:
    """Write one automatic memory and re-apply the per-person bound.

    The key is the **slot**, not a hash of the value, and that one choice is the
    whole lifecycle: a new value for the same slot upserts the same row, so
    "I program in JavaScript" then "I've switched to Python" is one memory that
    says Python rather than two that contradict each other. Replace, merge and
    deduplicate are therefore not three algorithms — they are what a slot-scoped
    upsert *is*. ``SLOTS`` is closed, so a person's automatic memories are
    bounded by the vocabulary rather than by how much they type.

    The per-person bound runs right here, the same indexed statement the
    explicit path uses, so an automatic write can never outgrow the ceiling.
    """
    db.memory_remember(
        int(chat_id),
        int(user_id),
        str(slot),
        category=SLOTS[slot][0],
        value=value,
        source=source,
        confidence=float(confidence),
    )
    db.memory_prune_user(
        int(chat_id), int(user_id), keep=int(config.NEXUS_MEMORY_MAX_PER_USER)
    )
    return True


def validate_candidate(candidate) -> dict | None:
    """A model's candidate, or ``None`` if the server will not have it.

    This is the boundary the brief draws and the reason model output can be
    treated as data: the candidate is *untrusted*, and the server decides. The
    checks are the same ones the deterministic path applies — the slot must be
    in the closed vocabulary, the value must be substantial, must carry no link
    or handle, and must not be somebody else's attribute — plus a clip to the
    configured value length. Anything else is dropped, silently and completely.
    """
    if not isinstance(candidate, dict):
        return None
    slot = str(candidate.get("slot") or "").strip()
    if slot not in SLOTS:
        return None
    # The rejections run on the value **as the model wrote it**, before the
    # leading speaker marker is stripped: "my brother is a programmer" must be
    # refused as somebody else's attribute, and stripping "my " first would hide
    # exactly the word that gives it away.
    raw_value = str(candidate.get("value") or "")
    if _NOT_A_VALUE_RE.search(raw_value) or _THIRD_PARTY_RE.search(raw_value):
        return None
    value = _clip(
        _norm_value(raw_value), int(config.NEXUS_MEMORY_VALUE_CHARS)
    )
    if len(value) < 2:
        return None
    if _NOT_A_VALUE_RE.search(value) or _THIRD_PARTY_RE.search(value):
        return None
    return {
        "slot": slot,
        "category": SLOTS[slot][0],
        "value": value,
        "source": SOURCE_MODEL,
        "confidence": 0.5,
    }


# ── Behaviour: counted, then promoted, then allowed to decay ───────────────
#
# A repeated *behaviour* is different from a stated fact: one playful message is
# not a personality, so a style is only remembered once it has been demonstrated
# ``NEXUS_MEMORY_SIGNAL_THRESHOLD`` times. The counters live in their own
# bounded table, hold a number and never a message, and decay by age — so a
# person who was playful a year ago is not labelled playful for ever.
#
# The detectors are deliberately about *form* rather than content: laughter and
# emoji, and colloquial address. Nothing here reads what a person meant, which
# is what keeps a behavioural memory a conversational preference rather than a
# psychological reading. The humour slots are reached only through an explicit
# statement (above) — a model is never asked to judge somebody's personality,
# and this version does not try to detect a taste from its subject matter.
_SIGNAL_RULES: dict[str, tuple[re.Pattern, str, str]] = {
    "playful": (
        re.compile(
            r"[\U0001F600-\U0001F64F\U0001F910-\U0001F92F]"
            r"|ها\s*ها|هه{2,}|خخ{2,}|لول|\blol\b|\bhaha\b|\bhehe\b",
            re.IGNORECASE | re.UNICODE,
        ),
        "style.playful",
        "frequent",
    ),
    "informal": (
        re.compile(
            r"(?:داداش|بچه‌ها|بچه\s*ها|چطوری|چه\s*خبر|چخبر|رفیق)"
            r"|\b(?:bro|dude|mate)\b",
            re.IGNORECASE | re.UNICODE,
        ),
        "preference.style",
        "informal",
    ),
}

# How a message *directed at Nexus* treats it. Separate from ``_SIGNAL_RULES``
# above for two reasons that are both structural:
#
#   * These count only on messages aimed at the assistant. A member cursing
#     about their ISP, or teasing another member, says nothing about how they
#     treat Nexus — so the caller passes ``directed`` and nothing is counted
#     otherwise. The addressed path already computes that flag once per message
#     (``main._nexus_directed``); this reuses it rather than guessing again.
#   * They promote into ``relationship.tone``, whose two values are the two
#     directions of the same question. Whichever direction has the stronger
#     count wins the slot, so the stored tone is the reading of the evidence
#     rather than whichever rule happened to be evaluated last.
#
# The lexicon is deliberately small and generic: profanity and plain insults,
# with no identity slur in it. It is auditable on sight, it is not a moderation
# classifier, and a message that matches it still only bumps a counter.
_RELATIONSHIP_RULES: dict[str, tuple[re.Pattern, str]] = {
    "hostile": (
        re.compile(
            r"(?:ک[صس]کش|ک[صس]خل|خارک[صس]ه|خارک[سص]ده"
            r"|حر[و]?م[ه]?زاد[ه]?"
            r"|مادرجنده|مادرقحبه|جنده"
            r"|کونی|دیوث"
            r"|گایید|گاییدم|گاییدن|گاییدی"
            r"|بی[\s\u200c]*ادب|بی[\s\u200c]*شعور"
            r"|احمق|خنگ|کودن|نفهم|نادان|ابله"
            r"|آشغال|اشغال|پدرسگ|پدر[\s\u200c]*سگ"
            r"|خفه[\s\u200c]*شو|دهن[\s\u200c]*بست)"
            r"|(?:\bf+u+c+k\b|\bshit\b|\bidiot\b|\bstupid\b)",
            re.IGNORECASE | re.UNICODE,
        ),
        "hostile",
    ),
    "friendly": (
        re.compile(
            r"(?:ممنون|مرسی|مرسیم|ممنونم"
            r"|دمت[\s\u200c]*گرم|دستت?[\s\u200c]*درد[\s\u200c]*نکنه"
            r"|دستت[\s\u200c]*طلا|خدا[\s\u200c]*قوت"
            r"|لطف[\s\u200c]*کردی|لطف[\s\u200c]*داری|لطفت"
            r"|عالی[\s\u200c]*بود|عالیه|احسنت|آفرین|افرین|ایول"
            r"|مخلص|مرام[\s\u200c]*داری"
            r"|مهربون|مهربان|خوبی[\s\u200c]*داری"
            r"|دوستت[\s\u200c]*دارم|دوسِ?ت[\s\u200c]*دارم)"
            r"|(?:\bthanks?\b|\bthank you\b|\bthx\b)",
            re.IGNORECASE | re.UNICODE,
        ),
        "friendly",
    ),
}


def _observe_relationship(
    chat_id: int, user_id: int, text: str, *, directed: bool
) -> list[str]:
    """Count how one *directed* message treats Nexus, and promote the tone.

    Returns the slots it wrote — ``["relationship.tone"]`` at most once. A
    message that is not aimed at the assistant is not evidence about the
    assistant and is never counted, which is what keeps "hostile" meaning
    "hostile *to you*" rather than "used a rude word in this room".

    The direction with the larger count owns the slot. A tie goes to hostile,
    because the cost of wrongly granting the rude register is one sharp reply
    from an assistant that the persona still bounds, while the cost of wrongly
    withholding it is nothing at all.
    """
    if not config.NEXUS_RELATIONSHIP_ENABLED or not directed:
        return []
    raw = str(text or "")
    if len(raw.strip()) < 2:
        return []
    threshold = max(1, int(config.NEXUS_RELATIONSHIP_THRESHOLD))
    crossed: dict[str, int] = {}
    for signal, (pattern, value) in _RELATIONSHIP_RULES.items():
        if not pattern.search(raw):
            continue
        try:
            count = db.signal_bump(int(chat_id), int(user_id), signal)
        except Exception:  # noqa: BLE001 - a counter is never worth a handler
            log.exception("could not count a relationship signal")
            continue
        if count >= threshold:
            crossed[value] = count
    if not crossed:
        return []
    # Hostile wins a tie: see the docstring.
    value = max(crossed, key=lambda tone: (crossed[tone], tone == "hostile"))
    try:
        rows = db.memory_for(int(chat_id), int(user_id), limit=0)
    except Exception:  # noqa: BLE001
        log.exception("could not read a person's memory before promoting")
        return []
    for row in rows:
        if str(row.get("key") or "") == "relationship.tone":
            if str(row.get("value") or "") == value:
                return []
            break
    try:
        _store(
            int(chat_id),
            int(user_id),
            "relationship.tone",
            value=value,
            source=SOURCE_AUTO,
            confidence=0.6,
        )
    except Exception:  # noqa: BLE001
        log.exception("could not promote a relationship tone")
        return []
    return ["relationship.tone"]


def relationship(chat_id: int, user_id: int) -> str:
    """How this person has treated Nexus, stated by the server, or ``""``.

    The behavioural half of the persona's rudeness rule. The persona may answer
    rudeness in kind *only* when there is a real history, and this is where that
    history is stated — as the server's count, never as the model's guess and
    never as something the person said about themselves.

    It is deliberately a *statement about behaviour* and not a transcript: the
    counts are what the server counted, and no message is quoted. It renders
    nothing when the switch is off, when no tone has been promoted, or on any
    failure — an absent line is the warm default, which is the safe direction.

    Bounded by ``NEXUS_RELATIONSHIP_CHARS``; never raises.
    """
    if not config.NEXUS_RELATIONSHIP_ENABLED:
        return ""
    if not chat_id or not user_id:
        return ""
    try:
        rows = db.memory_for(int(chat_id), int(user_id), limit=0)
    except Exception:  # noqa: BLE001 - context is never worth a failed answer
        log.exception("could not read a person's relationship")
        return ""
    tone = ""
    for row in rows:
        if str(row.get("key") or "") == "relationship.tone":
            tone = str(row.get("value") or "").strip()
            break
    if tone not in ("hostile", "friendly"):
        return ""
    try:
        counts = db.signal_for(int(chat_id), int(user_id))
    except Exception:  # noqa: BLE001
        log.exception("could not read a person's signal counts")
        counts = {}
    hostile = int(counts.get("hostile") or 0)
    friendly = int(counts.get("friendly") or 0)
    if tone == "hostile":
        body = (
            "── How this person has treated you (server-counted) ──\n"
            "They have been hostile with you before. You may answer their "
            "rudeness in kind, never harder, and only while it lasts — the "
            "moment they are friendly again, be friendly. "
            f"({hostile} hostile against {friendly} friendly directed "
            "messages.)\n"
        )
    else:
        body = (
            "── How this person has treated you (server-counted) ──\n"
            "They have been friendly with you. Stay warm with them. "
            f"({friendly} friendly against {hostile} hostile directed "
            "messages.)\n"
        )
    cap = int(config.NEXUS_RELATIONSHIP_CHARS)
    if cap > 0 and len(body) > cap:
        body = _clip(body, cap)
    return body


def _observe_signals(chat_id: int, user_id: int, text: str) -> list[str]:
    """Count the behavioural signals in one message, promoting past threshold.

    At most one bump per signal per message, so a message full of emoji is one
    observation rather than ten. Once a signal is over the threshold the memory
    is written *once*: the person's existing keys are read (a bounded read, at
    most the ceiling of rows) and a slot already present is left alone, so a
    playful person does not rewrite the same row on every message. The counters
    themselves decay on the retention sweep, which is the "demonstrate it again
    or lose the label" rule.
    """
    raw = str(text or "")
    if len(raw.strip()) < 2:
        return []
    threshold = max(1, int(config.NEXUS_MEMORY_SIGNAL_THRESHOLD))
    promoted: list[str] = []
    existing: set[str] | None = None
    for signal, (pattern, slot, value) in _SIGNAL_RULES.items():
        if not pattern.search(raw):
            continue
        try:
            count = db.signal_bump(int(chat_id), int(user_id), signal)
        except Exception:  # noqa: BLE001 - a counter is never worth a handler
            log.exception("could not count a memory signal")
            continue
        if count < threshold:
            continue
        if existing is None:
            try:
                existing = {str(row.get("key") or "") for row in db.memory_for(
                    int(chat_id), int(user_id)
                )}
            except Exception:  # noqa: BLE001
                log.exception("could not read a person's memory before promoting")
                existing = set()
        if slot in existing:
            continue
        try:
            _store(
                int(chat_id),
                int(user_id),
                slot,
                value=value,
                source=SOURCE_AUTO,
                confidence=0.6,
            )
        except Exception:  # noqa: BLE001
            log.exception("could not promote a memory signal")
            continue
        existing.add(slot)
        promoted.append(slot)
    return promoted


def _observe_deterministic(
    chat_id: int, user_id: int, text: str, *, directed: bool = False
) -> list[str]:
    """The free half of the automatic path: statements, then signals.

    ``directed`` is passed straight through to ``_observe_relationship`` and
    changes nothing else — the stated-fact and style rules are about the person
    whoever they were addressing, while "how they treat you" is only meaningful
    on a message aimed at the assistant.
    """
    stored: list[str] = []
    for candidate in automatic(text):
        try:
            _store(
                int(chat_id),
                int(user_id),
                candidate["slot"],
                value=candidate["value"],
                source=candidate["source"],
                confidence=candidate["confidence"],
            )
        except Exception:  # noqa: BLE001
            log.exception("could not store an automatic memory")
            continue
        stored.append(candidate["slot"])
    stored.extend(_observe_signals(int(chat_id), int(user_id), text))
    stored.extend(
        _observe_relationship(
            int(chat_id), int(user_id), text, directed=bool(directed)
        )
    )
    return stored


# The cheap pre-filter for the model seam: first-person language, and none of
# the refusals the deterministic gate already applies. This is what makes the
# gated percentage small — an ordinary remark about the weather carries no
# self-reference and never reaches a provider.
_SELF_RE = re.compile(
    r"(?:^|\s)(?:من|منم|منو|خودم|I|I'?m|I'?ve|I'?d|my|myself|we|our)(?:\s|$)",
    re.IGNORECASE | re.UNICODE,
)


def _looks_like_self_info(text: str) -> bool:
    """Whether a message could hold durable self-information at all.

    Deliberately permissive in one direction and strict in the other: it says
    "yes" for anything that speaks in the first person about a subject, and
    "no" for a transient state, a question, or a message that opens on somebody
    else. It is a gate, not an extractor — the extractor is the isolated model,
    and the server validates whatever it returns.
    """
    raw = str(text or "").strip()
    if len(raw) < 4:
        return False
    if _TRANSIENT_RE.search(raw) or _QUESTION_TAIL.search(raw):
        return False
    if _THIRD_PARTY_RE.match(raw):
        return False
    return bool(_SELF_RE.search(raw))


async def _observe_model(
    chat_id: int, user_id: int, text: str, *, already: bool
) -> list[str]:
    """The gated, isolated model half. Off unless an operator turns it on.

    Three things must all be true before a provider is touched: the switch is
    on, the deterministic layer found nothing (so the model never re-derives
    what a rule already knows), and the message looks like self-information.
    The workload is its own — its own credential slots, budget, breaker and
    counters — so a memory backlog can never spend the request somebody is
    waiting on an answer to. Whatever comes back is untrusted and is put
    through ``validate_candidate`` before it can reach the table.
    """
    if already or not config.NEXUS_MEMORY_EXTRACT_MODEL:
        return []
    if not _looks_like_self_info(text):
        return []
    from . import memory_extract  # local: the seam is optional and isolated

    if not memory_extract.enabled():
        return []
    candidates = await memory_extract.candidates(text)
    stored: list[str] = []
    for candidate in candidates or []:
        valid = validate_candidate(candidate)
        if not valid:
            continue
        try:
            _store(
                int(chat_id),
                int(user_id),
                valid["slot"],
                value=valid["value"],
                source=valid["source"],
                confidence=valid["confidence"],
            )
        except Exception:  # noqa: BLE001
            log.exception("could not store a model memory")
            continue
        stored.append(valid["slot"])
    return stored


async def observe(
    user, chat_id: int, text: str, *, directed: bool = False
) -> list[str]:
    """Learn what one ordinary message says about its author. Never raises.

    The single automatic entry point, and it is deliberately **not awaited on
    the answer path**: ``app/main.py`` schedules it as a background task, so a
    slow provider, a locked database or a broken rule can never delay the reply
    somebody is waiting for. The explicit clause is handled first, through the
    same ``remember`` the first version exposed, because what a person asked to
    be kept is the strongest signal there is.

    ``directed`` says whether the message was aimed at Nexus, and it is used by
    exactly one rule — the relationship counter — so "they cursed at you" is
    never inferred from a message that was aimed at somebody else. The caller
    already computed it (``main._nexus_directed``); it is passed rather than
    re-derived so the two can never disagree.

    Every stage is wrapped, so the worst case is that a memory is not learned —
    never that the handler fails. It returns the slots it stored, for tests and
    for the benchmark; a caller on the hot path ignores the value.
    """
    if not config.NEXUS_MEMORY_ENABLED or not config.NEXUS_MEMORY_AUTO_ENABLED:
        return []
    user_id = int(_get(user, "id", 0) or 0)
    if not user_id or not chat_id or _get(user, "is_bot", False):
        return []
    stored: list[str] = []
    try:
        remember(user, chat_id, text)
    except Exception:  # noqa: BLE001 - the explicit path is wrapped too
        log.exception("explicit memory failed")
    try:
        stored.extend(
            _observe_deterministic(
                int(chat_id), user_id, text, directed=bool(directed)
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("deterministic memory extraction failed")
    try:
        stored.extend(
            await _observe_model(int(chat_id), user_id, text, already=bool(stored))
        )
    except Exception:  # noqa: BLE001
        log.exception("model memory extraction failed")
    if stored:
        _maybe_prune()
    return stored
