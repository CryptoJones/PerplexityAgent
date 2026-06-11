import httpx
import pytest
import respx

from perplexity_agent.client import (
    PerplexityClient,
    PerplexityError,
    canonical_url,
    dedupe_results,
)


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
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(200, json=huge)
    )
    async with PerplexityClient(settings) as client:
        with pytest.raises(PerplexityError, match="too large"):
            await client.search("q")
