"""Optional raw-audio evidence, off unless an operator asks for it.

This is the one part of the archive that departs from AgentMD §53.11 ("no raw
audio is persisted"). It exists because a transcription bug can only be settled
by hearing the clip that produced it, and it is fenced on every side:

* `OBSERVE_AUDIO_ENABLED` defaults **false**, so the invariant holds unless an
  operator deliberately turns it on;
* when on, it ages out on **its own** window (`OBSERVE_AUDIO_RETENTION_SECONDS`,
  default six hours — shorter than the metadata) and its own byte ceiling, so
  turning it on cannot quietly become the archive's largest cost;
* the bytes never enter a log line, never reach Telegram, and are stored as
  files under the operator-only directory, not in the database.
"""

from __future__ import annotations

import os
import time

from .. import config
from . import store

_SUBDIR = "audio"


def enabled() -> bool:
    return bool(config.OBSERVE_AUDIO_ENABLED)


def directory() -> str:
    return store.subdir(_SUBDIR)


def save(name: str, payload: bytes, *, extension: str = "bin") -> str:
    """Write one clip. Returns its path, or ``""`` when it was not kept.

    Never raises: audio is the least important evidence here, and failing to
    keep it must never disturb the turn that produced it.
    """
    if not enabled() or not payload:
        return ""
    try:
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_")[:80]
        target = os.path.join(directory(), f"{safe}.{extension}")
        with open(target, "wb") as handle:
            handle.write(payload)
        return target
    except Exception:  # noqa: BLE001
        return ""


def size_bytes() -> int:
    total = 0
    try:
        for entry in os.scandir(directory()):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return 0
    return total


def prune() -> dict[str, int]:
    """Age and size both, oldest first. Reports files removed and bytes freed."""
    if not os.path.isdir(directory()):
        return {"files": 0, "bytes": 0}
    cutoff = time.time() - max(0, int(config.OBSERVE_AUDIO_RETENTION_SECONDS))
    removed = freed = 0
    keep: list[tuple[float, str, int]] = []
    try:
        for entry in os.scandir(directory()):
            if not entry.is_file():
                continue
            stat = entry.stat()
            if stat.st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
                freed += stat.st_size
            else:
                keep.append((stat.st_mtime, entry.path, stat.st_size))
    except OSError:
        return {"files": removed, "bytes": freed}

    ceiling = int(config.OBSERVE_AUDIO_MAX_BYTES)
    if ceiling > 0:
        total = sum(size for _, _, size in keep)
        for _, path, size in sorted(keep):
            if total <= ceiling:
                break
            try:
                os.remove(path)
                removed += 1
                freed += size
                total -= size
            except OSError:
                pass
    return {"files": removed, "bytes": freed}
