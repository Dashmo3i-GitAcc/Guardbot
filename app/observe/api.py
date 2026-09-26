"""The public surface: `emit`, the turn factory, and lifecycle.

`emit()` is the one primitive every call site uses, and it is written to be safe
to call from anywhere in Nexus:

* it returns immediately when observation is off or no collector is running —
  the guard is a single attribute read, so an uninstrumented deployment pays
  nothing;
* it never raises: a malformed field, an unredactable string or a dead store all
  end in a dropped record, never in an exception inside a reply;
* it never waits: submission is a non-blocking queue append.

Nothing in this module reads the archive. Observation is a sink, never a source
of authority.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import config
from . import redact as _redact
from . import schema, store
from .collector import Collector
from .context import Turn, current_turn, deployment_id, process_id
from .context import turn as _turn

log = logging.getLogger("guardbot.observe")

_COLLECTOR: Collector | None = None


# ── lifecycle ─────────────────────────────────────────────────────────────
def enabled() -> bool:
    return bool(config.OBSERVE_ENABLED)


def started() -> bool:
    return _COLLECTOR is not None and _COLLECTOR.running()


async def start(*, worker: bool = True) -> bool:
    """Start observation. Returns False (and stays inert) if it cannot."""
    global _COLLECTOR
    if not config.OBSERVE_ENABLED:
        return False
    if _COLLECTOR is not None and _COLLECTOR.running():
        return True
    collector = Collector()
    if not await collector.start(worker=worker):
        # The archive could not be opened. Observation stays off rather than
        # half-on, so nothing queues against a store that will never accept it.
        _COLLECTOR = None
        return False
    _COLLECTOR = collector
    log.info(
        "[observe] archive on path=%s retention=%ss audio=%s queue=%s",
        store.path(),
        int(config.OBSERVE_RETENTION_SECONDS),
        bool(config.OBSERVE_AUDIO_ENABLED),
        int(config.OBSERVE_QUEUE_MAX),
    )
    return True


async def stop() -> None:
    global _COLLECTOR
    collector, _COLLECTOR = _COLLECTOR, None
    if collector is not None:
        await collector.stop()


async def flush() -> int:
    if _COLLECTOR is None:
        return 0
    return await _COLLECTOR.flush()


def counters() -> dict[str, Any]:
    if _COLLECTOR is None:
        return {"running": False, "written": 0, "failed": 0, "dropped": 0, "queued": 0}
    return _COLLECTOR.counters()


def reset() -> None:
    """Drop the collector and close the store. For tests and the CLI."""
    global _COLLECTOR
    _COLLECTOR = None
    store.close()


# ── emitting ──────────────────────────────────────────────────────────────
def emit(kind: str, **fields: Any) -> bool:
    """Record one event against the current turn. Never raises, never waits."""
    collector = _COLLECTOR
    if collector is None or not collector.running():
        return False
    try:
        cap = int(config.OBSERVE_TEXT_CHARS)
        active = current_turn()

        text = fields.pop("text", "")
        data = fields.pop("data", None) or {}
        error = fields.pop("error", "")

        text, clipped = _redact.clip(_redact.redact(str(text or "")), cap)
        if clipped:
            data = {**data, "clipped_chars": clipped}
        error, _ = _redact.clip(_redact.redact(str(error or "")), 2000)
        data = _redact.scrub(data, cap)

        row = {
            "kind": kind,
            "at": schema.now(),
            "deployment_id": deployment_id(),
            "process_id": process_id(),
            "event": str(fields.pop("event", "") or ""),
            "ok": bool(fields.pop("ok", True)),
            "duration_ms": fields.pop("duration_ms", 0.0),
            "text": text,
            "error": error,
            "data": data,
        }
        # Turn identity: explicit wins, else the turn this task is inside.
        for name in ("turn_id", "trace_id", "conversation_id"):
            value = fields.pop(name, None)
            if value is None and active is not None:
                value = getattr(active, name)
            row[name] = value or ""
        for name in ("chat_id", "user_id", "message_id"):
            value = fields.pop(name, None)
            if value in (None, 0) and active is not None:
                value = getattr(active, name)
            row[name] = int(value or 0)
        # Anything the caller still passed is stage-specific detail.
        if fields:
            data = {**data, **fields}
            row["data"] = _redact.scrub(data, cap)

        row = schema.coerce(row)
        # What the turn ultimately said. A delivery is the answer going out, so
        # it sets `outbound`; a model response sets it too, which is what gives a
        # voice turn — whose delivery carries no text — a readable outbound.
        # Setting it here rather than at each call site means every path updates
        # the turn without any of them having to know about the turn.
        if active is not None and row["text"]:
            if kind == schema.KIND_DELIVERY or kind == schema.KIND_AI_DONE:
                active.outbound = row["text"]
        ok = collector.submit("event", row)
        if kind == schema.KIND_TURN:
            collector.submit(
                "turn.open",
                {
                    "turn_id": row["turn_id"] or "",
                    "trace_id": row["trace_id"] or "",
                    "conversation_id": row["conversation_id"] or "",
                    "chat_id": row["chat_id"],
                    "user_id": row["user_id"],
                    "message_id": row["message_id"],
                    "kind": row["event"],
                    "deployment_id": row["deployment_id"],
                    "started_at": row["at"],
                    "inbound": row["text"],
                },
            )
        elif kind == schema.KIND_TURN_END:
            try:
                detail = __import__("json").loads(row["data"])
            except Exception:  # noqa: BLE001
                detail = {}
            collector.submit(
                "turn.close",
                {
                    "turn_id": row["turn_id"] or "",
                    "ended_at": row["at"],
                    "outcome": row["event"] or detail.get("outcome", ""),
                    "reason": str(detail.get("reason", "")),
                    "outbound": row["text"],
                    "duration_ms": row["duration_ms"],
                },
            )
        return ok
    except Exception as exc:  # noqa: BLE001 — never raise into Nexus
        try:
            collector.dropped += 1
            collector.last_error = f"emit: {type(exc).__name__}: {exc}"
        except Exception:  # noqa: BLE001
            pass
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
    """Mint a turn. Safe to call when observation is off — its emits drop."""
    return _turn(
        kind,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
        conversation=conversation,
    )


# ── named helpers, so a call site is one readable line ────────────────────
def mark_deployment(note: str = "boot", image: str = "") -> None:
    """Record this boot's version, and emit the marker that anchors it."""
    sha = deployment_id()
    pid = process_id()
    try:
        if started():
            store.record_deployment(
                sha, sha=sha, image=image, note=note, pid=pid
            )
    except Exception:  # noqa: BLE001
        pass
    emit(schema.KIND_DEPLOY, event=note, data={"sha": sha, "image": image})


