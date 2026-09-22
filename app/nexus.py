"""Nexus: the runtime state and the trigger policy of the conversational layer.

"Nexus" is the name this project gives to the conversational AI layer as a
**role** — natural-language understanding, context, intent and orchestration. It
is not a model, a provider or a credential. Which model answers is decided by
``app/gemini_pool.py`` and the ``GEMINI_CHAT_*`` settings; nothing in this module
names a model, and changing the model does not change anything here.

What this module owns, and what it deliberately does not:

* **It owns the state.** ``ONLINE`` or ``OFFLINE``, persisted, and read before
  any expensive work happens. A model cannot change it: the transition goes
  through ``app/admin_service.py`` like every other administrative request, and
  the permission it needs (``nexus.control``) is held by the owner and by
  nobody else.
* **It owns the trigger policy.** Who is allowed to reach Nexus at all, whether
  a message is aimed at Nexus or merely said in its presence, and whether an
  unaddressed message is worth spending a model call on.
* **It owns the observation.** An authorized administrator's message that is not
  addressed to Nexus is recorded into *that administrator's own* bounded
  conversation context and answered with silence. This is the "watch without
  replying" requirement, and it costs no AI call at all.
* **It does not own authority.** ``is_actor`` decides who may *talk* to Nexus;
  it never decides who may *do* anything. Every action a conversation produces
  is authorised again from the actor's Telegram id by ``app/admin_service.py``
  against ``app/rbac.py``. This module is not imported by either of them, so
  there is no path from here to a permission.

The three states of a message, which are the whole policy in one place::

    ordinary member            → silence, and no AI call
    administrator, unaddressed → stored as context, and no AI call
    administrator, addressed   → a conversation, with tools if they hold them

The gate order is the requirement, not an implementation detail: identity, then
role, then state, then relevance, and only then the model. Every step before the
last one is a dictionary lookup, which is what keeps a 3000-member room from
spending the conversational allowance on traffic that was never going to be
answered.
"""
from __future__ import annotations

import logging
import re

from . import addressing, config, db, rbac

log = logging.getLogger("guardbot.nexus")

# ── State ─────────────────────────────────────────────────────────────────
ONLINE = "online"
OFFLINE = "offline"
STATES = (ONLINE, OFFLINE)

# What a deployment with no stored state row is. Online, because that is what
# the bot was before this table existed: a fresh install, or an upgrade from a
# version without Nexus state, must not come up silent.
DEFAULT_STATE = ONLINE

# The in-process cache. ``None`` means "not read yet" and is not the same fact as
# offline — ``load()`` is what turns one into the other, and it is called at
# startup before any handler can run.
_state: str | None = None


def reset_state() -> None:
    """Forget the cached state, so the next read comes from the database."""
    global _state
    _state = None


def load() -> str:
    """Read the persisted state into the cache. Returns what it read.

    An unreadable or unrecognised stored value falls back to the default rather
    than raising: a corrupted row must not stop the bot booting, and it must not
    be interpreted as "offline" either, because a bot that comes up silent after
    a bad row looks exactly like a bot that is broken.
    """
    global _state
    try:
        row = db.nexus_state_get()
    except Exception:  # noqa: BLE001 - a state read must never be fatal
        log.exception("could not read the Nexus state; assuming %s", DEFAULT_STATE)
        _state = DEFAULT_STATE
        return _state
    stored = (row or {}).get("state", "")
    if stored in STATES:
        _state = stored
    else:
        if stored:
            log.warning(
                "stored Nexus state %r is not a known state; using %s",
                stored,
                DEFAULT_STATE,
            )
        _state = DEFAULT_STATE
    return _state


def state() -> str:
    """The current state, reading it from the database on first use."""
    if _state is None:
        return load()
    return _state


def is_online() -> bool:
    return state() == ONLINE


