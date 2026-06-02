#!/usr/bin/env python3
"""Autonomous-send QUALITY FLOOR (2026-06-02): an autonomous draft may auto-send
ONLY if it scores >= the floor (8). This locks the FAIL-CLOSED gate decision:
a scoring error (score 0 / None), a non-numeric score, or any score below the
floor must NEVER auto-send — it routes to approval. Auto-send is allowed only
for a real integer score at or above the floor.

Run: python3 hermes-bridge/test_quality_floor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _quality_floor_ok  # noqa: E402


def test_at_and_above_floor_passes():
    for s in (8, 9, 10):
        assert _quality_floor_ok(s) is True, s


def test_below_floor_blocks():
    for s in (7, 5, 1):
        assert _quality_floor_ok(s) is False, s


def test_error_scores_block_fail_closed():
    # _anthropic_score returns 0 on any error -> must NOT auto-send.
    assert _quality_floor_ok(0) is False
    assert _quality_floor_ok(None) is False


def test_non_int_blocks_fail_closed():
    for s in ("8", 8.0, 8.5, True, [], {}):
        assert _quality_floor_ok(s) is False, s


def test_custom_threshold():
    assert _quality_floor_ok(8, threshold=9) is False
    assert _quality_floor_ok(9, threshold=9) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} quality-floor tests passed")
