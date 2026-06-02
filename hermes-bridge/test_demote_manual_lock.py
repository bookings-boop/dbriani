#!/usr/bin/env python3
"""Task #13 (stress #11): the analyzer auto-demote-to-COLD (routes.py ~3431)
could silently re-COLD a lead an operator just MANUALLY moved (e.g. the manual
HOT reverts on never-booked Dean Pearson rows this session). The existing guard
only protected ever-booked / unverifiable-passed closes, and the manual reverts
set no label_locked_until. This locks a pure helper: a RECENT manual override
(signal 'manual:*') must block the automatic demote until it ages out.

Run: python3 hermes-bridge/test_demote_manual_lock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _manual_override_protects  # noqa: E402


def test_recent_manual_revert_protects():
    assert _manual_override_protects("manual:demote_revert", 2) is True
    assert _manual_override_protects("manual:identity_unmerge", 0) is True
    assert _manual_override_protects("manual:analyzer_score0_correction", 13) is True


def test_expired_manual_override_not_protected():
    # Past the window, automation may re-evaluate.
    assert _manual_override_protects("manual:demote_revert", 15) is False
    assert _manual_override_protects("manual:demote_revert", 99) is False


def test_non_manual_signal_not_protected():
    assert _manual_override_protects("auto:analyzer_close", 1) is False
    assert _manual_override_protects("auto:analyzer_score0", 0) is False
    assert _manual_override_protects("reconcile:paid_not_confirmed", 1) is False


def test_blank_or_none_signal_not_protected():
    assert _manual_override_protects("", 1) is False
    assert _manual_override_protects(None, 1) is False


def test_custom_window():
    assert _manual_override_protects("manual:x", 20, window_days=30) is True
    assert _manual_override_protects("manual:x", 40, window_days=30) is False


def test_negative_or_unknown_age_protects_failsafe():
    # An unknown/negative age (just-written, clock skew) should fail SAFE = protect.
    assert _manual_override_protects("manual:demote_revert", -1) is True
    assert _manual_override_protects("manual:demote_revert", None) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1; print("FAIL", fn.__name__, "-", e or "assert")
        except Exception as e:
            failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
