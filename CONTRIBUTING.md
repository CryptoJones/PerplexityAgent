# Contributing

Thanks for improving PerplexityAgent. This repo is a security-hardened MCP
server; keep changes small and behavior-preserving unless a change is clearly
warranted.

## Dev setup
```bash
uv sync --extra dev --extra tui
```

## Gates (keep all green; run before committing)
```bash
uv run ruff check .
uv run mypy src
uv run pytest                       # coverage-gated (85%); I/O is mocked, never the network
```

## Rules
- New behavior needs a test. Tests mock the HTTP boundary with `respx` — never
  hit the real network.
- Tool logic goes in a testable `*_impl()`; the `@mcp.tool()` shim stays thin.
- Every file starts with the copyright header.
- Keep `CHANGELOG.md` current.
