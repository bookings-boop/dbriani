#!/usr/bin/env python3
"""Exclusion-guard (Layer 3, safety-only) — block proactive/marketing
outreach to internal_staff / external_crew_vendors / external_agents.

This is a pure BLOCK-LIST, not a customer lookup: it can only suppress
outreach, never mix up identities. Phase 1 of the customer-data pipeline.

Contract under test:
  - normalize_phone(): ONE normalizer used on BOTH the stored list and the
    incoming identity, mirroring server._normalize_phone (digits only, drop
    leading 00, UAE-local -> 971). A staff phone must match regardless of
    the format it arrives in (+971 / 971 / 00971 / spaces / missing +).
  - ExclusionGuard.is_excluded(): exact normalized membership. Handles
    WhatsApp ids: '<digits>@c.us' (phone IS the digits) and '<hash>@lid'
    (a privacy HASH, NOT a phone — only matchable once resolved to a phone).
  - FAIL-CLOSED: any internal error, or data that failed to load, must
    err toward BLOCKING outreach (return True), never toward sending.

Run (no pytest on the box): python3 hermes-bridge/test_exclusion_guards.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hermes_exclusion_guards import (  # noqa: E402
    ExclusionGuard,
    normalize_phone,
)


# ── normalize_phone: one normalizer, format-invariant ────────────────

def test_normalize_plus_and_spaces():
    assert normalize_phone("+971 50 123 4567") == "971501234567"


def test_normalize_bare_digits():
    assert normalize_phone("971501234567") == "971501234567"


def test_normalize_double_zero_intl_prefix():
    # 00971... is the same number as +971...
    assert normalize_phone("00971501234567") == "971501234567"


def test_normalize_uae_local_leading_zero():
    # 0xxxxxxxxx (10 digits) is UAE national format -> 971 + rest
    assert normalize_phone("0501234567") == "971501234567"


def test_normalize_uae_nine_digit_mobile():
    # 5xxxxxxxx (9 digits) is a UAE mobile without country code
    assert normalize_phone("501234567") == "971501234567"


def test_normalize_empty_and_none():
    assert normalize_phone("") == ""
    assert normalize_phone(None) == ""


def test_all_uae_formats_collapse_to_one():
    forms = ["+971501234567", "971501234567", "00971501234567",
             "+971 50 123 4567", "0501234567", "501234567",
             "971501234567@c.us"]
    norm = {normalize_phone(f.split("@")[0]) for f in forms}
    assert norm == {"971501234567"}, norm


# ── ExclusionGuard: exact membership, format-invariant ───────────────

def _guard():
    return ExclusionGuard({
        "internal_staff": ["+971503603978"],          # Swaheel (real staff)
        "external_crew_vendors": ["+971509767187"],    # captain (real crew)
        "external_agents": ["+15615657774"],           # agent
    })


def test_staff_phone_matches_regardless_of_format():
    g = _guard()
    for fmt in ["971503603978", "+971503603978", "+971 50 360 3978",
                "00971503603978", "971503603978@c.us"]:
        assert g.is_excluded(fmt) is True, fmt


def test_non_excluded_customer_not_blocked():
    g = _guard()
    # A real paying customer (Thomas) must NOT be excluded
    assert g.is_excluded("+33676004758") is False
    assert g.is_excluded("33676004758@c.us") is False


def test_category_reported():
    g = _guard()
    assert g.category_of("+971509767187") == "external_crew_vendors"
    assert g.category_of("+33676004758") is None


# ── @lid handling (hash, not a phone) ────────────────────────────────

def test_lid_resolving_to_staff_phone_is_blocked():
    g = _guard()
    # WAHA resolves this @lid hash to the captain's real phone -> block
    assert g.is_excluded("88812345@lid",
                         lid_resolver=lambda c: "+971509767187") is True


def test_lid_unresolvable_is_allowed_not_error():
    g = _guard()
    # No resolver / WAHA can't map the hash -> we can't confirm it's staff.
    # That is NOT an error; most @lid ids are normal customers -> ALLOW.
    assert g.is_excluded("88812345@lid") is False
    assert g.is_excluded("88812345@lid", lid_resolver=lambda c: "") is False


def test_recycled_lid_now_a_different_number_is_allowed():
    g = _guard()
    # The lid now maps to a NON-staff number -> not blocked.
    assert g.is_excluded("88812345@lid",
                         lid_resolver=lambda c: "+971500000000") is False


def test_unresolvable_lid_blocks_on_proactive_path():
    # Audit #14 (2026-06-07): on the PROACTIVE path (block_if_unresolved=True)
    # an unresolvable @lid must fail CLOSED — a block-listed staff/crew @lid in
    # a WAHA-degraded window must NOT get a nudge. Default (inbound) still allows.
    g = _guard()
    assert g.is_excluded("88812345@lid", lid_resolver=lambda c: "") is False
    assert g.is_excluded("88812345@lid", lid_resolver=lambda c: "",
                         block_if_unresolved=True) is True


# ── FAIL-CLOSED: errors / unloaded data must BLOCK ───────────────────

def test_resolver_exception_blocks():
    g = _guard()

    def boom(_c):
        raise RuntimeError("waha down")

    # An exception while resolving must err toward BLOCKING, not sending.
    assert g.is_excluded("88812345@lid", lid_resolver=boom) is True


def test_unloaded_guard_blocks_everything():
    # If the data file failed to load, the guard must block ALL outreach
    # (safe direction), not silently become a no-op that lets cold-pitches
    # reach staff.
    failed = ExclusionGuard.failed()
    assert failed.is_excluded("+33676004758") is True
    assert failed.is_excluded("anything") is True


# ── live data file (singleton) — green once exclusion_phones.json ships

def test_singleton_loads_real_data():
    import hermes_exclusion_guards as g
    assert g.load_ok() is True, "exclusion_phones.json failed to load"
    assert g.loaded_count() >= 390, g.loaded_count()
    # Known real staff/crew numbers from the roster must be excluded
    assert g.is_excluded("+971503603978") is True   # Swaheel (staff)
    assert g.is_excluded("971509767187@c.us") is True  # captain (crew)
    # A real customer must pass through
    assert g.is_excluded("+33676004758") is False   # Thomas (customer)


# ── stdlib runner (box has no pytest) ────────────────────────────────

def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed_n = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed_n += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed_n} failed, {len(tests)} total")
    return 1 if failed_n else 0


if __name__ == "__main__":
    sys.exit(_run())
