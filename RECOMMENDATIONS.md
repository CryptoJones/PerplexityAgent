# Recommendations: Feature Gap Analysis

## Context

PerplexityAgent (v0.2.9) implements the core MCP server for Perplexity's Search and Sonar APIs. It supports three tools:

- `perplexity_search` — ranked web results
- `sonar_ask` — grounded chat answers (OpenAI-compatible)
- `deep_research` — multi-step pipeline with citation validation

**Current API version matched**: Perplexity's legacy endpoint (`/api/search`, `/api/sonar`) with limited parameters.

**Date**: 2026-07-13

---

## Feature Gaps

The following features from Perplexity AI's current API are **not implemented**. Implementing them will bring the agent up to date with the provider's latest capabilities.

### 1. OpenAI-Compatible Responses API

**Endpoint**: `POST /v1/agent` (or OpenAI's `/v1/chat/completions` with responses semantics)

Perplexity has moved to a new API shape that mirrors OpenAI's Responses API. This enables richer multi-turn sessions, persistent state, and advanced tool handling.

**Required changes**:
- Add a new client method `create_response()` that calls the new endpoint.
- Parse the returned `ResponsesResponse` (or equivalent streaming events).
- Expose a new MCP tool `responses_create` (or unify under a different name) that accepts the same request schema as the API.
- Handle streaming via SSE events: `response.created`, `response.in_progress`, `response.completed`, and per-item deltas.

**Schema notes**:
- Request body must include `input` (string or array of `InputItem`), optional `model`, `reasoning`, `response_format`, `tools`, `previous_response_id`, `store`, and `max_output_tokens`.
- For Anthropic models, `max_output_tokens` is required.
- Response contains `output` array with items: `MessageOutputItem` (text messages), `SearchResultsOutputItem`, and tool outputs.

**Priority**: **High** — this is the primary API surface; existing tools are just thin wrappers.

---

### 2. New Search Tools

The API now offers specialized search capabilities beyond generic web search.

#### 2.1 `finance_search`

Searches financial data (stock quotes, market data, etc.) with structured result categories.

**Parameters**:
- `categories` — list of finance categories to request (e.g., `"quote"`).
- `tickers` — optional ticker symbols to focus on.

**Output**: `FinanceResultsOutputItem` containing a list of `FinanceResult` objects with `category`, `content`, `tickers`, and `sources`.

**Implementation notes**:
- Add a new client method `search_finance(categories: List[str], tickers: Optional[List[str]] = None)`.
- Create MCP tool `finance_search` that maps to this method.
- Consider caching results per category+ticker combination.

**Priority**: **Medium** — valuable for financial queries but not core to the agent's mission.

#### 2.2 `people_search`

Search for people/individuals with structured profiles.

**Parameters**:
- `query` — the search string.

**Output**: `PeopleSearchResultsOutputItem` containing `results` (each a `SearchResult` with date, title, snippet, url) and the `queries` the agent generated.

**Implementation notes**:
- Add client method `people_search(query: str)`.
- Expose MCP tool `people_search`.

**Priority**: **Low** — niche use case.

#### 2.3 `fetch_url` (tool-level URL fetch)

A dedicated tool that fetches a URL's content into the response, distinct from the SSRF-guarded page fetcher used only by the TUI.

**Parameters**:
- `url` — the URL to fetch.
- `max_urls` — optional cap (default 1).

**Output**: `FetchUrlResultsOutputItem` with `contents` (list of `UrlContent` objects: `url`, `title`, `snippet`).

**Implementation notes**:
- Reuse the existing fetch logic from `fetch.py` (SSRF guards already present).
- Add client method `fetch_url(url: str)`.
- Create MCP tool `fetch_url` that calls this method.

**Priority**: **High** — mirrors a major Perplexity feature and is needed for the full agent API.

---

### 3. Reasoning / Advanced Search Configuration

The legacy `sonar_ask` tool only accepts a `model` field (`"sonar"` or `"sonar-pro"`). The new API introduces explicit control over model effort and reasoning parameters.

**New fields**:
- `reasoning` — object with `effort` (`"low"`, `"medium"`, `"high"`, `"xhigh"`, or `"max"`).
- `response_format` — optional JSON schema for constrained output.
- `model` — provider/model identifier (e.g., `"openai/gpt-5"`, `"anthropic/claude-sonnet-4-6"`).

**Implementation notes**:
- Extend `schemas.py` to define a `ReasoningConfig` type and `ResponseFormat`.
- Update `sonar_ask` to accept these optional parameters.
- Map the new `model` values to the appropriate Perplexity endpoint; note that Perplexity's own models may be addressed differently (e.g., `perplexity/sonar-*`).

**Priority**: **Medium** — important for tuning model behavior but can remain optional.

---

### 4. Multipart Input (Messages + Images)

Perplexity's API now accepts an `input` array that can include image parts (`InputImage`) alongside text messages. This is useful for multimodal queries.

**Schema**:
```json
{
  "input": [
    { "type": "message", "role": "user", "content": [ { "type": "input_text", "text": "…" } ] },
    { "type": "image_url", "image_url": { "url": "data:image/png;base64,…" } }
  ]
}
```

**Implementation notes**:
- Extend `schemas.py` with an `InputItem` union (`InputText`, `InputImage`).
- Add client method `send_multipart(input: List[InputItem])`.
- Create MCP tool `chat` (or keep `sonar_ask` and add this as a new `multimodal` tool).

**Priority**: **Medium** — enables new use cases but not strictly required for basic text queries.

---

### 5. Tool Call Chaining (Function Calls)

The `responses_create` endpoint supports invoking arbitrary functions via `FunctionTool` and handling their outputs (`FunctionCallOutput`). This allows the model to call tools, process results, and continue the conversation.

**Relevant schemas**:
- `FunctionTool` — name, description, JSON Schema parameters.
- `FunctionCallInput` — call ID, function name, arguments, `thought_signature`.
- `FunctionCallOutputInput` — output with call ID.

**Implementation notes**:
- Add support for registering external Python functions as MCP tools.
- When the model returns a `FunctionCallOutput` item, route it to the corresponding external function and inject the result back into the `input` for the next turn.
- This is the most complex addition; it requires a new execution context and careful error handling.

**Priority**: **Low** — advanced feature, mainly for agentic workflows.

---

### 6. Persistent Responses (`store` parameter)

The API introduces a `store` flag that persists responses so they can be referenced later via `previous_response_id`. This enables multi-session continuity.

**Implementation notes**:
- Store responses in a database (new `store.db` under the TUI's path, or a separate table if using the main SQLite store).
- When `store=true`, generate a stable `id` and persist the full response payload.
- On `responses_create` with a `previous_response_id`, validate that the referenced response exists and belongs to the same session.

**Priority**: **Medium** — mostly for TUI continuity; MCP clients can manage their own session state.

---

### 7. Advanced Web Search Filters

The `WebSearchTool` supports filtering results by domain and date ranges, plus a recency filter.

**Parameters**:
- `filters` — object containing domain allowlist, `last_updated_after`, `last_updated_before`, and a `search_recency_filter` (`"hour"`, `"day"`, `"week"`, etc.).

**Implementation notes**:
- Extend `schemas.py` with `WebSearchFilters`.
- Pass these filters to the Perplexity search endpoint.

**Priority**: **Low** — useful but not essential.

---

## Summary Table

| Feature | Endpoint / Schema | Priority | Notes |
|---------|-------------------|----------|-------|
| OpenAI Responses API | `POST /v1/agent`, `ResponsesRequest` | High | Replace current tools; core API shift |
| `finance_search` | `FinanceSearchTool` | Medium | Financial data, structured results |
| `people_search` | `PeopleSearchTool` | Low | Niche use case |
| `fetch_url` (tool) | `FetchUrlTool` | High | Needed for full agent API |
| Reasoning config | `ReasoningConfig` | Medium | Effort levels, model identifiers |
| Multipart input | `InputItem` union | Medium | Images + text |
| Tool chaining (functions) | `FunctionTool` | Low | Complex; optional |
| Persistent storage | `store` bool | Medium | Database changes |
| Search date/domain filters | `WebSearchFilters` | Low | Nice-to-have |

---

## Implementation Order Suggestion

1. **OpenAI Responses API** (the foundation) — refactor or layer atop existing client.
2. **`fetch_url` tool** — reuses existing SSRF guards.
3. **Reasoning config** — relatively straightforward schema additions.
4. **`finance_search`** and **`people_search`** — new endpoints, similar pattern.
5. **Multipart input** — schema changes, no new endpoints.
6. **Persistent storage** — add DB table, update tool handlers.
7. **Search filters** — add fields to request schema.
8. **Tool chaining** — tackle last; highest risk.

---

## Implementation Status

All recommendations are implemented on `feature/agent-api-recommendations`:

- [x] Responses-compatible Agent API client and MCP tool, typed response parsing,
  and bounded SSE event parsing/collection.
- [x] Finance and people search clients/tools, plus opt-in bounded finance caching.
- [x] SSRF-hardened single/multi-URL MCP fetch with an enforced `max_urls` cap.
- [x] Reasoning, structured output, provider models, and Anthropic token validation.
- [x] Multipart text/image input schemas and client support.
- [x] Function schemas, client-driven outputs, allowlisted Python registration,
  automatic bounded chaining, and direct MCP invocation of registered handlers.
- [x] Upstream response continuity plus session-scoped SQLite snapshots and retrieval.
- [x] Domain, language, recency, publication-date, and last-updated search filters.

The implementation follows the provider's current contract where this planning
artifact used tentative language: finance and fetch are Agent API built-in tools,
and Perplexity—not this server—generates canonical response IDs. The local database
stores the returned response snapshot and session ownership metadata.

**Status**: Implemented and covered by acceptance tests.
