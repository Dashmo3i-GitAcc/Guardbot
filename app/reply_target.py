"""What the user means, and which message Nexus answers.

Two questions that look like one and are not:

* the **semantic target** — who or what the message is about. «این» in «این
  چیه؟» points at the message that was replied to; «این آدم» points at its
  author; «میلاد» names somebody outright.
* the **Telegram reply destination** — which message id the answer should be
  sent as a reply to.

Telegram hands us the first for free and the assistant used to throw it away. A
reply carries the whole parent message in the update (``reply_to_message``) — its
id, its author's id and name, its text or caption, and whether it holds media.
That is authoritative metadata, not a guess, and it is the difference between the
model reading «این چیه» as four characters and reading what «این» *is*. The
transcript marker (``app/awareness.py``) gave the author's name but never the
words, and only when the parent happened to still be inside the room window.

The second is a policy decision, and it is graded rather than boolean. The
default is still the **current** message — a message that refers to nothing else
belongs under itself — but the destination moves to the replied-to message on two
grades of evidence:

* **explicit** — the message asks for the answer to go there: «به این جواب بده»,
  «جواب اینو بده», «با این صحبت کن», «سر به سر این بذار», «میلاد رو جواب بده».
  Somebody who says this is asking Nexus to address somebody else, and an answer
  that is not a reply to them has not done the thing that was asked.
* **reference** — the message does not ask for anything, but it is *about* the
  replied-to message: a deictic («این چیه؟», «این طرف کیه؟», «ببین این چیه»), a
  possessive back-reference («حرفش درسته؟», «عکسش»), a third-person report of
  what the parent said («ببین چی گفته»), or an elliptical agreement («آره
  دقیقاً»). Here the reply edge is the evidence, and attaching the answer to the
  message it is about is the natural thing a person would do.

A reference only moves the destination when nothing competes with it: if the
message mentions a *different* person, or the parent is Nexus's own message, the
reading is ambiguous and the destination stays where it is. A message that merely
replies — with no reference of its own and no directive — is not about the
parent, and nothing moves.

What this module will not do
----------------------------
It never invents an identifier. Every message id it can return comes from one of
exactly three places: Telegram's own ``reply_to_message``, the server's stored
room window, or the message being answered. It never parses a message id out of
the message text, and it never reads one from model output. The model may say who
it believes «این» is; that reading is context, but the ids that reach a Telegram
call are the server's. No model output can widen this set.

It is pure and fail-soft, like every reader beside it: no database handle, no
model, no authority. It takes plain values and returns plain data, so it is
testable without a key, a clock or a bot, and a reader that raises contributes
nothing rather than failing a turn.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── The directive vocabulary ──────────────────────────────────────────────
# A message changes the Telegram reply destination only when it *asks* for the
# answer to go somewhere: a reply verb, or a verb of addressing a person. The
# list is deliberately tight, because widening it is how «جواب ندادی» (you did
# not answer) would start moving the destination. «ببین» (look), «چیه» (what is)
# and «چی میگه» (what does it say) are lookups and are deliberately absent — the
# semantic target still resolves for them, which is the whole point.
_REPLY_STEMS = ("جواب", "پاسخ", "ریپلای", "reply", "answer")
_ADDRESS_PHRASES = (
    "حرف بزن",
    "صحبت کن",
    "گپ بزن",
    "سر به سر",
    "بهش بگو",
    "بگو به",
    "بنویس به",
    "بهش بنویس",
)

# Asking Nexus to bring a *named* person into the thread — «فلانی رو تگ کن»,
# «صداش کن», «منشنش کن». These are their own list rather than additions to the
# list above because they carry a second meaning: the answer belongs under that
# person's message, and the server also mentions them in it. The owner reported
# this class directly — a reply to Nexus saying «فلانی رو تگ کن» used to resolve
# nobody and answer the *asker* — so it is read as a directive, and a directive
# that names a person is what moves the destination.
_TAG_PHRASES = (
    "تگ کن",
    "تگش کن",
    "تگ بزن",
    "منشن کن",
    "منشنش کن",
    "صداش کن",
    "صداش بزن",
    "صدا کن",
    "صدا بزن",
    "خطاب کن",
    "خطابش کن",
    "ادش کن",
    "ادرش کن",
)
# Every phrase that reads as "address a person", used by the directive test.
_DIRECTIVE_PHRASES = _ADDRESS_PHRASES + _TAG_PHRASES

# The "go and engage that one" shapes, as patterns rather than substrings.
#
# These are the phrasings the owner reported by name — «سر اینو گرم کن», «با این
# چت کن» — where a reply to somebody else's message is an instruction to Nexus
# to *address them*. They cannot be a plain substring list because the object
# sits between the verb and its particle («سر اینو گرم کن» beside «سرش گرم
# کن»), and because the bare particle is a different request entirely: «چای گرم
# کن» is tea, «اینو گرم کن» is a plate. Every pattern therefore carries its own
# object, and the caller still requires a pointer or a resolved name before the
# destination moves, which is what keeps «چای گرم کن» from being a directive.
#
# The folded text is what these match against, so «سر اینو» is spelled with the
# fold's own letters and the zero-width joiner has already become a space.
_ENGAGE_PATTERNS = (
    # «سر اینو گرم کن», «سرش رو گرم کن», «سر این گرم کن»
    r"سر\s*(?:این|اون|همون|همین|اینو|اونو|این\s*رو|اون\s*رو|ش|شو|شه)?"
    r"\s*[^\n]{0,14}?گرم\s*کن",
    # «با این چت کن», «باهاش حرف بزن», «با این گپ بزن»
    r"(?:با\s*(?:این|اون|همون|همین)|باهاش|باش)\s*[^\n]{0,12}?"
    r"(?:چت|حرف|صحبت|گپ)\s*(?:بزن|کن)",
    # «حالشو بگیر», «حال اینو بگیر»
    r"حال\s*(?:این|اون|همون|همین|ش|شو|شه)?\s*(?:رو|و)?\s*بگیر",
    # «باهاش شوخی کن», «با این کل کل کن»
    r"(?:با\s*(?:این|اون|همون|همین)|باهاش|باش)\s*[^\n]{0,12}?"
    r"(?:شوخی|کل\s*کل)\s*(?:بزن|کن)",
)

_ENGAGE_RE = None


def _engage():
    """The compiled engage-pattern matcher, built once and lazily."""
    global _ENGAGE_RE
    if _ENGAGE_RE is None:
        import re

        _ENGAGE_RE = re.compile(
            "|".join(_ENGAGE_PATTERNS), re.IGNORECASE | re.UNICODE
        )
    return _ENGAGE_RE

# ── The implicit reference vocabulary ─────────────────────────────────────
# A message can be *about* the replied-to message without asking for anything.
# These are the shapes that refer back, and they are grammatical rather than
# topical: none of them is a word that would make Nexus answer, and every one of
# them needs the reply edge to have a referent at all. A message with no reply
# edge matches none of them and moves nothing.
#
# Possessive back-references: a content noun carrying the third-person clitic
# «ـش». «حرفش» (their word), «پیامش» (their message), «عکسش» (the photo) all
# point at something already mentioned, and in a reply that something is the
# parent. The list is content nouns only — a clitic on any word would match half
# the language.
_POSSESSIVE_STEMS = (
    "حرف", "پیام", "عکس", "جواب", "نظر", "کار", "متن", "صدا", "ویدیو", "فیلم",
    "رفتار", "کلام", "سخن", "قول", "تصویر", "حال",
)

# Third-person reports of what the parent did or said. Only the forms that are
# unambiguously third person are here: «گفته» and «میگه» cannot be first person,
# while «گفت» can («من گفتم»), so «گفت» is deliberately absent. The list is
# matched after the shared fold, which maps «آ»→«ا» and drops the zero-width
# joiner, so these are the folded spellings.
_THIRD_PERSON_VERBS = ("گفته", "میگه", "میگفت", "فرستاده", "کرده", "نوشته", "پرسیده")

# Elliptical agreement: a turn that adds no content of its own and only agrees
# with what came before. In a reply, what came before is the parent — «آره
# دقیقاً» under somebody's photo is agreement *with the photo*. Both the folded
# and the unfolded spelling are listed because the fold is borrowed and a missing
# fold degrades to a plain casefold, which would leave «آره» unfolded.
_AGREEMENT_TOKENS = frozenset(
    {
        "اره", "آره", "بله", "دقیقا", "دقیقاً", "درسته", "موافقم", "همینه",
        "واقعا", "واقعاً", "صحیح", "اوکی", "اکی", "دقیقه", "ارهه",
    }
)
# Words that may sit beside an agreement without adding a subject of their own.
_AGREEMENT_FILLERS = frozenset(
    {
        "خب", "خیلی", "چه", "که", "هم", "و", "بابا", "دمت", "گرم", "جدا",
        "کاملا", "کاملآ", "حرفت", "حرفتو", "باریک", "عالی", "چه",
    }
)

# How many tokens a *deictic* reference may span before it stops counting. The
# listed references are short conversational turns; a long message that happens
# to contain «این» has a subject of its own and the deictic is not enough to
# claim the parent. The other three signals are about the parent by construction
# and carry no such cap.
_REFERENCE_TOKEN_CAP = 12

# Words that are never somebody's name here, so a name lookup does not spend a
# query on them. The fold has already mapped «آ» to «ا» and dropped the
# zero-width joiner, so these are the folded spellings.
_NAME_STOPWORDS = frozenset(
    {
        "این", "اینو", "اینرو", "اینا", "همین", "همینو", "همون", "همونو",
        "اون", "اونو", "اونرو", "قبلی", "قبلیش", "قبلیه",
        "جواب", "پاسخ", "ریپلای", "reply", "answer",
        "حرف", "بزن", "صحبت", "کن", "کنی", "گپ", "سر", "بذار", "بده",
        "بدی", "بدین", "بدم", "بگو", "بنویس", "لطفا", "میشه", "میتونی",
        "به", "با", "رو", "را", "برای", "برا", "از", "در", "که", "و", "یا",
        "یه", "یک", "هم", "پیام", "متن", "کسی", "چی", "چیه", "چرا", "چطور",
        "کجا", "کی", "چند", "اینجا", "اونجا", "بعد", "قبل", "الان",
    }
)

# How many tokens a name lookup may try before giving up. A message with a reply
# directive is short by nature; this only stops a pathological one from turning
# into an unbounded walk of the name memory.
_NAME_TOKEN_CAP = 4

# The excerpt of the replied-to message that reaches the model. Long enough to
# answer «این چیه», short enough that the relationship block stays a block.
_EXCERPT_CHARS = 160

# The default cap on the rendered block, matching the other readers' habit.
RENDER_CAP = 420


def _fold(text: str | None) -> str:
    """The shared fold, borrowed so a name and a directive match the same way.

    Late and guarded, exactly as ``app/referents.py`` guards its own borrow: a
    missing fold degrades to a plain casefold rather than an import error, and a
    fold must never be the reason a target fails to resolve.
    """
    try:
        from . import people

        return people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold is never worth a failure
        return " ".join(str(text or "").casefold().split())


def _message_text(msg) -> str:
    """The text of a message, whether it is a body or a caption."""
    return getattr(msg, "text", None) or getattr(msg, "caption", None) or ""


# ── What Telegram already told us ─────────────────────────────────────────
@dataclass(frozen=True)
class Replied:
    """The message this one replies to, as Telegram delivered it."""

    message_id: int = 0
    user_id: int = 0
    name: str = ""
    text: str = ""
    kind: str = ""
    has_media: bool = False


@dataclass(frozen=True)
class Mention:
    """A person the message mentions, from Telegram's own entities.

    ``user_id`` is set only for a ``text_mention`` entity — Telegram resolved the
    person to an id, which is the strongest identity evidence available. A plain
    ``mention`` carries only the ``@username``, which the server then resolves
    through the room's name memory.
    """

    user_id: int = 0
    username: str = ""
    name: str = ""


@dataclass(frozen=True)
class Incoming:
    """The inbound metadata for one message, read once and never guessed."""

    message_id: int = 0
    replied: Replied | None = None
    mentions: tuple[Mention, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """One person a spoken name could have meant, when it meant several.

    The server never picks between candidates — that is ``people.resolve``'s rule
    and it is kept here — so they are carried out to the caller, which asks. The
    username is included because it is what tells two people with the same first
    name apart in the question.
    """

    user_id: int = 0
    name: str = ""
    username: str = ""


@dataclass(frozen=True)
class Target:
    """What the message means, and where the answer should go.

    ``person_id``/``message_id`` are the semantic reading; ``reply_to`` is the
    Telegram decision. They are kept apart on purpose — «این آدم» can name a
    person while the answer still belongs under the asker's own message.
    """

    person_id: int = 0
    person_name: str = ""
    message_id: int = 0
    message_author_id: int = 0
    message_author_name: str = ""
    message_text: str = ""
    message_kind: str = ""
    surface: str = ""
    reply_to: int = 0
    explicit: bool = False
    # The candidates when a *single* spoken name matched more than one person in
    # the room. The server never picks between them; the caller asks instead.
    # Empty when there was nothing ambiguous.
    ambiguous: tuple[Candidate, ...] = ()
    # The spoken token that matched several people, so the clarification can
    # name it («منظورت کدوم علیه؟»). Empty when ``ambiguous`` is empty.
    ambiguous_query: str = ""
    # The message asked for a named person to be *mentioned* («فلانی رو تگ کن»),
    # and that person resolved. The caller may attach a Telegram mention for
    # them; the destination move is separate and already decided.
    wants_mention: bool = False
    # A person was named and resolved, but the server holds none of their
    # messages, so there is nothing to attach the answer to. The answer goes out
    # unattached rather than under the *asker's* message — quoting the person who
    # asked instead of the person asked about is the reported defect.
    no_reply: bool = False
    # The grade of the evidence for ``reply_to``: ``"explicit"`` when the message
    # asked for the reply, ``"reference"`` when it merely referred back to the
    # parent, and ``""`` when nothing moved. The two grades exist because the
    # caller may want to treat them differently — the model is told which one it
    # was, and a future caller could choose not to move on a weak reference
    # without having to re-derive the reading.
    confidence: str = ""
    why: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.message_id or self.person_id or self.reply_to or self.ambiguous)

    def destination(self, current: int | None = None) -> int | None:
        """The id to hand Telegram, falling back to the message being answered.

        Returns ``None`` when the reading says the answer must go out unattached
        (``no_reply``): a named target with no message of their own is a real
        case, and attaching the answer to the asker's message would be wrong.
        """
        if self.no_reply:
            return None
        return self.reply_to or current


def replied_identity(msg) -> tuple[int, str, int]:
    """The replied-to author's id and name, and the parent's message id.

    The narrow reading, for the callers that need *who* and *which message* and
    nothing else — the awareness capture, the observation record and the
    administrative turn. It is the single place ``reply_to_message`` is read for
    identity, so the content the richer :func:`read_incoming` adds cannot drift
    from the ids those callers act on.
    """
    replied = getattr(msg, "reply_to_message", None)
    if replied is None:
        return 0, "", 0
    author = getattr(replied, "from_user", None)
    if author is None:
        return 0, "", int(getattr(replied, "message_id", 0) or 0)
    return (
        int(getattr(author, "id", 0) or 0),
        str(getattr(author, "full_name", "") or ""),
        int(getattr(replied, "message_id", 0) or 0),
    )


def _read_replied(replied) -> Replied | None:
    """Telegram's own parent message, reduced to the fields that matter."""
    if replied is None:
        return None
    author = getattr(replied, "from_user", None)
    kind = ""
    try:
        from . import media

        ref = media.describe(replied)
        kind = str(getattr(ref, "kind", "") or "")
    except Exception:  # noqa: BLE001 - a media kind is not worth a failure
        kind = ""
    return Replied(
        message_id=int(getattr(replied, "message_id", 0) or 0),
        user_id=int(getattr(author, "id", 0) or 0),
        name=str(getattr(author, "full_name", "") or ""),
        text=_message_text(replied),
        kind=kind,
        has_media=bool(kind),
    )


