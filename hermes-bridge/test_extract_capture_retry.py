#!/usr/bin/env python3
"""Unit tests for AREA A — extraction + analyzer reliability (server.py).

Covers the three changes from the analysis-RCA:
  1. ABSOLUTE-DATE NORMALIZATION AT CAPTURE — _resolve_relative_date() +
     _booking_abs_date(), and extract_customer_facts() writing
     'booking_date_abs' so the analyzer future-anchor + passed-date veto stop
     silently no-op-ing on relative phrasing ('tomorrow' frozen days ago).
  2. BOOKING TIME + ADD-ONS CAPTURE — _extract_booking_time() / _extract_addons()
     pure helpers + extract_customer_facts() returning 'booking_time'/'addons'.
  3. ANALYZER TIMEOUT-RETRY — _run_hermes_analyze() bounded retry on a
     timeout/transient rc, fail-safe (never raises), final failure → None.

Plain-assert style. Stubs server.run_hermes / server._dubai_now where a Hermes
call or wall-clock would otherwise make the test non-deterministic.

Run: python3 hermes-bridge/test_extract_capture_retry.py
"""
import datetime as dt
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
from server import (  # noqa: E402
    _resolve_relative_date, _extract_booking_time, _extract_addons,
    _booking_abs_date, extract_customer_facts, hermes_analyze_lead,
    _run_hermes_analyze,
)

# Keep the suite fast: no real backoff sleeps unless a test opts in.
server.ANALYZE_LEAD_RETRY_BACKOFF = 0

# A fixed Wednesday anchor (2026-06-10, weekday=2) for deterministic dates.
WED = dt.datetime(2026, 6, 10, 14, 30)   # Wed 14:30
SAT = dt.datetime(2026, 6, 13, 10, 0)    # Sat 10:00


# --- change 1: _resolve_relative_date ------------------------------------

def test_resolve_tomorrow_today():
    assert _resolve_relative_date("tomorrow", WED) == "2026-06-11"
    assert _resolve_relative_date("today", WED) == "2026-06-10"
    assert _resolve_relative_date("tonight", WED) == "2026-06-10"


def test_resolve_in_n_days():
    assert _resolve_relative_date("in 3 days", WED) == "2026-06-13"
    assert _resolve_relative_date("in 1 day", WED) == "2026-06-11"


def test_resolve_this_weekend():
    # Wed -> coming Saturday.
    assert _resolve_relative_date("this weekend", WED) == "2026-06-13"
    # On Saturday itself, 'this weekend' is that same day.
    assert _resolve_relative_date("this weekend", SAT) == "2026-06-13"


def test_resolve_bare_weekday():
    # Wed -> the coming Friday / Saturday.
    assert _resolve_relative_date("Friday", WED) == "2026-06-12"
    assert _resolve_relative_date("Sat", WED) == "2026-06-13"
    # A bare weekday equal to today resolves to today.
    assert _resolve_relative_date("Wednesday", WED) == "2026-06-10"


def test_resolve_next_weekday():
    # 'next Friday' = the Friday of the following week (this-Friday + 7).
    assert _resolve_relative_date("next Friday", WED) == "2026-06-19"
    assert _resolve_relative_date("next wednesday", WED) == "2026-06-17"


def test_resolve_absolute_left_untouched():
    # Already-absolute dates are NOT relative — helper returns None and leaves
    # them for labels._parse_booking_date.
    assert _resolve_relative_date("Sat May 23", WED) is None
    assert _resolve_relative_date("June 14 2026", WED) is None


def test_resolve_empty_and_garbage():
    assert _resolve_relative_date("", WED) is None
    assert _resolve_relative_date(None, WED) is None
    assert _resolve_relative_date("whenever you like", WED) is None


# --- change 1: _booking_abs_date -----------------------------------------

def test_booking_abs_prefers_field():
    d = _booking_abs_date({"booking_date_abs": "2026-06-19", "dates": "tomorrow"},
                          "tomorrow")
    assert d == dt.date(2026, 6, 19)


def test_booking_abs_falls_back_to_parse():
    # No abs field -> parse the free 'dates' string (absolute).
    d = _booking_abs_date({"dates": "Sat May 23"}, "Sat May 23")
    assert d is not None and d.month == 5 and d.day == 23


def test_booking_abs_bad_field_falls_back():
    d = _booking_abs_date({"booking_date_abs": "not-a-date"}, "Sat May 23")
    assert d is not None and d.month == 5
    # Nothing parseable anywhere -> None (never raises).
    assert _booking_abs_date({"booking_date_abs": "xx"}, "whenever") is None
    assert _booking_abs_date(None, "") is None


# --- change 2: _extract_booking_time -------------------------------------

def test_booking_time_ranges():
    assert _extract_booking_time("we'd like 5PM-9PM") == "5PM-9PM"
    assert _extract_booking_time("how about 4-7pm?") == "4-7PM"
    assert _extract_booking_time("from 5 pm to 9 pm please") == "5PM-9PM"
    assert _extract_booking_time("17:00-21:00 works") == "17:00-21:00"


