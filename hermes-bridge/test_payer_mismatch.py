#!/usr/bin/env python3
"""Unit tests for _payer_mismatch() — payment payer-vs-customer matcher.

When Nomod confirms a payment, _payer_mismatch decides whether the
charge.customer_info phone matches the customer we sent the link to.

  False (no mismatch) is the SAFE default — operator gets no scary
  warning when we can't actually verify, and CONFIRMED still fires.
  True (mismatch) shows the operator a note so CRM can link the
  charge to the customer when a third party paid.

Layers checked, in order:
  A) customer_id digit suffix (catches @c.us-shape IDs — phone IS id)
  B) customer_facts.name digit suffix (catches phone-string pushNames)
  C) WAHA chat pushName digit suffix (catches @lid hashed IDs)

Plain-assert style. Run: python3 hermes-bridge/test_payer_mismatch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _payer_mismatch  # noqa: E402


def _payer(phone):
    """Build a Nomod charge.customer_info-shaped dict."""
    return {"phone_number": phone}


def test_no_row_no_mismatch():
    # No customer row → can't verify → don't false-flag.
    assert _payer_mismatch(_payer("971501234567"), None) is False
    assert _payer_mismatch(_payer("971501234567"), {}) is False


def test_no_payer_phone_no_mismatch():
    # Operator wants no warning when we can't actually compare.
    assert _payer_mismatch({"phone_number": ""},
                           {"customer_id": "971501234567@c.us"}) is False
    assert _payer_mismatch({"phone_number": None},
                           {"customer_id": "971501234567@c.us"}) is False


def test_at_cus_match():
    # Layer A: @c.us customer_id literally contains the phone.
    row = {"customer_id": "971501234567@c.us"}
    assert _payer_mismatch(_payer("971501234567"), row) is False
    # With + sign / spaces — should still match (digits-only compare).
    assert _payer_mismatch(_payer("+971 50 123 4567"), row) is False


def test_at_cus_mismatch():
    # Different phone for an @c.us id → REAL mismatch.
    row = {"customer_id": "971501234567@c.us"}
    assert _payer_mismatch(_payer("971559998888"), row) is True


def test_name_digit_fallback():
    # Layer B: WAHA pushName before backfill is a phone-string. Even
    # when customer_id is @lid (hashed), the .name field carries the
    # phone digits and should suffice.
    row = {"customer_id": "211429160448230@lid",
           "name": "+971 55 999 8888"}
    assert _payer_mismatch(_payer("971559998888"), row) is False


def test_lid_with_waha_chats_match():
    # Layer C: @lid customer_id, real human name in customer_facts.
    # Only WAHA chat list has the phone-pushName mapping.
    row = {"customer_id": "211429160448230@lid", "name": "Mark Hassan"}
    waha = [
        {"_serialized": "211429160448230@lid", "name": "+971 50 717 7877"},
        {"_serialized": "971508778669@c.us", "name": "Hussein"},
    ]
    assert _payer_mismatch(_payer("971507177877"), row, waha) is False


def test_lid_with_waha_chats_mismatch():
    # @lid customer, WAHA pushName carries phone, payer is someone else.
    row = {"customer_id": "211429160448230@lid", "name": "Mark Hassan"}
    waha = [
        {"_serialized": "211429160448230@lid", "name": "+971 50 717 7877"},
    ]
    assert _payer_mismatch(_payer("971559998888"), row, waha) is True


def test_lid_waha_chat_not_a_phone():
    # @lid + WAHA pushName is a real human name (not digits) — we can't
    # verify via Layer C. False-default per the safety policy.
    row = {"customer_id": "211429160448230@lid", "name": "Mark Hassan"}
    waha = [
        {"_serialized": "211429160448230@lid", "name": "Mark"},
    ]
    assert _payer_mismatch(_payer("971559998888"), row, waha) is False


def test_no_comparable_data_safe_default():
    # When customer_id is @lid (hashed, no phone digits in ID), name is
    # missing, AND no WAHA chats were passed, there's nothing to compare
    # against — function defaults to False (don't false-flag the
    # operator about a payment we can't actually verify).
    row = {"customer_id": "@lid", "name": ""}  # no digits anywhere
    assert _payer_mismatch(_payer("971501234567"), row) is False


def test_data_present_but_nothing_matches_is_mismatch():
    # The flip side: comparable data exists (@c.us-shape id) but the
    # payer phone matches none of it → REAL mismatch, surface it.
    row = {"customer_id": "971501234567@c.us"}
    assert _payer_mismatch(_payer("971559998888"), row) is True


def test_waha_id_via_nested_id_field():
    # WAHA sometimes returns {id: {_serialized: ...}} instead of a
    # top-level _serialized. Both must work.
    row = {"customer_id": "211429160448230@lid", "name": "Mark"}
    waha_nested = [
        {"id": {"_serialized": "211429160448230@lid"},
         "name": "+971 50 717 7877"},
    ]
    assert _payer_mismatch(_payer("971507177877"), row, waha_nested) \
        is False


def test_lid_hash_suffix_does_not_shortcircuit_real_mismatch():
    # B5: an @lid customer_id is a HASH, not a phone. A 9-digit suffix
    # coincidence between the hash digits and the payer must NOT clear a REAL
    # mismatch via Layer A — @lid is resolved by Layer C (WAHA pushName), which
    # here shows a different real phone, so this is a genuine mismatch.
    row = {"customer_id": "999509876543@lid", "name": "Khalid"}  # hash ~ tail
    waha = [{"id": {"_serialized": "999509876543@lid"},
             "name": "+971501112222"}]                            # real, different
    assert _payer_mismatch(_payer("971509876543"), row, waha) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} payer_mismatch tests passed")
