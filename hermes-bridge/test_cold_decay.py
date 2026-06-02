#!/usr/bin/env python3
"""Unit tests for _should_cold_decay() — the hourly-sweep cold-decay guard.

Bugs fixed 2026-06-02 (QC audit B2/B3):
- B2: last_customer_message_at NULL coalesced to 'epoch' (1970) -> a fake ~56yr
  silence -> a never-messaged lead (e.g. an operator-entered future booking) was
  demoted to COLD. Guard: never decay when the customer-message timestamp is NULL.
- B3: WAITING_FOR_PAYMENT (a paid, in-flight stage) was not excluded, so a paid
  lead awaiting payment confirmation could silent-decay to COLD. Guard: exclude it
  alongside COLD and PAUSED_*.

Run: python3 hermes-bridge/test_cold_decay.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labels import _should_cold_decay  # noqa: E402

WEEK = 7 * 86400


def test_normal_stale_lead_decays():
    assert _should_cold_decay(WEEK + 1, "HOT", False) is True
    assert _should_cold_decay(30 * 86400, "WARM", False) is True


def test_fresh_lead_does_not_decay():
    assert _should_cold_decay(3 * 86400, "HOT", False) is False


def test_boundary_exactly_threshold_does_not_decay():
    assert _should_cold_decay(WEEK, "HOT", False) is False  # strictly greater


def test_null_cmsg_never_decays_even_with_huge_silence():
    # B2: NULL -> 1970 -> ~56yr fake silence must NOT decay
    assert _should_cold_decay(56 * 365 * 86400, "HOT", True) is False


def test_waiting_for_payment_excluded():
    # B3: paid in-flight stage must not silent-decay
    assert _should_cold_decay(30 * 86400, "WAITING_FOR_PAYMENT", False) is False


def test_paused_excluded():
    assert _should_cold_decay(30 * 86400, "PAUSED_AUTONOMOUS", False) is False


def test_already_cold_no_op():
    assert _should_cold_decay(30 * 86400, "COLD", False) is False


def test_none_label_safe():
    assert _should_cold_decay(30 * 86400, None, False) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} cold-decay tests passed")
