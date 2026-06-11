# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A lifecycle regression test: the stdio server must exit when the MCP client
  closes the pipe, never linger deaf and silently discard requests (the
  failure mode diagnosed in obsidian-mcp, omind#49). Verified the server
  already behaves correctly; the test locks it in.

## [0.2.0] - 2026-06-10

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

### Added

- `deep_research` accepts an opt-in `use_model_decomposition` flag: Sonar derives
  the sub-questions (schema-constrained, length-capped, original question always
  first) instead of the deterministic angle list, falling back to the deterministic
  decomposition on any failure.
- `deep_research` results include a top-level `usage` field (synthesis token
  counts) when the API reports it.
- Audit `tool_result` events now carry a per-call **correlation id** (shared with
  the matching `tool_call`), **latency** (`duration_ms`), and **token usage** —
  making the SIEM-ingestion story concrete.

### Security

- GitHub Actions are pinned to **commit SHAs** instead of retargetable major tags,
  and the gitleaks scan now covers the **full git history** (pinned gitleaks
  v8.30.1) instead of only the working tree.
- The TUI's SQLite store is created **owner-only** (file 0600, directory 0700) —
  it holds browsing history and page text.
- The page fetcher refuses **non-text content types** (PDFs, images, archives …)
  up front; only `text/*`, HTML/XML, and JSON-ish bodies are fetched.
- The page fetcher now **pins each connection to the validated IP** (request goes
  to the resolved address, with the original hostname as the `Host` header and TLS
  SNI), closing the DNS-rebinding TOCTOU window that was previously documented as a
  residual risk in `SECURITY.md`.
- The fetch size cap is now enforced **while the body streams in**: an oversized
  `Content-Length` is rejected before any read, and a body that exceeds
  `PERPLEXITY_MAX_RESPONSE_BYTES` mid-download is aborted instead of being fully
  buffered into memory first (the behavior `SECURITY.md` already described).
- The HTTP transport's bearer-token check is now constant-time
  (`hmac.compare_digest` over raw header bytes). A plain `!=` short-circuits on
  the first differing byte, which leaks the token's length and prefix through
  response timing. Comparing bytes also means a malformed (non-ASCII)
  `Authorization` header still returns a clean `401` rather than erroring.

### Fixed

- `canonical_url` now lowercases only the scheme and host, preserving path/query
  case — case-sensitive paths no longer falsely dedupe or mis-validate citations.
- Audit-log redaction no longer false-positives on token *count* fields
  (`prompt_tokens`, `completion_tokens`, …); real `*token*` credentials are still
  redacted.
- Saved tabs no longer accumulate duplicates across sessions: the store dedupes
  per space+URL on save and keeps only the newest 50 tabs per space.
- Background `/task` monitors no longer die silently on a failed probe (network
  blip, rate limit, fetch refusal): a failure notifies the user and the watch keeps
  running; after 5 consecutive failures the task announces it is giving up and
  removes itself from the listing.

### Changed

- `deep_research` now searches its sub-questions **concurrently** (bounded at 4
  in flight) instead of serially — roughly a 4× latency cut on the retrieval stage
  for the default 4–8 sub-questions. Result order is preserved, so dedupe and
  citation validation are unchanged.
- The API client honors a numeric `Retry-After` header on retryable responses
  (429/5xx), capped at 30 s, instead of always using its own jittered backoff.
- CI now type-checks the package: `mypy` runs under `strict = true` (GitHub
  Actions). The one concession is a per-module relaxation of
  `disallow_any_generics` for `server.py`, because FastMCP only injects a tool's
  `Context` when it is annotated as the bare `Context` — parameterizing it to
  satisfy strict mode would silently break injection.

## [0.1.0] - 2026-06-03

Initial release: a security-hardened MCP server exposing `perplexity_search`,
`sonar_ask`, and `deep_research` (decompose → search → dedupe → synthesize →
validate citations) over stdio, with an optional bearer-token HTTP transport.
Strict pydantic input validation, token-bucket rate limiting, redacting JSON
audit log, and CI (ruff, pytest, pip-audit, gitleaks, CodeQL).

[Unreleased]: https://codeberg.org/CryptoJones/PerplexityAgent/compare/v0.2.0...HEAD
[0.2.0]: https://codeberg.org/CryptoJones/PerplexityAgent/compare/v0.1.0...v0.2.0
[0.1.0]: https://codeberg.org/CryptoJones/PerplexityAgent/releases/tag/v0.1.0

Proudly Made in Nebraska. Go Big Red! 🌽 https://xkcd.com/2347/
