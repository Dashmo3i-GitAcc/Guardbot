"""Increment Y — the minimum relevant combination of context for one chat turn.

The problem this module exists for
----------------------------------
An addressed reply is assembled from four *independent* context sources:

* **Conversation** — what was recently said. The assistant's own bounded
  transcript with this person, read by ``chat.reply`` itself.
* **Awareness** — what is happening around Nexus now. The room window and the
  server's reading of the message being answered, both room-scoped and both
  gated on the awareness switch.
* **State** — what the current interaction is trying to accomplish
  (``app/state.py``).
* **Memory** — what is worth remembering about this person
  (``app/memory.py``).

The four are different concepts and must stay different concepts. What was
missing was not another source but a *decision*: given this message, which of
the four is actually useful? The old addressed path answered "all of them,
always": every reply carried the room window, the whole reading, the memory
block and the state block whether or not the message needed any of it.

This module is that decision, and nothing else. It is deterministic, it makes
no model call, it reads no database and it stores nothing. It answers one
question — *what is the minimum sufficient context for this turn* — and the
answer is expressed as a :class:`ContextPlan`.

The boundary it must not cross
------------------------------
There is deliberately **no** ``UniversalContext`` here and no context database.
The plan holds no data of its own: the sources are read by the readers that
already exist, each behind its own switch and its own fail-soft behaviour, and
the plan only *selects*, *orders*, *de-duplicates* and *bounds* what they
returned. Removing this module would leave every source working exactly as it
does now; it would only put the room window back on every reply.

The reading, and the fast path
------------------------------
:func:`read` is a pure function of the message's own shape. It asks the scored
readers the project already has — ``referents.find_expression`` for an anaphor,
``discourse.read_act`` for a correction or an instruction, ``state.read`` for a
state transition — plus its own small patterns for a back-reference and an
opinion request («نظرت چیه؟», whose subject is the room's recent content), plus
two structural facts the handler already holds: whether the message is a
**reply**, and whether it carries **media**. Any of those is evidence that the
message depends on something outside itself, and the answer is then the *full*
path: the room is worth reading.

A message with none of those signals is *self-contained*, and the room window —
the one source whose cost is measured in hundreds of tokens — is omitted. That
is the fast path, and it is deliberately conservative in one direction only:
when the evidence is ambiguous the answer is the full path. Short is not the
same as simple — «همونو بزن» is four characters and needs the room, while a
two-sentence question with its own subject needs none of it — so the reading
never treats brevity alone as self-containment. The one short case it does call
dependent is a message with no content word at all («چی؟»), because there is
nothing in it to answer.

Precedence, freshness and de-duplication
----------------------------------------
The sources answer different questions, so there is no single ranking. What
this module does instead is per-conflict, and each rule is a refusal:

* **A fresh statement beats a stored fact.** A message read as a *correction*
  drops the memory lines that share a word with it — the person is fixing what
  the server has, and showing the old value beside the new one is how a model
  contradicts the person it is talking to.
* **A fresh statement beats a stored task.** State is asked for by its own
  reader (``state.current``), which withholds a stale task and one the message
  supersedes. This module does not re-implement that; it relies on it.
* **The room is not the person.** Awareness is selected only when the message
  depends on the room; a private chat has no room window to select, so a
  private intent can never drag a group's people into the answer.
* **A repeated fact is sent once.** A memory line whose words are already in
  the room context is dropped, and the state block is dropped when the room
  already states its topic. The test is conservative — every meaningful word of
  the lower-precedence block must already be present — so a block that adds one
  new word survives. Different sources carrying different *information* are
  never collapsed.

Bounding
--------
Every source is bounded by its own reader (``NEXUS_MEMORY_CHARS``,
``NEXUS_STATE_CHARS``, ``NEXUS_AWARENESS_CONTEXT_CHARS``,
``NEXUS_AWARENESS_WINDOW_CHARS``). On top of that, ``compose`` enforces
``NEXUS_CONTEXT_CHARS`` on the four **selectable** sources together by
**dropping whole sources** in reverse precedence — memory first, then state,
then the room window — never by slicing a rendered block in half. A source that
cannot fit is omitted rather than shown as a misleading fragment.

The administrative roster, the room's name memory, the server date and the web
findings are outside that budget, because they are never dropped: counting a
source the ceiling cannot remove would let a large roster push the limit past the
point where anything is left to drop, and the only thing that would happen is the
room being stripped out of an answer that needs it.

What this module may never do
-----------------------------
It is a reader of readers. It grants nothing: nothing in ``app/rbac.py`` or
``app/admin_service.py`` imports it, no decision it makes reaches a permission
check, and the text it composes is *data* in the system instruction, exactly as
the blocks it selects already were. A memory cannot become a permission by
being selected, and a role rendered in the room context is a sentence for the
model to read rather than an authority the server will honour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config, discourse, people, referents, state as state_module

log = logging.getLogger("guardbot.context")

# ── The four sources, as names ────────────────────────────────────────────
CONVERSATION = "conversation"
AWARENESS = "awareness"
STATE = "state"
MEMORY = "memory"
SOURCES: tuple[str, ...] = (CONVERSATION, AWARENESS, STATE, MEMORY)

# ── The two paths ─────────────────────────────────────────────────────────
FAST = "fast"
FULL = "full"

# ── The closed reason vocabulary ──────────────────────────────────────────
# Why a source was selected or omitted. A closed set so a diagnostic can be
# counted and asserted rather than merely read.
R_CONVERSATION = "conversation"          # always: the assistant's own transcript
R_STANDALONE = "standalone"              # the message stands on its own
R_REPLY = "reply"                        # it answers a specific message
R_ANAPHORA = "anaphora"                  # it points back at what was said
R_DEICTIC = "deictic"                    # it points at something present
R_BACKREFERENCE = "backreference"        # "what happened then?", "again"
R_OPINION = "opinion"                    # "what do you think?" — about the room
R_CORRECTION = "correction"              # it fixes something said
R_INSTRUCTION = "instruction"            # it asks for an action
R_MEDIA = "media"                        # an attachment needs the room
R_SUPERSEDE = "supersede"                # it ends or drops the current task
R_CONTINUATION = "continuation"          # it carries the current task on
R_ACTIVATE = "activate"                  # it starts a task
R_SHORT = "short"                        # no content word to answer
R_TRIVIAL = "trivial"                    # a greeting, acknowledgement, reaction
R_RELEVANT = "relevant"                  # the source rendered something
R_NOT_RELEVANT = "not_relevant"          # it rendered nothing
R_NO_ROOM = "no_room_dependency"         # the message needs no room context
R_DISABLED = "disabled"                  # its switch is off
R_DUPLICATE = "duplicate"                # already carried by a higher source
R_CEILING = "ceiling"                    # dropped to stay under the ceiling
R_UNAVAILABLE = "unavailable"            # its reader failed

# The reasons that make a turn depend on the **room** rather than only on the
# person's own task. A task starting, continuing or being dropped is not in this
# set: that is the person's own thread with Nexus, carried by the transcript and
# the state, and pulling the room's chatter in for it is exactly the "old
# conversation drags the answer backwards" failure the brief names. An
# instruction *is* here, because resolving who or what it means is what the
# room reading is for.
_ROOM_REASONS = frozenset(
    {
        R_REPLY,
        R_ANAPHORA,
        R_DEICTIC,
        R_BACKREFERENCE,
        R_OPINION,
        R_CORRECTION,
        R_INSTRUCTION,
        R_MEDIA,
        R_SHORT,
    }
)

# A message that is only one of these is a greeting, an acknowledgement or a
# reaction: it stands alone, and neither the room nor the person's stored
# preferences have anything to add to it.
_STANDALONE_WORDS = frozenset(
    {
        "سلام", "سلامعلیکم", "سلامعلیکم", "درود", "صبحبخیر", "شببخیر",
        "ممنون", "ممنونم", "مرسی", "سپاس", "قربونت", "دستت درد نکنه",
        "آره", "اره", "بله", "آرهه", "نه", "نخیر", "خیر", "باشه", "باشهه",
        "اوکی", "اوکیه", "ok", "okay", "okey", "k", "حتما", "چشم", "خب",
        "thanks", "thank", "thx", "ty", "hi", "hello", "hey", "yo",
    }
)

# The interrogatives, borrowed from the reader that already owns the list so a
# word added there is a word added here. Guarded: a missing list degrades to
# the smaller vocabulary below rather than to an import error.
def _interrogatives() -> frozenset[str]:
    try:
        return frozenset(discourse._QUESTION_WORDS)  # noqa: SLF001 - borrowed on purpose
    except Exception:  # noqa: BLE001 - a lexicon is never worth a crash
        return frozenset({"چی", "چیه", "چرا", "کجا", "کی", "چند", "چطور", "آیا"})


# Function words and interrogatives do not make a message self-contained on
# their own; a content word does. Used only by the short-message rule below.
_FUNCTION_WORDS = frozenset(
    {
        "و", "با", "به", "از", "در", "که", "رو", "را", "یا", "هم", "این",
        "اون", "آن", "من", "تو", "ما", "شما", "هست", "است", "بود", "نه",
        "بله", "آره", "باشه", "خب", "پس", "دیگه", "الان", "یه", "یک", "برا",
        "برای", "میشه", "میخوام", "میخوای", "میگم", "بگو", "بگو", "کاش",
    }
)

# An explicit back-reference: a phrase whose meaning is not in the phrase. The
# state reader already owns the "carry on with the task" vocabulary; this is the
# smaller set of "the thing we were talking about" phrases it does not.
_BACKREFERENCE_RE = None


def _backreference():
    """The compiled back-reference pattern, built once and lazily."""
    global _BACKREFERENCE_RE
    if _BACKREFERENCE_RE is None:
        import re

        _BACKREFERENCE_RE = re.compile(
            r"پس\s*(?:چی|یعنی|چقد|چقدر)"
            r"|چی\s*شد"
            r"|بعدش"
            r"|دوباره"
            r"|منظورت"
            r"|منظورم"
            r"|جوابت"
            r"|حرفت"
            r"|گفتی"
            r"|\b(?:again|what\s+happened|you\s+said)\b",
            re.IGNORECASE | re.UNICODE,
        )
    return _BACKREFERENCE_RE


# An opinion request — «نظرت چیه؟», «تو چی فکر میکنی؟», "what do you think?" —
# asks what Nexus makes of something, and the something is the room's recent
# content. It has no subject of its own, so it depends on the room exactly as a
# back-reference does; the possessive «نظرت» is why the short-message rule alone
# misses it (it reads «نظرت» as a content word).
_OPINION_RE = None


def _opinion():
    global _OPINION_RE
    if _OPINION_RE is None:
        import re

        _OPINION_RE = re.compile(
            r"(?:نظر|فکر|عقیده|ایده|دیدگاه|برداشت|حس)ت"
            r"|(?:چی|چه)\s*(?:فکر|نظر)"
            r"|\b(?:your\s+(?:opinion|thoughts?|take|view|idea)"
            r"|what\s+do\s+you\s+think"
            r"|how\s+do\s+you\s+feel)\b",
            re.IGNORECASE | re.UNICODE,
        )
    return _OPINION_RE


# The compound demonstratives — «اینطوری», «اینجوری», «اونجا», «همینطور» — which
# the referent reader does not read as a person-pointer but which still point at
# something outside the message. A closed list rather than a prefix rule, because
# «اینترنت» also begins with «این» and is a subject of its own.
_COMPOUND_DEICTIC = None


def _compound_deictic():
    global _COMPOUND_DEICTIC
    if _COMPOUND_DEICTIC is None:
        import re

        _COMPOUND_DEICTIC = re.compile(
            r"(?:^|\s)(?:این|اون|آن|همون|همین)"
            r"(?:طوری|طور|جوری|جور|جا|قد|قدر|همه|قسمت|مورد|شکل|کار)",
            re.IGNORECASE | re.UNICODE,
        )
    return _COMPOUND_DEICTIC


# A message that drops the active task — «بیخیال سرور» — supersedes whatever
# state is stored, and the fresh instruction wins. The state writer reads its own
# reset vocabulary; this is the smaller set of colloquial "never mind that"
# phrases it does not, so that a stored task cannot contaminate the new subject.
_DROP_TASK_RE = None


def _drop_task():
    global _DROP_TASK_RE
    if _DROP_TASK_RE is None:
        import re

        _DROP_TASK_RE = re.compile(
            r"بی\s*خیال"
            r"|ولش\s*کن"
            r"|رها\s*کن"
            r"|بگذر"
            r"|مهم\s*نیست"
            r"|کاریش\s*نداشته\s*باش"
            r"|\b(?:never\s+mind|forget\s+(?:that|it)|drop\s+(?:that|it))\b",
            re.IGNORECASE | re.UNICODE,
        )
    return _DROP_TASK_RE


# A first-person negation — «نه، من پایتون استفاده نمیکنم» — is the person
# correcting what the server holds about them. It is not a moderation
# correction, so ``discourse`` does not read it; this is the narrower rule the
# memory precedence needs, and it fires only when both halves are present.
_FIRST_PERSON = frozenset({"من", "منم", "منو", "خودم", "ماییم"})
_NEGATIONS = frozenset({"نه", "نیست", "نخیر", "نچ", "نیستم", "نیستن"})


def _corrects_the_person(tokens: list[str]) -> bool:
    if not any(token in _FIRST_PERSON for token in tokens):
        return False
    for token in tokens:
        if token in _NEGATIONS or token.startswith("نمی") or token.startswith("نیست"):
            return True
    return False


_WORD_RE = None


def _has_any_word(text: str) -> bool:
    """Whether the message contains a letter or a digit at all.

    A message that is only emoji («😂», «👍») has no words to depend on, so it
    is a reaction and stands alone.
    """
    global _WORD_RE
    if _WORD_RE is None:
        import re

        _WORD_RE = re.compile(r"[0-9A-Za-z\u0600-\u06ff]")
    return bool(_WORD_RE.search(str(text or "")))


# ── The reading: a pure function of the message ───────────────────────────
@dataclass(frozen=True)
class Reading:
    """What the message's own shape says about the context it depends on."""

    mode: str = FAST
    reasons: tuple[str, ...] = ()
    wants_conversation: bool = True
    wants_awareness: bool = False
    wants_state: bool = True
    wants_memory: bool = True
    trivial: bool = False

    @property
    def full(self) -> bool:
        return self.mode == FULL

    def has(self, reason: str) -> bool:
        return reason in self.reasons

    def summary(self) -> str:
        """A content-free one-line description, for a log."""
        return f"mode={self.mode} reasons={','.join(self.reasons) or '-'}"


