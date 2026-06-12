#!/usr/bin/env python3
"""Post-payment render — Bug #4 (Виолетта/Violetta, 2026-06-12).

A CONFIRMED + paid booking whose cached analysis PREDATES the payment showed a
stale pre-payment action line ("Reply NOW — confirm payment link is live, guide
them to complete it immediately") because payment events never trigger
re-analysis. F-A: when a payment landed after the analysis ran, suppress the
stale suggested_action and render post-payment logistics guidance. F-C: label
the gross-charged amount (1927.8 = 1800 × 1.071 processor fee) against the
agreed minted price (1800) instead of conflating the two.

Both gated behind REVIEW_POSTPAY_OVERRIDE_ENABLED; off → render unchanged.

Run: python3 hermes-bridge/test_postpay_render.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review  # noqa: E402
from review import _why_line, _booking_detail_line  # noqa: E402

# Far-future date so the CONFIRMED event_passed / stale-relative branches don't
# short-circuit before the post-payment override is reached.
_FUTURE = "Dec 31 2099"


def _paid_row(**ov):
    base = {
        "label": "CONFIRMED", "dates": _FUTURE,
        "suggested_action": "Reply NOW — confirm payment link is live, guide "
                            "them to complete it immediately.",
        "importance_analyzed_at_seconds": 7200,  # analysis ran 2h ago
        "paid_at_seconds": 3600,                  # payment landed 1h ago (AFTER)
        "paid_amount": "AED 1927.8", "minted_amount": "1800.0",
    }
    base.update(ov)
    return base


def test_postpay_override_off_keeps_suggested_action():
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False
    row = _paid_row()
    assert _why_line(row, "CONFIRMED") == row["suggested_action"]


def test_postpay_override_on_suppresses_stale_pre_payment_action():
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        out = _why_line(_paid_row(), "CONFIRMED")
        assert "payment link" not in out.lower(), out
        assert "Reply NOW" not in out, out
        assert "logistics" in out.lower(), out
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


def test_postpay_override_fires_on_tight_5s_gap():
    # Violetta's real shape: payment landed exactly 5s AFTER the analysis ran.
    # The override must still fire (the analysis is pre-payment advice).
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        row = _paid_row(importance_analyzed_at_seconds=3605, paid_at_seconds=3600)
        out = _why_line(row, "CONFIRMED")
        assert "logistics" in out.lower(), out
        assert "payment link" not in out.lower(), out
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


def test_postpay_no_override_when_simultaneous():
    # Analysis and payment within the tie buffer (~same second) → no override.
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        row = _paid_row(importance_analyzed_at_seconds=3600, paid_at_seconds=3600)
        assert _why_line(row, "CONFIRMED") == row["suggested_action"]
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


def test_postpay_no_override_when_analysis_is_fresh():
    # Analysis ran AFTER the payment (imp 10min ago, payment 1h ago) → the cached
    # advice is already post-payment-aware; keep the suggested_action.
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        row = _paid_row(importance_analyzed_at_seconds=600, paid_at_seconds=3600)
        assert _why_line(row, "CONFIRMED") == row["suggested_action"]
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


def test_amount_label_off_is_plain_gross():
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False
    det = _booking_detail_line(
        {"booked_yacht": "Elise 50", "paid_amount": "AED 1927.8",
         "minted_amount": "1800.0"})
    assert "💰 AED 1927.8 paid" in det, det
    assert "incl. card fee" not in det, det


def test_amount_label_on_splits_price_and_gross():
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        det = _booking_detail_line(
            {"booked_yacht": "Elise 50", "paid_amount": "AED 1927.8",
             "minted_amount": "1800.0"})
        assert "AED 1800 paid" in det, det
        assert "AED 1927.8 incl. card fee" in det, det
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


def test_amount_label_equal_no_fee_annotation():
    # Gross == minted (no processor fee) → plain line, no "(incl. card fee)".
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        det = _booking_detail_line(
            {"booked_yacht": "Elise 50", "paid_amount": "AED 1800",
             "minted_amount": "1800.0"})
        assert "incl. card fee" not in det, det
        assert "💰 AED 1800 paid" in det, det
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


def test_amount_label_on_no_minted_falls_back_plain():
    review.REVIEW_POSTPAY_OVERRIDE_ENABLED = True
    try:
        det = _booking_detail_line(
            {"booked_yacht": "Elise 50", "paid_amount": "AED 3534.3",
             "minted_amount": ""})
        assert "💰 AED 3534.3 paid" in det, det
        assert "incl. card fee" not in det, det
    finally:
        review.REVIEW_POSTPAY_OVERRIDE_ENABLED = False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} post-payment render tests passed")
