"""Investigation queries over the archive.

Every function here is a read. Nothing in this module writes, and nothing in it
is imported by Nexus — it is the coding agent's and the operator's interface, not
the bot's. Each returns plain JSON-serialisable dicts so the CLI can print them
verbatim and an agent can consume them without parsing prose.

The queries are deliberately *named after the questions an investigator asks*
("which turns failed", "what did this turn actually do", "did this begin after
deployment X") rather than after the tables, because the table shape is an
implementation detail and the questions are the interface.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import config
from . import schema, store


def _since(seconds: int) -> float:
    return time.time() - max(0, int(seconds))


def _decode(row: Any) -> dict[str, Any]:
    out = dict(row)
    raw = out.get("data")
    if isinstance(raw, str) and raw:
        try:
            out["data"] = json.loads(raw)
        except Exception:  # noqa: BLE001 — a malformed blob is still evidence
            out["data"] = {"_unparsed": raw}
    elif raw in ("", None):
        out["data"] = {}
    return out


# ── generic finder, which the named queries are built on ──────────────────
def find(
    *,
    seconds: int = 86400,
    kind: str = "",
    kinds: tuple[str, ...] = (),
    event: str = "",
    ok: bool | None = None,
    text: str = "",
    chat_id: int = 0,
    user_id: int = 0,
    turn_id: str = "",
    message_id: int = 0,
    deployment_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Events matching every clause given. All clauses are ANDed."""
    where = ["at >= ?"]
    args: list[Any] = [_since(seconds)]
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" for _ in kinds))
        args.extend(kinds)
    if event:
        where.append("event = ?")
        args.append(event)
    if ok is not None:
        where.append("ok = ?")
        args.append(1 if ok else 0)
    if text:
        where.append("text LIKE ?")
        args.append(f"%{text}%")
    if chat_id:
        where.append("chat_id = ?")
        args.append(int(chat_id))
    if user_id:
        where.append("user_id = ?")
        args.append(int(user_id))
    if turn_id:
        where.append("turn_id = ?")
        args.append(turn_id)
    if message_id:
        where.append("message_id = ?")
        args.append(int(message_id))
    if deployment_id:
        where.append("deployment_id = ?")
        args.append(deployment_id)
    sql = (
        "SELECT id, at, kind, deployment_id, turn_id, trace_id, conversation_id,"
        " chat_id, user_id, message_id, event, text, data, ok, error, duration_ms"
        f" FROM events WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT ?"
    )
    args.append(max(1, min(int(limit), 2000)))
    return [_decode(row) for row in store.rows(sql, tuple(args))]


def _named(seconds: int, *, kind: str, event: str = "", ok: bool | None = None,
           limit: int = 100, **extra: Any) -> dict[str, Any]:
    found = find(seconds=seconds, kind=kind, event=event, ok=ok, limit=limit, **extra)
    return {
        "query": {"kind": kind, "event": event, "ok": ok, "seconds": seconds},
        "count": len(found),
        "events": found,
    }


# ── the questions ─────────────────────────────────────────────────────────
def status() -> dict[str, Any]:
    """What the archive is, where it lives, and how big it is."""
    counts = store.counts()
    size = store.size_bytes()
    return {
        "enabled": bool(config.OBSERVE_ENABLED),
        "path": store.path(),
        "directory": store.directory(),
        "retention_seconds": int(config.OBSERVE_RETENTION_SECONDS),
        "retention_human": _human(config.OBSERVE_RETENTION_SECONDS),
        "audio_enabled": bool(config.OBSERVE_AUDIO_ENABLED),
        "audio_retention_seconds": int(config.OBSERVE_AUDIO_RETENTION_SECONDS),
        "text_chars_cap": int(config.OBSERVE_TEXT_CHARS),
        "max_bytes": int(config.OBSERVE_MAX_BYTES),
        "size_bytes": size,
        "counts": counts,
        "oldest_at": store.oldest(),
        "newest_at": store.newest(),
        "deployments": deployments(),
    }


