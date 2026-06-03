# PerplexityAgent

A simple, **security-hardened** [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) server that gives AI agents — [Claude Code](https://claude.com/claude-code),
Hermes, or any MCP client — access to the [Perplexity](https://docs.perplexity.ai)
Search and Sonar APIs.

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

## Configuration

All optional knobs are environment variables (see [`.env.example`](.env.example)):
timeouts, response-size cap, retry count, rate limits, and an optional JSON
audit-log path.

## Development

```bash
uv sync --extra dev
uv run pytest          # unit tests (no live API needed; httpx is mocked)
uv run ruff check .    # lint
uv run pip-audit       # dependency vulnerability scan
```

## License

MIT — see [`LICENSE`](LICENSE).