def set_state(new_state: str, *, actor_id: int = 0, reason: str = "") -> str:
    """Perform the transition. Returns the state the layer is now in.

    There is deliberately **no permission check here**, and the omission is the
    design rather than a hole. The authority for this — and for every other
    administrative act in this bot — lives in exactly one place,
    ``app/admin_service.execute``, which re-resolves the actor from their
    Telegram id and asks ``app/rbac.py`` whether they hold ``nexus.control``. A
    second check in this function would be a second authority model, and the
    whole architecture rests on there being one. This function is the mechanism;
    the service is the boundary.

    An unknown state is refused by returning the current one, because writing a
    third value into the table would leave the layer in a state no code path
    understands.
    """
    global _state
    if new_state not in STATES:
        log.warning("refused unknown Nexus state %r", new_state)
        return state()
    if new_state == state():
        # Idempotent: the requested state already holds, so there is nothing to
        # write and no reason to disturb the record of who last changed it.
        return new_state
    db.nexus_state_set(new_state, actor_id=actor_id, reason=reason)
    _state = new_state
    log.info(
        "nexus state=%s by=%s reason=%s", new_state, actor_id or "-", reason or "-"
    )
    return new_state


def state_label(value: str = "") -> str:
    """The Persian label for a state, for the operator's report."""
    if (value or state()) == OFFLINE:
        return config.NEXUS_STATE_OFFLINE_LABEL
    return config.NEXUS_STATE_ONLINE_LABEL


def describe() -> dict:
    """A log-safe summary. Ids and keys only — never a message, never a key."""
    row = db.nexus_state_get() or {}
    return {
        "state": state(),
        "changed_at": int(row.get("changed_at", 0) or 0),
        "changed_by": int(row.get("changed_by", 0) or 0),
        "reason": str(row.get("reason", "") or ""),
        "actors_only": bool(config.NEXUS_ACTORS_ONLY),
        "observe_admins": bool(config.NEXUS_OBSERVE_ADMINS),
        "names": len(names()),
    }


# ── Who may talk to Nexus ─────────────────────────────────────────────────
def is_actor(principal: rbac.Principal) -> bool:
    """Whether this principal is an authorized Nexus actor.

    The owner, or anybody the RBAC layer resolves to a role with at least one
    permission. That is the same set the administrative surface already trusts,
    and it is asked of ``app/rbac.py`` rather than decided here — this function
    is a *read* of the authority model, never a second copy of it.

    A guest resolves to an empty permission set, so an ordinary member is not an
    actor and their message never reaches the model. Note what this does not do:
    it does not look at a username, a display name, or anything the sender
    wrote. An impersonator is refused because their Telegram id is not in the
    table, not because their message failed to look convincing.
    """
    if principal is None:
        return False
    if principal.is_owner:
        return True
    return bool(principal.is_admin)


def accepts(principal: rbac.Principal) -> bool:
    """Whether Nexus will process anything from this principal **in a group**.

    Two conditions, and both are needed: the actor must be authorized, and the
    layer must be awake. When ``NEXUS_ACTORS_ONLY`` is off an ordinary member is
    also accepted — that switch exists only to restore the earlier
    answer-anybody behaviour, and it never widens what an *action* requires.

    When Nexus is offline nobody is accepted, not even the owner: the offline
    state is the owner's own instruction, and the way back is an explicit
    command or an addressed state phrase, both of which are handled before this
    is consulted.

    This is **not** the private-chat gate. A group has a room full of people who
    can already see each other's messages, so answering an administrator there
    discloses nothing that was not already public; a private chat has exactly
    one reader and no such argument applies. See ``accepts_private``.
    """
    if not is_online():
        return False
    if is_actor(principal):
        return True
    return not config.NEXUS_ACTORS_ONLY


