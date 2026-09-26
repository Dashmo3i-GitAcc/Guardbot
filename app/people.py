"""Identity memory: turning a name somebody said out loud into a Telegram id.

The brief's example is "Milad رو بن کن". For Nexus to construct a structured
request it needs a *number*, because a name is not an identity — usernames
change, display names collide, and "I am the owner" is a sentence anybody can
type. So the group's names are recorded as metadata, and a lookup turns one back
into the id that actually identifies somebody.

Three rules shape everything here, and each of them is a refusal:

**It grants nothing.** A row is written for every speaker, including people with
no role at all. Authority is resolved separately, from ``app/rbac.py``, keyed on
the Telegram user id — never on a name, never on this table. There is no
function in this module that returns a permission, and the model is told the
resolved id so it can put it in a tool call, where ``admin_service`` checks it
against the real actor anyway.

**It never guesses.** A match is an exact, normalised comparison — never a
similarity score, never a prefix, never "the closest one". If two people share a
name the answer is ``ambiguous`` with the candidates attached, and the model is
required to ask. Returning the most likely candidate would be the single most
dangerous thing this module could do, because the consequence of being wrong is
a ban on the wrong person.

One thing sits beside that rule rather than under it, and it is stated here so it
is not mistaken for a similarity score: when the exact comparison finds nobody, a
name spoken in one script and stored in another is read once more as a *sound
skeleton* (``_skeletons`` — «ساحل» and «Sahel» both reduce to ``shl``). It is a
fallback, never the first reading, it is bounded below so a two-consonant
skeleton cannot stand as evidence, and it answers ``ambiguous`` exactly like the
exact path when several people match. It exists because the owner reported a
directive naming «ساحل» that could not reach a person stored as «𝐒𝐚𝐡𝐞𝐥🪴».

**It stores no conversation.** Names, usernames and timestamps. There is no
column for a message body, and no code path writes one. The retention bounds are
in ``app/config.py`` and are applied on the observation path, because this
process has no scheduler.

The normalisation is the part that has to be right for the matching to be
useful: Persian is written with two different letters for the same sound, with
optional diacritics, and with a zero-width non-joiner that a human reader does
not see. "ميلاد" and "میلاد" are the same name and must resolve to the same
person, or a lookup fails for a reason that has nothing to do with identity.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from . import config, db

log = logging.getLogger("guardbot.people")

# Below this length a name is not a name. «بن» is a verb, «علی» is a name, and
# the boundary is deliberately at three: a two-character query is far more
# likely to be a word that happens to look like a name than a person.
MIN_NAME_LENGTH = 3

# Every how many recordings the retention bounds are applied. Pruning on each
# one would run two DELETEs per group message; never pruning would leave the
# table to grow until the row bound is the only thing keeping it honest, which
# it cannot do on its own because nothing would ever check it.
PRUNE_EVERY = 200
_since_prune = 0

# Characters that differ between keyboards but not between people.
_CHAR_MAP = {
    "ي": "ی", "ى": "ی", "ﻯ": "ی", "ﻰ": "ی",
    "ك": "ک", "ﻙ": "ک", "ﻚ": "ک",
    "ة": "ه", "ۀ": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ﺁ": "ا",
    "ؤ": "و", "ئ": "ی",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
}

# Diacritics and the tatweel. Dropped outright: they carry no identity.
_DROP = re.compile("[\u064b-\u0652\u0670\u0640\u202a-\u202e\ufeff]")

# The zero-width joiners become a *space* rather than being dropped. Persian
# writes a compound word with a non-joiner between its halves — «ميلادرضایی»
# is one word to a reader and two to a search — so folding it to nothing would
# make the name unmatchable by either half, which is the opposite of what this
# normalisation exists for. A space folds it to the same form as the
# space-separated spelling.
_SPACE_FOR = re.compile("[\u200b\u200c\u200d\u200e\u200f]")

_TRANSLATE = str.maketrans(_CHAR_MAP)


def normalize(text: str) -> str:
    """Fold a name to the form two people writing the same name will produce.

    Case, Arabic-versus-Persian letters, diacritics, the zero-width joiner,
    Arabic-Indic digits and every kind of whitespace. Unicode NFKC first, so a
    presentation form of a letter folds to its ordinary one before the table
    above sees it.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text))
    folded = _DROP.sub("", folded)
    folded = _SPACE_FOR.sub(" ", folded)
    folded = folded.translate(_TRANSLATE)
    folded = folded.casefold()
    return " ".join(folded.split())


