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
- **Optional per-Space retention caps** for the TUI's local store —
  `PERPLEXITY_MAX_HISTORY_PER_SPACE` and `PERPLEXITY_MAX_TABS_PER_SPACE`. Unset
  (the default) keeps everything: a deployed instance never deletes a user's
  data unless its operator explicitly opts in. When set to N, only the N
  most-recent rows per Space are kept; older rows are pruned on write.

### Security

- The HTTP transport's bearer-token check is now constant-time
  (`hmac.compare_digest` over raw header bytes). A plain `!=` short-circuits on
  the first differing byte, which leaks the token's length and prefix through
  response timing. Comparing bytes also means a malformed (non-ASCII)
  `Authorization` header still returns a clean `401` rather than erroring.
- The rate-limit + audit pairing is centralized in a single `RequestGuard`
  (`security.py`) and applied uniformly to the MCP tools, the TUI commands, and
  the background `/task` monitors. Previously the TUI built an audit logger it
  never called and background probes bypassed the rate limiter entirely, so a new
  surface can no longer acquire rate-limit tokens without also leaving an audit
  trail.
- Untrusted page titles are escaped before they are rendered in the tab bar, so a
  title containing console-markup metacharacters can neither crash the bar nor
  inject styling.

### Changed

- CI now type-checks the package: `mypy` runs under `strict = true` (GitHub
  Actions). The one concession is a per-module relaxation of
  `disallow_any_generics` for `server.py`, because FastMCP only injects a tool's
  `Context` when it is annotated as the bare `Context` — parameterizing it to
  satisfy strict mode would silently break injection.
- CI runs lint, type-check, and dependency-audit across the full Python
  3.11/3.12/3.13 matrix again (not just 3.12), adds a job that exercises the
  **tui-less core install**, and no longer cancels in-progress runs on `main`
  (so every commit that lands keeps a completed test/security record).
- The page fetcher enforces the response-size cap **while streaming** the body —
  an oversized download is aborted before it is fully buffered into memory — and
  resolves DNS off the event loop so a slow nameserver can't freeze the TUI.
- Saved tabs are keyed by URL within a Space: re-opening a page updates that tab
  in place (one row per URL) instead of stacking duplicates, and the store
  indexes its per-Space lookups.

### Fixed

- A background `/task` monitor no longer dies silently on the first transient
  error — it reports the error once and keeps retrying — and quitting the TUI
  awaits in-flight probes before closing the clients they share.
- A 3xx redirect with no `Location` header now reports the real problem instead
  of a misleading "too many redirects".
- `perplexity-agent --transport http tui` now errors instead of silently
  ignoring `--transport` and launching the interactive UI.
- `group_tabs` tolerates schema-nonconforming model output (and a malformed
  response shape) instead of raising.
- A blank retention env var (e.g. `PERPLEXITY_MAX_HISTORY_PER_SPACE=`) is treated
  as "unset" rather than crashing startup with a misleading "API key" error.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
