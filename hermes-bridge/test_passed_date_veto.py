#!/usr/bin/env python3
"""4b booking-killer (2026-06-02): the lead analyzer has no real "today", so on
seeing "Date(s): Jun 6 / Silent for: 138h" it HALLUCINATED "Booking date Jun 6
has passed ... Event is over" and returned verdict=close — which would auto-kill
a REAL future booking (Jun 6 is 4 days after today, 2026-06-02). It conflated a
long SILENCE with the event being over.

This locks the deterministic VETO: when the analyzer's reasoning claims the
booking date has passed/is over BUT the date deterministically parses to a
strictly-FUTURE date, the close is wrong and must be vetoed. Fail-safe: only
vetoes when the date parses cleanly to the future (unparseable/relative dates,
or genuinely-past dates, are left alone so real passed-date closes still work).

Run: python3 hermes-bridge/test_passed_date_veto.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _passed_date_close_is_wrong  # noqa: E402

TODAY = datetime.date(2026, 6, 2)


def test_future_date_claimed_passed_is_vetoed():
    # The exact incident.
    assert _passed_date_close_is_wrong(
        "Jun 6", "Booking date Jun 6 has passed (138h silence). Event is over.",
        today=TODAY) is True


def test_genuinely_past_date_is_not_vetoed():
    # May 28 really is in the past on Jun 2 — a real passed-date close, keep it.
    assert _passed_date_close_is_wrong(
        "May 28", "Booking date May 28 has passed.", today=TODAY) is False


def test_no_passed_claim_is_not_vetoed():
    # A close for a real reason (price) on a future date must NOT be vetoed.
    assert _passed_date_close_is_wrong(
        "Jun 6", "customer rejected the price, not interested.",
        today=TODAY) is False


def test_unparseable_relative_date_is_not_vetoed():
    # Can't be sure -> don't veto (avoid masking a real close).
    assert _passed_date_close_is_wrong(
        "tomorrow", "the event is over now.", today=TODAY) is False


def test_empty_date_is_not_vetoed():
    assert _passed_date_close_is_wrong(
        "", "event is over.", today=TODAY) is False
    assert _passed_date_close_is_wrong(
        None, "event has already passed.", today=TODAY) is False


def test_various_passed_phrasings_on_future_date():
    for r in ("the event is moot now", "this booking has already passed",
              "the date has passed", "event is over"):
        assert _passed_date_close_is_wrong("Dec 25", r, today=TODAY) is True, r


def test_empty_reasoning_is_not_vetoed():
    assert _passed_date_close_is_wrong("Jun 6", "", today=TODAY) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} passed-date-veto tests passed")
