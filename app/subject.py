"""What the room is talking about — and whether it is talking about Nexus.

The Awareness layer has always read a batch of messages and asked the *model*
whether any of it concerned the assistant. That judgement is good, and it stays
where it is. What it never had was a server-side reading of the conversation's
**subject**: a plain, deterministic answer to "who or what are these people
currently talking about, and is it us?" that could be stated to the model as
fact rather than left for it to reconstruct from a transcript on every pass.

This module is that reading. It walks the recent window oldest-first and follows
the conversation's subject the way a person who joined late would: a call to
Nexus makes Nexus the subject, a reply to a Nexus message keeps it there, the
assistant's name coming up in somebody's sentence is about it, a reply to
somebody else makes *them* the subject, and a turn that only agrees or points
back with «این» continues whatever was already being discussed.

Why this is not a keyword detector
----------------------------------
The distinction the brief draws is the one this module is built around. A generic
description of an assistant — «ربات», «هوش مصنوعی», «the bot», «the AI» — is a
*candidate referent*, not a trigger. It can never make Nexus the subject on its
own, because a room may be discussing bots in general, or another bot entirely,
and the word is identical in all three cases. What makes the candidate point at
Nexus is the thing beside it:

* a **deictic** — «این ربات» (this bot) points at a bot that is present, and the
  assistant is the bot present in this room. «رباتهای تلگرام» (Telegram's bots,
  plural, generic) points at a category and is deliberately read as general.
* an **established subject** — a bare «این یکی» or «خودش» or an agreement has no
  referent of its own, and it continues the subject the conversation already
  established. With no subject established it means nothing, and this module says
  so rather than guessing.

So the generic words are a vocabulary of *descriptions*, and the reference — the
deictic or the continuity — is what binds them. A room that invents a new slang
word for the assistant is still reachable through the reply edge, the call, or a
bare demonstrative that continues an established subject, none of which needs the
word to be on any list.

What it is not
--------------
It is **evidence, never a gate**. Nothing in this module decides whether Nexus
speaks, whether anything is authorised, or which message id is used. It reads a
window it was handed, invents no identifier — ``subject_user_id`` can only be a
value already stored on a row's reply edge — and returns a plain value. The
model's judgement and the server's controls remain exactly where they were; this
is the reading they are given.

It is pure and fail-soft: no database handle, no model, no clock, and a reader
that raises contributes nothing rather than failing a pass.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── The subject vocabulary ────────────────────────────────────────────────
# A closed set, because a classification nobody can enumerate is not one. The
# first three are "Nexus is the subject" in descending strength of evidence; the
# last three are the ways it is not.
DIRECT = "direct"      # somebody called Nexus («نکسوس اینو دیدی؟»)
IMPLICIT = "implicit"  # somebody replied to a Nexus message
ABOUT = "about"        # the room is discussing Nexus without calling it
OTHER = "other"        # the room is about another person
GENERAL = "general"    # a general discussion (bots, AI, anything) — not Nexus
NONE = "none"          # nothing established

KINDS = (DIRECT, IMPLICIT, ABOUT, OTHER, GENERAL, NONE)

# The three that mean "the subject is Nexus".
NEXUS_KINDS = (DIRECT, IMPLICIT, ABOUT)

# The confidence each signal starts from. A call is the strongest evidence there
# is, a reply edge is authoritative about what the person was answering, and the
# name appearing is real but weaker — «نکسوس گفت که...» is about Nexus without
# being to it.
_CONF = {
    DIRECT: 95,
    IMPLICIT: 88,
    ABOUT: 78,
    # A deictic generic description («این ربات») points at the assistant present
    # in the room. Strong enough to establish the subject, weaker than a call.
    "deictic-noun": 72,
    OTHER: 65,
}

# Each turn that merely *continues* the subject without a fresh signal costs a
# little confidence, and the floor is where a stale subject stops being claimed.
# Both are small on purpose: the reading is about a conversation of a few turns,
# and a subject that has survived ten turns of agreement is still the subject.
_CONTINUATION_PENALTY = 6
_CONF_FLOOR = 45

# The same penalty, larger, for a subject carried in from the previous pass —
# the window usually still contains the establishing message, so this only
# matters when it has aged out, and then the reading should be cautious.
_PASS_PENALTY = 12

# ── The vocabulary of reference ───────────────────────────────────────────
# Demonstratives and pronouns that point without naming. The fold maps «آ»→«ا»
# and turns the zero-width joiner into a space, so «اینیکی» arrives as two
# tokens; the bare-demonstrative test is therefore a token test.
_DEMONSTRATIVES = frozenset(
    {
        "این", "اینو", "اینرو", "اینا", "همین", "همینو", "همینرو",
        "اون", "اونو", "اونرو", "اونها", "همون", "همونو", "همونرو",
        "اینجوری", "اینطوری", "اونجوری", "اونطوری", "اینگونه", "اونگونه",
        "خودش", "خودشون", "خودشو",
    }
)

# Generic descriptions of an assistant. **Not triggers.** Each one is only ever
# a candidate; see the module docstring for what has to sit beside it to point at
# Nexus. The list is folded spellings.
_ASSISTANT_NOUNS = (
    "ربات", "بات", "دستیار", "هوش مصنوعی", "هوشمصنوعی", "اسیستنت",
    "assistant", "ai", "bot", "chatbot",
)

# What makes a description generic rather than deictic: a plural, or a scope word
# that names a category instead of the thing in the room. «رباتهای تلگرام» and
# «رباتها» are a discussion about bots; «این ربات» is this one.
#
# The plural may arrive glued («رباتها») or separated, because the shared fold
# turns the zero-width joiner into a space — «هوش مصنوعیها» folds to «هوش مصنوعی
# ها» — so both spellings are tested.
_PLURAL_MARKERS = ("ها", "های", "هایی", "هات")
_GENERIC_SCOPES = ("تلگرام", "بازار", "دنیا", "همه", "اکثر", "بعضی", "چندتا")

# Turns that continue the subject without restating it. Pure agreement, or a
# short turn that refers back — a demonstrative, a possessive, or a third-person
# report of what somebody (the subject) said or did. These never *establish* a
# subject; they only carry one forward, which is why they are safe as a
# continuation signal and would not be safe as a trigger.
_CONTINUATION_TOKENS = frozenset(
    {
        "اره", "آره", "بله", "دقیقا", "دقیقاً", "درسته", "موافقم", "همینه",
        "واقعا", "واقعاً", "صحیح", "اوکی", "اکی", "خب", "خیلی", "چه", "که",
        "هم", "و", "بابا", "دمت", "گرم", "جدا", "کاملا", "حرفت", "حرفتو",
        # Reactions, which add an opinion and no subject: «آره، خیلی عجیبه» is
        # agreement with what came before, not a new topic.
        "عجیبه", "عجیب", "جالبه", "جالب", "باحاله", "باحال", "عالیه", "عالی",
        "قشنگه", "قشنگ", "خفنه", "خفن", "توپه", "توپ", "حیرت", "عجب", "وای",
        "بده", "خوبه", "درسته", "صددرصد", "دقیقه",
    }
)
_CONTINUATION_VERBS = (
    "گفته", "میگه", "میگفت", "گفت", "جواب", "فهمید", "بلده", "رفتار", "کرد",
    "میکنه", "میزنه", "میفهمه",
)
_POSSESSIVE_STEMS = (
    "حرف", "پیام", "جواب", "نظر", "کار", "رفتار", "کلام", "سخن", "قول",
)

# How many content tokens the evidence line may carry. Bounded because it is
# context, and a list of words is not a topic.
_TOPIC_TOKENS = 8


def _fold(text: str | None) -> str:
    """The shared fold, borrowed so a name and a description match the same way."""
    try:
        from . import people

        return people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold is never worth a failure
        return " ".join(str(text or "").casefold().split())


def _tokens(text: str | None) -> list[str]:
    return _fold(text).split()


def _has_letters(token: str) -> bool:
    return any(ch.isalpha() for ch in token)


def _word(token: str) -> str:
    """A token reduced to its letters, so trailing punctuation cannot hide it.

    «آره،» and «آره» are the same word to a reader, and a fold that leaves the
    comma attached would make every vocabulary in this module miss the commonest
    way people write it.
    """
    return "".join(ch for ch in token if ch.isalpha())


def _has_demonstrative(text: str | None) -> bool:
    """Whether the message points with a demonstrative or a bare pronoun.

    ``entities.has_demonstrative`` is the house lexicon and is consulted first;
    the local list is OR'd in because this reading also cares about the pronoun
    forms («خودش», «اینجوری») that the thing-pointer lexicon has no reason to
    carry. Both are cheap token scans.
    """
    if any(token in _DEMONSTRATIVES for token in _tokens(text)):
        return True
    try:
        from . import entities

        return bool(entities.has_demonstrative(text))
    except Exception:  # noqa: BLE001 - a pointer we cannot read is not one
        return False


def _assistant_noun(folded: str) -> str:
    """The generic assistant description the message uses, or ``""``."""
    for noun in _ASSISTANT_NOUNS:
        if noun in folded:
            return noun
    return ""


def _generic_plural(folded: str) -> bool:
    """Whether an assistant description is generic rather than deictic.

    A plural («رباتها», «رباتهای تلگرام») or a scope word names a category, and a
    category is not the assistant. This is the clause that keeps «رباتهای تلگرام
    چطور کار میکنند؟» out of the Nexus subject.
    """
    for noun in _ASSISTANT_NOUNS:
        for marker in _PLURAL_MARKERS:
            if noun + marker in folded or noun + " " + marker in folded:
                return True
        if noun in folded and any(scope in folded for scope in _GENERIC_SCOPES):
            return True
    return False


def _deictic_noun(folded: str, tokens: list[str]) -> bool:
    """Whether a demonstrative points at an assistant description: «این ربات».

    The noun has to come *after* the demonstrative and within two tokens of it,
    so «این ربات» and «این هوش مصنوعی» match while a message that happens to
    contain both words in unrelated positions does not.
    """
    for index, token in enumerate(tokens):
        if token not in _DEMONSTRATIVES:
            continue
        window = " ".join(tokens[index + 1 : index + 3])
        if not window:
            continue
        for noun in _ASSISTANT_NOUNS:
            if window == noun or window.startswith(noun + " ") or window.startswith(noun):
                return True
    return False


def _self_reference(text: str | None) -> str:
    """How the message describes the assistant, or ``""``.

    ``"deictic-noun"`` (this bot), ``"deictic"`` (a bare pronoun with no noun) or
    ``"generic"`` (a description with no demonstrative). The distinction is the
    whole non-keyword property: only the first can *establish* Nexus as the
    subject, the second only continues one, and the third never does either.
    """
    folded = _fold(text)
    if not folded:
        return ""
    tokens = folded.split()
    if _assistant_noun(folded):
        if _deictic_noun(folded, tokens):
            return "deictic-noun"
        if _generic_plural(folded):
            return "generic"
        return "generic"
    if _has_demonstrative(text):
        return "deictic"
    return ""


def _possessive(folded: str) -> bool:
    """Whether the message carries a possessive back-reference («حرفش»)."""
    for token in folded.split():
        token = _word(token)
        for stem in _POSSESSIVE_STEMS:
            if token in (stem + "ش", stem + "شو", stem + "شه", stem + "هش"):
                return True
    return False


def _names_nexus(token: str) -> bool:
    """Whether a token is the assistant's name — a call, not content."""
    try:
        from . import addressing

        return bool(addressing.is_name(token))
    except Exception:  # noqa: BLE001 - a name we cannot read is not content
        return False


