"""SSRF-hardened URL fetcher for the interactive TUI.

This is the only egress path in the project other than ``api.perplexity.ai`` and
it is reachable **solely from the interactive TUI**, never from an MCP tool. It
reuses the DoS guards already in ``client.py`` (per-request timeout, and the shared
``enforce_size_cap`` response-size guard) and adds the controls a fetch of
*attacker-influenceable* URLs needs:

- scheme allowlist (``http`` / ``https`` only) — no ``file://``, ``gopher://`` …;
- DNS resolution + rejection of private / loopback / link-local / reserved IPs
  (NSA: constrain & sandbox) so a URL can't be used to reach internal services;
- the same check on **every redirect hop** (redirects are followed manually);
- a hard byte cap enforced while streaming the body;
- extracted page text is treated as *untrusted input* and flagged for indirect
  prompt injection (``scan_for_injection``) before it is ever shown to Sonar.

Residual risk: a classic DNS-rebinding TOCTOU window exists between validation and
connect. It is documented in ``SECURITY.md``; ``fetch_allow_private`` stays ``False``
by default so the blast radius is "public internet only".
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .security import enforce_size_cap, scan_for_injection

# Only these schemes may be fetched. Anything else (file, ftp, gopher, data …) is
# rejected outright.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cap on extracted *text* handed downstream (~80 KB of characters), independent of
# the raw-byte cap. Bounds the prompt we build for Sonar.
_MAX_TEXT_CHARS = 80_000

# How many redirects we will follow before giving up.
_MAX_REDIRECTS = 5


class FetchError(RuntimeError):
    """Raised when a URL is unsafe to fetch or the fetch fails."""


@dataclass
class FetchedPage:
    """The readable result of fetching one URL."""

    requested_url: str
    final_url: str
    title: str
    text: str
    fetched_bytes: int
    injection_flags: list[str] = field(default_factory=list)


def _is_public_ip(raw: str) -> bool:
    """True only for a global-scope (publicly routable) IP address."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # is_global is the positive test; the explicit checks below are belt-and-braces
    # for address classes some Python versions don't fold into is_global.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


async def _assert_host_allowed(host: str, *, allow_private: bool) -> None:
    """Resolve ``host`` and raise unless every resolved IP is allowed.

    ``getaddrinfo`` is a blocking syscall, so it runs in a worker thread to keep
    the (single) event loop the TUI shares with all fetches and monitor tasks
    responsive even when a nameserver is slow.
    """
    if not host:
        raise FetchError("URL has no host.")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host {host!r}: {exc}") from exc

    addrs = {str(info[4][0]) for info in infos}
    if not addrs:
        raise FetchError(f"Host {host!r} resolved to no addresses.")
    if allow_private:
        return
    for addr in addrs:
        if not _is_public_ip(addr):
            raise FetchError(
                f"Refusing to fetch {host!r}: resolves to non-public address "
                f"{addr} (SSRF guard). Set PERPLEXITY_FETCH_ALLOW_PRIVATE=true to override."
            )


async def _validate_url(url: str, *, allow_private: bool) -> str:
    """Validate scheme + host of ``url``; return the normalized URL."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchError(
            f"Refusing to fetch scheme {parts.scheme!r}; only http/https are allowed."
        )
    await _assert_host_allowed(parts.hostname or "", allow_private=allow_private)
    return url


def extract_text(html: str) -> tuple[str, str]:
    """Return ``(title, readable_text)`` from raw HTML.

    Strips script/style/noscript and collapses whitespace. Uses ``selectolax`` when
    available, falling back to a crude tag-stripper so the TUI still works if the
    optional parser isn't installed.
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:  # pragma: no cover - exercised only without the tui extra
        return _extract_text_fallback(html)

    tree = HTMLParser(html)
    title = ""
    title_node = tree.css_first("title")
    if title_node is not None:
        title = (title_node.text() or "").strip()
    for tag in tree.css("script, style, noscript, template"):
        tag.decompose()
    body = tree.body or tree.root
    text = body.text(separator=" ", strip=True) if body else ""
    text = " ".join(text.split())
    return title, text[:_MAX_TEXT_CHARS]


def _extract_text_fallback(html: str) -> tuple[str, str]:
    import re

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = (title_match.group(1).strip() if title_match else "")
    stripped = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    text = " ".join(stripped.split())
    return title, text[:_MAX_TEXT_CHARS]


class PageFetcher:
    """Fetch and clean web pages for the TUI, with SSRF + DoS guards."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        # follow_redirects=False: we resolve and re-validate each hop ourselves so a
        # redirect can't bounce us to an internal address after the first check.
        self._client = client or httpx.AsyncClient(
            timeout=settings.timeout,
            follow_redirects=False,
            headers={"User-Agent": settings.fetch_user_agent},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PageFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch(self, url: str) -> FetchedPage:
        """Fetch ``url`` (following safe redirects) and return cleaned page text."""
        allow_private = self._settings.fetch_allow_private
        requested = url
        current = await _validate_url(url, allow_private=allow_private)

        # Stream so the byte cap can abort an oversized body *before* it is fully
        # buffered, and so redirect responses never download their body at all.
        for _ in range(_MAX_REDIRECTS + 1):
            async with self._client.stream("GET", current) as resp:
                if resp.is_redirect:
                    if not resp.has_redirect_location:
                        raise FetchError(
                            f"Redirect (HTTP {resp.status_code}) with no Location header "
                            f"fetching {current!r}."
                        )
                    # Re-validate the redirect target before following it.
                    current = str(resp.url.join(resp.headers["location"]))
                    await _validate_url(current, allow_private=allow_private)
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"Fetch failed: HTTP {resp.status_code} for {current!r}.")

                body = await self._read_capped(resp)
                encoding = resp.charset_encoding or "utf-8"
                html = body.decode(encoding, errors="replace")
                title, text = extract_text(html)
                flags = scan_for_injection(f"{title} {text}")
                return FetchedPage(
                    requested_url=requested,
                    final_url=str(resp.url),
                    title=title,
                    text=text,
                    fetched_bytes=len(body),
                    injection_flags=flags,
                )

        raise FetchError(f"Too many redirects (>{_MAX_REDIRECTS}) fetching {requested!r}.")

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Read the streamed body, aborting as soon as it exceeds the byte cap."""
        cap = self._settings.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            enforce_size_cap(total, cap, FetchError)
            chunks.append(chunk)
        return b"".join(chunks)