def accepts_private(principal: rbac.Principal) -> bool:
    """Whether Nexus will answer **in a private chat**. The owner, and nobody else.

    This is a second boundary rather than a stricter reading of ``accepts``, and
    the difference is the whole point of having two functions:

    * ``NEXUS_ACTORS_ONLY`` does not widen it. That switch restores the earlier
      "answer anybody in the group" behaviour; it was never a statement about
      private messages, and reading it as one would silently reopen this door
      the first time an operator flipped it for an unrelated reason.
    * Being an administrator does not widen it. ``is_actor`` deliberately says
      yes to administrators, because a group's moderation is theirs to run — but
      a private chat with this bot is the owner's, and "an administrator" is not
      a lesser kind of owner. Nothing an administrator types, claims, or is
      promoted to can make this return true.
    * A private chat carries no room context, so there is nothing here that an
      administrator could legitimately need to see.

    The owner is resolved from their Telegram id by ``app/rbac.py`` and never
    from anything in the message, so a claim of ownership in the text of a
    message cannot reach this. When Nexus is offline the owner is refused too,
    for the same reason ``accepts`` refuses them: the offline state is the
    owner's own instruction.
    """
    if not is_online():
        return False
    if principal is None:
        return False
    return bool(principal.is_owner)


# ── Addressing ────────────────────────────────────────────────────────────
# The recognition itself lives in ``app/addressing.py``, and it moved there when
# it stopped being a word-list comparison. A group writes an assistant's name the
# way it is pronounced, not the way a config file spells it — «نکسی», «نکس»,
# «نکسوسو», «نکسووووس», «nexsus» are all the same call — and the matcher that
# reads those is a piece of orthography with its own tests rather than three
# lines in the middle of the trigger policy. What stays here is the *use* of it.
def names() -> tuple[str, ...]:
    """The configured names Nexus answers to. Lowercased, deduplicated."""
    return addressing.names()


def is_named(text: str) -> bool:
    """Whether the message calls Nexus by name.

    Delegates to the graded matcher and keeps only its strong reading. The
    distinction matters: this function decides whether ``app/main.py`` answers a
    message *now*, so it is allowed to be conservative. The weak reading — the
    name is present but nothing shows it is a call — is not this function's
    business; it is ``addressing.mentioned``, and ``app/awareness.py`` reads it
    directly to mark a transcript line the assistant was talked about in.
    """
    return addressing.addressed(text)


# ── The urgency hint ──────────────────────────────────────────────────────
# The words that make an unaddressed message worth reading *promptly*.
#
# This is not how intent is understood, and since Group Awareness it is not even
# how relevance is decided. Intent is the model's job, and the brief is explicit
# that administration must not be a list of exact keywords; relevance is now read
# from the conversation by ``app/awareness.py``. What is left here is *timing*:
# an unaddressed message that contains one of these words makes the room due for
# its awareness pass immediately instead of waiting for it to go quiet.
#
# The demotion is the point, so it is worth being exact about what this can and
# cannot do:
#
#   * it cannot make a message relevant — only the model decides that;
#   * it cannot make anything happen — only ``app/admin_service.py`` authorises;
#   * it cannot make the assistant speak — the model's decision does that;
#   * it can only move a room to the front of a queue it was already in.
#
# A wrong word therefore costs one slightly-early batched pass and nothing else,
# and a missing word costs a few seconds. Recall is still the right bias, because
# an instruction that arrives late is worse than one that arrives promptly, but
# the bias is now about latency rather than about correctness.
#
# The lexicon itself now lives in ``app/addressing.py``, because the name matcher
# there needs the same verbs for a different purpose — telling «نکسوس ساکتش کن»
# (a call, with an instruction after the name) from «نکسوس گفت که...» (a
# quotation). One list, two readers, so the two can never disagree about which
# words are moderation.
_ACTION_WORDS = addressing.ACTION_WORDS


def _action_words() -> frozenset[str]:
    """The built-in lexicon plus whatever the operator added.

    Built per call rather than cached, because a test — and an operator editing
    the environment — must be able to change it without restarting a module.
    The set is tiny and this is not a hot path: it runs once per unaddressed
    message from an administrator.
    """
    extra = {
        w.strip().lower()
        for w in (config.NEXUS_EXTRA_ACTION_WORDS or ())
        if w.strip()
    }
    return frozenset(_ACTION_WORDS) | extra


