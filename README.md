# PerplexityAgent

[![Release](https://img.shields.io/gitea/v/release/CryptoJones/PerplexityAgent?gitea_url=https%3A%2F%2Fcodeberg.org&label=release)](https://codeberg.org/CryptoJones/PerplexityAgent/releases)
[![CI](https://github.com/CryptoJones/PerplexityAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/CryptoJones/PerplexityAgent/actions/workflows/ci.yml)
[![CodeQL](https://github.com/CryptoJones/PerplexityAgent/actions/workflows/codeql.yml/badge.svg)](https://github.com/CryptoJones/PerplexityAgent/actions/workflows/codeql.yml)
[![Python ≥ 3.11](https://img.shields.io/badge/python-%E2%89%A5%203.11-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A simple, **security-hardened** [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) server that gives AI agents — [Claude Code](https://claude.com/claude-code),
Hermes, or any MCP client — access to the [Perplexity](https://docs.perplexity.ai)
Search, Sonar, and Responses-compatible Agent APIs.

It follows a retrieval-first reference architecture (search and synthesis kept
separate, citations validated against retrieval metadata) and applies the defensive
controls from the NSA's *Model Context Protocol (MCP): Security Design Considerations*
(May 2026). See [`SECURITY.md`](SECURITY.md) for the full control mapping.

- **Canonical repo:** https://codeberg.org/CryptoJones/PerplexityAgent
- **Mirror:** https://github.com/CryptoJones/PerplexityAgent

## Tools

| Tool | Description |
| --- | --- |
| `perplexity_search` | Ranked web results from the Perplexity Search API. |
| `sonar_ask` | A grounded answer from Sonar / Sonar Pro (OpenAI-compatible chat). |
| `deep_research` | Multi-step pipeline: decompose → search each sub-question → dedupe → synthesize (JSON schema) → **validate citations** → return a cited report with a `validation_report`. |
| `responses_create` | Full Agent API: reasoning, multimodal input, built-in/custom tools, streaming events, and response chaining. |
| `responses_retrieve` | Retrieve a stored Agent response snapshot by its `resp_` ID. |
| `finance_search` | Agent response with Perplexity's structured finance tool enabled. |
| `people_search` | Structured results from the Search API's people index. |
| `fetch_url` | Fetch a bounded set of public URLs through the SSRF-hardened page fetcher. |
| `registered_function_call` | Invoke an operator-registered Python handler; present only when a registry is configured. |
| `retrieve` | Retrieve a result offloaded because it exceeded the output budget. |
| `server_metrics` | In-process request, latency, and rate-limit counters. |

## Requirements

- Python ≥ 3.11
- [`uv`](https://docs.astral.sh/uv/)
- A Perplexity API key (https://www.perplexity.ai/settings/api)

## Setup

```bash
git clone https://codeberg.org/CryptoJones/PerplexityAgent.git
cd PerplexityAgent
uv sync                      # install (add --extra dev for tests)
cp .env.example .env         # then edit .env and set PERPLEXITY_API_KEY
```

The API key is read **server-side only** (from the environment or `.env`) and is
never returned in any tool output. The server refuses to start without it.

## Running

### stdio (default — recommended)

Runs as a local subprocess of the agent with no network exposure:

```bash
uv run perplexity-agent
```

### Register with Claude Code

```bash
claude mcp add perplexity -- uv --directory /abs/path/to/PerplexityAgent run perplexity-agent
```

or in your MCP client config (`mcpServers`):

```json
{
  "mcpServers": {
    "perplexity": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/PerplexityAgent", "run", "perplexity-agent"],
      "env": { "PERPLEXITY_API_KEY": "pplx-..." }
    }
  }
}
```

### Hermes

Hermes consumes MCP servers over stdio the same way — point it at the
`uv ... run perplexity-agent` command with `PERPLEXITY_API_KEY` in the environment.

### Optional hardened HTTP transport

Off by default. It **refuses to start without a bearer token** and binds to
localhost. Only enable it if you understand the added attack surface (see
`SECURITY.md`):

```bash
PERPLEXITY_HTTP_AUTH_TOKEN="$(openssl rand -hex 32)" uv run perplexity-agent --transport http
```

Clients must send `Authorization: Bearer <token>`. Terminate TLS in front of it
(reverse proxy) and keep it behind a filtering egress proxy.

## Calling the tools

Once the server is registered, the agent calls these tools automatically. The
signatures, sample arguments, and return shapes are below.

### `perplexity_search`

Ranked web results from the Search API.

| Param | Type | Default | Bounds |
| --- | --- | --- | --- |
| `query` | string | — (required) | 1–4096 chars |
| `max_results` | int | `5` | 1–20 |
| `max_tokens_per_page` | int | `1024` | 128–4096 |

```json
{ "query": "latest CRISPR base-editing clinical trials", "max_results": 8 }
```

Returns the raw Search API payload, e.g.:

```json
{
  "results": [
    { "title": "…", "url": "https://…", "snippet": "…" }
  ]
}
```

Optional Search API filters include `search_domain_filter` (up to 20 domains),
`search_language_filter`, `search_recency_filter`, and published/last-updated
before/after dates in `M/D/YYYY` form.

### `responses_create`

Calls `POST /v1/agent` using Perplexity's OpenAI Responses-compatible request
shape. It accepts text or message-array `input`, provider-qualified `model` (or a
fallback `models` list / `preset`), `reasoning`, `response_format`, `tools`,
`max_output_tokens`, and `max_steps`.

Set `store: true`, use `responses_retrieve` to read the returned ID later, and pass
that ID as `previous_response_id` to continue the response. Snapshots are kept in
the local SQLite store and scoped to the originating MCP session in addition to
Perplexity's upstream persistence. Retention defaults to the newest 100 snapshots
per session and is configurable with `PERPLEXITY_MAX_RESPONSES_PER_SESSION`.

Function tools support both client-driven and automatic chaining. Pass their JSON
schemas in `tools`; client-driven callers can submit a `function_call_output` with
the returned `call_id`. Server operators can instead pass an allowlisted
`function_registry` to `build_server()` and set `auto_execute_functions: true`.
Only registered handlers execute, arguments and outputs are bounded, errors become
tool outputs, and continuation rounds are capped. Registered handlers are also
available to MCP clients through `registered_function_call`.

For multimodal input, use message content parts:

```json
{
  "input": [{
    "type": "message",
    "role": "user",
    "content": [
      {"type": "input_text", "text": "Describe this image"},
      {"type": "input_image", "image_url": "https://example.com/image.png"}
    ]
  }],
  "model": "openai/gpt-5.5"
}
```

When `stream: true`, the API's SSE events (`response.created`, text deltas,
`response.completed`, and other item events) are parsed and returned in an
`events` array. MCP tool calls are request/response, so this surface collects the
events rather than emitting incremental MCP messages.

### Specialized search and fetch tools

- `finance_search` takes a natural-language `query` plus optional `categories`
  and `tickers` hints, and enables the Agent API's `finance_search` tool.
- `people_search` takes `query`, `max_results`, and `max_tokens_per_page`, and
  routes the standard Search API through `search_type="people"`.
- `fetch_url` accepts either `url` or a `urls` list plus an explicit `max_urls`
  cap (1–10), and returns extracted `contents` with final URLs, titles, text, and
  prompt-injection flags. It validates and pins every redirect hop to prevent SSRF
  and DNS rebinding.

Finance caching is disabled by default because prices change rapidly. Set
`PERPLEXITY_FINANCE_CACHE_TTL_S` to a small value (maximum 300 seconds) to enable
a bounded per-client cache for identical category/ticker queries.

### `sonar_ask`

A grounded answer from Sonar (OpenAI-compatible chat completion).

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `question` | string | — (required) | 1–4096 chars |
| `model` | string | `"sonar"` | legacy `sonar`/`sonar-pro` or an Agent `provider/model` ID |
| `system_prompt` | string | `null` | optional, ≤ 4096 chars |
| `reasoning` | object | `null` | optional Agent reasoning effort (`low` through `max`) |
| `response_format` | object | `null` | optional text/JSON/JSON-schema output format |
| `max_output_tokens` | int | `null` | required for Anthropic Agent models |

```json
{
  "question": "What changed in the EU AI Act's 2026 enforcement timeline?",
  "model": "sonar-pro",
  "system_prompt": "Answer concisely and cite sources."
}
```

Returns the chat-completion payload; the answer is at
`choices[0].message.content`, with citations in the response metadata.

### `deep_research`

The full pipeline: decompose → search each sub-question → dedupe → synthesize
(JSON schema) → validate citations.

| Param | Type | Default | Bounds |
| --- | --- | --- | --- |
| `question` | string | — (required) | 1–4096 chars |
| `num_subquestions` | int | `4` | 1–8 |
| `model` | string | `"sonar-pro"` | `"sonar"` or `"sonar-pro"` |
| `max_results_per_subquestion` | int | `5` | 1–10 |
| `use_model_decomposition` | bool | `false` | ask Sonar to derive the sub-questions (one extra call; falls back to the deterministic angles on any failure) |

```json
{ "question": "Is small modular nuclear cost-competitive with grid-scale solar?", "num_subquestions": 5 }
```

Returns a structured, **citation-validated** report:

```json
{
  "question": "…",
  "subquestions": ["…", "…"],
  "sources": [{ "title": "…", "url": "https://…", "snippet": "…" }],
  "report": {
    "answer": "…",
    "key_findings": ["…"],
    "open_questions": ["…"],
    "claims": [
      { "claim": "…", "supporting_urls": ["https://…"], "confidence": "high" }
    ]
  },
  "validation_report": {
    "total_claims": 6,
    "all_claims_supported": true,
    "all_urls_known": true,
    "passed": true,
    "flagged": []
  },
  "security_flags": { "possible_prompt_injection_patterns": [] },
  "usage": { "prompt_tokens": 1234, "completion_tokens": 567 }
}
```

A claim whose URL was never seen in retrieval is downgraded to `low` confidence
and listed in `validation_report.flagged` — `passed` is `false` if any claim is
unsupported or cites an unknown URL.

### From a Claude Code / agent prompt

You don't construct the JSON yourself — just ask, and the model picks the tool:

```
> Use deep_research to assess whether small modular reactors are cost-competitive
  with grid-scale solar, then summarize only the high-confidence claims.
```

### From a raw MCP client (stdio, for testing)

Any MCP client works. Using the Python SDK that ships with this project:

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="uv",
        args=["--directory", "/abs/path/to/PerplexityAgent", "run", "perplexity-agent"],
        env={"PERPLEXITY_API_KEY": "pplx-..."},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print([t.name for t in (await session.list_tools()).tools])
            result = await session.call_tool(
                "perplexity_search", {"query": "what is MCP", "max_results": 3}
            )
            print(result.content)

asyncio.run(main())
```

You can also explore the tools interactively with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run perplexity-agent
```

### Over the HTTP transport

With the server started via `--transport http`, point any streamable-HTTP MCP
client at `http://127.0.0.1:8080/mcp` and send the bearer token:

```
Authorization: Bearer <PERPLEXITY_HTTP_AUTH_TOKEN>
```

## Comet-style TUI (`perplexity-agent tui`)

An optional interactive terminal app that brings the spirit of Perplexity's
[Comet](https://www.perplexity.ai/comet/) browser to the terminal, backed by the
same Search / Sonar / deep-research client. A terminal can't render web pages, drive
a real browser (clicking, booking, buying), or do voice — those are out of scope by
physics. Everything else maps onto terminal-feasible equivalents:

| Comet feature | In the TUI |
| --- | --- |
| Assistant sidebar | A persistent chat pane (Sonar) that answers with your open "tabs" as context |
| Answer-first search | `/search` — ranked results **plus** a grounded, cited answer |
| Open / summarize a page | `/open <url>` — SSRF-guarded fetch → readable text → one-click summary |
| Ask about / translate a page | `/ask <q>`, `/translate <lang>` on the current page |
| Chat with your tabs / synthesis | `/summary` across all open tabs; bare chat is tab-aware |
| AI tab grouping | `/group` clusters open tabs into named groups |
| Deep research | `/research <q>` runs the full validated, cited pipeline |
| Memory & Spaces | Local SQLite store (owner-only 0600; tabs dedupe per space+URL, recent-tab cap and optional history retention are configurable); `/space [name]` switches workspaces |
| Background / scheduled tasks | `/task search\|fetch <seconds> <target>` monitors and alerts on change; `/untask <id>` stops it |
| Agentic task planning | Research-only planning (decompose a goal); **no real web actions** |

Install the extra and launch it (needs `PERPLEXITY_API_KEY`, same as the server):

```bash
uv sync --extra tui
uv run perplexity-agent tui
```

The page fetcher (`/open`) is the only egress path other than the Perplexity API and
is reachable **only from the TUI**, never via the MCP tools. It is SSRF-hardened
(scheme allowlist, private/loopback/link-local IPs rejected on every redirect hop,
connections pinned to the validated IP, a streaming size cap, and a text-only
content-type allowlist) and flags fetched text for indirect prompt injection before
it reaches Sonar. See [`SECURITY.md`](SECURITY.md). The MCP tool surface is unchanged.

## Configuration

All optional knobs are environment variables (see [`.env.example`](.env.example)):
timeouts, response-size cap, retry count, rate limits, an optional JSON audit-log
path, and (for the TUI) the fetch User-Agent, `PERPLEXITY_FETCH_ALLOW_PRIVATE`,
`PERPLEXITY_STORE_PATH`, and the per-Space store retention caps
(`PERPLEXITY_MAX_TABS_PER_SPACE`, default 50; `PERPLEXITY_MAX_HISTORY_PER_SPACE`,
default unbounded — set a positive integer to keep only the N most-recent rows
per Space, or leave blank to keep everything).

## Development

```bash
uv sync --extra dev --extra tui   # add --extra tui to exercise the TUI tests
uv run pytest          # unit tests (no live API needed; httpx is mocked)
uv run ruff check .    # lint
uv run mypy src        # type check (strict)
uv run pip-audit       # dependency vulnerability scan
```

## License

MIT — see [`LICENSE`](LICENSE).

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
