#!/usr/bin/env python3
"""5e (2026-06-02): real LOST leads (price / booked-elsewhere / cancelled /
ghosted) were bucketed together with vendors/spam under "not a customer". The
analyzer already distinguishes them in its reasoning text (RULE 1/2 = vendor/
spam; RULE 4 = lost), so this pure classifier splits a closed lead's reasoning
into LOST (with a reason) vs a true NOT_A_CUSTOMER, for the /review render split.

Operator decision (2026-06-02): Lost-with-reason (price/competitor/timing/
ghosted) + a separate Not-a-customer bucket.

Run: python3 hermes-bridge/test_close_bucket.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _close_bucket, _close_label_for  # noqa: E402
# reclassify_close_label is the function wired into handle_lead_analyze_disregard's
# verdict=='close' branch (FIX-GROUP 3 / H5): a strict SUPERSET of _close_label_for
# that adds the SCAM route on top of the existing DISREGARDED/LOST logic.
from analysis_guard import reclassify_close_label  # noqa: E402


def test_close_label_routes_real_losses_to_LOST():
    # Operator/audit 2026-06-07: 95 leads were DISREGARDED ("not a customer")
    # that were really lost SALES. A 'close' verdict on price/competitor/timing/
    # ghost = LOST, never DISREGARDED.
    assert _close_label_for("Rule 4: too expensive / out of budget") == "LOST"
    assert _close_label_for("customer found another one, booked elsewhere") == "LOST"
    assert _close_label_for("Booking date passed; customer ghosted, no reply") == "LOST"


def test_close_label_keeps_noncustomers_DISREGARDED():
    assert _close_label_for("vendor pitching marketing agency services") == "DISREGARDED"
    assert _close_label_for("spam / wrong number") == "DISREGARDED"


def test_close_label_unclear_defaults_to_LOST():
    # Never brand a real customer 'not a customer' on ambiguous reasoning.
    assert _close_label_for("") == "LOST"
    assert _close_label_for("customer went quiet, unclear next step") == "LOST"


def test_completed_booking_is_not_lost_or_noncustomer():
    # Tal Sudai: "booking fully executed 8 days ago" — a WON booking, must
    # NOT read as "not a customer".
    for r in ("booking fully executed 8 days ago",
              "trip completed last week, great feedback",
              "charter done — successfully delivered"):
        b, label = _close_bucket(r)
        assert b == "COMPLETED" and "completed" in label.lower(), r


def test_vendor_is_not_a_customer():
    b, label = _close_bucket("vendor pitching yacht maintenance services to us")
    assert b == "NOT_A_CUSTOMER"


def test_spam_is_not_a_customer():
    assert _close_bucket("spam / wrong number, not a real enquiry")[0] == \
        "NOT_A_CUSTOMER"


def test_price_rejection_is_lost():
    b, label = _close_bucket("repeated price rejection, said too expensive")
    assert b == "LOST" and "price" in label.lower()


def test_booked_elsewhere_is_lost_competitor():
    b, label = _close_bucket("customer booked elsewhere with another operator")
    assert b == "LOST" and ("competitor" in label.lower()
                            or "elsewhere" in label.lower())


def test_cancelled_is_lost_timing():
    b, label = _close_bucket("cancelled the trip, changed plans")
    assert b == "LOST" and ("cancel" in label.lower() or "timing" in label.lower())


def test_ghosted_is_lost_no_response():
    b, label = _close_bucket("ghosted 14+ days after the quote, no reply")
    assert b == "LOST" and ("ghost" in label.lower() or "response" in label.lower()
                            or "silent" in label.lower())


def test_unclear_returns_none():
    b, label = _close_bucket("closed for an unspecified reason")
    assert b == ""


def test_empty():
    assert _close_bucket("") == ("", "")
    assert _close_bucket(None) == ("", "")


# --- H5: disregard close-routing now goes through reclassify_close_label -----
def test_disregard_close_routes_crypto_scam_to_SCAM():
    # Mike (244221151858858@lid): "Classic crypto refund scam ... requested USDT
    # address ... textbook fraud." The OLD path (labels._close_label_for) had no
    # scam route → it fell to LOST and Mike was shown as a 'legit prospect to win
    # back'. The disregard close branch now uses reclassify_close_label → SCAM.
    assert reclassify_close_label("Classic crypto USDT refund scam") == "SCAM"
    assert reclassify_close_label(
        "requested wallet address — textbook advance-fee fraud") == "SCAM"


def test_reclassify_is_strict_superset_of_close_label_for():
    # No regression: for every NON-scam reasoning the wired router must return
    # the SAME terminal label as the previously-shipped _close_label_for
    # (DISREGARDED for vendor/spam/completed, LOST for real-but-lost + unclear).
    for r in ("vendor pitching marketing agency services",
              "spam / wrong number",
              "Rule 4: too expensive / out of budget",
              "customer found another one, booked elsewhere",
              "ghosted 14+ days after the quote, no reply",
              "booking fully executed 8 days ago",
              "",
              "customer went quiet, unclear next step"):
        assert reclassify_close_label(r) == _close_label_for(r), r


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} close-bucket tests passed")
