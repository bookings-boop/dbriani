#!/usr/bin/env python3
"""Unit tests for _is_feedback_due() — #6-auto-A completed-trip detector.

The daily feedback sweep cards a CONFIRMED booking ONCE its trip date has just
passed (ended 1..window_days ago — the window forgives missed cron days).
Same-day and future trips are not yet due; older trips are out of window
(a per-lead Redis dedup also guarantees one card per booking).

Plain-assert style. Run: python3 hermes-bridge/test_feedback_due.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _is_feedback_due  # noqa: E402

TODAY = datetime.date(2026, 6, 1)


def test_trip_yesterday_is_due():
    assert _is_feedback_due(datetime.date(2026, 5, 31), TODAY) is True


def test_trip_within_window_is_due():
    # default window 3 days → trip 3 days ago still due (forgives 2 missed crons)
    assert _is_feedback_due(datetime.date(2026, 5, 29), TODAY) is True


def test_trip_outside_window_not_due():
    assert _is_feedback_due(datetime.date(2026, 5, 28), TODAY) is False


def test_trip_today_not_due():
    # trip hasn't ended yet today → not a post-trip check-in
    assert _is_feedback_due(datetime.date(2026, 6, 1), TODAY) is False


def test_future_trip_not_due():
    assert _is_feedback_due(datetime.date(2026, 6, 2), TODAY) is False


def test_none_date_not_due():
    assert _is_feedback_due(None, TODAY) is False


def test_custom_window_boundary():
    assert _is_feedback_due(datetime.date(2026, 5, 25), TODAY, window_days=7) is True
    assert _is_feedback_due(datetime.date(2026, 5, 24), TODAY, window_days=7) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} feedback_due tests passed")
