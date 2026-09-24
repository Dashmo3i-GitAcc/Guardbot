"""Does this message ask for the action, or forbid it?

The problem this module exists for
----------------------------------
``app/discourse.py`` reads *what a message is doing* and reports one closed
vocabulary — ``instruction`` among them. But «بنش کن» and «بنش نکن» are both
built on the same directive, and they ask for **opposite things**: one asks for a
ban and the other forbids one. To the act reader they are the same message.

That is the dangerous half-truth this module exists to close. A transcript that
says "this is an instruction, the directive is «بنش»" while the message actually
says *do not ban him* is a transcript that invites the model to ban the person the
room just protected — and a wrong-person moderation action is the worst mistake
available here.

So the server reads three things the act alone cannot say:

* **the directive** — the word that makes it an instruction, quoted rather than
  categorised. The lexicon is ``app/discourse.py``'s, borrowed rather than copied;
  a second list would be a second answer that drifts.
* **the polarity** — whether the message asks for the action or forbids it, and
  which word does the forbidding.
* **the manner** — a bare imperative («بنش کن») or a politeness frame
  («میشه بنش کنی؟»). The same request, at a different social distance.

What this module is, and what it is not
---------------------------------------
It is **evidence**, in exactly the sense ``app/discourse.py``, ``app/referents.py``
and ``app/entities.py`` are. It reads text and reports what it found, with the
reason attached. It is not a decision and it is not a gate:

* nothing branches on it — not a reply, not an action, not a schedule;
* it cannot authorise anything; ``app/admin_service.py`` still re-authorises every
  request from the actor's Telegram id;
* it holds no path to a permission: no ``db``, no ``config``, no pool, no ``rbac``.
  It is pure at import time and a test asserts the import set.

(The name collides with the third-party HTTP library, and that was checked rather
than assumed: the app is a package and every entry point puts the *repository
root* on ``sys.path`` — ``python -m app.main``, ``tests/conftest.py``, the tools —
so ``import requests`` still resolves to the library and this module is only ever
``app.requests``. Nothing in this project imports the library anyway.)

Why the negation rule is scoped, and why the *other* list is deliberately broad
-------------------------------------------------------------------------------
Two negation rules, and they point in opposite directions on purpose.

The one that **claims** something — "this directive is negated" — is scoped
tightly: the prohibitor must be the token *immediately after* the directive. That
is the shape Persian actually uses («بنش نکن», «پاکش نکنید»), and a wider window
would claim a negation the message does not make.

The one that **downgrades** something is deliberately broad. When a negation
appears anywhere else in the message, the reader does not report "affirmative" —
it reports nothing at all, because it cannot tell what the negation scopes.
«این آدم خوب نیست، بنش کن» contains a negation that has nothing to do with the
directive, and an over-broad *detector* there costs only an abstention. That is
the opposite of the suffix rule ``app/discourse.py`` removed, where the broad rule
cost a false instruction: here the error direction is the safe one, so breadth is
the right choice.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ── The vocabulary ────────────────────────────────────────────────────────
POLARITY_AFFIRMATIVE = "affirmative"
POLARITY_NEGATED = "negated"

POLARITIES = (POLARITY_AFFIRMATIVE, POLARITY_NEGATED)

MANNER_COMMAND = "command"
MANNER_REQUEST = "request"

MANNERS = (MANNER_COMMAND, MANNER_REQUEST)

# The prohibitive paradigm — the «نکن» a group puts *after* the directive it is
# cancelling. This is the list that may **claim** a negation, so it holds only
# the forms that are unambiguously the prohibition itself, and the check is
# positional: it must be the very next token.
_PROHIBITORS = frozenset(
    {
        "نکن", "نکنم", "نکنی", "نکنه", "نکنیم", "نکنید", "نکنین", "نکنن",
        "نکنیدش", "نکنش",
        "dont", "donot", "never",
    }
)

# The negators that only ever **downgrade** a reading: when one of these appears
# anywhere else in the message, the reader abstains rather than claiming the
# message asks for the action. Breadth is the point — see the module docstring.
_NEGATORS = frozenset(
    {
        "نه", "نچ", "نخیر", "نا", "نیس", "نبود", "نشد", "نشه", "نباشه",
        "نکن", "نکنم", "نکنی", "نکنه", "نکنیم", "نکنید", "نکنین", "نکنن",
        "نده", "ندید", "ندهید", "ندهین", "ندن",
        "نزن", "نزنید", "نزنین", "نزنن",
        "نذار", "نذارید", "نذارین", "نگذار", "نگذارید", "نگذارین",
        "نگیر", "نگیرید", "نگیرین", "نفرست", "نفرستید", "نفرستین",
        "نگو", "نگید", "نگین", "نپرس", "نپرسید", "نرو", "نرید", "نیا", "نیاید",
        "نخون", "نخونید", "نبین", "نبینید", "نمون", "نمونید",
        "no", "not", "never", "dont", "donot", "without",
    }
)

# The prefixes that make a verb negative. A prefix rule is safe *here* and
# unsafe in a lexicon, for the reason the module docstring gives: «نمی» and «نی»
# only ever downgrade a claim to an abstention, so a false hit costs an
# abstention, where a false hit in the directive lexicon costs a false
# instruction. It covers the long tail — «نمیشه», «نمیخواد», «نمیتونم»,
# «نیست» — that no readable list would keep up with.
_NEGATIVE_PREFIXES = ("نمی", "نی")

# The negative past: «ن» + the past stem — «نکرد», «نگفت», «ندید», «نرفت». A
# bare «ن» is not a negation on its own («نگاه», «نام», «نوع» all start with it),
# so the *stem* is what makes it one, and the rule is a stem list rather than
# forty spelled-out forms.
#
# Like the prefix rule, this only ever **downgrades**: ``_is_negator`` is what
# consults it, never the positional claim, and that is why it can be this broad.
# «نبرد» ("battle") is a false hit and it costs an abstention, not a wrong
# claim — the safe error direction, and the whole reason the list can be long.
# «نیامد»/«نیومد» are absent on purpose: they start with «نی» and the prefix rule
# already has them.
_NEGATIVE_STEMS = (
    "کرد", "رفت", "داشت", "گفت", "خواست", "شد", "بود", "داد", "دید", "زد",
    "خورد", "گرفت", "آمد", "اومد", "فهمید", "دونست", "تونست", "گذاشت",
    "موند", "خوند", "نوشت", "بست", "برد", "آورد", "اورد",
)

# The politeness frames. A request is the same request whether it is a bare
# imperative or wrapped in one of these; the difference is the social distance,
# and it is legible from the words. Kept short and explicit, like every lexicon
# in this package: the ZWNJ-free forms, because the shared fold removes it.
_REQUEST_FRAMES = frozenset(
    {
        "میشه", "میتونی", "میتونید", "میتونین", "میتونن", "ممکنه",
        "ممنونمیشم", "ممنون", "لطفا", "زحمت", "بزحمت",
        "could", "would", "please", "canyou",
    }
)

# English negation goes **before** the verb — «don't ban him», «never ban him» —
# which is the opposite of the Persian prohibitive, and the tokenizer splits the
# apostrophe so «don't» arrives as «don» + «t». A short look-back window over
# these forms is the whole rule.
#
# Only English forms are listed, and that is the load-bearing part. A Persian
# word *before* a directive is not a negator: «نه بنش کن» is "no, ban him", and
# claiming a negation from it would be exactly backwards. A Persian negation that
# the reader cannot scope still downgrades the reading to an abstention through
# ``_NEGATORS``; it never makes it a claim.
_PRE_NEGATORS = frozenset({"don", "dont", "donot", "not", "never", "no"})
_PRE_WINDOW = 2


def _fold(text: str | None) -> str:
    """The shared fold, reused rather than copied, late and guarded.

    ``people.normalize`` already handles the Arabic-versus-Persian letters, the
    diacritics, the zero-width joiner and the digit sets. The import is inside the
    function so importing this module never pulls in ``config``, and it is wrapped
    so a host without the shared fold loses the orthography handling rather than
    the module.
    """
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a read fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(folded.split())


# The Arabic block's punctuation — «؟» «،» «؛» — lives *inside* ``\u0600-\u06ff``,
# so it has to be excluded by name or it stays glued to the word before it. This
# is what the polarity reader is most sensitive to: «میشه بنش نکنی؟» carries the
# prohibitor «نکنی؟», which is not «نکنی», and the message would read as a plain
# request *to* ban.
_TOKEN_SPLIT = re.compile(
    r"[^\w\u0600-\u06ff]|[\u060c\u061b\u061e\u061f\u066a\u066b\u066c\u066d\u06d4]|_"
)


def _tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def _is_negator(token: str) -> bool:
    """Whether a token is a negation, by the broad fail-safe rule.

    Three ways to be one, and all three only ever *downgrade* a reading: a
    listed form, a «نمی»/«نی» prefix, or the negative past («ن» + a known stem).
    """
    if token in _NEGATORS:
        return True
    if any(token.startswith(prefix) for prefix in _NEGATIVE_PREFIXES):
        return True
    return token.startswith("ن") and any(
        token[1:].startswith(stem) for stem in _NEGATIVE_STEMS
    )


@dataclass(frozen=True)
class Request:
    """What the message asks for, and in which direction.

    ``directive`` is the surface word, quoted rather than mapped to an action
    category: the server reporting *"the message used the word «بنش»"* is a fact,
    while the server reporting *"the message asks for a ban"* would be the server
    choosing the action — which is the model's job and ``admin_service``'s to
    authorise.
    """

    directive: str = ""
    index: int = -1
    polarity: str = ""
    negator: str = ""
    manner: str = ""
    why: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.directive)

    def negated(self) -> bool:
        return self.polarity == POLARITY_NEGATED


def _negator_after(tokens: list[str], index: int) -> str:
    """The prohibitor directly after a directive, or ``""``.

    The Persian shape, and the only rule that may *claim* a negation.
    """
    if index + 1 < len(tokens) and tokens[index + 1] in _PROHIBITORS:
        return tokens[index + 1]
    return ""


def _negator_before(tokens: list[str], index: int) -> str:
    """The English negator just before a directive, or ``""``."""
    for distance in range(1, _PRE_WINDOW + 1):
        position = index - distance
        if position >= 0 and tokens[position] in _PRE_NEGATORS:
            return tokens[position]
    return ""


def read_request(text: str | None) -> Request:
    """The directive the message uses, its polarity and its manner.

    Returns an empty ``Request`` when the message uses no directive — the common
    case and the cheap one. ``polarity`` is ``""`` rather than ``affirmative``
    whenever a negation is present that the reader cannot scope: abstention is
    the honest reading, and claiming "affirmative" for a message that forbids the
    action is exactly the mistake this module exists to prevent.

    The reading is about the **first** directive in the message, because that is
    the one whose neighbourhood decides the direction. A message that carries a
    second, negated directive is a message with two directions in it, and a
    one-line summary cannot hold both — so the reader abstains rather than
    summarising the half that points at acting.
    """
    tokens = _tokens(text)
    if not tokens:
        return Request()

    try:
        from . import discourse

        found = discourse.directives(text)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return Request()
    if not found:
        return Request()

    index, directive = found[0]
    why: list[str] = [f"the directive «{directive}»"]

    # The claim: the prohibitor is attached to this directive — the Persian
    # shape «بنش نکن», or the English «don't ban».
    negator = _negator_after(tokens, index) or _negator_before(tokens, index)
    if negator:
        why.append(f"it is negated by «{negator}»")
        return Request(
            directive=directive,
            index=index,
            polarity=POLARITY_NEGATED,
            negator=negator,
            manner=_manner(tokens),
            why=tuple(why),
        )

    # A *later* negated directive: the message forbids something else as well as
    # asking for this. Reporting "affirmative" for the first half would be the
    # dangerous direction — the model would read a message that carries a
    # prohibition as a plain instruction — so the reader abstains instead.
    for other_index, other_word in found[1:]:
        other = _negator_after(tokens, other_index) or _negator_before(
            tokens, other_index
        )
        if other:
            why.append(
                f"the later directive «{other_word}» is negated by «{other}», so "
                f"the message carries two directions"
            )
            return Request(
                directive=directive,
                index=index,
                polarity="",
                manner=_manner(tokens),
                why=tuple(why),
            )

    # The downgrade: a negation the reader cannot scope. It says nothing rather
    # than claiming the message asks for the action.
    elsewhere = [
        token
        for position, token in enumerate(tokens)
        if position != index and _is_negator(token)
    ]
    if elsewhere:
        why.append(
            f"a negation «{elsewhere[0]}» appears elsewhere in the message, and "
            f"the server cannot tell what it scopes"
        )
        return Request(
            directive=directive,
            index=index,
            polarity="",
            manner=_manner(tokens),
            why=tuple(why),
        )

    why.append("nothing in the message negates it")
    return Request(
        directive=directive,
        index=index,
        polarity=POLARITY_AFFIRMATIVE,
        manner=_manner(tokens),
        why=tuple(why),
    )


def _manner(tokens: list[str]) -> str:
    """A bare imperative, or one wrapped in a politeness frame."""
    for token in tokens:
        if token in _REQUEST_FRAMES:
            return MANNER_REQUEST
    return MANNER_COMMAND


# ── Rendering ─────────────────────────────────────────────────────────────
def render(request: Request) -> str:
    """The polarity line, or nothing. Evidence, and labelled as such.

    Deliberately **one line**, and deliberately not a block of its own: the act
    reader's block says "this message is an instruction", and an act that says
    *instruction* while the message forbids the action is the dangerous
    half-truth. The two are rendered by the same source so they cannot be
    separated by a budget.
    """
    if not request:
        return ""
    if request.negated():
        return (
            f"It **negates** «{request.directive}» with «{request.negator}» — the "
            "message asks for this *not* to be done. Do not read it as a request "
            "to act.\n"
        )
    if request.polarity == POLARITY_AFFIRMATIVE:
        if request.manner == MANNER_REQUEST:
            return (
                f"It asks for «{request.directive}» as a polite request, and "
                "nothing in the message negates it.\n"
            )
        return ""
    return (
        f"The message uses the directive «{request.directive}» but also carries a "
        "negation the server could not scope. Do not assume it asks for the "
        "action; the transcript decides.\n"
    )


__all__ = [
    "MANNER_COMMAND",
    "MANNER_REQUEST",
    "MANNERS",
    "POLARITIES",
    "POLARITY_AFFIRMATIVE",
    "POLARITY_NEGATED",
    "Request",
    "read_request",
    "render",
]
