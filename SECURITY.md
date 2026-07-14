# Security

PerplexityAgent is designed to be a *secure-by-default* MCP server. This document
maps its controls to the recommendations in the NSA's **Model Context Protocol
(MCP): Security Design Considerations for AI-Driven Automation** (U/OO/6030316-26,
May 2026) and describes the threat model and how to report issues.

## Threat model (in scope)

- A **malicious or compromised MCP client** sending malformed, oversized, or
  out-of-range tool parameters.
- **Untrusted web content** returned by Perplexity that may carry indirect prompt
  injection aimed at downstream agents.
- **Resource-exhaustion / fatigue** techniques (prompt storms, recursive requests).
- **Secret leakage** of the Perplexity API key through tool output or logs.

Out of scope: the security of the Perplexity service itself, and the trust of the
operating-system account the server runs under.

## NSA recommendation → control mapping

| NSA recommendation | How PerplexityAgent implements it |
| --- | --- |
| **Choose supported MCP projects** | Built on the official `mcp` Python SDK (FastMCP). Dependencies are pinned and fully locked in `uv.lock`. |
| **Design for boundaries / least privilege** | Default **stdio** transport runs locally with no network exposure. No shell execution, no filesystem writes (except an optional, explicitly-configured audit-log path). The API key lives only in the client layer and is **never** returned by a tool. Egress goes to `api.perplexity.ai`; the optional TUI and explicit `fetch_url` tool add an SSRF-guarded public-page fetcher (see below). |
| **Validate parameters** | Every tool input is validated against a strict `pydantic` model (`schemas.py`) with bounded strings/arrays (including Agent input, model chains, domains, tools, and image payloads), numeric ranges (`max_results` 1–20, `max_steps` 1–10), and constrained enums. Unknown fields are rejected (`extra="forbid"`), preventing parameter smuggling. |
| **Constrain & sandbox tool execution** | Per-request timeouts, a hard response-size cap, and capped retries with jittered backoff (`client.py`). Run the process under seccomp/AppArmor/SELinux or in a container for OS-level isolation (see below). |
| **Sign & verify messages (transport)** | stdio mode is local-trusted. The optional HTTP transport **refuses to start without a bearer token**, binds to localhost by default, and rejects any request lacking the exact `Authorization: Bearer <token>` header. Terminate TLS at a reverse proxy in front of it. |
| **Filter & monitor outputs / chained execution** | Perplexity responses are treated as **untrusted input** to the next stage. `deep_research` scans retrieved snippets for indirect-prompt-injection patterns and surfaces them in `security_flags`. Citations are taken only from API metadata, never from model free-text. |
| **Instrument for logging & detection** | A structured JSON audit log (`security.py`) records every tool call and result with redacted parameters, a result hash, validation status, a per-call **correlation id** (shared by the `tool_call`/`tool_result` pair), **latency** (`duration_ms`), and **token usage** when the API reports it — suitable for SIEM ingestion. |
| **Track & patch vulnerabilities** | Pinned deps + `uv.lock`; CI runs `pip-audit` on every push and Dependabot is enabled. GitHub Actions are **pinned to commit SHAs** (not retargetable tags), and the gitleaks secret scan covers the **full git history**, not just the worktree. |
| **DoS / fatigue resistance** | A token-bucket rate limiter (`rate_per_minute` / `rate_burst`) that — via `RequestGuard` — meters the MCP tools, the TUI commands, and background `/task` probes alike, plus bounded sub-question counts, input-size caps, and request timeouts. The TUI's local store is bounded by per-Space retention caps (`PERPLEXITY_MAX_TABS_PER_SPACE` / `PERPLEXITY_MAX_HISTORY_PER_SPACE`). |
| **Access control / token security** | `PERPLEXITY_API_KEY` is loaded server-side from the environment only; the server fails fast if it is absent. No token passthrough. |

## Interactive TUI page fetcher (added egress surface)

The **page fetcher** (`fetch.py`) backs both the optional TUI's `/open <url>` command
and the explicit `fetch_url` MCP tool—the only egress path other than
`api.perplexity.ai`. Because the fetched URL is attacker-influenceable, the fetcher
applies SSRF + DoS controls mirroring the API client:

- **Scheme allowlist** — only `http` / `https`; `file://`, `gopher://`, `data:` etc.
  are rejected.
