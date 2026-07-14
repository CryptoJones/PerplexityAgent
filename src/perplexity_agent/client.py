"""Async Perplexity API client.

A thin, hardened wrapper over Perplexity's Search and Sonar (chat completions)
endpoints. Implements per-request timeouts, a response-size cap, and capped
retries with jittered backoff for transient failures (NSA: constrain & sandbox,
DoS guard). The API key is held only here and injected as a header — never
returned to callers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings
from .schemas import ResponsesResponse, ResponseStreamEvent

FunctionHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]

# Status codes worth retrying (transient): 429 + 5xx.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Ceiling on a server-supplied Retry-After so a hostile/buggy header can't stall us.
_MAX_RETRY_AFTER_S = 30.0


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` header (seconds form), capped; None if unusable."""
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:  # HTTP-date form — fall back to our own backoff
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)


class PerplexityError(RuntimeError):
    """Raised for non-retryable or exhausted-retry API failures."""


class CircuitOpenError(PerplexityError):
    """Raised when the circuit breaker is open and failing fast."""


@dataclass
class CircuitBreaker:
    """Trip after consecutive upstream outages; fail fast until it cools down.

    closed → (failures ≥ threshold) → open → (after ``recovery_time`` s) → a
    *single* half-open probe → closed on success / re-open on failure. While a
    probe is in flight, concurrent callers fast-fail instead of stampeding a
    recovering API. Only genuine outages (transport errors / retry-exhausted 5xx)
    count as failures — a plain 4xx is the upstream answering. Every transition
    is lock-guarded so it stays correct off the single event loop too; the lock
    is held only for the brief transition, never across the awaited request.
    """

    failure_threshold: int = 5
    recovery_time: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _probing: bool = field(default=False, init=False)
    _lock: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """Admit a request or raise ``CircuitOpenError``; returns True for the probe.

        A ``True`` MUST be paired with ``release(True)`` in a ``finally`` so the
        slot is freed even on an unexpected error — otherwise the breaker wedges.
        """
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at < self.recovery_time:
                raise CircuitOpenError("circuit open: Perplexity API failing, retry after cooldown")
            if self._probing:
                raise CircuitOpenError("circuit half-open: a probe is already in flight")
            self._probing = True
            return True

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()

    def release(self, is_probe: bool) -> None:
        """Free the half-open probe slot — a no-op unless this call held it."""
        if is_probe:
            with self._lock:
                self._probing = False


def canonical_url(url: str) -> str:
    """Normalize a URL for dedup: drop fragment + trailing slash, lowercase scheme/host.

    Path and query keep their case — they are case-sensitive on most servers, so
    lowercasing them would falsely merge distinct pages (and falsely pass/fail
    citation validation).
    """
    base = (url or "").split("#", 1)[0].rstrip("/")
    if not base:
        return ""
    parts = urlsplit(base)
    if not parts.scheme:
        return base.lower()  # not URL-shaped; keep the old whole-string fold
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate search results by canonical URL, preserving order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in results:
        key = canonical_url(item.get("url") or "")
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def message_content(chat_response: dict[str, Any]) -> str:
    """Extract the assistant message text from an OpenAI-compatible chat completion.

    The single home for the Sonar response-shape contract: both the MCP research
    pipeline and the TUI assistant call this instead of duplicating the
    ``choices[0].message.content`` walk (and its error message).
    """
    try:
        content = chat_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected Sonar response shape: {exc}") from exc
    return str(content or "")


def citation_urls(chat_response: dict[str, Any]) -> list[str]:
    """Best-effort citation URLs from a Sonar response, deduped by canonical URL.

    Uses the same :func:`canonical_url` as the search path, so a trailing-slash
    or host-case variant of one link can't appear twice in the Sources list.
    """
    raw: list[str] = []
    for key in ("citations", "search_results"):
        items = chat_response.get(key) or []
        for item in items:
            if isinstance(item, str):
                raw.append(item)
            elif isinstance(item, dict) and item.get("url"):
                raw.append(str(item["url"]))
    seen: set[str] = set()
    out: list[str] = []
    for url in raw:
        canon = canonical_url(url)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(url)
    return out


def response_output_text(response: dict[str, Any]) -> str:
    """Aggregate text blocks from an OpenAI Responses-compatible payload."""
    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                chunks.append(str(content.get("text") or ""))
    return "".join(chunks)


