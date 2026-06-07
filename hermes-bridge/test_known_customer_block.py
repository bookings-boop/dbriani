#!/usr/bin/env python3
"""Unit tests for the KNOWN-PAID-CUSTOMER recognition block (Step 2 pivot,
2026-06-07).

PIVOT: the recognition data now comes from a SIMPLE draft-time phone lookup
(known_customers.lookup -> {name, revenue_aed, n_bookings}), NOT from
customer_facts profile columns (the dead "upsert clean profiles into
customer_facts" approach — customer_facts held only ~222 active-lead rows, not
the 1,383 all-time payers). The one-line recognition cue + the gate are
unchanged; only the data source moved.

Contract under test:
  - _known_customer_text(rev, nb): PURE — exact recognition line, or "" when
    there is no real revenue / booking data (never "AED 0", never "0 booking(s)").
  - known_customer_block(data): gated entry over a lookup-result dict
    ({revenue_aed, n_bookings, ...}). flag OFF -> ""; flag ON + data -> exact
    block; flag ON + no data / None -> "".
  - build_query injects the block ONLY when the flag is ON and
    known_customers.lookup returns data (a miss / @lid -> no block; OFF ->
    prompt byte-for-byte unchanged).

Plain-assert style; stdlib runner (the box has no pytest).
Run: python3 hermes-bridge/test_known_customer_block.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import known_customers  # noqa: E402
import server  # noqa: E402
from server import (  # noqa: E402
    known_customer_block,
    known_customer_profile_enabled,
    _known_customer_text,
)

FLAG = "KNOWN_CUSTOMER_PROFILE_ENABLED"
TROPHY = "\U0001F3C6"  # 🏆 as an escape so the test is encoding-independent
_EXPECT_BOTH = (TROPHY + " RETURNING PAID CUSTOMER — AED 994,104 lifetime "
                "across 6 booking(s); greet warmly, they know us.")
# A lookup-result dict (the shape known_customers.lookup returns).
_LOOKUP_HIT = {"name": "Christian Carmona", "revenue_aed": 994104,
               "n_bookings": 6}


def _set_flag(v):
    if v is None:
        os.environ.pop(FLAG, None)
    else:
        os.environ[FLAG] = v


# ── flag plumbing (mirror PROFILE_LOOKUP_ENABLED) ────────────────────

def test_flag_default_off():
    _set_flag(None)
    assert known_customer_profile_enabled() is False


def test_flag_truthy_values_enable():
    for v in ("1", "true", "TRUE", "yes", "on"):
        _set_flag(v)
        assert known_customer_profile_enabled() is True, v
    for v in ("0", "", "off", "no"):
        _set_flag(v)
        assert known_customer_profile_enabled() is False, v
    _set_flag(None)


# ── pure text builder (no flag, no fabrication) ──────────────────────

def test_text_both_fields_exact():
    assert _known_customer_text(994104, 6) == _EXPECT_BOTH


def test_text_revenue_only_no_zero_bookings():
    t = _known_customer_text(994104, None)
    assert "AED 994,104 lifetime" in t
    assert "booking(s)" not in t  # never fabricate a 0-booking phrase


def test_text_bookings_only_no_zero_revenue():
    t = _known_customer_text(None, 3)
    assert "3 booking(s) on record" in t
    assert "AED" not in t  # never fabricate an AED 0 figure


def test_text_no_data_is_empty():
    assert _known_customer_text(None, None) == ""
    assert _known_customer_text(0, 0) == ""
    assert _known_customer_text("", "") == ""


def test_text_revenue_float_formats_with_thousands():
    assert "AED 994,104 lifetime" in _known_customer_text(994104.0, 6)


# ── gated entry point (over a lookup-result dict) ────────────────────

def test_block_absent_when_flag_off():
    _set_flag(None)  # default OFF
    assert known_customer_block(dict(_LOOKUP_HIT)) == ""


def test_block_present_when_flag_on_and_data():
    _set_flag("1")
    assert known_customer_block(dict(_LOOKUP_HIT)) == _EXPECT_BOTH
    _set_flag(None)


def test_block_absent_when_flag_on_but_no_data():
    _set_flag("1")
    assert known_customer_block({"revenue_aed": None,
                                 "n_bookings": None}) == ""
    assert known_customer_block({"revenue_aed": 0, "n_bookings": 0}) == ""
    assert known_customer_block(None) == ""
    _set_flag(None)


# ── build_query wiring (gated end-to-end, lookup-sourced) ────────────

def _payload(cid="971585359272@c.us"):
    return {"customer_id": cid, "customer_name": "Christian",
            "incoming_message": "hi again", "history": ""}


def _patch_lookup(fn):
    orig = known_customers.lookup
    known_customers.lookup = fn
    def restore():
        known_customers.lookup = orig
    return restore


def test_build_query_injects_block_only_when_enabled():
    restore = _patch_lookup(lambda ident: dict(_LOOKUP_HIT))
    try:
        _set_flag("1")
        q_on = server.build_query(_payload())
        assert _EXPECT_BOTH in q_on

        _set_flag(None)  # default OFF -> prompt unchanged
        q_off = server.build_query(_payload())
        assert TROPHY not in q_off
        assert "RETURNING PAID CUSTOMER" not in q_off
    finally:
        restore()
        _set_flag(None)


def test_build_query_skips_when_lookup_miss():
    # flag ON but the phone is not a known payer -> no block (graceful).
    restore = _patch_lookup(lambda ident: None)
    try:
        _set_flag("1")
        q = server.build_query(_payload())
        assert TROPHY not in q
        assert "RETURNING PAID CUSTOMER" not in q
    finally:
        restore()
        _set_flag(None)


def test_build_query_skips_for_lid_identity():
    # An @lid resolves to no phone -> lookup returns None -> no block, even ON.
    seen = {}

    def fake(ident):
        seen["ident"] = ident
        return known_customers.lookup.__wrapped__(ident) \
            if hasattr(known_customers.lookup, "__wrapped__") else \
            (None if str(ident).endswith("@lid") else dict(_LOOKUP_HIT))

    restore = _patch_lookup(fake)
    try:
        _set_flag("1")
        q = server.build_query(_payload(cid="9999999@lid"))
        assert "RETURNING PAID CUSTOMER" not in q
        assert seen.get("ident") == "9999999@lid"
    finally:
        restore()
        _set_flag(None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    p = f = 0
    for t in tests:
        try:
            t()
            p += 1
            print(f"  PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            f += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{p} passed, {f} failed, {len(tests)} total")
    sys.exit(1 if f else 0)