def test_booking_time_single():
    assert _extract_booking_time("start at 5pm") == "5PM"
    assert _extract_booking_time("can we do 5:30 p.m.") == "5:30PM"


def test_booking_time_never_fabricates():
    assert _extract_booking_time("we are 6-8 guests") == ""   # party size, NOT time
    assert _extract_booking_time("call me on 555-1234") == ""
    assert _extract_booking_time("just the yacht thanks") == ""
    assert _extract_booking_time("") == ""
    assert _extract_booking_time(None) == ""


def test_booking_time_skips_partysize_finds_real_time():
    # A party-size range must not shadow a real time later in the text.
    assert _extract_booking_time("6-8 guests, 5pm-9pm") == "5PM-9PM"


# --- change 2: _extract_addons -------------------------------------------

def test_addons_basic():
    assert _extract_addons("we want BBQ and a jetski plus decoration") == \
        "BBQ, jetski, decoration"
    assert _extract_addons("can you add a photographer and a DJ?") == \
        "photographer, DJ"


def test_addons_dedup_and_forms():
    assert _extract_addons("jet ski, jetskis and a cake") == "jetski, cake"
    assert _extract_addons("flowers / floral arrangement + catering") == \
        "flowers, catering"


def test_addons_none():
    assert _extract_addons("just the yacht for 8 guests") == ""
    assert _extract_addons("") == ""
    assert _extract_addons(None) == ""


# --- change 1+2: extract_customer_facts end-to-end ------------------------

def _stub_facts_run(json_out):
    def _stub(q, timeout=None, priority="background"):
        return (0, json_out, "", 1234)
    return _stub


def test_extract_facts_normalizes_relative_date():
    orig_run, orig_now = server.run_hermes, server._dubai_now
    try:
        server._dubai_now = lambda: WED
        server.run_hermes = _stub_facts_run(
            '{"name":"Emilie","dates":"tomorrow","yachts":"Bliss 55",'
            '"party_size":"8","booking_time":"5pm-9pm","addons":"BBQ"}')
        facts = extract_customer_facts("hi", "history")
    finally:
        server.run_hermes, server._dubai_now = orig_run, orig_now
    assert facts is not None
    # Original relative text preserved for display.
    assert facts["dates"] == "tomorrow"
    # Resolved absolute date anchored to the (stubbed) extraction time.
    assert facts["booking_date_abs"] == "2026-06-11"
    assert facts["booking_time"] == "5PM-9PM"
    assert facts["addons"] == "BBQ"
    assert facts["name"] == "Emilie"


def test_extract_facts_absolute_date_gets_abs():
    orig_run, orig_now = server.run_hermes, server._dubai_now
    try:
        server._dubai_now = lambda: WED
        server.run_hermes = _stub_facts_run(
            '{"name":"","dates":"June 14 2026","yachts":"","party_size":""}')
        facts = extract_customer_facts("hi", "")
    finally:
        server.run_hermes, server._dubai_now = orig_run, orig_now
    assert facts["dates"] == "June 14 2026"
    assert facts["booking_date_abs"] == "2026-06-14"


def test_extract_facts_no_date_no_abs():
    orig_run, orig_now = server.run_hermes, server._dubai_now
    try:
        server._dubai_now = lambda: WED
        server.run_hermes = _stub_facts_run(
            '{"name":"Bob","dates":"","yachts":"","party_size":""}')
        facts = extract_customer_facts("hi", "")
    finally:
        server.run_hermes, server._dubai_now = orig_run, orig_now
    assert facts["booking_date_abs"] == ""
    assert facts["booking_time"] == ""
    assert facts["addons"] == ""


def test_extract_facts_addons_from_message_not_fabricated():
    # Model omits booking_time/addons; pure scan recovers them from the text,
    # and never invents one the text doesn't contain.
    orig_run, orig_now = server.run_hermes, server._dubai_now
    try:
        server._dubai_now = lambda: WED
        server.run_hermes = _stub_facts_run(
            '{"name":"Sam","dates":"tomorrow","yachts":"","party_size":""}')
        facts = extract_customer_facts(
            "we'd like a jetski and BBQ, 6pm-10pm", "earlier chat")
    finally:
        server.run_hermes, server._dubai_now = orig_run, orig_now
    assert facts["booking_time"] == "6PM-10PM"
    # Fixed canonical order (BBQ before jetski), regardless of mention order.
    assert facts["addons"] == "BBQ, jetski"


def test_extract_facts_hermes_failure_returns_none():
    orig_run = server.run_hermes
    try:
        server.run_hermes = lambda q, timeout=None, priority="background": \
            (1, "", "boom", 5)
        assert extract_customer_facts("hi", "") is None
    finally:
        server.run_hermes = orig_run


# --- change 3: _run_hermes_analyze bounded retry --------------------------

def test_retry_rc_fail_then_success():
    orig = server.run_hermes
    calls = []
    try:
        def stub(q, timeout=None, priority="background"):
            calls.append(1)
            if len(calls) == 1:
                return (1, "", "transient", 10)
            return (0, '{"verdict":"keep_open","importance_score":55}', "", 20)
        server.run_hermes = stub
        res = _run_hermes_analyze("q", "c@c.us")
    finally:
        server.run_hermes = orig
    assert res is not None
    assert res[0] == 0
    assert len(calls) == 2   # one retry after the transient rc!=0


