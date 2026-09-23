"""What a message is *doing*, and what the room has left unanswered.

The problem this module exists for
----------------------------------
The awareness pass is handed a transcript and asked to understand it. What it is
*not* handed is the cheap half of that understanding, which the server can read
off the text without a model and without a key:

* whether a message is **asking**, **instructing**, **correcting**, **greeting**
  or **reporting** — the difference between «چقدره؟» and «بنش کن» is the
  difference between a question and an order, and it is legible from the words;
* which questions in the window **nobody has answered** — the one piece of room
  state a group most reliably loses track of, and the one a server can compute
  exactly, because the reply edge is a stored column.

Both are things the brief lists as room state: *"which questions remain
unanswered"*, and the shape of what is being said.

What this module is, and what it is not
---------------------------------------
It is **evidence**, in the same sense ``app/referents.py`` and
``app/addressing.py`` are evidence. It reads text and rows and reports what it
found, with the reason. It is not a decision and not a gate:

* it cannot make a message relevant — relevance is the model's;
* it cannot make anything happen — only ``app/admin_service.py`` authorises;
* it cannot decide whether Nexus speaks, or what it says.

The existing invariant is unchanged: deterministic gates are for infrastructure
and security only, and relevance, action and speech are the model's exclusively.
This module produces a sentence for the prompt, not a branch in the code.

The vocabulary abstains
-----------------------
``read_act`` reports ``unknown`` when nothing it can defend fires. That is the
design rather than a gap: a classifier that always guesses would put a wrong act
in the prompt on every ordinary message, and the prompt is where a wrong label
does its damage. The benchmark therefore scores **precision** on the acts it
claims and **coverage** — how often it claims at all — rather than accuracy
alone, because on a corpus where most instructions are instructions, accuracy is
a number a constant would also get.

Why the precedence is what it is
--------------------------------
``correction > report > instruction > social > question``. A correction is a
statement *about the conversation*, so «نه منظورم مهدی بود، اینو بن کن» is a
correction that happens to carry an instruction; a report is a quotation, so
«نکسوس گفت اینو بن کن» is somebody repeating an order rather than giving one,
and reading it as an instruction is exactly the false positive ``addressing``
already guards against; and a greeting outranks the question mark, so «سلام بچه
ها چطوری» is a greeting rather than an interrogation.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ── Folding ───────────────────────────────────────────────────────────────
# The shared fold is ``people.normalize``, reused rather than copied for the
# reason ``addressing`` and ``referents`` reuse it: it already handles the
# Arabic-versus-Persian letters, the diacritics, the zero-width joiner and the
# digit sets, and a second implementation is a second place for the two to
# disagree. The import is late and guarded so this module stays importable on
# its own — a fold must never be the reason a reading fails.
def _fold(text: str) -> str:
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a read fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
        folded = " ".join(folded.split())
    return folded


# ── The acts ──────────────────────────────────────────────────────────────
# The closed vocabulary. ``unknown`` is the abstention and is deliberately not
# in ``ACTS``: it is the absence of a reading, not one of them.
ACT_QUESTION = "question"
ACT_INSTRUCTION = "instruction"
ACT_CORRECTION = "correction"
ACT_SOCIAL = "social"
ACT_REPORT = "report"
ACT_UNKNOWN = "unknown"

ACTS = (ACT_QUESTION, ACT_INSTRUCTION, ACT_CORRECTION, ACT_SOCIAL, ACT_REPORT)

# Precedence, strongest first. The reasoning is in the module docstring; the
# order is here so a reader can see the whole judgement in one place.
_PRECEDENCE = (ACT_CORRECTION, ACT_REPORT, ACT_INSTRUCTION, ACT_SOCIAL, ACT_QUESTION)

# The question words, plus the two Latin ones a mixed room actually uses. The
# question *mark* is checked separately, because it is the strongest signal and
# does not depend on the vocabulary being complete.
_QUESTION_WORDS = frozenset(
    {
        "چی", "چیه", "چیست", "چیا", "چیایی", "چرا", "کجا", "کی", "کیا",
        "چند", "چنده", "چقدر", "چقد", "چندتا", "چطور", "چجوری", "چگونه",
        "ایا", "آیا", "مگه", "مگر", "کدوم", "کدام", "کدومش", "کدومیک",
        "what", "why", "how", "when", "where", "who", "which",
    }
)

# The moderation verbs are borrowed, not copied, exactly as ``referents`` borrows
# them: the list that knows which words those are already exists, and a second
# copy would drift the first time either changed.
def _action_words() -> frozenset[str]:
    try:
        from . import addressing

        return frozenset(addressing.ACTION_WORDS)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return frozenset()


def _temporal_nouns() -> frozenset[str]:
    """The time nouns that turn a question word into a duration.

    «چند» asks "how many"; «چند دقیقه پیش» says "a few minutes ago". The word is
    the same and the reading is opposite, and what separates them is the noun
    after it — which is why this borrows ``app/temporal.py``'s ``TEMPORAL_NOUNS``
    rather than keeping a list of durations that would drift. Late and guarded,
    as every cross-module reach here is: a missing list degrades to the reading
    this module gave before, never to an import error.
    """
    try:
        from . import temporal

        return frozenset(temporal.TEMPORAL_NOUNS)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return frozenset()


def _question_hits(tokens) -> list[str]:
    """The question words that are actually asking.

    A question word directly before a time noun is a duration, not a question:
    «چند دقیقه پیش»، «چند ساعت پیش»، «چند وقت پیش». The question *mark* is not
    consulted here — it is the stronger signal and is checked on its own — so a
    sentence that really asks still reads as a question when it carries one.
    """
    nouns = _temporal_nouns()
    hits: list[str] = []
    for index, token in enumerate(tokens):
        if token not in _QUESTION_WORDS and _bare(token) not in _QUESTION_WORDS:
            continue
        if index + 1 < len(tokens) and tokens[index + 1] in nouns:
            continue
        hits.append(token)
    return hits


# The imperative endings a Persian directive ends with — kept only as
# documentation of what the explicit lexicons already cover, and deliberately
# **not** used as a suffix rule.
#
# It was one, and it was wrong: «نمیکن» ends in «کن» and read as an instruction,
# and so did every other word with the syllable at the end. The moderation
# lexicon already lists the clitic forms a group actually types («بنش»،
# «ساکتش»، «محدودش»), and ``_bare`` strips one clitic before the lookup, so the
# suffix rule bought nothing that the explicit lists did not already have and
# cost a false instruction on ordinary speech.
_IMPERATIVE_ENDINGS = ("کن", "کنید", "بده", "بزن", "بذار", "بگذار", "بس")

# The imperatives that are not built on «کن» and are not moderation verbs —
# «ببین» asks somebody to look, «بفرست» to send, «بگو» to say. A directive is
# still a directive when its verb is ordinary.
#
# Only forms that are *unambiguously* imperatives are listed. «درست», «نگاه»,
# «ارسال», «پیگیری» and «چک» were tried and removed: each is also an ordinary
# noun, so «آیا درسته» read as an instruction and «درستش کن» is already caught by
# the «کن» that follows it. A lexicon that reaches for the noun form buys
# coverage with a false instruction on every «درسته؟» in the room.
_IMPERATIVES = frozenset(
    {
        "ببین", "ببینید", "ببینن", "بگو", "بگید", "بگین", "بگن", "بفرست",
        "بفرستید", "بخون", "بخونید", "برو", "برید", "بیا", "بیاید", "بیاین",
        "کمک", "بگرد",
        # The bare «کن» is the imperative itself, and it is the token that
        # carries an ordinary directive whose verb is in no lexicon —
        # «بررسی کن», «چک کن», «درستش کن». It is listed as an exact token rather
        # than as a suffix rule, so «میکنم» and «نمیکن» do not match it.
        "کن", "کنید", "کنن", "بکن", "بکنید", "بکنن",
        # The other bare imperatives a directive is built on: «ادامه بده»,
        # «پاکش کن و بزن», «بذار ببینم». Each is unambiguous as a whole token —
        # the noun readings of «بده» and «بزن» are not standalone words a room
        # types on their own.
        "بده", "بدید", "بدهید", "بزن", "بزنید", "بذار", "بگذار", "بگذارید",
        "بس", "بسش",
        "see", "look", "check", "send", "tell", "show", "give", "stop",
    }
)

# A correction is a statement *about the conversation*, so its markers are
# words that refer to what was said rather than to what is being asked for. The
# list is deliberately short: «نه» on its own is a disagreement, not a
# correction, and treating it as one would fire on every contradiction in the
# room.
_CORRECTION_WORDS = frozenset(
    {
        "منظورم", "منظورماین", "منظورماینکه", "اشتباه", "اشتباهه", "غلط",
        "نگفتم", "نمیگم", "اصلاح", "تصحیح", "نهبابا", "نهنه", "ببخشیداشتباه",
        "correction", "icorrectthat", "imeant", "sorryimeant",
    }
)

# The openers that turn a first-person recollection into a correction. «نه گفتم
# مهدی نه سارا» is not a report of what somebody said — it is fixing what the
# speaker themselves said a moment ago, and the word doing the fixing is the
# «نه» at the front. The rule needs both halves: a first-person reporting verb
# *and* one of these, so «قبلاً گفتم که...» stays a reminder and a bare «نه»
# stays a disagreement.
_CORRECTION_OPENERS = frozenset({"نه", "نچ", "نخیر", "نا", "no", "nope"})

# The greetings and thanks. «سلام» is the one a room actually opens with, and
# the rest are here so a wall of «ممنون» does not read as an instruction.
_SOCIAL_WORDS = frozenset(
    {
        "سلام", "درود", "سلامعلیکم", "ممنون", "مرسی", "سپاس", "متشکر",
        "ممنونم", "خداحافظ", "خداحفظ", "بای", "بدرود", "شببخیر", "شبخوش",
        "صبحبخیر", "ظهربخیر", "روزبخیر", "تبریک", "مبارک", "خستهنباشی",
        "خستهنباشید", "دستتدرد", "دستتون", "قربونت", "لطفداری",
        "hello", "hi", "thanks", "thank", "bye", "goodbye", "goodmorning",
    }
)

# The verbs that make what follows a *report* rather than an act: somebody
# repeating or recalling what was said. Third-person forms are borrowed from
# ``addressing`` (the same list that makes «نکسوس گفت که…» a mention rather than
# a call); the first-person forms are added here because «قبلاً گفتم که…» is the
# same move from the other side.
_REPORT_FIRST_PERSON = frozenset(
    {
        "گفتم", "گفتمش", "میگفتم", "نوشتم", "پرسیدم", "بگم", "بگمکه",
        "said", "isaid", "told", "itold",
    }
)


def _report_words() -> frozenset[str]:
    try:
        from . import addressing

        return frozenset(addressing._REPORTING_VERBS)
    except Exception:  # noqa: BLE001
        return frozenset()


_CLITICS = ("رو", "را", "ها", "های", "یه", "یی", "ای", "ام", "ات", "اش", "ش", "ه")

# ``_`` is a separator as well as punctuation: a Telegram username is
# ``@nexus_bot``, and ``\w`` would keep the whole thing as one token.
#
# The Arabic block's **punctuation** is named explicitly, and that is a fix
# rather than a flourish. «؟» «،» «؛» live *inside* ``\u0600-\u06ff``, so a
# "split on anything that is not a Persian letter" class keeps them glued to the
# word before them: «این لینک؟» did not contain the word «لینک», «سارا؟» did not
# contain the name «سارا», and «ممنون؟» did not contain the greeting «ممنون».
# A trailing question mark is one of the most common things in this room.
_TOKEN_SPLIT = re.compile(
    r"[^\w\u0600-\u06ff]|[\u060c\u061b\u061e\u061f\u066a\u066b\u066c\u066d\u06d4]|_"
)

_QUESTION_MARKS = ("?", "؟", "؟؟", "??")


def _tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def _bare(token: str) -> str:
    """The token with at most one attached clitic removed."""
    for clitic in _CLITICS:
        if token.endswith(clitic) and len(token) - len(clitic) >= 2:
            return token[: -len(clitic)]
    return token


@dataclass(frozen=True)
class Act:
    """What a message is doing, and the words that say so."""

    kind: str = ACT_UNKNOWN
    why: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.kind != ACT_UNKNOWN


def _hits(tokens: list[str], words: frozenset[str]) -> list[str]:
    """The tokens that are in ``words``, with one clitic stripped first."""
    out: list[str] = []
    for token in tokens:
        if token in words or _bare(token) in words:
            out.append(token)
    return out


def _directives(tokens: list[str]) -> list[tuple[int, str, bool]]:
    """Every token that makes the message an instruction.

    ``(index, surface, is_action)``, in token order. ``is_action`` says the token
    is a moderation verb rather than an ordinary imperative, which is the
    distinction ``read_act`` needs to keep quoting the word that says *what* is
    being asked for («ببین بنش کن» quotes «بنش», not «ببین»).

    The index is carried because the *polarity* of a directive depends on what
    comes after it — «بنش کن» asks for a ban and «بنش نکن» forbids one — and
    ``app/requests.py`` must ask its question about the same word this module
    already found. One lexicon, two readers.
    """
    action = set(_hits(tokens, _action_words()))
    imperative = set(_hits(tokens, _IMPERATIVES))
    return [
        (index, token, token in action)
        for index, token in enumerate(tokens)
        if token in action or token in imperative
    ]


def directives(text: str | None) -> list[tuple[int, str]]:
    """The public form of :func:`_directives`, for the polarity reader.

    ``(index, surface)`` in token order. The polarity reader wants the *first*
    directive by position, because that is the one whose neighbourhood decides
    whether the message asks for the action or forbids it.
    """
    return [(index, token) for index, token, _ in _directives(_tokens(text))]


def read_act(text: str | None) -> Act:
    """What the message is doing, from the closed vocabulary, or nothing.

    Strongest signal first, and the first act with evidence wins. ``unknown``
    means the server has no reading to offer — which is not the same as "the
    message does nothing", only as "the words it uses are not ones this module
    can defend a reading from".
    """
    tokens = _tokens(text)
    if not tokens:
        return Act()

    folded = _fold(text)
    found: dict[str, tuple[str, ...]] = {}

    correction = _hits(tokens, _CORRECTION_WORDS)
    # «نه گفتم مهدی نه سارا» — a first-person recollection opened by «نه» is
    # fixing what the speaker said, not reporting what somebody else did.
    first_person = _hits(tokens, _REPORT_FIRST_PERSON)
    if not correction and first_person and _bare(tokens[0]) in _CORRECTION_OPENERS:
        correction = [tokens[0]]
    if correction:
        found[ACT_CORRECTION] = (
            f"the correction word «{correction[0]}»",
        )

    report = _hits(tokens, _report_words() | _REPORT_FIRST_PERSON)
    if report and ACT_CORRECTION not in found:
        found[ACT_REPORT] = (f"the reporting verb «{report[0]}»",)

    directive = _directives(tokens)
    if directive:
        # A moderation verb is preferred over an ordinary imperative, whichever
        # comes first in the message: «ببین بنش کن» quotes «بنش», the word that
        # says what is being asked for, rather than the «ببین» that only says
        # where to look. This is the rule the inline version had, kept.
        chosen = next((d for d in directive if d[2]), directive[0])
        found[ACT_INSTRUCTION] = (f"the directive «{chosen[1]}»",)

    social = _hits(tokens, _SOCIAL_WORDS)
    if social:
        found[ACT_SOCIAL] = (f"the greeting «{social[0]}»",)

    question = _question_hits(tokens)
    if question:
        found[ACT_QUESTION] = (f"the question word «{question[0]}»",)
    elif any(mark in folded for mark in _QUESTION_MARKS):
        found[ACT_QUESTION] = ("the question mark",)

    for kind in _PRECEDENCE:
        if kind in found:
            return Act(kind, found[kind])
    return Act()


# ── The room's unanswered questions ───────────────────────────────────────
@dataclass(frozen=True)
class Question:
    """A question in the window, and whether anything points at an answer."""

    user_id: int
    name: str
    role: str
    text: str
    at: int
    message_id: int

    def __bool__(self) -> bool:
        return bool(self.text)


def open_questions(messages, *, cap: int = 3) -> tuple[Question, ...]:
    """Questions in the window that no later message answers.

    "Answers" is read narrowly and on purpose: a later message **replying to the
    question's own id**. A room answers questions without using Telegram's reply
    as often as with it, so this over-reports, and the render says so — the
    block is labelled "no reply points at an answer" rather than "unanswered",
    because the second is a claim about meaning and the first is a fact about
    the rows.

    Nexus's own questions are included: a question the assistant asked and
    nobody picked up is exactly the thing a room forgets.
    """
    rows = list(messages or ())
    if not rows:
        return ()

    # Every id any message replies to. The edge is the whole test.
    answered = {
        int(row.get("reply_message_id") or 0) for row in rows
    }
    answered.discard(0)

    out: list[Question] = []
    for row in rows:
        if read_act(row.get("text")).kind != ACT_QUESTION:
            continue
        message_id = int(row.get("message_id") or 0)
        if message_id and message_id in answered:
            continue
        out.append(
            Question(
                user_id=int(row.get("user_id") or 0),
                name=str(row.get("name") or ""),
                role=str(row.get("role") or ""),
                text=str(row.get("text") or ""),
                at=int(row.get("at") or 0),
                message_id=message_id,
            )
        )

    # Newest first, and bounded: a room that asks ten questions does not get ten
    # lines in the prompt.
    out.sort(key=lambda q: (-q.at, -q.message_id))
    return tuple(out[: max(1, int(cap))])


# ── Rendering ─────────────────────────────────────────────────────────────
# How each act reads in a sentence. A dict rather than string concatenation
# because "a instruction" and "a social" are both wrong, and a template that
# has to special-case two of five entries is a template that will be edited
# wrongly later.
_ACT_PHRASE = {
    ACT_QUESTION: "a question",
    ACT_INSTRUCTION: "an instruction",
    ACT_CORRECTION: "a correction",
    ACT_SOCIAL: "social chatter",
    ACT_REPORT: "a report",
}


def render_act(act: Act) -> str:
    """One sentence for the prompt, or nothing when there is no reading."""
    if not act:
        return ""
    return (
        f"\nThe server reads this message as {_ACT_PHRASE.get(act.kind, act.kind)} "
        f"({act.why[0]}).\n"
    )


def render_questions(questions, *, cap: int = 600) -> str:
    """The unanswered-question block, bounded and labelled as evidence."""
    questions = tuple(questions or ())
    if not questions:
        return ""
    lines = [
        "\nQuestions in the room with no reply pointing at an answer "
        "(server-read, not a judgement that nobody answered):"
    ]
    for question in questions:
        who = question.name or str(question.user_id)
        if question.role == "nexus":
            who = "you"
        text = " ".join(question.text.split())
        if len(text) > 160:
            text = text[:159] + "…"
        lines.append(f'- {who}: "{text}"')
    return _clip("\n".join(lines) + "\n", cap)


def _clip(text: str, cap: int) -> str:
    """Bound the block, on a line boundary where there is one."""
    if cap <= 0 or len(text) <= cap:
        return text if cap > 0 else ""
    room = max(1, cap - 1)
    head = text[:room]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + "\n…\n"
