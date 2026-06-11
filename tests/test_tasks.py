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


class _FlakyAssistant:
    """search() raises ``failures`` times, then succeeds forever."""

    def __init__(self, failures):
        self._failures = failures

    async def search(self, _query, max_results=5):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("transient blip")
        return [{"url": "https://a.com"}]


async def test_step_survives_transient_failure():
    msgs = []
    mgr = TaskManager(_FlakyAssistant(failures=1), _FakeFetcher(), msgs.append)
    task = mgr.add("search", "widgets", 999)
    task._handle.cancel()  # drive step() manually instead of the background loop

    keep_going = await mgr.step(task)
    assert keep_going is True
    assert task.failures == 1
    assert any("failed" in m for m in msgs)

    # Next probe succeeds: the failure counter resets and the watch stays alive.
    keep_going = await mgr.step(task)
    assert keep_going is True
    assert task.failures == 0
    mgr.remove(task.id)


async def test_step_gives_up_after_consecutive_failures():
    from perplexity_agent.tasks import _MAX_CONSECUTIVE_FAILURES

    msgs = []
    mgr = TaskManager(_FlakyAssistant(failures=99), _FakeFetcher(), msgs.append)
    task = mgr.add("search", "widgets", 999)
    task._handle.cancel()  # drive step() manually instead of the background loop

    for _ in range(_MAX_CONSECUTIVE_FAILURES - 1):
        assert await mgr.step(task) is True
    assert await mgr.step(task) is False
    # The dead watch announced itself and was removed from the listing.
    assert any("giving up" in m for m in msgs)
    assert mgr.list() == []


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
