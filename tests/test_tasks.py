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

    assert await mgr.run_once(task) is False  # first run: baseline
    assert await mgr.run_once(task) is False  # unchanged
    assert await mgr.run_once(task) is True  # changed
    assert task.runs == 3
    assert any("watching" in m for m in msgs)
    assert any("changed" in m for m in msgs)


async def test_add_and_remove_tracks_tasks():
    assistant = _FakeAssistant([[{"url": "https://a.com"}]])
    mgr = TaskManager(assistant, _FakeFetcher(), lambda _m: None)
    task = mgr.add("search", "widgets", 999)
    assert task.id in {t.id for t in mgr.list()}
    assert mgr.remove(task.id) is True
    assert mgr.remove(task.id) is False
    await mgr.aclose()
