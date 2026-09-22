"""Being called by name, in the way a group actually writes it.

The problem this module exists for is narrow and concrete: a group does not
write an assistant's name the way a configuration file spells it. «نکسوس» gets
written «نکسی», «نکس», «نکسوسو», «نکسوس جان», «نکسووووس», «نیکسوس», «nexus»,
«nexsus» and «@nexus_bot» — and every one of those is the same person being
called. A whole-word equality test against a configured list misses all of them,
and the failure is invisible: the assistant simply never notices it was spoken
to, which looks exactly like the assistant ignoring you.

Two levels of evidence, and the split is the whole design
---------------------------------------------------------
Recognition is graded rather than boolean, because the two consumers of it have
opposite costs when they are wrong:

* **``addressed``** is the strong signal, and it is what lets ``app/main.py``
  answer a message *immediately* instead of waiting for the room to fall quiet.
  A false positive here makes the assistant talk when it was not spoken to,
  which is the more annoying of the two mistakes, so it demands real evidence:
  the token is the name, or the name plus a clitic, or one edit away from it, or
  a vowel-skeleton match that the surrounding words show is a *call* rather than
  a mention.
* **``mentioned``** is the weak signal, and it is context rather than a trigger.
  It is what the awareness pass is told — "your name came up here" — so the
  model can judge whether the conversation concerns it. A false positive costs
  one line of context; a false negative costs the assistant not knowing it was
  being discussed, which is the thing the brief asks to fix.

Why a skeleton rather than a bigger word list
---------------------------------------------
Persian is written with optional long vowels: «نکسوس», «نکسی» and «نکس» differ
only in which vowels the writer bothered to type, and Latin transliterations of
the same name vary the same way. Dropping the long vowels from both sides
collapses every one of those spellings onto one form — «نکس» — and that is a
property of the writing system rather than a list somebody has to maintain. It
is also why a skeleton match alone is not enough to count as *addressed*: the
same collapse maps «ناکس» onto the same skeleton, and a skeleton is evidence of
a name, not of a call. Context decides that, and ``_addressing_intent`` is where
it does.

What this module deliberately is not
------------------------------------
It is not a decision about relevance, and it is not authority. Whether a message
concerns Nexus is still the model's judgement; whether anything may be *done* is
still ``app/admin_service.py``. Nothing here reads the database, and nothing
here returns anything but a reading of the text.
"""
from __future__ import annotations

import re
import unicodedata

from . import config

# ── The evidence grades ───────────────────────────────────────────────────
NONE = 0
# The name appears in a form that could be it, but nothing shows it is a call.
MENTION = 1
# The name appears in a form that is it, or in a form that is a call.
ADDRESSED = 2


# ── Folding ───────────────────────────────────────────────────────────────
# The general fold is ``people.normalize``'s, and it is reused rather than
# copied: it already handles the Arabic-versus-Persian letters, the diacritics,
# the zero-width non-joiner and the digit sets, and a second implementation of
# the same fold is a second place for the two to disagree. The import is late
# and guarded so this module stays importable on its own.
def _fold(text: str) -> str:
    """The shared orthographic fold, plus the vowel skeleton's own rules."""
    try:
        from . import people

        folded = people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold must never be the reason a call fails
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
        folded = " ".join(folded.split())
    return folded


# Characters that carry no consonant and are therefore dropped when building the
# skeleton. Persian: alef, vav, yeh (and their hamza forms, already folded to
# these by ``people.normalize``). Latin: the vowels plus ``y``, which is a vowel
# in every transliteration of this name.
_VOWELS = frozenset("اویaeiouy")

# A run of three or more identical letters is elongation — «نکسووووس» — and a run
# of two is ordinary spelling in Persian and Latin alike, so only the longer run
# is collapsed. Collapsing pairs would turn «نکسس» into «نکس» before the edit
# distance ever saw it, which is the comparison that is supposed to catch it.
_ELONGATION = re.compile(r"(.)\1{2,}")

# The second collapse, applied *after* the vowels are gone. It folds a pair as
# well as a longer run, because two identical consonants that end up adjacent
# only because the vowel between them was dropped are one consonant: «نکسوس»
# loses its «و» and leaves «سس», which is the same «س» as «نکسی» has.
_DOUBLED = re.compile(r"(.)\1+")


def skeleton(text: str) -> str:
    """The consonant skeleton of a word: «نکسوس» and «nexus» both become «نکس».

    The duplicate collapse runs *twice*, and the second pass is the one that
    matters. Dropping the vowels from «نکسوس» leaves the two «س» that were
    separated by the «و» now adjacent — «نکسس» — while «نکسی» leaves one — «نکس».
    Collapsing only before the drop would leave those two spellings of the same
    name on different skeletons, which is precisely the miss this exists to
    prevent.
    """
    folded = _ELONGATION.sub(r"\1", _fold(text))
    stripped = "".join(ch for ch in folded if ch not in _VOWELS and not ch.isspace())
    return _DOUBLED.sub(r"\1", stripped)


