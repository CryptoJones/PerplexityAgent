"""Async Perplexity API client.

A thin, hardened wrapper over Perplexity's Search and Sonar (chat completions)
endpoints. Implements per-request timeouts, a response-size cap, and capped
retries with jittered backoff for transient failures (NSA: constrain & sandbox,
DoS guard). The API key is held only here and injected as a header — never
returned to callers.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings

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
            raise exc_type(
                f"Response too large (>{max_bytes} bytes cap); rejected as a DoS guard."
            )
        chunks.append(chunk)
    return b"".join(chunks)


class PerplexityClient:
    """Async client for the Perplexity Search + Sonar APIs."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._breaker = CircuitBreaker()
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

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with retry/jitter and a streamed, hard response-size cap.

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
                    async with self._client.stream("POST", path, json=payload) as resp:
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
                    sleep_s = min(2 ** attempt, 8) * (0.5 + random.random() / 2)  # noqa: S311 - not crypto
                await asyncio.sleep(sleep_s)

            self._breaker.on_failure()  # transport errors exhausted retries → outage
            raise PerplexityError(
                f"Request to {path} failed after {self._settings.max_retries} retries: {last_exc}"
            )
        finally:
            self._breaker.release(is_probe)

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Stream the body, enforcing the size cap (shared with the page fetcher)."""
        return await read_capped(resp, self._settings.max_response_bytes, PerplexityError)

    async def search(
        self, query: str, max_results: int = 5, max_tokens_per_page: int = 1024
    ) -> dict[str, Any]:
        """Call the Search API (POST /search) for ranked web results."""
        payload = {
            "query": query,
            "max_results": max_results,
            "max_tokens_per_page": max_tokens_per_page,
        }
        return await self._post("/search", payload)

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
