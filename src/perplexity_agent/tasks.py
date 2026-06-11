"""Background assistant: recurring monitor tasks (Comet's tasks / price-watch).

A small :mod:`asyncio` task runner that periodically re-runs a search or re-fetches
a URL and notifies when the result *changes*. This is the terminal-feasible slice of
Comet's "background assistant / scheduled tasks": it can watch and alert, but it
takes no real web actions (no clicking Buy). Change detection uses the project's
stable :func:`content_hash` so a notification only fires on a genuine diff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from .assistant import Assistant
from .fetch import PageFetcher
from .security import content_hash

Notify = Callable[[str], None]
TaskKind = Literal["search", "fetch"]

# A monitor survives transient failures (network blips, rate limits) but gives up
# — loudly — after this many consecutive ones, rather than dying silently.
_MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class MonitorTask:
    """One recurring watch over a query or URL."""

    id: int
    kind: TaskKind
    target: str
    interval_s: float
    last_signature: str | None = None
    last_summary: str = ""
    runs: int = 0
    failures: int = 0
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
        for task in list(self._tasks.values()):
            if task._handle is not None:
                task._handle.cancel()
        self._tasks.clear()

    async def _loop(self, task: MonitorTask) -> None:
        try:
            while await self.step(task):
                await asyncio.sleep(task.interval_s)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise

    async def step(self, task: MonitorTask) -> bool:
        """One guarded check (the loop body). Returns False when the monitor should stop.

        A failed probe (network error, rate limit, fetch refusal) notifies the user
        and keeps the monitor alive; after ``_MAX_CONSECUTIVE_FAILURES`` in a row the
        task announces it is giving up and removes itself.
        """
        try:
            await self.run_once(task)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise
        except Exception as exc:  # noqa: BLE001 - any probe failure must not kill the loop
            task.failures += 1
            if task.failures >= _MAX_CONSECUTIVE_FAILURES:
                self._tasks.pop(task.id, None)
                self._notify(
                    f"[task {task.id}] giving up on '{task.target}' after "
                    f"{task.failures} consecutive failures: {exc}"
                )
                return False
            self._notify(
                f"[task {task.id}] check of '{task.target}' failed "
                f"({task.failures}/{_MAX_CONSECUTIVE_FAILURES}): {exc}"
            )
            return True
        task.failures = 0
        return True

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
        task.last_summary = summary
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


# Allow an awaitable notifier too, without forcing callers to provide one.
AsyncNotify = Callable[[str], Awaitable[None]]