def looks_actionable(text: str) -> bool:
    """Whether an unaddressed message should make its room due *promptly*.

    Deliberately not "is this an instruction", and no longer even "is this worth
    asking" — that second question moved to the awareness layer when the
    assistant learned to read the room. This answers "should the model look at
    this room sooner rather than later", and it is allowed to be wrong in either
    direction: a false positive costs one batched pass a few seconds early, and a
    false negative costs a few seconds.

    Kept rather than deleted because the latency it removes is real — a group
    that is busy never goes quiet, and an instruction should not wait for it to —
    but its power is now confined to *when*, never *whether*.
    """
    low = (text or "").lower()
    if not low:
        return False
    words = _action_words()
    for token in re.split(r"[^\w\u0600-\u06ff]+", low):
        if token and token in words:
            return True
    return False


# ── The owner's state phrases ─────────────────────────────────────────────
def mentions(text: str, phrase: str) -> bool:
    """Whole-word, case-insensitive match of a fixed phrase.

    This is the *phrase* matcher, and it is not the name matcher above: the
    owner's state phrases are fixed strings whose meaning is their wording, so
    they are compared as written. Escaped before it becomes a pattern, because a
    phrase comes from configuration and an operator typing ``.`` should get a
    literal dot rather than a wildcard. Whole-word matching matters here for the
    same reason it always does in this language: the Persian ban stem «بن»
    appears inside «بنظر» and «بنفش», and a substring match would turn ordinary
    conversation into an administrative instruction.

    Public because it is the one place this rule is written. The voice-live
    commands are the same kind of thing — a fixed phrase list whose meaning is
    its wording, matched before any model is consulted — and a second copy of
    this regex is a second answer to "does this message contain that phrase"
    that would drift the first time either list changed.
    """
    if not text or not phrase:
        return False
    try:
        return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None
    except re.error:
        return False


