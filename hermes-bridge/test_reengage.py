#!/usr/bin/env python3
"""Unit tests for the passed-date re-engage helpers (#5, 2026-06-01).

Passed-date leads (analyzer signal `date_passed`) get a GENTLE re-engage
check-in draft instead of a sales nudge:
  - labels._is_reengage_followup(signal) gates the re-engage directive
    in handle_draft_followup.
  - review._card_draft_label(label_key, signal) labels the /review button
    "💬 Draft check-in" (not "💬 Draft nudge") on those cards, while
    AWAITING_REPLY keeps "✍️ Draft reply".

Plain-assert style. Run: python3 hermes-bridge/test_reengage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _is_reengage_followup  # noqa: E402
from review import _card_draft_label       # noqa: E402


# --- labels._is_reengage_followup -------------------------------------------
def test_date_passed_is_reengage():
    assert _is_reengage_followup("date_passed") is True


def test_date_passed_whitespace_tolerant():
    assert _is_reengage_followup("  date_passed ") is True


def test_other_terminal_signals_not_reengage():
    # confirmed_terminal is a completed booking (→ #6 feedback path), not a
    # passed-date re-engage; cold_decay is dormant. Neither triggers re-engage.
    assert _is_reengage_followup("confirmed_terminal") is False
    assert _is_reengage_followup("cold_decay") is False


def test_empty_or_none_signal_not_reengage():
    assert _is_reengage_followup("") is False
    assert _is_reengage_followup(None) is False


# --- review._card_draft_label -----------------------------------------------
def test_awaiting_card_keeps_draft_reply():
    # owe-reply precedence: if a (stale-signal) date_passed lead re-engaged and
    # landed back in AWAITING, the operator should reply, not "check in".
    assert _card_draft_label("AWAITING_REPLY", "date_passed") == "✍️ Draft reply"


def test_date_passed_card_is_check_in():
    # date_passed leads carry label COLD and render in NOT_A_CUSTOMER — both
    # should show the check-in verb.
    assert _card_draft_label("NOT_A_CUSTOMER", "date_passed") == "💬 Draft check-in"
    assert _card_draft_label("COLD", "date_passed") == "💬 Draft check-in"


def test_score0_supplier_card_is_nudge():
    # a score-0 supplier/spam lead in the bucket (non-date_passed) keeps nudge
    assert _card_draft_label("NOT_A_CUSTOMER", "new_window") == "💬 Draft nudge"


def test_active_card_is_nudge():
    assert _card_draft_label("HOT", "sticky_hot") == "💬 Draft nudge"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} reengage tests passed")
