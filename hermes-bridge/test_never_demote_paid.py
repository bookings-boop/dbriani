#!/usr/bin/env python3
"""AREA B — HARD never-demote-a-won/paid rule (2026-06-07).

handle_pipeline_analyze's auto-COLD path demotes a lead when the analyzer
returns a 'close' verdict OR importance_score 0. The RCA (954c52b8 spec) found
11 "demote BLOCKED" saves in 5 days where a None / unreliable / score-0 analysis
would otherwise have demoted a lead that was ALREADY booked/paid — the lead only
survived by luck (the _demote_to_cold_blocked elif happening to fire).

This makes it a HARD deterministic rule: a won / in-flight / paid lead
(CONFIRMED, WAITING_FOR_PAYMENT, ever-booked, or with payment_received
evidence) is CATEGORICALLY protected from a degraded-analysis demotion — never
the lucky save. `_won_or_paid_protected` is the pure gate; it runs BEFORE the
other (heuristic) demote guards.

Run: python3 hermes-bridge/test_never_demote_paid.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from routes import _won_or_paid_protected  # noqa: E402


def test_confirmed_label_categorically_protected():
    # The flagship case: a CONFIRMED + score-0 row must STAY CONFIRMED — never
    # demoted to COLD on a degraded/score-0 analysis. Protection is categorical,
    # not contingent on ever_booked/paid evidence being detectable.
    assert _won_or_paid_protected("CONFIRMED", False, False) is True


def test_waiting_for_payment_categorically_protected():
    # An in-flight (paylink sent, awaiting payment) lead must not be auto-COLD'd
    # off a degraded/score-0 analysis.
    assert _won_or_paid_protected("WAITING_FOR_PAYMENT", False, False) is True


def test_ever_booked_protected_even_in_active_label():
    # A lead currently HOT (e.g. re-engaged / extending) that EVER reached a
    # booked/paid label is protected — this is the case the demote path actually
    # reaches (CONFIRMED/WAITING never enter the NEW/WARM/HOT/NEEDS demote set).
    assert _won_or_paid_protected("HOT", True, False) is True


def test_payment_received_evidence_protected():
    # Any recorded payment_received evidence protects the lead regardless of its
    # current sticky label.
    assert _won_or_paid_protected("WARM", False, True) is True


def test_fresh_active_lead_not_protected():
    # A genuinely never-booked, never-paid active lead is NOT protected — the
    # normal demote heuristics still apply (we don't break auto-COLD for real
    # dead leads).
    assert _won_or_paid_protected("HOT", False, False) is False
    assert _won_or_paid_protected("NEW", False, False) is False
    assert _won_or_paid_protected("WARM", False, False) is False


def test_none_and_blank_label_safe():
    # None-safe / blank-safe: no label + no evidence => not protected (no crash).
    assert _won_or_paid_protected(None, False, False) is False
    assert _won_or_paid_protected("", None, None) is False
    # case-insensitive on the label.
    assert _won_or_paid_protected("confirmed", False, False) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} never-demote-paid tests passed")