# The one place where a fixed phrase list is the correct design rather than a
# shortcut. Turning Nexus off must keep working when Nexus is already off, and
# when Gemini is unreachable, and when the daily allowance is spent — so it
# cannot depend on the model. The model *also* has a tool for this, which is
# what handles the phrasing below, but this path is the one that always works.
#
# The phrases are matched as whole words after lowercasing, and the direction is
# resolved only when exactly one side matches. A message that asks for both, or
# that contains a negation, resolves to nothing at all and the owner is expected
# to use ``/nexus on`` — refusing to guess is the correct behaviour for a
# switch that changes whether the bot speaks.
#
# ── Two lists, and a second pair that needs the layer to be named ─────────
# These lists started as the direct imperatives and missed how the owner actually
# speaks: of fourteen real phrasings, one worked. Widening them is the right fix
# — it is data, it keeps the path model-free, and it is the same move
# ``NEXUS_AWARENESS_NAMES`` already represents. But the two directions are not
# equally safe to widen, and the asymmetry decides where a phrase goes:
#
# * an **on** phrase that misfires costs an answer — the assistant says something
#   when it was not asked to. Cheap, and visible.
# * an **off** phrase that misfires costs the assistant. It goes silent, and
#   silence is indistinguishable from a crash, a spent allowance or a network
#   fault. Expensive, and it reads as something being broken.
#
# The first pair is consulted always. It holds the phrasings whose direction is
# unambiguous on their own: the imperatives, and the object-pronoun forms
# («خاموشش کن» — "turn it off") that are how this language actually conjugates.
#
# The second pair is consulted **only when the message names a layer** — see the
# ``names_layer`` argument to :func:`command_from`. It holds the phrasings that
# are perfectly clear about the layer and perfectly ambiguous about everything
# else: «بیا پایین» is how the owner says "come down from awareness" and also how
# one says "come downstairs"; «راه بنداز» is "get it going" about anything;
# «چشاتو باز کن» is "open your eyes" about anything. Matching those on their own
# would put the assistant's own switch behind an idiom — a moderator telling
# somebody to come downstairs would have silenced the bot.
#
# Requiring the name costs the owner one word («اورنس چشاتو باز کن») and buys the
# property that a phrase can only move a switch when the message says which
# switch it means. It is also why these cannot simply be added to the first pair:
# there, the direction is the whole of the meaning.
_OFF_PHRASES = (
    "خاموش شو", "خاموش کن", "خاموش باش", "خاموش", "قطع کن", "متوقف کن",
    "برو آفلاین", "آفلاین شو", "افلاین شو", "دیگه جواب نده", "جواب نده",
    "پاسخ نده", "ساکت شو", "بخواب",
    "shut down", "shutdown", "go offline", "offline", "turn off",
    "stop responding", "stand down", "go to sleep",
)
_ON_PHRASES = (
    "روشن شو", "روشن کن", "روشن", "برگرد", "بازگرد", "فعال شو", "فعال کن",
    "آنلاین شو", "انلاین شو", "بیا آنلاین", "جواب بده", "پاسخ بده", "بیدار شو",
    "شروع کن",
    # «offline» was on the off list and «online» was on neither, so the two
    # directions disagreed about what counts as a phrase. This is the missing
    # half, and it is unambiguous in a way that the bare word «on» is not.
    "online",
    "come back online", "come online", "go online", "back online", "turn on",
    "wake up", "enable",
)
# Consulted only when the message names Nexus or the awareness layer.
_OFF_PHRASES_NAMED = (
    # "Turn it off", "cut it off" — the object is a pronoun, and the layer is
    # the only thing the message names, which is what makes it unambiguous.
    "خاموشش کن", "خاموشش", "قطعش کن", "قطعش",
    # Directional, and only meaningful about something already running.
    "بیا پایین", "بیار پایین", "بیارش پایین", "برو پایین",
    # Idioms that mean "stop watching" about the layer and "take a rest" or
    # "stop looking" about a person.
    "استراحت کن", "دیگه نبین", "چشاتو ببند",
)
_ON_PHRASES_NAMED = (
    "روشنش کن", "روشنش", "فعالش کن",
    # «بیدار شو» is above and «بیدار کن» was missing, which is the difference
    # between "wake up" and "wake it up" — and the owner says the second.
    "بیدار کن",
    # "Get it going" — the owner's «آگاهی رو راه بنداز». «راه» is a separate
    # word from the verb, so the compound is listed as written.
    "راه بنداز", "راه بینداز", "راه انداز",
    # The mirror of the off list's «بیا پایین».
    "بیا بالا", "بیار بالا", "بیارش بالا",
    # "Connect to the environment", "open your eyes" — about the layer when the
    # layer is named, about anything otherwise.
    "وصل شو", "چشاتو باز کن",
)
# A negation anywhere in the message cancels the whole thing. Over-broad on
# purpose: "don't go offline" and "روشن شو، خاموش نشو" both resolve to nothing,
# and the cost of that is one `/nexus on`, where the cost of getting it wrong is
# a bot that silences itself because somebody said "not yet".
_NEGATIONS = (
    "نکن", "نشو", "نزن", "نباش", "نمیخوام", "نمی‌خوام", "الان نه", "هنوز نه",
    "do not", "don't", "dont", "not now", "no thanks",
)


def negated(text: str) -> bool:
    """Whether a message contains a negation, which cancels a spoken command.

    Public because the voice-live commands need the same rule, and for the same
    reason: «برو ویس‌کال نکن» contains the join phrase and asks for the opposite
    of joining. A second copy of this list would be a second answer to "did the
    owner say not to", and the two would drift the first time one was widened.

    Deliberately over-broad, exactly as it is for the switch — see the comment
    on the list. A blocked command costs the owner one repeat; an obeyed
    negation costs a call they said not to open.
    """
    low = (text or "").lower()
    if not low:
        return False
    return any(mentions(low, word) for word in _NEGATIONS)