def _continuation(text: str | None) -> bool:
    """Whether the turn continues the established subject rather than shifting.

    Two shapes, and both are deliberately unable to *establish* anything: a turn
    made only of agreement (or a reaction, which adds an opinion and no subject),
    and a short turn that refers back (a demonstrative, a possessive, or a
    third-person report of what the subject said or did). A long message, or one
    with no back-reference, is a new subject and resets the reading — that is the
    difference between a conversation and a keyword match.

    The assistant's own name is dropped before any of this: «نکسوس آره دقیقاً» is
    a call plus agreement, and the call is not content of its own.
    """
    tokens = [_word(token) for token in _tokens(text)]
    tokens = [token for token in tokens if token and not _names_nexus(token)]
    if not tokens:
        return False
    if any(token in _CONTINUATION_TOKENS for token in tokens) and all(
        token in _CONTINUATION_TOKENS for token in tokens
    ):
        return True
    if len(tokens) > 8:
        return False
    folded = _fold(text)
    if _has_demonstrative(text) or _possessive(folded):
        return True
    return any(token in _CONTINUATION_VERBS for token in tokens)


def _topic_tokens(messages) -> tuple[str, ...]:
    """A short list of the content words in the window, for the evidence line."""
    seen: list[str] = []
    for row in messages:
        for token in _tokens((row or {}).get("text")):
            if len(token) < 3 or token in _CONTINUATION_TOKENS:
                continue
            if token not in seen:
                seen.append(token)
            if len(seen) >= _TOPIC_TOKENS:
                return tuple(seen)
    return tuple(seen)


