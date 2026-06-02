#!/usr/bin/env python3
"""Guards for the 2026-06-02 pipeline-review root-cause campaign.

Family A — false identity merge (Qurbani/Royalty 136 collapsed into Antonio/
Bliss 55 via a recycled WhatsApp LID that mapped to the wrong person's number;
the reconcile merged on that single lookup with NO same-person check):
  -> labels._merge_blocked

Family B — a booked/active lead auto-killed to COLD on an UNVERIFIABLE
"passed date" (Tal Sudai: dates='today' never anchored -> analyzer read it as
"6+ days in the past" -> NEW->COLD), and post-event CONFIRMED cards needing a
"did the event happen?" detector:
  -> labels._demote_to_cold_blocked
  -> labels.event_passed

Run: python3 hermes-bridge/test_identity_lifecycle_guard.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import (  # noqa: E402
    _merge_blocked,
    _is_real_name,
    _demote_to_cold_blocked,
    event_passed,
)

TODAY = datetime.date(2026, 6, 2)


# ---------------------------------------------------------------- Family A
def test_distinct_real_names_block_merge():
    # The exact incident: Qurbani's row carried name 'Zayn'; the recycled LID
    # row was 'Antonio'. Distinct real names => different humans => never merge.
    assert _merge_blocked("Zayn", "Antonio") is True
    assert _merge_blocked("Qurbani", "Antonio") is True


def test_same_name_does_not_block():
    assert _merge_blocked("Antonio", "Antonio") is False
    assert _merge_blocked("  antonio ", "Antonio") is False  # case/space-insens


def test_empty_or_placeholder_name_does_not_block():
    # A bare @lid with no name legitimately merges into its @c.us row.
    assert _merge_blocked("", "Antonio") is False
    assert _merge_blocked("Antonio", "") is False
    assert _merge_blocked("unknown", "Antonio") is False
    assert _merge_blocked("Customer", "Antonio") is False


def test_phone_as_name_does_not_block():
    # A phone-number 'name' is not a real name (don't let it block a merge).
    assert _merge_blocked("+971509767187", "Antonio") is False
    assert _merge_blocked("971509767187", "Antonio") is False


def test_is_real_name():
    assert _is_real_name("Antonio") is True
    assert _is_real_name("  ") is False
    assert _is_real_name("unknown") is False
    assert _is_real_name("+971...") is False


# ---------------------------------------------------------------- Family B demote
def test_tal_today_passed_claim_is_blocked():
    # The exact incident: an UNPARSEABLE relative date claimed "passed".
    assert _demote_to_cold_blocked(
        "today",
        "Booking was for 'today' — that date is now 6+ days in the past",
        ever_booked=False, today=TODAY) is True


def test_genuinely_past_parseable_date_close_allowed():
    # May 28 really is before Jun 2 — a real passed-date close still works.
    assert _demote_to_cold_blocked(
        "May 28", "the date has passed", ever_booked=False,
        today=TODAY) is False


def test_future_date_passed_claim_is_blocked():
    assert _demote_to_cold_blocked(
        "Jun 6", "event is over", ever_booked=False, today=TODAY) is True


def test_real_reason_close_on_unparseable_date_allowed():
    # A genuine non-date close (price) must still close even with a vague date.
    assert _demote_to_cold_blocked(
        "today", "customer rejected the price, not interested",
        ever_booked=False, today=TODAY) is False


def test_ever_booked_always_blocks():
    # A lead that ever reached a paid/booked state must never be auto-COLD'd.
    assert _demote_to_cold_blocked(
        "May 28", "the date has passed", ever_booked=True, today=TODAY) is True
    assert _demote_to_cold_blocked(
        "", "lost, booked elsewhere", ever_booked=True, today=TODAY) is True


def test_tomorrow_passed_claim_is_blocked():
    assert _demote_to_cold_blocked(
        "tomorrow", "the event has already passed", ever_booked=False,
        today=TODAY) is True


# ---------------------------------------------------------------- event_passed
def test_event_passed_for_past_parseable_date():
    assert event_passed("May 28, 4-7 PM", today=TODAY) is True
    assert event_passed("Sun May 24, 6-8 PM", today=TODAY) is True


def test_event_not_passed_for_future():
    assert event_passed("Jun 20", today=TODAY) is False


def test_event_not_passed_for_unparseable_or_empty():
    assert event_passed("today", today=TODAY) is False
    assert event_passed("", today=TODAY) is False
    assert event_passed(None, today=TODAY) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} identity/lifecycle guard tests passed")
