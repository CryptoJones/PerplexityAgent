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
