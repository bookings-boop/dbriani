#!/usr/bin/env python3
"""F3 (2026-06-12): "📅 DATE THIS WEEK" digest — surfaces the orphaned
population Ahmed fell into (an imminent-date lead we are NOT owed a reply on,
which every other /review path misses because each is owed-keyed or cap-bound
and booking date fed no rank term).

Tests the pure builder + the behavior-neutrality of the _booking_urgency_bonus
extraction. Membership/ordering only — no DB, no render side effects.

Run: python3 hermes-bridge/test_dates_digest.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review  # noqa: E402
from review import (  # noqa: E402
    build_dates_digest, _date_urgency, _urgency_from_date,
    _booking_urgency_bonus)


def _iso(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _row(**kw):
    r = {"label": "WARM", "name": "X", "importance_score": 50,
         "last_customer_message_at_seconds": 9000,
         "last_operator_reply_at_seconds": 100,   # we replied last → NOT owed
         "last_nudge_drafted_at_seconds": None}
    r.update(kw)
    return r


# --- _booking_urgency_bonus extraction is behavior-neutral ------------------
def test_urgency_extraction_equivalence():
    # the refactor must not change any tier boundary for month-name dates
    today = dt.date.today()
    for days, expect in [(0, 600), (1, 600), (2, 400), (3, 400),
                         (7, 200), (8, 0), (-1, 0)]:
        d = today + dt.timedelta(days=days)
        s = d.strftime("%b %d")  # month-name form the parser anchors
        assert _booking_urgency_bonus(s) == expect, (s, expect)
        assert _urgency_from_date(d) == expect


def test_urgency_none_and_past():
    assert _urgency_from_date(None) == 0
    assert _booking_urgency_bonus("") == 0
    assert _booking_urgency_bonus(None) == 0


# --- _date_urgency: ISO booking_date_abs is now ranked ----------------------
def test_date_urgency_prefers_iso_abs():
    # booking_date_abs ISO — the month-name parser returns None on this, so it
    # fed NO ranking before the fix; _date_urgency must anchor it.
    assert _date_urgency({"booking_date_abs": _iso(4)}) == 200
    assert _date_urgency({"booking_date_abs": _iso(0)}) == 600
    assert _date_urgency({"booking_date_abs": _iso(30)}) == 0


def test_date_urgency_falls_back_to_dates_string():
    soon = (dt.date.today() + dt.timedelta(days=2)).strftime("%b %d")
    assert _date_urgency({"dates": soon}) == 400
    assert _date_urgency({"booking_date_abs": "", "dates": soon}) == 400


def test_date_urgency_junk_safe():
    assert _date_urgency({"booking_date_abs": "not-a-date"}) == 0
    assert _date_urgency({}) == 0
    assert _date_urgency(None) == 0


# --- build_dates_digest: membership ------------------------------------------
def test_digest_includes_imminent_not_owed():
    scored = [(1690, _row(name="Ahmed", booking_date_abs=_iso(4),
                          importance_score=92))]
    out = build_dates_digest(scored)
    assert "DATE THIS WEEK" in out
    assert "Ahmed" in out
    assert "imp 92" in out


def test_digest_excludes_owed_lead():
    # owed leads belong to the 🔴 digest, not here
    owed = _row(name="Owed", booking_date_abs=_iso(2),
                last_operator_reply_at_seconds=9000,
                last_customer_message_at_seconds=100)  # customer last → owed
    assert build_dates_digest([(100, owed)]) == ""


def test_digest_excludes_far_and_past_dates():
    assert build_dates_digest([(100, _row(booking_date_abs=_iso(30)))]) == ""
    assert build_dates_digest([(100, _row(booking_date_abs=_iso(-2)))]) == ""
    assert build_dates_digest([(100, _row(booking_date_abs=""))]) == ""


def test_digest_excludes_terminal_labels():
    for lbl in ("CONFIRMED", "LOST", "SCAM", "DISREGARDED", "PAUSED_SNOOZE"):
        r = _row(label=lbl, booking_date_abs=_iso(2))
        assert build_dates_digest([(100, r)]) == "", lbl


def test_digest_includes_cold_misdemote():
    # COLD is deliberately included so a label-engine misdemote can't hide a
    # dated lead (the exact upstream contributor in the Ahmed RCA).
    out = build_dates_digest([(100, _row(label="COLD",
                                         booking_date_abs=_iso(1)))])
    assert "DATE THIS WEEK" in out


# --- build_dates_digest: ordering + cap --------------------------------------
def test_digest_orders_by_urgency_then_value():
    today_lead = _row(name="Today", booking_date_abs=_iso(0),
                      yachts="Ferretti 780", importance_score=50)
    week_lead = _row(name="NextWeek", booking_date_abs=_iso(6),
                     yachts="Ferretti 780", importance_score=90)
    out = build_dates_digest([(100, week_lead), (200, today_lead)])
    # sooner date wins regardless of the other lead's higher score
    assert out.index("Today") < out.index("NextWeek")


def test_digest_cap_and_overflow():
    rows = [(100, _row(name=f"L{i}", booking_date_abs=_iso(3)))
            for i in range(30)]
    out = build_dates_digest(rows, cap=25)
    assert "_+5 more this week_" in out
    assert out.count(" • ") == 25


def test_digest_junk_safe():
    assert build_dates_digest([]) == ""
    assert build_dates_digest(None) == ""
    assert build_dates_digest([None, "x", (1,), (1, "notadict")]) == ""


def test_digest_empty_when_nothing_due():
    assert build_dates_digest([(100, _row(booking_date_abs=_iso(20)))]) == ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} dates_digest tests passed")
