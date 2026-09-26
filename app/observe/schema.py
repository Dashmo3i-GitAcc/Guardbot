"""The event vocabulary and the row shape.

Two things live here and nothing else: the **closed** list of event kinds, and
the coercion of a caller's keyword arguments into the fixed columns of a row.
A closed vocabulary is what makes the query interface deterministic — a kind is
either known or it is a bug, so a typo cannot silently create a new category
that no investigation query looks at.
"""

from __future__ import annotations

import json
import time
from typing import Any

# ── The vocabulary ────────────────────────────────────────────────────────
#
# Grouped by the stage of the runtime story each one belongs to. The names are
# stable identifiers: an investigation query, a report counter and a test all
# refer to them by these strings.
KIND_UPDATE = "update.received"          # an incoming Telegram event
KIND_DEDUP = "update.duplicate"          # the update guard refused a replay
KIND_BOUNDARY = "room.boundary"          # the room allowlist decision
KIND_ROUTING = "routing.decided"         # did Nexus consider itself addressed
KIND_TURN = "turn.started"               # a turn (conversation/voice/awareness)
KIND_TURN_END = "turn.ended"             # the same turn finished
KIND_CONTEXT = "context.composed"        # the context plan
KIND_AI = "ai.request"                   # a model request
KIND_AI_DONE = "ai.response"             # a model response (draft or final)
KIND_DELIVERY = "delivery"               # what Telegram actually received
KIND_AWARENESS = "awareness.pass"        # an awareness pass and its decision
KIND_QUEUE = "queue"                     # chat queue lifecycle
KIND_VOICE = "voice.stage"               # download/transcribe/route/tts/send
KIND_POOL = "pool.event"                 # retry, breaker, rate limit, discovery
KIND_ADMIN = "admin.command"             # an owner/admin command that changes behaviour
KIND_FLAG = "feature.flag"               # a feature-flag snapshot
KIND_ERROR = "error"                     # a caught failure, with its stage
KIND_DEPLOY = "deployment.marker"        # a boot, with the version that booted
KIND_REPORT = "report.generated"         # a daily observation report was written
KIND_CLEANUP = "retention.cleanup"       # retention ran, and what it removed
KIND_MEMORY = "memory.write"             # a memory/state write (bounded facts)

KINDS: frozenset[str] = frozenset(
    {
        KIND_UPDATE,
        KIND_DEDUP,
        KIND_BOUNDARY,
        KIND_ROUTING,
        KIND_TURN,
        KIND_TURN_END,
        KIND_CONTEXT,
        KIND_AI,
        KIND_AI_DONE,
        KIND_DELIVERY,
        KIND_AWARENESS,
        KIND_QUEUE,
        KIND_VOICE,
        KIND_POOL,
        KIND_ADMIN,
        KIND_FLAG,
        KIND_ERROR,
        KIND_DEPLOY,
        KIND_REPORT,
        KIND_CLEANUP,
        KIND_MEMORY,
    }
)

# The columns of an `events` row that a caller may set directly. Everything
# stage-specific goes into `data` (JSON), which keeps the table append-oriented
# and the indexes small.
COLUMNS: tuple[str, ...] = (
    "at",
    "kind",
    "deployment_id",
    "process_id",
    "turn_id",
    "trace_id",
    "conversation_id",
    "chat_id",
    "user_id",
    "message_id",
    "event",
    "text",
    "data",
    "ok",
    "error",
    "duration_ms",
)


def now() -> float:
    """Unix seconds, sub-second resolution. One clock for the whole package."""
    return time.time()


def known(kind: str) -> bool:
    return kind in KINDS


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def coerce(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a caller's dict into the row's fixed shape.

    Never raises and never invents a value: an unreadable number is zero and an
    unreadable blob is an empty object. A malformed event must cost its own
    record, not the writer's loop.
    """
    out: dict[str, Any] = {name: None for name in COLUMNS}
    out["at"] = _as_float(row.get("at")) or now()
    kind = str(row.get("kind") or "")
    out["kind"] = kind if kind in KINDS else KIND_ERROR
    out["deployment_id"] = str(row.get("deployment_id") or "")
    out["process_id"] = str(row.get("process_id") or "")
    out["turn_id"] = str(row.get("turn_id") or "")
    out["trace_id"] = str(row.get("trace_id") or "")
    out["conversation_id"] = str(row.get("conversation_id") or "")
    out["chat_id"] = _as_int(row.get("chat_id"))
    out["user_id"] = _as_int(row.get("user_id"))
    out["message_id"] = _as_int(row.get("message_id"))
    out["event"] = str(row.get("event") or "")
    out["text"] = str(row.get("text") or "")
    out["ok"] = 1 if row.get("ok", True) else 0
    out["error"] = str(row.get("error") or "")
    out["duration_ms"] = _as_float(row.get("duration_ms"))
    data = row.get("data")
    if isinstance(data, dict):
        out["data"] = json.dumps(data, ensure_ascii=False, default=str)
    elif isinstance(data, str):
        out["data"] = data
    else:
        out["data"] = "{}"
    return out
