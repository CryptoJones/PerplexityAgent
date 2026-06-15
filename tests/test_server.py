"""In-memory integration tests for the FastMCP server.

These drive the real server through its lifespan (which builds a PerplexityClient)
using an in-memory client session, with httpx mocked by respx. They exercise the
tool wrappers, the rate-limit/audit guard, and the deep-research pipeline end to end
without a live API or a subprocess.
"""

import json

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session as connect
from pydantic import SecretStr

from perplexity_agent.config import Settings
from perplexity_agent.server import build_server


def _settings():
    return Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=6000,
        rate_burst=1000,
    )


def _text(result):
    # CallToolResult.content is a list of content blocks; the first is text JSON.
    return result.content[0].text


async def test_list_tools_exposes_all():
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        tools = sorted(t.name for t in (await client.list_tools()).tools)
    assert tools == [
        "deep_research", "perplexity_search", "retrieve", "server_metrics", "sonar_ask"
    ]


async def test_server_metrics_tool_reports_counters():
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        out = json.loads(_text(await client.call_tool("server_metrics", {})))
    assert "total_calls" in out
    assert "latency_ms" in out


@respx.mock
async def test_large_result_offloaded_and_retrievable():
    # A result that overflows the (tiny, for the test) budget is bounded into a
    # {truncated, retrieve_key} envelope; the retrieve tool fetches the original.
    big = {"results": [{"url": f"https://a.com/{i}", "blob": "x" * 100} for i in range(50)]}
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json=big)
    )
    settings = Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=6000,
        rate_burst=1000,
        max_tool_output_chars=300,
    )
    mcp, _ = build_server(settings)
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("perplexity_search", {"query": "q"})
        payload = json.loads(_text(result))
        assert payload["truncated"] is True
        key = payload["retrieve_key"]
        fetched = json.loads(_text(await client.call_tool("retrieve", {"key": key})))
    assert "https://a.com/0" in fetched["content"]


async def test_retrieve_unknown_key_errors_cleanly():
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("retrieve", {"key": "deadbeefdeadbeef"})
    assert "error" in json.loads(_text(result))


@respx.mock
async def test_search_tool_roundtrip():
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json={"results": [{"url": "https://a.com"}]})
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("perplexity_search", {"query": "q", "max_results": 3})
    assert "https://a.com" in _text(result)


@respx.mock
async def test_sonar_ask_tool_roundtrip():
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "grounded"}}]})
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "sonar_ask", {"question": "why?", "model": "sonar-pro", "system_prompt": "cite"}
        )
    assert "grounded" in _text(result)


@respx.mock
async def test_deep_research_tool_roundtrip():
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json={"results": [{"url": "https://a.com", "title": "A"}]})
    )
    report = {
        "answer": "answer",
        "key_findings": ["k"],
        "open_questions": [],
        "claims": [{"claim": "c", "supporting_urls": ["https://a.com"], "confidence": "high"}],
    }
    chat_json = {"choices": [{"message": {"content": json.dumps(report)}}]}
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json=chat_json)
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "deep_research", {"question": "is x true?", "num_subquestions": 2}
        )
    payload = json.loads(_text(result))
    assert payload["validation_report"]["passed"] is True
    assert payload["report"]["answer"] == "answer"


@respx.mock
async def test_audit_correlates_call_and_result_with_latency():
    import logging

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.events = []

        def emit(self, record):
            self.events.append(json.loads(record.getMessage()))

    handler = _Capture()
    audit_logger = logging.getLogger("perplexity_agent.audit")
    audit_logger.addHandler(handler)
    try:
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                },
            )
        )
        mcp, _ = build_server(_settings())
        async with connect(mcp._mcp_server) as client:
            await client.call_tool("sonar_ask", {"question": "why?"})
    finally:
        audit_logger.removeHandler(handler)

    calls = [e for e in handler.events if e["event"] == "tool_call"]
    results = [e for e in handler.events if e["event"] == "tool_result"]
    assert calls and results
    # The pair shares a correlation id and the result carries latency + usage.
    assert results[-1]["call_id"] == calls[-1]["call_id"]
    assert results[-1]["duration_ms"] >= 0
    assert results[-1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


async def test_invalid_params_rejected():
    # Empty query violates the SearchInput min_length bound.
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("perplexity_search", {"query": ""})
    assert result.isError


async def test_rate_limit_enforced():
    settings = Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        rate_per_minute=1,
        rate_burst=1,
        max_retries=0,
    )
    mcp, _ = build_server(settings)
    with respx.mock:
        respx.post("https://api.perplexity.ai/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        async with connect(mcp._mcp_server) as client:
            first = await client.call_tool("perplexity_search", {"query": "q"})
            second = await client.call_tool("perplexity_search", {"query": "q"})
    assert not first.isError
    assert second.isError  # burst of 1 exhausted -> rate limited


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
