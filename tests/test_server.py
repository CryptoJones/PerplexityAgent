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
        store_path=":memory:",
    )


def _text(result):
    # CallToolResult.content is a list of content blocks; the first is text JSON.
    return result.content[0].text


def _agent_response(response_id="resp_1", output=None):
    return {
        "created_at": 1,
        "id": response_id,
        "model": "openai/gpt-5.5",
        "object": "response",
        "output": output or [],
        "status": "completed",
    }


async def test_list_tools_exposes_all():
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        tools = sorted(t.name for t in (await client.list_tools()).tools)
    assert tools == [
        "deep_research",
        "fetch_url",
        "finance_search",
        "people_search",
        "perplexity_search",
        "responses_create",
        "responses_retrieve",
        "retrieve",
        "server_metrics",
        "sonar_ask",
    ]


async def test_responses_create_description_documents_server_side_controls():
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    description = tools["responses_create"].description or ""
    assert "auto_execute_functions" in description
    assert "max_function_rounds" in description


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
    respx.post("https://api.perplexity.ai/search").mock(return_value=httpx.Response(200, json=big))
    settings = Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=6000,
        rate_burst=1000,
        max_tool_output_chars=300,
        store_path=":memory:",
    )
    mcp, _ = build_server(settings)
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("perplexity_search", {"query": "q"})
        payload = json.loads(_text(result))
        assert payload["truncated"] is True
        key = payload["retrieve_key"]
        fetched = json.loads(_text(await client.call_tool("retrieve", {"key": key})))
    assert "https://a.com/0" in fetched["content"]


@respx.mock
async def test_offloaded_result_cannot_be_retrieved_from_another_session():
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
        store_path=":memory:",
    )
    mcp, _ = build_server(settings)
    async with connect(mcp._mcp_server) as owner:
        result = await owner.call_tool("perplexity_search", {"query": "q"})
        key = json.loads(_text(result))["retrieve_key"]
        async with connect(mcp._mcp_server) as stranger:
            foreign = json.loads(_text(await stranger.call_tool("retrieve", {"key": key})))
        restored = json.loads(_text(await owner.call_tool("retrieve", {"key": key})))
    assert "error" in foreign
    assert "https://a.com/0" in restored["content"]


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
async def test_search_tool_forwards_advanced_filters():
    route = respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        await client.call_tool(
            "perplexity_search",
            {
                "query": "space",
                "search_domain_filter": ["nasa.gov"],
                "search_recency_filter": "week",
            },
        )
    body = json.loads(route.calls.last.request.content)
    assert body["search_domain_filter"] == ["nasa.gov"]
    assert body["search_recency_filter"] == "week"


@respx.mock
async def test_responses_create_roundtrip_with_state_and_multimodal_input():
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(
            200,
            json=_agent_response("resp_2"),
        )
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "responses_create",
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "describe"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/image.png",
                            },
                        ],
                    }
                ],
                "model": "openai/gpt-5.5",
                "reasoning": {"effort": "high"},
                "tools": [{"type": "web_search"}],
                "store": True,
                "previous_response_id": "resp_1",
            },
        )
        cached = await client.call_tool("responses_retrieve", {"response_id": "resp_2"})
    assert json.loads(_text(result))["id"] == "resp_2"
    assert json.loads(_text(cached))["id"] == "resp_2"
    body = json.loads(route.calls.last.request.content)
    assert body["store"] is True
    assert body["previous_response_id"] == "resp_1"
    assert body["input"][0]["content"][1]["type"] == "input_image"


@respx.mock
async def test_responses_create_streaming_branch_collects_and_persists_completed_response():
    completed = _agent_response("resp_stream")
    body = (
        f"data: {json.dumps({'type': 'response.created', 'response': completed})}\n\n"
        'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        f"data: {json.dumps({'type': 'response.completed', 'response': completed})}\n\n"
        "data: [DONE]\n\n"
    )
    respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        streamed = json.loads(
            _text(
                await client.call_tool(
                    "responses_create",
                    {"input": "hi", "model": "openai/gpt-5.5", "stream": True},
                )
            )
        )
        cached = json.loads(
            _text(await client.call_tool("responses_retrieve", {"response_id": "resp_stream"}))
        )
    assert streamed["object"] == "response.stream"
    assert [event["type"] for event in streamed["events"]][-1] == "response.completed"
    assert streamed["response"]["id"] == "resp_stream"
    assert cached["id"] == "resp_stream"


