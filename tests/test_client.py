import json
import threading

import httpx
import pytest
import respx

from perplexity_agent.client import (
    CircuitBreaker,
    CircuitOpenError,
    PerplexityClient,
    PerplexityError,
    canonical_url,
    dedupe_results,
    response_output_text,
)


def _agent_response(response_id="resp_1", output=None):
    return {
        "created_at": 1,
        "id": response_id,
        "model": "openai/gpt-5.5",
        "object": "response",
        "output": output or [],
        "status": "completed",
    }


def test_canonical_url():
    assert canonical_url("https://Example.com/a/#frag") == "https://example.com/a"
    assert canonical_url("") == ""


def test_canonical_url_preserves_path_and_query_case():
    # Paths/queries are case-sensitive on most servers: only scheme+host fold.
    assert canonical_url("HTTPS://Example.COM/Page?Q=Value") == "https://example.com/Page?Q=Value"
    assert canonical_url("https://a.com/X") != canonical_url("https://a.com/x")


def test_dedupe_results_preserves_order():
    rows = [
        {"url": "https://a.com/x"},
        {"url": "https://a.com/x/"},
        {"url": "https://b.com/y"},
        {"url": ""},
    ]
    out = dedupe_results(rows)
    assert [r["url"] for r in out] == ["https://a.com/x", "https://b.com/y"]


@respx.mock
async def test_search_calls_endpoint(settings):
    route = respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json={"results": [{"url": "https://a.com"}]})
    )
    async with PerplexityClient(settings) as client:
        out = await client.search("test query", max_results=3)
    assert route.called
    assert out["results"][0]["url"] == "https://a.com"
    sent = route.calls.last.request
    assert sent.headers["authorization"].startswith("Bearer pplx-")


@respx.mock
async def test_search_sends_filters_and_people_type(settings):
    route = respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    async with PerplexityClient(settings) as client:
        await client.search(
            "recent space news",
            search_domain_filter=["nasa.gov"],
            search_recency_filter="week",
        )
        await client.people_search("VP Engineering at Stripe")
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["search_domain_filter"] == ["nasa.gov"]
    assert first["search_recency_filter"] == "week"
    assert second["search_type"] == "people"


@respx.mock
async def test_create_response_calls_agent_endpoint(settings):
    response = _agent_response(
        output=[
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
            }
        ]
    )
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(200, json=response)
    )
    async with PerplexityClient(settings) as client:
        out = await client.create_response(
            {"input": "hi", "model": "openai/gpt-5.5", "stream": False}
        )
    assert response_output_text(out) == "hello"
    assert "stream" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_retrieve_response_calls_agent_get_endpoint(settings):
    route = respx.get("https://api.perplexity.ai/v1/agent/resp_123").mock(
        return_value=httpx.Response(200, json=_agent_response("resp_123"))
    )
    async with PerplexityClient(settings) as client:
        out = await client.retrieve_response("resp_123")
    assert route.called
    assert out["id"] == "resp_123"


@respx.mock
async def test_create_response_rejects_malformed_typed_payload(settings):
    respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(200, json={"id": "resp_broken", "output": []})
    )
    async with PerplexityClient(settings) as client:
        with pytest.raises(PerplexityError, match="Unexpected Agent API response shape"):
            await client.create_response({"input": "hi"})


@respx.mock
async def test_stream_response_parses_sse_events(settings):
    completed = _agent_response()
    body = (
        f"data: {json.dumps({'type': 'response.created', 'response': completed})}\n\n"
        'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        f"data: {json.dumps({'type': 'response.completed', 'response': completed})}\n\n"
        "data: [DONE]\n\n"
    )
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    async with PerplexityClient(settings) as client:
        events = [event async for event in client.stream_response({"input": "hi"})]
    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_text.delta",
        "response.completed",
    ]
    assert json.loads(route.calls.last.request.content)["stream"] is True


