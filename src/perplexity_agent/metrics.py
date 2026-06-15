"""Lightweight, dependency-free runtime metrics for the MCP server.

An optional in-process collector wired into the tool guard, so an operator can
ask "how many calls, how slow, how often rate-limited" via the ``server_metrics``
tool without standing up Prometheus/OTel. Memory is bounded: only summary
counters and a capped latency reservoir are kept.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from typing import Any


class MetricsCollector:
    """Aggregate per-tool call counts, rate-limit rejections, and latencies."""

    def __init__(self, max_samples: int = 1024) -> None:
        self._max_samples = max(1, max_samples)
        self.calls: Counter[str] = Counter()
        self.rate_limited: Counter[str] = Counter()
        # Bounded reservoir: deque(maxlen) drops the oldest in O(1).
        self._latencies_ms: deque[float] = deque(maxlen=self._max_samples)

    def record_call(self, tool: str, latency_ms: float) -> None:
        """Record one completed tool call and its latency."""
        self.calls[tool] += 1
        self._latencies_ms.append(latency_ms)

    def record_rate_limited(self, tool: str) -> None:
        """Record a call the rate limiter rejected (it never ran)."""
        self.rate_limited[tool] += 1

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable summary of the counters collected so far."""
        lat = sorted(self._latencies_ms)
        return {
            "total_calls": sum(self.calls.values()),
            "rate_limited": sum(self.rate_limited.values()),
            "latency_ms": {
                "p50": _percentile(lat, 0.50),
                "p95": _percentile(lat, 0.95),
                "max": lat[-1] if lat else 0.0,
            },
            "by_tool": {tool: self.calls[tool] for tool in sorted(self.calls)},
        }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list (0.0 when empty)."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_values)))
    return round(sorted_values[min(rank, len(sorted_values)) - 1], 3)
