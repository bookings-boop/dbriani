#!/usr/bin/env python3
"""Unit tests for the durable-store fixes (#11 real ts + #14 card-label denylist).

#11: _record_message_sql must accept an optional ts (WAHA epoch) so a recorded
     row carries the REAL message time, not INSERT-time now(). ts=None preserves
     the current behavior exactly (no ts column -> DEFAULT now()).
#14: _is_operator_card_text flags operator-card placeholder chrome (e.g.
     'NO REPLY - likely spam/B2B (see notes). Tap Skip.') so record_outbound_draft
     never persists it as a customer transcript line.

Run: python3 hermes-bridge/test_durable_store.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import _record_message_sql, _is_operator_card_text  # noqa: E402


# --- #11: ts param --------------------------------------------------------
def test_no_ts_preserves_current_sql():
    sql = _record_message_sql("x@c.us", "in", "hi", "m1")
    assert "to_timestamp" not in sql
    assert " ts" not in sql and "(ts" not in sql and ", ts" not in sql
    assert "ON CONFLICT (customer_id, msg_id)" in sql


def test_ts_seconds_sets_to_timestamp():
    sql = _record_message_sql("x@c.us", "in", "hi", "m1", ts=1780828050)
    assert "ts" in sql and "to_timestamp(1780828050" in sql


def test_ts_milliseconds_normalized_to_seconds():
    sql = _record_message_sql("x@c.us", "in", "hi", "m1", ts=1780828050000)
    assert "to_timestamp(1780828050" in sql  # ms -> s, not 1.78e12


def test_ts_zero_or_garbage_omits_column():
    assert "to_timestamp" not in _record_message_sql("x@c.us", "in", "h", "m", ts=0)
    assert "to_timestamp" not in _record_message_sql("x@c.us", "in", "h", "m", ts="nope")
    assert "to_timestamp" not in _record_message_sql("x@c.us", "in", "h", "m", ts=None)


# --- #14: card-label denylist --------------------------------------------
def test_card_label_text_flagged():
    assert _is_operator_card_text("NO REPLY - likely spam/B2B (see notes). Tap Skip.") is True
    assert _is_operator_card_text("Tap Skip") is True
    assert _is_operator_card_text("No auto-reply") is True


def test_real_customer_message_not_flagged():
    assert _is_operator_card_text(
        "Hi! The Bliss 55 is available Saturday 5-9PM — shall I hold it for you?") is False
    assert _is_operator_card_text("yes please send the link") is False


def test_card_label_none_empty_safe():
    assert _is_operator_card_text("") is False
    assert _is_operator_card_text(None) is False


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} durable_store tests passed")
