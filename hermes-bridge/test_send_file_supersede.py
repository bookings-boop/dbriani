#!/usr/bin/env python3
"""test_send_file_supersede.py — _supersede_pending_for_cid (server.py).

Verifies the shared supersede sweep used by _draft_save AND handle_send_file:
a file/brochure send (or a new draft) flips the customer's OTHER open pending
drafts to 'superseded' and drops them from the active set, while leaving
except_did and OTHER customers' drafts untouched.

Regression for the 2026-06-02 bug: a brochure sent via handle_send_file did
not run this sweep, so stale draft cards lingered ("2 drafts stay open after
sending brochure"). Runs on the box against live Redis (test prefixes).

    ssh dubriani-ec2 'python3 ~/hermes-bridge/test_send_file_supersede.py'
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

try:
    _pong, _ = server._redis(["PING"])
except Exception:  # pragma: no cover
    _pong = ""
if "PONG" not in (_pong or "").upper():
    print("SKIP test_send_file_supersede.py — redis not reachable via _redis "
          "(run on the box)")
    sys.exit(0)

P = "__test_sfs__"
CID = P + "cust"
CID2 = P + "cust2"
PASS = []
FAIL = []


def check(label, cond, detail=""):
    if cond:
        PASS.append(label)
        print(f"  ✓ {label}")
    else:
        FAIL.append((label, detail))
        print(f"  ✗ {label}  {detail}")


def _plant(did, cid, status="pending"):
    """Plant a draft directly in Redis WITHOUT going through _draft_save
    (whose own auto-supersede would interfere with the setup)."""
    d = {"id": did, "customer_phone": cid, "status": status,
         "messages": ["hi there"]}
    server._redis(["SET", server._draft_key(did), json.dumps(d),
                   "EX", str(server.QUEUE_TTL)])
    server._redis(["ZADD", server._byc_key(cid),
                   str(int(time.time() * 1000)), did])
    if status == "pending":
        server._redis(["SADD", server.DRAFTS_ACTIVE, did])


def _status(did):
    d, _ = server._draft_get(did)
    return (d or {}).get("status")


def _active(did):
    out, _ = server._redis(["SISMEMBER", server.DRAFTS_ACTIVE, did])
    return (out or "").strip() == "1"


def _clean(*ids):
    for did in ids:
        server._redis(["DEL", server._draft_key(did)])
        server._redis(["SREM", server.DRAFTS_ACTIVE, did])
    server._redis(["DEL", server._byc_key(CID)])
    server._redis(["DEL", server._byc_key(CID2)])


def test_file_send_closes_open_drafts():
    print("\n--- a file send closes the customer's open draft cards ---")
    a, b = P + "a", P + "b"
    _clean(a, b)
    _plant(a, CID); _plant(b, CID)
    check("setup: a + b both pending+active",
          _status(a) == "pending" and _status(b) == "pending"
          and _active(a) and _active(b))
    server._supersede_pending_for_cid(CID)          # simulate file-send sweep
    check("a superseded", _status(a) == "superseded", f"a={_status(a)}")
    check("b superseded", _status(b) == "superseded", f"b={_status(b)}")
    check("a dropped from active", not _active(a))
    check("b dropped from active", not _active(b))
    _clean(a, b)


def test_except_did_and_other_customer_untouched():
    print("\n--- except_did + other customers are left alone ---")
    a, b, c = P + "a", P + "b", P + "c"
    _clean(a, b, c)
    _plant(a, CID); _plant(b, CID); _plant(c, CID2)
    server._supersede_pending_for_cid(CID, except_did=b)
    check("except_did b stays pending", _status(b) == "pending",
          f"b={_status(b)}")
    check("b stays active", _active(b))
    check("a (same cust) superseded", _status(a) == "superseded",
          f"a={_status(a)}")
    check("c (other customer) untouched", _status(c) == "pending"
          and _active(c), f"c={_status(c)}")
    _clean(a, b, c)


if __name__ == "__main__":
    test_file_send_closes_open_drafts()
    test_except_did_and_other_customer_untouched()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for lbl, det in FAIL:
            print(f"  FAILED: {lbl}  {det}")
    sys.exit(1 if FAIL else 0)
