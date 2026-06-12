import json

import httpx
import respx

from perplexity_agent.assistant import Assistant, Tab, citation_urls
from perplexity_agent.client import PerplexityClient


def _chat(content, **extra):
    return {"choices": [{"message": {"content": content}}], **extra}


def test_citation_urls_dedupes_and_extracts():
    resp = _chat(
        "x",
        citations=["https://a.com", "https://a.com"],
        search_results=[{"url": "https://b.com"}, {"no_url": 1}],
    )
    assert citation_urls(resp) == ["https://a.com", "https://b.com"]


@respx.mock
async def test_search_dedupes(settings):
    respx.post("https://api.perplexity.ai/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://a.com/x"}, {"url": "https://a.com/x/"}]},
        )
    )
    async with PerplexityClient(settings) as client:
        results = await Assistant(client).search("q")
    assert [r["url"] for r in results] == ["https://a.com/x"]


@respx.mock
async def test_answer_includes_context_and_history(settings):
    captured = {}

    def responder(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_chat("the answer", citations=["https://s.com"]))

    respx.post("https://api.perplexity.ai/chat/completions").mock(side_effect=responder)
    tab = Tab(title="Doc", url="https://doc", text="important context")
    async with PerplexityClient(settings) as client:
        reply = await Assistant(client).answer(
            "what?",
            context=[tab],
            history=[{"role": "user", "content": "earlier"}],
        )
    assert reply.text == "the answer"
    assert reply.citations == ["https://s.com"]
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles[0] == "system"  # answer system prompt
    blob = json.dumps(captured["body"]["messages"])
    assert "important context" in blob
    assert "earlier" in blob


@respx.mock
async def test_summarize_page(settings):
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat("- point one"))
    )
    async with PerplexityClient(settings) as client:
        reply = await Assistant(client).summarize_page("long text", "Title")
    assert "point one" in reply.text


@respx.mock
async def test_group_tabs_parses_schema(settings):
    groups = {"groups": [{"name": "Shopping", "tab_indexes": [0, 1]},
                         {"name": "News", "tab_indexes": [2]}]}
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(json.dumps(groups)))
    )
    tabs = [
        Tab("Amazon", "https://a", ""),
        Tab("eBay", "https://b", ""),
        Tab("BBC", "https://c", ""),
    ]
    async with PerplexityClient(settings) as client:
        out = await Assistant(client).group_tabs(tabs)
    assert [g["name"] for g in out] == ["Shopping", "News"]
    assert [t.title for t in out[0]["tabs"]] == ["Amazon", "eBay"]


@respx.mock
async def test_group_tabs_ignores_bad_indexes(settings):
    groups = {"groups": [{"name": "X", "tab_indexes": [0, 99]}]}
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(json.dumps(groups)))
    )
    tabs = [Tab("A", "https://a", "")]
    async with PerplexityClient(settings) as client:
        out = await Assistant(client).group_tabs(tabs)
    assert len(out) == 1
    assert [t.title for t in out[0]["tabs"]] == ["A"]


@respx.mock
async def test_group_tabs_survives_nonconforming_shapes(settings):
    # json_schema is best-effort: groups as bare strings, or tab_indexes as a
    # non-list, must not raise — they yield no groups instead.
    bad = {"groups": ["Shopping", {"name": "News", "tab_indexes": 3}, {"tab_indexes": [0]}]}
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(json.dumps(bad)))
    )
    tabs = [Tab("A", "https://a", "")]
    async with PerplexityClient(settings) as client:
        out = await Assistant(client).group_tabs(tabs)
    # Only the last entry is well-formed (valid index, default name).
    assert [g["name"] for g in out] == ["Group"]
    assert [t.title for t in out[0]["tabs"]] == ["A"]


def test_citation_urls_dedupes_by_canonical_url():
    # Same source cited with a trailing slash and different case collapses to one,
    # matching how the search path dedupes.
    resp = _chat("x", citations=["https://Example.com/", "https://example.com"])
    assert citation_urls(resp) == ["https://Example.com/"]


@respx.mock
async def test_group_tabs_survives_malformed_response_shape(settings):
    # A response missing choices/message makes message_content() raise ValueError;
    # group_tabs must absorb it and return [], not propagate.
    respx.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    tabs = [Tab("A", "https://a", "")]
    async with PerplexityClient(settings) as client:
        out = await Assistant(client).group_tabs(tabs)
    assert out == []


def test_plan_task_reuses_decompose():
    # No network: plan_task is deterministic decomposition.
    class _Dummy:
        pass

    steps = Assistant(_Dummy()).plan_task("explore widgets", steps=3)  # type: ignore[arg-type]
    assert steps[0] == "explore widgets"
    assert len(steps) == 3