def _read_mentions(msg) -> tuple[Mention, ...]:
    """Every person the message mentions, from its Telegram entities.

    Both entity lists are read: a photo with «@Nexus ببین» on it carries its
    words in ``caption_entities``, and a mention that only counts when it is a
    body is the same defect the ``_message_text`` helper exists to prevent.
    """
    found: list[Mention] = []
    for attr in ("entities", "caption_entities"):
        for entity in getattr(msg, attr, None) or ():
            etype = str(getattr(entity, "type", "") or "")
            if etype == "text_mention":
                user = getattr(entity, "user", None)
                if user is None:
                    continue
                found.append(
                    Mention(
                        user_id=int(getattr(user, "id", 0) or 0),
                        username=str(getattr(user, "username", "") or ""),
                        name=str(getattr(user, "full_name", "") or ""),
                    )
                )
            elif etype == "mention":
                surface = _entity_surface(msg, entity)
                if surface.startswith("@") and len(surface) > 1:
                    found.append(Mention(username=surface[1:]))
    return tuple(found)


def _entity_surface(msg, entity) -> str:
    """The text an entity spans, or ``""``.

    ``MessageEntity.extract_from`` is the library's own offset arithmetic and is
    preferred; the fallback reads the same field the entity was found in. Both
    are guarded, because an entity that cannot be located is not a reason to
    fail a turn.
    """
    try:
        extracted = entity.extract_from(msg)
        if extracted:
            return str(extracted)
    except Exception:  # noqa: BLE001 - an unlocatable entity is not a failure
        pass
    return ""


