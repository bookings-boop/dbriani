#!/usr/bin/env python3
"""Deterministic booked_yacht persistence (2026-06-04, QC D3 Variant B).

The booked_yacht column (shown as ✅ in /review) was read-only — nothing wrote
it. On a CONFIRMED transition we now persist it deterministically (NO LLM) ONLY
when the discussed-yachts accumulator is unambiguous (exactly one yacht). A
multi-yacht CONFIRMED is left for the operator (keeps the ⚠️ 'which one?' flag),
and an already-set booked_yacht is never overwritten — so a hallucinated/auth-
oritative-✅ wrong yacht can never be written.

Run: python3 hermes-bridge/test_booked_yacht_persist.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _booked_yacht_from_accumulator as P  # noqa: E402


def test_single_yacht_is_persisted():
    assert P("Bliss 55", "") == "Bliss 55"


def test_multi_yacht_is_left_for_operator():
    assert P("Bliss 55, Sunseeker Satoshi 70, Pershing 82", "") == ""


def test_already_booked_never_overwritten():
    assert P("Bliss 55", "Lana 62") == ""


def test_empty_accumulator_writes_nothing():
    assert P("", "") == ""
    assert P(None, "") == ""


def test_whitespace_single_yacht_trimmed():
    assert P("  Royalty 136  ", "") == "Royalty 136"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} booked-yacht-persist tests passed")
