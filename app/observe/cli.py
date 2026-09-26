"""The investigation interface: `python -m app.observe <command>`.

Deterministic, read-only by default, and JSON on stdout, because the consumer is
a coding agent rather than a person reading a log tail. The commands are named
after the questions an investigator asks, so an agent does not need to know the
table shape:

    observe status                     what the archive is and how big
    observe health                     one deterministic verdict
    observe recent --last 24h          the newest turns
    observe failures --last 6h         everything that went wrong
    observe find transcription         a named failure class
    observe trace <turn_id>            one turn, completely
    observe conversation <id>          one person's thread, in order
    observe message <message_id>       everything about one Telegram message
    observe search "<text>"            search what was said and answered
    observe incidents "<description>"  find a described bug
    observe compare <dep_a> <dep_b>    behaviour before vs after a deployment
    observe summarize --last 24h       the counts a report is made of
    observe report --write             produce the JSON + Markdown report
    observe cleanup [--dry-run]        retention, and what it would remove
    observe deployments                the version markers

It runs in the container (`docker exec guardbot python -m app.observe ...`) and
needs no running bot: it opens the archive file directly.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import audio, query, report, retention, schema, store

WINDOWS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "48h": 48 * 3600,
    "3d": 3 * 86400,
    "7d": 7 * 86400,
}


def _window(value: str) -> int:
    """`24h`, `3d`, `90m`, or a bare number of seconds."""
    text = (value or "").strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("empty window")
    if text in WINDOWS:
        return WINDOWS[text]
    if text.endswith("h"):
        return int(float(text[:-1]) * 3600)
    if text.endswith("d"):
        return int(float(text[:-1]) * 86400)
    if text.endswith("m"):
        return int(float(text[:-1]) * 60)
    if text.endswith("s"):
        return int(float(text[:-1]))
    return int(text)


def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--last",
        "--since",
        dest="window",
        type=_window,
        default=86400,
        metavar="WINDOW",
        help="1h, 6h, 24h, 48h, 3d, 7d or seconds (default 24h)",
    )
    parser.add_argument("--limit", type=int, default=100)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.observe",
        description="Nexus production observation, archive and investigation.",
    )
    parser.add_argument("--json", action="store_true", help="force JSON (the default)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what the archive is, where, and how big")
    sub.add_parser("health", help="one deterministic health verdict")
    sub.add_parser("deployments", help="deployment markers")

    for name in ("recent", "turns"):
        p = sub.add_parser(name, help="the newest turns")
        _add_window(p)
        p.add_argument("--chat-id", type=int, default=0)
        p.add_argument("--kind", default="")
        p.add_argument("--outcome", default="")
        p.add_argument("--deployment", default="")

    p = sub.add_parser("failures", help="failed events and turns")
    _add_window(p)
    p.add_argument("--stage", default="", help="an exact event name to filter on")
    p.add_argument("--chat-id", type=int, default=0)

    p = sub.add_parser("find", help="a named failure class")
    _add_window(p)
    p.add_argument(
        "what",
        choices=(
            "empty", "reply-target", "named-target", "voice", "transcription",
            "tts", "delivery", "awareness", "timeouts", "retries", "provider",
        ),
    )

    p = sub.add_parser("trace", help="one turn, completely")
    p.add_argument("turn_id")

    p = sub.add_parser("trace-trace", help="every event under one trace id")
    p.add_argument("trace_id")

    p = sub.add_parser("conversation", help="one person's thread in one room")
    p.add_argument("conversation_id")
    p.add_argument("--last", dest="window", type=_window, default=7 * 86400)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("message", help="everything about one Telegram message")
    p.add_argument("message_id", type=int)
    p.add_argument("--last", dest="window", type=_window, default=7 * 86400)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("search", help="search recorded text")
    p.add_argument("text")
    _add_window(p)

    p = sub.add_parser("search-conversations", help="search what was said and answered")
    p.add_argument("text")
    _add_window(p)

    p = sub.add_parser("incidents", help="find a described bug")
    p.add_argument("description")
    _add_window(p)

    p = sub.add_parser("compare", help="behaviour before vs after a deployment")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--last", dest="window", type=_window, default=7 * 86400)

    p = sub.add_parser("summarize", help="the counts a report is made of")
    _add_window(p)

    p = sub.add_parser("report", help="the daily observation report")
    _add_window(p)
    p.add_argument("--write", action="store_true", help="write it to the archive")
    p.add_argument("--latest", type=int, default=0, help="list the last N reports")

    p = sub.add_parser("cleanup", help="retention, and what it would remove")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--vacuum", action="store_true")

    sub.add_parser("capacity", help="how full the archive is")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    store.init()
    command = args.command

    if command == "status":
        return query.status()
    if command == "health":
        return query.health()
    if command == "deployments":
        return {"deployments": query.deployments()}
    if command in ("recent", "turns"):
        return query.turns(
            seconds=args.window,
            limit=args.limit,
            chat_id=args.chat_id,
            kind=args.kind,
            outcome=args.outcome,
            deployment_id=args.deployment,
        )
    if command == "failures":
        return query.failures(
            seconds=args.window, limit=args.limit, stage=args.stage, chat_id=args.chat_id
        )
    if command == "find":
        table = {
            "empty": query.empty_responses,
            "reply-target": query.reply_target_failures,
            "named-target": query.named_target_failures,
            "voice": query.voice_failures,
            "transcription": query.transcription_failures,
            "tts": query.tts_failures,
            "delivery": query.delivery_failures,
            "awareness": query.awareness_anomalies,
            "timeouts": query.timeouts,
            "retries": query.retries,
            "provider": query.provider_failures,
        }
        return table[args.what](args.window, args.limit)
    if command == "trace":
        return query.trace(args.turn_id)
    if command == "trace-trace":
        return query.trace_by_trace_id(args.trace_id)
    if command == "conversation":
        return query.conversation(
            args.conversation_id, seconds=args.window, limit=args.limit
        )
    if command == "message":
        return query.by_message(args.message_id, seconds=args.window, limit=args.limit)
    if command == "search":
        return query.search_events(args.text, seconds=args.window, limit=args.limit)
    if command == "search-conversations":
        return query.search_conversations(
            args.text, seconds=args.window, limit=args.limit
        )
    if command == "incidents":
        return query.incidents(args.description, seconds=args.window, limit=args.limit)
    if command == "compare":
        return query.compare(args.before, args.after, seconds=args.window)
    if command == "summarize":
        return query.summarize(args.window)
    if command == "report":
        if args.latest:
            return {"reports": report.latest(args.latest)}
        if args.write:
            return report.write(args.window)
        return report.build(args.window)
    if command == "cleanup":
        if args.dry_run:
            cutoff = schema.now() - max(0, int(retention.config.OBSERVE_RETENTION_SECONDS))
            would = {
                "events": store.one(
                    "SELECT COUNT(*) FROM events WHERE at < ?", (cutoff,)
                )[0],
                "turns": store.one(
                    "SELECT COUNT(*) FROM turns WHERE started_at < ?", (cutoff,)
                )[0],
            }
            return {
                "dry_run": True,
                "retention_seconds": int(retention.config.OBSERVE_RETENTION_SECONDS),
                "would_remove": would,
                "audio": {"bytes": audio.size_bytes()},
            }
        result = retention.sweep()
        if args.vacuum:
            store.vacuum()
            result["vacuumed"] = True
        return result
    if command == "capacity":
        return retention.capacity()
    raise SystemExit(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:  # noqa: BLE001 — the CLI reports, it does not crash
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    if isinstance(result, dict) and result.get("ok") is False:
        return 1
    return 0