def read_incoming(msg) -> Incoming:
    """Read the inbound metadata for one message. Never raises, never guesses."""
    if msg is None:
        return Incoming()
    try:
        return Incoming(
            message_id=int(getattr(msg, "message_id", 0) or 0),
            replied=_read_replied(getattr(msg, "reply_to_message", None)),
            mentions=_read_mentions(msg),
        )
    except Exception:  # noqa: BLE001 - context, never worth a crash
        return Incoming(message_id=int(getattr(msg, "message_id", 0) or 0))


# ── The reading ───────────────────────────────────────────────────────────
def _expression(text: str):
    """The person-pointing expression the message used, or an empty one."""
    try:
        from . import referents

        return referents.find_expression(text)
    except Exception:  # noqa: BLE001 - a missing reader is not a failure
        return None


def _points_at_something(text: str) -> bool:
    """Whether the message carries any pointer at all.

    Two readers, because a reply directive can point at a *thing* as easily as at
    a person: «به این پیام جواب بده» is skipped by the person reader (the word
    after the demonstrative names a message, not a person) and caught by the
    entity reader's demonstrative scan.
    """
    if _expression(text):
        return True
    try:
        from . import entities

        return bool(entities.has_demonstrative(text))
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return False


def _engage_directive(text: str) -> bool:
    """Whether the message uses one of the "go and engage that one" shapes.

    Separate from :func:`_reply_directive` because it is also *evidence of
    pointing*: the phrase carries its own object («سرش», «باهاش», «حالشو»), so
    a message that uses one has pointed at somebody even when no demonstrative
    and no separate person expression appear. That is what lets «با این چت کن»
    and «سرش رو گرم کن» resolve to the same target.
    """
    folded = _fold(text)
    return bool(folded) and bool(_engage().search(folded))


