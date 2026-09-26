"""How much answer the message is asking for.

The assistant used to be told, always, that two or three sentences is right —
and it obeyed even when somebody had asked for the whole thing. A web search the
person explicitly requested came back as a three-line summary of it, because the
length was a house rule rather than a reading of the request. This module is that
reading, and it is deterministic: a small closed vocabulary, no model, no
database, no authority. It answers one question — *short, normal, full, or
current-news?* — and the conversational path renders it into the trusted context
as a server reading, the same way the date is.

The default is deliberately **empty**: most messages ask for nothing in
particular, and the persona's own default (a couple of sentences for ordinary
chat) is the right answer for them. Only an explicit ask moves the reading, so a
message that merely contains «چرا» does not become a request for an essay.

Two properties are load-bearing:

* **It only ever widens or narrows length, never content.** The reading says how
  much to write, not what to say, and it cannot make the model skip a fact or
  invent one.
* **It never fails a turn.** A missing fold degrades to a plain casefold, and
  every reader is guarded, exactly like the other deterministic readers beside
  it.
"""
from __future__ import annotations

KIND_FULL = "full"
KIND_SHORT = "short"
KIND_NEWS = "news"
KIND_NORMAL = ""

# Explicit requests for completeness. Matched as substrings against the folded
# text, so «کامل توضیح بده» and «کاملش رو بگو» both hit «کامل». The list is
# phrases rather than single words where a single word is too common — «همه» on
# its own is not a request for detail, but «همه چیز» and «همهی» are.
_FULL_PHRASES = (
    "کامل توضیح",
    "کامل بگو",
    "کاملش",
    "کامل شرح",
    "کامل بنویس",
    "همه چیز",
    "همهچیز",
    "همه ی",
    "همهی",
    "همشو",
    "همش رو",
    "با جزییات",
    "با جزئیات",
    "جزییات",
    "جزئیات",
    "مفصل",
    "شرح بده",
    "توضیح بده",
    "توضیح کامل",
    "بیشتر توضیح",
    "دقیق توضیح",
    "explain in detail",
    "in detail",
    "explain fully",
    "all the details",
    "everything",
    "full answer",
    "tell me everything",
    "go through it",
)

# Explicit requests for brevity. This is the one case the reading narrows, and it
# is only honoured when the person actually said it — never inferred.
_SHORT_PHRASES = (
    "خلاصه",
    "کوتاه بگو",
    "مختصر",
    "سریع بگو",
    "توی یه خط",
    "توی یک خط",
    "در یک خط",
    "سر راست",
    "briefly",
    "in short",
    "short answer",
    "keep it short",
    "tldr",
)

# A request for current information. A search's findings are *material the person
# asked for*, so the answer carries them rather than a digest of them.
_NEWS_PHRASES = (
    "اخرین خبر",
    "آخرین خبر",
    "خبر جدید",
    "خبرهای جدید",
    "خبرهاش",
    "خبراش",
    "اخبارش",
    "اخبار",
    "خبر بده",
    "چی خبر",
    "latest news",
    "the news",
    "what happened",
    "search for",
    "search the web",
    "look it up",
    "سرچ کن",
    "جستجو کن",
)

# A guard against the reading firing on a message that is *about* brevity rather
# than asking for it — «خلاصهش چی بود؟» asks what the summary was, not for one.
_META = ("خلاصه ش", "خلاصهش", "خلاصه ی", "خلاصهی")


def _fold(text: str | None) -> str:
    """The shared fold, borrowed so the phrases match the same way elsewhere."""
    try:
        from . import people

        return people.normalize(text)
    except Exception:  # noqa: BLE001 - a fold is never worth a failure
        return " ".join(str(text or "").casefold().split())


def read(text: str) -> str:
    """The answer-length reading for one message. Never raises.

    ``full`` and ``news`` both mean "give the whole thing"; they are kept apart
    because the wording the model sees is different — one is about an explanation,
    the other about findings — and because a caller may want to know that a search
    was what made the answer long.
    """
    folded = _fold(text)
    if not folded:
        return KIND_NORMAL
    if any(phrase in folded for phrase in _FULL_PHRASES):
        return KIND_FULL
    if any(phrase in folded for phrase in _NEWS_PHRASES):
        return KIND_NEWS
    # Brevity is read last and only when it is an instruction, not a reference.
    if any(phrase in folded for phrase in _SHORT_PHRASES):
        if any(meta in folded for meta in _META):
            return KIND_NORMAL
        return KIND_SHORT
    return KIND_NORMAL


def render(kind: str) -> str:
    """The block the model reads, or ``""`` for an ordinary message.

    A server reading, phrased as evidence like the other blocks: it says what the
    person asked for and what that means for the answer, and it grants nothing.
    """
    if kind == KIND_FULL:
        return (
            "\n── What this message is asking for (read by the server) ──\n"
            "The person asked for a complete answer. Give them all of the "
            "useful detail, as long as it genuinely takes, and do not summarise "
            "or shorten it. Completeness is the point here.\n"
        )
    if kind == KIND_NEWS:
        return (
            "\n── What this message is asking for (read by the server) ──\n"
            "The person asked for current information or news. Present the "
            "findings fully and usefully — every relevant item, with enough "
            "detail to be useful — not a one-line summary of them.\n"
        )
    if kind == KIND_SHORT:
        return (
            "\n── What this message is asking for (read by the server) ──\n"
            "The person asked for a short answer. Keep it brief and to the "
            "point; do not pad it out.\n"
        )
    return ""


__all__ = ["KIND_FULL", "KIND_NEWS", "KIND_NORMAL", "KIND_SHORT", "read", "render"]
