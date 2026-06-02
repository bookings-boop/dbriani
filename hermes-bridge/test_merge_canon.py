#!/usr/bin/env python3
"""Unit tests for _merge_canonical_pick() — choosing the surviving row in an
identity merge.

Bug fixed 2026-06-02 (QC A1): reconcile chose canon purely by message_count, so
a CONFIRMED/booked lead could be buried under a chattier non-booked duplicate
(Qurbani class). A booked/CONFIRMED side must always survive; message_count only
breaks ties when both or neither are booked.

Run: python3 hermes-bridge/test_merge_canon.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _merge_canonical_pick  # noqa: E402

A, B = "a@lid", "b@c.us"


def test_booked_side_wins_over_chattier_unbooked():
    # A is booked but has FEWER messages; A must still be canon (the bug)
    assert _merge_canonical_pick(A, B, 2, 99, True, False) == (A, B)


def test_booked_side_wins_when_it_is_b():
    assert _merge_canonical_pick(A, B, 99, 1, False, True) == (B, A)


def test_both_booked_falls_back_to_message_count():
    assert _merge_canonical_pick(A, B, 10, 5, True, True) == (A, B)
    assert _merge_canonical_pick(A, B, 5, 10, True, True) == (B, A)


def test_neither_booked_falls_back_to_message_count():
    assert _merge_canonical_pick(A, B, 7, 7, False, False) == (A, B)  # >= keeps A
    assert _merge_canonical_pick(A, B, 3, 8, False, False) == (B, A)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} merge-canon tests passed")