def _reply_directive(text: str) -> bool:
    """Whether the message asks for the answer to be sent to the target."""
    folded = _fold(text)
    if not folded:
        return False
    if any(stem in folded for stem in _REPLY_STEMS):
        return True
    if any(phrase in folded for phrase in _DIRECTIVE_PHRASES):
        return True
    # The "go and engage that one" shapes, which carry their object inside the
    # phrase and so cannot be a substring list. See ``_ENGAGE_PATTERNS``.
    return bool(_engage().search(folded))


def _tag_directive(text: str) -> bool:
    """Whether the message asks Nexus to *mention* a named person.

    A narrower reading than the directive above: every tag phrase is a directive,
    but only a tag phrase asks for the person to be brought in by name. The
    server mentions them in the answer only for this grade, so a plain «جوابشو
    بده» — where the reply edge already notifies — does not also grow a mention.
    """
    folded = _fold(text)
    return bool(folded) and any(phrase in folded for phrase in _TAG_PHRASES)


# ── The implicit reference ────────────────────────────────────────────────
def _word(token: str) -> str:
    """A token reduced to its letters, so trailing punctuation cannot hide it.

    «آره،» and «آره» are the same word to a reader, and a fold that leaves the
    comma attached would make the agreement vocabulary miss the commonest way
    people write it. Only letters are kept; the fold has already normalised them.
    """
    return "".join(ch for ch in token if ch.isalpha())


def _names_nexus(token: str) -> bool:
    """Whether a token is the assistant's name, so it is a call and not content.

    «نکسوس آره دقیقاً» is agreement plus a call, and a check that counted the
    name as content would miss the agreement. The matcher is ``addressing``'s, so
    the fold, clitic, typo and skeleton evidence is the same one that routes the
    message.
    """
    try:
        from . import addressing

        return bool(addressing.is_name(token))
    except Exception:  # noqa: BLE001 - a name we cannot read is not content
        return False


