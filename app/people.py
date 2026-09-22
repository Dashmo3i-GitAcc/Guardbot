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


def _tokens(text: str) -> list[str]:
    """The individual name words, normalised and long enough to be one."""
    return [t for t in normalize(text).split() if len(t) >= MIN_NAME_LENGTH]


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
        normalize(first),
        normalize(last),
        normalize(f"{first} {last}"),
        normalize(row.get("username", "") or ""),
    }
    keys.update(_tokens(first))
    keys.update(_tokens(last))
    return {k for k in keys if len(k) >= MIN_NAME_LENGTH}


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

    ``chat_id`` is used only to order and annotate the candidates; identity is
    global, because a Telegram user id is global. Filtering by room would make
    the same person unresolvable in a second group, which is a bug rather than a
    privacy property.
    """
    if not config.NEXUS_PEOPLE_ENABLED:
        return {"status": "disabled"}
    query = normalize(name)
    if len(query) < MIN_NAME_LENGTH:
        return {
            "status": "unknown",
            "query": name,
            "reason": "too short to be a name",
        }

    limit = max(1, int(config.NEXUS_PEOPLE_MAX_CANDIDATES))
    rows = db.people_rows(limit=0)
    matches = [row for row in rows if query in _keys(row)]

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
    if chat_id and int(row.get("chat_id", 0) or 0) != int(chat_id):
        # Seen elsewhere. Still the right person — the id is global — but the
        # caller is told, because "they are in another group" is worth knowing
        # before acting on somebody who is not in this room.
        log.info("name resolved to a member of another chat")
    return {"status": "ok", **_public(row)}
