# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Comet-style interactive TUI** (`perplexity-agent tui`, optional `tui` extra).
  A Textual app that maps Perplexity Comet's browser features onto the terminal,
  backed by the existing Search / Sonar / deep-research client: an assistant chat
  sidebar, answer-first `/search`, page `/open` + `/summary` + `/ask` + `/translate`,
  "chat with your tabs" cross-tab synthesis, AI `/group`ing, `/research`, local
  Spaces/history (SQLite), and background `/task` monitors. Out of scope by physics:
  voice and real web actions (clicking/booking/buying).
- An **SSRF-hardened page fetcher** (`fetch.py`) backing `/open`: scheme allowlist,
  private/loopback/link-local IP rejection re-checked on every redirect hop,
  size/time caps, and indirect-prompt-injection flagging. Reachable only from the
  TUI, never from an MCP tool — the tool surface is unchanged. See `SECURITY.md`.

### Security

- The HTTP transport's bearer-token check is now constant-time
  (`hmac.compare_digest` over raw header bytes). A plain `!=` short-circuits on
  the first differing byte, which leaks the token's length and prefix through
  response timing. Comparing bytes also means a malformed (non-ASCII)
  `Authorization` header still returns a clean `401` rather than erroring.

### Changed

- CI now type-checks the package: `mypy` runs under `strict = true` (GitHub
  Actions). The one concession is a per-module relaxation of
  `disallow_any_generics` for `server.py`, because FastMCP only injects a tool's
  `Context` when it is annotated as the bare `Context` — parameterizing it to
  satisfy strict mode would silently break injection.