def _agreement(text: str) -> bool:
    """Whether the message is pure agreement and nothing else.

    Every content token has to be an agreement or a filler beside one, because
    the claim being made is that the turn has no subject of its own — «آره
    دقیقاً» agrees with what came before, while «آره ولی فردا میام» introduces a
    new statement and is not about the parent at all. A mention (``@guardbot``)
    and the assistant's own name are dropped rather than counted, because
    addressing the assistant is not content: «نکسوس آره دقیقاً» is still pure
    agreement.
    """
    tokens: list[str] = []
    for raw in _fold(text).split():
        if raw.startswith("@"):
            continue
        word = _word(raw)
        if not word or _names_nexus(word):
            continue
        tokens.append(word)
    if not tokens:
        return False
    if not any(token in _AGREEMENT_TOKENS for token in tokens):
        return False
    return all(
        token in _AGREEMENT_TOKENS or token in _AGREEMENT_FILLERS for token in tokens
    )


def _possessive(text: str) -> str:
    """The stem of a possessive back-reference, or ``""``.

    One token wide: the clitic has to be attached to the noun, so «حرفش» matches
    and «حرف من» does not. That is the point — the clitic is what makes it a
    reference to something already mentioned rather than a fresh subject.
    """
    for token in _fold(text).split():
        token = _word(token)
        for stem in _POSSESSIVE_STEMS:
            if token in (stem + "ش", stem + "شو", stem + "شه", stem + "هش"):
                return stem
    return ""


def _third_person(text: str) -> str:
    """A third-person report of what the parent said or did, or ``""``."""
    for token in _fold(text).split():
        if _word(token) in _THIRD_PERSON_VERBS:
            return _word(token)
    return ""


def _implicit_reference(text: str) -> str:
    """How the message refers back to the replied-to message, or ``""``.

    Returns the evidence as a short phrase rather than a boolean, because the
    reason is carried into ``Target.why`` and into the block the model reads. The
    order is strength: a deictic is the weakest claim on its own (any message can
    contain «این»), and the other three are about something prior by
    construction.
    """
    if _points_at_something(text):
        return "deictic"
    stem = _possessive(text)
    if stem:
        return f"possessive «{stem}»"
    verb = _third_person(text)
    if verb:
        return f"third-person «{verb}»"
    if _agreement(text):
        return "agreement"
    return ""


def _competing_person(incoming: Incoming, *, replied, bot_id: int, bot_username: str) -> bool:
    """Whether the message points at somebody other than the parent's author.

    Only Telegram's own mention entities are consulted, and deliberately so: this
    runs on the common reply path, and a name-memory lookup per token would put a
    query on every group message. A message that mentions a third person is
    ambiguous — «این» could be about them — so nothing moves. An ``@username``
    the server cannot turn into an id without a query is treated as competing
    too, because the safe direction is to stay where the message was written.
    """
    parent_id = int(getattr(replied, "user_id", 0) or 0)
    for mention in incoming.mentions:
        if mention.user_id:
            if mention.user_id not in (bot_id, parent_id):
                return True
            continue
        if (
            mention.username
            and bot_username
            and mention.username.casefold() == bot_username.casefold()
        ):
            continue
        if mention.username:
            return True
    return False


def needs_window(incoming: Incoming | None, text: str) -> bool:
    """Whether resolving this message needs the stored room window.

    Only a reply directive needs it, and only to find the newest message by a
    person the message *names* — the replied-to content comes from Telegram
    itself and needs no query. Exposed so the caller can avoid the read on the
    common turn, where the reading already decided the window was wanted.
    """
    return _reply_directive(text)


def is_instruction(text: str, incoming: Incoming | None = None) -> bool:
    """Whether this message instructs Nexus about a message it replies to.

    The reported case, stated once: somebody replies to a person's message and
    tells Nexus to engage them — «سر اینو گرم کن», «با این چت کن». The reply
    edge points at a *third* person, so the message is not "addressed to the
    bot" by Telegram's own signals, and it used to be left to the awareness
    pass — which is why Nexus answered the asker instead of the person asked
    about. An explicit directive about a reply target is aimed at Nexus.

    Deliberately cheap and database-free: the handler asks it on every group
    message, so it is the directive vocabulary plus the same pointer evidence
    ``resolve`` requires before a destination moves. It requires the message to
    *be* a reply, because that is the whole premise — a directive with no reply
    edge is the awareness layer's to read, and widening this to every
    instruction would make a keyword list decide who Nexus answers.

    It grants nothing. It says only that this message is Nexus's to answer; the
    destination, the person and every authority check are still decided by
    :func:`resolve` and the layers above it.
    """
    incoming = incoming or Incoming()
    if incoming.replied is None or not incoming.replied.message_id:
        return False
    if not _reply_directive(text):
        return False
    if _points_at_something(text):
        return True
    # A directive that names somebody outright — «با میلاد چت کن» — carries no
    # pointer of its own. Only Telegram's own resolved entities are consulted
    # here, because a name-memory lookup on every group message is exactly the
    # query this function exists to avoid; the caller's ``resolve`` does the
    # lookup once it has decided the turn is Nexus's.
    return any(mention.user_id for mention in incoming.mentions)


