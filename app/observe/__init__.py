"""Nexus runtime observation, conversation archive and incident investigation.

This package is the **production evidence system**. It records what Nexus
actually did, so a coding agent can later reconstruct a real conversation or a
single turn and debug from evidence rather than from a log tail.

It is deliberately a *leaf*: it imports `config` and the standard library, and
nothing that could send, delete, restrict or ask a model. That is what lets every
other module — including the Gemini pool, which must never reach Telegram —
emit to it safely. It never imports `telegram`, `main`, `chat`, `db` or any
authority module, and nothing in the authority path imports it.

The rules this package is built to, and that its tests pin:

* **Telemetry is never authority.** An event is a record of what happened. No
  decision anywhere may read the archive: `rbac` and `admin_service` do not
  import it, and a model's own words are stored as *evidence*, never replayed as
  a command.
* **A failure here can never change Nexus.** Every write is off the response
  path; `emit()` is a no-op until a collector is started, and every stage of the
  collector is wrapped so an exception is counted and dropped, never raised. If
  the store is unreadable, the queue is full or the disk is gone, Nexus answers
  exactly as it would have.
* **The archive is a separate, operator-only store.** It lives in its own SQLite
  file under `/data/observability`, with its own connection and its own lock, so
  it can neither contend with nor corrupt the production database. It is never
  served over HTTP, never reaches Telegram, and is never read into a prompt.

Two of this project's own invariants are about *not* keeping things, and this
package is a deliberate, owner-authorised exception to both — recorded here
rather than quietly broken:

* AgentMD §53.6 says no store holds a message body except the bounded
  conversation history. The archive holds message bodies **on purpose**: that is
  the whole point of being able to reconstruct a real bug. It is isolated to
  this store, which the conversation path never reads.
* AgentMD §53.11 says no raw audio is persisted. Audio capture here is
  **off by default** (`OBSERVE_AUDIO_ENABLED=false`) and, when an operator turns
  it on, is retained on a window of its own, independent of the metadata and
  transcript retention. Metadata and transcripts are on by default; the audio
  itself is the operator's explicit choice.
"""

from __future__ import annotations

from . import schema
from .api import (
    counters,
    emit,
    enabled,
    error,
    flag,
    flush,
    mark_deployment,
    report_now,
    reset,
    start,
    started,
    stop,
    sweep,
    turn,
)
from .context import (
    Turn,
    conversation_id,
    current_turn,
    deployment_id,
    process_id,
)

__all__ = [
    "schema",
    "emit",
    "enabled",
    "start",
    "started",
    "stop",
    "flush",
    "sweep",
    "report_now",
    "turn",
    "counters",
    "mark_deployment",
    "flag",
    "error",
    "reset",
    "Turn",
    "current_turn",
    "conversation_id",
    "deployment_id",
    "process_id",
]
