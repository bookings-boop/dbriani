#!/usr/bin/env python3
"""AREA B — itemised BOOKING-DETAIL line for CONFIRMED / paid leads (2026-06-07).

Xeno incident: a paid CONFIRMED booking rendered a vague "booked & paid" line
with no date / time / yacht / add-ons / amount — the operator complained "paid
but not showing the date they booked and timings". `_booking_detail_line(row)`
builds the itemised line from the structured booking facts, defensively:
shows whatever is present, prefers the normalized absolute date + booked yacht,
NEVER fabricates, and degrades to '' when nothing is known.

Run: python3 hermes-bridge/test_booking_detail.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _booking_detail_line  # noqa: E402


def test_all_fields_present_itemised():
    line = _booking_detail_line({
        "booked_yacht": "Bliss 55",
        "booking_date_abs": "2026-06-20",
        "booking_time": "4–7 PM",
        "party_size": "12 guests",
        "addons": "DJ, photographer",
        "paid_amount": "AED 3534.3",
    })
    # every itemised field surfaces.
    assert "Bliss 55" in line, line
    assert "2026-06-20" in line, line
    assert "4–7 PM" in line, line
    assert "12 guests" in line, line
    assert "DJ, photographer" in line, line
    assert "AED 3534.3" in line, line
    assert "paid" in line, line


def test_absolute_date_preferred_over_relative():
    # When the normalized absolute date is present it is shown instead of the
    # raw (possibly stale relative) dates string.
    line = _booking_detail_line({
        "booked_yacht": "Bliss 55",
        "dates": "tomorrow 4–7 PM",
        "booking_date_abs": "2026-06-20",
    })
    assert "2026-06-20" in line, line
    assert "tomorrow" not in line, line


def test_booked_yacht_preferred_over_accumulator():
    line = _booking_detail_line({
        "booked_yacht": "Bliss 55",
        "yachts": "Bliss 55, Mila 141, Azimut 62",
    })
    assert "Bliss 55" in line, line
    assert "Mila 141" not in line, line


def test_partial_fields_degrade_gracefully():
    # Only yacht + amount known — show those, no fabricated date/time/add-ons.
    line = _booking_detail_line({
        "booked_yacht": "Azimut 62",
        "paid_amount": "AED 5000",
    })
    assert "Azimut 62" in line, line
    assert "AED 5000" in line, line
    # nothing invented for the missing fields.
    assert "PM" not in line and "guests" not in line, line


def test_falls_back_to_raw_dates_and_yachts_when_no_structured():
    # No absolute date / booked_yacht captured yet — fall back to the raw
    # accumulator fields rather than showing nothing.
    line = _booking_detail_line({
        "yachts": "Sunseeker Satoshi 70",
        "dates": "Jun 20",
    })
    assert "Sunseeker Satoshi 70" in line, line
    assert "Jun 20" in line, line


def test_absent_everything_is_empty():
    assert _booking_detail_line({}) == ""
    assert _booking_detail_line(None) == ""
    assert _booking_detail_line({
        "booked_yacht": "", "dates": "", "paid_amount": "",
        "booking_time": "", "addons": "", "booking_date_abs": "",
    }) == ""


def test_never_fabricates_amount_when_absent():
    line = _booking_detail_line({"booked_yacht": "Bliss 55"})
    assert "paid" not in line, line
    assert "AED" not in line, line


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} booking-detail tests passed")