def _letters(text: str) -> str:
    """The folded token with elongation collapsed and separators removed."""
    folded = _ELONGATION.sub(r"\1", _fold(text))
    return "".join(ch for ch in folded if ch.isalnum())


# ── Affixes a group attaches to a name ────────────────────────────────────
# Persian attaches the object marker, the conjunction and the vocative directly
# to the word, so «نکسوسو» and «نکسوسیه» are the name with a clitic rather than a
# different word. The list is deliberately short and the strip is deliberately
# single: stripping repeatedly would turn a long ordinary word into a name.
_CLITICS = (
    "رو", "را", "ها", "های", "یه", "یی", "ای", "ام", "ات", "اش",
    "تون", "مون", "شون", "و", "م", "ت", "ش",
)
# Separate words a group puts after the name. They are separate tokens after the
# fold, so this is a lookahead rather than a suffix strip.
_VOCATIVES = ("جان", "جون", "عزیز", "گرامی", "خان", "خانم", "آقا", "اقا")
# Separate words a group puts *before* the name when calling somebody. A message
# that opens with one of these and then the name is a call.
_CALLERS = ("ای", "هی", "یا", "الا", "سلام", "درود")

# The moderation verbs, used only to tell a *call* from a *mention*: «نکسوس
# ساکتش کن» is addressed, «نکسوس گفت که ساکتش کنه» is being talked about.
#
# This lexicon lives here rather than in ``app/nexus.py`` because this is now its
# only consumer that needs it as evidence about meaning; ``nexus`` imports it for
# the timing hint it still makes, so there is still one list.
ACTION_WORDS = (
    "بن", "بنش", "بنشون", "بنشونش", "آنبن", "انبن", "آنبنش", "انبنش",
    "اخراج", "اخراجش", "بیرون", "بنداز", "بندازش", "حذف", "حذفش", "پاک", "پاکش",
    "ساکت", "ساکتش", "خفه", "خفهش", "محدود", "محدودش", "اخطار", "اخطارش",
    "ادمین", "ادمینش", "مدیر", "مدیرش", "ارتقا", "تنزل", "مسدود", "مسدودش",
    "بلاک", "بلاکش", "آزاد", "ازاد", "رفع", "ممنوع", "توقیف", "قطع",
    "دسترسی", "دسترسیش", "سطح", "سطحش", "نقش", "نقشش", "رول", "رولش",
    "محروم", "تعلیق", "بنکن",
    "محدودیت", "محدودیتش", "نتونه", "نتونن", "نذار", "نزار",
    "ban", "unban", "mute", "unmute", "kick", "promote", "demote", "warn",
    "delete", "remove", "restrict", "admin", "moderator", "role", "roles",
    "permission", "permissions", "revoke", "suspend",
)

# The imperative endings a Persian instruction ends with. Used the same way as
# ``ACTION_WORDS``: a verb form after the name makes it a call.
_IMPERATIVE_ENDINGS = ("کن", "کنش", "کنید", "بده", "بزن", "شو")

# ``_`` is a separator as well as punctuation: a Telegram username is
# ``@nexus_bot``, and ``\\w`` would keep the whole thing as one token that equals
# no configured name.
_TOKEN_SPLIT = re.compile(r"[^\w\u0600-\u06ff]|_")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(_fold(text)) if t]


def _action_words() -> frozenset[str]:
    """The built-in lexicon plus the operator's additions.

    Built per call for the same reason ``nexus._action_words`` is: a test and an
    operator editing the environment must both be able to change it without
    restarting a module.
    """
    extra = {
        w.strip().lower()
        for w in (config.NEXUS_EXTRA_ACTION_WORDS or ())
        if w.strip()
    }
    return frozenset(ACTION_WORDS) | extra


def _is_imperative(token: str) -> bool:
    """Whether a token reads as an instruction rather than as a report."""
    if not token:
        return False
    if token in _action_words():
        return True
    return token.endswith(_IMPERATIVE_ENDINGS) and len(token) >= 4


# ── The reading ───────────────────────────────────────────────────────────
class Address:
    """What a message's words say about whether Nexus is being spoken to.

    ``form`` is the surface token that matched, kept so an operator reading a
    log can see *why* the assistant thought it was called — «نکسوسو» is a much
    more convincing answer to "why did it reply" than a boolean is.
    """

    __slots__ = ("strength", "form", "name", "reason")

    def __init__(self, strength: int = NONE, form: str = "", name: str = "",
                 reason: str = "") -> None:
        self.strength = int(strength)
        self.form = form
        self.name = name
        self.reason = reason

    @property
    def found(self) -> bool:
        return self.strength > NONE

    @property
    def addressed(self) -> bool:
        return self.strength >= ADDRESSED

    def __bool__(self) -> bool:
        return self.found

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Address(strength={self.strength}, form={self.form!r}, "
            f"reason={self.reason!r})"
        )


