#!/usr/bin/env python3
"""
build_nomod_skip_improver.py — Nomod follow-up #2: skip the FR-5 improver on
payment drafts.

Bug: the improver's Apply Improvement mutates the pendingQueue draft's
`messages` with Hermes /improve's "improved" reply — which has NO knowledge
of the deterministic booking block + link Build Payment Message appended.
Result: the link is stripped from the queue before the operator presses Send,
and Send One to Customer dispatches a link-less message. Observed live in
exec 751/752: card initially had the link, was edited to the improved (linkless)
version, and the sent text matched the improved version.

Fix — 2 small edits:
- Queue & Format: tag `pending.is_payment = !!paymentMessage`.
- Check Pending: if `d.is_payment === true` -> return [] (improver skipped).

Payment drafts: card shows the deterministic block + real link, operator sends
the same text the customer eventually receives.

Usage:
  python3 scripts/build_nomod_skip_improver.py            # dry run
  python3 scripts/build_nomod_skip_improver.py --deploy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

# --- Queue & Format edit ---
QF_OLD = "  is_lead: !!input.is_lead\n};"
QF_NEW = ("  is_lead: !!input.is_lead,\n"
          "  is_payment: !!paymentMessage   // Nomod: skip the FR-5 improver\n"
          "};")

# --- Check Pending edit ---
CP_OLD = ("const d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
          "if (!d || d.status !== 'pending') {")
CP_NEW = ("const d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
          "if (d && d.is_payment === true) {\n"
          "  // Nomod: payment drafts carry a deterministic booking block +\n"
          "  // real Nomod link appended by Build Payment Message; the\n"
          "  // improver doesn't know about them and would strip them.\n"
          "  return [];\n"
          "}\n"
          "if (!d || d.status !== 'pending') {")


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_nomod_skip_improver.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    by = {x["name"]: x for x in wf["nodes"]}
    for req in ("Queue & Format", "Check Pending"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    qf = by["Queue & Format"]["parameters"]
    cp = by["Check Pending"]["parameters"]

    if "is_payment:" in qf.get("jsCode", "") or \
       "is_payment ===" in cp.get("jsCode", ""):
        print("2. already applied — nothing to do.")
        return

    if QF_OLD not in qf.get("jsCode", ""):
        raise DeployError("Queue & Format: 'is_lead' anchor not found")
    if CP_OLD not in cp.get("jsCode", ""):
        raise DeployError("Check Pending: pendingQueue.find anchor not found")

    qf["jsCode"] = qf["jsCode"].replace(QF_OLD, QF_NEW, 1)
    cp["jsCode"] = cp["jsCode"].replace(CP_OLD, CP_NEW, 1)

    print("2. Queue & Format: pending.is_payment marker added")
    print("3. Check Pending: skip improver when d.is_payment is true")

    if not deploy:
        print("\nDRY RUN — both anchors matched. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("4. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="NOMODSKIPIMPR")
    by_f = {x["name"]: x for x in final.get("nodes", [])}
    ok = ("is_payment:" in by_f["Queue & Format"]["parameters"].get("jsCode", "")
          and "is_payment ===" in by_f["Check Pending"]["parameters"].get("jsCode", ""))
    print(f"5. VERIFY: both edits present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("NOMOD SKIP-IMPROVER DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-NOMODSKIPIMPR backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
