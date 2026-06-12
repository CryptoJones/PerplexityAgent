import asyncio

import pytest

from perplexity_agent.security import AuditLogger, RateLimitError, RequestGuard, TokenBucket
from perplexity_agent.tasks import MonitorTask, TaskManager


class _FakeAssistant:
    def __init__(self, results):
        self._results = list(results)

    async def search(self, _query, max_results=5):
        return self._results.pop(0)


class _FakeFetcher:
    pass


async def test_run_once_notifies_on_first_then_change():
    # First probe establishes baseline (first-watch notice), second identical = no
    # change, third differs = change notice.
    assistant = _FakeAssistant(
        [
            [{"url": "https://a.com"}],
            [{"url": "https://a.com"}],
            [{"url": "https://b.com"}],
        ]
    )
    msgs = []
    mgr = TaskManager(assistant, _FakeFetcher(), msgs.append)
    task = MonitorTask(id=1, kind="search", target="widgets", interval_s=999)

    # Assign before asserting: an `assert <call>` is dropped under `python -O`,
    # which would silently skip the probe. Keep the side effect out of assert.
    first = await mgr.run_once(task)
    second = await mgr.run_once(task)
    third = await mgr.run_once(task)
    assert first is False  # first run: baseline
    assert second is False  # unchanged
    assert third is True  # changed
    assert task.runs == 3
    assert any("watching" in m for m in msgs)
    assert any("changed" in m for m in msgs)


async def test_add_and_remove_tracks_tasks():
    assistant = _FakeAssistant([[{"url": "https://a.com"}]])
    mgr = TaskManager(assistant, _FakeFetcher(), lambda _m: None)
    task = mgr.add("search", "widgets", 999)
    assert task.id in {t.id for t in mgr.list()}
    removed = mgr.remove(task.id)
    removed_again = mgr.remove(task.id)
    assert removed is True
    assert removed_again is False
    await mgr.aclose()


async def test_probe_goes_through_the_guard():
    # With a guard whose bucket is exhausted, a probe is rate-limited instead of
    # making an unmetered, unaudited API/web call.
    guard = RequestGuard(TokenBucket(rate_per_minute=1, burst=1), AuditLogger())
    guard.acquire("warmup")  # drain the only token
    assistant = _FakeAssistant([[{"url": "https://a.com"}]])
    mgr = TaskManager(assistant, _FakeFetcher(), lambda _m: None, guard=guard)
    task = MonitorTask(id=1, kind="search", target="widgets", interval_s=999)
    with pytest.raises(RateLimitError):
        await mgr.run_once(task)


class _ErroringAssistant:
    """Raises on the first probe, then succeeds — to prove the loop survives."""

    def __init__(self):
        self.calls = 0

    async def search(self, _query, max_results=5):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient 503")
        return [{"url": "https://ok.example"}]


async def test_loop_survives_transient_error_and_notifies():
    # A transient error on one probe must not kill the monitor: the loop reports
    # it and keeps running so the next interval can succeed.
    assistant = _ErroringAssistant()
    msgs = []
    mgr = TaskManager(assistant, _FakeFetcher(), msgs.append)
    mgr.add("search", "widgets", 0.001)
    # Give the loop time to hit the error and then run again.
    for _ in range(100):
        await asyncio.sleep(0.005)
        if assistant.calls >= 2:
            break
    await mgr.aclose()
    assert assistant.calls >= 2  # ran again after the failure
    assert any("error" in m and "transient 503" in m for m in msgs)


class _AlwaysFailingAssistant:
    def __init__(self):
        self.calls = 0

    async def search(self, _query, max_results=5):
        self.calls += 1
        raise RuntimeError("still down")


async def test_loop_does_not_spam_repeated_identical_errors():
    # A persistent failure (same error every interval, e.g. a drained rate-limit
    # bucket) must be reported once, not re-notified every tick.
    assistant = _AlwaysFailingAssistant()
    msgs = []
    mgr = TaskManager(assistant, _FakeFetcher(), msgs.append)
    mgr.add("search", "widgets", 0.001)
    for _ in range(100):
        await asyncio.sleep(0.005)
        if assistant.calls >= 3:
            break
    await mgr.aclose()
    assert assistant.calls >= 3  # it kept retrying
    error_msgs = [m for m in msgs if "still down" in m]
    assert len(error_msgs) == 1  # but only notified once


async def test_aclose_awaits_inflight_probe_before_returning():
    # aclose() must await cancelled handles so an in-flight probe finishes
    # unwinding before the caller closes the shared clients it uses.
    started = asyncio.Event()
    released = asyncio.Event()

    class _SlowAssistant:
        async def search(self, _query, max_results=5):
            started.set()
            await asyncio.sleep(10)  # parked until cancelled
            released.set()  # only reached if cancellation is not awaited away
            return []

    mgr = TaskManager(_SlowAssistant(), _FakeFetcher(), lambda _m: None)
    handle = mgr.add("search", "widgets", 999)._handle
    await asyncio.wait_for(started.wait(), timeout=1)
    await mgr.aclose()
    assert handle is not None and handle.done()
    assert not released.is_set()
