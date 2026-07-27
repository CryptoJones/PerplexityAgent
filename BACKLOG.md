# Code Review Backlog

This document captures findings from a comprehensive code review of the PerplexityAgent
codebase. Each finding is also tracked as a GitHub Issue. Issues are grouped by priority.

## Summary

- **182 tests pass**, **89.19% coverage** (gate: 85%), **ruff clean**, **mypy strict clean**
- **0 critical vulnerabilities** found
- **3 HIGH**, **5 MEDIUM**, **5 LOW** priority items

---

## HIGH Priority

### H1: Audit log entries lack timestamps

**File:** `src/perplexity_agent/security.py`, `AuditLogger.record()`

The `AuditLogger` uses `logging.Formatter("%(message)s")` which outputs only the JSON
message — no timestamp. The payload `{"event": event, **fields}` also doesn't include
a timestamp. Audit logs without timestamps are of limited forensic value.

**Fix:** Add a timestamp to the payload in `record()`:
```python
payload = {"event": event, "ts": time.time(), **{k: redact(v) for k, v in fields.items()}}
```

### H2: Validation checks after rate-limit guard waste tokens

**File:** `src/perplexity_agent/server.py`, `responses_create` tool (lines 262–274)

The `_guard()` call (which charges a rate-limit token and emits an audit record) happens
at line 262, but two validation checks happen *after* it:
- Line 273: `if args.stream and auto_execute_functions: raise ValueError(...)`
- Line 276: `if not function_registry: raise PerplexityError(...)`

A client sending `stream=true` with `auto_execute_functions=true` will consume a
rate-limit token and generate an audit record before the request is rejected. These
checks should be moved **before** the `_guard()` call, alongside the existing
`max_function_rounds` validation (which is correctly placed before the guard at line 260).

### H3: `retrieve` tool doesn't session-scope offloaded values

**File:** `src/perplexity_agent/server.py`, `retrieve` tool (line 545)

The `retrieve` tool doesn't take a `ctx: Context` parameter and doesn't check session
ownership of the offloaded value. The `OffloadStore` is created once in `build_server()`
and shared across all MCP sessions. Any client that can guess or obtain a `retrieve_key`
(a 24-char SHA-256 prefix) can retrieve any offloaded value from any session.

While the keys are content hashes (not easily guessable), this is still an
information-leak vector in a multi-tenant deployment. The `responses_retrieve` tool
correctly checks `response_owner` — the `retrieve` tool should do the same by
session-scoping the offload store.

---

## MEDIUM Priority

### M1: `fetch_url` tool fails entirely if one URL in a batch fails

**File:** `src/perplexity_agent/server.py`, `fetch_url` tool (line 426)

`asyncio.gather` without `return_exceptions=True` means if one URL fails (e.g.,
non-text content-type, SSRF rejection), the entire tool call fails and all results
are lost. For a multi-URL fetch, partial results with per-URL error information
would be more useful.

### M2: `fetch_user_agent` version mismatch

**File:** `src/perplexity_agent/config.py`, line 65

The default User-Agent string contains `PerplexityAgent-TUI/0.2`, but the project
is at version `0.3.0`. This should be updated to `0.3` or derived from `__version__`.

### M3: `retrieve` and `server_metrics` tools lack `ctx: Context` parameter

**File:** `src/perplexity_agent/server.py`, lines 545 and 566

All other tools take `ctx: Context` as their first parameter. The `retrieve` and
`server_metrics` tools don't — they use closure variables instead. This works
functionally but is inconsistent and prevents them from accessing lifespan context
or session information.

### M4: Stale comment in `assistant.py`

**File:** `src/perplexity_agent/assistant.py`, line 221

The comment says `_content()` raises a ValueError, but the function is actually
`message_content()`. This is a minor documentation inaccuracy.

### M5: `tasks.py` uses `asyncio.ensure_future` instead of `asyncio.create_task`

**File:** `src/perplexity_agent/tasks.py`, line 72

`asyncio.ensure_future` is the older API; `asyncio.create_task` is preferred in
Python 3.11+. Both work, but `create_task` is more explicit about creating a task
on the running loop.

---

## LOW Priority

### L1: `AuditLogger` handler management on module-level logger

**File:** `src/perplexity_agent/security.py`, `AuditLogger.__init__`

The `AuditLogger` adds handlers to a module-level `logger` object. If multiple
instances are created (e.g., one for the server and one for the TUI in the same
process), only the first adds the stderr handler, but each adds its own file
handler. This could lead to duplicate file handlers for the same path. In practice,
only one instance is created per process, so this is low-risk.

### L2: `server.py` — `responses_create` tool description doesn't document server-level parameters

The `auto_execute_functions` and `max_function_rounds` parameters are server-level
controls, not part of the Perplexity API request. They're not documented in the tool's
docstring, which could confuse MCP clients. The docstring should mention these
parameters and their purpose.

### L3: `memory.py` — No `check_same_thread=False` on sqlite3 connection

The `Store` class uses `sqlite3.connect()` without `check_same_thread=False`. In the
current codebase, the store is only accessed from the main thread (TUI) or the server's
event loop, so this is fine. But if the code is ever used in a multi-threaded context,
this would raise `ProgrammingError`. Adding `check_same_thread=False` with a comment
would future-proof it.

### L4: `schemas.py` — `InputImage.image_url` not validated as a URL

The `image_url` field is typed as `str` with `min_length=1, max_length=8*1024*1024`
but is not validated as a URL (unlike `FetchUrlInput.urls` which uses `HttpUrl`).
A user could pass an arbitrary string. The Perplexity API will validate it, but
early rejection would be more consistent with the project's "validate parameters"
principle.

### L5: `server.py` — `deep_research` accesses `result["validation_report"]["passed"]` without defensive access

Line 538: `validation_passed=result["validation_report"]["passed"]`. While `research.py`
always returns a `validation_report` key, using `.get()` with a default would be more
defensive against future changes.

---

## Testing Gaps

The following untested paths were identified during the review:

1. **`__main__.py` (52% coverage):** HTTP transport, TUI dispatch, graceful shutdown
2. **`server.py` streaming path (lines 288–299):** `responses_create` tool's streaming branch
3. **`client.py` finance cache eviction:** Cache eviction logic (lines 468–470)
4. **`security.py` audit truncation:** `_MAX_AUDIT_BYTES` truncation path
5. **`efficiency.py` `OffloadStore` eviction:** FIFO eviction when `max_entries` exceeded
6. **`fetch.py` fallback text extraction:** `_extract_text_fallback` regex path
7. **No end-to-end integration test** for the full `deep_research` pipeline