def _resolve_name(query: str, chat_id: int) -> tuple[int, str]:
    """One name to one id, through the sanctioned entry point. ``(0, "")`` if not."""
    if not query:
        return 0, ""
    try:
        from . import people

        found = people.resolve(query, chat_id=chat_id)
    except Exception:  # noqa: BLE001 - a lookup is never worth a failure
        return 0, ""
    if str(found.get("status") or "") != "ok":
        return 0, ""
    return int(found.get("user_id") or 0), str(found.get("name") or "")


def _name_lookup(query: str, chat_id: int) -> tuple[list[Candidate], bool]:
    """Resolve one spoken token, keeping *why* it failed.

    Returns ``(candidates, ambiguous)``. ``ok`` yields one candidate and
    ``ambiguous`` yields several with the flag set — the difference the caller
    needs, because "I do not know this name" is answered by the model while "two
    people answer to it" must be answered by the *server* asking which one.
    Anything else yields nothing.
    """
    if not query:
        return [], False
    try:
        from . import people

        found = people.resolve(query, chat_id=chat_id)
    except Exception:  # noqa: BLE001 - a lookup is never worth a failure
        return [], False
    status = str(found.get("status") or "")
    if status == "ok":
        user_id = int(found.get("user_id") or 0)
        if not user_id:
            return [], False
        return (
            [
                Candidate(
                    user_id=user_id,
                    name=str(found.get("name") or ""),
                    username=str(found.get("username") or ""),
                )
            ],
            False,
        )
    if status == "ambiguous":
        candidates = [
            Candidate(
                user_id=int(c.get("user_id") or 0),
                name=str(c.get("name") or ""),
                username=str(c.get("username") or ""),
            )
            for c in (found.get("candidates") or [])
            if int(c.get("user_id") or 0)
        ]
        return candidates, bool(candidates)
    return [], False


def _mentioned_person(
    incoming: Incoming, *, chat_id: int, bot_id: int, bot_username: str
) -> tuple[int, str]:
    """The person the message mentions, from Telegram entities first.

    A ``text_mention`` is already an id and is taken as-is. An ``@username`` is
    resolved through the room's name memory. The bot's own mention is skipped: a
    message that says «@Nexus» has addressed the assistant, not named a target.
    """
    for mention in incoming.mentions:
        if not mention.user_id or mention.user_id == bot_id:
            continue
        return mention.user_id, mention.name
    for mention in incoming.mentions:
        if not mention.username:
            continue
        if bot_username and mention.username.casefold() == bot_username.casefold():
            continue
        found = _resolve_name(mention.username, chat_id)
        if found[0]:
            return found
    return 0, ""


def _named_person(
    text: str, *, chat_id: int
) -> tuple[int, str, tuple[Candidate, ...], str]:
    """The one person a plainly-written name resolves to, or nobody.

    Returns ``(user_id, name, ambiguous_candidates, ambiguous_query)``. Only
    consulted when the message carries a reply directive, because that is the
    only case the destination depends on a name. Every token is tried; if they
    resolve to *different* people the answer is nobody, because two names in one
    message is exactly the ambiguity the server must not resolve by picking. A
    *single* token that matches several people is different: that is a question
    the server can answer with the candidates, so they are returned rather than
    swallowed, together with the token that was ambiguous so the question can
    name it.
    """
    folded = _fold(text)
    if not folded:
        return 0, "", (), ""
    found: dict[int, str] = {}
    ambiguous: dict[int, Candidate] = {}
    ambiguous_query = ""
    tried = 0
    for token in folded.split():
        if tried >= _NAME_TOKEN_CAP:
            break
        if len(token) < 3 or token in _NAME_STOPWORDS:
            continue
        if token.startswith("@"):
            token = token[1:]
        tried += 1
        candidates, is_ambiguous = _name_lookup(token, chat_id)
        if is_ambiguous:
            ambiguous_query = ambiguous_query or token
            for candidate in candidates:
                ambiguous.setdefault(candidate.user_id, candidate)
            continue
        for candidate in candidates:
            found.setdefault(candidate.user_id, candidate.name)
    if ambiguous:
        return (
            0,
            "",
            tuple(sorted(ambiguous.values(), key=lambda c: c.user_id)),
            ambiguous_query,
        )
    if len(found) == 1:
        user_id = next(iter(found))
        return user_id, found[user_id], (), ""
    return 0, "", (), ""


def _newest_message_by(window, user_id: int, *, before: int) -> int:
    """The newest stored message by one person, before the current one.

    A message id the server already holds, so it is a fact rather than a guess.
    ``before`` keeps the destination behind the message being answered — a reply
    that pointed forward would quote a message nobody has seen yet.
    """
    if not user_id or not window:
        return 0
    best_id = 0
    best_at = -1
    for row in window:
        try:
            if int(row.get("user_id") or 0) != int(user_id):
                continue
            message_id = int(row.get("message_id") or 0)
            if not message_id or (before and message_id >= before):
                continue
            at = int(row.get("at") or 0)
        except Exception:  # noqa: BLE001 - a malformed row is not a failure
            continue
        if at >= best_at and message_id > best_id:
            best_id, best_at = message_id, at
    return best_id


