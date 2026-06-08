#!/usr/bin/env python3
"""Unit tests for labels._ghost_recovery_service() — picks the service noun for
a proactive ghost-recovery nudge so it names what the lead ACTUALLY asked about.

Operator 2026-06-08: a wakeboarding-lesson lead (Walid) was nudged with "have
you given up on booking a private yacht?" — the phrase hardcoded "yacht". The
helper must:
  - return "a private yacht" for a charter lead (yacht in facts OR convo),
  - name the specific watersport when clearly stated (wakeboard/jet ski/...),
  - NEVER name a service the lead didn't raise — fall back to a generic "with us"
    when the activity isn't clearly detectable.

Plain-assert style. Run: python3 hermes-bridge/test_ghost_recovery_service.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _ghost_recovery_service  # noqa: E402


def test_yacht_from_facts_field():
    assert _ghost_recovery_service("Bliss 55", "hi") == "a private yacht"


def test_yacht_from_charter_word_in_convo():
    assert _ghost_recovery_service("", "we'd like to charter a yacht Saturday") \
        == "a private yacht"


def test_yacht_wins_even_if_addon_mentioned():
    # A charter lead (yacht in facts) who also asked about a jet-ski add-on is
    # still a yacht lead — the add-on must not flip the service noun.
    assert _ghost_recovery_service("Satoshi 70", "can we add a jet ski?") \
        == "a private yacht"


def test_wakeboarding_lead():
    assert _ghost_recovery_service("", "my first wakeboard class, instructor?") \
        == "wakeboarding"


def test_jetski_lead():
    assert _ghost_recovery_service("", "can we rent a jet ski for an hour") \
        == "a jet ski session"


def test_generic_lesson_no_specific_activity_is_with_us():
    # Walid's actual snippet: a lesson/instructor/session with NO named activity
    # and NO yacht -> safe generic, NEVER "a private yacht".
    out = _ghost_recovery_service(
        "", "It will be my first class. Does it include an instructor too? "
            "the session is 1,200 AED for 1 hour")
    assert out == "with us"
    assert "yacht" not in out


def test_none_safe():
    assert _ghost_recovery_service(None, None) == "with us"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} ghost_recovery_service tests passed")