# ── The name key: the fold, minus decoration ──────────────────────────────
# Emoji, punctuation and symbols carry no identity, and leaving them in the key
# is a real defect rather than tidiness: a person whose Telegram name is
# «𝐒𝐚𝐡𝐞𝐥🪴» had the key «sahel🪴», so neither «Sahel» nor «ساحل» could reach
# them. Everything that is not a letter, a digit or whitespace is dropped from a
# *name* key. The shared :func:`normalize` is deliberately left alone —
# ``reply_target`` borrows it as its fold, and a fold has to keep the
# punctuation a sentence is made of.
_NOT_A_LETTER = re.compile(r"[^\w\s]", re.UNICODE)


def name_key(text: str) -> str:
    """The form a *name* is matched in: :func:`normalize` with decoration gone."""
    return " ".join(_NOT_A_LETTER.sub("", normalize(text)).split())


# ── The phonetic skeleton: the bridge between two scripts ─────────────────
# A name spoken in Persian and stored in Latin shares no character with it —
# «ساحل» against «Sahel» — so an exact comparison can never match the two, and
# the owner reported exactly that. Each script is reduced to the same *sound*
# skeleton: consonants only, digraphs folded, doubled letters collapsed. Both
# sides land on ``shl`` for Sahel, ``mhmd`` for Mohammad, ``hsyn`` for Hossein.
#
# This is a *fallback*, never the first reading — an exact match always outranks
# it — and it keeps the module's cardinal rule: a skeleton that matches several
# people is ``ambiguous`` and the model must ask. ``_PHONETIC_MIN`` is the
# threshold that keeps a two-consonant skeleton from standing as evidence on its
# own: measured on this bot's own rooms, 60 of 550 members in one room reduce to
# two consonants (Ali, Reza), and those stay reachable by their exact spelling
# but not by sound. Lowering it is one constant; it widens the match at the cost
# of more false ambiguity, which is answered with a question rather than a pick.
_PHONETIC_MIN = 3
_PHONETIC_VARIANT_CAP = 8

# Post-:func:`normalize` Persian letters, by the sound each carries. Vowels and
# «ع» carry no consonant and drop out; the rest fold to one Latin spelling so a
# romanisation written either way meets in the middle.
_FA_SOUND = {
    "ا": "", "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j", "چ": "ch",
    "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "ژ": "zh",
    "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "",
    "غ": "gh", "ف": "f", "ق": "gh", "ک": "k", "گ": "g", "ل": "l", "م": "m",
    "ن": "n", "و": "v", "ه": "h", "ی": "y",
}
# A romanisation's digraphs, matched before the single letters below.
_LATIN_DIGRAPHS = {
    "kh": "kh", "gh": "gh", "sh": "sh", "ch": "ch", "zh": "zh",
    "ph": "f", "th": "t", "ck": "k",
}
# Latin letters by the same sounds. Vowels drop out; «i» is handled by
# ``_AMBIGUOUS`` above, because it is the consonant of «Milad» and the vowel of
# «Sahil» and only one reading would lose a name.
_LATIN_SOUND = {
    "b": "b", "p": "p", "t": "t", "s": "s", "c": "k", "j": "j", "h": "h",
    "d": "d", "z": "z", "r": "r", "f": "f", "g": "g", "k": "k", "l": "l",
    "m": "m", "n": "n", "v": "v", "w": "v", "y": "y", "x": "ks", "q": "gh",
    "a": "", "e": "", "o": "", "u": "",
}
# The letters with two honest readings, in either script. A Persian «و» is a
# «v» in «نوید» (Navid) and a vowel in «سروش» (Soroush); a Latin «i» is the
# consonant of «Milad» (میلاد) and the vowel of «Sahil». Both skeletons are
# produced for each, so a name written either way is found.
_AMBIGUOUS = {"و": ("v", ""), "i": ("y", "")}