def _tokens(text: str) -> list[str]:
    return [token for token in people.normalize(text).split() if token]


def _fold(text: str) -> str:
    """The text the local patterns match against: ZWNJ replaced by a space.

    Persian is written with and without the zero-width non-joiner — «بی‌خیال» and
    «بی خیال» are the same phrase — so folding to a space before matching is what
    makes one pattern cover both. Same fold ``app/state.py`` and ``app/memory.py``
    use.
    """
    return str(text or "").replace("\u200c", " ")


def _meaningful(text: str) -> set[str]:
    """The words worth comparing two blocks by: normalised, length three plus."""
    return {token for token in people.normalize(text).split() if len(token) >= 3}


def _is_question(text: str) -> bool:
    return "?" in text or "؟" in text


def _has_content_word(tokens: list[str]) -> bool:
    """Whether the message carries a word that could be its own subject.

    A word of three or more letters that is neither an interrogative nor a
    function word. «قیمت چنده؟» has one («قیمت»); «چی؟» has none, which is why
    the second is read as depending on the conversation and the first is not.
    """
    words = _interrogatives() | _FUNCTION_WORDS
    return any(len(token) >= 4 and token not in words for token in tokens)


def _is_standalone(text: str, tokens: list[str], act_kind: str) -> bool:
    """A greeting, an acknowledgement or a reaction: nothing to contextualise."""
    if not _has_any_word(text):
        # Emoji and nothing else. There are no words to depend on.
        return True
    if not tokens:
        return False
    if len(tokens) <= 3 and all(token in _STANDALONE_WORDS for token in tokens):
        return True
    return len(tokens) <= 2 and act_kind == discourse.ACT_SOCIAL


