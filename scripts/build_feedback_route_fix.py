#!/usr/bin/env python3
"""
build_feedback_route_fix.py — fix the Route Action rules added by Step 3.

The 3 rules added for feedback_yes/modify/no used `={{ $json.action }}`,
but at Route Action's position $json is the Draft Exists? IF output, not
the Parse Callback item. Existing rules use `={{ $('Parse Callback').item
.json.action }}` — switch to that.

Observed live (exec 789-791): Parse Callback.action='feedback_yes',
draft_found=true, Route Action ran but matched no rule, last node ended
at Route Action. Yes button felt unresponsive.

Usage:
  python3 scripts/build_feedback_route_fix.py            # dry run
  python3 scripts/build_feedback_route_fix.py --deploy
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_feedback_route_fix.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    ra = next((x for x in wf["nodes"] if x["name"] == "Route Action"), None)
    if ra is None:
        raise DeployError("Route Action node not found")
    rules = ra["parameters"]["rules"]["values"]

    target = {"feedback_yes", "feedback_modify", "feedback_no"}
    fixed = 0
    for r in rules:
        if r.get("outputKey") not in target:
            continue
        conds = r.get("conditions", {}).get("conditions") or []
        if not conds:
            continue
        cond = conds[0]
        if cond.get("leftValue") == "={{ $json.action }}":
            cond["leftValue"] = "={{ $('Parse Callback').item.json.action }}"
            fixed += 1
            print(f"   fixed rule outputKey={r['outputKey']}")

    if fixed == 0:
        print("2. nothing to fix — rules already use $('Parse Callback')")
        return
    print(f"2. {fixed} rule(s) re-pointed at $('Parse Callback').item.json.action")

    if not deploy:
        print("\nDRY RUN — re-run with --deploy.")
        return

    print("3. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="FEEDBACKROUTEFIX")
    final_ra = next((x for x in final.get("nodes", []) if x["name"] == "Route Action"), None)
    fixed_keys = set()
    for r in (final_ra["parameters"]["rules"]["values"] if final_ra else []):
        if r.get("outputKey") in target:
            c = (r.get("conditions", {}).get("conditions") or [{}])[0]
            if c.get("leftValue") == "={{ $('Parse Callback').item.json.action }}":
                fixed_keys.add(r["outputKey"])
    ok = fixed_keys == target
    print(f"4. VERIFY: all 3 rules fixed={ok}  ({fixed_keys})")
    print("FEEDBACK ROUTE FIX DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-FEEDBACKROUTEFIX backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
