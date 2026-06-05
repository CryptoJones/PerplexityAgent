"""The Textual app: a Comet-style assistant browser for the terminal.

Layout: an assistant **chat sidebar** on the left, a **content pane** on the right,
a **tab bar** listing open "tabs" (fetched pages / saved answers), and a command
input at the bottom. Everything is driven from one input via a small command
palette (``/search``, ``/open``, ``/summary`` …); bare text goes to the assistant.

Each Perplexity / fetch call runs in a Textual worker so the UI never blocks. The
app owns one :class:`PerplexityClient`, one :class:`PageFetcher`, one :class:`Store`,
one :class:`Assistant`, and one :class:`TaskManager`, sharing them across handlers.
"""

from __future__ import annotations

import time

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from ..assistant import Assistant, Reply, Tab
from ..client import PerplexityClient
from ..config import Settings, load_settings
from ..fetch import FetchError, PageFetcher
from ..memory import Store, StoredTab
from ..research import deep_research
from ..security import AuditLogger, RateLimitError, TokenBucket
from ..tasks import TaskManager

_HELP = """\
# Comet-in-the-terminal — commands

- **`/search <query>`** — answer-first web search (ranked results).
- **`/open <url>`** — fetch a page into a tab (SSRF-guarded) and summarize it.
- **`/ask <question>`** — ask about the current page.
- **`/summary`** — summarize the current page, or all tabs if none is current.
- **`/tabs`** — list open tabs. **`/group`** — AI-group the open tabs.
- **`/research <question>`** — full deep-research pipeline (cited, validated).
- **`/translate <lang>`** — translate the current page.
- **`/space [name]`** — list Spaces, or switch/create one.
- **`/task search|fetch <seconds> <target>`** — background monitor; **`/untask <id>`** to stop.
- **`/help`** — this help. Bare text (no slash) goes to the assistant with tab context.

_Out of scope by physics: voice, and real web actions (clicking/booking/buying)._
"""


