#!/usr/bin/env python3
"""Unit tests for the R4 no-draft fallback helpers (2026-06-02).

When the drafter (cloud gate + local Hermes) returns no usable message —
e.g. `messages` that are dicts MISSING the 'text' key — handle_draft_followup
used to surface "Hermes returned no draft" (empty draft_text). R4 funnels
that empty state into a fact-anchored placeholder so the operator always has
something to edit, flagged via `fallback_used` + a warning badge.

Two pure helpers (labels.py):
  - _join_draft_parts(messages): extract text parts from Hermes' messages
    list (tolerant of str and dict shapes; a dict-without-'text' yields no
    usable text — the exact real path that produced the empty draft).
  - _fact_anchored_fallback(name, yacht, date, party): build the warm,
    fact-anchored placeholder; NEVER returns empty.

Plain-assert style. Run: python3 hermes-bridge/test_draft_fallback.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _join_draft_parts, _fact_anchored_fallback  # noqa: E402


# --- _join_draft_parts -------------------------------------------------------
def test_join_string_messages():
    assert _join_draft_parts(["  hi mark  ", "ready when you are"]) == \
        ["hi mark", "ready when you are"]


def test_join_dict_messages_with_text():
    assert _join_draft_parts([{"role": "assistant", "text": " hey "}]) == ["hey"]


def test_join_dict_missing_text_yields_no_usable_text():
    # THE R4 real path: Hermes returned message dicts with no 'text' key.
    parts = _join_draft_parts([{"role": "assistant"}, {"foo": "bar"}])
    assert [p for p in parts if p] == []   # nothing usable -> triggers fallback


def test_join_mixed_drops_only_empty():
    parts = _join_draft_parts(["hello", {"role": "x"}, {"text": "there"}])
    assert [p for p in parts if p] == ["hello", "there"]


def test_join_handles_none_and_non_str_dict():
    assert _join_draft_parts(None) == []
    assert _join_draft_parts([]) == []
    assert [p for p in _join_draft_parts([None, 5, "ok"]) if p] == ["ok"]


# --- _fact_anchored_fallback -------------------------------------------------
def test_fallback_anchors_on_known_facts():
    msg = _fact_anchored_fallback(name="Anton", yacht="Majesty 90",
                                  date="June 12", party="8")
    assert "Anton" in msg
    assert "Majesty 90" in msg
    assert "June 12" in msg
    assert "8" in msg
    assert "just checking in" not in msg.lower()   # NOT a generic line


def test_fallback_personalises_with_name_only():
    msg = _fact_anchored_fallback(name="Sara")
    assert msg.strip()
    assert "Sara" in msg


def test_fallback_never_empty_even_with_nothing():
    # The whole point of R4: never surface "Hermes returned no draft".
    msg = _fact_anchored_fallback()
    assert msg.strip()
    assert "checking in" not in msg.lower()


def test_fallback_ignores_blank_and_zero_facts():
    # party "0"/"" and blank yacht/date must not leak into the message.
    msg = _fact_anchored_fallback(name="   ", yacht="  ", date="", party="0")
    assert msg.strip()
    assert "0 guests" not in msg
    assert "the  " not in msg            # no dangling 'the <blank>'
    assert "for  " not in msg            # no dangling 'for <blank>'


def test_fallback_uses_first_name_only():
    msg = _fact_anchored_fallback(name="Anton Petrov")
    assert "Anton" in msg
    assert "Petrov" not in msg


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} draft-fallback tests passed")
