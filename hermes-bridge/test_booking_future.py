#!/usr/bin/env python3
"""4b follow-up (2026-06-02): leads with a FUTURE booking date were still shown
in /review's NOT_A_CUSTOMER bucket as "Jun 4 / Jun 6 has passed". Root cause:
the analyzer CACHED a wrong close (score 0) BEFORE the 4b veto deployed, and the
/review router buckets any score-0 lead as not-a-customer. A future booking date
must deterministically override a stale cached close — an upcoming booking is an
ACTIVE lead, never "not a customer". This locks the pure future-date check used
by that render guard.

Run: python3 hermes-bridge/test_booking_future.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _booking_date_is_future  # noqa: E402

TODAY = datetime.date(2026, 6, 2)


def test_future_dates_true():
    for d in ("Jun 4", "Jun 6", "Thu Jun 4", "Fri Jun 6", "Dec 25"):
        assert _booking_date_is_future(d, today=TODAY) is True, d


def test_past_dates_false():
    # Recent-past dates stay in the current year (within ~60d). NOTE: a date
    # >60d "past" (e.g. "Jan 3" in June) is inferred as NEXT year by
    # _parse_booking_date — i.e. a FUTURE booking — which is the safe reading
    # (never wrongly close a lead), so it is intentionally not tested as past.
    for d in ("May 28", "Sat May 23", "Apr 20"):
        assert _booking_date_is_future(d, today=TODAY) is False, d


def test_today_is_not_future():
    assert _booking_date_is_future("Jun 2", today=TODAY) is False


def test_unparseable_false():
    for d in ("tomorrow", "this weekend", "", None, "end of month"):
        assert _booking_date_is_future(d, today=TODAY) is False, d


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} booking-future tests passed")
