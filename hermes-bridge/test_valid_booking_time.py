#!/usr/bin/env python3
"""Unit tests for labels._valid_booking_time() — reject a malformed booking_time
where a 12-hour meridian (am/pm) is attached to an hour outside 1-12.

Bug (Antonio, 2026-06-08): booking_time stored as '30PM' (a '5:30PM' that lost
its hour). The validator must drop clearly-bad meridian hours while NEVER
dropping a valid time (incl. ranges and ':MM' clocks).

Run: python3 hermes-bridge/test_valid_booking_time.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _valid_booking_time as V  # noqa: E402


def test_rejects_30pm():
    assert V("30PM") == ""            # the Antonio bug


def test_rejects_13pm_and_0pm_and_25():
    assert V("13PM") == ""
    assert V("0PM") == ""
    assert V("25 p.m.") == ""


def test_keeps_valid_single():
    assert V("5PM") == "5PM"
    assert V("12PM") == "12PM"
    assert V("9AM") == "9AM"
    assert V("5:30PM") == "5:30PM"    # ':MM' must NOT be read as the hour
    assert V("12:30AM") == "12:30AM"


def test_keeps_valid_ranges():
    assert V("5-9PM") == "5-9PM"
    assert V("2PM-6PM") == "2PM-6PM"
    assert V("4-7pm") == "4-7pm"


def test_keeps_24h_and_unparseable():
    assert V("17:30") == "17:30"      # 24h, no meridian -> untouched
    assert V("17:00-21:00") == "17:00-21:00"


def test_empty_and_none_safe():
    assert V("") == ""
    assert V(None) == ""
    assert V("   ") == ""


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} valid_booking_time tests passed")
