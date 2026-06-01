#!/usr/bin/env python3
"""
test_queue.py — unit tests for the Redis-backed pendingQueue helpers.

Runs against the live Redis on the bridge box (uses test prefixes so it
won't collide with real drafts). Run on the box:

    python3 ~/hermes-bridge/test_queue.py

Or via SSH:
    ssh dubriani-ec2 'python3 ~/hermes-bridge/test_queue.py'

All test draft ids start with `__test__` so they're easy to clean up.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


# Environment guard — this suite exercises the LIVE Redis, which the bridge
# reaches via `docker exec <redis container> redis-cli` (db.py._redis), NOT a
# host redis-cli binary. Probe the REAL transport (a PING) rather than
# shutil.which("redis-cli"): the box has no host redis-cli, so the old which()
# guard skipped this suite on the box even though _redis works fine. Skips
# cleanly where redis isn't reachable (e.g. a laptop without the docker stack).
try:
    _pong, _ = server._redis(["PING"])
except Exception:  # pragma: no cover
    _pong = ""
if "PONG" not in (_pong or "").upper():
    print("SKIP test_queue.py — redis not reachable via _redis "
          "(run on the box: ssh dubriani-ec2 "
          "'python3 ~/hermes-bridge/test_queue.py')")
    sys.exit(0)


TEST_PREFIX = "__test_q__"
PASS = []
FAIL = []


def _clean(*ids):
    """Best-effort cleanup of test data."""
    for did in ids:
        server._redis(["DEL", server._draft_key(did)])
        server._redis(["SREM", server.DRAFTS_ACTIVE, did])
    server._redis(["DEL", server._byc_key(TEST_PREFIX + "cust")])


def check(label, cond, detail=""):
    if cond:
        PASS.append(label)
        print(f"  ✓ {label}")
    else:
        FAIL.append((label, detail))
        print(f"  ✗ {label}  {detail}")


def test_save_get_roundtrip():
    print("\n--- save → get round-trips ---")
    did = TEST_PREFIX + "rt1"
    cid = TEST_PREFIX + "cust"
    _clean(did)
    draft = {
        "id": did, "customer_phone": cid, "status": "pending",
        "messages": ["hi"], "draft_text": "hi mark", "is_payment": False,
    }
    ok, err = server._draft_save(draft)
    check("save returns ok", ok and err is None, f"err={err}")
    d, err = server._draft_get(did)
    check("get returns the draft", d is not None and err is None,
          f"d={d!r} err={err}")
    check("draft_text round-trips", d and d.get("draft_text") == "hi mark")
    check("is_payment round-trips", d and d.get("is_payment") is False)
    # active set
    out, _ = server._redis(["SISMEMBER", server.DRAFTS_ACTIVE, did])
    check("added to drafts:active", (out or "").strip() == "1",
          f"out={out!r}")
    _clean(did)


def test_save_rejects_missing_fields():
    print("\n--- save validates required fields ---")
    ok, err = server._draft_save({"id": "", "customer_phone": "x"})
    check("missing id rejected", not ok and "required" in (err or ""))
    ok, err = server._draft_save({"id": "x", "customer_phone": ""})
    check("missing customer_phone rejected", not ok and "required" in (err or ""))
    ok, err = server._draft_save("not a dict")
    check("non-dict rejected", not ok and err is not None)


def test_update_patches_fields():
    print("\n--- update patches + preserves other fields ---")
    did = TEST_PREFIX + "up1"
    cid = TEST_PREFIX + "cust"
    _clean(did)
    server._draft_save({"id": did, "customer_phone": cid,
                        "status": "pending", "messages": ["a"],
                        "draft_text": "orig", "messages_sent_count": 0})
    d, err = server._draft_update(did, {"draft_text": "patched"})
    check("update returns merged dict",
          d and d.get("draft_text") == "patched" and err is None)
    check("update preserves status",
          d and d.get("status") == "pending")
    check("update preserves messages_sent_count",
          d and d.get("messages_sent_count") == 0)
    # Verify via fresh GET
    d2, _ = server._draft_get(did)
    check("update persisted to Redis",
          d2 and d2.get("draft_text") == "patched")
    _clean(did)


def test_mark_sent_removes_from_active():
    print("\n--- mark 'sent' removes from drafts:active ---")
    did = TEST_PREFIX + "ms1"
    cid = TEST_PREFIX + "cust"
    _clean(did)
    server._draft_save({"id": did, "customer_phone": cid,
                        "status": "pending", "messages": ["x"]})
    out, _ = server._redis(["SISMEMBER", server.DRAFTS_ACTIVE, did])
    check("before: in active set", (out or "").strip() == "1")
    d, _ = server._draft_update(did, {"status": "sent"})
    check("mark sent returns draft", d and d.get("status") == "sent")
    out, _ = server._redis(["SISMEMBER", server.DRAFTS_ACTIVE, did])
    check("after: removed from active set",
          (out or "").strip() == "0", f"out={out!r}")
    _clean(did)


def test_update_missing_draft():
    print("\n--- update on missing draft returns error ---")
    d, err = server._draft_update(TEST_PREFIX + "no_such",
                                  {"status": "sent"})
    check("missing draft → None + 'not found'",
          d is None and "not found" in (err or ""), f"err={err}")


def test_latest_for_customer_picks_newest():
    print("\n--- latest-for-customer + auto-supersede semantics ---")
    cid = TEST_PREFIX + "cust"
    did_old = TEST_PREFIX + "old"
    did_new = TEST_PREFIX + "new"
    _clean(did_old, did_new)
    server._draft_save({"id": did_old, "customer_phone": cid,
                        "status": "pending"})
    time.sleep(0.05)  # ensure a later timestamp
    # Saving a newer pending draft AUTO-SUPERSEDES the older one
    # (server._draft_save, Luke double-drafts fix 2026-05-27), so a
    # customer never has two pending drafts at once. The old test asserted
    # a "next-newest pending" that auto-supersede made impossible — updated
    # 2026-06-02 to encode the real (correct) behavior instead.
    server._draft_save({"id": did_new, "customer_phone": cid,
                        "status": "pending"})
    old_now, _ = server._draft_get(did_old)
    check("older pending auto-superseded on newer save",
          old_now and old_now.get("status") == "superseded",
          f"got={old_now.get('status') if old_now else None}")
    d, err = server._draft_latest_for_customer(cid)
    check("returned the newest draft id",
          d and d.get("id") == did_new and err is None,
          f"got={d.get('id') if d else None}  err={err}")
    d, _ = server._draft_latest_for_customer(cid, want_status="pending")
    check("newest IS the only pending draft",
          d and d.get("id") == did_new,
          f"got={d.get('id') if d else None}")
    # Once the sole pending draft is sent, NO pending remains (the older one
    # is superseded, not pending) → None is correct.
    server._draft_update(did_new, {"status": "sent"})
    d, _ = server._draft_latest_for_customer(cid, want_status="pending")
    check("no pending left after sending the newest → None",
          d is None, f"got={d.get('id') if d else None}")
    d, _ = server._draft_latest_for_customer(cid, want_status="superseded")
    check("superseded filter still finds the older draft",
          d and d.get("id") == did_old,
          f"got={d.get('id') if d else None}")
    d, _ = server._draft_latest_for_customer(cid, want_status="skipped")
    check("filtered by absent status returns None",
          d is None, f"got={d}")
    _clean(did_old, did_new)


def test_get_missing_key():
    print("\n--- get on missing key returns (None, None) ---")
    d, err = server._draft_get(TEST_PREFIX + "definitely_not_there")
    check("missing key → (None, None)", d is None and err is None,
          f"d={d!r} err={err}")


def test_drop_cleans_indexes():
    print("\n--- drop removes key + active membership ---")
    did = TEST_PREFIX + "drop1"
    cid = TEST_PREFIX + "cust"
    _clean(did)
    server._draft_save({"id": did, "customer_phone": cid,
                        "status": "pending"})
    ok, err = server._draft_drop(did)
    check("drop returns ok", ok and err is None)
    d, _ = server._draft_get(did)
    check("after drop: get returns None", d is None)
    out, _ = server._redis(["SISMEMBER", server.DRAFTS_ACTIVE, did])
    check("after drop: not in active set", (out or "").strip() == "0")


def main():
    print("=== test_queue.py ===")
    tests = [
        test_save_get_roundtrip,
        test_save_rejects_missing_fields,
        test_update_patches_fields,
        test_mark_sent_removes_from_active,
        test_update_missing_draft,
        test_latest_for_customer_picks_newest,
        test_get_missing_key,
        test_drop_cleans_indexes,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  ✗ {t.__name__} raised: {e!r}")
            FAIL.append((t.__name__, repr(e)))
    print()
    print(f"=== {len(PASS)} pass / {len(FAIL)} fail ===")
    if FAIL:
        for label, detail in FAIL:
            print(f"  FAIL: {label}  {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