def read(
    text: str,
    *,
    kind: str = "",
    reply: bool = False,
    media: bool = False,
) -> Reading:
    """Read the message's context dependency. Pure, cheap and never raising.

    ``reply`` is whether the message answers another message — the strongest
    structural signal there is, and one the handler already holds.
    ``media`` is whether the turn carries an attachment; a reaction GIF or a
    voice note is read as a reaction, so it takes the room with it.
    ``kind`` is the media kind, carried for symmetry and reserved for a future
    rule that distinguishes a voice transcript from a sticker.

    A reader that raises contributes nothing: the reading falls back to the
    signals it did manage, and never fails the turn. A message with no signals
    is self-contained, which is the only case the fast path fires in.
    """
    reasons: list[str] = []
    raw = str(text or "")
    folded = _fold(raw)
    tokens = _tokens(raw)

    try:
        act = discourse.read_act(raw)
        act_kind = act.kind
    except Exception:  # noqa: BLE001 - a reader is never worth a turn
        log.exception("the act reader failed while planning context")
        act_kind = discourse.ACT_UNKNOWN

    try:
        expression = referents.find_expression(raw)
        anaphoric = bool(expression) and expression.anaphoric()
        deictic = bool(expression)
    except Exception:  # noqa: BLE001
        log.exception("the referent reader failed while planning context")
        anaphoric = False
        deictic = False

    try:
        change = state_module.read(raw)
    except Exception:  # noqa: BLE001
        log.exception("the state reader failed while planning context")
        change = None

    trivial = _is_standalone(raw, tokens, act_kind) and not media
    drops_task = bool(_drop_task().search(folded)) and not trivial

    if not trivial:
        if reply:
            reasons.append(R_REPLY)
        if anaphoric:
            reasons.append(R_ANAPHORA)
        elif deictic or _compound_deictic().search(folded):
            reasons.append(R_DEICTIC)
        if act_kind == discourse.ACT_CORRECTION or _corrects_the_person(tokens):
            reasons.append(R_CORRECTION)
        elif act_kind == discourse.ACT_INSTRUCTION:
            reasons.append(R_INSTRUCTION)
        if change:
            transition = str(change.get("transition") or "")
            if transition in (
                state_module.TRANSITION_RESET,
                state_module.TRANSITION_COMPLETE,
            ):
                reasons.append(R_SUPERSEDE)
            elif transition == state_module.TRANSITION_CONTINUE:
                reasons.append(R_CONTINUATION)
            elif transition == state_module.TRANSITION_ACTIVATE:
                reasons.append(R_ACTIVATE)
        if drops_task:
            reasons.append(R_SUPERSEDE)
        if _backreference().search(folded):
            reasons.append(R_BACKREFERENCE)
        if _is_question(raw) and _opinion().search(folded):
            reasons.append(R_OPINION)
        if media:
            reasons.append(R_MEDIA)

    if not reasons and not trivial:
        # No dependency signal. The last question is whether the message has
        # anything of its own to answer: «چی؟» does not, «قیمت چنده؟» does.
        if len(tokens) <= 2 and not _has_content_word(tokens):
            reasons.append(R_SHORT)

    if trivial:
        reasons = [R_TRIVIAL]

    unique = tuple(dict.fromkeys(reasons))
    mode = FAST if not unique or unique == (R_TRIVIAL,) else FULL
    wants_awareness = any(reason in _ROOM_REASONS for reason in unique)

    return Reading(
        mode=mode,
        reasons=unique,
        wants_conversation=True,
        wants_awareness=wants_awareness,
        # State is *asked for* by default; its own reader withholds a stale task
        # or one this message supersedes, and re-deciding that here would be a
        # second copy of the freshness rule. The one exception is a message that
        # explicitly drops the task — «بیخیال سرور» — where the fresh instruction
        # wins outright and the stored task is not even read.
        wants_state=not drops_task,
        # Memory is about the person, so it is worth retrieving for anything
        # that is not a bare acknowledgement. The reader's own relevance
        # ranking then omits what does not bear on the message.
        wants_memory=not trivial,
        trivial=trivial,
    )