def command_from(text: str, *, names_layer: bool = False) -> str | None:
    """The state an owner's phrase asks for, or ``None``.

    Purely textual: this says what the words ask for, never whether the person
    saying them may have it. The caller checks that the speaker is the owner,
    and the transition itself goes through ``app/admin_service.py``.

    ``names_layer`` says whether the message names Nexus or the awareness layer,
    and it is a fact about the text rather than about the speaker, so it does not
    weaken the sentence above. It widens the vocabulary to the phrases that are
    only unambiguous *because* the layer is named — «بیا پایین», «چشاتو باز کن»,
    «راه بنداز» — and the default is ``False`` so that a caller which has not
    worked the name out cannot accidentally get the wider reading.
    """
    low = (text or "").lower()
    if not low:
        return None
    if negated(low):
        return None
    off_phrases = _OFF_PHRASES + (_OFF_PHRASES_NAMED if names_layer else ())
    on_phrases = _ON_PHRASES + (_ON_PHRASES_NAMED if names_layer else ())
    wants_off = any(mentions(low, phrase) for phrase in off_phrases)
    wants_on = any(mentions(low, phrase) for phrase in on_phrases)
    if wants_off == wants_on:
        # Both, or neither. Neither is an ordinary message; both is a
        # contradiction, and guessing at a contradiction is how a bot ends up
        # silent after somebody said "خاموش شو، نه روشن".
        return None
    return OFFLINE if wants_off else ONLINE


# ── Observation: watch without replying ───────────────────────────────────
def observation_enabled() -> bool:
    """Whether an unaddressed administrator message is worth remembering."""
    return bool(config.NEXUS_OBSERVE_ADMINS) and is_online()


def observe(
    chat_id: int,
    user_id: int,
    text: str,
    *,
    kind: str = "",
    reply_user_id: int = 0,
    reply_name: str = "",
) -> bool:
    """Record one unaddressed administrator message as context. Never replies.

    The message is stored in **that administrator's own** conversation history —
    the same ``(chat_id, user_id)`` scoped store the model is later shown — which
    is what makes "این کاربر خیلی مزاحم شده" available to a later "بنش کن"
    without ever putting one person's words in another person's prompt.

    Two markers are added, both server-generated, and both there for the same
    reason: a message that is not addressed to anybody is much less useful
    without knowing what it was a reaction to. A media turn is recorded as its
    kind (the bytes are never stored), and a reply is recorded with the id of
    the person replied to — which is exactly the target a later "بنش کن" needs.

    No model call, no Telegram call, no reply. Bounded by the same turn limit and
    TTL the conversation itself uses, and pruned opportunistically here because
    this process has no scheduler.
    """
    if not observation_enabled():
        return False
    body = (text or "").strip()
    if kind:
        body = f"[{kind}] {body}".strip() if body else f"[{kind}]"
    if not body:
        return False
    if reply_user_id:
        who = f"{reply_name} ({reply_user_id})" if reply_name else str(reply_user_id)
        body = f"[در پاسخ به {who}] {body}"
    try:
        db.chat_append(chat_id, user_id, "user", body)
        db.chat_trim(
            chat_id, user_id, keep=max(2, int(config.GEMINI_CHAT_HISTORY_TURNS))
        )
        db.chat_purge(max(1, int(config.GEMINI_CHAT_HISTORY_TTL)))
    except Exception:  # noqa: BLE001 - a context write is never worth a crash
        log.exception("could not record the observed message")
        return False
    log.info(
        "nexus observed chat=%s actor=%s chars=%d reply_to=%s",
        chat_id,
        user_id,
        len(body),
        reply_user_id or "-",
    )
    return True
