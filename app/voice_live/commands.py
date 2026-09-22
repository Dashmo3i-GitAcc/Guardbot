"""The owner's spoken commands: bringing Nexus into a call, and taking it out.

These are commands and not requests, and the reason is the same one that makes
the assistant's own on/off switch a phrase list: they have to work when nothing
else does. A model is not consulted to decide whether to open a call — the
model is what a call is *for* — so the decision cannot be made by one, and it
cannot depend on an allowance, a network or a provider. It is a fixed list of
phrases, matched before any conversational path is reached.

Three properties, and each is a decision rather than a detail.

**The vocabulary is data, in ``app/config.py``.** An operator whose group says
«بپر تو ویس» adds it to ``GEMINI_LIVE_JOIN_PHRASES`` and nothing else changes.
That is the same move the awareness layer's names already represent, and it is
the reason a phrase list is the right design here rather than a shortcut.

**A message that asks for both directions is refused.** «برو ویسکال، نه بیا
بیرون» is a contradiction, and guessing at one half of a contradiction is how a
call is opened when the owner meant to close one. ``None`` is the honest answer
and the owner can use the unambiguous form.

**This module does not know who is speaking.** It says what the words ask for,
and the caller checks that the speaker is the owner — exactly as
``nexus.command_from`` does for the switch, and for the same reason: an
authority model that lives in two places is two authority models.

What it deliberately does not do: it does not start or stop anything, does not
touch the manager, and does not read the room. It answers one question about
one string, which is what makes it testable without a call, a credential or an
event loop.
"""
from __future__ import annotations

from .. import config, nexus

# The two directions, as machine keys. Strings rather than an enum because the
# caller branches on them and they appear in the log.
JOIN = "join"
LEAVE = "leave"

#: Every direction this module can report.
ACTIONS = (JOIN, LEAVE)


def action_for(text: str) -> str:
    """Which voice command a message is: ``JOIN``, ``LEAVE``, or ``""``.

    ``""`` covers three different things and deliberately does not distinguish
    them: an ordinary message, a message about neither direction, and a message
    about both. The caller does the same thing with all three — fall through to
    the next router — so telling them apart here would be information nobody
    uses.

    Matched with ``nexus.mentions``, the whole-word phrase matcher the switch
    phrases already use. Whole-word rather than substring because these are
    Persian words and the short ones live inside longer ones: a substring match
    on a phrase containing «ویس» would fire on any sentence that mentions voice
    chat in passing.

    A negation cancels the whole message, through the same ``nexus.negated`` the
    switch uses. «برو ویس‌کال نکن» contains the join phrase and asks for the
    opposite of joining, and a router that read only the phrase would open the
    call the owner just declined.
    """
    low = (text or "").lower()
    if not low:
        return ""
    if nexus.negated(low):
        return ""
    joins = any(nexus.mentions(low, phrase) for phrase in config.GEMINI_LIVE_JOIN_PHRASES)
    leaves = any(
        nexus.mentions(low, phrase) for phrase in config.GEMINI_LIVE_LEAVE_PHRASES
    )
    if joins == leaves:
        # Both, or neither. Neither is an ordinary message; both is a
        # contradiction, and a contradiction is not something to guess at when
        # the guess opens or closes a voice channel.
        return ""
    return JOIN if joins else LEAVE


def names_layer(text: str) -> bool:
    """Whether the message names this layer rather than the assistant.

    Used by the router for the same purpose ``NEXUS_AWARENESS_NAMES`` serves
    for the awareness layer: so that a phrase about voice live is recognised as
    being about voice live, and never mistaken for the assistant's own on/off
    switch. The two vocabularies share a verb — «نکسوس بیا» turns the assistant
    on and «نکسوس بیا بیرون» leaves a call — and the shared verb is exactly why
    the layer can be named at all.
    """
    low = (text or "").lower()
    if not low:
        return False
    return any(nexus.mentions(low, name) for name in config.GEMINI_LIVE_NAMES)