def resolve(
    *,
    text: str,
    incoming: Incoming | None = None,
    chat_id: int = 0,
    window=(),
    current_message_id: int = 0,
    bot_id: int = 0,
    bot_username: str = "",
) -> Target:
    """Read one message into a semantic target and a Telegram reply destination.

    The order is the design. The replied-to message is Telegram's fact and is
    read first; a person named outright (a mention entity, or a name in a reply
    directive) outranks the reply edge, because a message that says «میلاد رو
    جواب بده» means میلاد even when it is a reply to somebody else; and the
    destination moves only for a directive that actually resolves to a message.
    """
    incoming = incoming or Incoming()
    text = str(text or "")
    replied = incoming.replied
    why: list[str] = []

    expression = _expression(text)
    surface = str(getattr(expression, "surface", "") or "")

    directive = _reply_directive(text)
    tag = _tag_directive(text)
    mentioned_id, mentioned_name = _mentioned_person(
        incoming, chat_id=chat_id, bot_id=bot_id, bot_username=bot_username
    )
    # A name is looked up whenever the message carries a reply directive, with or
    # without a demonstrative: «با میلاد حرف بزن» names its target outright and
    # carries no pointer at all, and requiring one would have missed exactly the
    # explicit-person case the reader exists for. The tag phrases («تگ کن») are
    # directives too, which is what lets «فلانی رو تگ کن» resolve the person
    # instead of silently answering the asker.
    named_id, named_name, ambiguous, ambiguous_query = 0, "", (), ""
    if mentioned_id:
        named_id, named_name = mentioned_id, mentioned_name
    elif directive:
        named_id, named_name, ambiguous, ambiguous_query = _named_person(
            text, chat_id=chat_id
        )

    # Whether the message *points* at something outside itself — a demonstrative
    # or a person expression. Read once here because two later decisions need it:
    # the person resolution below, and the destination rule that requires a
    # directive to point somewhere before it moves. An engage phrase («سر اینو
    # گرم کن», «باهاش حرف بزن») carries its object inside itself, so it points
    # even when no demonstrative appears. A stray reply verb with nothing to
    # point at («جواب ندادی», a complaint) therefore resolves nobody and moves
    # nothing.
    pointing = (
        bool(expression) or _points_at_something(text) or _engage_directive(text)
    )

    # The semantic message: the replied-to message, when there is one. This is
    # the reading the assistant never had — the parent's own words.
    message_id = 0
    message_author_id = 0
    message_author_name = ""
    message_text = ""
    message_kind = ""
    if replied is not None and replied.message_id:
        message_id = replied.message_id
        message_author_id = replied.user_id
        message_author_name = replied.name
        message_text = replied.text
        message_kind = replied.kind
        why.append(f"it replies to message {message_id}")

    # The semantic person. An explicit name wins; then a *person* pointer reads
    # the reply edge's author; and then — the case the owner reported — a message
    # that *asks Nexus to address somebody* and points at the replied-to message
    # means the author of that message. «سر اینو گرم کن» as a reply to علی is an
    # instruction to engage علی, so leaving ``person_id`` at zero was why the
    # model was never told who it was being asked to talk to. The bot's own
    # message is excluded: a reply to Nexus with a directive is about the
    # *content*, not about Nexus as a person to address.
    person_id = 0
    person_name = ""
    if named_id:
        person_id, person_name = named_id, named_name
        why.append("the message names them")
    elif replied is not None and expression:
        kind = str(getattr(expression, "kind", "") or "")
        if kind in ("person", "clitic") and replied.user_id:
            person_id = replied.user_id
            person_name = replied.name
            why.append(f"«{surface}» points at the author of the replied-to message")
    if (
        not person_id
        and directive
        and pointing
        and replied is not None
        and replied.user_id
        and int(replied.user_id) != int(bot_id)
    ):
        person_id = replied.user_id
        person_name = replied.name
        why.append(
            "the message asks for the reply to go to the replied-to message, so "
            "the person meant is its author"
        )

    # The Telegram destination. The default is the message being answered, which
    # is what the caller passes as ``current_message_id``; a destination is set
    # here only when the reading resolves to a real, server-known message.
    #
    # Two grades of evidence, and the order is the design. An explicit directive
    # is read first, because a message that asks for a reply to go somewhere is
    # not ambiguous about it.
    #
    # The directive must also *point* at something — a demonstrative, or a person
    # it names. That conjunction is what keeps a stray reply verb out: «جواب
    # ندادی» (you did not answer) as a reply is a complaint, not a request to
    # quote the parent, and with the verb alone it would have moved the
    # destination. Nothing resolves in that case, so the condition is False and
    # the answer stays under the message that was written.
    confidence = ""
    reply_to = 0
    no_reply = False
    if directive and (pointing or named_id):
        if person_id and replied is not None and person_id == replied.user_id:
            reply_to = replied.message_id
        elif person_id:
            reply_to = _newest_message_by(window, person_id, before=current_message_id)
            if not reply_to:
                if pointing and message_id:
                    # The message named a person *and* pointed at the parent
                    # («اینو ببین و به علی بگو»). The person has no message the
                    # server holds, but the pointer is an explicit ask for the
                    # parent, so the answer goes there.
                    reply_to = message_id
                else:
                    # The person is known and the server holds none of their
                    # messages. Answering under the *asker's* message would be the
                    # reported defect — Nexus quoting whoever asked instead of the
                    # person asked about — so the answer goes out unattached.
                    no_reply = True
        elif message_id:
            reply_to = message_id
        if reply_to or no_reply:
            confidence = "explicit"
            if reply_to:
                why.append(
                    f"the message asks for a reply, so the answer goes to {reply_to}"
                )
            else:
                why.append(
                    "the message asks about a named person the server holds no "
                    "message for, so the answer goes out unattached"
                )

    # The implicit grade. The message does not ask for anything, but it is *about*
    # the replied-to message — a deictic, a possessive, a third-person report or a
    # bare agreement. Here the reply edge itself is the evidence, and the answer
    # belongs attached to the message it is about.
    #
    # The destination only moves when nothing competes: the parent must not be
    # Nexus's own message (an answer that quotes itself is a loop nobody asked
    # for), and the message must not point at a third person (then «این» is
    # ambiguous and the honest answer is to stay put). A deictic is the weakest
    # signal on its own, so it is bounded by length — a long message that happens
    # to contain «این» has a subject of its own.
    if not reply_to and replied is not None and replied.message_id:
        evidence = _implicit_reference(text)
        if evidence == "deictic" and len(_fold(text).split()) > _REFERENCE_TOKEN_CAP:
            evidence = ""
        if evidence:
            if replied.user_id and int(replied.user_id) == int(bot_id):
                why.append(
                    "it is a reply to your own message, so the answer stays here"
                )
            elif _competing_person(
                incoming, replied=replied, bot_id=bot_id, bot_username=bot_username
            ):
                why.append(
                    "it also points at somebody else, so the answer stays here"
                )
            else:
                reply_to = replied.message_id
                confidence = "reference"
                why.append(
                    f"it refers back to the replied-to message ({evidence}), so the "
                    f"answer goes to {reply_to}"
                )

    return Target(
        person_id=person_id,
        person_name=person_name,
        message_id=message_id,
        message_author_id=message_author_id,
        message_author_name=message_author_name,
        message_text=message_text,
        message_kind=message_kind,
        surface=surface,
        reply_to=reply_to,
        explicit=confidence == "explicit",
        ambiguous=ambiguous,
        ambiguous_query=ambiguous_query,
        wants_mention=bool(person_id and tag and not ambiguous),
        no_reply=no_reply,
        confidence=confidence,
        why=tuple(why),
    )