def health() -> dict[str, Any]:
    """A deterministic verdict, so an agent can check the archive in one call."""
    checks: list[dict[str, Any]] = []
    problems: list[str] = []

    try:
        counts = store.counts()
        reachable = counts.get("events", -1) >= 0
    except Exception as exc:  # noqa: BLE001
        counts, reachable = {}, False
        checks.append({"name": "store_reachable", "ok": False, "detail": str(exc)})
    if reachable:
        checks.append({"name": "store_reachable", "ok": True, "detail": counts})

    size = store.size_bytes()
    over = size > int(config.OBSERVE_MAX_BYTES) > 0
    checks.append(
        {
            "name": "under_size_limit",
            "ok": not over,
            "detail": {"size_bytes": size, "max_bytes": int(config.OBSERVE_MAX_BYTES)},
        }
    )
    if over:
        problems.append(
            "the archive is over OBSERVE_MAX_BYTES; shorten OBSERVE_RETENTION_SECONDS "
            "or raise the limit deliberately — evidence is never dropped silently"
        )

    newest = store.newest()
    checks.append(
        {
            "name": "has_events",
            "ok": newest > 0,
            "detail": {"newest_at": newest, "events": counts.get("events", 0)},
        }
    )
    if newest <= 0:
        problems.append("no events recorded yet")

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "problems": problems,
        "counters": counters(),
        "status": status(),
    }


def counters() -> dict[str, Any]:
    """The live writer's counters, if this process is the one running it."""
    try:
        from . import api

        return api.counters()
    except Exception:  # noqa: BLE001
        return {}


def recent(seconds: int = 86400, *, limit: int = 50, chat_id: int = 0) -> dict[str, Any]:
    """The newest turns, most recent first."""
    return turns(seconds=seconds, limit=limit, chat_id=chat_id)


def turns(
    *,
    seconds: int = 86400,
    limit: int = 100,
    chat_id: int = 0,
    conversation_id: str = "",
    kind: str = "",
    outcome: str = "",
    deployment_id: str = "",
) -> dict[str, Any]:
    where = ["started_at >= ?"]
    args: list[Any] = [_since(seconds)]
    if chat_id:
        where.append("chat_id = ?")
        args.append(int(chat_id))
    if conversation_id:
        where.append("conversation_id = ?")
        args.append(conversation_id)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if outcome:
        where.append("outcome = ?")
        args.append(outcome)
    if deployment_id:
        where.append("deployment_id = ?")
        args.append(deployment_id)
    sql = (
        "SELECT turn_id, trace_id, conversation_id, chat_id, user_id, message_id,"
        " kind, deployment_id, started_at, ended_at, outcome, reason, inbound,"
        " outbound, duration_ms FROM turns"
        f" WHERE {' AND '.join(where)} ORDER BY started_at DESC LIMIT ?"
    )
    args.append(max(1, min(int(limit), 2000)))
    found = [dict(row) for row in store.rows(sql, tuple(args))]
    return {"count": len(found), "turns": found}


def failures(
    *, seconds: int = 86400, limit: int = 200, stage: str = "", chat_id: int = 0
) -> dict[str, Any]:
    """Everything that went wrong: failed events *and* turns that did not send."""
    events = find(seconds=seconds, ok=False, event=stage, limit=limit, chat_id=chat_id)
    bad_turns = turns(
        seconds=seconds, limit=limit, chat_id=chat_id, outcome="failed"
    )["turns"]
    return {
        "seconds": seconds,
        "failed_events": len(events),
        "failed_turns": len(bad_turns),
        "events": events,
        "turns": bad_turns,
    }


def trace(turn_id: str) -> dict[str, Any]:
    """One turn, completely: its row and every event, in order."""
    turn = store.one("SELECT * FROM turns WHERE turn_id=?", (turn_id,))
    events = store.rows(
        "SELECT id, at, kind, event, text, data, ok, error, duration_ms,"
        " deployment_id, process_id, chat_id, user_id, message_id"
        " FROM events WHERE turn_id=? ORDER BY at ASC, id ASC",
        (turn_id,),
    )
    return {
        "turn_id": turn_id,
        "found": bool(turn) or bool(events),
        "turn": dict(turn) if turn else None,
        "events": [_decode(row) for row in events],
    }


