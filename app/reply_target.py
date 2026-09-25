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

The second is a policy decision, and the default is deliberately the **current**
message: somebody who writes «@Nexus ببین این چیه» has asked Nexus a question,
and the answer belongs under their question. The destination changes to the
resolved target only when the message actually asks for that — «به این جواب بده»,
«جواب اینو بده», «با این صحبت کن», «سر به سر این بذار», «میلاد رو جواب بده» —
because then the person is asking Nexus to address somebody else, and an answer
that is not a reply to them has not done the thing that was asked. «این رو ببین»
and «این چیه» are *lookups*, not replies, and they change nothing.

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

import re
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
    why: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.message_id or self.person_id or self.reply_to)

    def destination(self, current: int | None = None) -> int | None:
        """The id to hand Telegram, falling back to the message being answered."""
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


def _reply_directive(text: str) -> bool:
    """Whether the message asks for the answer to be sent to the target."""
    folded = _fold(text)
    if not folded:
        return False
    if any(stem in folded for stem in _REPLY_STEMS):
        return True
    return any(phrase in folded for phrase in _ADDRESS_PHRASES)


def needs_window(incoming: Incoming | None, text: str) -> bool:
    """Whether resolving this message needs the stored room window.

    Only a reply directive needs it, and only to find the newest message by a
    person the message *names* — the replied-to content comes from Telegram
    itself and needs no query. Exposed so the caller can avoid the read on the
    common turn, where the reading already decided the window was wanted.
    """
    return _reply_directive(text)


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
) -> tuple[int, str]:
    """The one person a plainly-written name resolves to, or nobody.

    Only consulted when the message carries a reply directive, because that is
    the only case the destination depends on a name. Every token is tried; if
    they resolve to *different* people the answer is nobody, because two names in
    one message is exactly the ambiguity the server must not resolve by picking.
    """
    folded = _fold(text)
    if not folded:
        return 0, ""
    found: dict[int, str] = {}
    tried = 0
    for token in folded.split():
        if tried >= _NAME_TOKEN_CAP:
            break
        if len(token) < 3 or token in _NAME_STOPWORDS:
            continue
        if token.startswith("@"):
            token = token[1:]
        tried += 1
        user_id, name = _resolve_name(token, chat_id)
        if user_id:
            found.setdefault(user_id, name)
    if len(found) == 1:
        user_id = next(iter(found))
        return user_id, found[user_id]
    return 0, ""


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
    mentioned_id, mentioned_name = _mentioned_person(
        incoming, chat_id=chat_id, bot_id=bot_id, bot_username=bot_username
    )
    # A name is looked up whenever the message carries a reply directive, with or
    # without a demonstrative: «با میلاد حرف بزن» names its target outright and
    # carries no pointer at all, and requiring one would have missed exactly the
    # explicit-person case the reader exists for.
    named_id, named_name = 0, ""
    if mentioned_id:
        named_id, named_name = mentioned_id, mentioned_name
    elif directive:
        named_id, named_name = _named_person(text, chat_id=chat_id)

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

    # The semantic person. An explicit name wins; otherwise a *person* pointer
    # reads the reply edge's author, while a bare «این» stays about the message.
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

    # The Telegram destination. The default is the message being answered, which
    # is what the caller passes as ``current_message_id``; a destination is set
    # here only when the directive resolves to a real, server-known message.
    #
    # The directive must also *point* at something — a demonstrative, or a person
    # it names. That conjunction is what keeps a stray reply verb out: «جواب
    # ندادی» (you did not answer) as a reply is a complaint, not a request to
    # quote the parent, and with the verb alone it would have moved the
    # destination. Nothing resolves in that case, so the condition is False and
    # the answer stays under the message that was written.
    pointing = bool(expression) or _points_at_something(text)
    reply_to = 0
    if directive and (pointing or named_id):
        if person_id and replied is not None and person_id == replied.user_id:
            reply_to = replied.message_id
        elif person_id:
            reply_to = _newest_message_by(window, person_id, before=current_message_id)
        elif message_id:
            reply_to = message_id
        if reply_to:
            why.append(f"the message asks for a reply, so the answer goes to {reply_to}")

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
        explicit=bool(reply_to),
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

    if target.reply_to:
        lines.append(
            f"Send the answer as a Telegram reply to message id {target.reply_to} — "
            "the message asks for a reply to it.\n"
        )

    text = "".join(lines)
    if cap > 0 and len(text) > cap:
        # Bound on a line boundary where there is one, exactly as the other
        # readers do, so a cut never leaves half a sentence.
        cut = text.rfind("\n", 0, max(1, cap - 1))
        text = text[: cut if cut > 0 else cap].rstrip() + "\n"
    return text


__all__ = [
    "Incoming",
    "Mention",
    "Replied",
    "RENDER_CAP",
    "Target",
    "needs_window",
    "read_incoming",
    "render",
    "replied_identity",
    "resolve",
]