def _skeletons(text: str) -> set[str]:
    """Every sound skeleton a name could reduce to, ignoring script.

    A *set* rather than one string because a romanisation is not a function: the
    same Persian word has more than one honest Latin reading, and producing both
    is what lets «سروش» and «Soroush» meet.
    """
    out: set[str] = set()
    for word in name_key(text).split():
        variants = [""]
        index = 0
        while index < len(word):
            char = word[index]
            if char in _AMBIGUOUS:
                options = _AMBIGUOUS[char]
                index += 1
            elif char in _FA_SOUND:
                options = (_FA_SOUND[char],)
                index += 1
            elif char.isascii():
                pair = word[index:index + 2]
                if pair in _LATIN_DIGRAPHS:
                    options = (_LATIN_DIGRAPHS[pair],)
                    index += 2
                else:
                    options = (_LATIN_SOUND.get(char, char),)
                    index += 1
            else:
                options = ("",)
                index += 1
            variants = [base + option for base in variants for option in options]
            variants = variants[:_PHONETIC_VARIANT_CAP]
        # A doubled consonant is one sound: «Mohammad» and «محمد» are ``mhmd``.
        collapsed = {re.sub(r"(.)\1+", r"\1", v) for v in variants}
        # A final «ی» is often the long vowel «-i» (مصطفی ~ Mostafa), so the
        # same word with it dropped is a second legitimate reading.
        collapsed |= {v[:-1] for v in collapsed if v.endswith("y")}
        out |= collapsed
    return {s for s in out if len(s) >= _PHONETIC_MIN}


def _tokens(text: str) -> list[str]:
    """The individual name words, normalised and long enough to be one."""
    return [t for t in name_key(text).split() if len(t) >= MIN_NAME_LENGTH]


def reset_state() -> None:
    """Forget the prune counter. For tests."""
    global _since_prune
    _since_prune = 0


def remember(user, chat_id: int) -> bool:
    """Record one speaker's name metadata. Never raises, never stores content.

    Takes whatever the caller has — a Telegram ``User`` or a plain dict — and
    reads only the three name fields. A missing id is a no-op rather than an
    error: this is called from a message handler, and a context write is never
    worth failing a handler over.
    """
    if not config.NEXUS_PEOPLE_ENABLED:
        return False
    user_id = int(_get(user, "id", 0) or 0)
    if not user_id or not chat_id:
        return False
    if getattr(user, "is_bot", False):
        return False
    try:
        db.people_remember(
            chat_id,
            user_id,
            first_name=_get(user, "first_name", "") or "",
            last_name=_get(user, "last_name", "") or "",
            username=_get(user, "username", "") or "",
        )
    except Exception:  # noqa: BLE001 - never the reason a handler fails
        log.exception("could not record a person")
        return False
    # Every speaker is also given their stable internal handle, here, on the one
    # path that already runs for every message. It is deliberately a separate
    # call rather than a column on ``people``: the handle is global to a person
    # while a name row is per room, and folding them together would mint a
    # second handle the first time somebody spoke in a second group.
    from . import identity  # imported late: identity imports this module

    identity.ensure(user_id)
    _maybe_prune()
    return True


