"""Who does «این» mean? The candidates, ranked — never the choice.

The problem this module exists for
----------------------------------
A moderation instruction in a group is almost never self-contained. «اینو ساکت
کن», «همون کاربر رو بن کن», «ادمینه رو محدود کن» — the person is named by a
pronoun or a role, and the only thing in the world that says who that is, is the
conversation around it. Today the server resolves exactly one of those cases: the
*reply edge*. If the instruction was sent as a reply, the target is a stored
column and ``awareness.instruction_block`` states it as fact. If it was not, the
server says so and tells the model to look at the transcript and ask if it cannot
tell.

That is the right fail-safe direction, but it leaves a large, determinable class
of cases on the table. A message that names somebody («رضا رو بن کن»), states an
id, or follows a person who has been the subject of the last three replies has a
referent the server could have found without a model call — and a model that is
handed a ranked list of candidates makes fewer wrong-person mistakes than one
that is asked to re-derive the room from a transcript.

What this module is, and what it is not
---------------------------------------
It is **evidence**, in exactly the sense ``app/addressing.py`` is evidence: it
reads the text and the window and reports what it found, with the strength of
each finding. It is not a decision, and the distinction is load-bearing rather
than pedantic:

* it cannot make a message relevant — relevance is the model's;
* it cannot make anything happen — only ``app/admin_service.py`` authorises;
* it cannot choose the referent — the model chooses, and the chosen id is
  re-authorised from the actor's Telegram id like every other request.

That last one is why ``resolve`` reports ``ambiguous`` instead of picking. When
two candidates are genuinely close, the honest answer is "the server could not
tell these apart", and the model is told that so it can ask. A resolver that
silently picked the higher score would be guessing with extra steps, and a
wrong-person moderation action is the worst mistake available here.

The one case where the server *does* know
-----------------------------------------
A reply edge is not evidence, it is the answer: when somebody replies to a
person's message and says «این», «این» is that person. So a reply target is
reported as ``confident`` outright, and no other candidate can make it
ambiguous. This is the same reading ``awareness.instruction_block`` already
states as fact; this module generalises it rather than replacing it.

What it deliberately is not
---------------------------
No database, no model, no config, no authority. It takes a window of message
dicts and an anchor dict — the same rows ``db.group_window`` returns and
``awareness.anchor`` picks — and returns plain data. That is what makes it
testable against a realistic corpus without a key, a clock or a bot.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ── Folding ───────────────────────────────────────────────────────────────
# The shared fold is ``people.normalize``, and it is reused rather than copied
# for the same reason ``addressing`` reuses it: it already handles the
# Arabic-versus-Persian letters, the diacritics, the zero-width joiner and the
# digit sets, and a second implementation is a second place for the two to
# disagree. The import is late and guarded so this module stays importable on
# its own — a fold must never be the reason a name fails to match.
def _fold(text: str) -> str:
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a call fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
        folded = " ".join(folded.split())
    return folded


# ── The expressions ───────────────────────────────────────────────────────
# Persian points at a person with a demonstrative, optionally with a noun after
# it, or with a role word. The three kinds are not equally strong evidence about
# *personhood*, which is why the kind is carried rather than flattened to a
# boolean:
#
# * ``person`` — «این کاربر», «همون طرف». The message says "person" outright.
# * ``role``   — «ادمینه», «مدیره». It names a role, not a person, so the
#   candidates are whoever holds that role now — which is a fact about
#   ``rbac``/the window, not about the text.
# * ``prior``  — «قبلی», «قبلیش». It points at what came before, which is the
#   one referent ``instruction_block`` explicitly warns against reusing; the
#   candidates are offered with that warning attached rather than suppressed.
# * ``deictic`` — the bare «این», «اون», «همون». The weakest about personhood:
#   it may point at a message, a config or a link. Reported, but the block that
#   renders it says so.
KIND_PERSON = "person"
KIND_ROLE = "role"
KIND_PRIOR = "prior"
KIND_CLITIC = "clitic"
KIND_DEICTIC = "deictic"

# The demonstratives are listed as **surfaces**, not stems, and that is the
# lesson of the first draft. Stripping the object marker «و» generically turns
# «اینو» into «این» — and also turns «آمو» or any other word ending in «و» into
# something it is not, because «و» is both the object marker and an ordinary
# letter. Listing «اینو» and «همونو» explicitly costs two strings and removes the
# whole class of false folds. A separated marker («این رو») is handled in the
# scan by skipping the marker token, not by stripping it.
_NEAR_SURFACES = frozenset(
    {"این", "اینو", "اینرو", "اینا", "اینیکی", "اینیک",
     "همین", "همینو", "همینرو", "همینیکی", "همینیک"}
)
_FAR_SURFACES = frozenset(
    {"اون", "اونو", "اونرو", "اونا", "اونیکی", "اونیک",
     "همون", "همونو", "همونرو", "همونیکی", "همونیک"}
)
_PERSON_NOUNS = frozenset(
    {
        "کاربر", "یوزر", "شخص", "طرف", "آدم", "بنده", "کاربره", "یوزره", "طرفه",
        # The Latin half. A group that writes Persian and English in one
        # sentence is the norm here, not the exception, and «این user» points at
        # a person exactly as «این کاربر» does.
        "user", "guy", "dude", "person", "member",
    }
)
_ROLE_WORDS = frozenset(
    {
        "ادمین", "مدیر", "مالک", "صاحب", "ادمینه", "مدیره",
        "admin", "moderator", "mod", "owner",
    }
)
_PRIOR_WORDS = frozenset({"قبلی", "قبلیش", "قبلیه", "قبلیا"})

# The object marker as its own token, skipped when it follows a demonstrative.
_OBJECT_MARKERS = frozenset({"رو", "را"})

# A clitic a group attaches directly to a role or a person noun: «ادمینه» is
# «ادمین» + the object marker, and the fold leaves them joined. The list holds
# no bare «و», for the reason above.
_CLITICS = ("رو", "را", "ها", "های", "یه", "یی", "ای", "ام", "ات", "اش", "ش", "ه")


def _deictic(token: str) -> str:
    """``"near"``, ``"far"`` or ``""`` for a folded token."""
    if token in _NEAR_SURFACES:
        return "near"
    if token in _FAR_SURFACES:
        return "far"
    return ""


def _action_words() -> frozenset[str]:
    """The moderation vocabulary, borrowed rather than copied.

    «ساکتش کن», «بنش کن», «حذفش کن» point at a person with no demonstrative at
    all: the object is the clitic «ـش» attached to the verb. The list that knows
    which words those are already exists — ``app/addressing.py``'s
    ``ACTION_WORDS``, whose stated purpose is telling a *call* from a *mention* —
    and a second copy here would be a second answer that drifts the first time
    either changed.

    The import is late and guarded, exactly as ``_fold`` guards its import of
    ``people``: this module must stay importable and testable with no
    configuration, so a missing lexicon degrades to "no clitic found" rather
    than to an import error.
    """
    try:
        from . import addressing

        return frozenset(addressing.ACTION_WORDS)
    except Exception:  # noqa: BLE001 - a missing lexicon is not a failure
        return frozenset()


def _clitic_person(token: str) -> bool:
    """Whether a token is an action verb with the 3rd-person object clitic.

    Only the «ـش» forms count. The bare stem («بن», «ساکت») is a topic word that
    may or may not have a person behind it, and treating it as a referent would
    fire on ordinary conversation; the clitic is what makes the object explicit.
    Checked against the *raw* token, because the generic clitic stripper would
    have already removed the «ش» that carries the meaning.
    """
    if len(token) < 3 or not token.endswith("ش"):
        return False
    words = _action_words()
    if token in words:
        return True
    # «بیرونش» is «بیرون» + the clitic, and the lexicon lists the bare stem.
    stem = token[:-1]
    return len(stem) >= 2 and stem in words

_TOKEN_SPLIT = re.compile(r"[^\w\u0600-\u06ff]|_")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def _bare(token: str) -> str:
    """The token with at most one attached clitic removed."""
    for clitic in _CLITICS:
        if token.endswith(clitic) and len(token) - len(clitic) >= 2:
            return token[: -len(clitic)]
    return token


@dataclass(frozen=True)
class Expression:
    """The deictic a message used, and how strongly it implies a *person*.

    ``distance`` is ``"near"``, ``"far"`` or ``""``, and it is not decoration:
    «همون» and «اون» are *anaphoric* — "that same one", the one already under
    discussion — while «این» points at whatever is nearest and may be a message,
    a config or a link. The resolver reads the difference: for an anaphoric
    expression, what the room has been about is decisive evidence; for a near
    one it is a hint among hints.
    """

    surface: str
    kind: str
    strength: int
    distance: str = ""

    def __bool__(self) -> bool:
        return bool(self.surface)

    def anaphoric(self) -> bool:
        """Whether the expression points at a person the conversation has set up.

        «همون»/«اون» are anaphoric in the strict sense — "that same one", the
        entity already under discussion — and so is the object clitic: «ساکتش
        کن» says "mute *him*", and "him" can only be somebody already on the
        table. Both are read against what the room has been about.

        «قبلی» also points backwards, but at a *position in a sequence* ("the
        previous one"), which is a different claim about the room, and a bare
        «این» may point at a message or a link rather than a person; neither is
        counted here.
        """
        return self.distance == "far" or self.kind == KIND_CLITIC


def find_expression(text: str | None) -> Expression:
    """The strongest person-pointing expression in ``text``, or an empty one.

    Scanned strongest-kind first, so «این کاربر» is reported as ``person`` rather
    than as the bare «این» it contains, and the surface form is kept so an
    operator reading a log can see what matched. ``""`` means the message points
    at nobody by these words — which is not the same as "points at nobody", only
    as "the server has no candidate from the text alone".
    """
    tokens = _tokens(text)
    if not tokens:
        return Expression("", "", 0)

    bare = [_bare(token) for token in tokens]

    def surface(index: int, extra: int = 0) -> str:
        return " ".join(tokens[index : index + extra + 1])

    # Two passes, and the split is the design rather than an accident.
    #
    # The first pass looks for the words that *name what the object is*: a
    # person noun, a role, a backwards pointer, or the object clitic on a verb.
    # Those are checked across the whole message before any bare demonstrative
    # is considered, because the kind selects which evidence the resolver may
    # use — a role expression is what makes the role candidates available — and a
    # message like «این ادمینه رو محدود کن» carries both. Reporting the bare
    # «این» there would throw away the one signal that identifies the person.
    #
    # The second pass takes the bare demonstratives, which are the weakest about
    # personhood and the most common in ordinary talk.
    for index, token in enumerate(bare):
        # person: a person noun, optionally with a demonstrative before it.
        if token in _PERSON_NOUNS:
            if index and _deictic(bare[index - 1]):
                return Expression(
                    surface(index - 1, 1), KIND_PERSON, 3, _deictic(bare[index - 1])
                )
            return Expression(tokens[index], KIND_PERSON, 3)
        # person: a demonstrative followed by a person noun.
        if _deictic(token) and index + 1 < len(bare):
            if bare[index + 1] in _PERSON_NOUNS:
                return Expression(surface(index, 1), KIND_PERSON, 3, _deictic(token))
        if token in _ROLE_WORDS:
            return Expression(tokens[index], KIND_ROLE, 2)
        if token in _PRIOR_WORDS:
            return Expression(tokens[index], KIND_PRIOR, 2)
        # The object clitic on an action verb — «ساکتش کن» says "mute him" with
        # no demonstrative at all. Weaker than a person noun, which names what
        # the object is, and stronger than a bare «این», which may point at a
        # message. When a message carries both («اینو ساکتش کن») this is the kind
        # reported, because the two agree about the referent and the clitic is
        # the more explicit about there being a person at all.
        if _clitic_person(tokens[index]):
            return Expression(tokens[index], KIND_CLITIC, 2)

    # The bare demonstratives, last because they are the weakest about
    # personhood. A separated object marker is folded into the surface so the
    # rendered block reads the way the message did.
    for index, token in enumerate(bare):
        if _deictic(token):
            extra = 0
            if index + 1 < len(bare) and bare[index + 1] in _OBJECT_MARKERS:
                extra = 1
            return Expression(surface(index, extra), KIND_DEICTIC, 1, _deictic(token))
    return Expression("", "", 0)


# ── Scoring ───────────────────────────────────────────────────────────────
# Each source is a reason to believe one person is the referent, with a weight.
# The weights are ordered by how much the evidence *is* the referent rather than
# merely correlates with it: a stated id is not a hint, and a reply edge is the
# answer.
SCORE_REPLY = 1.00        # the message is a reply to them
SCORE_STATED_ID = 0.95    # the message contains their Telegram id
SCORE_NAMED = 0.80        # the message contains their name
SCORE_ROLE = 0.70         # the message names a role they hold
SCORE_ABOUT_MAX = 0.40    # the room's recent replies have been aimed at them
SCORE_RECENT_MAX = 0.50   # they spoke just before the instruction

# An anaphoric expression — «همون کاربر», «اون», or the clitic «ـش» — points at
# the person the room has already been about, and when the room's replies have
# been aimed at one person and nobody else, that is not a hint among hints — it
# is what the word means. The score is high enough to settle the referent on its
# own, and the rule fires only on a unanimous, repeated signal (see
# ``_about_focus``): one incidental reply edge is not "what the room has been
# about".
SCORE_ABOUT_FOCUS = 0.90
ABOUT_FOCUS_MIN_REPLIES = 2

# A second, independent source adds a little: two weak signals agreeing is
# stronger than one, but never enough to overtake a strong single signal.
MULTI_SOURCE_BONUS = 0.05

# Recency bands. Coarse on purpose — a band is a fact a test can pin, and the
# difference between "two minutes ago" and "three minutes ago" is not a
# difference the referent turns on.
RECENCY_BANDS = ((120, 0.50), (600, 0.30), (1800, 0.15))

# How the verdict is read.
CONFIDENT_MIN = 0.70      # the top candidate must be at least this strong
AMBIGUOUS_MIN = 0.40      # a runner-up this strong is a real alternative
MARGIN = 0.25             # and closer than this to the top makes it ambiguous

# A cap, so a window of forty messages cannot become forty candidates. Ranked,
# so the cap drops the weakest.
CANDIDATE_LIMIT = 6


@dataclass(frozen=True)
class Candidate:
    """One person the message may mean, with the evidence for it."""

    user_id: int
    name: str
    score: float
    why: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    """What the server found. Evidence, and an honest reading of its strength."""

    expression: Expression
    candidates: tuple[Candidate, ...] = ()
    confident: bool = False
    ambiguous: bool = False

    def __bool__(self) -> bool:
        return bool(self.expression) and bool(self.candidates)

    def top(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _speakers(messages) -> dict[int, dict]:
    """The last thing each human said, newest wins. Nexus is not a candidate."""
    out: dict[int, dict] = {}
    for message in messages or ():
        if str(message.get("role") or "") == "nexus":
            continue
        user_id = int(message.get("user_id") or 0)
        if user_id:
            out[user_id] = message
    return out


def _name_hits(anchor_text: str, people: dict[int, dict]) -> dict[int, str]:
    """People whose name appears in the anchor, by folded-token overlap.

    Token overlap rather than substring, for the reason this language always
    forces: «رضا» inside «رضایی» is a different name, and a display name is often
    two words of which a message uses one. A name token of three characters or
    more that appears as a whole token in the message is the match.
    """
    text_tokens = set(_tokens(anchor_text))
    if not text_tokens:
        return {}
    out: dict[int, str] = {}
    for user_id, row in people.items():
        name = str(row.get("name") or "")
        for token in _tokens(name):
            if len(token) >= 3 and token in text_tokens:
                out[user_id] = name
                break
    return out


def _stated_id(anchor_text: str, people: dict[int, dict]) -> int:
    """A person whose Telegram id appears as a token in the message."""
    text_tokens = set(_tokens(anchor_text))
    for user_id in people:
        if str(user_id) in text_tokens:
            return user_id
    return 0


def _reply_target(anchor: dict | None) -> int:
    return int((anchor or {}).get("reply_user_id") or 0)


def _about_scores(messages) -> dict[int, float]:
    """Who the room's recent replies have been aimed at.

    The frequency of reply edges *into* a person, capped and normalised, so the
    person the conversation has been about scores highest. This is the signal
    behind «همون کاربر» — the one everybody has been answering.
    """
    counts: dict[int, int] = {}
    for message in messages or ():
        target = int(message.get("reply_user_id") or 0)
        if target:
            counts[target] = counts.get(target, 0) + 1
    if not counts:
        return {}
    top = max(counts.values())
    return {
        user_id: SCORE_ABOUT_MAX * (count / top) for user_id, count in counts.items()
    }


def _about_focus(messages) -> int:
    """The one person the room's replies have been aimed at, or 0.

    «همون کاربر» means "that same user" — the one already under discussion. When
    every reply in the window targets the same person, and there is more than one
    of them, that person *is* what the room has been about, and the anaphoric
    demonstrative points at them rather than at a set of options.

    Deliberately strict: a single reply edge is not "what the room has been
    about", and a room whose replies are split between two people is exactly the
    case where the resolver must stay unsure. Both return 0, which leaves the
    ordinary hint-scoring to decide — and to report ambiguity if it cannot.
    """
    targets = [int(message.get("reply_user_id") or 0) for message in messages or ()]
    targets = [target for target in targets if target]
    if len(targets) < ABOUT_FOCUS_MIN_REPLIES:
        return 0
    unique = set(targets)
    return next(iter(unique)) if len(unique) == 1 else 0


def _recent_scores(anchor: dict | None, people: dict[int, dict]) -> dict[int, float]:
    """Who spoke just before the instruction, by how long ago."""
    anchor_at = int((anchor or {}).get("at") or 0)
    anchor_user = int((anchor or {}).get("user_id") or 0)
    if not anchor_at:
        return {}
    out: dict[int, float] = {}
    for user_id, row in people.items():
        if user_id == anchor_user:
            continue
        at = int(row.get("at") or 0)
        if not at or at > anchor_at:
            continue
        age = anchor_at - at
        for limit, score in RECENCY_BANDS:
            if age <= limit:
                out[user_id] = max(out.get(user_id, 0.0), score)
                break
    return out


def resolve(
    anchor: dict | None,
    *,
    messages=None,
    roles: dict[int, str] | None = None,
    limit: int = CANDIDATE_LIMIT,
) -> Resolution:
    """Rank the people an anchor's deictic expression may mean.

    Returns an empty ``Resolution`` when the message points at nobody by these
    words, which is the common case and the cheap one. Otherwise every source is
    evaluated, the per-person evidence is combined, and the strength of the
    result is read against the thresholds above.

    A reply target short-circuits the verdict rather than merely scoring high:
    when the message *is* a reply, that id is the referent, and reporting it as
    ambiguous because some other person was also named would be worse than
    useless. The candidates are still all returned, so the model can see the
    alternatives — only the ``confident``/``ambiguous`` reading is settled.
    """
    expression = find_expression((anchor or {}).get("text"))
    if not expression:
        return Resolution(expression)

    people = _speakers(messages)
    if not people:
        return Resolution(expression)

    roles = dict(roles or {})
    evidence: dict[int, list[tuple[float, str]]] = {}

    def add(user_id: int, score: float, why: str) -> None:
        if user_id:
            evidence.setdefault(int(user_id), []).append((float(score), why))

    reply = _reply_target(anchor)
    if reply:
        add(reply, SCORE_REPLY, "the message is a reply to them")

    stated = _stated_id(str((anchor or {}).get("text") or ""), people)
    if stated:
        add(stated, SCORE_STATED_ID, "the message states their id")

    for user_id, name in _name_hits(
        str((anchor or {}).get("text") or ""), people
    ).items():
        add(user_id, SCORE_NAMED, f"named in the message ({name})")

    if expression.kind == KIND_ROLE:
        for user_id in people:
            role = roles.get(user_id) or str(people[user_id].get("role") or "")
            if role in ("owner", "admin"):
                add(user_id, SCORE_ROLE, f"holds the role {role}")

    for user_id, score in _about_scores(messages).items():
        add(user_id, score, "the room's recent replies have been aimed at them")

    # An anaphoric expression — «همون»/«اون», or the object clitic — points back
    # at the person the room has been about. When that person is unambiguous —
    # every reply in the window aimed at them — the pointer is settled by the
    # word itself, not merely hinted at.
    if expression.anaphoric():
        focus = _about_focus(messages)
        if focus:
            add(
                focus,
                SCORE_ABOUT_FOCUS,
                f"the room's replies have all been aimed at them, and "
                f"«{expression.surface}» points back at them",
            )

    for user_id, score in _recent_scores(anchor, people).items():
        add(user_id, score, "they spoke shortly before this message")

    scored: list[Candidate] = []
    for user_id, findings in evidence.items():
        best = max(score for score, _ in findings)
        combined = min(1.0, best + MULTI_SOURCE_BONUS * (len(findings) - 1))
        why = tuple(dict.fromkeys(reason for _, reason in findings))
        scored.append(
            Candidate(
                user_id=user_id,
                name=str(people.get(user_id, {}).get("name") or ""),
                score=round(combined, 3),
                why=why,
            )
        )
    if not scored:
        return Resolution(expression)

    # Strongest first; a tie is broken by who spoke most recently, which is the
    # only ordering the window can justify.
    scored.sort(key=lambda c: (-c.score, -int(people.get(c.user_id, {}).get("at") or 0)))
    candidates = tuple(scored[: max(1, int(limit))])

    if reply:
        return Resolution(expression, candidates, confident=True, ambiguous=False)

    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    confident = top.score >= CONFIDENT_MIN and (
        second is None or (top.score - second.score) >= MARGIN
    )
    ambiguous = (
        second is not None
        and second.score >= AMBIGUOUS_MIN
        and (top.score - second.score) < MARGIN
    )
    return Resolution(expression, candidates, confident=confident, ambiguous=ambiguous)


# ── Rendering ─────────────────────────────────────────────────────────────
def render(resolution: Resolution, *, cap: int = 900) -> str:
    """The candidate block for the model. Context, and labelled as such.

    Three shapes, and each says something different on purpose: a confident
    reading, an ambiguous one, and a found-but-unresolvable one. The ambiguous
    case is the one worth the words — it is the server telling the model that it
    could not tell two people apart, which is the honest input to "should I ask".
    """
    if not resolution.expression:
        return ""
    if not resolution.candidates:
        return (
            f"\nThe message uses «{resolution.expression.surface}», but the server "
            "found no person it could be. If it needs a person and the transcript "
            "does not make one plain, ask which — do not guess.\n"
        )

    lines = [
        f"\nWho «{resolution.expression.surface}» may mean "
        "(server-built candidates, strongest first — evidence, not a decision):"
    ]
    for candidate in resolution.candidates:
        who = candidate.name or "?"
        reasons = "; ".join(candidate.why)
        lines.append(f"- {who} ({candidate.user_id}), {candidate.score:.2f} — {reasons}")

    if resolution.confident:
        lines.append(
            "The server is confident in the first candidate. Use its id, and do "
            "not substitute a name from an earlier exchange.\n"
        )
    elif resolution.ambiguous:
        lines.append(
            "The server could not tell the top candidates apart. If you must act "
            "on a person, ask which one is meant rather than choosing.\n"
        )
    else:
        lines.append(
            "The server is not confident. Choose only if the transcript makes it "
            "plain; otherwise ask.\n"
        )
    return _clip("\n".join(lines) + "\n", cap)


def _clip(text: str, cap: int) -> str:
    """Bound the block, on a line boundary where there is one."""
    if cap <= 0 or len(text) <= cap:
        return text if cap > 0 else ""
    room = max(1, cap - 1)
    cut = text.rfind("\n", 0, room)
    if cut <= 0:
        cut = room
    return text[:cut].rstrip() + "\n"


__all__ = [
    "Candidate",
    "Expression",
    "Resolution",
    "find_expression",
    "render",
    "resolve",
]
