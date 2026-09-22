"""The room's context for a live call, read through the existing awareness.

This module exists because of one instruction, and the instruction is the whole
design: **Voice Live must not grow a second awareness.** It must ask the same
awareness the text side already has, and it must ask it read-only.

What that rules out is easy to state and easy to get wrong. A live session
cannot read ``app/db.py`` directly — a second reader of the message window would
be a second definition of what a room's context *is*, and the two would drift
the first time either changed. It cannot read the admin tables, because
``awareness_context`` already renders administrative history through
``db.audit_since`` and the audit trail is a thing this bot has exactly one
reader of. It cannot read the message history at all, except through
``awareness.window``, which is the same call an awareness pass makes.

So there is exactly one path here, and it is two calls:

    ctx = awareness_context.build_ctx(chat_id)
    text = awareness_context.blocks(ctx)

Both are the existing module's public surface. ``build_ctx`` resolves the room
name, the message window, the anchor and the roles; ``blocks`` renders the
staged sources and enforces the ceiling. Nothing in this file decides what
context is — it decides *when to ask*, and that is a different question.

Why "when" is a question at all
-------------------------------
A live call is continuous and an awareness pass is not. If the bridge rebuilt
its context for every utterance, a five-minute conversation would run a hundred
queries and rebuild a hundred identical strings, because a room's name and its
administrative history do not change between two sentences. That is the
"snapshot, not full history per utterance" requirement, and it is why this is a
*cache with a refresh policy* rather than a function.

The policy is deliberately simple and stated in one place: a snapshot is reused
for ``GEMINI_LIVE_CONTEXT_TTL_SECONDS``, and ``refresh`` rebuilds it early when
the call has a reason to (a turn is about to be answered and the snapshot is
older than the TTL; the session was just reconnected; somebody asked). A refresh
reports whether anything actually changed, so the session can decide whether the
model needs to be told at all.

What the bridge will not do
---------------------------
* **It will not write.** Not to the database, not to the room cache, not to the
  awareness switch. ``note_room`` in particular is *not* called here even though
  it would make the room's name available: the message handler already calls it
  for every message, and the join command is a message, so the cache is filled
  by the existing path. A read-only bridge that writes "just one cache" is not
  read-only.
* **It will not cross rooms.** One bridge is one ``chat_id``, fixed at
  construction and carried into every call it makes. A bridge with no room is
  refused rather than defaulted, because ``build_ctx(0)`` would read an empty
  window and answer confidently about nowhere.
* **It will not pass a secret through.** The block is built from room names,
  display names and audit rows, none of which should ever hold a credential —
  but a display name is whatever a person typed, and a backstop is cheap. Every
  snapshot is scanned for the credential shapes this deployment uses and any hit
  is redacted before the text can reach a prompt. That is a second line, not the
  first: the first is that nothing sensitive is put in the block.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass

from .. import awareness_context, config

log = logging.getLogger("guardbot.voice.awareness")

# The shapes a credential in this deployment can take. Deliberately narrow and
# anchored on the provider's own prefixes rather than "anything that looks
# random", because a broad pattern would redact ordinary Persian text and a
# redacted room name is a context block that reads as broken.
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{10,}"),
)
_REDACTED = "[redacted]"

# How a refresh says why it happened. Machine keys, like everywhere else in this
# package: the caller branches on them and the log carries them verbatim.
REFRESH_COLD = "cold"
REFRESH_TTL = "ttl"
REFRESH_FORCED = "forced"
REFRESH_FRESH = "fresh"


@dataclass(frozen=True)
class Snapshot:
    """One room's context, as it was when it was built.

    ``sources`` is the set of source *names* the room's state called for — not
    the set that survived the character ceiling. The distinction is stated here
    because the text is the authority and this is a description of it: a source
    clipped away by the ceiling is still one the batch asked for, and reporting
    it as absent would make the block look like it had failed.
    """

    chat_id: int
    text: str
    built_at: float
    sources: tuple[str, ...] = ()
    fingerprint: str = ""

    def age(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.monotonic()) - self.built_at)

    def describe(self) -> dict:
        """A safe summary: how big, how old, which sources. Never the text."""
        return {
            "chat_id": self.chat_id,
            "chars": len(self.text),
            "age_seconds": round(self.age(), 1),
            "sources": list(self.sources),
            "fingerprint": self.fingerprint,
            "empty": not self.text,
        }


@dataclass(frozen=True)
class Refresh:
    """The result of asking for a snapshot, and whether anything moved.

    ``changed`` is the answer to the only question the caller has: does the
    model need to be told that the room is different now? Re-sending an
    unchanged block costs tokens on every turn and tells the model nothing, so
    an unchanged refresh is reported as exactly that rather than as a no-op the
    caller has to infer.
    """

    snapshot: Snapshot
    changed: bool
    reason: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def describe(self) -> dict:
        return {
            "changed": self.changed,
            "reason": self.reason,
            "added": list(self.added),
            "removed": list(self.removed),
            **self.snapshot.describe(),
        }


class AwarenessContextBridge:
    """One room's context, cached, for the life of one call.

    Not shared between sessions: the cache is per room and per call, because a
    second call in the same room starting from the first call's snapshot would
    inherit a context built before it existed. The cost of that choice is one
    query per call, which is the cheapest thing in this whole subsystem.
    """

    def __init__(self, chat_id: int, *, ttl: float | None = None) -> None:
        chat_id = int(chat_id or 0)
        if not chat_id:
            # Refused rather than defaulted. A bridge with no room would read an
            # empty window and describe nowhere, and every downstream consumer
            # would treat that as a real answer about a real room.
            raise ValueError("an awareness bridge needs a chat id")
        self.chat_id = chat_id
        self.ttl = (
            float(ttl)
            if ttl is not None
            else max(1.0, float(config.GEMINI_LIVE_CONTEXT_TTL_SECONDS))
        )
        self._snapshot: Snapshot | None = None
        self._builds = 0
        self._refreshes = 0

    # -- reads --
    def snapshot(self, *, force: bool = False, now: float | None = None) -> Snapshot:
        """The current snapshot, building it if there is none or it is stale.

        A convenience over ``refresh`` for callers that only want the text. It
        still goes through the same build, so there is one implementation of
        "what is this room's context".
        """
        return self.refresh(force=force, now=now).snapshot

    def refresh(self, *, force: bool = True, now: float | None = None) -> Refresh:
        """Rebuild if needed, and report whether the result differs.

        ``force=False`` is the turn path: it returns the cached snapshot when it
        is inside its TTL, and only rebuilds when it is not. That is what keeps a
        long conversation from running a query per sentence while still noticing
        a room that has changed.
        """
        moment = now if now is not None else time.monotonic()
        current = self._snapshot
        if current is not None and not force and current.age(moment) < self.ttl:
            self._refreshes += 1
            return Refresh(snapshot=current, changed=False, reason=REFRESH_FRESH)

        built = self._build(moment)
        if current is None:
            reason = REFRESH_COLD
        elif force:
            reason = REFRESH_FORCED
        else:
            reason = REFRESH_TTL
        changed = built.fingerprint != (current.fingerprint if current else "")
        added, removed = _diff(current, built)
        self._snapshot = built
        self._builds += 1
        self._refreshes += 1
        if changed:
            log.info(
                "[voice] context refreshed chat=%s reason=%s chars=%d "
                "sources=%s added=%s removed=%s",
                self.chat_id,
                reason,
                len(built.text),
                ",".join(built.sources) or "-",
                ",".join(added) or "-",
                ",".join(removed) or "-",
            )
        return Refresh(
            snapshot=built,
            changed=changed,
            reason=reason,
            added=added,
            removed=removed,
        )

    def stale(self, now: float | None = None) -> bool:
        """Whether the cached snapshot is past its TTL. True when there is none."""
        if self._snapshot is None:
            return True
        moment = now if now is not None else time.monotonic()
        return self._snapshot.age(moment) >= self.ttl

    def prompt_block(self, *, now: float | None = None) -> str:
        """The context text to hand the model, refreshing if it has gone stale.

        This is the only method the session needs on the hot path, and it is
        deliberately the one that decides for itself: a caller that had to
        remember to refresh first would forget, and the failure would be an
        assistant answering from a stale room rather than an error anyone could
        see.
        """
        return self.refresh(force=False, now=now).snapshot.text

    def describe(self) -> dict:
        """Safe state for the status line: sizes and counts, never the text."""
        return {
            "chat_id": self.chat_id,
            "ttl_seconds": self.ttl,
            "builds": self._builds,
            "refreshes": self._refreshes,
            "snapshot": self._snapshot.describe() if self._snapshot else None,
        }

    def clear(self) -> None:
        """Forget the snapshot. Called when a session ends."""
        self._snapshot = None

    # -- the one place context is built --
    def _build(self, moment: float) -> Snapshot:
        """Ask the existing awareness for this room's context.

        Every failure is caught and turned into an empty snapshot. A context
        block is worth a lot and is never worth a call: if the database is
        unavailable, the honest outcome is a session that answers without
        context, not a session that drops. The same reasoning
        ``awareness_context.blocks`` applies to a single source, applied to the
        whole thing — because from here, the whole thing is one source.
        """
        try:
            ctx = awareness_context.build_ctx(
                self.chat_id, now=int(time.time())
            )
            text = awareness_context.blocks(ctx)
            wanted = _wanted_sources(ctx)
        except Exception:  # noqa: BLE001 - context is never worth a call
            log.exception("[voice] could not build the room context chat=%s",
                          self.chat_id)
            text, wanted = "", ()
        text = _scrub(text)
        return Snapshot(
            chat_id=self.chat_id,
            text=text,
            built_at=moment,
            sources=wanted,
            fingerprint=_fingerprint(text),
        )


def _wanted_sources(ctx) -> tuple[str, ...]:
    """Which sources this room's state calls for, by name.

    Read from the same registry ``blocks`` walks, using the same public
    predicates — the tier test and ``Source.when``. This is a *description* of
    the snapshot, not a second renderer: the text always comes from ``blocks``,
    so there is no second definition of the context anywhere.
    """
    deep = bool(config.NEXUS_AWARENESS_CONTEXT_DEEP)
    names: list[str] = []
    for source in awareness_context.SOURCES:
        if source.tier != awareness_context.TIER_ALWAYS and not deep:
            continue
        try:
            if source.when(ctx):
                names.append(source.name)
        except Exception:  # noqa: BLE001 - a predicate is never worth a call
            log.exception("[voice] context source %r predicate failed", source.name)
    return tuple(names)


def _diff(before: Snapshot | None, after: Snapshot) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Which sources appeared and which went away between two snapshots.

    Used for the log and for the decision to tell the model. Source names rather
    than a text diff, because a text diff of a rendered block would report a
    change every time a timestamp inside it aged by a second — which would make
    "changed" mean "always" and the cache pointless.
    """
    if before is None:
        return after.sources, ()
    old, new = set(before.sources), set(after.sources)
    return tuple(sorted(new - old)), tuple(sorted(old - new))


def _fingerprint(text: str) -> str:
    """A short, stable identity for a rendered block.

    Truncated to eight hex characters. It is compared for equality and printed
    in a log line; it is not a security primitive, and a full digest in a log is
    noise rather than rigour.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _scrub(text: str) -> str:
    """Redact anything shaped like a credential. A backstop, not the control.

    The control is that ``awareness_context`` renders room names, display names
    and audit rows — none of which is a secret. This exists because a display
    name is user-controlled, and "somebody set their Telegram name to an API key
    and it ended up in a prompt" is a cheap thing to make impossible.
    """
    if not text:
        return ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text
