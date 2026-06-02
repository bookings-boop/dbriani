#!/usr/bin/env python3
"""Unit tests for _passed_date_note() — passed-date awareness for the drafter
and scorer (2026-06-01).

Marimuthu incident: the analyzer told the drafter to "ask for the date/
occasion" and the scorer flagged a correct graceful-exit draft as "7/10 —
ignores known date" — both because they didn't check the actual (passed)
booking date. This note, injected into _lead_state_block (drafter) and
build_quality_query (scorer), tells both that a forward-looking graceful exit
is the CORRECT reply for a passed date and must not be penalised.

Run: python3 hermes-bridge/test_passed_date_note.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _passed_date_note  # noqa: E402


def test_past_date_gives_graceful_guidance():
    n = _passed_date_note("Jan 1 2020").lower()
    assert "passed" in n and "graceful" in n and "future" in n


def test_future_date_no_note():
    assert _passed_date_note("Dec 31 2099") == ""


def test_unparseable_date_no_note():
    # free-text with no day/month can't be confirmed past → no note (don't guess)
    assert _passed_date_note("Friday night (10:30 PM, 3 hrs)") == ""


def test_empty_no_note():
    assert _passed_date_note("") == ""
    assert _passed_date_note(None) == ""


# --- reasoning-based trigger (relative dates the parser can't resolve) -------
def test_reasoning_says_passed_fires_even_when_unparseable():
    # "today" can't parse, but the analyzer's reasoning says it passed (Elise)
    n = _passed_date_note(
        "today",
        "Booking date was 'today' 7 days ago — that date has passed. "
        "Lead is moot; no future date discussed.").lower()
    assert "graceful" in n and "passed" in n


def test_unparseable_without_passed_reasoning_no_note():
    assert _passed_date_note("today", "") == ""
    assert _passed_date_note("today", "customer wants a quote this week") == ""


def test_future_with_neutral_reasoning_no_note():
    assert _passed_date_note("Dec 31 2099", "keen, wants pricing") == ""


# --- same-day time-aware (2026-06-02, Lili/Marina) ---------------------------
import datetime as _dt  # noqa: E402


def test_sameday_time_passed_fires():
    # asked for today 5 PM, now 7 PM Dubai -> graceful future pivot
    n = _passed_date_note("Jun 2, 5 PM",
                          now=_dt.datetime(2026, 6, 2, 19, 0)).lower()
    assert "passed" in n and "future" in n and "today" in n


def test_sameday_time_from_reasoning_fires():
    # Lili: dates has no time; the window is in the analyzer reasoning
    n = _passed_date_note("Jun 2",
                          "booking is TODAY (Jun 2), 17:30-20:30, engaged",
                          now=_dt.datetime(2026, 6, 2, 20, 32)).lower()
    assert "passed" in n and "graceful" in n


def test_sameday_time_not_yet_no_note():
    # evening slot still upcoming -> no note (don't false-pivot)
    assert _passed_date_note("Jun 2, 9 PM",
                             now=_dt.datetime(2026, 6, 2, 18, 0)) == ""


def test_sameday_no_time_no_note():
    # Marina: same day, no parseable time -> deterministic note stays silent
    # (the drafter's injected current-time anchor handles this case instead)
    assert _passed_date_note("Tue Jun 2",
                             now=_dt.datetime(2026, 6, 2, 22, 0)) == ""


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} passed_date_note tests passed")