def flag(name: str, value: Any) -> None:
    """A feature-flag snapshot, recorded so a report can explain a behaviour."""
    emit(schema.KIND_FLAG, event=name, data={"name": name, "value": value})


def error(stage: str, exc: BaseException, **fields: Any) -> None:
    """A caught failure, with the stage that caught it."""
    emit(
        schema.KIND_ERROR,
        ok=False,
        event=stage,
        error=f"{type(exc).__name__}: {exc}",
        **fields,
    )


# ── scheduled maintenance, off the response path ──────────────────────────
#
# Retention and the report run on the observation worker's own clock, never
# inside a turn. Both are wrapped so a failure is a returned error dict — never
# an exception inside a `job_queue` callback, which would be logged as a bot
# fault for something that only concerns the archive.
async def sweep() -> dict:
    """Run retention once, in a thread. Never raises."""
    if not started():
        return {"skipped": "not started"}
    try:
        from . import retention

        return await asyncio.to_thread(retention.sweep)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


async def report_now(seconds: int | None = None) -> dict:
    """Write one observation report, in a thread. Never raises."""
    if not started():
        return {"ok": False, "skipped": "not started"}
    try:
        from . import report

        window = int(seconds or config.OBSERVE_REPORT_INTERVAL_SECONDS)
        return await asyncio.to_thread(report.write, window)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