# ── The reading ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Subject:
    """What the room is talking about, and how sure the server is.

    ``kind`` is the classification, ``confidence`` is how much evidence stands
    behind it (0–100), and ``subject_user_id``/``subject_name`` name the person
    when the subject is a person rather than Nexus. ``message_id`` is the message
    that most recently established or continued the subject — the conversational
    turn the reading is about — and ``evidence`` is the chain, for the model and
    for a log line.

    A plain value: no ids are invented, nothing is acted on, and ``is_nexus`` is
    a derived convenience rather than a second source of truth.
    """

    kind: str = NONE
    confidence: int = 0
    subject_user_id: int = 0
    subject_name: str = ""
    message_id: int = 0
    evidence: tuple[str, ...] = ()

    @property
    def is_nexus(self) -> bool:
        return self.kind in NEXUS_KINDS

    def __bool__(self) -> bool:
        return self.kind != NONE or bool(self.subject_user_id)


def _seed(previous) -> tuple[str, int, int, str, int]:
    """The subject carried in from the last pass, decayed. Never raises."""
    if not previous:
        return NONE, 0, 0, "", 0
    try:
        kind = str(previous.get("subject_kind") or NONE)
        confidence = int(previous.get("subject_confidence") or 0)
        user_id = int(previous.get("subject_user_id") or 0)
        name = str(previous.get("subject_name") or "")
        message_id = int(previous.get("subject_message_id") or 0)
    except Exception:  # noqa: BLE001 - a stale reading is not a failure
        return NONE, 0, 0, "", 0
    if kind not in KINDS or kind == NONE:
        return NONE, 0, 0, "", 0
    confidence = max(0, confidence - _PASS_PENALTY)
    if confidence < _CONF_FLOOR:
        return NONE, 0, 0, "", 0
    return kind, confidence, user_id, name, message_id