def names() -> tuple[str, ...]:
    """The configured names, folded and deduplicated."""
    seen: list[str] = []
    for name in config.NEXUS_NAMES or ():
        folded = _letters(name)
        if len(folded) >= 2 and folded not in seen:
            seen.append(folded)
    return tuple(seen)


def _distance(a: str, b: str, limit: int = 1) -> int:
    """Bounded Levenshtein distance. Returns ``limit + 1`` once it exceeds it.

    Bounded because the only question ever asked of it is "is this one edit
    away", and the answer is needed on the message path. A full distance matrix
    would compute a number nobody reads.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            )
            current.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _strip_clitic(token: str) -> str:
    """The token with at most one attached clitic removed."""
    for clitic in _CLITICS:
        if token.endswith(clitic) and len(token) - len(clitic) >= 2:
            return token[: -len(clitic)]
    return token


def _addressing_intent(tokens: list[str], index: int) -> bool:
    """Whether the words around a matched token show a *call* rather than a mention.

    Three signals, each of them about grammar rather than about a phrase list,
    and any one of them is enough:

    * a calling word immediately before it — «ای نکسوس», «هی نکسوس»;
    * a vocative immediately after it — «نکسوس جان»;
    * the token opening the message, or an imperative verb in the same message.
      A group that types the name first and then says what it wants is calling;
      a group that says «نکسوس گفت که...» is quoting.

    Used only to promote a *skeleton* match. An exact match needs no help, and
    letting this function demote one would make the strong signal context-
    dependent for no gain.
    """
    if index == 0:
        return True
    if tokens[index - 1] in _CALLERS:
        return True
    if index + 1 < len(tokens) and tokens[index + 1] in _VOCATIVES:
        return True
    return any(_is_imperative(token) for token in tokens)


def detect(text: str, *, name_list: tuple[str, ...] | None = None) -> Address:
    """Read a message for a call to Nexus. Pure; no database, no model.

    The order of the tests is the order of the evidence, and it is worth stating
    because it is what keeps the strong grade honest:

    1. **exact** — the folded token is the folded name;
    2. **clitic** — the token is the name with one Persian clitic attached;
    3. **typo** — one edit away, and only for names of four letters or more, so
       a two-letter configured name cannot be matched by half the language;
    4. **skeleton** — the same consonants in the same order, promoted to
       *addressed* only when ``_addressing_intent`` shows a call and otherwise
       reported as a mention.

    The skeleton test needs no length comparison, and that is worth a sentence
    because it looks like an omission. Two words with the same consonant
    sequence in the same order can only differ by vowels, and in this writing
    system a name plus or minus its vowels *is* the same name — «نکسوس», «نکسی»
    and «نکس» are one word typed three ways. A length bound would reject exactly
    the spellings this exists to accept.
    """
    folded_names = name_list if name_list is not None else names()
    if not folded_names:
        return Address()
    tokens = _tokens(text)
    if not tokens:
        return Address()

    best = Address()
    for index, token in enumerate(tokens):
        for name in folded_names:
            address = _match(token, name, tokens, index)
            if address.strength > best.strength:
                best = address
                if best.strength >= ADDRESSED and best.reason in ("exact", "clitic"):
                    # Nothing can beat this, and the common case is the cheap one.
                    return best
    return best


def _match(token: str, name: str, tokens: list[str], index: int) -> Address:
    """One token against one configured name. Returns the strongest reading."""
    bare = _letters(token)
    if not bare:
        return Address()

    if bare == name:
        return Address(ADDRESSED, token, name, "exact")

    stripped = _strip_clitic(bare)
    if stripped and stripped != bare and stripped == name:
        return Address(ADDRESSED, token, name, "clitic")

    if len(name) >= 4 and _distance(bare, name, 1) <= 1:
        return Address(ADDRESSED, token, name, "typo")

    # The skeleton comparison, last because it is the weakest evidence.
    skeleton_name = skeleton(name)
    if len(skeleton_name) < 3 or len(name) < 4:
        return Address()
    if skeleton(bare) != skeleton_name:
        return Address()
    # Two edits, on top of the skeleton match, and the pair is what keeps this
    # honest. A skeleton alone is a consonant outline, and an outline is shared
    # by words that have nothing to do with each other — dropping the vowels
    # from «noxious» lands on the same «nxs» as «nexus» does. The distance test
    # is what separates "the same word spelled with different vowels", which is
    # every Persian spelling of this name, from "a different word that happens
    # to share its consonants".
    if _distance(bare, name, 2) > 2:
        return Address()
    if _addressing_intent(tokens, index):
        return Address(ADDRESSED, token, name, "skeleton")
    return Address(MENTION, token, name, "skeleton")


def addressed(text: str) -> bool:
    """Whether this message is calling Nexus. The strong signal."""
    return detect(text).addressed


def mentioned(text: str) -> bool:
    """Whether Nexus came up at all. The weak signal, for context only."""
    return detect(text).found
