#!/usr/bin/env python3
"""Unit tests for review._owe_reply_candidates() — the candidate filter for the
proactive UNANSWERED-customer sweep (operator 2026-06-08: "too many customers
get lost waiting on a response and we don't even see it").

Keeps leads where WE owe a reply (_owes_reply True) and the label is not a
terminal/closed or operator-paused state, sorted longest-unanswered first.
Exclusion-guard + cap are applied by the caller, not here. Pure.

Run: python3 hermes-bridge/test_owe_reply_candidates.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review import _owe_reply_candidates, _last_msg_is_inbound  # noqa: E402


def _row(label="HOT", cs=3600, op=7200, nudge=None, cid="x@c.us"):
    # seconds = AGE in seconds (smaller = more recent). cs < op => we owe.
    return {"customer_id": cid, "label": label,
            "last_customer_message_at_seconds": cs,
            "last_operator_reply_at_seconds": op,
            "last_nudge_drafted_at_seconds": nudge}


def test_owed_hot_included():
    out = _owe_reply_candidates([_row("HOT", cs=3600, op=7200)])
    assert len(out) == 1


def test_not_owed_excluded():
    # we replied more recently than the customer -> NOT owed
    out = _owe_reply_candidates([_row("HOT", cs=7200, op=3600)])
    assert out == []


def test_confirmed_owed_included():
    # Xeno: CONFIRMED + an unanswered new message ("no valet parking?") -> owed
    out = _owe_reply_candidates([_row("CONFIRMED", cs=1800, op=7200)])
    assert len(out) == 1


def test_terminal_and_paused_excluded_even_if_owed():
    rows = [_row("DISREGARDED", cs=100, op=9000),
            _row("LOST", cs=100, op=9000),
            _row("SCAM", cs=100, op=9000),
            _row("COMPLETED", cs=100, op=9000),
            _row("PAUSED_OPERATOR", cs=100, op=9000)]
    assert _owe_reply_candidates(rows) == []


def test_sorted_longest_unanswered_first():
    a = _row("WARM", cs=3600, op=99999, cid="a@c.us")   # owed 1h
    b = _row("WARM", cs=86400, op=99999, cid="b@c.us")  # owed 24h (longer)
    out = _owe_reply_candidates([a, b])
    assert [r["customer_id"] for r in out] == ["b@c.us", "a@c.us"]


def test_none_safe():
    assert _owe_reply_candidates(None) == []


# --- _last_msg_is_inbound: the WAHA last-direction guard (anti-stale-owe) -----
def test_last_inbound_means_still_owed():
    raw = [{"ts": 100, "direction": "out", "body": "hi"},
           {"ts": 200, "direction": "in", "body": "no valet parking?"}]
    assert _last_msg_is_inbound(raw) is True  # Xeno: customer's Q is last -> card


def test_last_outbound_means_answered_skip():
    # staff replied last (possibly from another phone) -> NOT really owed -> skip
    raw = [{"ts": 100, "direction": "in", "body": "?"},
           {"ts": 200, "direction": "out", "body": "yes sir"}]
    assert _last_msg_is_inbound(raw) is False


def test_no_waha_evidence_returns_none_for_fallback():
    # empty -> None so the caller falls back to conversation_state (never miss)
    assert _last_msg_is_inbound([]) is None
    assert _last_msg_is_inbound(None) is None


def test_picks_most_recent_by_ts_not_order():
    raw = [{"ts": 500, "direction": "in", "body": "later msg"},
           {"ts": 100, "direction": "out", "body": "earlier"}]
    assert _last_msg_is_inbound(raw) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} owe_reply_candidates tests passed")
