"""What does «این» point at when it is not a person?

The problem this module exists for
----------------------------------
``app/referents.py`` answers "who does «این» mean" and offers a ranked list of
*people*. But a demonstrative in a group very often points at a **thing**: the
photo somebody just posted, the link, the file. When an administrator replies
«اینو پاک کن» to a photograph, the resolver's honest answer is that it found no
person it could be — and the block it renders offers the room's members as the
things «اینو» might mean. That is a wrong lead, and a wrong-person moderation
action is the worst mistake available here.

The server knows the things, and it knows them exactly:

* **media** — the message row carries the media ``kind`` (``photo``, ``video``,
  ``voice`` …), written by the capture path from ``media.describe``. It is a
  stored column, not an inference. The same fact is also written into the text as
  a ``[kind]`` prefix, which is the fallback when the column is empty.
* **links** — a URL in a message is a regular expression away.
* **the message it replies to** — ``reply_message_id`` is a stored column, so when
  the anchor *names* a message («این پیام رو پاک کن») the server can point at the
  exact row.

What this module is, and what it is not
---------------------------------------
It is **evidence**, in exactly the sense ``app/referents.py``, ``app/discourse.py``
and ``app/room_state.py`` are. It reads a window and an anchor and reports what it
found, with the evidence attached. It is not a decision and it is not a gate:

* nothing branches on it — not a reply, not an action, not a schedule;
* it cannot choose a target — the model chooses, and every action is re-authorised
  from the actor's Telegram id;
* it holds no path to a permission: no ``db``, no ``config``, no pool, no
  ``rbac``. It is pure at import time and a test asserts the import set.

Why the block says "these are things, not people"
-------------------------------------------------
Because that is the correction it exists to make. The resolver's block and this
one are read together, and the failure mode this prevents is the model acting on
a person when the message was about a photograph. The sentence is evidence
framing, not an instruction — the model still decides.

That last sentence was true of the docstring and not of the code. The block
closed with *"do not act on a person unless the message names one"*, which is an
order, and printed under the resolver's ranked people it ordered the model to
disregard the block above it. The order belongs to the block that knows the side:
``app/objects.py`` states it when the verb decides the object, and says nothing
when the verb is unclassified — which is exactly the case where the resolver's
people are still live. This block states the implication of its own hypothesis
and stops there.

Why the correction has conditions
---------------------------------
A correction is a claim, and this one was being made whether or not the evidence
supported it. Two rules narrow it, and both are in ``Entities.offered``:

* **the message must point at something.** A greeting and a bare imperative
  («پاک کن») point at nothing, so the room's newest photograph is not a candidate
  for them. This is the same rule ``app/objects.py`` applies to the object it
  reports — *"a bare «پاک کن» points at nothing, and the room's newest photograph
  is not its object just because the room has one"* — applied to the candidates
  this block offers.
* **the request must not act on a member.** When the object reader has read the
  request as acting on a person, offering things beside it — with the sentence
  "do not act on a person unless the message names one" — tells the model the
  opposite of the block next to it. This is the mirror of the guard
  ``app/referents.py`` applies, and it is written with the same three clauses so
  a message that asks for both keeps every candidate.

Neither rule touches what the reader *found*: ``read_entities`` reports every
item and ``Entities.of_kind`` sees every item. Only the rendering is narrowed.

Why it only names the reply target when the anchor says "message"
----------------------------------------------------------------
A reply edge always has a target, so pointing at it unconditionally would print
the transcript's own text back to the model on every reply — tokens for something
already on screen. The anchor *naming* a message («پیام»، «کامنت»، «پست») is what
makes the target worth stating.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

KIND_MEDIA = "media"
KIND_LINK = "link"
KIND_MESSAGE = "message"

KINDS = (KIND_MEDIA, KIND_LINK, KIND_MESSAGE)

# A URL, and only a URL: the scheme form, or a bare «www.» host. Deliberately
# narrow — a rule that guessed at bare domains would match ordinary Persian words
# with a dot in them.
_URL = re.compile(r"https?://[^\s<>\"'()]+|www\.[^\s<>\"'()]+", re.IGNORECASE)

# The media kind the capture path writes into the text when the column is empty:
# ``awareness.capture`` stores a media message as ``[photo] caption``. Reading the
# prefix rather than importing ``app/media.py``'s kind table keeps this module
# free of that dependency and free of a second copy of the list.
_MEDIA_PREFIX = re.compile(r"^\[([a-z][a-z_]{2,20})\]\s*")

# The nouns that name a *thing* rather than a person, and which kind each names.
# Stems only: a group attaches the object marker and the possessive to them
# freely («لینکشو»، «فایلش»، «عکسش»)، so ``_bare`` strips one clitic before the
# lookup rather than the table listing every combination — which it did at first,
# and it missed three of the ten forms in the tests.
#
# The strip is safe because a hit is only accepted when the *stripped* form is a
# known noun: «فایده» strips to «فاید» and matches nothing, while «فایده» itself is
# not a noun here either.
_THING_NOUNS = {
    # links
    "لینک": KIND_LINK, "link": KIND_LINK, "url": KIND_LINK,
    # media
    "فایل": KIND_MEDIA, "file": KIND_MEDIA, "عکس": KIND_MEDIA,
    "photo": KIND_MEDIA, "تصویر": KIND_MEDIA, "ویدیو": KIND_MEDIA,
    "video": KIND_MEDIA, "فیلم": KIND_MEDIA, "ویس": KIND_MEDIA,
    "voice": KIND_MEDIA, "صدا": KIND_MEDIA, "استیکر": KIND_MEDIA,
    "sticker": KIND_MEDIA, "گیف": KIND_MEDIA, "gif": KIND_MEDIA,
    # messages
    "پیام": KIND_MESSAGE, "message": KIND_MESSAGE, "متن": KIND_MESSAGE,
    "کامنت": KIND_MESSAGE, "comment": KIND_MESSAGE, "پست": KIND_MESSAGE,
    "post": KIND_MESSAGE,
}

# One attached clitic, longest form first. Deliberately short: it holds the object
# marker, the possessive and the plural, and nothing that would let an ordinary
# word fold onto a noun it is not.
_CLITICS = ("شو", "های", "ش", "و", "رو", "ها", "ه")

# A demonstrative, so the module knows the anchor is *pointing* at something. It
# is the same near/far set ``referents`` reads; listed here because the two answer
# different questions and this one only needs to know that a pointer exists.
_DEMONSTRATIVES = frozenset(
    {
        "این", "اینو", "اینرو", "اینا", "اینیکی", "اینیک",
        "همین", "همینو", "همینرو", "همینیکی", "همینیک",
        "اون", "اونو", "اونرو", "اونا", "اونیکی", "اونیک",
        "همون", "همونو", "همونرو", "همونیکی", "همونیک",
        "قبلی", "قبلیش", "قبلیه", "قبلیا",
    }
)


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
# so it has to be excluded by name or it stays glued to the word before it:
# «این لینک؟» would not contain the noun «لینک», and this module would report no
# thing while the person resolver offered the room's members for a message about
# a link. A trailing question mark is one of the most common things in a room.
_TOKEN_SPLIT = re.compile(
    r"[^\w\u0600-\u06ff]|[\u060c\u061b\u061e\u061f\u066a\u066b\u066c\u066d\u06d4]|_"
)


def _tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def _bare(token: str) -> str:
    """The token with at most one attached clitic removed.

    A hit is only ever accepted on a known noun, so stripping cannot invent one.
    """
    for clitic in _CLITICS:
        if token.endswith(clitic) and len(token) - len(clitic) >= 2:
            return token[: -len(clitic)]
    return token


def thing_kind(token: str | None) -> str:
    """The thing-kind a **single** token names, or ``""``.

    The one-token form of :func:`named_kind`, exposed because a reader that is
    looking at one position in a message — ``app/referents.py`` asking "is the
    word right after this demonstrative a thing?" — must not have to re-tokenize
    the whole message, and must not keep a second copy of the noun list that
    drifts from this one. The clitic strip is included, so «لینکشو» and «فایلش»
    read as the nouns they are.
    """
    token = str(token or "")
    if not token:
        return ""
    return _THING_NOUNS.get(token) or _THING_NOUNS.get(_bare(token)) or ""


# The domain's own thing nouns — the things a room talks about that it does not
# hold as a row. «کانفیگ», «سرور», «تنظیمات»: a demonstrative bound to one of
# them is a **determiner**, not a person pronoun — «همون کانفیگ رو بده» is not
# about anybody — and the resolver offered the room's members for it, the same
# wrong lead §46 fixed for «این لینک». They are deliberately **not** in
# ``_THING_NOUNS``: that table drives the entity block, which points at things
# the room *holds* (a stored media kind, a URL, a reply row), and the room holds
# no «کانفیگ». So this is a second, closed list, and ``thing_word`` is the union
# the resolver needs rather than a kind the entity block would misrender.
_GENERIC_THING_NOUNS = frozenset(
    {
        "کانفیگ", "config", "تنظیمات", "settings",
        "سرور", "server", "سرویس", "service",
        "اشتراک", "اکانت", "account", "پنل", "panel",
        "لایسنس", "license", "کانال", "channel",
    }
)


def thing_word(token: str | None) -> bool:
    """Whether a noun names a *thing* rather than a person, room-held or not.

    The union the **resolver** needs: the room-held kinds (``thing_kind``) plus
    the domain's own thing nouns. It is deliberately not what the entity block
    renders — that block only points at things the room holds — so this answers
    "is the word after this demonstrative a noun, not a person?" and nothing
    more. The clitic strip is included, as in ``thing_kind``.
    """
    token = str(token or "")
    if not token:
        return False
    if thing_kind(token):
        return True
    return token in _GENERIC_THING_NOUNS or _bare(token) in _GENERIC_THING_NOUNS


def named_kind(text: str | None) -> tuple[str, str]:
    """The thing the message *names*, and the word it used.

    Returns ``("", "")`` when the message names no thing. The word is returned
    alongside the kind so the rendered reason can quote what the message said
    rather than the server's category for it.
    """
    for token in _tokens(text):
        kind = thing_kind(token)
        if kind:
            return kind, token
    return "", ""


def has_demonstrative(text: str | None) -> bool:
    """Whether the message points at something with a demonstrative."""
    return any(token in _DEMONSTRATIVES for token in _tokens(text))


def _points_at_something(text: str | None) -> bool:
    """Whether the message carries any expression that points at something.

    Two kinds, and the block needs both:

    * a **demonstrative** — «اینو»، «همون»، «قبلی» — which this module reads for
      its own sake; and
    * the **object clitic on an action verb**. «پاکش کن» says "delete it" with no
      demonstrative anywhere. ``referents.find_expression`` is the reader that
      knows the clitic forms, and ``app/objects.py`` consults it for the same
      question, so it is borrowed late and guarded rather than re-implemented
      here as a second copy of the clitic table.

    What it *excludes* is the point of it. A bare imperative («پاک کن») points at
    nothing, and neither does a greeting — the room's newest photograph is not
    their object just because the room has one. ``app/objects.py`` states that
    rule and applies it to the object it reports; this applies it to the
    candidates this block offers.
    """
    if has_demonstrative(text):
        return True
    try:
        from . import referents

        return bool(referents.find_expression(text))
    except Exception:  # noqa: BLE001 - a pointer we cannot read is not one
        return False


def _acts_on_a_person(text: str | None) -> bool:
    """Whether the message's directives act on a member and on no thing.

    The mirror of the guard ``app/referents.py`` applies one level down, read the
    other way round. That one stops the resolver offering a *person* for a request
    that acts on a thing; this one stops this block offering *things* for a
    request that acts on a person. «ساکتش کن» asks for a member to be muted, and a
    list of the room's photographs beside it — carrying the sentence "do not act
    on a person unless the message names one" — is the same wrong lead with the
    sides swapped.

    Three clauses, and each is load-bearing: there must be a directive; **no**
    directive may act on a thing, so a message that asks for both («ساکتش کن و
    اینو پاک کن») keeps every candidate, because losing a target is worse than an
    extra one; and at least one must act on a person. An unclassified directive
    answers nothing, so it never fires this guard — the same abstention
    ``discourse.acts_on`` makes for ``app/objects.py``.
    """
    try:
        from . import discourse

        found = discourse.directives(text)
        if not found:
            return False
        sides = [discourse.acts_on(word) for _index, word in found]
        if any(side == discourse.ACTS_ON_THING for side in sides):
            return False
        return any(side == discourse.ACTS_ON_PERSON for side in sides)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return False


def _is_media(row: dict) -> tuple[bool, str]:
    """Whether a row is a media message, and which kind.

    The stored ``kind`` column first — it is what the capture path wrote — and the
    ``[kind]`` text prefix as the fallback, because the same fact is written in
    both places and the column can be empty on a row captured before it existed.
    """
    kind = str(row.get("kind") or "").strip()
    if kind:
        return True, kind
    match = _MEDIA_PREFIX.match(str(row.get("text") or ""))
    if match:
        return True, match.group(1)
    return False, ""


def _excerpt(text: str | None, cap: int = 60) -> str:
    """A short, single-line excerpt, with the media prefix taken off."""
    body = _MEDIA_PREFIX.sub("", str(text or ""))
    body = " ".join(body.split())
    if len(body) <= cap:
        return body
    return body[: cap - 1].rstrip() + "…"


@dataclass(frozen=True)
class Entity:
    """One thing the anchor may point at, and why the server offers it."""

    kind: str
    detail: str
    user_id: int = 0
    at: int = 0
    text: str = ""
    newest: bool = False
    why: str = ""


@dataclass(frozen=True)
class Entities:
    """The things a demonstrative may mean, and the class the anchor named.

    ``pointing`` and ``acts_on_a_person`` are the two readings the *block* needs
    to decide what it may offer. Both are taken only when there is at least one
    item — with no candidates they cannot change anything, and taking them costs
    more than the rest of the reader — so both are ``False`` on a state whose
    ``items`` is empty. Read them through :meth:`offered`, which is the only
    thing that consumes them.
    """

    named: str = ""
    named_surface: str = ""
    items: tuple[Entity, ...] = ()
    pointing: bool = False
    acts_on_a_person: bool = False

    def __bool__(self) -> bool:
        return bool(self.items)

    def of_kind(self, kind: str) -> tuple[Entity, ...]:
        return tuple(item for item in self.items if item.kind == kind)

    def offered(self) -> tuple[Entity, ...]:
        """The items the block may offer, under its own two rules.

        The header says the message **may point at** these, so they are offered
        only when the message carries a pointer at all, and only when the object
        reader has not already read the request as acting on a member.

        Both rules are about what the *block claims*, not about what the reader
        found: ``read_entities`` still reports every item, and ``of_kind`` still
        sees every item. The rendering is the only thing narrowed.
        """
        if not self.pointing or self.acts_on_a_person:
            return ()
        return self.items


def _prior(messages, anchor: dict | None) -> list[dict]:
    """The window's rows that came before the anchor, the anchor excluded."""
    anchor = anchor or {}
    key_id = int(anchor.get("message_id") or 0)
    key = (
        ("id", key_id)
        if key_id
        else (
            "triple",
            int(anchor.get("user_id") or 0),
            int(anchor.get("at") or 0),
            str(anchor.get("text") or ""),
        )
    )
    anchor_at = int(anchor.get("at") or 0)
    out: list[dict] = []
    for row in messages or ():
        row_id = int(row.get("message_id") or 0)
        row_key = (
            ("id", row_id)
            if row_id
            else (
                "triple",
                int(row.get("user_id") or 0),
                int(row.get("at") or 0),
                str(row.get("text") or ""),
            )
        )
        if row_key == key:
            continue
        at = int(row.get("at") or 0)
        if anchor_at and at and at > anchor_at:
            continue
        out.append(row)
    return out


