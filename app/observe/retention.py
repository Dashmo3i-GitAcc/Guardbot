"""Retention and capacity, run on the observation worker's own clock.

Retention is a sweep, not a trim-on-write, so it never runs inside a turn. It is
configurable with no short maximum — the point of the archive is to be able to
look further back when something is wrong — and every sweep records what it
removed, so a shrinking archive is always explained by an event rather than
inferred from an absence.

Capacity is made **explicit rather than silent**: when the archive outgrows
`OBSERVE_MAX_BYTES` this reports it and logs it once, instead of deleting
evidence to stay under a number nobody chose.
"""

from __future__ import annotations

import logging
import time

from .. import config
from . import audio, schema, store

log = logging.getLogger("guardbot.observe")


def sweep(*, now: float | None = None) -> dict:
    """Delete evidence past its window. Never raises; always reports."""
    moment = now or time.time()
    result: dict = {
        "at": moment,
        "retention_seconds": int(config.OBSERVE_RETENTION_SECONDS),
        "audio_retention_seconds": int(config.OBSERVE_AUDIO_RETENTION_SECONDS),
    }
    try:
        cutoff = moment - max(0, int(config.OBSERVE_RETENTION_SECONDS))
        result["removed"] = store.prune(cutoff)
    except Exception as exc:  # noqa: BLE001
        result["removed"] = {}
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("[observe] retention sweep failed: %s", result["error"])
    try:
        result["audio"] = audio.prune()
    except Exception as exc:  # noqa: BLE001
        result["audio"] = {}
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        result["size_bytes"] = store.size_bytes()
        result["counts"] = store.counts()
    except Exception:  # noqa: BLE001
        pass

    over = int(config.OBSERVE_MAX_BYTES) > 0 and result.get("size_bytes", 0) > int(
        config.OBSERVE_MAX_BYTES
    )
    result["over_max_bytes"] = over
    if over:
        # Named, once per sweep, and recorded — never a silent deletion.
        log.warning(
            "[observe] archive is %s bytes, over OBSERVE_MAX_BYTES=%s; "
            "shorten OBSERVE_RETENTION_SECONDS or raise the limit deliberately",
            result.get("size_bytes"),
            int(config.OBSERVE_MAX_BYTES),
        )

    try:
        from . import api

        api.emit(
            schema.KIND_CLEANUP,
            event="sweep",
            data=result,
            ok=not result.get("error"),
            error=result.get("error", ""),
        )
    except Exception:  # noqa: BLE001
        pass
    return result


def capacity() -> dict:
    """A read-only view of how full the archive is, for `observe status`."""
    size = store.size_bytes()
    ceiling = int(config.OBSERVE_MAX_BYTES)
    audio_size = audio.size_bytes()
    return {
        "size_bytes": size,
        "max_bytes": ceiling,
        "fraction": (size / ceiling) if ceiling > 0 else 0.0,
        "over": ceiling > 0 and size > ceiling,
        "audio_bytes": audio_size,
        "audio_max_bytes": int(config.OBSERVE_AUDIO_MAX_BYTES),
        "audio_over": int(config.OBSERVE_AUDIO_MAX_BYTES) > 0
        and audio_size > int(config.OBSERVE_AUDIO_MAX_BYTES),
        "counts": store.counts(),
        "oldest_at": store.oldest(),
        "newest_at": store.newest(),
    }
