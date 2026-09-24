"""Who is talking to whom, and whether this message is still the same thread.

The problem this module exists for
----------------------------------
An awareness pass reads a transcript and is asked what the room is doing. The
transcript says *what was said*, message by message, in order. It does not say
the thing a person in the room knows without thinking: **who is answering whom,
who the room has converged on, and whether the message in front of us is a
continuation of what came before or the start of something else.**

The server can read all three without a model, and two of them exactly:

* **The reply graph.** Every reply is a stored column — ``reply_user_id`` on the
  row. Who replied to whom is not an inference; it is the same fact
  ``awareness.instruction_block`` already states for one message, generalised to
  the window.
* **Who the room has converged on.** The target with the most incoming reply
  edges. A count, not a judgement.
* **Continuation.** This one *is* a reading, and it is the only heuristic here:
  whether the anchor's content words overlap the words of the messages before
  it. Shared words are the evidence, and they are reported with the reading
  rather than hidden behind it.

What this module is, and what it is not
---------------------------------------
It is **evidence**, in exactly the sense ``app/referents.py`` and
``app/discourse.py`` are: it reads a window and an anchor and reports what it
found, with the evidence attached. It is not a decision and it is not a gate:

* nothing branches on the reading — not a reply, not an action, not a schedule;
* it cannot make a message relevant — relevance is the model's;
* it cannot choose a person — the referent resolver reports candidates and the
  model chooses;
* it holds no path to a permission: no ``db``, no ``config``, no pool, no
  ``rbac``. It is pure at import time and a test asserts the import set.

Why the continuation reading is deliberately timid
--------------------------------------------------
«This message changed the subject» is a claim about meaning, and a wrong one
would be worse than none — a room whose messages happen to share no nouns is not
necessarily a room that moved on («باشه» shares nothing with anything). So the
reading abstains unless the anchor is *substantive*: it must carry at least
``MIN_TOPIC_TOKENS`` content words before the module will say either
``continues`` or ``shifts``. Below that the answer is ``unclear``, which is the
honest one.

The stopword list is the other half of that care. Overlap is only evidence if
the words that overlap *mean* something: «این», «که», «رو», «میشه» appear in
almost every Persian sentence and would make every message "continue" every
other one.

A clitic is the same hazard in a different shape. The fold turns the zero-width
non-joiner into a space, so «بچهها» and «بچه ها» both arrive as two tokens and
the bound morpheme «ها» becomes a *content word*: two messages that share any
plural noun "continue" each other, and the reason rendered to the model names
«ها» beside the real word. The clitic paradigm is closed, so it is listed with
the stopwords rather than guessed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# The continuation vocabulary. A closed set, like every other reading in this
# feature: ``""`` is "not enough to say", ``unclear`` is "there is something to
# judge and the evidence does not settle it", and the two verdicts are
# ``continues`` and ``shifts``.
RELATION_CONTINUES = "continues"
RELATION_SHIFTS = "shifts"
RELATION_UNCLEAR = "unclear"

RELATIONS = (RELATION_CONTINUES, RELATION_SHIFTS, RELATION_UNCLEAR)

# An anchor with fewer content words than this is not judged: a bare order or a
# one-word reply shares no nouns with anything, and reading that as "the topic
# changed" would fire on half the traffic in a moderation room.
MIN_TOPIC_TOKENS = 2

# Content words are at least this long. A single character is a clitic or a
# fragment, never a subject; everything else is decided by the stopword list,
# which is where the job belongs. A length cutoff is a crude proxy and it was
# one word too crude: «چک» ("check") is two characters and is exactly what a
# message about a file is about, so a floor of three dropped it and made
# «فایل رو چک کن» too short to judge.
MIN_TOKEN = 2

# The words that carry no topic. Every one of them is a word that appears in
# nearly every Persian sentence, which is exactly why overlap on them would be
# meaningless. Kept explicit rather than derived, so it can be read and argued
# with; the fold means the Arabic spellings need not be listed separately.
_STOP = frozenset(
    {
        # pronouns and demonstratives
        "من", "تو", "او", "ما", "شما", "اونا", "ایشون", "خودم", "خودت",
        "این", "اون", "همین", "همون", "اینا", "اینو", "اونو", "همینو", "همونو",
        "اینها", "اونها", "اینجا", "اونجا", "اینطور", "اونطور", "اینکار",
        # copulas and common verbs
        "هست", "است", "نیست", "بود", "بودم", "بودی", "بودن", "شد", "شدم",
        "شده", "میشه", "میشد", "بشه", "دارم", "داره", "دارن", "داشت",
        "کرد", "کردم", "کردی", "کرده", "کنم", "کنی", "کنه", "کنید", "کن",
        "میکن", "میکنم", "بکن", "بکنم", "بکنید", "گفت", "گفتم", "گفتی", "بگو",
        "میگم", "میگه", "رفت", "رفتم", "رفتی", "میام", "میاد", "بیا", "اومد",
        "بیاین", "بده", "بدم", "بدید", "بگیر", "بگیرم", "ببین", "ببینم", "بدون",
        # particles, connectors and discourse markers
        "که", "رو", "را", "و", "با", "به", "در", "از", "بر", "برای", "تا",
        "یا", "اگر", "اگه", "چون", "ولی", "اما", "پس", "هم", "همم", "فقط",
        "خیلی", "بیشتر", "کمتر", "همه", "هیچ", "یه", "یک", "دو", "سه",
        "بله", "آره", "اره", "نه", "خب", "خوب", "باشه", "حتما", "شاید",
        "لطفا", "ممنون", "سلام", "مرسی", "بعد", "قبل", "الان", "حالا",
        # question words — they ask about the topic, they are not the topic
        "چی", "چیه", "چیست", "چرا", "کجا", "کی", "چند", "چقدر", "چطور",
        "چجوری", "آیا", "ایا", "کدوم", "کدام", "مگه", "مگر",
        # the Latin half a mixed room writes
        "the", "and", "for", "you", "are", "not", "but", "can", "please",
        "this", "that", "with", "what", "why", "how", "who", "when", "where",
    }
)

# The plural and possessive clitics. A clitic is a bound morpheme — it cannot
# stand on its own — but the ZWNJ fold splits it off, so «بچهها» arrives as
# «بچه» + «ها» and the second token passed the length floor and the stopword
# list. Two messages that share only a plural noun then "continued" each other,
# and the rendered reason named «ها» beside the word that mattered. The paradigm
# is closed, so it is listed rather than derived. The single-character clitics
# («م» «ت» «ش») are not here: they never survive ``MIN_TOKEN``, which already
# drops anything shorter than two characters.
_CLITIC = frozenset(
    {
        # plural
        "ها", "های", "هایی",
        # possessive, attached to the plural
        "هام", "هات", "هاش", "هامان", "هاتان", "هاشان",
        "هامون", "هاتون", "هاشون",
        # possessive, the formal/ezafe spellings
        "هایم", "هایت", "هایش", "هایمان", "هایتان", "هایشان",
    }
)


def _fold(text: str | None) -> str:
    """The shared fold, reused rather than copied.

    ``people.normalize`` already handles the Arabic-versus-Persian letters, the
    diacritics, the zero-width joiner and the digit sets. The import is late and
    guarded so this module stays importable on its own — a fold must never be
    the reason a read fails.
    """
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a read fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(folded.split())


# The Arabic block's punctuation — «؟» «،» «؛» — lives *inside* ``\u0600-\u06ff``,
# so it has to be excluded by name or it stays glued to the word before it:
# «چی شده؟» would carry the token «شده؟», which is not the stopword «شده», and
# two messages that differ only by a question mark would share no content word.
_TOKEN_SPLIT = re.compile(
    r"[^\w\u0600-\u06ff]|[\u060c\u061b\u061e\u061f\u066a\u066b\u066c\u066d\u06d4]|_"
)


def _tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def content_tokens(text: str | None) -> tuple[str, ...]:
    """The words a message is *about*, in order, deduped.

    Deduped because overlap is a set question: «فایل» twice is not two pieces of
    evidence that the topic is files. Order is kept so a rendered reason reads
    in the order the message used the words. A clitic is never one of them:
    «ها» is a bound morpheme the fold split off, not a thing a message is about.
    """
    out: list[str] = []
    for token in _tokens(text):
        if len(token) < MIN_TOKEN or token in _STOP or token in _CLITIC:
            continue
        if token not in out:
            out.append(token)
    return tuple(out)


# ── The reply graph ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Edge:
    """One reply: who wrote it, and who they were answering."""

    source_id: int
    target_id: int
    message_id: int = 0
    at: int = 0


@dataclass(frozen=True)
class RoomState:
    """What the server can say about the room, with the evidence for each part."""

    edges: tuple[Edge, ...] = ()
    focus_id: int = 0
    focus_count: int = 0
    participants: tuple[int, ...] = ()
    relation: str = ""
    shared: tuple[str, ...] = ()
    anchor_tokens: tuple[str, ...] = ()
    why: str = ""

    def __bool__(self) -> bool:
        return bool(self.edges or self.participants or self.relation)

    @property
    def focus_sources(self) -> tuple[int, ...]:
        """The distinct members whose replies were aimed at the focus.

        One member replying twice is one voice, not two. The count over the
        edges says how many *replies* point at the focus; this says how many
        *people* do, which is what "the room's replies have converged" claims.
        """
        return tuple(
            sorted({e.source_id for e in self.edges if e.target_id == self.focus_id})
        )

    def converged(self) -> bool:
        """Whether more than one *member* has replied to the same person.

        Two conditions, and the second was missing: more than one reply (the
        edge count) **and** those replies from more than one member. The
        sentence says "the room's replies have converged on X", and one member
        replying twice is not the room. ``anaphoric-split-room`` — the corpus's
        own note calls that room "split" — had one member replying twice to each
        of two people, and the sentence called it a convergence. The edge is
        still reported; only the *word* is withheld.
        """
        return len(self.focus_sources) >= 2


def _anchor_key(anchor: dict | None) -> tuple:
    """How to recognise the anchor's own row inside the window.

    The message id when the row has one — the stored column, and exact. When it
    does not (an anchor the pass built by hand), the triple of who, when and what
    they said, which is what the row *is*. Two genuinely different messages
    cannot share all three.
    """
    anchor = anchor or {}
    message_id = int(anchor.get("message_id") or 0)
    if message_id:
        return ("id", message_id)
    return (
        "triple",
        int(anchor.get("user_id") or 0),
        int(anchor.get("at") or 0),
        str(anchor.get("text") or ""),
    )


def _row_key(row: dict) -> tuple:
    message_id = int(row.get("message_id") or 0)
    if message_id:
        return ("id", message_id)
    return (
        "triple",
        int(row.get("user_id") or 0),
        int(row.get("at") or 0),
        str(row.get("text") or ""),
    )


def _is_nexus(row: dict) -> bool:
    return str(row.get("role") or "") == "nexus"


def _prior(messages, anchor: dict | None) -> list[dict]:
    """The window's messages that came *before* the anchor, anchor excluded.

    The anchor is usually one of the window's own rows — ``awareness.anchor``
    picks it from there — so it has to be taken out before the rest can be
    called "what came before". A row that arrived after the anchor is not prior
    either, which is what the timestamp test is for.
    """
    key = _anchor_key(anchor)
    anchor_at = int((anchor or {}).get("at") or 0)
    out: list[dict] = []
    for row in messages or ():
        if _row_key(row) == key:
            continue
        at = int(row.get("at") or 0)
        if anchor_at and at and at > anchor_at:
            continue
        out.append(row)
    return out


def _edges(rows) -> tuple[Edge, ...]:
    """Every reply edge in ``rows``, oldest first.

    Read straight off the stored column. Nexus's own replies are included: "the
    assistant answered X" is part of who is talking to whom.
    """
    out: list[Edge] = []
    for row in rows or ():
        target = int(row.get("reply_user_id") or 0)
        if not target:
            continue
        source = int(row.get("user_id") or 0)
        if source == target:
            continue
        out.append(
            Edge(
                source_id=source,
                target_id=target,
                message_id=int(row.get("message_id") or 0),
                at=int(row.get("at") or 0),
            )
        )
    out.sort(key=lambda edge: (edge.at, edge.message_id))
    return tuple(out)


def _focus(edges: tuple[Edge, ...]) -> tuple[int, int]:
    """The person the replies converge on, and how many were aimed at them.

    The most-replied-to target; a tie is broken by the most recent edge, which is
    the only ordering the window can justify. ``(0, 0)`` when there is no edge.
    """
    if not edges:
        return 0, 0
    counts: dict[int, int] = {}
    latest: dict[int, int] = {}
    for edge in edges:
        counts[edge.target_id] = counts.get(edge.target_id, 0) + 1
        latest[edge.target_id] = max(latest.get(edge.target_id, 0), edge.at)
    top = max(counts, key=lambda target: (counts[target], latest.get(target, 0)))
    return top, counts[top]


def _participants(rows) -> tuple[int, ...]:
    """Who has spoken in the window, newest first, humans only, deduped.

    Nexus is excluded on purpose: the question this answers is "which *people*
    are currently in play", and the assistant is not one of them. Every id is a
    speaker the room already saw, never a reply target who did not speak.
    """
    seen: list[int] = []
    for row in reversed(list(rows or ())):
        if _is_nexus(row):
            continue
        user_id = int(row.get("user_id") or 0)
        if user_id and user_id not in seen:
            seen.append(user_id)
    return tuple(seen)


def _relation(
    anchor: dict | None, prior: list[dict]
) -> tuple[str, tuple[str, ...], tuple[str, ...], str]:
    """Whether the anchor continues the thread, with the words that decided it.

    Returns ``(relation, shared, anchor_tokens, why)``. Abstains when there is no
    prior message to compare against (nothing to continue), and when the anchor
    is too short to be about anything.
    """
    anchor_tokens = content_tokens((anchor or {}).get("text"))
    prior_tokens: set[str] = set()
    for row in prior:
        prior_tokens.update(content_tokens(row.get("text")))

    if not prior_tokens:
        return "", (), anchor_tokens, "there is nothing before this message to continue"
    if len(anchor_tokens) < MIN_TOPIC_TOKENS:
        return (
            RELATION_UNCLEAR,
            (),
            anchor_tokens,
            "the message is too short to say what it is about",
        )
    shared = tuple(token for token in anchor_tokens if token in prior_tokens)
    if shared:
        return (
            RELATION_CONTINUES,
            shared,
            anchor_tokens,
            f"it shares {', '.join('«' + t + '»' for t in shared)} with what came before",
        )
    return (
        RELATION_SHIFTS,
        (),
        anchor_tokens,
        "it shares no content word with what came before",
    )


def read_state(messages, anchor: dict | None = None) -> RoomState:
    """Read the room: the reply graph, who it converged on, and the thread.

    ``messages`` is the window the pass already read and ``anchor`` is the
    message the pass is about — the same two things every other reader here takes,
    so a pass pays no extra query.
    """
    rows = list(messages or ())
    prior = _prior(rows, anchor)
    # The anchor's own reply edge is part of the graph even when the anchor is not
    # in the window: it is the one edge the pass is *about*.
    edge_rows = list(rows)
    anchor_key = _anchor_key(anchor)
    if anchor and not any(_row_key(row) == anchor_key for row in rows):
        edge_rows.append(anchor)
    edges = _edges(edge_rows)
    focus_id, focus_count = _focus(edges)
    relation, shared, anchor_tokens, why = _relation(anchor, prior)
    return RoomState(
        edges=edges,
        focus_id=focus_id,
        focus_count=focus_count,
        participants=_participants(rows),
        relation=relation,
        shared=shared,
        anchor_tokens=anchor_tokens,
        why=why,
    )


# ── Rendering ─────────────────────────────────────────────────────────────
def render_graph(state: RoomState, *, cap: int = 600, people_cap: int = 6) -> str:
    """Who replied to whom, and who the room has converged on. Evidence only.

    Two blocks' worth of facts in one, because they are one reading: the edges
    are the raw record and the focus is the count over them. The focus sentence
    changes with the count — "converged on" needs more than one reply *from more
    than one member*, and the weaker cases say so plainly rather than borrowing
    the stronger word.
    """
    if not state.edges and not state.participants:
        return ""
    lines = ["\nThe room's state, from the server's own record (evidence, not a judgement):"]
    if state.edges:
        pairs = ", ".join(f"{e.source_id} → {e.target_id}" for e in state.edges)
        lines.append(f"- Replies in this window: {pairs}")
        if state.converged():
            lines.append(
                f"- The room's replies have converged on {state.focus_id} "
                f"({state.focus_count} of {len(state.edges)})."
            )
        elif state.focus_id:
            if state.focus_count == 1:
                lines.append(
                    f"- One reply was aimed at {state.focus_id}; that is not a "
                    "convergence."
                )
            else:
                lines.append(
                    f"- {state.focus_count} replies were aimed at "
                    f"{state.focus_id}, all from one member; that is not a "
                    "convergence."
                )
    else:
        lines.append("- No reply in this window was aimed at anyone.")
    if state.participants:
        who = ", ".join(str(user_id) for user_id in state.participants[:people_cap])
        lines.append(f"- People who spoke, newest first: {who}")
    return _clip("\n".join(lines) + "\n", cap)


def render_thread(state: RoomState, *, cap: int = 500) -> str:
    """Whether the anchor continues the thread, with the words that decided it.

    The reason is rendered, not just the verdict, because the verdict is a
    heuristic and the model is the one that should weigh it. An abstention
    renders nothing: a line saying "the topic is unclear" would spend tokens to
    tell the model what it can already see.
    """
    if state.relation in ("", RELATION_UNCLEAR):
        return ""
    if state.relation == RELATION_CONTINUES:
        head = "\nThis message continues the thread the room is already on"
    else:
        head = "\nThis message reads as a change of subject"
    return _clip(f"{head} — {state.why}. That is the server's reading of the words.\n", cap)


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
    "Edge",
    "RoomState",
    "RELATION_CONTINUES",
    "RELATION_SHIFTS",
    "RELATION_UNCLEAR",
    "RELATIONS",
    "content_tokens",
    "read_state",
    "render_graph",
    "render_thread",
]
