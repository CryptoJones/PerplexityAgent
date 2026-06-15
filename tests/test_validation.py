from perplexity_agent.validation import build_known_urls, validate_report


def test_build_known_urls_canonicalizes():
    sources = [{"url": "https://Example.com/page/#frag"}, {"url": "https://example.com/page"}]
    known = build_known_urls(sources)
    assert known == {"https://example.com/page"}


def test_validate_passes_when_all_claims_supported():
    sources = [{"url": "https://a.com/x"}, {"url": "https://b.com/y"}]
    known = build_known_urls(sources)
    report = {
        "answer": "ok",
        "claims": [
            {"claim": "c1", "supporting_urls": ["https://a.com/x"]},
            {"claim": "c2", "supporting_urls": ["https://b.com/y/"]},
        ],
    }
    _, vr = validate_report(report, known)
    assert vr["passed"] is True
    assert vr["total_claims"] == 2


def test_validate_flags_unsupported_claim():
    known = build_known_urls([{"url": "https://a.com/x"}])
    report = {"claims": [{"claim": "no source", "supporting_urls": []}]}
    annotated, vr = validate_report(report, known)
    assert vr["passed"] is False
    assert vr["claims_without_support"] == 1
    assert annotated["claims"][0]["confidence"] == "uncertain"


def test_validate_flags_unknown_url():
    known = build_known_urls([{"url": "https://a.com/x"}])
    report = {"claims": [{"claim": "bad url", "supporting_urls": ["https://evil.com/z"]}]}
    annotated, vr = validate_report(report, known)
    assert vr["all_urls_known"] is False
    assert annotated["claims"][0]["unsupported_urls"] == ["https://evil.com/z"]
    assert annotated["claims"][0]["confidence"] == "low"


def test_flagged_entries_carry_index_and_preview():
    known = build_known_urls([{"url": "https://a.com/x"}])
    report = {
        "claims": [
            {"claim": "supported", "supporting_urls": ["https://a.com/x"]},
            {"claim": "C" * 200, "supporting_urls": []},
        ]
    }
    _, vr = validate_report(report, known)
    assert len(vr["flagged"]) == 1
    f = vr["flagged"][0]
    assert f["index"] == 1  # the second claim
    assert f["reason"] == "no_supporting_url"
    assert f["claim_preview"] == "C" * 100  # truncated to a 100-char preview
