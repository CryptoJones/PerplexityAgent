"""SSRF-hardened URL fetcher for the interactive TUI.

This is the only egress path in the project other than ``api.perplexity.ai`` and
it is reachable **solely from the interactive TUI**, never from an MCP tool. It
mirrors the DoS guards already in ``client.py`` (per-request timeout, response-size
cap) and adds the controls a fetch of *attacker-influenceable* URLs needs:

- scheme allowlist (``http`` / ``https`` only) — no ``file://``, ``gopher://`` …;
- DNS resolution + rejection of private / loopback / link-local / reserved IPs
  (NSA: constrain & sandbox) so a URL can't be used to reach internal services;
- the same check on **every redirect hop** (redirects are followed manually);
- the connection is **pinned to the validated IP** (the request goes to the IP,
  with the original hostname sent as the ``Host`` header and TLS SNI) so a
  DNS-rebinding flip between validation and connect cannot reach a different
  address than the one that was checked;
- a hard byte cap enforced while the body streams in — oversized responses are
  aborted mid-download, never fully buffered;
- extracted page text is treated as *untrusted input* and flagged for indirect
  prompt injection (``scan_for_injection``) before it is ever shown to Sonar.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings
from .security import scan_for_injection

# Only these schemes may be fetched. Anything else (file, ftp, gopher, data …) is
# rejected outright.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cap on extracted *text* handed downstream (~80 KB of characters), independent of
# the raw-byte cap. Bounds the prompt we build for Sonar.
_MAX_TEXT_CHARS = 80_000

# How many redirects we will follow before giving up.
_MAX_REDIRECTS = 5

# Non-text bodies (PDFs, images, archives …) would only decode to garbage and waste
# the prompt budget; refuse them up front. A missing Content-Type is allowed —
# plenty of legitimate servers omit it.
_TEXTUAL_MIME_EXACT = frozenset({"application/xml", "application/json", "application/xhtml+xml"})


def _is_textual_mime(mime: str) -> bool:
    return (
        mime.startswith("text/")
        or mime in _TEXTUAL_MIME_EXACT
        or mime.endswith(("+xml", "+json"))
    )


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


def _assert_host_allowed(host: str, *, allow_private: bool) -> str:
    """Resolve ``host``, raise unless every resolved IP is allowed, return one.

    The returned address is the one the connection will be pinned to, so the IP
    that was validated is exactly the IP that gets dialed (no re-resolution
    window for DNS rebinding).
    """
    if not host:
        raise FetchError("URL has no host.")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host {host!r}: {exc}") from exc

    # Ordered dedupe: getaddrinfo sorts by RFC 6724 preference; pin the first.
    addrs = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addrs:
        raise FetchError(f"Host {host!r} resolved to no addresses.")
    if not allow_private:
        for addr in addrs:
            if not _is_public_ip(addr):
                raise FetchError(
                    f"Refusing to fetch {host!r}: resolves to non-public address "
                    f"{addr} (SSRF guard). Set PERPLEXITY_FETCH_ALLOW_PRIVATE=true to override."
                )
    return addrs[0]


def _validate_url(url: str, *, allow_private: bool) -> str:
    """Validate scheme + host of ``url``; return the validated IP to pin to."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchError(
            f"Refusing to fetch scheme {parts.scheme!r}; only http/https are allowed."
        )
    return _assert_host_allowed(parts.hostname or "", allow_private=allow_private)


def _pin_to_ip(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Rewrite ``url`` to dial ``ip`` while presenting the original hostname.

    Returns ``(pinned_url, headers, extensions)``: the URL with the host replaced
    by the validated IP, a ``Host`` header carrying the original hostname, and the
    ``sni_hostname`` extension so TLS handshakes (and certificate verification)
    still use the hostname.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    ip_netloc = f"[{ip}]" if ":" in ip else ip
    host_header = host
    if parts.port is not None:
        ip_netloc += f":{parts.port}"
        host_header += f":{parts.port}"
    pinned = urlunsplit((parts.scheme, ip_netloc, parts.path, parts.query, ""))
    return pinned, {"Host": host_header}, {"sni_hostname": host}


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
        current = url

        for _ in range(_MAX_REDIRECTS + 1):
            # Validate every hop, then pin the connection to the validated IP.
            ip = _validate_url(current, allow_private=allow_private)
            pinned, headers, extensions = _pin_to_ip(current, ip)
            async with self._client.stream(
                "GET", pinned, headers=headers, extensions=extensions
            ) as resp:
                if resp.is_redirect and resp.has_redirect_location:
                    # Join against the logical (hostname) URL, not the pinned one.
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"Fetch failed: HTTP {resp.status_code} for {current!r}.")
                mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if mime and not _is_textual_mime(mime):
                    raise FetchError(
                        f"Refusing non-text content-type {mime!r} for {current!r}."
                    )
                body = await self._read_capped(resp)

            html = body.decode(resp.charset_encoding or "utf-8", errors="replace")
            title, text = extract_text(html)
            flags = scan_for_injection(f"{title} {text}")
            return FetchedPage(
                requested_url=requested,
                final_url=current,
                title=title,
                text=text,
                fetched_bytes=len(body),
                injection_flags=flags,
            )

        raise FetchError(f"Too many redirects (>{_MAX_REDIRECTS}) fetching {requested!r}.")

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Read the body in chunks, aborting as soon as the size cap is exceeded."""
        cap = self._settings.max_response_bytes
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise FetchError(
                f"Page too large (Content-Length {declared} bytes > {cap} cap); "
                "rejected as a DoS guard."
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise FetchError(
                    f"Page too large (>{cap} bytes); download aborted as a DoS guard."
                )
            chunks.append(chunk)
        return b"".join(chunks)