def read_entities(
    messages, anchor: dict | None = None, *, limit: int = 3
) -> Entities:
    """The things in the window the anchor may point at.

    Media and links come from the messages *before* the anchor — the thing a
    demonstrative points at is what the room already has. The anchor's reply
    target is added only when the anchor **names** a message, for the reason in
    the module docstring. Newest first, and bounded.
    """
    anchor = anchor or {}
    named, surface = named_kind(anchor.get("text"))
    prior = _prior(messages, anchor)
    found: list[Entity] = []

    for row in prior:
        is_media, media_kind = _is_media(row)
        if is_media:
            found.append(
                Entity(
                    kind=KIND_MEDIA,
                    detail=media_kind,
                    user_id=int(row.get("user_id") or 0),
                    at=int(row.get("at") or 0),
                    text=_excerpt(row.get("text")),
                    why=f"a {media_kind} was posted here",
                )
            )
        for url in _URL.findall(str(row.get("text") or "")):
            found.append(
                Entity(
                    kind=KIND_LINK,
                    detail=_host(url),
                    user_id=int(row.get("user_id") or 0),
                    at=int(row.get("at") or 0),
                    text=url,
                    why="a link was posted here",
                )
            )

    if named == KIND_MESSAGE:
        target = _reply_target_row(prior, anchor)
        if target is not None:
            found.append(
                Entity(
                    kind=KIND_MESSAGE,
                    detail="",
                    user_id=int(target.get("user_id") or 0),
                    at=int(target.get("at") or 0),
                    text=_excerpt(target.get("text")),
                    why="it is the message this one replies to",
                )
            )

    # Newest first, then bounded. "Newest" is the only ordering the window can
    # justify, and it is also the one a demonstrative prefers.
    found.sort(key=lambda item: (-item.at, item.kind))
    capped = tuple(found[: max(1, int(limit))])
    newest_at = max((item.at for item in capped), default=0)
    capped = tuple(
        Entity(
            kind=item.kind,
            detail=item.detail,
            user_id=item.user_id,
            at=item.at,
            text=item.text,
            newest=bool(newest_at and item.at == newest_at),
            why=item.why,
        )
        for item in capped
    )
    # The two readings the *block* needs, kept apart from the items: the reader
    # reports everything it found, and ``Entities.offered`` narrows what may be
    # put under the "may point at" header.
    #
    # They are taken **only when there are candidates**, and that is a decision
    # about cost rather than about meaning: with no items both answers are
    # already inert — ``offered`` returns nothing whatever they say — and the two
    # lexicons they borrow (``referents``' expression table, ``discourse``'s
    # action words) are most of what this reader costs. Measured in-process over
    # the corpus, best of three: taken unconditionally they cost **+60 µs** a
    # call, taken here **+7.6 µs** — and only one case in five has a candidate
    # for them to narrow.
    if capped:
        text = anchor.get("text")
        pointing = _points_at_something(text)
        acts_on_a_person = _acts_on_a_person(text)
    else:
        pointing = False
        acts_on_a_person = False
    return Entities(
        named=named,
        named_surface=surface,
        items=capped,
        pointing=pointing,
        acts_on_a_person=acts_on_a_person,
    )


