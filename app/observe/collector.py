"""The asynchronous collector: the only thing between Nexus and the archive.

The response path never touches the disk. It appends a dict to a bounded
`asyncio.Queue` and returns; a background worker drains the queue in batches and
writes them from a thread, so neither a slow disk nor a locked database can stall
the event loop or a person's reply.

Everything here is written so that failure is *contained*:

* a full queue drops the event and counts it — it never waits;
* a failed batch is counted and discarded — it is never retried into a loop that
  could grow without bound, and it never raises into the worker;
* the worker never stops on an error, so one bad batch does not end observation;
* `submit()` returns a bool and cannot raise, because its callers are inside
  Nexus's own code paths.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .. import config
from . import store

log = logging.getLogger("guardbot.observe")


class Collector:
    """A bounded queue and a batching writer."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._batch = max(1, int(config.OBSERVE_BATCH))
        self._interval = max(0.05, float(config.OBSERVE_FLUSH_SECONDS))
        self.dropped = 0
        self.written = 0
        self.failed = 0
        self.batches = 0
        self.started_at = 0.0
        self.last_error = ""
        self.last_write_at = 0.0
        self._draining = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self, *, worker: bool = True) -> bool:
        """Open the store and (optionally) start the worker. Never raises."""
        try:
            store.init()
        except Exception as exc:  # noqa: BLE001 — observation must fail alone
            self.last_error = f"store: {type(exc).__name__}: {exc}"
            log.warning("[observe] archive unavailable: %s", self.last_error)
            return False
        self._queue = asyncio.Queue(maxsize=max(64, int(config.OBSERVE_QUEUE_MAX)))
        self.started_at = time.time()
        if worker:
            self._task = asyncio.create_task(self._run(), name="observe-writer")
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await self.flush()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"flush: {type(exc).__name__}: {exc}"

    def running(self) -> bool:
        return self._queue is not None

    # ── submission ────────────────────────────────────────────────────────
    def submit(self, op: str, payload: Any) -> bool:
        """Enqueue one item. Returns False if it was dropped. Never raises."""
        queue = self._queue
        if queue is None:
            return False
        try:
            queue.put_nowait((op, payload))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        except Exception:  # noqa: BLE001
            self.dropped += 1
            return False

    # ── the worker ────────────────────────────────────────────────────────
    async def _run(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            try:
                first = await queue.get()
                batch = [first]
                # Take whatever else is already waiting, up to the batch size,
                # so a burst costs one transaction rather than one per event.
                while len(batch) < self._batch:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await asyncio.to_thread(self._apply, batch)
                for _ in batch:
                    queue.task_done()
                if len(batch) < self._batch and self._interval:
                    # Nothing more was waiting: yield for a moment so an idle
                    # process is not spinning on an empty queue.
                    await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the worker outlives errors
                self.last_error = f"worker: {type(exc).__name__}: {exc}"
                log.warning("[observe] writer error: %s", self.last_error)

    def _apply(self, items: list[tuple[str, Any]]) -> None:
        """Apply a drained batch in order. Runs in a worker thread."""
        events: list[dict] = []
        try:
            for op, payload in items:
                if op == "event":
                    events.append(payload)
                    continue
                if events:
                    self._write(events)
                    events = []
                try:
                    if op == "turn.open":
                        store.open_turn(payload)
                    elif op == "turn.close":
                        store.close_turn(**payload)
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    self.last_error = f"{op}: {type(exc).__name__}: {exc}"
            if events:
                self._write(events)
        except Exception as exc:  # noqa: BLE001
            self.failed += len(items)
            self.last_error = f"apply: {type(exc).__name__}: {exc}"

    def _write(self, events: list[dict]) -> None:
        try:
            store.insert_events(events)
            self.written += len(events)
            self.batches += 1
            self.last_write_at = time.time()
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001
            self.failed += len(events)
            self.last_error = f"write: {type(exc).__name__}: {exc}"

    # ── flushing ──────────────────────────────────────────────────────────
    async def flush(self) -> int:
        """Write everything queued right now. Used by tests, probes and stop()."""
        queue = self._queue
        if queue is None:
            return 0
        if self._task is not None:
            try:
                await asyncio.wait_for(queue.join(), timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"join: {type(exc).__name__}: {exc}"
            return 0
        drained: list[tuple[str, Any]] = []
        while True:
            try:
                drained.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if drained:
            await asyncio.to_thread(self._apply, drained)
        return len(drained)

    def counters(self) -> dict[str, Any]:
        return {
            "running": self.running(),
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "written": self.written,
            "failed": self.failed,
            "dropped": self.dropped,
            "batches": self.batches,
            "started_at": self.started_at,
            "last_write_at": self.last_write_at,
            "last_error": self.last_error,
        }
