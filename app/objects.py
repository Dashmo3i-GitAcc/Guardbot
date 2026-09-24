"""What does the request act on?

The gap this module exists for
-------------------------------
Three readers already answer three neighbouring questions:

* ``app/requests.py`` — which way the directive points («بنش کن» asks, «بنش نکن»
  forbids);
* ``app/entities.py`` — what a demonstrative points at when it is **not** a person;
* ``app/referents.py`` — which *people* a pronoun may mean.

None of them says what the **directive** acts on, and that is the join that
matters in a moderation room:

    «فایل رو پاک کن»   the server says: instruction, the directive «پاک»
                       …and, on another line: things — media

    «بنش کن»           the server says: instruction, the directive «بنش»
                       …and, on another line: who «بنش» may mean — the room

The model has to join those two lines itself, and the join is exactly where the
worst mistake available here happens: acting on a **person** when the message was
about a file. The measured baseline for this stage was that mistake, counted — on
a window holding both a person and a media row, **4 of 10** requests whose object
was a thing still handed the model a person as the target. Every one of the four
was the object clitic on a content verb: «فایل رو پاکش کن», «پاکش کن»,
«حذفش کن».

The reading
-----------
One question, one closed answer: **what class of thing does the directive act
on**, and how does the server know.

    person    the request acts on a person
    media     …on a media message (a file, a photo, a voice note, …)
    link      …on a link
    message   …on a text message
    thing     …on a thing whose kind the message does not state
    ""        no reading — abstention

and the evidence, one of ``named`` (the message names it: «این فایل رو پاک کن»),
``pointed`` (the message points at a thing the room holds: «پاکش کن»), or
``verb`` (only the verb says which side it acts on: «پاک کن»).

**The verb decides person-versus-thing, and that is the load-bearing rule.** The
surface shape cannot: «پاکش کن» and «بنش کن» are the same shape — a directive
carrying the object clitic «ـش» — and one acts on a file while the other acts on a
person. So the split is stated **once**, in ``app/discourse.py`` beside the
lexicon it splits, and borrowed here; a test asserts its two lists cover that
lexicon and do not overlap, so a word added there fails the test until somebody
decides its side.

What this module is, and what it is not
---------------------------------------
It is **evidence**, in exactly the sense the readers above are. It reports what it
read with the reason attached. It is not a decision and it is not a gate:

* nothing branches on it — not a reply, not an action, not a schedule;
* it cannot authorise anything; ``app/admin_service.py`` still re-authorises every
  request from the actor's Telegram id;
* it holds no path to a permission: no ``db``, no ``config``, no pool, no ``rbac``.
  It is pure at import time and a test asserts the import set.

Why a verb in neither list abstains
-----------------------------------
An operator may add a word to the moderation lexicon through
``NEXUS_EXTRA_ACTION_WORDS``, and a future release may add one to the built-in
list. Neither arrives with a side attached, and guessing a side is exactly the
mistake this module exists to prevent — a guessed **person** for a message about a
file is the dangerous direction. So an unclassified verb yields no object reading
at all. The abstention is silent, and the transcript still shows the model the
words.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ── The vocabulary ────────────────────────────────────────────────────────
CLASS_PERSON = "person"
CLASS_MEDIA = "media"
CLASS_LINK = "link"
CLASS_MESSAGE = "message"
CLASS_THING = "thing"

# The specific kinds, in the order a tie is broken: a message that could point at
# both a link and a photograph is about the photograph, because that is what a
# room posts and then asks about.
CLASSES = (CLASS_PERSON, CLASS_MEDIA, CLASS_LINK, CLASS_MESSAGE, CLASS_THING)
_KIND_ORDER = (CLASS_MEDIA, CLASS_LINK, CLASS_MESSAGE)

# How the server knows. ``verb`` is the weakest — it says which side, not which
# thing — and it is labelled as such so the model can weigh it.
SOURCE_NAMED = "named"
SOURCE_POINTED = "pointed"
SOURCE_VERB = "verb"

SOURCES = (SOURCE_NAMED, SOURCE_POINTED, SOURCE_VERB)

# ── The one judgment this module leans on ─────────────────────────────────
# Which side a directive verb falls on is stated in ``app/discourse.py``, beside
# the lexicon it splits, because two readers need it — this one, and
# ``referents``, which must stop reading the object clitic on a *content* verb as
# a person. The split is borrowed rather than repeated: a second copy would be a
# second answer, and the two would drift the first time either changed.
#
# A verb in neither list answers nothing, and this reader then abstains. That is
# deliberate: an operator may add a word to the moderation lexicon without a side
# attached, and a guessed *person* for a message about a file is the worst
# direction available here.

# The Arabic block's punctuation — «؟» «،» «؛» — lives *inside* ``\u0600-\u06ff``,
# so it has to be excluded by name or it stays glued to the word before it. The
# same copy every reader carries, pinned together by a test.
_TOKEN_SPLIT = re.compile(
    r"[^\w\u0600-\u06ff]|[\u060c\u061b\u061e\u061f\u066a\u066b\u066c\u066d\u06d4]|_"
)


def _fold(text: str | None) -> str:
    """The shared fold, reused rather than copied, late and guarded."""
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a read fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(folded.split())


def _tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def _side(directive: str) -> str:
    """What the directive acts on, borrowed from the lexicon that owns it."""
    try:
        from . import discourse

        return discourse.acts_on(directive)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return ""


@dataclass(frozen=True)
class Object:
    """What the directive acts on, and how the server knows.

    ``surface`` is the word the message used — «فایل», «اینو» — quoted rather
    than mapped, because the server reporting *"the message used the word «فایل»"*
    is a fact, while the server reporting *"the message is about a file"* is a
    reading it must be able to defend word by word.
    """

    kind: str = ""
    surface: str = ""
    source: str = ""
    why: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.kind)

    def is_person(self) -> bool:
        return self.kind == CLASS_PERSON


def read_object(
    text: str | None, messages=None, anchor: dict | None = None
) -> Object:
    """What the anchor's directive acts on.

    Returns an empty ``Object`` when the message uses no directive, when the
    verb's side is not known, or when nothing in the message says which side.
    Abstention is the honest reading: a guessed *person* for a message about a
    file is the mistake this module exists to prevent.
    """
    tokens = _tokens(text)
    if not tokens:
        return Object()

    try:
        from . import discourse

        found = discourse.directives(text)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return Object()
    if not found:
        return Object()

    directive = found[0][1]
    why: list[str] = [f"the directive «{directive}»"]

    # The strongest evidence, and it is in the message's own words: a noun that
    # names a thing. It wins over the verb because it is what the room actually
    # said; the verb only says which side an argument has.
    kind, surface = _named(text)
    if kind:
        why.append(f"the message names the thing «{surface}»")
        return Object(kind=kind, surface=surface, source=SOURCE_NAMED, why=tuple(why))

    # The verb's side. This is the rule that separates «پاکش کن» from «بنش کن» —
    # the same shape, opposite sides.
    side = _side(directive)
    if side == "person":
        pointed = _pointing_surface(text, directive)
        why.append(f"«{directive}» is a verb that acts on a member")
        if pointed:
            why.append(f"and the message points at them with «{pointed}»")
        return Object(
            kind=CLASS_PERSON, surface=pointed, source=SOURCE_VERB, why=tuple(why)
        )

    if side == "thing":
        why.append(f"«{directive}» is a verb that acts on a thing, not a member")
        pointed = _pointed_kind(text, messages, anchor)
        if pointed:
            why.append(f"and the message points at {pointed}, which the room holds")
            return Object(
                kind=pointed, surface="", source=SOURCE_POINTED, why=tuple(why)
            )
        return Object(kind=CLASS_THING, surface="", source=SOURCE_VERB, why=tuple(why))

    # The verb is in neither list — a generic imperative («بکن»), or a word an
    # operator added. Guessing a side is the dangerous direction, so: nothing.
    return Object()


def _named(text: str | None) -> tuple[str, str]:
    """The thing the message names, late and guarded."""
    try:
        from . import entities

        return entities.named_kind(text)
    except Exception:  # noqa: BLE001
        return ("", "")


def _pointing_surface(text: str | None, directive: str) -> str:
    """The expression that points at the person, or ``""``.

    A clitic attached to the directive itself («بنش») is the object marker inside
    the verb, so it is reported as nothing rather than as a second quotation of
    the same word.
    """
    try:
        from . import referents

        expression = referents.find_expression(text)
    except Exception:  # noqa: BLE001
        return ""
    surface = str(getattr(expression, "surface", "") or "")
    return "" if surface == directive else surface


def _pointed_kind(text: str | None, messages, anchor: dict | None) -> str:
    """The kind of thing the message points at, if it points at one.

    Only consulted when the message actually carries a referring expression: a
    bare «پاک کن» points at nothing, and the room's newest photograph is not its
    object just because the room has one.
    """
    try:
        from . import entities, referents
    except Exception:  # noqa: BLE001
        return ""
    try:
        if not referents.find_expression(text):
            return ""
        state = entities.read_entities(messages or (), anchor or {})
    except Exception:  # noqa: BLE001
        return ""
    for kind in _KIND_ORDER:
        if state.of_kind(kind):
            return kind
    return ""


# ── Rendering ─────────────────────────────────────────────────────────────
def render(state: Object) -> str:
    """The object line, or nothing. Evidence, and labelled as such.

    Deliberately **one line**, and deliberately rendered by the same source as the
    act and the direction: "instruction, the directive «پاک»" without "acts on a
    thing" is the half-truth this module exists for, so a budget must never be
    able to keep one and drop the other.
    """
    if not state:
        return ""
    if state.kind == CLASS_PERSON:
        pointed = f" («{state.surface}»)" if state.surface else ""
        return (
            f"The request acts on a **person**{pointed}, not on a thing — read the "
            "person from the transcript, not from this line.\n"
        )
    label = {
        CLASS_MEDIA: "a media message",
        CLASS_LINK: "a link",
        CLASS_MESSAGE: "a message",
        CLASS_THING: "a thing",
    }[state.kind]
    if state.kind == CLASS_THING:
        # The label for CLASS_THING is literally "a thing", so interpolating it
        # here read "acts on a thing — a thing, not a person": the same word
        # twice. Spelled out instead, so the one line stays one line and says it
        # once. The meaning and the length are otherwise unchanged.
        return (
            "The request acts on a thing, not a person. The message does "
            "not say which thing; the transcript does.\n"
        )
    # The room-held branches used to end "Do not read it as aimed at anybody in
    # the room" — an order, and a false one on a shape the corpus could not see
    # until it carried it: a request that acts on a thing *and* carries an
    # explicit source for a person. A reply edge, a stated id and a name are
    # facts, and ``app/referents.py`` keeps them for exactly this case, because
    # they identify who the thing belongs to — so the block printed beside this
    # one names that person, often as ``confident``. Two blocks, one prompt,
    # opposite claims.
    #
    # The block that knows the object side does not know the people side, so it
    # states its own half and stops. That is the same rule the entity block's
    # closing line was corrected to (see ``_entity_gives_an_order`` in the
    # harness): the sentence is evidence framing, not an instruction.
    return (
        f"The request acts on {label} — a thing, not a person. The thing is not a "
        "member of the room.\n"
    )


__all__ = [
    "CLASSES",
    "CLASS_LINK",
    "CLASS_MEDIA",
    "CLASS_MESSAGE",
    "CLASS_PERSON",
    "CLASS_THING",
    "Object",
    "SOURCES",
    "SOURCE_NAMED",
    "SOURCE_POINTED",
    "SOURCE_VERB",
    "read_object",
    "render",
]
