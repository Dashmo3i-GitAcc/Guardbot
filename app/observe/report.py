"""The periodic observation report.

Written by the observation infrastructure on its own clock — **never** by Nexus,
and never by a model. It is counts and patterns over the archive, so it can be
produced from evidence alone and cannot itself become a claim.

Two forms are written together: a machine-readable JSON file an agent consumes,
and a human-readable Markdown file an operator reads. Both land in the archive's
own `reports/` directory, and the run is recorded as an event so the report's own
existence is part of the evidence.
"""

from __future__ import annotations

import json
import os
import time

from . import query, retention, schema, store


def build(seconds: int = 86400) -> dict:
    """The report's data: the summary, the capacity, and the notable findings."""
    summary = query.summarize(seconds)
    notable: list[str] = []
    if summary["failed_turns"]:
        notable.append(f"{summary['failed_turns']} failed turn(s)")
    for name in (
        "transcription_failures",
        "tts_failures",
        "delivery_failures",
        "empty_responses",
        "timeouts",
        "provider_failures",
        "reply_target_failures",
        "named_target_failures",
        "awareness_anomalies",
    ):
        if summary.get(name):
            notable.append(f"{summary[name]} {name.replace('_', ' ')}")
    if summary.get("repeated_incidents"):
        notable.append(
            f"{len(summary['repeated_incidents'])} repeated error signature(s)"
        )
    if summary.get("new_error_patterns"):
        notable.append(
            f"{len(summary['new_error_patterns'])} new error pattern(s) on the "
            f"newest deployment"
        )
    return {
        "summary": summary,
        "capacity": retention.capacity(),
        "notable": notable,
        "deployments": query.deployments(),
    }


def _markdown(data: dict, seconds: int) -> str:
    summary = data["summary"]
    capacity = data["capacity"]
    lines = [
        "# Nexus observation report",
        "",
        f"- window: last {query._human(seconds)}",
        f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(summary['generated_at']))}",
        f"- newest deployment: `{summary.get('newest_deployment') or 'unknown'}`",
        "",
        "## Turns",
        "",
        f"- total: **{summary['total_turns']}**",
        f"- sent: **{summary['successful_turns']}**",
        f"- failed: **{summary['failed_turns']}**",
        f"- voice turns: **{summary['voice_turns']}**",
        f"- awareness passes: **{summary['awareness_passes']}**",
        "",
        "## Failures and anomalies",
        "",
    ]
    for name in (
        "empty_responses",
        "retries",
        "timeouts",
        "provider_failures",
        "delivery_failures",
        "transcription_failures",
        "tts_failures",
        "reply_target_failures",
        "named_target_failures",
        "awareness_anomalies",
    ):
        lines.append(f"- {name.replace('_', ' ')}: {summary.get(name, 0)}")
    lines += [
        "",
        "## Latency (turn duration)",
        "",
        f"- p50: {summary['latency_ms']['p50']:.0f} ms",
        f"- p95: {summary['latency_ms']['p95']:.0f} ms",
        f"- max: {summary['latency_ms']['max']:.0f} ms",
        "",
        "## Outcomes",
        "",
    ]
    for outcome, count in sorted(summary["outcomes"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {outcome or 'none'}: {count}")
    lines += ["", "## Error signatures", ""]
    if summary["error_signatures"]:
        for signature, count in summary["error_signatures"].items():
            lines.append(f"- `{signature}` × {count}")
    else:
        lines.append("- none")
    lines += ["", "## Deployments seen in this window", ""]
    for deployment, count in sorted(
        summary["deployments_seen"].items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"- `{deployment}`: {count} turn(s)")
    lines += [
        "",
        "## Archive",
        "",
        f"- path: `{store.path()}`",
        f"- size: {capacity['size_bytes']} bytes "
        f"({capacity['fraction'] * 100:.1f}% of the configured maximum)",
        f"- events: {capacity['counts'].get('events', 0)}",
        f"- turns: {capacity['counts'].get('turns', 0)}",
        f"- audio: {capacity['audio_bytes']} bytes "
        f"(capture {'on' if capacity['audio_max_bytes'] and _audio_on() else 'off'})",
        "",
        "## Notable",
        "",
    ]
    lines += [f"- {item}" for item in data["notable"]] or ["- nothing notable"]
    lines.append("")
    return "\n".join(lines)


def _audio_on() -> bool:
    from .. import config

    return bool(config.OBSERVE_AUDIO_ENABLED)


def write(seconds: int = 86400) -> dict:
    """Produce and store one report. Returns where it landed. Never raises."""
    try:
        data = build(seconds)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    directory = store.subdir("reports")
    json_path = os.path.join(directory, f"report-{stamp}.json")
    md_path = os.path.join(directory, f"report-{stamp}.md")
    try:
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        with open(md_path, "w", encoding="utf-8") as handle:
            handle.write(_markdown(data, seconds))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        store.record_report(
            window_seconds=seconds, path=json_path, summary=data["summary"]
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import api

        api.emit(
            schema.KIND_REPORT,
            event="written",
            data={
                "path": json_path,
                "markdown": md_path,
                "window_seconds": seconds,
                "notable": data["notable"],
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "json": json_path,
        "markdown": md_path,
        "window_seconds": seconds,
        "notable": data["notable"],
        "summary": data["summary"],
    }


def latest(limit: int = 10) -> list[dict]:
    rows = store.rows(
        "SELECT at, window_seconds, path FROM reports ORDER BY at DESC LIMIT ?",
        (max(1, int(limit)),),
    )
    return [dict(row) for row in rows]
