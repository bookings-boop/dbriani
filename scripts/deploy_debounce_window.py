#!/usr/bin/env python3
"""Widen the inbound debounce window so consecutive customer messages
consolidate into ONE draft (operator 2026-06-06: 'two drafts for the same
customer' — Xeno sent msg #9 then #10 ~45s apart; the 15s window flushed #9
before #10 arrived -> two separate draft cards).

Change (n8n 'Buffer Message' node, one line):
    const wait_seconds = BOOKING_SIGNAL.test(text) ? 5 : 15;
 -> const wait_seconds = BOOKING_SIGNAL.test(text) ? 10 : 60;

Booking-signal messages still flush fast-ish (10s); everything else waits 60s
so a customer typing a few messages over up to a minute gets ONE consolidated
reply. Trade-off: a non-urgent draft now appears up to 60s after the customer's
last message — acceptable for an approval-first queue. Tune the numbers below.

Deploys via the safe path (re-fetches the live draft queue; backs up; fails
closed). Run from the repo root:  python3 scripts/deploy_debounce_window.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

OLD = "BOOKING_SIGNAL.test(text) ? 5 : 15"
NEW = "BOOKING_SIGNAL.test(text) ? 10 : 60"
NODE = "Buffer Message"


def main():
    n = N8N()
    wf = n.get_workflow()
    node = next((x for x in wf.get("nodes", []) if x.get("name") == NODE), None)
    if not node:
        raise DeployError(f"node {NODE!r} not found in live workflow")
    code = (node.get("parameters", {}) or {}).get("jsCode", "")
    count = code.count(OLD)
    if count != 1:
        raise DeployError(
            f"expected exactly 1 occurrence of {OLD!r} in {NODE}, found {count} "
            "— ABORT (live node differs from expectation; inspect before edit)")
    node["parameters"]["jsCode"] = code.replace(OLD, NEW)
    print(f"   {NODE}: '{OLD}' -> '{NEW}'")
    n.safe_put(wf, tag="DEBOUNCE_WINDOW")
    # verify
    fresh = n.get_workflow()
    fnode = next((x for x in fresh.get("nodes", []) if x.get("name") == NODE), None)
    ok = NEW in ((fnode.get("parameters", {}) or {}).get("jsCode", "") if fnode else "")
    print("   VERIFY:", "OK — new window live" if ok else "FAILED — change not present")
    if not ok:
        raise DeployError("post-deploy verify failed")


if __name__ == "__main__":
    try:
        main()
        print("debounce window widened.")
    except DeployError as e:
        sys.exit(f"x deploy failed: {e}")
