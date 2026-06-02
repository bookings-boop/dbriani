#!/usr/bin/env python3
"""Autonomous handoff exemption (2026-06-02): in autonomous mode the conservative
handoff/holding line is a SAFE non-answer the operator explicitly allows to
auto-send even though it scores low. _is_handoff_message must match THAT line
(robust to minor whitespace/punctuation) and must NEVER match a real answer —
otherwise it becomes a floor bypass for substantive replies.

Run: python3 hermes-bridge/test_handoff.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _is_handoff_message  # noqa: E402

HANDOFF = ("I'm probably not the best person to answer that question. I "
           "recommend that one of my colleagues contact you with the correct "
           "details. Would that be okay?")


def test_exact_handoff_matches():
    assert _is_handoff_message(HANDOFF) is True


def test_handoff_with_minor_variation_matches():
    # lowercase / extra spaces / different trailing punctuation still matches
    v = ("im probably not the best person to answer that question  i recommend "
         "that one of my colleagues contact you with the correct details would "
         "that be okay")
    assert _is_handoff_message(v) is True


def test_real_answer_never_matches():
    for r in (
        "The Satoshi 70 is AED 3,000/hour and seats 15 guests.",
        "Yes! We have availability this weekend — how many guests?",
        "Our minimum charter is 2 hours at AED 900/hr.",
        "Hi! Thanks for reaching out, I'd be happy to help.",
    ):
        assert _is_handoff_message(r) is False, r


def test_partial_phrase_does_not_match():
    # a fragment must NOT match — the full handoff signature is required
    assert _is_handoff_message("not the best person to answer") is False
    assert _is_handoff_message("one of my colleagues will contact you") is False


def test_empty_is_false():
    assert _is_handoff_message("") is False
    assert _is_handoff_message(None) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} handoff tests passed")
