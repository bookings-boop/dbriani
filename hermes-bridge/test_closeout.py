#!/usr/bin/env python3
"""Touch-3 CLOSE-OUT — give-up card for dead leads (2026-06-16).

After a lead got both nudges (followup_count >= FOLLOWUP_CAP), stayed silent
(ghosted us), and aged past CLOSEOUT_AFTER_DAYS, post an APPROVAL-FIRST card with
the operator's close-out message. On the operator's ✅ Send (claim-send WON +
is_closeout) the lead is marked DISREGARDED so it's never re-engaged. Flag
CLOSEOUT_SWEEP_ENABLED (default OFF). Approval-first — NEVER auto.

Run: python3 hermes-bridge/test_closeout.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def test_closeout_message_is_operator_text():
    m = server.GHOST_RECOVERY_CLOSEOUT
    assert "close your request" in m, m
    assert "future bookings" in m, m
    assert "given up" in m


def test_closeout_flag_default_off():
    os.environ.pop("CLOSEOUT_SWEEP_ENABLED", None)
    assert server._closeout_enabled() is False
    os.environ["CLOSEOUT_SWEEP_ENABLED"] = "1"
    try:
        assert server._closeout_enabled() is True
    finally:
        os.environ.pop("CLOSEOUT_SWEEP_ENABLED", None)


def test_closeout_sql_gates():
    sql = server._closeout_candidate_sql()
    # cap-reached: both nudges already received
    assert "followup_count" in sql and ">=" in sql
    # ghosted us (we replied last)
    assert "last_operator_reply_at" in sql
    # aged past the give-up window since the last nudge
    assert "last_nudge_drafted_at" in sql
    # never re-close a terminal lead
    assert "DISREGARDED" in sql and "LOST" in sql and "CONFIRMED" in sql
    # canonical identities only
    assert "merged_into IS NULL" in sql


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} closeout tests passed")
