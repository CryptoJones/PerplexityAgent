"""Background assistant: recurring monitor tasks (Comet's tasks / price-watch).

A small :mod:`asyncio` task runner that periodically re-runs a search or re-fetches
a URL and notifies when the result *changes*. This is the terminal-feasible slice of
Comet's "background assistant / scheduled tasks": it can watch and alert, but it
takes no real web actions (no clicking Buy). Change detection uses the project's
stable :func:`content_hash` so a notification only fires on a genuine diff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .assistant import Assistant
from .fetch import PageFetcher
from .security import content_hash

Notify = Callable[[str], None]
TaskKind = Literal["search", "fetch"]


@dataclass
class MonitorTask:
    """One recurring watch over a query or URL."""

    id: int
    kind: TaskKind
    target: str
    interval_s: float
    last_signature: str | None = None
    runs: int = 0
    _handle: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)


class TaskManager:
    """Owns the set of background monitor tasks and their asyncio loops."""

    def __init__(
        self,
        assistant: Assistant,
        fetcher: PageFetcher,
        notify: Notify,
    ) -> None:
        self._assistant = assistant
        self._fetcher = fetcher
        self._notify = notify
        self._tasks: dict[int, MonitorTask] = {}
        self._next_id = 1

    def list(self) -> list[MonitorTask]:
        return list(self._tasks.values())

    def add(self, kind: TaskKind, target: str, interval_s: float) -> MonitorTask:
        """Register and start a new monitor; returns the created task."""
        task = MonitorTask(id=self._next_id, kind=kind, target=target, interval_s=interval_s)
        self._tasks[task.id] = task
        self._next_id += 1
        task._handle = asyncio.ensure_future(self._loop(task))
        return task

    def remove(self, task_id: int) -> bool:
        """Stop and forget a monitor. Returns True if it existed."""
        task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        if task._handle is not None:
            task._handle.cancel()
        return True

    async def aclose(self) -> None:
        handles = [t._handle for t in self._tasks.values() if t._handle is not None]
        for handle in handles:
            handle.cancel()
        self._tasks.clear()
        # Await the cancelled handles so any in-flight probe finishes unwinding
        # before the caller closes the shared fetcher/client they run against.
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)

    async def _loop(self, task: MonitorTask) -> None:
        while True:
            try:
                await self.run_once(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a monitor must survive transient errors
                # A transient fetch/search failure must not kill the watch: report
                # it and keep looping so the next interval retries.
                self._notify(f"[task {task.id}] '{task.target}' error: {exc}")
            await asyncio.sleep(task.interval_s)

    async def run_once(self, task: MonitorTask) -> bool:
        """Run one check. Returns True if the result changed since last time.

        Separated from the loop so it can be driven directly in tests without
        waiting on real time.
        """
        summary, signature = await self._probe(task)
        task.runs += 1
        changed = task.last_signature is not None and signature != task.last_signature
        first = task.last_signature is None
        task.last_signature = signature
        if changed:
            self._notify(f"[task {task.id}] '{task.target}' changed: {summary}")
        elif first:
            self._notify(f"[task {task.id}] watching '{task.target}': {summary}")
        return changed

    async def _probe(self, task: MonitorTask) -> tuple[str, str]:
        if task.kind == "search":
            results = await self._assistant.search(task.target, max_results=5)
            top = [r.get("url", "") for r in results]
            summary = f"{len(results)} results; top: {top[0] if top else '—'}"
            return summary, content_hash(top)
        page = await self._fetcher.fetch(task.target)
        summary = f"{page.title or page.final_url} ({page.fetched_bytes} bytes)"
        return summary, content_hash(page.text)
