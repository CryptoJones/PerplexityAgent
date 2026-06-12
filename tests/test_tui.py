import httpx
import pytest
import respx
from pydantic import SecretStr
from textual.widgets import Input, RichLog, Static

from perplexity_agent.config import Settings
from perplexity_agent.tui.app import CometApp


@pytest.fixture
def tui_settings(tmp_path):
    return Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=6000,
        rate_burst=1000,
        store_path=str(tmp_path / "store.db"),
    )


def _chat_reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def _submit(app, pilot, text):
    app.query_one(Input).value = text
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def _content_lines(app):
    return list(app.query_one("#content", RichLog).lines)


async def test_app_boots_with_help(tui_settings):
    app = CometApp(tui_settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _content_lines(app)  # help text rendered on mount


async def test_unknown_command_notifies(tui_settings):
    app = CometApp(tui_settings)
    async with app.run_test() as pilot:
        before = len(_content_lines(app))
        await _submit(app, pilot, "/bogus")
        assert len(_content_lines(app)) > before


async def test_search_command_routes(tui_settings):
    app = CometApp(tui_settings)
    with respx.mock:
        respx.post("https://api.perplexity.ai/search").mock(
            return_value=httpx.Response(
                200, json={"results": [{"url": "https://a.com", "title": "A"}]}
            )
        )
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply("answer")
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/search widgets")
            # Search + answer both rendered content.
            assert _content_lines(app)


async def test_open_creates_tab(tui_settings, monkeypatch):
    import perplexity_agent.fetch as fetch_mod

    monkeypatch.setattr(
        fetch_mod.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    app = CometApp(tui_settings)
    with respx.mock:
        # The fetcher pins the connection to the resolved IP, so mock the IP URL.
        respx.get("https://93.184.216.34/").mock(
            return_value=httpx.Response(200, html="<title>Example</title><p>Body text</p>")
        )
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply("summary")
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/open https://example.com/")
            assert len(app._open_tabs) == 1
            assert app._current is not None
            assert app._current.title == "Example"
            # Tab bar reflects the open tab.
            assert "Example" in str(app.query_one("#tabbar", Static).renderable)


async def test_space_switch_creates_space(tui_settings):
    app = CometApp(tui_settings)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/space work")
        assert app._space == "work"
        assert "work" in app._store.spaces()


def _patch_public_dns(monkeypatch):
    import perplexity_agent.fetch as fetch_mod

    monkeypatch.setattr(
        fetch_mod.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


async def test_bare_text_goes_to_assistant(tui_settings):
    app = CometApp(tui_settings)
    with respx.mock:
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply("hi there")
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "hello")
            chat_lines = list(app.query_one("#chat", RichLog).lines)
            assert chat_lines
            # The exchange is persisted to the store.
            hist = app._store.history(space="default")
            assert {m["role"] for m in hist} == {"user", "assistant"}


async def test_page_interaction_commands(tui_settings, monkeypatch):
    _patch_public_dns(monkeypatch)
    app = CometApp(tui_settings)
    with respx.mock:
        respx.get("https://93.184.216.34/").mock(
            return_value=httpx.Response(200, html="<title>Doc</title><p>content</p>")
        )
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply("response text")
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/open https://example.com/")
            await _submit(app, pilot, "/ask what is this about?")
            await _submit(app, pilot, "/summary")
            await _submit(app, pilot, "/translate Spanish")
            await _submit(app, pilot, "/tabs")
            assert app._current is not None
            assert _content_lines(app)


async def test_ask_and_translate_without_page_warn(tui_settings):
    app = CometApp(tui_settings)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/ask anything")
        await _submit(app, pilot, "/translate French")
        # No current page: both warn rather than crash.
        assert app._current is None
        assert _content_lines(app)


async def test_group_command(tui_settings):
    app = CometApp(tui_settings)
    groups = {"groups": [{"name": "News", "tab_indexes": [0]}]}
    app._open_tabs = [_tab("BBC", "https://bbc.com")]
    with respx.mock:
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply(__import__("json").dumps(groups))
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/group")
            assert _content_lines(app)


async def test_summary_synthesizes_tabs_when_no_current(tui_settings):
    app = CometApp(tui_settings)
    app._open_tabs = [_tab("A", "https://a.com"), _tab("B", "https://b.com")]
    with respx.mock:
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply("combined overview")
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/summary")
            assert _content_lines(app)


async def test_research_command(tui_settings):
    import json

    report = {
        "answer": "the answer",
        "key_findings": ["finding one"],
        "open_questions": [],
        "claims": [
            {"claim": "c", "supporting_urls": ["https://a.com"], "confidence": "high"}
        ],
    }
    app = CometApp(tui_settings)
    with respx.mock:
        respx.post("https://api.perplexity.ai/search").mock(
            return_value=httpx.Response(
                200, json={"results": [{"url": "https://a.com", "title": "A"}]}
            )
        )
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply(json.dumps(report))
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/research are widgets cost-effective?")
            assert _content_lines(app)


async def test_task_register_and_untask(tui_settings):
    app = CometApp(tui_settings)
    with respx.mock:
        respx.post("https://api.perplexity.ai/search").mock(
            return_value=httpx.Response(200, json={"results": [{"url": "https://a.com"}]})
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/task search 5 widgets")
            assert len(app._tasks.list()) == 1
            task_id = app._tasks.list()[0].id
            await _submit(app, pilot, f"/untask {task_id}")
            assert app._tasks.list() == []


async def test_task_usage_errors(tui_settings):
    app = CometApp(tui_settings)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/task bogus args here")
        await _submit(app, pilot, "/task search notanumber widgets")
        await _submit(app, pilot, "/untask notanumber")
        assert app._tasks.list() == []


async def test_space_listing(tui_settings):
    app = CometApp(tui_settings)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/space")  # list, no arg
        assert _content_lines(app)


async def test_tabbar_escapes_markup_in_titles(tui_settings):
    # An untrusted page title with Rich-markup metacharacters must not raise
    # MarkupError or inject styling when the tab bar renders.
    app = CometApp(tui_settings)
    app._open_tabs = [_tab("[2026] Best [GPU] deals", "https://x")]
    app._current = app._open_tabs[0]
    async with app.run_test() as pilot:
        app._refresh_tabbar()
        await pilot.pause()  # force a render; an unescaped title would raise here
        rendered = str(app.query_one("#tabbar", Static).renderable)
        assert "[2026]" in rendered  # shown literally, not parsed as a style tag


async def test_search_surfaces_error_without_crashing(tui_settings):
    # If one of the two concurrent /search calls fails, the error is surfaced and
    # the app keeps running (no orphaned coroutine / unhandled crash).
    app = CometApp(tui_settings)
    with respx.mock:
        respx.post("https://api.perplexity.ai/search").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        respx.post("https://api.perplexity.ai/chat/completions").mock(
            return_value=_chat_reply("answer")
        )
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/search widgets")
            text = "\n".join(str(line) for line in _content_lines(app))
    assert "error" in text.lower() or "perplexity" in text.lower()


def _tab(title, url):
    from perplexity_agent.assistant import Tab

    return Tab(title=title, url=url, text="body")
