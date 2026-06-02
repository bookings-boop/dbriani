#!/usr/bin/env python3
"""Customer-level sibling-card send guard (2026-06-02 false-positive).

The csent:<phone> guard blocked a SAME-card re-tap as if it were a sibling/
duplicate card (194905951437046@lid — a single, un-merged card), showing
"already sent ... sibling-card guard ... merge the duplicate cards". It must
only fire for a DIFFERENT draft_id (a real sibling from an @lid/@c.us split);
the same card re-tapped should fall through to the per-draft claim + status
guards, which give the accurate 'already sending' / 'already sent' message.

Run: python3 hermes-bridge/test_sibling_send_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _sibling_send_blocked  # noqa: E402


def test_different_draft_id_is_a_real_sibling_block():
    assert _sibling_send_blocked("draftA", "draftB") is True


def test_same_draft_id_is_not_blocked_here():
    # same card re-tapped -> not a sibling; let per-draft/status guards handle it
    assert _sibling_send_blocked("draftA", "draftA") is False
    assert _sibling_send_blocked(" draftA\n", "draftA") is False  # redis newline


def test_no_prior_send_is_not_blocked():
    assert _sibling_send_blocked("", "draftA") is False
    assert _sibling_send_blocked(None, "draftA") is False


def test_missing_current_did_is_not_blocked():
    assert _sibling_send_blocked("draftA", "") is False
    assert _sibling_send_blocked("draftA", None) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} sibling-send-guard tests passed")
