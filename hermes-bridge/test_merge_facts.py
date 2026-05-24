#!/usr/bin/env python3
"""Unit tests for _merge_facts() — the cached-vs-extracted merge rule.

The system extracts {name, dates, yachts, party_size} on every gated
message. A failed extraction (Hermes timeout / bad JSON) returns None
or empty fields — _merge_facts must NEVER let a failed extraction
erase a previously-known fact.

Contract:
  - non-empty extracted field   → overwrites cached
  - empty / missing extracted   → keeps cached
  - None inputs                  → no crash, empty dict

Plain-assert style. Run: python3 hermes-bridge/test_merge_facts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _merge_facts  # noqa: E402


def test_both_none_returns_empty():
    out = _merge_facts(None, None)
    assert out == {"name": "", "dates": "", "yachts": "", "party_size": ""}


def test_extracted_wins_when_present():
    cached = {"name": "old", "dates": "old", "yachts": "old",
              "party_size": "old"}
    extracted = {"name": "new", "dates": "new", "yachts": "new",
                 "party_size": "new"}
    out = _merge_facts(cached, extracted)
    assert out == {"name": "new", "dates": "new", "yachts": "new",
                   "party_size": "new"}


def test_extracted_empty_keeps_cached():
    # Critical: empty extracted field must NOT clobber cached. This
    # rule prevents 'name=Mark' from becoming 'name=""' when Hermes
    # times out on a subsequent message.
    cached = {"name": "Mark", "dates": "Sat Dec 14", "yachts": "Aurora",
              "party_size": "6 guests"}
    extracted = {"name": "", "dates": "", "yachts": "", "party_size": ""}
    assert _merge_facts(cached, extracted) == cached


def test_partial_extraction_partial_overwrite():
    # Hermes commonly returns ONE new fact per message (e.g. customer
    # adds party_size after dates were already known). Each field is
    # merged independently.
    cached = {"name": "Mark", "dates": "Sat Dec 14", "yachts": "Aurora",
              "party_size": ""}
    extracted = {"name": "", "dates": "", "yachts": "",
                 "party_size": "8 guests"}
    out = _merge_facts(cached, extracted)
    assert out["name"] == "Mark"
    assert out["dates"] == "Sat Dec 14"
    assert out["yachts"] == "Aurora"
    assert out["party_size"] == "8 guests"


def test_cached_none_extracted_present():
    # First-message path: no cached row exists yet.
    extracted = {"name": "Sarah", "dates": "tomorrow",
                 "yachts": "Satoshi", "party_size": "4"}
    assert _merge_facts(None, extracted) == extracted


def test_cached_present_extracted_none():
    # Hermes-disabled path or extraction-gate-skipped path passes
    # None for extracted. Cache must persist verbatim.
    cached = {"name": "Mark", "dates": "", "yachts": "Aurora",
              "party_size": ""}
    assert _merge_facts(cached, None) == cached


def test_extracted_whitespace_treated_as_empty():
    # Hermes occasionally returns '  ' for unknowns. Whitespace-only
    # should NOT overwrite a real cached value.
    cached = {"name": "Mark", "dates": "", "yachts": "", "party_size": ""}
    extracted = {"name": "   ", "dates": "", "yachts": "",
                 "party_size": ""}
    assert _merge_facts(cached, extracted)["name"] == "Mark"


def test_extracted_missing_keys_safe():
    # Hermes JSON missing a key entirely — merge must NOT KeyError.
    cached = {"name": "Mark", "dates": "Sat", "yachts": "Aurora",
              "party_size": "6"}
    extracted = {"name": "new"}  # only one key
    out = _merge_facts(cached, extracted)
    assert out["name"] == "new"
    # Other fields fell back to cached
    assert out["dates"] == "Sat"
    assert out["yachts"] == "Aurora"
    assert out["party_size"] == "6"


def test_returns_all_four_keys():
    # Caller (upsert_customer_facts) depends on ALL FOUR keys always
    # being present in the output, even if everything was empty.
    out = _merge_facts(None, None)
    for k in ("name", "dates", "yachts", "party_size"):
        assert k in out


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} merge_facts tests passed")