@respx.mock
async def test_function_tool_chain_executes_registered_handler(settings):
    first = _agent_response(
        output=[
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "add",
                "arguments": '{"a":2,"b":3}',
            }
        ]
    )
    second = _agent_response("resp_2")
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )

    async with PerplexityClient(settings) as client:
        out = await client.run_response_with_tools(
            {
                "input": "add numbers",
                "model": "openai/gpt-5.5",
                "tools": [
                    {
                        "type": "function",
                        "name": "add",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            {"add": lambda args: args["a"] + args["b"]},
        )
    assert out["id"] == "resp_2"
    followup = json.loads(route.calls[1].request.content)
    assert followup["previous_response_id"] == "resp_1"
    assert followup["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "5"}
    ]


@respx.mock
async def test_finance_cache_is_bounded_opt_in(settings):
    settings.finance_cache_ttl_s = 30
    route = respx.post("https://api.perplexity.ai/v1/agent").mock(
        return_value=httpx.Response(200, json=_agent_response("resp_fin"))
    )
    async with PerplexityClient(settings) as client:
        first = await client.search_finance("quote", tickers=["NVDA"])
        second = await client.search_finance("quote", tickers=["NVDA"])
    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_chat_includes_response_format(settings):
    captured = {}

    def responder(request):
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    respx.post("https://api.perplexity.ai/chat/completions").mock(side_effect=responder)
    async with PerplexityClient(settings) as client:
        await client.chat(
            [{"role": "user", "content": "hi"}],
            model="sonar-pro",
            response_format={"type": "json_schema"},
        )
    assert "response_format" in captured["body"]
    assert "sonar-pro" in captured["body"]


@respx.mock
async def test_http_error_raises(settings):
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    async with PerplexityClient(settings) as client:
        with pytest.raises(PerplexityError):
            await client.search("q")


@respx.mock
async def test_retry_honors_retry_after(settings, monkeypatch):
    import asyncio

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings.max_retries = 1
    respx.post("https://api.perplexity.ai/search").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    async with PerplexityClient(settings) as client:
        out = await client.search("q")
    assert out == {"results": []}
    assert sleeps == [2.0]  # server-supplied wait, not the jittered backoff


@respx.mock
async def test_unparseable_retry_after_falls_back_to_jitter(settings, monkeypatch):
    import asyncio

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings.max_retries = 1
    respx.post("https://api.perplexity.ai/search").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    async with PerplexityClient(settings) as client:
        await client.search("q")
    # HTTP-date form isn't parsed: the jittered backoff for attempt 0 is [0.5, 1.0).
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] < 1.0


@respx.mock
async def test_retry_after_is_capped(settings, monkeypatch):
    import asyncio

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings.max_retries = 1
    respx.post("https://api.perplexity.ai/search").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "9999"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    async with PerplexityClient(settings) as client:
        await client.search("q")
    assert sleeps == [30.0]  # hostile/buggy header is capped


@respx.mock
async def test_response_size_cap(settings):
    huge = {"results": [{"blob": "x" * (settings.max_response_bytes + 100)}]}
    respx.post("https://api.perplexity.ai/search").mock(return_value=httpx.Response(200, json=huge))
    async with PerplexityClient(settings) as client:
        with pytest.raises(PerplexityError, match="too large"):
            await client.search("q")


@respx.mock
async def test_response_content_length_rejected_before_read(settings):
    # The shared read_capped helper gives the client the same Content-Length
    # pre-check the page fetcher has (a huge declared size is refused up front).
    settings.max_response_bytes = 100
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, headers={"content-length": "999999"}, content=b"")
    )
    async with PerplexityClient(settings) as client:
        with pytest.raises(PerplexityError, match="Content-Length"):
            await client.search("q")


def test_circuit_breaker_opens_then_half_opens_and_recovers():
    cb = CircuitBreaker(failure_threshold=2, recovery_time=30.0)
    assert cb.acquire() is False  # closed
    cb.on_failure()
    assert cb.acquire() is False  # 1 < threshold
    cb.on_failure()  # opens
    with pytest.raises(CircuitOpenError):
        cb.acquire()
    cb._opened_at -= 31.0  # cooldown elapsed
    assert cb.acquire() is True  # the single half-open probe
    with pytest.raises(CircuitOpenError):  # a concurrent caller is refused
        cb.acquire()
    cb.on_success()
    cb.release(True)
    assert cb.acquire() is False  # fully closed again


def test_circuit_breaker_failed_probe_reopens_without_wedging():
    cb = CircuitBreaker(failure_threshold=1, recovery_time=30.0)
    cb.on_failure()
    cb._opened_at -= 31.0
    assert cb.acquire() is True
    cb.on_failure()  # probe failed → re-open
    cb.release(True)  # slot freed even though it re-opened
    with pytest.raises(CircuitOpenError):
        cb.acquire()
    cb._opened_at -= 31.0
    assert cb.acquire() is True  # a fresh probe is admitted — slot not wedged


def test_circuit_breaker_release_only_clears_own_probe():
    cb = CircuitBreaker(failure_threshold=1, recovery_time=30.0)
    cb.on_failure()
    cb._opened_at -= 31.0
    assert cb.acquire() is True
    cb.release(False)  # a non-probe caller releasing must be a no-op
    with pytest.raises(CircuitOpenError):
        cb.acquire()


def test_circuit_breaker_lock_serializes_transitions(monkeypatch):
    # Deterministic proof the lock gives mutual exclusion: while acquire() holds
    # it, a concurrent on_success() cannot proceed (fails if the lock is removed).
    import perplexity_agent.client as client_mod

    cb = CircuitBreaker(failure_threshold=1, recovery_time=30.0)
    cb.on_failure()
    cb._opened_at -= 31.0  # half-open

    real_monotonic = client_mod.time.monotonic
    entered = threading.Event()
    hold = threading.Event()

    def gated() -> float:
        if threading.current_thread().name == "acquirer" and not entered.is_set():
            entered.set()
            hold.wait(timeout=2)
        return real_monotonic()

    monkeypatch.setattr(client_mod.time, "monotonic", gated)

    probe: dict[str, bool] = {}
    success_done = threading.Event()

    def do_acquire() -> None:
        probe["admitted"] = cb.acquire()

    def do_success() -> None:
        cb.on_success()
        success_done.set()

    ta = threading.Thread(target=do_acquire, name="acquirer")
    ta.start()
    assert entered.wait(1)  # acquire() now parked while holding the lock
    ts = threading.Thread(target=do_success)
    ts.start()
    assert not success_done.wait(0.2)  # on_success() blocked on the same lock
    hold.set()
    ta.join()
    ts.join()
    assert success_done.is_set()
    assert probe["admitted"] is True


@respx.mock
async def test_breaker_opens_after_repeated_5xx(settings):
    settings.max_retries = 0  # 1 attempt, no backoff sleeps
    respx.post("https://api.perplexity.ai/search").mock(return_value=httpx.Response(503))
    async with PerplexityClient(settings) as client:
        client._breaker.failure_threshold = 2
        for _ in range(2):
            with pytest.raises(PerplexityError):
                await client.search("q")
        with pytest.raises(CircuitOpenError):  # breaker now open → fails fast
            await client.search("q")
