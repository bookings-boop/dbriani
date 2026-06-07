#!/usr/bin/env python3
"""Profile-lookup (Layer 1) — tests for the returning-customer context injector.

Contract:
  - keys on server._normalize_phone (same standard as exclusion guard / revenue);
  - resolves '<digits>@c.us' directly and '<hash>@lid' via a resolver;
  - GATED by PROFILE_LOOKUP_ENABLED (default OFF -> profile_block == "");
  - FAIL-SAFE: unknown profile / resolver error -> "" (never raises);
  - reads merged-body customers' preferences (Emiel) — proves the dedup append flows through;
  - never renders internal revenue figures.

Run (no pytest on the box): python3 hermes-bridge/test_profile_lookup.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profile_lookup as P  # noqa: E402

CHRISTIAN = "971585359272"   # complete profile
GIUSEPPE  = "971586513671"   # rich prefs (wine/yacht)
AAMIR     = "971502350420"   # chat-reliant, has revenue (must NOT leak)
EMIEL     = "31653332213"    # merged-body customer (_merged_dup_extractions:2)


def _on():  os.environ["PROFILE_LOOKUP_ENABLED"] = "1"
def _off(): os.environ.pop("PROFILE_LOOKUP_ENABLED", None)


def test_normalize_phone_standard():
    assert P.normalize_phone("+971 50 123 4567") == "971501234567"
    assert P.normalize_phone("00971501234567") == "971501234567"
    assert P.normalize_phone("0501234567") == "971501234567"
    assert P.normalize_phone("") == ""


def test_index_loads():
    assert P.load_ok() is True
    assert P.loaded_count() >= 2000, P.loaded_count()


def test_disabled_by_default_returns_empty():
    _off()
    assert P.enabled() is False
    assert P.profile_block(GIUSEPPE + "@c.us") == ""   # known customer, but flag off


def test_lookup_by_c_us():
    prof = P.lookup(CHRISTIAN + "@c.us")
    assert prof and prof.get("name") == "Christian Carmona", prof


def test_lookup_by_lid_with_resolver():
    prof = P.lookup("99999hash@lid", phone_resolver=lambda c: "+" + CHRISTIAN)
    assert prof and prof.get("name") == "Christian Carmona"


def test_block_enabled_returning_has_name_and_prefs():
    _on()
    try:
        b = P.profile_block(GIUSEPPE + "@c.us")
        assert "Giuseppe" in b, b[:200]
        assert ("Provence" in b or "Prosecco" in b or "SX88" in b), b[:300]
        assert "RETURNING" in b
    finally:
        _off()


def test_merged_customer_prefs_flow_through():
    _on()
    try:
        b = P.profile_block(EMIEL + "@c.us")
        assert "Emiel de Kok" in b and len(b) > 80, b[:200]
    finally:
        _off()


def test_unknown_customer_returns_empty():
    _on()
    try:
        assert P.profile_block("100000000000@c.us") == ""
    finally:
        _off()


def test_failsafe_resolver_raises_returns_empty():
    _on()
    try:
        def boom(_c): raise RuntimeError("waha down")
        assert P.profile_block("abc@lid", phone_resolver=boom) == ""  # no exception
    finally:
        _off()


def test_no_internal_revenue_in_block():
    _on()
    try:
        b = P.profile_block(AAMIR + "@c.us")
        assert "clean_revenue" not in b and "counts_as_revenue" not in b and "gateway_aed" not in b, b[:300]
    finally:
        _off()


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    p = f = 0
    for t in tests:
        try:
            t(); p += 1; print(f"  PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            f += 1; print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{p} passed, {f} failed, {len(tests)} total")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(_run())
