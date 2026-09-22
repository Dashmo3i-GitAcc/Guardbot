"""Who is talking, decided by Telegram and never by the model.

This is the smallest module in the package and the one the whole security story
rests on. Everything else about a live call can be wrong in a way that is
annoying; getting this wrong means the assistant can be talked into acting as
somebody else.

The problem is that a voice conversation has no message to attribute. In a group
chat every action carries an ``actor_id`` that came from an ``Update`` the
Telegram servers signed. In a voice chat there is no update per utterance — just
a stream of audio frames, each tagged with an ``ssrc``, which is the transport's
identifier for a *stream* and not for a person.

The mapping from ``ssrc`` to a Telegram user id comes from the transport's own
participant list, which comes from Telegram. That is the only path by which an
identity enters this subsystem, and the model has no way to influence it: the
provider is told who is speaking for the sake of the conversation, and its
opinion about it is never read back as an identity.

Three rules follow, and they are the module:

1. **An unattributed utterance has no actor.** If the current stream is not in
   the participant map, the speaker is ``0`` — not "probably the last person",
   not "the only person here". ``0`` fails closed at every downstream check,
   because ``admin_service`` refuses a request with no actor as malformed.

2. **A claim is not an identity.** There is deliberately no method here that
   accepts a name, a username or an id *from the model*. Adding one would make
   "Nexus thinks this is the owner" a thing this code could express, and the
   point of the module is that it cannot.

3. **Stale is unknown.** A speaker who stopped sending frames is no longer the
   current speaker. An action requested two seconds after somebody's last frame
   is not attributed to them, because by then the audio that arrived is not
   theirs — and "whoever spoke most recently" is exactly the guess that turns a
   quiet room into a confused one.

The role attached to a speaker is a *label for the context*, read from
``app/rbac.py`` by the same function the rest of the bot uses. It is never a
check: every action is authorised again, from the actor id, in
``app/admin_service.py``. A role rendered into a prompt is a sentence for the
model to read, and reading it grants nothing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("guardbot.voice.speakers")

# How long a speaker stays "current" after their last frame. Two seconds is
# about as long as an utterance's tail; beyond it the stream has moved on and
# attributing a new request to the old speaker would be a guess.
SPEAKER_TTL_SECONDS = 2.0


@dataclass(frozen=True)
class Participant:
    """One person in the voice chat, as the transport describes them.

    ``ssrc`` is the transport's stream identifier and ``user_id`` is Telegram's.
    The pairing is the whole content of this record; everything else is for the
    context and for the log.
    """

    user_id: int
    ssrc: int = 0
    name: str = ""
    username: str = ""

    def describe(self) -> dict:
        """A safe summary for the log: ids and the name, never audio."""
        return {
            "user_id": self.user_id,
            "ssrc": self.ssrc,
            "name": self.name,
            "username": self.username,
        }


@dataclass
class SpeakerMap:
    """Which stream belongs to whom, and who is speaking now.

    Per session, because the participants and their streams belong to one call.
    ``_by_ssrc`` is the identity path and ``_by_user`` is its inverse, kept
    together so the two cannot drift: both are written by ``update`` and both are
    cleared by ``reset``.
    """

    _by_ssrc: dict[int, Participant] = field(default_factory=dict)
    _by_user: dict[int, Participant] = field(default_factory=dict)
    _current_ssrc: int = 0
    _current_at: float = 0.0
    # Injectable for the same reason the stopwatch's is: the TTL is the rule
    # this module exists to enforce, and a rule that can only be observed by
    # waiting two real seconds is a rule the tests cannot pin down.
    clock: object = time.monotonic

    def update(self, participants) -> int:
        """Replace the roster from the transport's participant list.

        Replaces rather than merges. A participant list is Telegram's answer for
        *now*, and merging it with what was there before would keep somebody who
        has left in the map for ever — which is a person who can be spoken about
        and, worse, attributed to.

        Returns how many people were mapped, for the log. Entries without a
        usable id or stream are skipped rather than stored with a zero: a zero
        ssrc would collide with "no stream" and a zero user id would collide
        with "unknown speaker", and both of those collisions resolve *towards*
        attribution, which is the wrong direction to fail.
        """
        by_ssrc: dict[int, Participant] = {}
        by_user: dict[int, Participant] = {}
        for raw in participants or ():
            person = _coerce(raw)
            if person is None:
                continue
            by_user[person.user_id] = person
            if person.ssrc:
                by_ssrc[person.ssrc] = person
        self._by_ssrc = by_ssrc
        self._by_user = by_user
        if self._current_ssrc not in self._by_ssrc:
            # The person who was talking is no longer in the call. Their audio
            # is not arriving, so they are not the current speaker.
            self._current_ssrc = 0
        return len(by_user)

    def note_frame(self, ssrc: int) -> Participant | None:
        """Record that a frame arrived on a stream. Returns who it belongs to.

        This is the only writer of "who is speaking". It is called from the
        media callback, so it does no work beyond a dictionary lookup and a
        timestamp.
        """
        ssrc = int(ssrc or 0)
        person = self._by_ssrc.get(ssrc)
        if person is None:
            # An unknown stream. It might be a participant Telegram has not
            # told us about yet, and it might be noise; either way it is not
            # attributable, and saying so is the safe answer.
            return None
        self._current_ssrc = ssrc
        self._current_at = self.clock()  # type: ignore[operator]
        return person

    def current(self, *, now: float | None = None) -> Participant | None:
        """The person speaking right now, or None when that is not knowable."""
        if not self._current_ssrc:
            return None
        moment = now if now is not None else self.clock()  # type: ignore[operator]
        if moment - self._current_at > SPEAKER_TTL_SECONDS:
            return None
        return self._by_ssrc.get(self._current_ssrc)

    def current_user_id(self, *, now: float | None = None) -> int:
        """The current speaker's Telegram id, or 0 when it cannot be known.

        ``0`` is the fail-closed answer and it is deliberately the same value an
        absent actor has everywhere else in this codebase. Nothing downstream
        has to know that voice exists to refuse it correctly.
        """
        person = self.current(now=now)
        return person.user_id if person else 0

    def user_for(self, ssrc: int) -> int:
        """The id behind one stream, or 0. For the media path and the tests."""
        person = self._by_ssrc.get(int(ssrc or 0))
        return person.user_id if person else 0

    def ssrc_for(self, user_id: int) -> int:
        """The stream belonging to one person, or 0. Used to attribute a frame
        the transport tagged by person rather than by stream."""
        person = self._by_user.get(int(user_id or 0))
        return person.ssrc if person else 0

    def participants(self) -> list[Participant]:
        """Everyone currently mapped, ordered by id so the order is stable."""
        return [self._by_user[uid] for uid in sorted(self._by_user)]

    def known(self, user_id: int) -> bool:
        """Whether a person is in the call as far as the transport knows.

        Not an authorisation check and not used as one. It answers "is this
        person here", which the context needs, and nothing else.
        """
        return int(user_id or 0) in self._by_user

    def clear(self) -> None:
        """Forget everyone. Called when a session ends."""
        self._by_ssrc.clear()
        self._by_user.clear()
        self._current_ssrc = 0
        self._current_at = 0.0

    def describe(self) -> dict:
        """A safe summary: how many, and who is current. No audio, no text."""
        return {
            "participants": len(self._by_user),
            "mapped_streams": len(self._by_ssrc),
            "current_user_id": self.current_user_id(),
        }


def _coerce(raw) -> Participant | None:
    """One participant from whatever the transport handed over.

    The transport is an interface, so this is written against three shapes
    rather than one: a ``Participant`` (the fake, and the real adapter's own
    type), a mapping (a plain dict from a JSON-ish source), and an object with
    the right attributes (the native library's own participant type, which this
    package does not import).

    Everything is read defensively and a missing id is a skip, not a zero — see
    ``update`` for why the direction of that failure matters.
    """
    if isinstance(raw, Participant):
        return raw if raw.user_id else None
    if isinstance(raw, dict):
        get = raw.get
    else:
        def get(key, default=None):
            return getattr(raw, key, default)

    try:
        user_id = int(get("user_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not user_id:
        return None
    try:
        ssrc = int(get("ssrc", 0) or get("source", 0) or 0)
    except (TypeError, ValueError):
        ssrc = 0
    return Participant(
        user_id=user_id,
        ssrc=ssrc,
        name=str(get("name", "") or get("first_name", "") or ""),
        username=str(get("username", "") or ""),
    )
