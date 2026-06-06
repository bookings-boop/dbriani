#!/usr/bin/env python3
"""Bubble-count cap in sanitize_draft_messages (operator 2026-06-06:
'overtexting' — Emma got 2-4 bubbles per reply, ~17 outbound to ~7 inbound;
one draft had ~10 bubbles). Cap the number of SEPARATE WhatsApp messages per
reply, merging any overflow into the last bubble (newline-spaced) so no content
is lost — fewer, calmer messages (Ritz-Carlton concision). The drafter's own
brevity is handled separately by the STYLE directive (n8n prompt).

Run: python3 hermes-bridge/test_bubble_cap.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import sanitize_draft_messages  # noqa: E402


def test_caps_bubbles_and_loses_no_content():
    msgs = ["bubble one", "bubble two", "bubble three",
            "bubble four", "bubble five", "bubble six"]
    out, stripped = sanitize_draft_messages(msgs)
    assert len(out) == 4, out
    assert stripped is True, out
    joined = "\n".join(out)
    for w in ("one", "two", "three", "four", "five", "six"):
        assert w in joined, (w, out)


def test_three_bubbles_unchanged():
    msgs = ["hello there friend", "here are the options", "which one works best?"]
    out, stripped = sanitize_draft_messages(msgs)
    assert out == msgs, out
    assert stripped is False, out


def test_exactly_four_unchanged():
    msgs = ["alpha first", "bravo second", "charlie third", "delta fourth"]
    out, _ = sanitize_draft_messages(msgs)
    assert out == msgs, out


def test_overflow_merged_into_last_bubble():
    msgs = ["first message here", "second message here", "third message here",
            "fourth message here", "fifth message here"]
    out, _ = sanitize_draft_messages(msgs)
    assert len(out) == 4, out
    # 4th + 5th merged into the last bubble
    assert "fourth message here" in out[-1] and "fifth message here" in out[-1], out


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} bubble_cap tests passed")