def _get(obj, key: str, default=""):
    """One field from a ``User`` object or a dict, without assuming which."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _maybe_prune() -> None:
    global _since_prune
    _since_prune += 1
    if _since_prune < PRUNE_EVERY:
        return
    _since_prune = 0
    prune()


def prune() -> int:
    """Apply both retention bounds. Best effort; never raises."""
    try:
        return db.people_prune(
            keep=max(0, int(config.NEXUS_PEOPLE_MAX)),
            max_age=max(0, int(config.NEXUS_PEOPLE_RETENTION)),
        )
    except Exception:  # noqa: BLE001
        log.exception("people retention prune failed")
        return 0


def _keys(row: dict) -> set[str]:
    """Every normalised string this person could reasonably be called by.

    The full name, each name word, and the username. A *token* match is included
    because Telegram's ``first_name`` is often a full name in this language —
    "سید میلاد" — and somebody in the group will call that person "میلاد". It is
    still an exact comparison of one word against one word, so it can only ever
    widen the candidate set, and a widened set is answered with a question
    rather than a pick.
    """
    first = row.get("first_name", "") or ""
    last = row.get("last_name", "") or ""
    keys = {
        name_key(first),
        name_key(last),
        name_key(f"{first} {last}"),
        name_key(row.get("username", "") or ""),
    }
    keys.update(_tokens(first))
    keys.update(_tokens(last))
    return {k for k in keys if len(k) >= MIN_NAME_LENGTH}


def _phonetic_keys(row: dict) -> set[str]:
    """Every sound skeleton this person's stored names reduce to.

    Built from the same keys the exact comparison uses, so the two readings can
    never disagree about *what* a person is called — only about how it sounds.
    """
    keys: set[str] = set()
    for key in _keys(row):
        keys |= _skeletons(key)
    return keys


def _public(row: dict) -> dict:
    """One candidate, as the model and the operator should see it."""
    name = " ".join(
        part for part in (row.get("first_name", ""), row.get("last_name", "")) if part
    ).strip()
    return {
        "user_id": int(row["user_id"]),
        "name": name,
        "username": row.get("username", "") or "",
        "chat_id": int(row.get("chat_id", 0) or 0),
    }


def resolve(name: str, *, chat_id: int = 0) -> dict:
    """Resolve a spoken name to a Telegram user id, or explain why it cannot.

    Four answers, and the caller is expected to act on each differently:

    * ``ok`` — exactly one person matches. The id is safe to put in a request,
      where it is still authorised against the *actor* before anything happens.
    * ``ambiguous`` — several people match. The candidates come back so the
      model can ask "which Milad?", and the model must ask: there is no field
      anywhere that lets it pick one.
    * ``unknown`` — nobody matches. The model asks for a reply or an id.
    * ``disabled`` — the identity memory is switched off.

    ``chat_id`` scopes the lookup to one room, and it is the isolation rule
    rather than a nicety: a group's conversation may only be resolved against
    the names recorded in *that* group. A name spoken in one room and a person
    who only ever appeared in another are different facts, and matching them
    would leak one group's membership into another's answer. The canonical
    identity — the numeric Telegram user id — is global and stays global; it is
    the *name memory* that is per room.
    """
    if not config.NEXUS_PEOPLE_ENABLED:
        return {"status": "disabled"}
    query = name_key(name)
    if len(query) < MIN_NAME_LENGTH:
        return {
            "status": "unknown",
            "query": name,
            "reason": "too short to be a name",
        }

    limit = max(1, int(config.NEXUS_PEOPLE_MAX_CANDIDATES))
    # Scoped to the room when one is given. The unscoped read exists only for a
    # caller that genuinely has no room (an operator's own tooling), and is never
    # reached from a group's conversational path.
    rows = db.people_rows(int(chat_id)) if chat_id else db.people_rows(limit=0)
    matches = [row for row in rows if query in _keys(row)]
    if not matches:
        # The exact fold found nobody. A name spoken in Persian and stored in
        # Latin shares no character with it, so the query is read a second time
        # as a sound skeleton — the bridge the owner asked for. It is a
        # *fallback*: an exact match always outranks it, and a skeleton shorter
        # than ``_PHONETIC_MIN`` never counts at all. Everything below is
        # unchanged, so a skeleton that matches several people is still
        # ``ambiguous`` and the model must ask rather than pick.
        spoken = _skeletons(query)
        if spoken:
            matches = [row for row in rows if spoken & _phonetic_keys(row)]
            if matches:
                log.info("name resolved by sound skeleton, not by exact fold")

    # One person, even if they were seen in several chats: deduplicate by user
    # id and keep the most recent row, which is the first one ``people_rows``
    # returns because it is ordered by ``last_seen`` descending.
    unique: dict[int, dict] = {}
    for row in matches:
        unique.setdefault(int(row["user_id"]), row)

    if not unique:
        return {"status": "unknown", "query": name}
    if len(unique) > 1:
        candidates = [_public(row) for row in list(unique.values())[:limit]]
        log.info(
            "ambiguous name lookup matched %d people; asking rather than guessing",
            len(unique),
        )
        return {
            "status": "ambiguous",
            "query": name,
            "count": len(unique),
            "candidates": candidates,
            "hint": (
                "More than one person matches. Ask which one, or ask them to "
                "reply to the person's message."
            ),
        }

    row = next(iter(unique.values()))
    # Scoped to the room, so the matched row is always from this room when a
    # room was given. Nothing to annotate: a name that only exists in another
    # group is simply not resolvable here, which is the isolation working.
    return {"status": "ok", **_public(row)}


def roster(chat_id: int, *, text: str = "", limit: int = 0) -> list[dict]:
    """The people this room knows whose names the message actually mentions.

    The reader behind "Nexus knows everybody's name". The room transcript names
    the people who spoke *recently*; this names the people a message is *about*
    even when they have not spoken in the window — «میلاد دیروز چی گفت؟» reaches
    میلاد's id without dumping the membership.

    Relevance is by construction, not by a score: a row is returned only when one
    of its normalised keys is a whole token of the message, the same exact
    comparison ``resolve`` makes. Nothing is guessed and nothing is ranked — an
    unmatched name is simply absent, and the model is not shown a candidate set
    it might pick from. It is a *read* that grants nothing: it returns rows for
    the caller to render, and the ids it carries are Telegram's, not an authority.

    ``limit`` bounds the result and ``chat_id`` is required, so a caller cannot
    ask across rooms — the same isolation rule ``resolve`` enforces.

    The scan is bounded by ``NEXUS_PEOPLE_ROSTER_SCAN`` of the room's most
    recently seen people, because this reader runs on every room-dependent reply
    and an unbounded scan of a large room costs tens of milliseconds on the hot
    path (measured). A name a message mentions is overwhelmingly somebody
    recently present; and because a miss costs a context line rather than
    correctness, bounding it is the right trade. The exact comparison is
    unchanged — the bound only narrows what is compared.
    """
    if not config.NEXUS_PEOPLE_ENABLED or not chat_id:
        return []
    limit = int(limit or config.NEXUS_PEOPLE_CONTEXT_ITEMS)
    if limit <= 0:
        return []
    wanted = set(_tokens(text))
    # People write a username with its ``@`` — «@milad سلام» — while the stored
    # key has none, so both spellings are tried. Only the ``@`` is folded: the
    # comparison stays exact, and a token that is only ``@`` disappears.
    wanted |= {token[1:] for token in wanted if token.startswith("@")}
    wanted = {token for token in wanted if len(token) >= MIN_NAME_LENGTH}
    if not wanted:
        return []
    scan = max(0, int(config.NEXUS_PEOPLE_ROSTER_SCAN))
    try:
        rows = db.people_rows(int(chat_id), limit=scan)
    except Exception:  # noqa: BLE001 - a context read is never worth a turn
        log.exception("could not read the room roster")
        return []
    out: list[dict] = []
    for row in rows:
        if _keys(row) & wanted:
            out.append(_public(row))
            if len(out) >= limit:
                break
    return out