def test_retry_timeout_then_success():
    orig = server.run_hermes
    calls = []
    try:
        def stub(q, timeout=None, priority="background"):
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
            return (0, '{"verdict":"close","importance_score":0}', "", 5)
        server.run_hermes = stub
        res = _run_hermes_analyze("q", "c@c.us")
    finally:
        server.run_hermes = orig
    assert res is not None and res[0] == 0
    assert len(calls) == 2


def test_retry_exhausts_then_none():
    orig = server.run_hermes
    calls = []
    try:
        def stub(q, timeout=None, priority="background"):
            calls.append(1)
            return (2, "", "still failing", 1)
        server.run_hermes = stub
        res = _run_hermes_analyze("q", "c@c.us")
    finally:
        server.run_hermes = orig
    assert res is None
    assert len(calls) == 2   # initial + 1 bounded retry, then give up


def test_retry_success_first_try_no_retry():
    orig = server.run_hermes
    calls = []
    try:
        def stub(q, timeout=None, priority="background"):
            calls.append(1)
            return (0, '{"verdict":"keep_open","importance_score":70}', "", 3)
        server.run_hermes = stub
        res = _run_hermes_analyze("q", "c@c.us")
    finally:
        server.run_hermes = orig
    assert res is not None and len(calls) == 1


def test_retry_never_raises_on_exception():
    orig = server.run_hermes
    try:
        def stub(q, timeout=None, priority="background"):
            raise RuntimeError("kaboom")
        server.run_hermes = stub
        # Must not propagate the exception; final failure -> None.
        assert _run_hermes_analyze("q", "c@c.us") is None
    finally:
        server.run_hermes = orig


def test_retry_backoff_between_attempts():
    orig_run = server.run_hermes
    orig_backoff = server.ANALYZE_LEAD_RETRY_BACKOFF
    orig_sleep = time.sleep
    sleeps = []
    try:
        server.ANALYZE_LEAD_RETRY_BACKOFF = 0.01
        time.sleep = lambda s: sleeps.append(s)

        def stub(q, timeout=None, priority="background"):
            return (1, "", "fail", 1)
        server.run_hermes = stub
        _run_hermes_analyze("q", "c@c.us")
    finally:
        server.run_hermes = orig_run
        server.ANALYZE_LEAD_RETRY_BACKOFF = orig_backoff
        time.sleep = orig_sleep
    # Backoff happens BETWEEN attempts only — once for 2 attempts, never after
    # the final failure.
    assert len(sleeps) == 1


# --- change 3 wiring: hermes_analyze_lead uses the retry ------------------

def test_analyze_lead_retries_then_succeeds():
    orig = server.run_hermes
    calls = []
    try:
        def stub(q, timeout=None, priority="background"):
            calls.append(1)
            if len(calls) == 1:
                return (1, "", "transient", 1)
            return (0, '{"verdict":"keep_open","importance_score":42,'
                       '"reasoning":"engaged buyer"}', "", 1)
        server.run_hermes = stub
        out = hermes_analyze_lead(
            "c@c.us", "some history",
            {"name": "A", "dates": "", "yachts": "", "party_size": ""},
            message_count=5, silent_hours=10)
    finally:
        server.run_hermes = orig
    assert out is not None and out["importance_score"] == 42
    assert len(calls) == 2


def test_analyze_lead_all_fail_returns_none():
    orig = server.run_hermes
    try:
        server.run_hermes = lambda q, timeout=None, priority="background": \
            (1, "", "fail", 1)
        out = hermes_analyze_lead(
            "c@c.us", "h", {"name": "A", "dates": "", "yachts": "",
                            "party_size": ""}, message_count=5)
    finally:
        server.run_hermes = orig
    assert out is None   # caller keeps prior score (unchanged behaviour)


# --- change 1 wiring: relative-date passed-veto now fires -----------------

def test_analyze_lead_vetoes_passed_close_on_relative_abs_date():
    # The analyzer wrongly closes a FUTURE booking as 'date has passed'. With a
    # captured absolute date (far future), the veto must flip it back to
    # keep_open — the Emilie class, which previously slipped the absolute-only
    # veto because 'tomorrow' is unparseable.
    orig = server.run_hermes
    try:
        server.run_hermes = lambda q, timeout=None, priority="background": (
            0,
            '{"verdict":"close","importance_score":0,'
            '"reasoning":"customer silent for days, the charter date has '
            'passed - opportunity gone"}',
            "", 1)
        out = hermes_analyze_lead(
            "c@c.us", "history",
            {"name": "Emilie", "dates": "tomorrow",
             "booking_date_abs": "2099-01-01", "yachts": "Bliss 55",
             "party_size": "8"},
            message_count=20, silent_hours=251)
    finally:
        server.run_hermes = orig
    assert out is not None
    assert out["verdict"] == "keep_open"      # veto fired
    assert out["importance_score"] >= 40      # kept visible for the operator


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} extract/capture/retry tests passed")
