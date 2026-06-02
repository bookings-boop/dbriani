#!/usr/bin/env python3
"""4a (2026-06-02): the "🛑 Hermes flagged N lead(s) as not-convertible" digest
showed the same customer ("Noha") TWICE. Root cause: handle_pipeline_analyze
builds the card list with no dedup, and an unmerged @lid/@c.us identity split
yields two rows -> two analyses -> two 'close' entries. canonicalize_cid can't
collapse them pre-merge (both merged_into NULL), so a NAME-based pass is needed.

This locks the pure dedup: one entry per customer, by cid then by normalized
non-empty name (the name pass catches the unmerged split). Order preserved.

Run: python3 hermes-bridge/test_dedup_leads.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _dedup_leads  # noqa: E402


def test_name_collapses_unmerged_split():
    # The Noha case: same display name under two different ids.
    assert _dedup_leads([
        ("12345@lid", "Noha", "ghosted after quote"),
        ("971500000009@c.us", "Noha", "ghosted after quote"),
        ("971500000003", "Sara", "price too high"),
    ]) == [
        ("12345@lid", "Noha", "ghosted after quote"),
        ("971500000003", "Sara", "price too high"),
    ]


def test_exact_cid_dup_collapses():
    assert _dedup_leads([("a", "X", "r1"), ("a", "X", "r2")]) == [("a", "X", "r1")]


def test_empty_names_not_collapsed():
    # Two distinct nameless ids must both survive (don't over-merge).
    assert _dedup_leads([("a", "", "r1"), ("b", "", "r2")]) == \
        [("a", "", "r1"), ("b", "", "r2")]


def test_distinct_names_kept():
    out = _dedup_leads([("a", "Ali", "r1"), ("b", "Omar", "r2")])
    assert len(out) == 2


def test_name_normalization_case_and_space():
    assert _dedup_leads([
        ("a", "Noha  Lsb", "r1"),
        ("b", "noha lsb", "r2"),
    ]) == [("a", "Noha  Lsb", "r1")]


def test_order_preserved_first_wins():
    out = _dedup_leads([
        ("a", "Ali", "first"), ("b", "Sara", "x"), ("a", "Ali", "second")])
    assert out == [("a", "Ali", "first"), ("b", "Sara", "x")]


def test_empty_input():
    assert _dedup_leads([]) == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} dedup-leads tests passed")
