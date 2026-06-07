#!/usr/bin/env python3
"""Unit tests for known_customers.py — the FAIL-SAFE draft-time phone lookup
that replaced the dead customer_facts-upsert approach (Step 2 pivot, 2026-06-07).

Contract under test:
  - _read(path): PURE — missing / garbage / non-dict file -> {} (never raises).
  - load(): module-cached; returns a dict; never raises.
  - lookup(phone_or_cid) -> {name, revenue_aed, n_bookings} | None:
      * normalizes via the PROVEN hermes_exclusion_guards.normalize_phone
      * '<digits>@c.us' -> extract + normalize the digits
      * '<hash>@lid' (or anything that yields no phone) -> None (graceful: no
        recognition is safer than WRONG recognition)
      * miss / empty / None -> None
      * never raises; returns a COPY so a caller can't mutate the cache.

Plain-assert style; stdlib runner (the box has no pytest).
Run: python3 hermes-bridge/test_known_customers.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import known_customers  # noqa: E402
from known_customers import lookup, load, _read  # noqa: E402
from hermes_exclusion_guards import normalize_phone  # noqa: E402

# A known payer (Christian Carmona) used across the lookup tests.
PHONE = "971585359272"
ENTRY = {"name": "Christian Carmona", "revenue_aed": 994104, "n_bookings": 6}


def _set_cache(d):
    """Override the module cache and return a restore() callable."""
    orig = known_customers._CACHE
    known_customers._CACHE = d
    def restore():
        known_customers._CACHE = orig
    return restore


# ── _read: fail-safe on missing / garbage / wrong-shape ──────────────

def test_read_missing_file_is_empty_dict():
    assert _read("/no/such/known_customers_file.json") == {}


def test_read_garbage_is_empty_dict():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{not valid json,,,")
        path = f.name
    try:
        assert _read(path) == {}
    finally:
        os.unlink(path)


def test_read_non_dict_json_is_empty_dict():
    # A valid JSON that is a list (wrong shape) must degrade to {}.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(["a", "b"], f)
        path = f.name
    try:
        assert _read(path) == {}
    finally:
        os.unlink(path)


def test_read_valid_dict_round_trips():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({PHONE: ENTRY}, f)
        path = f.name
    try:
        assert _read(path) == {PHONE: ENTRY}
    finally:
        os.unlink(path)


# ── load(): module-cached, never raises ──────────────────────────────

def test_load_returns_dict_and_caches():
    known_customers._CACHE = None  # force a fresh read
    first = load()
    assert isinstance(first, dict)
    second = load()
    assert first is second  # cached: same object, not re-read


def test_load_real_file_is_populated():
    # The committed generator produced a real known_customers.json next to the
    # module; it should hold the 1,383 clean payers, each with the 3 fields.
    known_customers._CACHE = None
    d = load()
    assert isinstance(d, dict)
    assert len(d) >= 1000, len(d)
    sample = next(iter(d.values()))
    assert set(sample.keys()) == {"name", "revenue_aed", "n_bookings"}
    known_customers._CACHE = None  # don't leak the real cache into later tests


# ── lookup(): hit / miss / cid shapes / normalize equivalence ────────

def test_lookup_hit_returns_entry():
    restore = _set_cache({PHONE: dict(ENTRY)})
    try:
        assert lookup(PHONE) == ENTRY
    finally:
        restore()


def test_lookup_miss_returns_none():
    restore = _set_cache({PHONE: dict(ENTRY)})
    try:
        assert lookup("971500000000") is None
    finally:
        restore()


def test_lookup_cus_cid_extracts_digits():
    restore = _set_cache({PHONE: dict(ENTRY)})
    try:
        assert lookup(PHONE + "@c.us") == ENTRY
    finally:
        restore()


def test_lookup_lid_returns_none():
    # An @lid is a privacy hash, not a phone — never a (possibly wrong) match,
    # even when the cache is populated.
    restore = _set_cache({PHONE: dict(ENTRY), "12345": dict(ENTRY)})
    try:
        assert lookup("12345@lid") is None
    finally:
        restore()


def test_lookup_normalize_equivalence():
    restore = _set_cache({PHONE: dict(ENTRY)})
    try:
        a = lookup("+971 58 535 9272")
        b = lookup(PHONE + "@c.us")
        c = lookup(PHONE)
        assert a == b == c == ENTRY
        # sanity: the normalizer collapses all three to the same key
        assert (normalize_phone("+971 58 535 9272")
                == normalize_phone(PHONE) == PHONE)
    finally:
        restore()


def test_lookup_empty_and_none_return_none():
    restore = _set_cache({PHONE: dict(ENTRY)})
    try:
        assert lookup("") is None
        assert lookup(None) is None
        assert lookup("   ") is None
    finally:
        restore()


def test_lookup_returns_copy_not_cache_ref():
    cache = {PHONE: dict(ENTRY)}
    restore = _set_cache(cache)
    try:
        got = lookup(PHONE)
        got["name"] = "MUTATED"
        assert cache[PHONE]["name"] == "Christian Carmona"  # cache untouched
    finally:
        restore()


def test_lookup_never_raises_on_weird_input():
    restore = _set_cache({PHONE: dict(ENTRY)})
    try:
        for v in (123, [], {}, b"x"):
            try:
                lookup(v)  # must not raise
            except Exception as e:  # noqa: BLE001
                assert False, f"lookup raised on {v!r}: {e!r}"
    finally:
        restore()


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
