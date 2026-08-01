"""Tests for the token-efficiency toolkit."""

from __future__ import annotations

import json

from perplexity_agent import efficiency


def test_estimate_tokens():
    assert efficiency.estimate_tokens("") == 0
    assert efficiency.estimate_tokens("x" * 40) == 10


def test_bound_text_under_and_over_budget():
    text, truncated = efficiency.bound_text("hello", 100)
    assert text == "hello" and truncated is False
    text, truncated = efficiency.bound_text("x" * 100, 10)
    assert truncated is True
    assert text.startswith("x" * 10)
    assert "truncated" in text


def test_bound_text_is_utf8_safe():
    # Truncating on code points never splits a multi-byte char.
    text, truncated = efficiency.bound_text("é" * 50, 10)
    assert truncated is True
    assert text[:10] == "é" * 10


def test_paginate_reports_has_more():
    page = efficiency.paginate(list(range(100)), limit=10, offset=0)
    assert page["returned"] == 10
    assert page["total"] == 100
    assert page["has_more"] is True
    assert page["next_offset"] == 10
    last = efficiency.paginate(list(range(5)), limit=10, offset=0)
    assert last["has_more"] is False and "next_offset" not in last


def test_project_keeps_only_named_fields():
    rows = [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5}]
    assert efficiency.project(rows, ("a", "b")) == [{"a": 1, "b": 2}, {"a": 4, "b": 5}]
    assert efficiency.project({"a": 1, "z": 9}, ("a",)) == {"a": 1}


def test_compact_list_keeps_head_and_tail():
    kept, meta = efficiency.compact_list(list(range(100)), max_items=10, dedup=False)
    assert len(kept) <= 10
    assert kept[0] == 0  # head preserved
    assert kept[-1] == 99  # tail preserved
    assert meta["dropped"] > 0


def test_compact_list_dedups():
    kept, meta = efficiency.compact_list([1, 1, 2, 2, 3], max_items=10, dedup=True)
    assert kept == [1, 2, 3]
    assert meta["deduped"] == 2


def test_offload_store_roundtrip_and_eviction():
    store = efficiency.OffloadStore(max_entries=2)
    k1 = store.stash("one")
    k2 = store.stash("two")
    assert store.retrieve(k1) == "one"
    store.stash("three")  # evicts the oldest (k1)
    assert store.retrieve(k1) is None
    assert store.retrieve(k2) == "two"


def test_offload_store_keys_are_unguessable_capabilities():
    # MCP 2026-07-28 removed sessions: retrieval is authorized by possession of an
    # unguessable server-minted key, not by a session owner. Each stash mints a
    # fresh key (so identical content still yields distinct handles), the key
    # retrieves its payload, and a key never handed out cannot be guessed.
    store = efficiency.OffloadStore()
    key_a = store.stash("secret payload")
    key_b = store.stash("secret payload")
    assert key_a != key_b
    assert len(key_a) == 24 and all(c in "0123456789abcdef" for c in key_a)
    assert store.retrieve(key_a) == "secret payload"
    assert store.retrieve(key_b) == "secret payload"
    assert store.retrieve("0" * 24) is None


def test_bound_result_passthrough_when_small():
    result = {"a": 1}
    assert efficiency.bound_result(result, max_chars=1000) == result


def test_bound_result_offloads_when_large():
    store = efficiency.OffloadStore()
    result = {"results": ["x" * 100 for _ in range(50)]}
    bounded = efficiency.bound_result(result, max_chars=200, store=store)
    assert bounded["truncated"] is True
    assert "retrieve_key" in bounded
    # The full original is recoverable, byte-for-byte, from the store.
    restored = store.retrieve(bounded["retrieve_key"])
    assert json.loads(restored) == result


def test_bound_result_truncates_string_without_store():
    bounded = efficiency.bound_result("x" * 500, max_chars=50)
    assert isinstance(bounded, str)
    assert "truncated" in bounded