# ── Rendering ─────────────────────────────────────────────────────────────
def _excerpt(text: str, cap: int = _EXCERPT_CHARS) -> str:
    """One line of somebody's message, bounded. Never raises."""
    body = " ".join(str(text or "").split())
    if cap > 0 and len(body) > cap:
        body = body[: cap - 1].rstrip() + "…"
    return body


def render(target: Target, *, cap: int = RENDER_CAP) -> str:
    """The relationship block for the model: what this message points at.

    This is the block the architecture was missing. It states, as fact and in one
    place, the three things the model used to have to reconstruct from a bare
    demonstrative: the message being answered, the message it replies to and who
    wrote it, and — when the person asked for it — the message the answer will
    quote. Renders nothing when there is nothing to say, so a self-contained
    message carries no overhead.

    The wording is evidence, not an instruction: it says what the server read, and
    the ids it names are the server's own. It never tells the model to use an id
    of its own choosing.
    """
    if not target:
        return ""
    lines = ["\n── The message and what it points at (read by the server) ──\n"]

    if target.message_id:
        author = target.message_author_name or "?"
        who = (
            f" by {author} ({target.message_author_id})"
            if target.message_author_id
            else f" by {author}"
        )
        lines.append(
            f"The message being answered is a reply to message id "
            f"{target.message_id}{who}.\n"
        )
        if target.message_text:
            lines.append(f"That message says: «{_excerpt(target.message_text)}»\n")
        elif target.message_kind:
            lines.append(f"That message carries a {target.message_kind}.\n")
        if target.surface:
            lines.append(
                f"«{target.surface}» in the current message points at that "
                f"replied-to message (id {target.message_id}).\n"
            )

    if target.person_id:
        lines.append(
            f"The person meant is {target.person_name or '?'} "
            f"({target.person_id}).\n"
        )
        if target.wants_mention:
            lines.append(
                "The message asked for that person to be brought in, so address "
                "them by name.\n"
            )

    if target.ambiguous:
        names = ", ".join(
            f"{candidate.name or '?'} ({candidate.user_id})"
            for candidate in target.ambiguous
        )
        lines.append(
            "The name the message used matches more than one person here "
            f"({names}). Do not guess which one — ask.\n"
        )

    if target.no_reply:
        lines.append(
            "Send the answer as a plain message, not as a Telegram reply: the "
            "person asked about has no message here to attach it to.\n"
        )

    if target.reply_to:
        if target.confidence == "explicit":
            reason = "the message asks for a reply to it"
        else:
            reason = "it is about that message"
        lines.append(
            f"Send the answer as a Telegram reply to message id {target.reply_to} — "
            f"{reason}.\n"
        )

    text = "".join(lines)
    if cap > 0 and len(text) > cap:
        # Bound on a line boundary where there is one, exactly as the other
        # readers do, so a cut never leaves half a sentence.
        cut = text.rfind("\n", 0, max(1, cap - 1))
        text = text[: cut if cut > 0 else cap].rstrip() + "\n"
    return text


__all__ = [
    "Candidate",
    "Incoming",
    "Mention",
    "Replied",
    "RENDER_CAP",
    "Target",
    "is_instruction",
    "needs_window",
    "read_incoming",
    "render",
    "replied_identity",
    "resolve",
]