# ── The plan ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """What the selector decided about one source, and why."""

    source: str
    selected: bool
    reason: str
    chars: int = 0


@dataclass(frozen=True)
class ContextPlan:
    """One turn's context decision: what was chosen, why, and what it costs.

    An application-side object. It is never rendered to the model — the model
    sees the composed ``text`` and nothing else — and it is never persisted.
    """

    mode: str
    text: str
    decisions: tuple[Decision, ...]
    chars: int
    dropped: tuple[tuple[str, str], ...] = ()
    reasons: tuple[str, ...] = ()

    def selected(self) -> tuple[str, ...]:
        return tuple(d.source for d in self.decisions if d.selected)

    def omitted(self) -> tuple[str, ...]:
        return tuple(d.source for d in self.decisions if not d.selected)

    def decision(self, source: str) -> Decision | None:
        for entry in self.decisions:
            if entry.source == source:
                return entry
        return None

    def reason(self, source: str) -> str:
        entry = self.decision(source)
        return entry.reason if entry else ""

    def summary(self) -> str:
        """A content-free line for the log: names, reasons, sizes only."""
        chosen = ",".join(self.selected()) or "-"
        dropped = ",".join(f"{name}:{why}" for name, why in self.dropped) or "-"
        return (
            f"mode={self.mode} selected={chosen} dropped={dropped} "
            f"chars={self.chars}"
        )