def trace_by_trace_id(trace_id: str) -> dict[str, Any]:
    events = store.rows(
        "SELECT id, at, kind, event, text, data, ok, error, duration_ms"
        " FROM events WHERE trace_id=? ORDER BY at ASC, id ASC",
        (trace_id,),
    )
    return {"trace_id": trace_id, "found": bool(events), "events": [_decode(r) for r in events]}


def conversation(conversation_id: str, *, seconds: int = 604800, limit: int = 200) -> dict[str, Any]:
    """One person's thread in one room, in order — the real conversation."""
    found = turns(seconds=seconds, conversation_id=conversation_id, limit=limit)
    events = find(seconds=seconds, limit=limit * 5)
    events = [e for e in events if e.get("conversation_id") == conversation_id]
    events.reverse()
    return {
        "conversation_id": conversation_id,
        "turns": list(reversed(found["turns"])),
        "events": events,
    }


def by_message(message_id: int, *, seconds: int = 604800, limit: int = 200) -> dict[str, Any]:
    events = find(seconds=seconds, message_id=message_id, limit=limit)
    events.reverse()
    return {"message_id": message_id, "count": len(events), "events": events}


def search_events(text: str, *, seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """Full-text search over recorded message text, answers and transcripts."""
    found = find(seconds=seconds, text=text, limit=limit)
    return {"query": text, "seconds": seconds, "count": len(found), "events": found}


def search_conversations(text: str, *, seconds: int = 604800, limit: int = 100) -> dict[str, Any]:
    """Search what people said and what Nexus answered."""
    like = f"%{text}%"
    sql = (
        "SELECT turn_id, trace_id, conversation_id, chat_id, user_id, message_id,"
        " kind, deployment_id, started_at, outcome, reason, inbound, outbound,"
        " duration_ms FROM turns WHERE started_at >= ?"
        " AND (inbound LIKE ? OR outbound LIKE ? OR reason LIKE ?)"
        " ORDER BY started_at DESC LIMIT ?"
    )
    found = [
        dict(row)
        for row in store.rows(
            sql, (_since(seconds), like, like, like, max(1, min(int(limit), 2000)))
        )
    ]
    return {"query": text, "seconds": seconds, "count": len(found), "turns": found}


def incidents(description: str, *, seconds: int = 604800, limit: int = 200) -> dict[str, Any]:
    """Find a described bug: search failures, then the turns around them.

    This is the entry point for "find the bug I described": the words are matched
    against what people said, what Nexus answered, the reason a turn failed and
    the error text, and the turns that match are returned so the investigator can
    reconstruct each one.
    """
    like = f"%{description}%"
    sql = (
        "SELECT turn_id, trace_id, conversation_id, chat_id, user_id, message_id,"
        " kind, deployment_id, started_at, outcome, reason, inbound, outbound,"
        " duration_ms FROM turns WHERE started_at >= ?"
        " AND (inbound LIKE ? OR outbound LIKE ? OR reason LIKE ?)"
        " ORDER BY started_at DESC LIMIT ?"
    )
    matched_turns = [
        dict(row)
        for row in store.rows(
            sql, (_since(seconds), like, like, like, max(1, min(int(limit), 2000)))
        )
    ]
    matched_events = find(seconds=seconds, text=description, limit=limit)
    errors = find(seconds=seconds, ok=False, text=description, limit=limit)
    return {
        "description": description,
        "seconds": seconds,
        "turns": matched_turns,
        "events": matched_events,
        "errors": errors,
        "hint": "use `trace <turn_id>` on any turn_id above to reconstruct it",
    }


# ── the named failure finders the brief enumerates ────────────────────────
#
# Each one is a predicate over the events the instrumentation actually writes,
# not a name invented here and hoped for. The `event` labels below are the ones
# `app/main.py`, `app/chat.py`, `app/transcribe.py` and `app/gemini_pool.py`
# emit; a change to one of those labels without a change here would make a
# finder silently return nothing, so each is pinned by a test.
def empty_responses(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """A model turn that produced no usable answer."""
    return _named(seconds, kind=schema.KIND_AI_DONE, ok=False, limit=limit)


def reply_target_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """A reply addressed to Nexus that the routing gate did not route."""
    return _named(seconds, kind=schema.KIND_ROUTING, event="missed", limit=limit)


def named_target_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """A tag directive whose named person could not be resolved."""
    return _named(
        seconds, kind=schema.KIND_ROUTING, event="target_unresolved", limit=limit
    )


def transcription_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    return _named(
        seconds, kind=schema.KIND_VOICE, event="transcribe", ok=False, limit=limit
    )


def tts_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    return _named(seconds, kind=schema.KIND_VOICE, event="tts", ok=False, limit=limit)


def delivery_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """Anything Telegram refused: a text send, a partial send or a voice send."""
    return _named(seconds, kind=schema.KIND_DELIVERY, ok=False, limit=limit)


def voice_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    return _named(seconds, kind=schema.KIND_VOICE, ok=False, limit=limit)


def awareness_anomalies(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """Passes that did not reach a decision, or that raised."""
    found = find(seconds=seconds, kind=schema.KIND_AWARENESS, ok=False, limit=limit)
    silent = find(
        seconds=seconds, kind=schema.KIND_AWARENESS, event="incomplete", limit=limit
    )
    return {"failed": len(found), "incomplete": len(silent), "events": found + silent}


def timeouts(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    return _named(seconds, kind=schema.KIND_POOL, event="time_budget", limit=limit)


def retries(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    return _named(seconds, kind=schema.KIND_POOL, event="retry", limit=limit)


def provider_failures(seconds: int = 86400, limit: int = 100) -> dict[str, Any]:
    """A pool request that ended without a usable answer, whatever the cause."""
    return _named(seconds, kind=schema.KIND_POOL, ok=False, limit=limit)


def deployments() -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.rows(
            "SELECT deployment_id, at, sha, image, note, pid FROM deployments ORDER BY at DESC"
        )
    ]


def compare(before: str, after: str, *, seconds: int = 604800) -> dict[str, Any]:
    """Behaviour on one deployment beside another.

    The question this answers is "did this begin after deployment X", so it
    reports the same outcome and failure counts for each side, plus which error
    signatures appear only on the newer one.
    """
    def side(deployment_id: str) -> dict[str, Any]:
        rows = store.rows(
            "SELECT outcome, COUNT(*) AS n, AVG(duration_ms) AS avg_ms"
            " FROM turns WHERE deployment_id=? AND started_at>=?"
            " GROUP BY outcome",
            (deployment_id, _since(seconds)),
        )
        outcomes = {row["outcome"] or "none": row["n"] for row in rows}
        errors = store.rows(
            "SELECT error, COUNT(*) AS n FROM events"
            " WHERE deployment_id=? AND at>=? AND ok=0 GROUP BY error"
            " ORDER BY n DESC LIMIT 20",
            (deployment_id, _since(seconds)),
        )
        return {
            "deployment_id": deployment_id,
            "turns": sum(outcomes.values()),
            "outcomes": outcomes,
            "errors": {row["error"]: row["n"] for row in errors},
        }

    first, second = side(before), side(after)
    new = {k: v for k, v in second["errors"].items() if k not in first["errors"]}
    gone = {k: v for k, v in first["errors"].items() if k not in second["errors"]}
    return {
        "before": first,
        "after": second,
        "new_error_signatures": new,
        "gone_error_signatures": gone,
    }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return float(ordered[index])


def summarize(seconds: int = 86400) -> dict[str, Any]:
    """The counts a report is made of. Deterministic, no model involved."""
    since = _since(seconds)
    turns_rows = store.rows(
        "SELECT kind, outcome, duration_ms, deployment_id FROM turns WHERE started_at>=?",
        (since,),
    )
    outcomes: dict[str, int] = {}
    kinds: dict[str, int] = {}
    durations: list[float] = []
    deployments_seen: dict[str, int] = {}
    for row in turns_rows:
        outcomes[row["outcome"] or "none"] = outcomes.get(row["outcome"] or "none", 0) + 1
        kinds[row["kind"] or ""] = kinds.get(row["kind"] or "", 0) + 1
        if row["duration_ms"]:
            durations.append(float(row["duration_ms"]))
        deployments_seen[row["deployment_id"] or "unknown"] = (
            deployments_seen.get(row["deployment_id"] or "unknown", 0) + 1
        )

    def count(sql: str, args: tuple = ()) -> int:
        row = store.one(sql, (since, *args))
        return int(row[0]) if row and row[0] is not None else 0

    total = len(turns_rows)
    failed = outcomes.get("failed", 0)
    errors = store.rows(
        "SELECT error, COUNT(*) AS n FROM events WHERE at>=? AND ok=0 AND error<>''"
        " GROUP BY error ORDER BY n DESC LIMIT 25",
        (since,),
    )
    newest_deployment = ""
    marker = store.one("SELECT deployment_id FROM deployments ORDER BY at DESC LIMIT 1")
    if marker:
        newest_deployment = marker[0]
    new_patterns = {}
    if newest_deployment:
        rows = store.rows(
            "SELECT error, COUNT(*) AS n FROM events WHERE at>=? AND ok=0 AND error<>''"
            " AND deployment_id=? GROUP BY error",
            (since, newest_deployment),
        )
        older = {
            row["error"]
            for row in store.rows(
                "SELECT DISTINCT error FROM events WHERE at>=? AND ok=0"
                " AND deployment_id<>?",
                (since, newest_deployment),
            )
        }
        new_patterns = {row["error"]: row["n"] for row in rows if row["error"] not in older}

    return {
        "seconds": seconds,
        "since": since,
        "generated_at": time.time(),
        "total_turns": total,
        "successful_turns": outcomes.get("sent", 0),
        "failed_turns": failed,
        "outcomes": outcomes,
        "turns_by_kind": kinds,
        "voice_turns": kinds.get("voice", 0),
        "awareness_passes": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=?", (schema.KIND_AWARENESS,)
        ),
        "empty_responses": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND ok=0",
            (schema.KIND_AI_DONE,),
        ),
        "retries": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND event=?",
            (schema.KIND_POOL, "retry"),
        ),
        "timeouts": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND event=? AND ok=0",
            (schema.KIND_POOL, "time_budget"),
        ),
        "provider_failures": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND ok=0",
            (schema.KIND_POOL,),
        ),
        "delivery_failures": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND ok=0",
            (schema.KIND_DELIVERY,),
        ),
        "transcription_failures": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND event=? AND ok=0",
            (schema.KIND_VOICE, "transcribe"),
        ),
        "tts_failures": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND event=? AND ok=0",
            (schema.KIND_VOICE, "tts"),
        ),
        "reply_target_failures": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND event=?",
            (schema.KIND_ROUTING, "missed"),
        ),
        "named_target_failures": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND event=?",
            (schema.KIND_ROUTING, "target_unresolved"),
        ),
        "awareness_anomalies": count(
            "SELECT COUNT(*) FROM events WHERE at>=? AND kind=? AND ok=0",
            (schema.KIND_AWARENESS,),
        ),
        "queue_delays": {
            "max_ms": float(
                (store.one(
                    "SELECT MAX(duration_ms) FROM events WHERE at>=? AND kind=?",
                    (since, schema.KIND_QUEUE),
                ) or [0])[0]
                or 0.0
            ),
        },
        "latency_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else 0.0,
        },
        "error_signatures": {row["error"]: row["n"] for row in errors},
        "repeated_incidents": {
            row["error"]: row["n"] for row in errors if row["n"] > 1
        },
        "new_error_patterns": new_patterns,
        "deployments_seen": deployments_seen,
        "newest_deployment": newest_deployment,
        "archive": {
            "size_bytes": store.size_bytes(),
            "counts": store.counts(),
        },
    }


def _human(seconds: int) -> str:
    value = int(seconds or 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if value and value % size == 0:
            return f"{value // size}{unit}"
    return f"{value}s"