def read_subject(
    chat_id: int = 0,
    messages=(),
    *,
    bot_id: int = 0,
    bot_username: str = "",
    previous=None,
) -> Subject:
    """Read the room's window into a subject. Pure, deterministic, fail-soft.

    The order of the tests is the order of the evidence: a call beats a reply
    edge, a reply edge beats the name appearing, the name appearing beats a reply
    to somebody else, and any of those beats a continuation. ``previous`` seeds
    the walk so a subject survives a window that has aged past its establishing
    message; the window is the authority and always overrides it.
    """
    rows = [row for row in (messages or ()) if isinstance(row, dict)]
    kind, confidence, subject_user_id, subject_name, message_id = _seed(previous)
    evidence: list[str] = []

    for row in rows:
        role = str(row.get("role") or "")
        if role == "nexus":
            # The assistant speaking does not make the room about the assistant —
            # it answered a question about VPNs, and the room is still about
            # VPNs. It does keep a Nexus subject warm for the turns that follow,
            # because a conversation after an answer is often about the answer.
            continue

        mid = int(row.get("message_id") or 0)
        text = row.get("text") or ""
        reply_user_id = int(row.get("reply_user_id") or 0)
        reply_name = str(row.get("reply_name") or "").strip()

        # 1. A call. The strongest evidence there is.
        if row.get("directed"):
            kind, confidence, message_id = DIRECT, _CONF[DIRECT], mid
            subject_user_id, subject_name = 0, ""
            evidence.append(f"message {mid} called Nexus")
            continue

        # 2. A reply to a Nexus message. Telegram's own edge, so it is a fact.
        if bot_id and reply_user_id == int(bot_id):
            kind, confidence, message_id = IMPLICIT, _CONF[IMPLICIT], mid
            subject_user_id, subject_name = 0, ""
            evidence.append(f"message {mid} replies to a Nexus message")
            continue

        # 3. The name came up without a call: «نکسوس گفت که...».
        if _mentioned(text):
            kind, confidence, message_id = ABOUT, _CONF[ABOUT], mid
            subject_user_id, subject_name = 0, ""
            evidence.append(f"message {mid} mentions Nexus without calling it")
            continue

        # 4. A reply to somebody else: the room is about that person now.
        if reply_user_id:
            kind, confidence, message_id = OTHER, _CONF[OTHER], mid
            subject_user_id, subject_name = reply_user_id, reply_name
            evidence.append(
                f"message {mid} replies to {reply_name or '?'} ({reply_user_id})"
            )
            continue

        # 5. A description of the assistant, read by what sits beside it.
        reference = _self_reference(text)
        if reference == "generic" and _generic_plural(_fold(text)):
            # A category, not the assistant. «رباتهای تلگرام» is a discussion
            # about bots; it is general even if Nexus was the subject before.
            kind, confidence, message_id = GENERAL, 0, mid
            subject_user_id, subject_name = 0, ""
            evidence.append(f"message {mid} discusses assistants in general")
            continue
        if reference:
            if kind in NEXUS_KINDS:
                # A turn that only refers back is the room *discussing* Nexus,
                # not addressing it — the call or the reply edge was the earlier
                # turn. Demoting the kind keeps the label the model reads honest
                # while the subject stays Nexus.
                kind = ABOUT
                confidence = max(_CONF_FLOOR, confidence - _CONTINUATION_PENALTY)
                message_id = mid
                evidence.append(
                    f"message {mid} refers back ({reference}); the subject was Nexus"
                )
                continue
            if kind in (GENERAL, OTHER):
                # The demonstrative binds to the referent the room already
                # established: «همون هوش مصنوعی» in a discussion about bots
                # continues *that* discussion, it does not jump to the assistant
                # merely because one is present. This is the clause that keeps a
                # deictic from overriding an established non-Nexus subject.
                confidence = max(_CONF_FLOOR, confidence - _CONTINUATION_PENALTY)
                message_id = mid
                continue
            if reference == "deictic-noun":
                # «این ربات» — a deictic pointing at the assistant present here,
                # with nothing else established for it to bind to.
                kind, confidence, message_id = ABOUT, _CONF["deictic-noun"], mid
                subject_user_id, subject_name = 0, ""
                evidence.append(f"message {mid} points at the assistant ({reference})")
                continue
            # A bare pronoun or a generic description with no established subject
            # is exactly the ambiguity this module refuses to resolve by picking.
            kind, confidence, message_id = NONE, 0, mid
            subject_user_id, subject_name = 0, ""
            continue

        # 6. No reference at all: does the turn continue, or is it a new subject?
        if kind in NEXUS_KINDS and _continuation(text):
            kind = ABOUT
            confidence = max(_CONF_FLOOR, confidence - _CONTINUATION_PENALTY)
            message_id = mid
            evidence.append(f"message {mid} continues the subject")
            continue

        kind, confidence, message_id = NONE, 0, mid
        subject_user_id, subject_name = 0, ""

    return Subject(
        kind=kind,
        confidence=max(0, min(100, int(confidence))),
        subject_user_id=subject_user_id,
        subject_name=subject_name,
        message_id=message_id,
        evidence=tuple(evidence[-6:]),
    )


