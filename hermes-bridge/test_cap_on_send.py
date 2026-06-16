#!/usr/bin/env python3
"""CAP-BUG retiming — FOLLOWUP_CAP spends on SEND, not POST (2026-06-16).

The proactive ghost-recovery engine bounds itself to FOLLOWUP_CAP unsolicited
follow-ups per silence window. The legacy bug: that budget was burned when a
card was POSTED to Telegram (nudge_drafted), so a lead whose cards the operator
never tapped ✅ Send on still hit the cap and got permanently excluded having
delivered ZERO messages.

The fix splits the single nudge_drafted event into two, gated by
FOLLOWUP_CAP_ON_SEND_ENABLED (default OFF = legacy behavior byte-for-byte):
  - nudge_carded : on POST -> cooldown (last_nudge_drafted_at) + reengage_attempts,
                   NO followup_count++  (the lead is throttled but un-burned)
  - nudge_sent   : on the operator's ACTUAL ✅ Send of a PROACTIVE nudge ->
                   followup_count++ only.

Scoping matters: is_followup is True for BOTH proactive nudges AND owe-reply
direct replies; only genuine proactive nudges (is_proactive_nudge) spend the cap.

DB layer mocked — SQL asserted without a live write.
Run: python3 hermes-bridge/test_cap_on_send.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import routes  # noqa: E402

CAP_INC = "followup_count = conversation_state.followup_count + 1"


def _capture_sql():
    cap = {}
    server.canonicalize_cid = lambda c: c
    server._psql = lambda sql, *a, **k: (cap.__setitem__("sql", sql),
                                         ("ok", None))[1]
    return cap


# ---- SQL-level: the two new events ----------------------------------------

def test_nudge_carded_sets_cooldown_but_not_cap():
    cap = _capture_sql()
    server.upsert_conversation_state("971500000001@c.us", "nudge_carded")
    sql = cap["sql"]
    assert "last_nudge_drafted_at = now()" in sql, sql      # cooldown bumped
    assert "reengage_attempts" in sql, sql                   # attempt tracked
    assert CAP_INC not in sql, ("nudge_carded must NOT spend the cap — that is "
                                "the whole bug being fixed:\n" + sql)


def test_nudge_sent_bumps_cap_only():
    cap = _capture_sql()
    server.upsert_conversation_state("971500000002@c.us", "nudge_sent")
    sql = cap["sql"]
    assert CAP_INC in sql, ("nudge_sent must spend exactly one unit of the "
                            "FOLLOWUP_CAP budget:\n" + sql)
    # The ON CONFLICT UPDATE (normal path — the candidate row already exists)
    # must touch ONLY followup_count + updated_at; cooldown + attempt counter
    # belong to the POST (nudge_carded), not the send.
    update = sql.split("DO UPDATE", 1)[1]
    assert "last_nudge_drafted_at" not in update, update
    assert "reengage_attempts" not in update, update


def test_legacy_nudge_drafted_unchanged():
    # Flag-OFF path must remain byte-for-byte legacy: cooldown + cap together.
    cap = _capture_sql()
    server.upsert_conversation_state("971500000003@c.us", "nudge_drafted")
    sql = cap["sql"]
    assert "last_nudge_drafted_at = now()" in sql, sql
    assert CAP_INC in sql, sql


# ---- event-name selector / flag -------------------------------------------

def test_post_event_flag_off_is_nudge_drafted():
    os.environ.pop("FOLLOWUP_CAP_ON_SEND_ENABLED", None)
    assert server._nudge_post_event() == "nudge_drafted"
    assert server._followup_cap_on_send() is False


def test_post_event_flag_on_is_nudge_carded():
    os.environ["FOLLOWUP_CAP_ON_SEND_ENABLED"] = "1"
    try:
        assert server._nudge_post_event() == "nudge_carded"
        assert server._followup_cap_on_send() is True
    finally:
        os.environ.pop("FOLLOWUP_CAP_ON_SEND_ENABLED", None)


# ---- claim-send: send-side cap bump, correctly scoped ----------------------

def _drive_claim_send(draft, flag_on, recorded=True, redis_stub=None, runs=1):
    """Drive `runs` send-claim attempts and capture upsert_conversation_state
    calls. force=True skips the sibling guard; pending status passes. The default
    redis stub returns OK for every SET; pass a stateful redis_stub to exercise
    the NX idempotency marker. `recorded` controls record_outbound_draft."""
    calls = []
    saved = (server._draft_get, routes._redis,
             server.record_outbound_draft, server.upsert_conversation_state)
    if flag_on:
        os.environ["FOLLOWUP_CAP_ON_SEND_ENABLED"] = "1"
    else:
        os.environ.pop("FOLLOWUP_CAP_ON_SEND_ENABLED", None)
    server._draft_get = lambda did: (draft, None)
    routes._redis = redis_stub or (lambda args, *a, **k: (
        ("OK", None) if (args and str(args[0]).upper() == "SET") else ("", None)))
    server.record_outbound_draft = lambda d: recorded
    server.upsert_conversation_state = lambda cid, ev: (
        calls.append((cid, ev)) or ("ok", None))
    try:
        for _ in range(runs):
            routes.handle_queue(
                {"action": "claim-send", "draft_id": draft["id"], "force": True},
                lambda code, obj: None)
    finally:
        (server._draft_get, routes._redis,
         server.record_outbound_draft, server.upsert_conversation_state) = saved
        os.environ.pop("FOLLOWUP_CAP_ON_SEND_ENABLED", None)
    return calls


def test_claim_send_proactive_nudge_spends_cap_when_flag_on():
    draft = {"id": "n1", "customer_phone": "999000111@lid", "status": "pending",
             "messages": ["just checking back in on your charter"],
             "is_followup": True, "is_proactive_nudge": True}
    calls = _drive_claim_send(draft, flag_on=True)
    assert ("999000111@lid", "nudge_sent") in calls, (
        "a PROACTIVE nudge actually sent must spend the cap (nudge_sent) — got: "
        + repr(calls))
    # operator_reply still fires (re-arms silence / clears owe badge).
    assert ("999000111@lid", "operator_reply") in calls, repr(calls)


def test_claim_send_owe_reply_does_not_spend_cap():
    # is_followup True (owe-reply card) but NOT a proactive nudge -> never spends
    # the proactive budget, even with the flag on.
    draft = {"id": "o1", "customer_phone": "999000222@lid", "status": "pending",
             "messages": ["yes the 12th works, here are the details"],
             "is_followup": True}
    calls = _drive_claim_send(draft, flag_on=True)
    assert ("999000222@lid", "nudge_sent") not in calls, (
        "owe-reply (is_followup but not is_proactive_nudge) must NOT spend the "
        "proactive cap — got: " + repr(calls))
    assert ("999000222@lid", "operator_reply") in calls, repr(calls)


def test_claim_send_flag_off_never_spends_cap():
    # Shadow safety: even a proactive nudge does not fire nudge_sent when OFF.
    draft = {"id": "p1", "customer_phone": "999000333@lid", "status": "pending",
             "messages": ["checking in"],
             "is_followup": True, "is_proactive_nudge": True}
    calls = _drive_claim_send(draft, flag_on=False)
    assert ("999000333@lid", "nudge_sent") not in calls, (
        "flag OFF must be a no-op on the send side (shadow-first) — got: "
        + repr(calls))


def test_claim_send_chrome_nudge_does_not_spend_cap():
    # record_outbound_draft False (card chrome / empty) must NOT spend the cap —
    # same _recorded gate as operator_reply. A genuine nudge always has a body so
    # this is defensive, but it keeps the cap aligned to REAL outbounds.
    draft = {"id": "c1", "customer_phone": "999000444@lid", "status": "pending",
             "messages": ["NO REPLY - chrome"],
             "is_followup": True, "is_proactive_nudge": True}
    calls = _drive_claim_send(draft, flag_on=True, recorded=False)
    assert ("999000444@lid", "nudge_sent") not in calls, (
        "an unrecorded (chrome) send must not spend the proactive cap — got: "
        + repr(calls))


def test_claim_send_nudge_sent_is_idempotent_per_draft():
    # A retry that re-WINS the send-claim after its TTL expired must NOT
    # double-spend the cap: the nudge_sent:<did> NX marker dedupes. Stateful
    # redis: the claim key always re-wins (simulating TTL expiry) but the
    # nudge_sent marker persists, so the SECOND run's NX SET fails.
    seen = set()

    def _stateful(args, *a, **k):
        if args and str(args[0]).upper() == "SET":
            key = str(args[1])
            if key.startswith("nudge_sent:"):
                if key in seen:
                    return ("", None)        # NX fails — already spent
                seen.add(key)
                return ("OK", None)
            return ("OK", None)              # claim key etc — always re-win
        return ("", None)

    draft = {"id": "i1", "customer_phone": "999000555@lid", "status": "pending",
             "messages": ["checking back in"],
             "is_followup": True, "is_proactive_nudge": True}
    calls = _drive_claim_send(draft, flag_on=True, redis_stub=_stateful, runs=2)
    ns = [c for c in calls if c == ("999000555@lid", "nudge_sent")]
    assert len(ns) == 1, (
        "nudge_sent must fire exactly once across two send-claims for the same "
        "draft — got " + repr(ns) + " from all calls " + repr(calls))


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"all {len(fns)} cap-on-send tests passed")
