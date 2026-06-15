from perplexity_agent.metrics import MetricsCollector


def test_snapshot_counts_and_latency():
    m = MetricsCollector()
    m.record_call("search", 10.0)
    m.record_call("search", 30.0)
    m.record_rate_limited("search")
    snap = m.snapshot()
    assert snap["total_calls"] == 2
    assert snap["rate_limited"] == 1
    assert snap["by_tool"]["search"] == 2
    assert snap["latency_ms"]["max"] == 30.0


def test_empty_snapshot_is_safe():
    snap = MetricsCollector().snapshot()
    assert snap["total_calls"] == 0
    assert snap["latency_ms"]["p50"] == 0.0
    assert snap["by_tool"] == {}


def test_latency_reservoir_is_bounded():
    m = MetricsCollector(max_samples=3)
    for i in range(10):
        m.record_call("t", float(i))
    assert len(m._latencies_ms) == 3
    assert list(m._latencies_ms) == [7.0, 8.0, 9.0]  # most-recent kept