def parse_agent_response(response: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize an Agent API response into JSON-native values."""
    try:
        parsed = ResponsesResponse.model_validate(response)
    except ValueError as exc:
        raise PerplexityError(f"Unexpected Agent API response shape: {exc}") from exc
    return parsed.model_dump(mode="json", exclude_none=True)


async def read_capped(resp: httpx.Response, max_bytes: int, exc_type: type[Exception]) -> bytes:
    """Stream a response body, enforcing ``max_bytes`` as bytes arrive.

    Shared by the API client and the TUI page fetcher so the size-cap guard
    (a Content-Length pre-check plus a streamed abort) lives in exactly one
    place. ``exc_type`` lets each caller raise its own error
    (``PerplexityError`` / ``FetchError``).
    """
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise exc_type(
            f"Response too large (Content-Length {declared} bytes > {max_bytes} cap); "
            "rejected as a DoS guard."
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise exc_type(f"Response too large (>{max_bytes} bytes cap); rejected as a DoS guard.")
        chunks.append(chunk)
    return b"".join(chunks)


class PerplexityClient:
    """Async client for the Perplexity Search + Sonar APIs."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._breaker = CircuitBreaker()
        self._finance_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout,
            headers={
                "Authorization": f"Bearer {settings.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            # Explicit, bounded pool so a burst can't exhaust FDs / memory.
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PerplexityClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Request with retry/jitter and a streamed, hard response-size cap.

        The body is read incrementally and aborted the moment it crosses
        ``max_response_bytes`` — a hostile or misconfigured upstream can't buffer
        a huge body into memory first (NSA: constrain & sandbox, DoS guard).
        """
        is_probe = self._breaker.acquire()  # fail fast if the API is known-down
        try:
            last_exc: Exception | None = None
            for attempt in range(self._settings.max_retries + 1):
                retry_after: float | None = None
                try:
                    async with self._client.stream(method, path, json=payload) as resp:
                        if (
                            resp.status_code in _RETRYABLE_STATUS
                            and attempt < self._settings.max_retries
                        ):
                            last_exc = PerplexityError(f"Transient HTTP {resp.status_code}")
                            retry_after = _retry_after_seconds(resp)
                        else:
                            body = await self._read_capped(resp)
                            if resp.status_code >= 400:
                                # A retryable status with retries spent is an upstream
                                # outage; a plain 4xx is the upstream answering, so it
                                # must not trip the breaker.
                                if resp.status_code in _RETRYABLE_STATUS:
                                    self._breaker.on_failure()
                                text = body.decode("utf-8", "replace")[:500]
                                raise PerplexityError(
                                    f"Perplexity API error {resp.status_code}: {text}"
                                )
                            data: dict[str, Any] = json.loads(body)
                            self._breaker.on_success()
                            return data
                except httpx.RequestError as exc:  # network/timeout — transient
                    last_exc = exc

                # Honor a server-supplied Retry-After; otherwise back off with full jitter.
                if retry_after is not None:
                    sleep_s = retry_after
                else:
                    sleep_s = min(2**attempt, 8) * (0.5 + random.random() / 2)  # noqa: S311 - not crypto
                await asyncio.sleep(sleep_s)

            self._breaker.on_failure()  # transport errors exhausted retries → outage
            raise PerplexityError(
                f"Request to {path} failed after {self._settings.max_retries} retries: {last_exc}"
            )
        finally:
            self._breaker.release(is_probe)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, payload)

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Stream the body, enforcing the size cap (shared with the page fetcher)."""
        return await read_capped(resp, self._settings.max_response_bytes, PerplexityError)

    async def search(
        self,
        query: str,
        max_results: int = 5,
        max_tokens_per_page: int = 1024,
        *,
        search_type: str | None = None,
        search_domain_filter: list[str] | None = None,
        search_language_filter: list[str] | None = None,
        search_recency_filter: str | None = None,
        search_after_date_filter: str | None = None,
        search_before_date_filter: str | None = None,
        last_updated_after_filter: str | None = None,
        last_updated_before_filter: str | None = None,
    ) -> dict[str, Any]:
        """Call the Search API (POST /search) for ranked web results."""
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "max_tokens_per_page": max_tokens_per_page,
        }
        optional = {
            "search_type": search_type,
            "search_domain_filter": search_domain_filter,
            "search_language_filter": search_language_filter,
            "search_recency_filter": search_recency_filter,
            "search_after_date_filter": search_after_date_filter,
            "search_before_date_filter": search_before_date_filter,
            "last_updated_after_filter": last_updated_after_filter,
            "last_updated_before_filter": last_updated_before_filter,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return await self._post("/search", payload)

    async def people_search(
        self, query: str, max_results: int = 5, max_tokens_per_page: int = 1024
    ) -> dict[str, Any]:
        """Search Perplexity's structured people index."""
        return await self.search(
            query,
            max_results,
            max_tokens_per_page,
            search_type="people",
        )

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a non-streaming OpenAI-compatible Agent API response."""
        body = dict(payload)
        body.pop("stream", None)
        raw = await self._post("/v1/agent", body)
        return parse_agent_response(raw)

    async def retrieve_response(self, response_id: str) -> dict[str, Any]:
        """Retrieve a stored Agent API response snapshot by ID."""
        raw = await self._get(f"/v1/agent/{response_id}")
        return parse_agent_response(raw)

    async def run_response_with_tools(
        self,
        payload: dict[str, Any],
        registry: Mapping[str, FunctionHandler],
        *,
        max_rounds: int = 8,
    ) -> dict[str, Any]:
        """Create a response and execute allowlisted function calls until complete.

        Only handlers explicitly supplied by the server operator can run. Arguments
        must decode to a JSON object, each call result is size-bounded, and the
        number of continuation rounds is capped to prevent runaway agent loops.
        """
        current = dict(payload)
        for _round in range(max_rounds + 1):
            response = await self.create_response(current)
            calls = [item for item in response["output"] if item.get("type") == "function_call"]
            if not calls:
                return response
            if _round >= max_rounds:
                raise PerplexityError(f"Function-call chain exceeded {max_rounds} rounds")

            outputs: list[dict[str, str]] = []
            for call in calls:
                name = str(call.get("name") or "")
                handler = registry.get(name)
                if handler is None:
                    raise PerplexityError(f"Function {name!r} is not registered")
                try:
                    arguments = json.loads(str(call.get("arguments") or "{}"))
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must decode to a JSON object")
                    value = handler(arguments)
                    if inspect.isawaitable(value):
                        value = await value
                    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
                except Exception as exc:
                    rendered = json.dumps(
                        {"error": type(exc).__name__, "message": str(exc)[:1000]},
                        sort_keys=True,
                    )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call["call_id"]),
                        "output": rendered[:16_384],
                    }
                )

            current = {
                key: value
                for key, value in payload.items()
                if key not in {"input", "previous_response_id", "store", "stream"}
            }
            current["input"] = outputs
            current["previous_response_id"] = response["id"]
        raise AssertionError("unreachable")

    async def search_finance(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        tickers: list[str] | None = None,
        model: str = "perplexity/sonar",
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Run an Agent response with Perplexity's finance tool enabled."""
        cache_key = (
            query,
            tuple(categories or []),
            tuple(tickers or []),
            model,
            max_output_tokens,
        )
        ttl = self._settings.finance_cache_ttl_s
        cached = self._finance_cache.get(cache_key)
        if ttl and cached and time.monotonic() - cached[0] < ttl:
            return dict(cached[1])
        hints: list[str] = []
        if categories:
            hints.append(f"Requested categories: {', '.join(categories)}.")
        if tickers:
            hints.append(f"Focus tickers: {', '.join(tickers)}.")
        payload: dict[str, Any] = {
            "input": " ".join([query, *hints]),
            "model": model,
            "tools": [{"type": "finance_search"}],
        }
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        response = await self.create_response(payload)
        if ttl:
            if len(self._finance_cache) >= 128:
                oldest = min(self._finance_cache, key=lambda key: self._finance_cache[key][0])
                self._finance_cache.pop(oldest, None)
            self._finance_cache[cache_key] = (time.monotonic(), response)
        return response

    async def fetch_url(self, url: str, *, model: str = "perplexity/sonar") -> dict[str, Any]:
        """Ask the Agent API to fetch a known URL with its built-in fetch tool."""
        return await self.create_response(
            {
                "input": f"Fetch and return the content at {url}",
                "model": model,
                "tools": [{"type": "fetch_url"}],
            }
        )

    async def send_multipart(
        self,
        input: list[dict[str, Any]],
        *,
        model: str,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send message content containing text and image parts to the Agent API."""
        payload: dict[str, Any] = {"input": input, "model": model}
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        return await self.create_response(payload)

    async def stream_response(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed SSE events from the Agent API with a cumulative size cap.

        FastMCP tool calls are request/response rather than streaming transports,
        so the server collects these events into its bounded result envelope. The
        client API remains an async iterator for callers that can consume deltas.
        """
        is_probe = self._breaker.acquire()
        body = dict(payload)
        body["stream"] = True
        total = 0
        try:
            async with self._client.stream("POST", "/v1/agent", json=body) as resp:
                if resp.status_code >= 400:
                    error_body = await self._read_capped(resp)
                    if resp.status_code in _RETRYABLE_STATUS:
                        self._breaker.on_failure()
                    detail = error_body.decode("utf-8", "replace")[:500]
                    raise PerplexityError(f"Perplexity API error {resp.status_code}: {detail}")
                declared = resp.headers.get("content-length")
                if (
                    declared
                    and declared.isdigit()
                    and int(declared) > self._settings.max_response_bytes
                ):
                    raise PerplexityError("Streaming response exceeds configured size cap")
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    total += len(line.encode("utf-8")) + 1
                    if total > self._settings.max_response_bytes:
                        raise PerplexityError("Streaming response exceeds configured size cap")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        continue
                    if line or not data_lines:
                        continue
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise PerplexityError(f"Malformed Agent API SSE event: {exc}") from exc
                    if not isinstance(event, dict):
                        raise PerplexityError("Malformed Agent API SSE event: expected object")
                    try:
                        parsed = ResponseStreamEvent.model_validate(event)
                    except ValueError as exc:
                        raise PerplexityError(f"Malformed Agent API SSE event: {exc}") from exc
                    yield parsed.model_dump(mode="json", exclude_none=True)
                self._breaker.on_success()
        except httpx.RequestError:
            self._breaker.on_failure()
            raise
        finally:
            self._breaker.release(is_probe)

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str = "sonar",
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the Sonar chat completions API (OpenAI-compatible)."""
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            payload["response_format"] = response_format
        return await self._post("/chat/completions", payload)
