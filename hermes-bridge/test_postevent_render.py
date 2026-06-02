#!/usr/bin/env python3
"""Post-event CONFIRMED render (2026-06-02 — Émilie & Saif).

A CONFIRMED booking whose event date has PASSED must not show active-deal
guidance ('confirm logistics / upsell / re-engage / wait for reply') or the
open-sale Hermes score — those are meaningless for a won, completed trip. The
card should read as a won / post-event nurture. Upcoming CONFIRMED bookings keep
the logistics/upsell guidance.

Run: python3 hermes-bridge/test_postevent_render.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _why_line  # noqa: E402

TODAY = datetime.date(2026, 6, 2)


def test_past_confirmed_event_is_complete_not_reengage():
    # Saif: stale suggested_action says "Wait for reply ... Re-engage" — wrong
    # for a completed trip. Must show a post-event line instead.
    row = {"label": "CONFIRMED", "dates": "Sun May 24, 6-8 PM",
           "suggested_action": "Wait for reply — already followed up. "
                               "Re-engage when customer or referrals reach out."}
    out = _why_line(row, "CONFIRMED", today=TODAY)
    assert "event complete" in out.lower(), out
    assert "re-engage" not in out.lower(), out
    assert "upsell" not in out.lower(), out


def test_future_confirmed_keeps_suggested_action():
    # Antonio (Jun 20, future): logistics/upsell guidance is still valid.
    row = {"label": "CONFIRMED", "dates": "Jun 20",
           "suggested_action": "confirm boarding time and add-ons"}
    assert _why_line(row, "CONFIRMED", today=TODAY) == \
        "confirm boarding time and add-ons"


def test_future_confirmed_no_suggestion_keeps_boarding_default():
    row = {"label": "CONFIRMED", "dates": "Jun 20"}
    assert "boarding" in _why_line(row, "CONFIRMED", today=TODAY).lower()


def test_past_confirmed_overrides_even_with_no_suggestion():
    row = {"label": "CONFIRMED", "dates": "May 28, 4-7 PM"}
    assert "event complete" in _why_line(row, "CONFIRMED", today=TODAY).lower()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} post-event render tests passed")
