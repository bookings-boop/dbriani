#!/usr/bin/env python3
"""Bug B follow-up: when a lead is LOST (declined / wants a service we don't
offer), the drafter should write a BRIEF warm GOODBYE — not a sales push.
_graceful_goodbye_line(label) rides _lead_state_block -> behavioral_context ->
the drafter (operator decision 2026-06-06: auto-demote -> LOST + draft a
graceful goodbye). '' for any non-LOST label (safe to concatenate).

Run: python3 hermes-bridge/test_graceful_goodbye.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _graceful_goodbye_line, GRACEFUL_GOODBYE_DIRECTIVE  # noqa: E402


def test_lost_emits_goodbye_directive():
    line = _graceful_goodbye_line("LOST").lower()
    assert "goodbye" in line or "thank" in line, line
    # must steer AWAY from a sales push
    assert ("do not" in line or "don't" in line) and "push" in line, line


def test_case_insensitive():
    assert _graceful_goodbye_line("lost") == GRACEFUL_GOODBYE_DIRECTIVE


def test_non_lost_is_empty():
    assert _graceful_goodbye_line("HOT") == ""
    assert _graceful_goodbye_line("CONFIRMED") == ""
    assert _graceful_goodbye_line("") == ""
    assert _graceful_goodbye_line(None) == ""


def test_directive_is_substantial():
    assert len(GRACEFUL_GOODBYE_DIRECTIVE.strip()) > 100


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} graceful_goodbye tests passed")