# The label vocabulary the state renderer uses. Parsed rather than guessed, so
# the de-duplication reads the same words the model will.
_STATE_VALUE_LABELS = ("active topic:", "active goal:", "unresolved question:")


def _state_values(block: str) -> set[str]:
    """The words a state block actually claims, without its framing."""
    values: set[str] = set()
    for line in (block or "").splitlines():
        for label in _STATE_VALUE_LABELS:
            if label in line:
                values |= _meaningful(line.split(label, 1)[1])
    return values


def _dedup_lines(block: str, against: set[str]) -> tuple[str, bool]:
    """Drop the content lines of ``block`` whose *value* is already in ``against``.

    The comparison is on the value rather than the whole line — ``programming:
    Python`` is tested as ``Python`` — because the label is the block's framing
    rather than its information: if the room already says the value, the label
    alone adds nothing.

    Conservative by construction: a line survives unless *every* meaningful word
    of its value already appears in the higher-precedence text. A line that adds
    one new word is kept, because different sources can carry different
    information and collapsing them by similarity would lose it. Matching is on
    shared words, so a fact recorded in one script and mentioned in another
    (``Python`` beside «پایتون») is not recognised as a duplicate — a known
    limitation, recorded rather than guessed at.
    """
    if not block or not against:
        return block, False
    lines = block.splitlines(keepends=True)
    header = [line for line in lines if not line.lstrip().startswith("- ")]
    content = [line for line in lines if line.lstrip().startswith("- ")]
    if not content:
        return block, False
    kept: list[str] = []
    dropped = False
    for line in content:
        body = line.lstrip()[2:]
        value = body.split(": ", 1)[1] if ": " in body else body
        words = _meaningful(value)
        if words and words <= against:
            dropped = True
            continue
        kept.append(line)
    if not kept:
        return "", dropped
    return "".join(header) + "".join(kept), dropped


