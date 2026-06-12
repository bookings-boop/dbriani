#!/usr/bin/env python3
"""F-B payment-triggered re-analysis enqueue — Bug #4 (2026-06-12).

A received payment must force a fresh re-analysis (bypassing the 30-min cooldown
marker) so the stored score/advice stop reflecting the pre-payment state. Gated
by PAYMENT_TRIGGERS_REANALYSIS_ENABLED. Redis is mocked.

Run: python3 hermes-bridge/test_reanalyze_on_payment.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402
import server  # noqa: E402


def test_off_is_noop():
    os.environ["PAYMENT_TRIGGERS_REANALYSIS_ENABLED"] = "0"
    calls = []
    routes._redis = lambda args, **k: (calls.append(list(args)), ("OK", None))[1]
    server.canonicalize_cid = lambda c: c
    routes._reanalyze_on_payment("x@c.us")
    assert calls == [], calls  # flag off → never touches Redis


def test_on_bypasses_cooldown_and_enqueues():
    os.environ["PAYMENT_TRIGGERS_REANALYSIS_ENABLED"] = "1"
    try:
        calls = []
        routes._redis = lambda args, **k: (calls.append(list(args)),
                                           ("OK", None))[1]
        server.canonicalize_cid = lambda c: c
        routes._reanalyze_on_payment("971@c.us")
        cmds = [c[0] for c in calls]
        assert "DEL" in cmds, cmds          # cooldown marker deleted (bypass)
        assert "SET" in cmds, cmds          # _enqueue_reanalyze re-arms marker
        assert "RPUSH" in cmds, cmds        # lead pushed onto the reanalyze queue
        assert cmds.index("DEL") < cmds.index("SET"), cmds  # bypass BEFORE re-arm
        # the DEL + RPUSH target the same cid's marker / queue
        assert any(c[0] == "DEL" and "971@c.us" in c[1] for c in calls), calls
    finally:
        os.environ["PAYMENT_TRIGGERS_REANALYSIS_ENABLED"] = "0"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} reanalyze-on-payment tests passed")
