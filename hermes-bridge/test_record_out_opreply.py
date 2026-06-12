#!/usr/bin/env python3
"""operator_reply_echo event — Bug #1 bridge half (2026-06-12, dormant).

An operator's DIRECT phone reply (WAHA fromMe) must bump last_operator_reply_at
so /review stops showing "🔴 needs your reply" after they answered — WITHOUT
resetting reengage_attempts/followup_count (bot sends echo through fromMe too;
zeroing the counter would re-arm the FOLLOWUP_CAP and nudge forever). DB layer is
mocked so the SQL is asserted without a live write.

Run: python3 hermes-bridge/test_record_out_opreply.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def _capture_sql():
    cap = {}
    server.canonicalize_cid = lambda c: c
    server._psql = lambda sql, *a, **k: (cap.__setitem__("sql", sql),
                                         ("ok", None))[1]
    return cap


def test_operator_reply_echo_bumps_reply_only():
    cap = _capture_sql()
    res = server.upsert_conversation_state("971569631989@c.us",
                                           "operator_reply_echo")
    sql = cap["sql"]
    assert "last_operator_reply_at" in sql, sql
    # The whole point: must NOT reset the proactive-followup counters.
    assert "reengage_attempts" not in sql, sql
    assert "followup_count" not in sql, sql
    # Monotonic — a replayed old echo can't move the clock backwards.
    assert "GREATEST" in sql, sql
    assert res == ("ok", None)


def test_operator_reply_resets_counter_unchanged():
    # Guard against accidentally aliasing the echo onto the real operator_reply
    # branch — operator_reply (draft pipeline) SHOULD still reset reengage_attempts.
    cap = _capture_sql()
    server.upsert_conversation_state("x@c.us", "operator_reply")
    assert "reengage_attempts = 0" in cap["sql"], cap["sql"]


def test_unknown_event_still_unknown():
    server.canonicalize_cid = lambda c: c
    res = server.upsert_conversation_state("x@c.us", "bogus_event")
    assert res[0] == "" and "unknown event" in (res[1] or ""), res


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} operator_reply_echo tests passed")
