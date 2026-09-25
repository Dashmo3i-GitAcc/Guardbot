"""Finding the voice chat that is already open, and saying why when there is none.

Why this module exists, in one paragraph
----------------------------------------
Joining a Telegram group call is two steps: *find* the active call, then *join*
it. ``py-tgcalls`` does both, but its finder is built for a client that has been
online since before the call started. It keeps an ``InputGroupCall`` cache that
is filled by ``UpdateGroupCall`` — a notification Telegram sends *when a call
starts* — and falls back to asking for the channel's full info only on a cache
miss. That fallback is correct, but it is wrapped in

    except Exception:
        pass

so *every* way discovery can fail — not a member, forbidden, flood wait, a
network error, an id the client cannot resolve — is flattened into the same
``None``, which the caller then reports as ``NoActiveGroupCall``. "There is no
call" and "I could not ask" become one sentence, and they need two different
fixes.

This module does the finding itself, in the open, so that the answer is a fact
rather than a guess:

* **It uses the API Telegram actually exposes for this.** A group's active call
  is ``ChannelFull.call`` for a channel/supergroup and ``ChatFull.call`` for a
  basic group — the same two fields ``py-tgcalls`` reads, fetched directly.
  (The alternative, waiting for ``UpdateGroupCall``, only works for a client that
  was connected when the call began; a session that connects afterwards never
  sees it. That is why a raw-update listener can sit for a minute and see
  nothing while a call is plainly running.)
* **It distinguishes the four outcomes** that were previously one: there is no
  call, there is a *scheduled* call, the account cannot see the call, or asking
  failed. Each has a different fix and gets a different machine reason.
* **It hands the found call back** so the caller can give it to the library
  rather than letting the library look it up a second time and possibly miss it.

Dependency discipline
---------------------
Nothing here imports ``telethon`` at module load. ``app/voice_live`` is imported
in deployments where the transport library is absent, and that must keep
working; the import happens inside :func:`discover`, which is only ever reached
after the transport has already confirmed the library is present. It is also a
single function so that a test can replace it with a stub without a real
Telethon install.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import errors

log = logging.getLogger("guardbot.voice.discovery")

# ── The outcomes ──────────────────────────────────────────────────────────
# ``active`` is the only one that permits a join. The rest are refusals, and
# each carries its own reason so the log says which fix applies.
ACTIVE = "active"
SCHEDULED = "scheduled"
NONE = "none"
NO_ACCESS = "no_access"
UNSUPPORTED = "unsupported"
ERROR = "error"

#: Outcome → the machine reason the transport raises. ``active`` has no reason
#: because nothing is wrong; a lookup of it is a programming error, not a state.
REASON_FOR = {
    NONE: errors.REASON_NO_ACTIVE_CALL,
    SCHEDULED: errors.REASON_SCHEDULED_CALL,
    NO_ACCESS: errors.REASON_CALL_NOT_VISIBLE,
    UNSUPPORTED: errors.REASON_DISCOVERY_FAILED,
    ERROR: errors.REASON_DISCOVERY_FAILED,
}

# Errors that mean "this account cannot see this chat's call". Named rather than
# imported so that the list is readable without a Telethon install, and resolved
# against the real error classes at call time — see ``_no_access_types``.
_NO_ACCESS_NAMES = (
    "ChannelPrivateError",
    "ChannelForbiddenError",
    "ChatForbiddenError",
    "ChannelInvalidError",
    "ChatInvalidError",
    "ChannelPublicGroupNaError",
    "PeerIdInvalidError",
)


def reason_for(kind: str) -> str:
    """The machine reason for a refusal outcome. ``""`` for ``active``."""
    if kind == ACTIVE:
        return ""
    return REASON_FOR.get(kind, errors.REASON_DISCOVERY_FAILED)


@dataclass(frozen=True)
class Discovery:
    """What asking Telegram produced.

    ``input_call`` is the ``InputGroupCall`` (or its slug form) that Telegram
    returned, and it is only set when ``kind`` is ``active`` or ``scheduled`` —
    i.e. when there was something real to find. ``detail`` is a class name or a
    short machine word for the log; it is never a credential and never a peer's
    identity, which is why it is a *type name* and not ``str(exception)``.
    """

    kind: str
    input_call: Any = None
    detail: str = ""

    @property
    def active(self) -> bool:
        return self.kind == ACTIVE


def _telethon():
    """The Telethon module, imported on demand.

    A function so a test can replace it, and so that importing this module in a
    deployment without the library costs nothing.
    """
    import telethon

    return telethon


def _no_access_types(telethon) -> tuple:
    """The real error classes from ``_NO_ACCESS_NAMES`` that exist here.

    ``isinstance`` against real classes rather than comparing ``type(exc).__name__``
    strings: a name comparison silently stops matching if Telethon renames a
    class, while a missing class here is simply skipped and the error falls to
    ``ERROR`` — which is honest, because an unrecognised failure *is* unknown.
    """
    found = []
    for name in _NO_ACCESS_NAMES:
        cls = getattr(getattr(telethon, "errors", None), name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


def _classify(exc: BaseException, no_access_types: tuple) -> str:
    """Which refusal an exception is. Flood waits and unknowns are ``ERROR``.

    ``ValueError`` is grouped with the visibility errors because that is what
    Telethon raises when it cannot resolve an entity at all — an id the account
    is not in, or one that does not exist. From the caller's point of view that
    is the same fact as ``ChannelPrivateError``: this account cannot see this
    chat's call, and the fix is membership, not a retry. It is deliberately not
    flattened into ``ERROR``, which would suggest asking again might work.
    """
    if isinstance(exc, ValueError):
        return NO_ACCESS
    if no_access_types and isinstance(exc, no_access_types):
        return NO_ACCESS
    return ERROR


async def discover(client, chat_id: int) -> Discovery:
    """Find the active voice chat in ``chat_id``, or say why there is none.

    Never raises for an expected outcome — a refusal is a :class:`Discovery`
    with a ``kind``, not an exception, because "there is no call" is a normal
    answer and not a fault. Only a genuinely unexpected failure inside this
    function itself would propagate, and the caller treats that as
    ``discovery_failed``.
    """
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return Discovery(UNSUPPORTED, detail="bad_chat_id")

    try:
        telethon = _telethon()
    except Exception as exc:  # noqa: BLE001 - absence is a reportable state
        return Discovery(ERROR, detail=f"telethon:{type(exc).__name__}")

    types = telethon.tl.types
    functions = telethon.tl.functions
    no_access = _no_access_types(telethon)

    # ── Resolve the peer ──
    try:
        peer = await client.get_input_entity(chat_id)
    except Exception as exc:  # noqa: BLE001 - mapped to a kind, not raised
        return Discovery(_classify(exc, no_access), detail=type(exc).__name__)

    # ── Ask for the full chat, which carries the call ──
    try:
        if isinstance(peer, types.InputPeerChannel):
            full = await client(
                functions.channels.GetFullChannelRequest(
                    types.InputChannel(peer.channel_id, peer.access_hash)
                )
            )
        elif isinstance(peer, types.InputPeerChat):
            full = await client(
                functions.messages.GetFullChatRequest(peer.chat_id)
            )
        else:
            return Discovery(UNSUPPORTED, detail=type(peer).__name__)
    except Exception as exc:  # noqa: BLE001 - mapped to a kind, not raised
        return Discovery(_classify(exc, no_access), detail=type(exc).__name__)

    full_chat = getattr(full, "full_chat", None)
    call = getattr(full_chat, "call", None)
    if call is None:
        # Telegram's answer, not a failure: this group has no voice chat open.
        return Discovery(NONE)

    slug_type = getattr(types, "InputGroupCallSlug", None)
    call_types = (types.InputGroupCall,) + ((slug_type,) if slug_type else ())
    if not isinstance(call, call_types):
        # A future layer's call object we do not understand. Say so rather than
        # pretending it is absent.
        return Discovery(NONE, detail=type(call).__name__)

    # ── A call exists. Is it live, or merely scheduled? ──
    #
    # PyTgCalls refuses a call whose ``schedule_date`` is set, and it is right
    # to: "come into the call" cannot mean "wait until Tuesday". Detecting it
    # here turns that refusal into a sentence that says so.
    try:
        raw = await client(
            functions.phone.GetGroupCallRequest(call=call, limit=0)
        )
        schedule_date = getattr(getattr(raw, "call", None), "schedule_date", None)
    except Exception as exc:  # noqa: BLE001
        # We *did* find a call; failing to read its schedule must not hide it.
        # Report it active and let the join itself be the judge.
        log.debug(
            "[voice] could not read the schedule of the call (%s)",
            type(exc).__name__,
        )
        return Discovery(ACTIVE, input_call=call, detail="schedule_unknown")

    if schedule_date is not None:
        return Discovery(SCHEDULED, input_call=call)
    return Discovery(ACTIVE, input_call=call)
