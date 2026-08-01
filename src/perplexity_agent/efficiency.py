"""Memory / token-efficiency toolkit for MCP tool output.

The dominant cost of an MCP server is the tokens its tool results spend in the
agent's context. A web-research server is the worst offender: a ``deep_research``
report or a long ``search`` page list can be tens of KB. This module is the
choke point that keeps that bounded — every tool result passes through
``bound_result`` before it leaves the server:

* ``estimate_tokens`` — cheap char-based token estimate.
* ``bound_text`` — UTF-8-safe truncation to a char budget.
* ``paginate`` — page a list and report ``has_more``.
* ``project`` — keep only the fields the model needs.
* ``compact_list`` — keep the head + tail of a long list, drop the middle.
* ``bound_result`` / ``OffloadStore`` — bound any result, optionally offloading
  the full value behind a hash the agent can fetch later via the ``retrieve``
  tool (lossless + bounded).

Pure-Python, no heavy dependencies, bounded memory.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any

# A coarse, dependency-free token estimate. ~4 chars/token is accurate to
# roughly 10% and needs no tokenizer.
_CHARS_PER_TOKEN = 4.0

# Default char budget handed to the model for a single tool result. Generous
# enough for a real research report, small enough that one chatty tool can't
# blow the context. Override per call / via Settings.max_tool_output_chars.
DEFAULT_MAX_CHARS = 100_000

_TRUNCATION_MARKER = "\n…[truncated {dropped} chars to fit the {budget}-char budget]"


def estimate_tokens(text: str, chars_per_token: float = _CHARS_PER_TOKEN) -> int:
    """Estimate the token count of ``text`` (coarse, tokenizer-free)."""
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))


def bound_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, bool]:
    """Truncate ``text`` to ``max_chars`` characters. Returns ``(text, truncated)``.

    Operating on ``str`` (Unicode code points) keeps truncation UTF-8-safe by
    construction — we never split a multi-byte sequence. A marker naming the
    dropped count is appended so the model knows the result is incomplete.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    dropped = len(text) - max_chars
    return text[:max_chars] + _TRUNCATION_MARKER.format(dropped=dropped, budget=max_chars), True


def paginate(items: list[Any], *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Return one bounded page of ``items`` plus pagination metadata."""
    limit = max(1, limit)
    offset = max(0, offset)
    total = len(items)
    page = items[offset : offset + limit]
    has_more = offset + limit < total
    out: dict[str, Any] = {
        "items": page,
        "returned": len(page),
        "total": total,
        "offset": offset,
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + limit
    return out


def project(obj: Mapping[str, Any] | Iterable[Mapping[str, Any]], fields: Iterable[str]) -> Any:
    """Keep only ``fields`` on a dict (or each dict in a list).

    Returning a lean shape instead of a full upstream payload is the cheapest
    token win there is. Missing fields are simply omitted.
    """
    keep = tuple(fields)

    def _one(d: Mapping[str, Any]) -> dict[str, Any]:
        return {k: d[k] for k in keep if k in d}

    if isinstance(obj, Mapping):
        return _one(obj)
    return [_one(d) for d in obj if isinstance(d, Mapping)]


def compact_list(
    items: list[Any],
    *,
    max_items: int = 20,
    first_frac: float = 0.3,
    last_frac: float = 0.15,
    dedup: bool = True,
) -> tuple[list[Any], dict[str, Any]]:
    """Shrink a long list to ``max_items``, keeping the head and tail.

    Order and the boundaries usually carry the signal; the middle is the most
    droppable. Returns ``(kept, meta)`` recording how many were dropped so the
    agent isn't misled into thinking it saw everything.
    """
    original_total = len(items)
    if dedup:
        seen: set[str] = set()
        unique: list[Any] = []
        for it in items:
            key = _stable_key(it)
            if key not in seen:
                seen.add(key)
                unique.append(it)
        items = unique
    deduped_total = len(items)

    if deduped_total <= max_items:
        return items, {
            "total": original_total,
            "kept": deduped_total,
            "dropped": original_total - deduped_total,
            "deduped": original_total - deduped_total if dedup else 0,
        }

    head = max(1, int(max_items * first_frac))
    tail = max(1, int(max_items * last_frac))
    head = min(head, max_items - tail)
    kept = items[:head] + items[-tail:] if tail else items[:head]
    return kept, {
        "total": original_total,
        "kept": len(kept),
        "dropped": deduped_total - len(kept),
        "deduped": original_total - deduped_total if dedup else 0,
        "note": f"kept first {head} + last {tail} of {deduped_total}; middle dropped",
    }


def _stable_key(value: Any) -> str:
    """A cheap content key for de-dup (not for security)."""
    try:
        raw = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(value)
    return hashlib.md5(raw.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:16]


class OffloadStore:
    """Bounded in-memory store for the "compress-cache-retrieve" pattern.

    When a result is too big, ``stash`` it and hand the agent a short content
    hash; the ``retrieve`` tool pulls the full value back on demand. FIFO-evicts
    the oldest entries past ``max_entries`` so the footprint stays bounded.
    """

    def __init__(self, max_entries: int = 128) -> None:
        self.max_entries = max(1, max_entries)
        self._store: OrderedDict[str, str] = OrderedDict()

    def stash(self, payload: str) -> str:
        """Store ``payload`` and return an unguessable retrieval key.

        The key is a fresh random token, not a content hash, so it is a genuine
        server-minted capability: possession of the key IS the authorization to
        retrieve. MCP revision 2026-07-28 removed protocol sessions, so retrieval
        can no longer be scoped to a session identity — the spec's replacement is
        exactly this, an opaque handle passed back as an ordinary tool argument.
        A random key also means an attacker cannot fabricate one by guessing the
        content, which a content-hash key allowed.
        """
        key = secrets.token_hex(12)  # 96 bits, same width as the old short hash
        self._store[key] = payload
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)  # evict oldest
        return key

    def retrieve(self, key: str) -> str | None:
        """Return the payload for ``key``, or ``None`` if absent or evicted."""
        return self._store.get(key)


def bound_result(
    result: Any,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    store: OffloadStore | None = None,
) -> Any:
    """Bound any tool result to ``max_chars``.

    ``str`` is truncated in place. A ``dict``/``list`` is returned untouched when
    its JSON form fits, else it's serialized, truncated, and returned as a
    ``{truncated, content, ...}`` envelope. When an ``OffloadStore`` is given,
    the full value is stashed and the envelope carries a ``retrieve_key`` so the
    agent can fetch the original — bounding without data loss.
    """
    if isinstance(result, str):
        bounded, truncated = bound_text(result, max_chars)
        if truncated and store is not None:
            return {
                "truncated": True,
                "content": bounded,
                "retrieve_key": store.stash(result),
            }
        return bounded

    serialized = json.dumps(result, default=str)
    if len(serialized) <= max_chars:
        return result
    bounded, _ = bound_text(serialized, max_chars)
    envelope: dict[str, Any] = {
        "truncated": True,
        "estimated_full_tokens": estimate_tokens(serialized),
        "content": bounded,
    }
    if store is not None:
        envelope["retrieve_key"] = store.stash(serialized)
    return envelope
