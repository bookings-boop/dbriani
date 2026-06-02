#!/usr/bin/env python3
"""Unit tests for _deterministic_break() — the autonomous-send break backstop.

Bug 2026-06-02 (QC AUTO-2): break-conditions (human-handoff / discount / strong
negative) relied SOLELY on the LLM self-flagging in n8n; a missed flag fails
OPEN and the draft auto-sends. This deterministic backstop runs at the commit
point as an OR with the LLM flag — additive only (it can only ADD a break,
never suppress the LLM's), and a false positive just routes to approval (the
safe direction).

Run: python3 hermes-bridge/test_break_backstop.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _deterministic_break  # noqa: E402


def test_handoff_request_breaks():
    assert _deterministic_break("can I speak to a human please?")


def test_discount_request_breaks():
    assert _deterministic_break("any discount available?") == "discount_request"
    assert _deterministic_break("that's too expensive for us") == "discount_request"
    assert _deterministic_break("can you do a better price?") == "discount_request"


def test_negative_sentiment_breaks():
    assert _deterministic_break("not interested, please stop messaging me") \
        == "negative_sentiment"
    assert _deterministic_break("unsubscribe") == "negative_sentiment"


def test_clean_booking_message_no_break():
    assert _deterministic_break("yes let's book Bliss 55 for Saturday 5pm") == ""
    assert _deterministic_break("great, what's the next step?") == ""


def test_empty_no_break():
    assert _deterministic_break("") == ""
    assert _deterministic_break(None) == ""


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} break-backstop tests passed")