@respx.mock
async def test_responses_retrieve_roundtrip():
    route = respx.get("https://api.perplexity.ai/v1/agent/resp_2").mock(
        return_value=httpx.Response(200, json=_agent_response("resp_2"))
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("responses_retrieve", {"response_id": "resp_2"})
    assert route.called
    assert json.loads(_text(result))["id"] == "resp_2"


@respx.mock
async def test_finance_search_enables_builtin_tool_and_hints():
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(200, json=_agent_response("resp_fin"))
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        await client.call_tool(
            "finance_search",
            {"query": "current valuation", "categories": ["quote"], "tickers": ["NVDA"]},
        )
    body = json.loads(route.calls.last.request.content)
    assert body["tools"] == [{"type": "finance_search"}]
    assert "NVDA" in body["input"]
    assert "quote" in body["input"]


@respx.mock
async def test_people_search_uses_people_index():
    route = respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json={"results": [{"title": "Ada"}]})
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("people_search", {"query": "Ada Lovelace"})
    assert "Ada" in _text(result)
    assert json.loads(route.calls.last.request.content)["search_type"] == "people"


@respx.mock
async def test_fetch_url_tool_uses_hardened_fetcher(monkeypatch):
    import perplexity_agent.fetch as fetch_mod

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        fetch_mod.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    monkeypatch.setattr(fetch_mod.asyncio, "to_thread", inline_to_thread)
    respx.get("https://93.184.216.34/article").mock(
        return_value=httpx.Response(200, html="<title>Article</title><p>safe text</p>")
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("fetch_url", {"url": "https://example.com/article"})
    payload = json.loads(_text(result))
    assert payload["contents"][0]["title"] == "Article"
    assert "safe text" in payload["contents"][0]["snippet"]


@respx.mock
async def test_fetch_url_tool_accepts_bounded_url_list(monkeypatch):
    import perplexity_agent.fetch as fetch_mod

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        fetch_mod.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    # Keep concurrent fake DNS calls out of the interpreter's default executor;
    # some Python builds wait on that executor during per-test loop teardown.
    monkeypatch.setattr(fetch_mod.asyncio, "to_thread", inline_to_thread)
    respx.get("https://93.184.216.34/a").mock(
        return_value=httpx.Response(200, html="<title>A</title><p>first</p>")
    )
    respx.get("https://93.184.216.34/b").mock(
        return_value=httpx.Response(200, html="<title>B</title><p>second</p>")
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "fetch_url",
            {"urls": ["https://example.com/a", "https://example.com/b"], "max_urls": 2},
        )
    payload = json.loads(_text(result))
    assert [item["title"] for item in payload["contents"]] == ["A", "B"]


@respx.mock
async def test_fetch_url_returns_partial_results_when_one_url_fails(monkeypatch):
    import perplexity_agent.fetch as fetch_mod

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        fetch_mod.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    monkeypatch.setattr(fetch_mod.asyncio, "to_thread", inline_to_thread)
    respx.get("https://93.184.216.34/good").mock(
        return_value=httpx.Response(200, html="<title>Good</title><p>usable</p>")
    )
    respx.get("https://93.184.216.34/bad").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF",
        )
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "fetch_url",
            {
                "urls": ["https://example.com/good", "https://example.com/bad"],
                "max_urls": 2,
            },
        )
    contents = json.loads(_text(result))["contents"]
    assert contents[0]["title"] == "Good"
    assert contents[1]["url"] == "https://example.com/bad"
    assert contents[1]["error_type"] == "FetchError"