def compose(
    reading: Reading,
    *,
    admin: str = "",
    target: str = "",
    people: str = "",
    room: str = "",
    awareness: str = "",
    state: str = "",
    memory: str = "",
    date: str = "",
    search: str = "",
    message: str = "",
    ceiling: int = 0,
) -> ContextPlan:
    """Assemble the selected blocks in one deterministic order, bounded.

    The blocks are already rendered by their own readers; this function only
    selects, orders, de-duplicates and bounds them, which is what keeps it pure
    and testable without a database.

    The selection is the reading's: a block the reading did not ask for is
    dropped here even if a caller rendered it, so the plan is the minimum *by
    construction* rather than by the caller's discipline. The caller still does
    not *read* an unwanted source — that is where "no duplicate retrieval" lives
    — but a plan that carried one anyway would not be the decision it claims to
    be.

    The order is fixed and is the order the sources answer their questions in:
    the administrative roster, the reply relationship (what this message is
    answering and what it points at), the room's **name memory** (who the message
    mentions), the room (its transcript, then the server's reading of the
    message), the active state, the person's memory, the server's date, and
    finally any web findings. A skipped source leaves no trace — no empty label,
    no placeholder — so the model never reads a heading for something that is not
    there.

    ``people`` is deliberately a slot of its own rather than a suffix on the room
    block. It carries names and ids, and the de-duplication below treats the room
    as the higher-precedence text: folding the roster into the room would let a
    person's name in it make an unrelated memory line look like a duplicate of
    the room and drop it. It is not selectable and is never dropped, like the
    administrative roster, the reply relationship, the date and the findings —
    and it is bounded by its own reader (``NEXUS_PEOPLE_CONTEXT_CHARS``).
    """
    room_text = str(room or "")
    awareness_text = str(awareness or "")
    state_text = str(state or "")
    memory_text = str(memory or "")
    admin_text = str(admin or "")
    target_text = str(target or "")
    people_text = str(people or "")
    date_text = str(date or "")
    search_text = str(search or "")

    dropped: list[tuple[str, str]] = []

    # The reading owns the selection. The administrative roster, the reply
    # relationship, the date and the web findings are not gated: they are not
    # among the four selectable sources — the first is a security property, the
    # second is a structural fact about the message being answered (it is not
    # retrieved, it is read from the message and Telegram's own metadata), the
    # third stops a date being invented, and the fourth is policy's to decide,
    # not the selector's.
    if not reading.wants_awareness:
        room_text = ""
        awareness_text = ""
    if not reading.wants_state:
        state_text = ""
    if not reading.wants_memory:
        memory_text = ""

    # The room is the higher-precedence text for de-duplication: it is what was
    # actually said, while a memory or a state is a derived summary of it.
    room_words = _meaningful(room_text) | _meaningful(awareness_text)

    # A correction drops the memory that shares a word with it — the person is
    # fixing the record, and the old value must not be shown beside the new one.
    if memory_text and reading.has(R_CORRECTION):
        said = _meaningful(message)
        memory_text, did = _dedup_lines(memory_text, said)
        if did:
            dropped.append((MEMORY, R_CORRECTION))

    # A memory line whose words the room already states adds nothing.
    if memory_text and room_words:
        memory_text, did = _dedup_lines(memory_text, room_words)
        if did:
            dropped.append((MEMORY, R_DUPLICATE))

    # A state whose topic the room already states adds nothing either.
    if state_text and room_words:
        values = _state_values(state_text)
        if values and values <= room_words:
            state_text = ""
            dropped.append((STATE, R_DUPLICATE))

    # The ceiling, applied by dropping whole sources in reverse precedence. It
    # bounds the four **selectable** sources only. The administrative roster,
    # the date and the web findings are never dropped — the first is a security
    # property, the second is what stops a date being invented, and the third is
    # the only reason the answer can be grounded — so counting them would make
    # the ceiling self-defeating: a roster larger than the limit would leave
    # nothing to drop and the only effect would be to strip the room out. A
    # ceiling may only bound what it can remove.
    limit = int(ceiling or config.NEXUS_CONTEXT_CHARS)

    def total() -> int:
        return len(room_text) + len(awareness_text) + len(state_text) + len(
            memory_text
        )

    while total() > limit:
        if memory_text:
            memory_text = ""
            dropped.append((MEMORY, R_CEILING))
        elif state_text:
            state_text = ""
            dropped.append((STATE, R_CEILING))
        elif room_text:
            room_text = ""
            dropped.append((AWARENESS, R_CEILING))
        elif awareness_text:
            awareness_text = ""
            dropped.append((AWARENESS, R_CEILING))
        else:
            break

    pieces: list[str] = []
    if admin_text:
        pieces.append(admin_text)
    if target_text:
        pieces.append(target_text)
    if people_text:
        pieces.append(people_text)
    if room_text:
        pieces.append(room_text)
    if awareness_text:
        pieces.append(awareness_text)
    if state_text:
        pieces.append(state_text)
    if memory_text:
        pieces.append(memory_text)
    if date_text:
        pieces.append(date_text)
    if search_text:
        pieces.append(search_text)

    awareness_chars = len(room_text) + len(awareness_text)
    if room_text or awareness_text:
        awareness_reason = R_RELEVANT
    elif not reading.wants_awareness:
        awareness_reason = R_NO_ROOM
    else:
        awareness_reason = R_NOT_RELEVANT

    # An omitted source is named by *why* it was omitted, so a log line reads
    # "state:supersede" rather than a generic "not relevant" that hides the
    # decision the reading actually made.
    if state_text:
        state_reason = R_RELEVANT
    elif not reading.wants_state and reading.has(R_SUPERSEDE):
        state_reason = R_SUPERSEDE
    else:
        state_reason = R_NOT_RELEVANT

    if memory_text:
        memory_reason = R_RELEVANT
    elif not reading.wants_memory and reading.trivial:
        memory_reason = R_TRIVIAL
    else:
        memory_reason = R_NOT_RELEVANT

    decisions = (
        Decision(CONVERSATION, True, R_CONVERSATION, 0),
        Decision(AWARENESS, bool(awareness_chars), awareness_reason, awareness_chars),
        Decision(STATE, bool(state_text), state_reason, len(state_text)),
        Decision(MEMORY, bool(memory_text), memory_reason, len(memory_text)),
    )

    text = "".join(pieces)
    return ContextPlan(
        mode=reading.mode,
        text=text,
        decisions=decisions,
        chars=len(text),
        dropped=tuple(dropped),
        reasons=reading.reasons,
    )


