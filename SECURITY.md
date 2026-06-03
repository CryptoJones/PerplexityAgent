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
| **Design for boundaries / least privilege** | Default **stdio** transport runs locally with no network exposure. No shell execution, no filesystem writes (except an optional, explicitly-configured audit-log path). Egress is only to `api.perplexity.ai`. The API key lives only in the client layer and is **never** returned by a tool. |
| **Validate parameters** | Every tool input is validated against a strict `pydantic` model (`schemas.py`) with bounded string lengths (≤ 4 KB), numeric ranges (`max_results` 1–20, `num_subquestions` 1–8), and a `model` enum. Unknown fields are rejected (`extra="forbid"`), preventing parameter smuggling. |
| **Constrain & sandbox tool execution** | Per-request timeouts, a hard response-size cap, and capped retries with jittered backoff (`client.py`). Run the process under seccomp/AppArmor/SELinux or in a container for OS-level isolation (see below). |
| **Sign & verify messages (transport)** | stdio mode is local-trusted. The optional HTTP transport **refuses to start without a bearer token**, binds to localhost by default, and rejects any request lacking the exact `Authorization: Bearer <token>` header. Terminate TLS at a reverse proxy in front of it. |
| **Filter & monitor outputs / chained execution** | Perplexity responses are treated as **untrusted input** to the next stage. `deep_research` scans retrieved snippets for indirect-prompt-injection patterns and surfaces them in `security_flags`. Citations are taken only from API metadata, never from model free-text. |
| **Instrument for logging & detection** | A structured JSON audit log (`security.py`) records every tool call and result with redacted parameters, a result hash, and validation status — suitable for SIEM ingestion. |
| **Track & patch vulnerabilities** | Pinned deps + `uv.lock`; CI runs `pip-audit` on every push. Enable Renovate/Dependabot on the GitHub mirror to automate updates. |
| **DoS / fatigue resistance** | A token-bucket rate limiter (`rate_per_minute` / `rate_burst`), bounded sub-question counts, input-size caps, and request timeouts. |
| **Access control / token security** | `PERPLEXITY_API_KEY` is loaded server-side from the environment only; the server fails fast if it is absent. No token passthrough. |

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

## Reporting a vulnerability

Please report security issues privately to the maintainer rather than opening a
public issue: open a confidential issue on the canonical Codeberg repo, or contact
the maintainer directly. Include reproduction steps and affected version. Please
allow reasonable time for a fix before public disclosure.
