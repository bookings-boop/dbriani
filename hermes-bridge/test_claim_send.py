#!/usr/bin/env python3
"""Regression test for the handle_queue `_redis` shadow (2026-06-01 incident).

Commit 983861b added `from db import _redis` INSIDE handle_queue (the new
'awaiting' action). A function-local import makes `_redis` a local for the
WHOLE function, so the 'claim-send' action (which references the module-level
_redis before that line) raised:

    UnboundLocalError: cannot access local variable '_redis'

Every send-claim crashed → send-race protection dead, malformed bridge
responses (n8n shipped a literal `false` to customers), and draft supersede
broke (stale "ready to send" cards survived next to already-sent ones).

The fix: drop the redundant local import (module-level `_redis` is imported at
routes.py top). This test asserts `_redis` is NOT a function-local of
handle_queue, which is redis-free, instant, and catches any re-introduction.

Run: python3 hermes-bridge/test_claim_send.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402


def test_redis_not_shadowed_in_handle_queue():
    locals_ = routes.handle_queue.__code__.co_varnames
    assert "_redis" not in locals_, (
        "_redis is a function-local in handle_queue (a `from db import _redis` "
        "shadow) — the claim-send path will UnboundLocalError on every send")


def test_redis_is_a_module_global():
    # positive: _redis resolves to the module-level import (a free/global name)
    assert "_redis" in routes.handle_queue.__code__.co_names


def test_claim_send_won_bumps_operator_reply_state():
    """State-sync (2026-06-09): the claim-send WON branch must bump
    conversation_state(operator_reply) for the SAME draft it records to the
    transcript — so /review's owe verdict can't go stale when the n8n
    "Mark Op Reply (Approved)" node aborts in its post-send done-chain.
    Behavioral: drive a clean WON (force=True skips the sibling guard; a pending
    draft passes the status guard; SET-NX returns OK) with record_outbound_draft
    stubbed True, and assert upsert_conversation_state(cid, "operator_reply")
    fired with the draft's canonical phone."""
    import server
    calls = []
    saved = (server._draft_get, routes._redis,
             server.record_outbound_draft, server.upsert_conversation_state)
    draft = {"id": "d1", "customer_phone": "999000111@lid", "status": "pending",
             "messages": ["here are a few options for you"]}
    server._draft_get = lambda did: (draft, None)
    routes._redis = lambda args, *a, **k: (
        ("OK", None) if (args and str(args[0]).upper() == "SET") else ("", None))
    server.record_outbound_draft = lambda d: True            # a REAL reply recorded
    server.upsert_conversation_state = lambda cid, ev: (
        calls.append((cid, ev)) or ("ok", None))
    sent = {}
    try:
        routes.handle_queue(
            {"action": "claim-send", "draft_id": "d1", "force": True},
            lambda code, obj: sent.update(code=code, obj=obj))
    finally:
        (server._draft_get, routes._redis,
         server.record_outbound_draft, server.upsert_conversation_state) = saved
    assert ("999000111@lid", "operator_reply") in calls, (
        "claim-send WON must bump conversation_state(operator_reply) beside the "
        "transcript write — got: " + repr(calls))


def test_claim_send_skips_state_bump_for_card_chrome():
    """A draft that record_outbound_draft REJECTS (operator-card chrome / empty —
    returns False) is NOT an operator reply, so it must NOT bump
    last_operator_reply_at (else a 'Tap Skip' card falsely marks us as replied)."""
    import server
    calls = []
    saved = (server._draft_get, routes._redis,
             server.record_outbound_draft, server.upsert_conversation_state)
    draft = {"id": "d2", "customer_phone": "999000222@lid", "status": "pending",
             "messages": ["NO REPLY - likely spam/B2B. Tap Skip."]}
    server._draft_get = lambda did: (draft, None)
    routes._redis = lambda args, *a, **k: (
        ("OK", None) if (args and str(args[0]).upper() == "SET") else ("", None))
    server.record_outbound_draft = lambda d: False           # chrome -> not recorded
    server.upsert_conversation_state = lambda cid, ev: (
        calls.append((cid, ev)) or ("ok", None))
    try:
        routes.handle_queue(
            {"action": "claim-send", "draft_id": "d2", "force": True},
            lambda code, obj: None)
    finally:
        (server._draft_get, routes._redis,
         server.record_outbound_draft, server.upsert_conversation_state) = saved
    assert calls == [], (
        "card-chrome (record_outbound_draft False) must NOT bump operator_reply "
        "— got: " + repr(calls))


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} claim-send shadow tests passed")