# The two awareness-context sources the addressed path renders itself.
#
# ``user_memory`` and ``conversation_state`` are sources in the awareness
# registry, and they reach an addressed reply through the room reading when it
# is built. Increment Y renders them separately so the selector can gate them
# and de-duplicate them against the room it selected; naming them here is what
# keeps the reading from carrying a second copy. The rest of
# ``CONVERSATION_SKIP`` — the date, the room's name, and the database-backed
# room memory — is the room's own business and the caller applies it too.
READING_OWN_SOURCES = frozenset({"user_memory", "conversation_state"})


def reading_skip() -> frozenset[str]:
    """The source names the addressed path routes to its own renderers."""
    return READING_OWN_SOURCES


def room_budget() -> int:
    """How much room window fits under the ceiling, given the other three.

    The room window is the largest and least bounded selectable source, so it is
    the one that has to yield. This reserves room for the reading, the state
    block and the memory block — the other three selectable sources — and hands
    the window what is left, bounded as it always was by
    ``NEXUS_AWARENESS_WINDOW_CHARS``. The result is never negative, and never
    below the renderer's own floor.

    It reserves nothing for the roster, the date or the findings, for the same
    reason the ceiling does not count them: they are never dropped, so they are
    not part of the budget the window shares. It is derived rather than
    configured: one number (``NEXUS_CONTEXT_CHARS``) bounds the selectable
    combination, and the split between the sources follows from the caps each
    already has.
    """
    ceiling = max(0, int(config.NEXUS_CONTEXT_CHARS))
    reserve = (
        max(0, int(config.NEXUS_AWARENESS_CONTEXT_CHARS))
        + max(0, int(config.NEXUS_STATE_CHARS))
        + max(0, int(config.NEXUS_MEMORY_CHARS))
    )
    window = max(200, int(config.NEXUS_AWARENESS_WINDOW_CHARS))
    return max(200, min(window, ceiling - reserve))


__all__ = [
    "AWARENESS",
    "CONVERSATION",
    "ContextPlan",
    "Decision",
    "FAST",
    "FULL",
    "MEMORY",
    "READING_OWN_SOURCES",
    "Reading",
    "SOURCES",
    "STATE",
    "compose",
    "read",
    "reading_skip",
    "room_budget",
]
