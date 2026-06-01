#!/usr/bin/env python3
"""Guards the NO_INVENT_DIRECTIVE that leads the drafter's behavioral context.

2026-06-01 incident: the drafter invented a "375 AED" fine-dining price
despite the no-invent rule existing (buried in the 50KB base prompt + 40
learned rules). behavioral_context().formatted now prepends this hard
directive so it leads the dynamic context the drafter actually reads
(n8n "Fetch Behavioral Context").

Run: python3 hermes-bridge/test_no_invent.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import NO_INVENT_DIRECTIVE  # noqa: E402


def test_directive_forbids_inventing():
    d = NO_INVENT_DIRECTIVE.lower()
    assert "invent" in d
    # must steer to a holding reply rather than a guessed number
    assert "confirm" in d
    assert "price" in d


def test_directive_is_substantial():
    # not an empty/placeholder constant
    assert len(NO_INVENT_DIRECTIVE.strip()) > 120


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} no_invent tests passed")