- **Private-address rejection** — the host is resolved and the fetch is refused if
  **any** resolved IP is private, loopback, link-local (incl. `169.254.169.254`
  cloud metadata), multicast, reserved, or unspecified. This is re-checked on **every
  redirect hop** (redirects are followed manually, not by httpx). Default-deny;
  override only with `PERPLEXITY_FETCH_ALLOW_PRIVATE=true`.
- **IP pinning (DNS-rebinding defense)** — the connection is made to the exact IP
  that passed validation: the request targets the validated address while the
  original hostname is sent as the `Host` header and TLS SNI (so certificate
  verification still checks the hostname). A resolver that flips public→private
  between validation and connect cannot change where the socket goes.
- **Size / time caps** — reuses `PERPLEXITY_TIMEOUT` and `PERPLEXITY_MAX_RESPONSE_BYTES`.
  The byte cap is enforced **while the body streams in**: an oversized
  `Content-Length` is rejected before any read, and a response that exceeds the cap
  mid-download is aborted, never fully buffered.
- **Text-only content types** — non-text bodies (PDFs, images, archives,
  `application/octet-stream` …) are refused up front; only `text/*`, HTML/XML, and
  JSON-ish types are fetched, so binaries are never decoded into the prompt.
- **Untrusted-output handling** — extracted page text is run through the same
  indirect-prompt-injection scan as `deep_research`; matches are surfaced to the user
  before the text is sent to Sonar.
- **Audit & rate limit** — the TUI's commands, page fetches, and background
  `/task` probes pass through one `RequestGuard` that charges the `TokenBucket`
  and writes a redacted audit record, so the interactive surface is metered and
  logged like the MCP tools.

**Residual risk:** with the connection pinned to the validated IP, the classic
DNS-rebinding TOCTOU window is closed. What remains is by design: the fetcher will
reach any *publicly routable* address an attacker-supplied URL points at. Operators
who fetch untrusted URLs should still run the TUI behind a filtering egress proxy.

The TUI's local SQLite store (history/tabs/Spaces) holds only the user's own
artifacts — no secrets. It is still browsing history, so the file is created
owner-only (0600, directory 0700), tabs are deduplicated per space+URL, and each
space retains only the most-recent tabs (default 50, configurable). Conversation
history is unbounded by default — it is never auto-deleted — but an operator can
cap it with `PERPLEXITY_MAX_HISTORY_PER_SPACE`.

Stored Agent response snapshots share this owner-only SQLite database. Each row is
scoped to the originating MCP client session; a known response ID owned by another
session is rejected before continuation or retrieval. Retention is bounded per
session (default 100 snapshots). Automatic function chaining is opt-in and can
invoke only server-operator-registered handlers. Function JSON
schemas, arguments, outputs, and continuation rounds are all bounded; arbitrary
Python supplied by an MCP caller is never evaluated.

## Secret handling

- The API key is stored as a `pydantic` `SecretStr`, kept out of `repr`/logs.
- Audit logging recursively redacts keys/tokens/secrets and scrubs inline
  `Bearer …` / `pplx-…` strings.
- `.env` is git-ignored; only `.env.example` (no secrets) is committed.

## Hardening recommendations for operators

- Prefer the default **stdio** transport. Only enable HTTP behind TLS + a strong,
  rotated bearer token, and behind a filtering egress proxy / DLP solution.
- Run under an OS sandbox (seccomp / AppArmor / SELinux) or a minimal container
  with no access to sensitive files or internal networks.
- Set `PERPLEXITY_AUDIT_LOG_PATH` and forward the JSON log to your SIEM.
- Keep dependencies current; review `pip-audit` output in CI.
- **Scale-out note:** the token-bucket rate limiter and audit logger are
  in-process by design — correct for the intended single-user, single-instance
  deployment. If you ever front the HTTP transport with multiple replicas, rate
  limiting needs a shared backend (e.g. Redis) and audit logs need centralized
  collection; neither is built in.

## Reporting a vulnerability

Please report security issues privately to the maintainer rather than opening a
public issue: open a confidential issue on the canonical Codeberg repo, or contact
the maintainer directly. Include reproduction steps and affected version. Please
allow reasonable time for a fix before public disclosure.
