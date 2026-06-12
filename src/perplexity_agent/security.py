"""Cross-cutting security controls: rate limiting, audit logging, output filtering.

Implements several NSA MCP recommendations:
- DoS / fatigue resistance via a token-bucket rate limiter.
- Logging & detection via a structured JSON audit log with secret redaction.
- Output filtering: Perplexity responses are treated as *untrusted input* to the
  next stage — length checks plus best-effort indirect-prompt-injection flagging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("perplexity_agent.audit")

# Substrings that commonly indicate a secret; values are redacted from audit logs.
_SECRET_KEY_HINTS = ("key", "token", "secret", "authorization", "password", "bearer")

# Keys the hints match but that hold harmless counts, not credentials (the "token"
# hint would otherwise redact LLM usage metrics out of the audit log).
_SECRET_KEY_ALLOWLIST = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens", "max_tokens", "max_tokens_per_page"}
)

# Best-effort patterns that hint at indirect prompt injection inside fetched text.
# These only *flag* content (added to a metadata field); they never block it, to
# avoid silently dropping legitimate results.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(the\s+)?(system|previous)\s+prompt", re.I),
    re.compile(r"you\s+are\s+now\s+(a|an|in)\b", re.I),
    re.compile(r"</?(system|assistant|tool)\b", re.I),
    re.compile(r"\bBEGIN\s+(SYSTEM|PROMPT)\b", re.I),
    re.compile(r"exfiltrate|reveal\s+your\s+(system\s+)?prompt", re.I),
]


def redact(value: object) -> object:
    """Recursively redact secret-looking values for safe logging."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            lowered = k.lower() if isinstance(k, str) else ""
            if (
                lowered
                and lowered not in _SECRET_KEY_ALLOWLIST
                and any(h in lowered for h in _SECRET_KEY_HINTS)
            ):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Redact bearer tokens / pplx keys that may appear inline.
        v = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer ***REDACTED***", value)
        v = re.sub(r"pplx-[A-Za-z0-9]{8,}", "pplx-***REDACTED***", v)
        return v
    return value


def scan_for_injection(text: str) -> list[str]:
    """Return labels of indirect-prompt-injection patterns found in ``text``."""
    if not text:
        return []
    hits: list[str] = []
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            hits.append(pat.pattern)
    return hits


def content_hash(value: object) -> str:
    """Stable sha256 of a JSON-serializable value, for audit correlation."""
    raw = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


class RateLimitError(RuntimeError):
    """Raised when the token-bucket rate limit is exceeded."""


@dataclass
class TokenBucket:
    """Simple monotonic token-bucket limiter (NSA: DoS / fatigue resistance)."""

    rate_per_minute: float
    burst: int
    _tokens: float = field(init=False)
    _updated: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._updated = time.monotonic()

    def acquire(self, cost: float = 1.0) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self.burst, self._tokens + elapsed * (self.rate_per_minute / 60.0))
        if self._tokens < cost:
            raise RateLimitError(
                "Rate limit exceeded; slow down requests "
                f"(limit ~{self.rate_per_minute:.0f}/min, burst {self.burst})."
            )
        self._tokens -= cost


class AuditLogger:
    """Structured JSON audit log to stderr and (optionally) a file."""

    def __init__(self, file_path: str | None = None) -> None:
        self._logger = logger
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False
        if file_path:
            fh = logging.FileHandler(file_path)
            fh.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(fh)

    def record(self, event: str, **fields: object) -> None:
        """Emit one audit event. All fields pass through secret redaction."""
        payload = {"event": event, **{k: redact(v) for k, v in fields.items()}}
        self._logger.info(json.dumps(payload, default=str, sort_keys=True))


class RequestGuard:
    """Wires the token-bucket rate limit and the audit log together for a surface.

    The MCP server has its own inline guard (with per-call correlation ids). This
    is the equivalent for the surfaces that previously had neither half wired up:
    the interactive TUI's commands and its background monitor tasks. Going through
    one object means a new TUI command or task can't charge a rate-limit token
    without also leaving an audit trail, or vice versa.
    """

    def __init__(self, bucket: TokenBucket, audit: AuditLogger) -> None:
        self._bucket = bucket
        self._audit = audit

    def acquire(self, event: str, **fields: object) -> None:
        """Charge one token and audit the call; on rate-limit audit then re-raise."""
        try:
            self._bucket.acquire()
        except RateLimitError:
            self._audit.record("rate_limited", blocked=event, **fields)
            raise
        self._audit.record(event, **fields)

    def record(self, event: str, **fields: object) -> None:
        """Audit an event without charging a token (e.g. a command result)."""
        self._audit.record(event, **fields)
