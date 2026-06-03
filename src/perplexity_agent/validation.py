"""Validation layer for synthesized research output.

Treats citations as first-class, verifiable output fields rather than cosmetic
text. Enforces the three checks from the reference architecture:

1. every claim has at least one supporting URL,
2. every supporting URL exists in the retrieval results / citation metadata,
3. claims whose support is missing/unknown are flagged ``uncertain``.
"""

from __future__ import annotations

from typing import Any

from .client import canonical_url


def build_known_urls(sources: list[dict[str, Any]], extra: list[str] | None = None) -> set[str]:
    """Set of canonical URLs that actually appeared in retrieval/citation metadata."""
    known = {canonical_url(s.get("url") or "") for s in sources}
    if extra:
        known.update(canonical_url(u) for u in extra)
    known.discard("")
    return known


def validate_report(
    report: dict[str, Any], known_urls: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and annotate a research report against known source URLs.

    Returns ``(annotated_report, validation_report)``. The report's claims are
    annotated in place with ``unsupported_urls`` and a downgraded ``confidence``
    where evidence is weak; nothing is dropped, only flagged.
    """
    claims = report.get("claims") or []
    total = len(claims)
    claims_without_support = 0
    claims_with_unknown_urls = 0
    flagged: list[dict[str, Any]] = []

    for claim in claims:
        urls = claim.get("supporting_urls") or []
        canon = [canonical_url(u) for u in urls if u]
        unknown = [u for u, c in zip(urls, canon, strict=False) if c and c not in known_urls]

        if not canon:
            claims_without_support += 1
            claim["confidence"] = "uncertain"
            flagged.append({"claim": claim.get("claim"), "reason": "no_supporting_url"})
        elif unknown:
            claims_with_unknown_urls += 1
            claim["unsupported_urls"] = unknown
            # Don't trust links the retrieval stage never produced.
            if claim.get("confidence") not in ("low", "uncertain"):
                claim["confidence"] = "low"
            flagged.append({"claim": claim.get("claim"), "reason": "url_not_in_sources"})

    validation_report = {
        "total_claims": total,
        "claims_without_support": claims_without_support,
        "claims_with_unknown_urls": claims_with_unknown_urls,
        "all_claims_supported": claims_without_support == 0,
        "all_urls_known": claims_with_unknown_urls == 0,
        "passed": claims_without_support == 0 and claims_with_unknown_urls == 0,
        "flagged": flagged,
    }
    return report, validation_report
