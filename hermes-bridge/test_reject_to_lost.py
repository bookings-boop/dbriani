#!/usr/bin/env python3
"""Bug B (Marc 2026-06-06): a customer who declined ('no thanks') AND wanted a
bareboat/self-drive charter (which Dubriani doesn't offer) stayed labelled HOT
'push toward booking'. compute_label now routes a CLEAR rejection or an explicit
bareboat ask to LOST (operator decision: auto-demote -> LOST). Tight guards keep
an engaged 'no thanks, what about Saturday?' / 'not interested in the bigger one'
from being mis-LOST, and never demote an already-CONFIRMED (paid) booking.

Run: python3 hermes-bridge/test_reject_to_lost.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
from server import compute_label  # noqa: E402

# compute_label reads the DB (payment intent/link) further down the cascade —
# stub those out so this pure-logic test runs locally without docker/psql.
server._has_recent_payment_intent = lambda *a, **k: False
server._has_recent_payment_link_sent = lambda *a, **k: False


def f(label="HOT", **ov):
    d = {"customer_id": "x@lid", "message_count": 3, "label": label,
         "yachts": "", "dates": ""}
    d.update(ov)
    return d


def test_clear_no_thanks_to_lost():
    lab, sig, _ = compute_label("no thanks", f())
    assert lab == "LOST" and sig == "declined", (lab, sig)


def test_not_interested_to_lost():
    assert compute_label("not interested", f())[0] == "LOST"


def test_booked_elsewhere_to_lost():
    assert compute_label("we booked elsewhere, thanks", f())[0] == "LOST"


def test_bareboat_self_driving_to_lost():
    lab, sig, _ = compute_label("Self driving", f())
    assert lab == "LOST" and sig == "service_mismatch", (lab, sig)


def test_no_captain_to_lost():
    assert compute_label("we don't want a captain", f())[0] == "LOST"


# --- false-positive guards (must NOT go LOST) -------------------------------
def test_engaged_no_thanks_not_lost():
    assert compute_label("no thanks, what about Saturday?", f())[0] != "LOST"


def test_not_interested_in_X_not_lost():
    assert compute_label(
        "not interested in the bigger one, the Von Dutch is fine", f())[0] != "LOST"


def test_confirmed_lead_no_thanks_not_lost():
    assert compute_label("no thanks", f(label="CONFIRMED"))[0] != "LOST"


def test_license_question_alone_not_lost():
    # ambiguous 'need a license?' is answerable (we're crewed) -> not auto-LOST
    assert compute_label("do I need a boat driver license?", f())[0] != "LOST"


def test_marc_same_day_question_still_hot():
    lab, _, _ = compute_label(
        "Do you have one boat available today at 2:30pm?", f(yachts="Bliss 55"))
    assert lab == "HOT", lab


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} reject_to_lost tests passed")
