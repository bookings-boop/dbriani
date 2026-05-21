#!/usr/bin/env python3
"""
build_break_detection_prompt.py — Task 2 of docs/break-condition-detection-plan.md

Makes the initial-draft path emit and carry a structured `break_condition`:
  - Build Prompt   : the systemPrompt schema gains a break_condition field and
                     a "## BREAK-CONDITION CHECK" section is appended.
  - Parse Response : carries break_condition from the parsed JSON onto its
                     output, defaulting safely to {hit:false}.
  - Queue & Format : carries break_condition onto the queued draft object.

3 existing nodes edited, 0 added. Deploys via n8n_deploy.safe_put.
Each anchor is asserted before patching — a missed anchor aborts the run.

Usage:
  python3 scripts/build_break_detection_prompt.py            # dry run
  python3 scripts/build_break_detection_prompt.py --deploy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

MARKER = "BREAK-CONDITION CHECK"

BREAK_BLOCK = """

## BREAK-CONDITION CHECK

Also include a "break_condition" field in your JSON. Judge ONLY the customer's
newest message. Set "hit": true only if it clearly matches one of:
  - "discount_request": asks for a discount or a lower price, says "best
    price", says it is too expensive, or is haggling on price.
  - "human_request": asks to speak to a person / human / manager / "real
    person", or to be transferred off the bot.
  - "negative_sentiment": the customer is clearly upset, angry, frustrated, or
    complaining — NOT mild hesitation or an ordinary sales objection.
A normal price question ("how much is the 80ft on Saturday?") is NOT a break.
Format when something matches:
  "break_condition": {"hit": true, "reason": "discount_request", "detail": "<one short line>"}
Format when nothing matches:
  "break_condition": {"hit": false}
"""

# Build Prompt — systemPrompt schema block
SCHEMA_OLD = ('  "notes_for_zayn": "30 words max — why this approach, '
              'what to watch for"\n}')
SCHEMA_NEW = ('  "notes_for_zayn": "30 words max — why this approach, '
              'what to watch for",\n  "break_condition": {"hit": false}\n}')

# Parse Response — main return path
PR_MAIN_OLD = '  notes: parsed.notes_for_zayn || "(no notes)",'
PR_MAIN_NEW = ('  notes: parsed.notes_for_zayn || "(no notes)",\n'
               "  break_condition: (parsed && parsed.break_condition "
               "&& typeof parsed.break_condition.hit === 'boolean')\n"
               "    ? parsed.break_condition : { hit: false },")

# Parse Response — error (extract_failed) return path
PR_ERR_OLD = '    parse_status: "extract_failed",'
PR_ERR_NEW = ('    parse_status: "extract_failed",\n'
              "    break_condition: { hit: false },")

# Queue & Format — the queued draft object
QF_OLD = "  status: 'pending',"
QF_NEW = ("  status: 'pending',\n"
          "  break_condition: input.break_condition || { hit: false },")


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_break_detection_prompt.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = {x["name"]: x for x in wf["nodes"]}
    for req in ("Build Prompt", "Parse Response", "Queue & Format"):
        if req not in nodes:
            raise DeployError(f"required node {req!r} not found")

    sp_assign = next((a for a in nodes["Build Prompt"]["parameters"]
                      ["assignments"]["assignments"] if a["name"] == "systemPrompt"),
                     None)
    if sp_assign is None:
        raise DeployError("Build Prompt: systemPrompt assignment not found")
    if MARKER in sp_assign["value"]:
        print("2. already applied — break-condition prompt present. nothing to do.")
        return

    # --- Build Prompt: schema + appended section ---
    sp = sp_assign["value"]
    if SCHEMA_OLD not in sp:
        raise DeployError("Build Prompt: JSON-schema anchor not found — aborting")
    sp_assign["value"] = sp.replace(SCHEMA_OLD, SCHEMA_NEW, 1) + BREAK_BLOCK
    print("2. Build Prompt: schema field + BREAK-CONDITION CHECK section added")

    # --- Parse Response: both return paths ---
    js = nodes["Parse Response"]["parameters"]["jsCode"]
    for old in (PR_MAIN_OLD, PR_ERR_OLD):
        if old not in js:
            raise DeployError(f"Parse Response: anchor not found:\n  {old!r}")
    nodes["Parse Response"]["parameters"]["jsCode"] = (
        js.replace(PR_MAIN_OLD, PR_MAIN_NEW, 1).replace(PR_ERR_OLD, PR_ERR_NEW, 1))
    print("3. Parse Response: break_condition carried on both return paths")

    # --- Queue & Format: the queued draft ---
    qjs = nodes["Queue & Format"]["parameters"]["jsCode"]
    if QF_OLD not in qjs:
        raise DeployError(f"Queue & Format: anchor not found:\n  {QF_OLD!r}")
    nodes["Queue & Format"]["parameters"]["jsCode"] = qjs.replace(QF_OLD, QF_NEW, 1)
    print("4. Queue & Format: break_condition added to the queued draft object")

    if not deploy:
        print("\nDRY RUN — anchors all matched, nothing deployed. "
              "Re-run with --deploy to safe_put.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="BREAKPROMPT")
    fbp = next((x for x in final["nodes"] if x["name"] == "Build Prompt"), {})
    fsp = next((a for a in fbp.get("parameters", {}).get("assignments", {})
                .get("assignments", []) if a["name"] == "systemPrompt"), {})
    ok = MARKER in fsp.get("value", "")
    print(f"6. VERIFY: break-condition prompt present={ok}, "
          f"nodes={len(final['nodes'])}")
    print("DEPLOYED — initial drafts now emit a break_condition field." if ok
          else "x VERIFY FAILED — inspect; PRE-BREAKPROMPT backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
