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


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} passed_date_note tests passed")
