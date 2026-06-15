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


class PerplexityClient:
    """Async client for the Perplexity Search + Sonar APIs."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout,
            headers={
                "Authorization": f"Bearer {settings.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
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
                            # Surface a concise error without leaking request headers.
                            text = body.decode("utf-8", "replace")[:500]
                            raise PerplexityError(
                                f"Perplexity API error {resp.status_code}: {text}"
                            )
                        data: dict[str, Any] = json.loads(body)
                        return data
            except httpx.RequestError as exc:  # network/timeout — transient
                last_exc = exc

            # Honor a server-supplied Retry-After; otherwise back off with full jitter.
            if retry_after is not None:
                sleep_s = retry_after
            else:
                sleep_s = min(2 ** attempt, 8) * (0.5 + random.random() / 2)  # noqa: S311 - not crypto
            await asyncio.sleep(sleep_s)

        raise PerplexityError(
            f"Request to {path} failed after {self._settings.max_retries} retries: {last_exc}"
        )

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Stream the body, enforcing ``max_response_bytes`` as bytes arrive."""
        cap = self._settings.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise PerplexityError(
                    f"Response too large (>{cap} bytes cap); rejected as a DoS guard."
                )
            chunks.append(chunk)
        return b"".join(chunks)

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
