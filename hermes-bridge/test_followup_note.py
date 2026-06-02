#!/usr/bin/env python3
"""5c (2026-06-02): too many /review leads showed the SAME line
"✅ followed up X ago — awaiting reply, don't re-nudge". It's a hardcoded
template (review.py:737) that fires for every already-followed-up lead and
PREEMPTS the analyzer's real per-lead text. This makes the line lead-specific:
keep the timing + anti-pushiness cue, but append the analyzer's own reasoning /
suggested action so each line carries unique info.

Run: python3 hermes-bridge/test_followup_note.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _followup_note  # noqa: E402


def test_appends_analyzer_reasoning():
    out = _followup_note("2d", "paylink sent, hesitant on price", "")
    assert "followed up 2d ago" in out
    assert "awaiting reply" in out
    assert "paylink sent, hesitant on price" in out


def test_falls_back_to_suggested_action():
    out = _followup_note("3h", "", "anchor a mid-tier deal")
    assert "anchor a mid-tier deal" in out


def test_reasoning_preferred_over_suggestion():
    out = _followup_note("1d", "REA text", "SUG text")
    assert "REA text" in out and "SUG text" not in out


def test_no_specific_info_keeps_dont_renudge():
    out = _followup_note("5d", "", "")
    assert "followed up 5d ago" in out
    assert "don't re-nudge" in out


def test_two_leads_differ():
    a = _followup_note("1d", "wants weekend slot", "")
    b = _followup_note("1d", "price too high, lost", "")
    assert a != b


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} followup-note tests passed")
