#!/usr/bin/env python3
"""_merged_label — on an identity merge, a STALE canonical (LOST/DISREGARDED/
COLD) re-activates when the merged-in duplicate is an ACTIVE lead (a returning
customer's fresh @c.us enquiry must not stay buried under the old @lid label).
A won/in-flight canonical always wins. Audit #3 companion, 2026-06-07.

Run: python3 hermes-bridge/test_merged_label.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _merged_label  # noqa: E402


def test_stale_canon_reactivates_to_active_dup():
    assert _merged_label("LOST", "HOT") == "HOT"
    assert _merged_label("DISREGARDED", "NEEDS_ATTENTION") == "NEEDS_ATTENTION"
    assert _merged_label("COLD", "WARM") == "WARM"


def test_default_new_dup_does_not_reactivate_closed_canon():
    # fix-group 2 (review, 2026-06-07): NEW is the DEFAULT label of any fresh
    # row, NOT evidence of a genuine enquiry — a default-NEW dup must NOT
    # re-activate a deliberately-closed/stale canonical (reopened spam on merge).
    assert _merged_label("DISREGARDED", "NEW") == "DISREGARDED"
    assert _merged_label("LOST", "NEW") == "LOST"
    assert _merged_label("COLD", "NEW") == "COLD"


def test_won_or_inflight_canon_never_downgraded():
    # CONFIRMED / WAITING_FOR_PAYMENT must win over an 'active' dup.
    assert _merged_label("CONFIRMED", "HOT") == "CONFIRMED"
    assert _merged_label("WAITING_FOR_PAYMENT", "NEW") == "WAITING_FOR_PAYMENT"
    assert _merged_label("PAUSED_SPAM", "HOT") == "PAUSED_SPAM"


def test_stale_canon_with_non_active_dup_unchanged():
    assert _merged_label("LOST", "COLD") == "LOST"        # dup not active
    assert _merged_label("LOST", "LOST") == "LOST"
    assert _merged_label("HOT", "NEW") == "HOT"           # canon already active


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} merged-label tests passed")
