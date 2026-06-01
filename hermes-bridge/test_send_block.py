#!/usr/bin/env python3
"""Unit tests for the WAHA-degraded send-block decision (2026-06-02).

When the operator approves a draft while the WAHA session is in a DEFINITIVE
non-sending state, the send would silently fail (operator thinks it sent, the
customer gets nothing). The claim-send path now blocks the send + tells the
operator instead. The block decision is a pure function so it can't misfire:

  labels._waha_send_blocked(ok, status) — True ONLY for a definitive bad state
  (STOPPED / SCAN_QR_CODE / FAILED). WORKING never blocks; an ambiguous probe
  (UNREACHABLE / UNKNOWN / STARTING / anything else) does NOT block, so a
  transient probe blip can never wedge all customer sends.

Plain-assert style. Run: python3 hermes-bridge/test_send_block.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _waha_send_blocked  # noqa: E402


def test_working_never_blocks():
    assert _waha_send_blocked(True, "WORKING") is False


def test_definitive_bad_states_block():
    assert _waha_send_blocked(False, "STOPPED") is True
    assert _waha_send_blocked(False, "SCAN_QR_CODE") is True
    assert _waha_send_blocked(False, "FAILED") is True


def test_ambiguous_probe_does_not_block():
    # A probe blip (UNREACHABLE) or transient state must NOT block — the actual
    # send attempt + the existing badge warning cover it; blocking here would
    # let one timeout wedge every send.
    assert _waha_send_blocked(False, "UNREACHABLE") is False
    assert _waha_send_blocked(False, "UNKNOWN") is False
    assert _waha_send_blocked(False, "STARTING") is False
    assert _waha_send_blocked(False, "") is False


def test_case_insensitive():
    assert _waha_send_blocked(False, "stopped") is True
    assert _waha_send_blocked(False, "scan_qr_code") is True


def test_ok_true_wins_even_with_bad_status():
    # Defensive: if a caller ever passes ok=True with a stale bad status,
    # ok=True means the session is working → never block.
    assert _waha_send_blocked(True, "STOPPED") is False


def test_none_status_safe():
    assert _waha_send_blocked(False, None) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} send-block tests passed")
