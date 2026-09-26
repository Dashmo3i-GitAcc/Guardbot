"""Correlation: the deployment marker, the process, and the turn.

The model is deliberately small. One **turn** is one thing Nexus did for one
person — an addressed reply, a voice note answered, an awareness pass — and it
carries one `turn_id` that every event recorded while it was in flight inherits.
`trace_id` is the same value, kept as a separate name because it is the field a
human asks for ("trace this"), and `conversation_id` groups the turns of one
person in one room so a conversation can be replayed in order.

The current turn lives in a `ContextVar`, so a helper deep inside the chat or
transcription code can record an event against the turn it is part of without
that turn being threaded through every signature. It is context-local and
therefore asyncio-safe: two concurrent turns never see each other's.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# ── The deployment marker ─────────────────────────────────────────────────
#
# The image contains no `.git` (`.dockerignore` excludes it), so the version
# cannot be read from the working tree at runtime. The build writes it to a
# file instead (`Dockerfile`, `ARG GIT_SHA`), and this reads that file once and
# caches it. `GUARDBOT_BUILD_SHA` in the environment wins, which is what a
# probe or a test uses to pretend to be a different deployment. The path itself
# is read at call time rather than frozen at import, so a probe or a test can
# point it elsewhere without reloading the module.
_DEFAULT_BUILD_INFO = "/srv/BUILD_INFO"
_deployment: str | None = None

# A fresh id per process start, so two processes with the same pid — a restart —
# are still distinguishable in the archive.
_BOOT = uuid.uuid4().hex[:8]
_process = f"{os.getpid()}-{_BOOT}"


def deployment_id() -> str:
    """The version that produced an event: the built sha, or ``unknown``."""
    global _deployment
    if _deployment is not None:
        return _deployment
    value = (os.getenv("GUARDBOT_BUILD_SHA") or "").strip()
    if not value:
        path = os.getenv("GUARDBOT_BUILD_INFO") or _DEFAULT_BUILD_INFO
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
            if raw.startswith("{"):
                value = str(json.loads(raw).get("sha") or "").strip()
            else:
                value = raw.split()[0] if raw else ""
        except Exception:  # noqa: BLE001 — a missing marker is not a failure
            value = ""
    _deployment = value or "unknown"
    return _deployment


def process_id() -> str:
    """This process: pid plus the id it was given when it started."""
    return _process


def conversation_id(chat_id: int, user_id: int) -> str:
    """One person's thread in one room. The tenant key is still `chat_id`."""
    return f"{int(chat_id or 0)}:{int(user_id or 0)}"


# ── The turn ──────────────────────────────────────────────────────────────
_CURRENT: ContextVar["Turn | None"] = ContextVar("observe_turn", default=None)


@dataclass
class Turn:
    """One thing Nexus did, and the id every event inside it inherits."""

    kind: str
    chat_id: int = 0
    user_id: int = 0
    message_id: int = 0
    conversation_id: str = ""
    trace_id: str = ""
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    outcome: str = ""
    reason: str = ""
    inbound: str = ""
    outbound: str = ""
    _ended: bool = False

    def __post_init__(self) -> None:
        if not self.conversation_id:
            self.conversation_id = conversation_id(self.chat_id, self.user_id)
        if not self.trace_id:
            self.trace_id = self.turn_id

    # -- emitting against this turn ---------------------------------------
    def emit(self, kind: str, **fields: Any) -> None:
        from .api import emit

        emit(
            kind,
            turn_id=self.turn_id,
            trace_id=self.trace_id,
            conversation_id=self.conversation_id,
            chat_id=self.chat_id,
            user_id=self.user_id,
            message_id=self.message_id,
            **fields,
        )

    def finish(
        self,
        outcome: str = "none",
        *,
        reason: str = "",
        text: str = "",
        **fields: Any,
    ) -> None:
        """Close the turn. Idempotent, and never raises."""
        if self._ended:
            return
        self._ended = True
        self.outcome = outcome
        self.reason = reason
        if text:
            self.outbound = text
        self.emit(
            "turn.ended",
            event=outcome,
            text=self.outbound,
            duration_ms=(time.time() - self.started_at) * 1000.0,
            data={"outcome": outcome, "reason": reason, **fields},
        )

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "Turn":
        self._token = _CURRENT.set(self)
        self.emit("turn.started", text=self.inbound, event=self.kind)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        token = getattr(self, "_token", None)
        if token is not None:
            _CURRENT.reset(token)
        if exc is not None:
            self.emit(
                "error",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                event="turn.unhandled",
            )
            self.finish("failed", reason=type(exc).__name__)
            # Never swallow: the caller's error handling still runs.
            return False
        if not self._ended:
            self.finish("none")
        return False


def turn(
    kind: str,
    *,
    chat_id: int = 0,
    user_id: int = 0,
    message_id: int = 0,
    text: str = "",
    conversation: str = "",
) -> Turn:
    """Mint a turn. It works even when observation is off (its emits drop)."""
    return Turn(
        kind=kind,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        inbound=text,
        conversation_id=conversation,
    )


def current_turn() -> "Turn | None":
    """The turn this task is inside, or ``None``."""
    return _CURRENT.get()
