#!/usr/bin/env python3
"""Deterministic guardrail over the (LLM) Hermes lead-analysis — catches the
known-wrong patterns the analyzer keeps producing (scam scored LOST, vendor vs
lost-sale conflation, garbage off empty history, stale relative dates) so they
don't propagate into labels / sort / display. 2026-06-07.

Run: python3 hermes-bridge/test_analysis_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis_guard import (  # noqa: E402
    is_scam, reclassify_close_label, is_analysis_unreliable,
    is_stale_relative_date,
)


def test_scam_detected():
    for r in ("Customer is a crypto investment scam — USDT doubling scheme",
              "romance scam / advance fee fraud",
              "bitcoin airdrop phishing link"):
        assert is_scam(r) is True, r


def test_non_scam_not_flagged():
    for r in ("Customer said too expensive", "booked elsewhere",
              "vendor pitch — catering supplier", "ghosted after pricing"):
        assert is_scam(r) is False, r


def test_reclassify_routes_scam_vendor_lost():
    assert reclassify_close_label("crypto USDT investment scam") == "SCAM"
    assert reclassify_close_label("vendor pitch, marketing agency") == "DISREGARDED"
    assert reclassify_close_label("too expensive / out of budget") == "LOST"
    assert reclassify_close_label("found another one, booked elsewhere") == "LOST"
    assert reclassify_close_label("") == "LOST"           # unclear → never 'not a customer'


def test_unreliable_when_analyzer_claims_empty_but_history_exists():
    # Émilie: 157 msgs but "first contact, no prior messages" → can't be trusted.
    assert is_analysis_unreliable(157, 33, "First contact, no prior messages") is True
    assert is_analysis_unreliable(65, 5, "Customer ghosted") is True   # substantial lead, ~no history fetched


def test_reliable_when_history_present():
    assert is_analysis_unreliable(22, 1776, "Customer chose Bliss 55") is False
    assert is_analysis_unreliable(1, 0, "First contact") is False      # genuinely new lead


def test_stale_relative_date():
    assert is_stale_relative_date("tomorrow 4-7 PM", 10.0) is True
    assert is_stale_relative_date("this weekend", 3.0) is True
    assert is_stale_relative_date("Friday", 2.0) is True
    assert is_stale_relative_date("2026-06-25", 10.0) is False         # absolute date — fine
    assert is_stale_relative_date("tomorrow", 0.2) is False            # set just now — still valid


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} analysis-guard tests passed")
