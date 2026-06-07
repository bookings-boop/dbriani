#!/usr/bin/env python3
"""booked_yacht display (2026-06-02): a booked customer's card should show the
single CONFIRMED yacht (✅), not the full discussed-yachts accumulator. Falls
back to the list (flagged for CONFIRMED) when booked_yacht is unset.

Run: python3 hermes-bridge/test_yacht_display.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _yacht_display  # noqa: E402


def test_booked_yacht_preferred_for_confirmed():
    # Audit #18 (2026-06-07): gate on the REAL label (row['label']), not the
    # section key — so the row must carry label='CONFIRMED'.
    r = {"label": "CONFIRMED", "booked_yacht": "Bliss 55",
         "yachts": "Bliss 55, Sunseeker Satoshi 70, Pershing 82"}
    assert _yacht_display(r, "CONFIRMED") == "✅ Bliss 55"


def test_multi_yacht_confirmed_without_booked_is_flagged():
    out = _yacht_display(
        {"label": "CONFIRMED", "yachts": "Azimut 62, Sunseeker Satoshi 70"},
        "CONFIRMED")
    assert "Azimut 62, Sunseeker Satoshi 70" in out
    assert "confirm" in out.lower()


def test_confirmed_owed_routed_to_awaiting_still_flags():
    # Audit #18: a CONFIRMED+owed booking is rendered in the AWAITING_REPLY
    # section (label_key != CONFIRMED) but must STILL show ✅ / multi-yacht ⚠️.
    r = {"label": "CONFIRMED", "booked_yacht": "Bliss 55", "yachts": "x"}
    assert _yacht_display(r, "AWAITING_REPLY") == "✅ Bliss 55"
    multi = _yacht_display(
        {"label": "CONFIRMED", "yachts": "Azimut 62, Satoshi 70"},
        "AWAITING_REPLY")
    assert "confirm" in multi.lower()


def test_single_yacht_no_flag():
    assert _yacht_display({"yachts": "Royalty 136"}, "HOT") == "Royalty 136"


def test_no_yacht():
    assert _yacht_display({"yachts": ""}, "NEW") == "no yacht set"


def test_booked_yacht_noncon_shown_plain():
    assert _yacht_display({"booked_yacht": "Bliss 55", "yachts": "x"}, "HOT") == "Bliss 55"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} yacht-display tests passed")
