#!/usr/bin/env python3
"""3 (2026-06-02): on a SECOND Edit, the first Edit's feedback was forgotten —
the refine path is stateless (each edit used only the latest typed feedback +
the current draft, then overwrote draft_text; no feedback_history). This is the
BRIDGE half: accumulate every edit's feedback on the draft blob so the refine
prompt can replay ALL prior corrections. (The n8n Prep Refine REPLAY half is
separate.)

Run: python3 hermes-bridge/test_accumulate_feedback.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _accumulate_feedback  # noqa: E402


def test_first_feedback():
    assert _accumulate_feedback(None, "remove the word luxury") == \
        ["remove the word luxury"]


def test_accumulates_across_edits():
    h = _accumulate_feedback([], "remove luxury")
    h = _accumulate_feedback(h, "make it shorter")
    assert h == ["remove luxury", "make it shorter"]


def test_empty_feedback_ignored():
    assert _accumulate_feedback(["a"], "") == ["a"]
    assert _accumulate_feedback(["a"], "   ") == ["a"]
    assert _accumulate_feedback(["a"], None) == ["a"]


def test_consecutive_duplicate_ignored():
    assert _accumulate_feedback(["make it shorter"], "make it shorter") == \
        ["make it shorter"]


def test_caps_length():
    h = []
    for i in range(15):
        h = _accumulate_feedback(h, f"edit {i}")
    assert len(h) == 10
    assert h[-1] == "edit 14"  # keeps the most recent


def test_trims_whitespace():
    assert _accumulate_feedback([], "  keep it warm  ") == ["keep it warm"]


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} accumulate-feedback tests passed")