def _mentioned(text: str | None) -> bool:
    """Whether Nexus's name came up at all — the weak grade, from the matcher."""
    if not text:
        return False
    try:
        from . import addressing

        return bool(addressing.mentioned(text))
    except Exception:  # noqa: BLE001 - a matcher we cannot read is not evidence
        return False


# ── Rendering ─────────────────────────────────────────────────────────────
# The label the model reads for each classification. Written as the server's own
# reading, never as an instruction, and never as a claim about what to do.
_KIND_LABEL = {
    DIRECT: "the room is addressing Nexus directly",
    IMPLICIT: "the room is replying to Nexus",
    ABOUT: "the room is discussing Nexus without addressing it",
    OTHER: "the room is about another person",
    GENERAL: "the room is a general discussion, not about Nexus",
    NONE: "no subject has been established",
}

RENDER_CAP = 420


def render(subject: Subject, *, cap: int = RENDER_CAP) -> str:
    """The subject reading, as a block for the model. ``""`` when there is none.

    Stated as evidence with its confidence, so the model can weigh it against the
    transcript rather than obey it. It names no id the server did not already
    hold, and it never tells the model to speak — the decision stays with the
    model and the participation gate.
    """
    if not subject or subject.kind == NONE:
        return ""
    lines = ["\n── What this conversation is about (read by the server) ──\n"]
    lines.append(
        f"The server reads the recent conversation as: "
        f"{_KIND_LABEL.get(subject.kind, subject.kind)} "
        f"(confidence {subject.confidence}/100).\n"
    )
    if subject.subject_user_id:
        lines.append(
            f"The person it is about is {subject.subject_name or '?'} "
            f"({subject.subject_user_id}).\n"
        )
    if subject.evidence:
        lines.append("Why: " + "; ".join(subject.evidence) + ".\n")
    lines.append(
        "That is a reading of the conversation, not an instruction: if the "
        "messages below have moved on, follow them.\n"
    )
    text = "".join(lines)
    if cap > 0 and len(text) > cap:
        cut = text.rfind("\n", 0, max(1, cap - 1))
        text = text[: cut if cut > 0 else cap].rstrip() + "\n"
    return text


def destination(subject: Subject, messages, *, bot_id: int = 0) -> int:
    """The message an ambient reply should quote, from the stored window.

    The conversational turn the reading is about, when it is a real message in
    the window and it is not the assistant's own — quoting yourself is a loop
    nobody asked for. ``0`` means "no destination", which is the honest answer
    for an ambient reply with no single turn to attach to. The id can only be one
    already stored on a row, so the model has no part in choosing it.
    """
    if not subject or not subject.message_id:
        return 0
    for row in messages or ():
        if not isinstance(row, dict):
            continue
        if int(row.get("message_id") or 0) != int(subject.message_id):
            continue
        if str(row.get("role") or "") == "nexus":
            return 0
        return int(subject.message_id)
    return 0


__all__ = [
    "ABOUT",
    "DIRECT",
    "GENERAL",
    "IMPLICIT",
    "KINDS",
    "NEXUS_KINDS",
    "NONE",
    "OTHER",
    "RENDER_CAP",
    "Subject",
    "destination",
    "read_subject",
    "render",
]
