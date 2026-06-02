#!/usr/bin/env python3
"""Unit tests for same-day time-aware slot-passed detection (2026-06-02).

Bug (operator, Lili & Marina): a customer asks for a slot TODAY at a time that
has already elapsed (e.g. "Jun 2, 5 PM" when it is now 7 PM Dubai). The old
passed-date logic compared DATES only (and required >=1 full day past), so a
same-day-but-time-elapsed slot was never flagged — the drafter kept "confirming
logistics" instead of gracefully pivoting to a future day.

_parse_booking_time: pull the slot START (earliest clock time) as minutes since
midnight, from the dates/reasoning free-text. None when no clock time present.
slot_passed: True when the requested slot is in the past relative to a Dubai
'now' — prior calendar day, OR same day with a parseable start time <= now.
Conservative: same-day with no parseable time, or unparseable date -> False.

Run: python3 hermes-bridge/test_slot_passed.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _parse_booking_time, slot_passed, _dubai_now  # noqa: E402


def N(h, mi=0):
    """A Dubai 'now' on Jun 2 2026 at h:mi."""
    return dt.datetime(2026, 6, 2, h, mi)


# --- _parse_booking_time: returns START minutes-since-midnight, or None -------
def test_time_single_12h():
    assert _parse_booking_time("May 27, 5 PM") == 17 * 60


def test_time_single_with_minutes():
    assert _parse_booking_time("Nov 20, 1:15 PM") == 13 * 60 + 15


def test_time_am():
    assert _parse_booking_time("tomorrow at 9am") == 9 * 60


def test_time_range_pm_inherits_meridian():
    # "6–8 PM": the 6 inherits PM from the 8 PM -> start 18:00
    assert _parse_booking_time("Sun May 24, 6–8 PM") == 18 * 60


def test_time_range_both_explicit():
    assert _parse_booking_time("Jun 7, 5:30–8:30pm") == 17 * 60 + 30


def test_time_24h_range():
    assert _parse_booking_time("17:30–20:30") == 17 * 60 + 30


def test_time_24h_single_paren():
    assert _parse_booking_time("Sun May 31 (18:00, 3 hrs)") == 18 * 60


def test_time_none_when_date_only():
    assert _parse_booking_time("Jun 2") is None
    assert _parse_booking_time("Tue Jun 2") is None


def test_time_none_when_empty():
    assert _parse_booking_time("") is None
    assert _parse_booking_time(None) is None


def test_time_ignores_bare_relative_numbers():
    # "1 min ago" / "70h ago" must not be read as a clock time
    assert _parse_booking_time("replied 1 min ago, 70h since") is None


# --- slot_passed -------------------------------------------------------------
def test_prior_day_passed():
    assert slot_passed("May 27, 5 PM", now=N(10)) is True


def test_future_day_not_passed():
    assert slot_passed("Jun 7, 5:30–8:30pm", now=N(22)) is False


def test_sameday_time_passed_from_dates():
    # operator's exact example: asked for 5 PM, now 7 PM -> passed
    assert slot_passed("Jun 2, 5 PM", now=N(19)) is True


def test_sameday_time_passed_from_reasoning():
    # Lili: dates="Jun 2" (no time), time lives in the analyzer reasoning
    assert slot_passed(
        "Jun 2",
        reasoning="Rule 6: booking is TODAY (Jun 2), 17:30-20:30, engaged.",
        now=N(20, 32)) is True


def test_sameday_time_not_yet_passed():
    # evening slot still upcoming -> NOT passed (don't false-pivot)
    assert slot_passed("Jun 2, 9 PM", now=N(18)) is False


def test_sameday_no_time_is_conservative_false():
    # Marina: same day, no parseable time -> we don't guess deterministically
    assert slot_passed("Tue Jun 2", now=N(22)) is False


def test_unparseable_date_not_passed():
    assert slot_passed("Friday night (10:30 PM, 3 hrs)", now=N(23)) is False


def test_none_not_passed():
    assert slot_passed(None, now=N(12)) is False
    assert slot_passed("", now=N(12)) is False


def test_sameday_exact_minute_boundary_not_passed():
    # now == start: the slot is starting right now, not passed yet
    assert slot_passed("Jun 2, 8 PM", now=N(20, 0)) is False


# --- _dubai_now: UTC+4, no DST ----------------------------------------------
def test_dubai_now_is_utc_plus_4():
    delta = _dubai_now() - dt.datetime.utcnow()
    secs = abs(delta.total_seconds() - 4 * 3600)
    assert secs < 120, f"_dubai_now off by {secs}s from UTC+4"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} slot-passed tests passed")