def _host(url: str) -> str:
    """The host of a URL, for a reason line that fits in a prompt."""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split("?", 1)[0][:60]


def _reply_target_row(prior: list[dict], anchor: dict) -> dict | None:
    target_id = int(anchor.get("reply_message_id") or 0)
    if not target_id:
        return None
    for row in prior:
        if int(row.get("message_id") or 0) == target_id:
            return row
    return None


# ── Rendering ─────────────────────────────────────────────────────────────
def render(state: Entities, *, cap: int = 600) -> str:
    """The things the anchor may point at, and the class it named. Evidence only.

    Two claims, and each is now backed by its own evidence.

    The **item list** is offered under a header that says the message *may point
    at* these, so it renders only when the message carries a pointer at all and
    the object reader has not already read the request as acting on a member —
    both rules in ``Entities.offered``. It used to render whenever the window had
    a photograph and the message had anything at all: a greeting («سلام بچه ها»)
    reached the model as *"Things this message may point at … If the message means
    one of these, it is not about a person — do not act on a person unless the
    message names one."*

    The **named class** is a different fact and stands on its own: «فایل رو چک
    کن» names a file without pointing at one, so the line renders with no header
    when there is no list to head. The header belongs to the list, not to the
    block.

    Renders nothing when there is nothing to point at *and* nothing named: a
    block that says "no things found" would spend tokens to tell the model what
    the transcript already shows.
    """
    items = state.offered()
    if not items and not state.named:
        return ""
    lines: list[str] = []
    if items:
        lines.append(
            "\nThings this message may point at — things, not people "
            "(server-built; evidence, not a decision):"
        )
        for item in items:
            who = f" by {item.user_id}" if item.user_id else ""
            where = " (the newest)" if item.newest else ""
            if item.kind == KIND_MEDIA:
                what = f"a {item.detail}{who}{where}"
            elif item.kind == KIND_LINK:
                what = f"a link to {item.detail}{who}{where}"
            else:
                what = f"the message it replies to{who}"
            lines.append(f"- {what}: «{item.text}»" if item.text else f"- {what}")
    if state.named:
        named_line = (
            f"The message names «{state.named_surface}», so the "
            f"{_CLASS_WORDS[state.named]} is what it is about."
        )
        # The header above belongs to the item list, so the named line carries
        # its own leading break when it is the whole block.
        lines.append(named_line if lines else f"\n{named_line}")
    if items and not state.named:
        # Evidence framing, not an instruction — the module's own contract, and
        # the sentence was an order. "Do not act on a person unless the message
        # names one" printed under `referent_candidates`' three ranked people is
        # an order to disregard the block above it, and it is the opposite of
        # what that block exists for. The order belongs to the block that knows
        # the side: `app/objects.py` states it when the verb decides, and it says
        # nothing when the verb is unclassified — which is exactly when the
        # resolver's people are still live and this block must not contradict
        # them.
        lines.append(
            "If the message means one of these, it is about a thing rather than a "
            "person."
        )
    return _clip("\n".join(lines) + "\n", cap)


_CLASS_WORDS = {
    KIND_MEDIA: "file, photo or recording",
    KIND_LINK: "link",
    KIND_MESSAGE: "message",
}


def _clip(text: str, cap: int) -> str:
    """Bound the block, on a line boundary where there is one."""
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    room = max(1, cap - 1)
    cut = text.rfind("\n", 0, room)
    if cut <= 0:
        cut = room
    return text[:cut].rstrip() + "\n"


__all__ = [
    "Entity",
    "Entities",
    "KIND_LINK",
    "KIND_MEDIA",
    "KIND_MESSAGE",
    "KINDS",
    "has_demonstrative",
    "named_kind",
    "read_entities",
    "render",
    "thing_kind",
    "thing_word",
]
