#!/usr/bin/env python3
"""Pillar C (2026-06-02): the drafter must ASK (needs_operator_input) instead
of guessing a missing essential fact. The instruction rides on
behavioral_context().formatted as RULE #2 (right after NO_INVENT RULE #1) — the
same channel n8n 'Fetch Behavioral Context' feeds the drafter. n8n Parse
Response then detects needs_operator_input, calls /ask-operator, and skips the
card. This locks the directive's key elements + conservative framing so it
can't silently drift.

Run: python3 hermes-bridge/test_ask_before_guess.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import ASK_BEFORE_GUESS_DIRECTIVE  # noqa: E402


def test_directive_names_the_field():
    assert "needs_operator_input" in ASK_BEFORE_GUESS_DIRECTIVE


def test_directive_requires_empty_messages():
    # When it asks, the drafter must leave messages empty (so Parse Response
    # routes to /ask-operator instead of posting a draft card).
    assert "messages" in ASK_BEFORE_GUESS_DIRECTIVE.lower()
    assert "[]" in ASK_BEFORE_GUESS_DIRECTIVE or \
        "empty" in ASK_BEFORE_GUESS_DIRECTIVE.lower()


def test_directive_is_conservative():
    # Must bias toward drafting, not over-asking the operator.
    d = ASK_BEFORE_GUESS_DIRECTIVE.lower()
    assert "sparingly" in d or "no input" in d
    assert "holding" in d            # offers the holding-line alternative
    assert "one question" in d       # at most one question


def test_directive_forbids_re_asking_known():
    assert "history" in ASK_BEFORE_GUESS_DIRECTIVE.lower()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} ask-before-guess tests passed")