@respx.mock
async def test_responses_create_auto_executes_registered_functions():
    first = _agent_response(
        output=[
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "multiply",
                "arguments": '{"a":6,"b":7}',
            }
        ]
    )
    second = _agent_response("resp_done")
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    mcp, _ = build_server(
        _settings(), function_registry={"multiply": lambda args: args["a"] * args["b"]}
    )
    async with connect(mcp._mcp_server) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert "registered_function_call" in tools
        direct = await client.call_tool(
            "registered_function_call", {"name": "multiply", "arguments": {"a": 3, "b": 4}}
        )
        result = await client.call_tool(
            "responses_create",
            {
                "input": "multiply 6 by 7",
                "model": "openai/gpt-5.5",
                "tools": [
                    {
                        "type": "function",
                        "name": "multiply",
                        "parameters": {"type": "object"},
                    }
                ],
                "auto_execute_functions": True,
            },
        )
    assert json.loads(_text(direct))["output"] == 12
    assert json.loads(_text(result))["id"] == "resp_done"
    followup = json.loads(route.calls[1].request.content)
    assert followup["input"][0]["output"] == "42"


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
async def test_sonar_ask_routes_provider_models_to_agent_api():
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(200, json=_agent_response("resp_agent"))
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "sonar_ask",
            {
                "question": "why?",
                "model": "openai/gpt-5.5",
                "reasoning": {"effort": "high"},
                "max_output_tokens": 512,
            },
        )
    assert json.loads(_text(result))["id"] == "resp_agent"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "openai/gpt-5.5"
    assert body["reasoning"] == {"effort": "high"}


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
async def test_deep_research_full_pipeline_with_model_decomposition():
    search_route = respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://a.com", "title": "A", "snippet": "evidence"}]},
        )
    )
    report = {
        "answer": "answer",
        "key_findings": ["finding"],
        "open_questions": [],
        "claims": [
            {
                "claim": "supported",
                "supporting_urls": ["https://a.com"],
                "confidence": "high",
            }
        ],
    }
    chat_route = respx.post("https://api.perplexity.ai/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps({"subquestions": ["angle"]})}}
                    ]
                },
            ),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(report)}}]},
            ),
        ]
    )
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool(
            "deep_research",
            {
                "question": "is x true?",
                "num_subquestions": 2,
                "use_model_decomposition": True,
            },
        )
    payload = json.loads(_text(result))
    assert payload["subquestions"] == ["is x true?", "angle"]
    assert payload["validation_report"]["passed"] is True
    assert search_route.call_count == 2
    assert chat_route.call_count == 2


async def test_deep_research_audit_defensively_handles_missing_validation_report(monkeypatch):
    import perplexity_agent.server as server_mod

    async def incomplete_result(*_args, **_kwargs):
        return {"report": {"answer": "future shape"}}

    monkeypatch.setattr(server_mod, "_deep_research", incomplete_result)
    mcp, _ = build_server(_settings())
    async with connect(mcp._mcp_server) as client:
        result = await client.call_tool("deep_research", {"question": "q"})
    assert not result.isError
    assert json.loads(_text(result))["report"]["answer"] == "future shape"


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


async def test_responses_create_server_validation_happens_before_rate_limit_guard():
    settings = Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=1,
        rate_burst=1,
        store_path=":memory:",
    )
    mcp, _ = build_server(settings, function_registry={"noop": lambda _args: None})
    async with connect(mcp._mcp_server) as client:
        invalid = await client.call_tool(
            "responses_create",
            {
                "input": "q",
                "stream": True,
                "auto_execute_functions": True,
            },
        )
        metrics = await client.call_tool("server_metrics", {})
    assert invalid.isError
    assert not metrics.isError  # invalid request did not spend the only token


async def test_missing_function_registry_is_rejected_before_rate_limit_guard():
    settings = Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=1,
        rate_burst=1,
        store_path=":memory:",
    )
    mcp, _ = build_server(settings)
    async with connect(mcp._mcp_server) as client:
        invalid = await client.call_tool(
            "responses_create",
            {"input": "q", "auto_execute_functions": True},
        )
        metrics = await client.call_tool("server_metrics", {})
    assert invalid.isError
    assert not metrics.isError


async def test_rate_limit_enforced():
    settings = Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        rate_per_minute=1,
        rate_burst=1,
        max_retries=0,
        store_path=":memory:",
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