class CometApp(App[None]):
    """Comet-style assistant browser in the terminal."""

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #sidebar { width: 38%; border-right: solid $accent; }
    #main { width: 1fr; }
    #tabbar { height: 3; border-bottom: solid $accent; padding: 0 1; color: $text-muted; }
    #chat, #content { height: 1fr; padding: 0 1; }
    Input { dock: bottom; }
    """

    TITLE = "PerplexityAgent — Comet TUI"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self._settings = settings or load_settings()
        # Built here (not in on_mount) so they're always present and non-optional;
        # constructing the httpx/sqlite clients needs no running loop. on_unmount
        # closes them. UI-only setup (greeting, focus) stays in on_mount.
        self._client = PerplexityClient(self._settings)
        self._fetcher = PageFetcher(self._settings)
        self._store = Store.from_settings(self._settings)
        self._assistant = Assistant(self._client)
        self._tasks = TaskManager(self._assistant, self._fetcher, self._notify)
        self._bucket = TokenBucket(
            self._settings.rate_per_minute, self._settings.rate_burst
        )
        self._audit = AuditLogger(self._settings.audit_log_path)
        self._space = "default"
        self._open_tabs: list[Tab] = []
        self._current: Tab | None = None

    # --- composition / lifecycle ------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield RichLog(id="chat", wrap=True, markup=True, highlight=True)
            with Vertical(id="main"):
                yield Static("No open tabs", id="tabbar")
                yield RichLog(id="content", wrap=True, markup=True, highlight=True)
        yield Input(placeholder="Type a message, or /help for commands", id="command")
        yield Footer()

    def on_mount(self) -> None:
        self._store.create_space(self._space, now=time.time())
        self._content().write(Markdown(_HELP))
        self._chat().write("[b]Assistant ready.[/b] Ask me anything.")
        self.query_one(Input).focus()

    async def on_unmount(self) -> None:
        await self._tasks.aclose()
        await self._fetcher.aclose()
        await self._client.aclose()
        self._store.close()

    # --- small accessors ---------------------------------------------------
    def _chat(self) -> RichLog:
        return self.query_one("#chat", RichLog)

    def _content(self) -> RichLog:
        return self.query_one("#content", RichLog)

    def _notify(self, message: str) -> None:
        self._content().write(Text(message, style="yellow"))

    def _refresh_tabbar(self) -> None:
        if not self._open_tabs:
            label = "No open tabs"
        else:
            parts = []
            for i, t in enumerate(self._open_tabs):
                mark = "*" if t is self._current else " "
                parts.append(f"{mark}{i + 1}:{t.title[:22]}")
            label = "  ".join(parts)
        self.query_one("#tabbar", Static).update(label)

    # --- input dispatch ----------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        self.run_worker(self._dispatch(text), exclusive=False)

    async def _dispatch(self, text: str) -> None:
        try:
            if text.startswith("/"):
                await self._command(text)
            else:
                await self._assist(text)
        except RateLimitError as exc:
            self._notify(f"Rate limited: {exc}")
        except FetchError as exc:
            self._notify(f"Fetch error: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            self._notify(f"Error: {exc}")

    async def _command(self, text: str) -> None:
        cmd, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        handler = {
            "help": self._cmd_help,
            "search": self._cmd_search,
            "open": self._cmd_open,
            "ask": self._cmd_ask,
            "summary": self._cmd_summary,
            "tabs": self._cmd_tabs,
            "group": self._cmd_group,
            "research": self._cmd_research,
            "translate": self._cmd_translate,
            "space": self._cmd_space,
            "task": self._cmd_task,
            "untask": self._cmd_untask,
        }.get(cmd.lower())
        if handler is None:
            self._notify(f"Unknown command /{cmd}. Try /help.")
            return
        self._bucket.acquire()
        await handler(rest)

    # --- assistant (bare text) --------------------------------------------
    async def _assist(self, question: str) -> None:
        self._bucket.acquire()
        self._chat().write(Text(f"› {question}", style="bold cyan"))
        self._store.add_message("user", question, now=time.time(), space=self._space)
        history = self._store.history(space=self._space, limit=20)[:-1]
        reply = await self._assistant.answer(
            question, context=self._open_tabs, history=history
        )
        self._write_reply(self._chat(), reply)
        self._store.add_message("assistant", reply.text, now=time.time(), space=self._space)

    # --- command handlers --------------------------------------------------
    async def _cmd_help(self, _rest: str) -> None:
        self._content().write(Markdown(_HELP))

    async def _cmd_search(self, rest: str) -> None:
        if not rest:
            self._notify("Usage: /search <query>")
            return
        self._content().write(Text(f"Searching: {rest}", style="bold"))
        results = await self._assistant.search(rest)
        lines = [f"{i + 1}. [{r.get('title') or r.get('url')}]({r.get('url')})"
                 for i, r in enumerate(results)]
        self._content().write(Markdown("\n".join(lines) or "_No results._"))
        reply = await self._assistant.answer(rest, context=self._open_tabs)
        self._write_reply(self._content(), reply)

    async def _cmd_open(self, rest: str) -> None:
        if not rest:
            self._notify("Usage: /open <url>")
            return
        self._content().write(Text(f"Fetching: {rest}", style="bold"))
        page = await self._fetcher.fetch(rest)
        if page.injection_flags:
            self._notify(f"⚠ possible prompt-injection patterns in page: {page.injection_flags}")
        tab = Tab(title=page.title or page.final_url, url=page.final_url, text=page.text)
        self._add_tab(tab)
        summary = await self._assistant.summarize_page(page.text, tab.title)
        self._content().write(Markdown(f"## {tab.title}\n{tab.url}"))
        self._write_reply(self._content(), summary)

    async def _cmd_ask(self, rest: str) -> None:
        if self._current is None:
            self._notify("No current page. /open a URL first.")
            return
        if not rest:
            self._notify("Usage: /ask <question about the page>")
            return
        reply = await self._assistant.ask_page(self._current.text, rest, self._current.title)
        self._write_reply(self._content(), reply)

    async def _cmd_summary(self, _rest: str) -> None:
        if self._current is not None:
            reply = await self._assistant.summarize_page(
                self._current.text, self._current.title
            )
        else:
            reply = await self._assistant.synthesize_tabs(self._open_tabs)
        self._write_reply(self._content(), reply)

    async def _cmd_tabs(self, _rest: str) -> None:
        if not self._open_tabs:
            self._content().write("_No open tabs._")
            return
        lines = [f"{i + 1}. [{t.title}]({t.url}) — _{t.kind}_"
                 for i, t in enumerate(self._open_tabs)]
        self._content().write(Markdown("\n".join(lines)))

    async def _cmd_group(self, _rest: str) -> None:
        groups = await self._assistant.group_tabs(self._open_tabs)
        if not groups:
            self._content().write("_No tabs to group._")
            return
        out = []
        for g in groups:
            out.append(f"### {g['name']}")
            out.extend(f"- [{t.title}]({t.url})" for t in g["tabs"])
        self._content().write(Markdown("\n".join(out)))

    async def _cmd_research(self, rest: str) -> None:
        if not rest:
            self._notify("Usage: /research <question>")
            return
        self._content().write(Text(f"Deep research: {rest} (this can take a while)…", style="bold"))
        result = await deep_research(self._client, rest)
        report = result["report"]
        md = [f"## {rest}", "", report.get("answer", "")]
        if report.get("key_findings"):
            md.append("\n**Key findings**")
            md += [f"- {f}" for f in report["key_findings"]]
        vr = result["validation_report"]
        md.append(f"\n_citations validated: {vr['passed']} "
                  f"({vr['total_claims']} claims)_")
        self._content().write(Markdown("\n".join(md)))

    async def _cmd_translate(self, rest: str) -> None:
        if self._current is None:
            self._notify("No current page. /open a URL first.")
            return
        lang = rest or "English"
        reply = await self._assistant.translate_page(self._current.text, lang)
        self._write_reply(self._content(), reply)

    async def _cmd_space(self, rest: str) -> None:
        if not rest:
            spaces = self._store.spaces()
            self._content().write(Markdown(
                "**Spaces:** " + ", ".join(f"`{s}`" for s in spaces)
                + f"\n\nCurrent: `{self._space}`"
            ))
            return
        self._space = rest
        self._store.create_space(rest, now=time.time())
        self._open_tabs = [
            Tab(t.title, t.url, t.text, t.kind) for t in self._store.tabs(space=rest)
        ]
        self._current = self._open_tabs[-1] if self._open_tabs else None
        self._refresh_tabbar()
        self._notify(f"Switched to space '{rest}'.")

    async def _cmd_task(self, rest: str) -> None:
        parts = rest.split(maxsplit=2)
        if len(parts) < 3 or parts[0] not in ("search", "fetch"):
            self._notify("Usage: /task search|fetch <seconds> <target>")
            return
        kind, interval_s, target = parts[0], parts[1], parts[2]
        try:
            interval = max(5.0, float(interval_s))
        except ValueError:
            self._notify("Interval must be a number of seconds.")
            return
        task = self._tasks.add(kind, target, interval)  # type: ignore[arg-type]
        self._notify(f"Started task {task.id}: {kind} every {interval:g}s on '{target}'.")

    async def _cmd_untask(self, rest: str) -> None:
        try:
            task_id = int(rest)
        except ValueError:
            self._notify("Usage: /untask <id>")
            return
        ok = self._tasks.remove(task_id)
        self._notify(f"Task {task_id} stopped." if ok else f"No task {task_id}.")

    # --- helpers -----------------------------------------------------------
    def _add_tab(self, tab: Tab) -> None:
        self._open_tabs.append(tab)
        self._current = tab
        self._store.save_tab(
            StoredTab(tab.title, tab.url, tab.kind, tab.text),
            now=time.time(),
            space=self._space,
        )
        self._refresh_tabbar()

    def _write_reply(self, log: RichLog, reply: Reply) -> None:
        log.write(Markdown(reply.text or "_(no answer)_"))
        if reply.citations:
            cites = "\n".join(f"- {u}" for u in reply.citations[:10])
            log.write(Markdown(f"**Sources**\n{cites}"))


def run_tui(settings: Settings | None = None) -> None:
    """Launch the TUI (blocking)."""
    CometApp(settings).run()
