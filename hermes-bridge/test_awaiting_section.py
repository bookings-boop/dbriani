#!/usr/bin/env python3
"""Unit tests for _awaiting_section_for() — the /review section router.

Decides whether a (non-paused, valid-label) lead belongs in:
  - 'AWAITING_REPLY'  : we owe a reply to a GENUINE active prospect
  - 'NOT_A_CUSTOMER'  : analyzer score 0 (supplier/spam/done) OR a FRESH
                        terminal verdict on an owed-reply lead (#2 fix)
  - ''                : render in its own label section (default)

#2 fix (2026-06-01): an owed-reply lead the analyzer has FRESHLY judged
terminal (confirmed_terminal / date_passed) must leave AWAITING for the
💤 NO ACTIVE SALE bucket — UNLESS the customer messaged after that verdict
(then they're re-engaging and stay owed). cold_decay is dormant, NOT terminal.

Plain-assert style. Run: python3 hermes-bridge/test_awaiting_section.py

Seconds fields are AGE-in-seconds: smaller = more recent.
  owe  = customer msg more recent than our last reply/nudge  (_cs < _out)
  fresh terminal = analysis at least as recent as the customer msg (_anz <= _cs)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _awaiting_section_for, _no_sale_reason  # noqa: E402


def _r(label="HOT", **overrides):
    base = {
        "label": label,
        "importance_score": 50,
        "last_customer_message_at_seconds": None,
        "last_operator_reply_at_seconds": None,
        "last_nudge_drafted_at_seconds": None,
        "last_analysis_signal": "",
        "last_analyzed_at_seconds": None,
    }
    base.update(overrides)
    return base


# --- the #2 fix: fresh terminal verdict on an owed lead -> NO ACTIVE SALE ---
def test_fresh_date_passed_owe_goes_to_not_a_customer():
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=3600,   # customer msg 1h ago
             last_operator_reply_at_seconds=7200,      # we replied 2h ago -> owe
             last_analysis_signal="date_passed",
             last_analyzed_at_seconds=1800)            # analysed 0.5h ago -> fresh
    assert _awaiting_section_for(row, 800) == "NOT_A_CUSTOMER"


def test_fresh_confirmed_terminal_owe_goes_to_not_a_customer():
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 800) == "NOT_A_CUSTOMER"


# --- freshness guard: customer messaged AFTER the terminal verdict ----------
def test_stale_terminal_verdict_stays_in_awaiting():
    # analysed 2h ago, customer messaged 0.5h ago -> they re-engaged -> AWAITING
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=1800,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=7200)
    assert _awaiting_section_for(row, 800) == "AWAITING_REPLY"


def test_terminal_signal_without_analysis_time_stays_in_awaiting():
    # no last_analyzed_at -> cannot trust the verdict -> AWAITING
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="date_passed",
             last_analyzed_at_seconds=None)
    assert _awaiting_section_for(row, 800) == "AWAITING_REPLY"


# --- unchanged behaviour (regression guards) --------------------------------
def test_active_owed_lead_goes_to_awaiting():
    row = _r("HOT", importance_score=80,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="sticky_hot",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 800) == "AWAITING_REPLY"


def test_cold_decay_is_not_terminal_owe_stays_awaiting():
    row = _r("COLD", importance_score=40,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="cold_decay",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 200) == "AWAITING_REPLY"


def test_score_zero_owe_goes_to_not_a_customer():
    row = _r("NEW", importance_score=0,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="new_window",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 300) == "NOT_A_CUSTOMER"


def test_score_zero_not_owe_still_not_a_customer():
    # score-0 always bucketed, owe or not (matches pre-fix behaviour)
    row = _r("COLD", importance_score=0,
             last_customer_message_at_seconds=7200,
             last_operator_reply_at_seconds=3600)
    assert _awaiting_section_for(row, 200) == "NOT_A_CUSTOMER"


def test_confirmed_stays_in_own_section():
    row = _r("CONFIRMED", importance_score=90,
             last_customer_message_at_seconds=3600,
             last_operator_reply_at_seconds=7200,
             last_analysis_signal="confirmed_terminal",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 5000) == ""


def test_non_owe_terminal_lead_is_untouched():
    # not owed (we replied more recently than the customer) + terminal -> stays
    # in its own tier; the fix only diverts OWED leads (scope guard).
    row = _r("HOT", importance_score=57,
             last_customer_message_at_seconds=7200,
             last_operator_reply_at_seconds=3600,
             last_analysis_signal="date_passed",
             last_analyzed_at_seconds=1800)
    assert _awaiting_section_for(row, 800) == ""


# --- 💤 NO ACTIVE SALE bucket reason line -----------------------------------
def test_no_sale_reason_date_passed():
    row = _r(last_analysis_signal="date_passed",
             importance_reasoning="HOT — wants AK Royalty next month")
    # terminal signal wins over stale positive reasoning
    assert _no_sale_reason(row) == "booking date has already passed"


def test_no_sale_reason_confirmed_terminal():
    row = _r(last_analysis_signal="confirmed_terminal", importance_reasoning="")
    assert _no_sale_reason(row) == "booking completed — no open sale"


def test_no_sale_reason_falls_back_to_analyzer_reasoning():
    # a score-0 supplier (non-terminal signal) keeps its analyzer reasoning
    row = _r(last_analysis_signal="new_window",
             importance_reasoning="Alma is a supplier (Fruitful Day), no booking intent")
    assert _no_sale_reason(row) == "Alma is a supplier (Fruitful Day), no booking intent"


def test_no_sale_reason_default_when_no_reasoning():
    row = _r(last_analysis_signal="", importance_reasoning="")
    assert _no_sale_reason(row) == "analyzer scored 0 — no open sale"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} _awaiting_section_for tests passed")
