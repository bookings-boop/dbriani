#!/usr/bin/env python3
"""
build_mark_auto_sent_residual.py — fix the "Mark Auto Sent staticData residual".

THE RESIDUAL
  Mark Auto Sent wrote d.status='sent' (+ d.auto_sent, d.messages_sent_count)
  into pendingQueue via $getWorkflowStaticData('global'). That write is
  unreliable (concurrent-execution snapshot race — the BUG-1 root cause). Its
  ONLY real consumer is Check Pending, the FR-5 improver gate:
      if (d.status !== 'pending') return [];
  So when the write loses the race, the FR-5 improver "improves" an
  already-auto-sent draft and edits a Telegram card whose text never went out.
  (d.auto_sent and d.messages_sent_count have no readers at all.)

THE FIX  (chosen approach: "skip improver via the autosend key")
  Don't port the write to Redis — that would orphan it (every other status
  reader is staticData). Instead, make the FR-5 improver SKIP autonomous
  drafts outright, detected via the Redis autosend key that Arm Autosend
  already sets:

    Improve Wait ─▶ Check Autosend Key (NEW) ─▶ Check Pending

  - Check Autosend Key: POST bridge /autosend-state {action:get} for the
    draft. Runs ~20s after the draft posts (Improve Wait) — long after
    Arm Autosend, well within the key's 3600s TTL. No race, no TTL risk.
  - Check Pending: if armed===true -> return [] (autonomous draft, skip).
    Fail-open: a bridge error -> proceed (improving is only cosmetic).
  - Mark Auto Sent: the entire $getWorkflowStaticData block is removed — the
    status flag is now superseded by the autosend-key gate, and auto_sent /
    messages_sent_count had no readers. The node keeps its pass-through only.

1 node added · Mark Auto Sent + Check Pending modified · 2 connections rewired.
Deploys via n8n_deploy.safe_put. Refs: docs/feature-backlog.md (BUG-2 residual).

Usage:
  python3 scripts/build_mark_auto_sent_residual.py            # dry run
  python3 scripts/build_mark_auto_sent_residual.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_URL = "http://172.18.0.1:8788/autosend-state"
BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

CHECK_AUTOSEND_BODY = (
    '={ "action": "get", '
    '"draft_id": {{ JSON.stringify($(\'Save Telegram MsgID\').item.json.draft_id) }} }')

# --- Mark Auto Sent: drop the unreliable staticData block (full rewrite) ---
MARK_AUTO_SENT_NEW = """// FR-4 4-C: pass the auto-sent draft's identifiers to Edit Auto-Sent Card.
// BUG-2 residual fix: the old $getWorkflowStaticData('global') write of
// d.status='sent' was unreliable (concurrent-execution snapshot race) and is
// no longer needed — the FR-5 improver now skips autonomous drafts via the
// Redis autosend key (see Check Autosend Key). d.auto_sent and
// d.messages_sent_count had zero readers, so they are dropped too.
const dec = $('Auto Decide').item.json;
return [{ json: {
  draft_id: dec.draft_id,
  customer_phone: dec.customer_phone,
  customer_name: dec.customer_name,
  telegram_message_id: dec.telegram_message_id,
  draft_text: dec.draft_text
} }];
"""

# --- Check Pending: insert the autosend-key gate right after draftId ---
CP_ANCHOR = ("const draftId = sid.draft_id;\n"
             "const data = $getWorkflowStaticData('global');")
CP_REPLACE = (
    "const draftId = sid.draft_id;\n"
    "// BUG-2 residual fix: skip autonomous drafts — they auto-send the copy\n"
    "// armed at countdown start, so improving them is moot and would edit a\n"
    "// card whose text never went out. Detect via the Redis autosend key.\n"
    "// Fail-open: a bridge error leaves __as.armed undefined -> proceed.\n"
    "const __as = $('Check Autosend Key').item.json || {};\n"
    "if (__as.armed === true) { return []; }\n"
    "const data = $getWorkflowStaticData('global');")


def gid():
    return str(uuid.uuid4())


def check_autosend_node():
    return {
        "parameters": {
            "method": "POST",
            "url": BRIDGE_URL,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": CHECK_AUTOSEND_BODY,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": gid(), "name": "Check Autosend Key",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [6268, 1180],
        "onError": "continueRegularOutput",
    }


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_mark_auto_sent_residual.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Check Autosend Key" in by:
        print("2. already applied — 'Check Autosend Key' present. nothing to do.")
        return
    for req in ("Mark Auto Sent", "Check Pending", "Improve Wait",
                "Save Telegram MsgID", "Auto Decide"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- rewrite Mark Auto Sent (drop the staticData block) ---
    mas_js = by["Mark Auto Sent"]["parameters"].get("jsCode", "")
    if "$getWorkflowStaticData" not in mas_js:
        raise DeployError("Mark Auto Sent: no staticData block found — already "
                          "changed? aborting")
    by["Mark Auto Sent"]["parameters"]["jsCode"] = MARK_AUTO_SENT_NEW

    # --- modify Check Pending (insert the autosend-key gate) ---
    cp_js = by["Check Pending"]["parameters"].get("jsCode", "")
    if CP_ANCHOR not in cp_js:
        raise DeployError("Check Pending: draftId/staticData anchor not found "
                          "— aborting")
    if "Check Autosend Key" in cp_js:
        raise DeployError("Check Pending already references Check Autosend Key "
                          "— aborting")
    by["Check Pending"]["parameters"]["jsCode"] = cp_js.replace(
        CP_ANCHOR, CP_REPLACE, 1)

    # --- add the new node ---
    nodes.append(check_autosend_node())

    # --- rewire  Improve Wait ─▶ Check Autosend Key ─▶ Check Pending ---
    iw_main = conns.get("Improve Wait", {}).get("main")
    if not iw_main or not iw_main[0]:
        raise DeployError("Improve Wait has no out[0] — aborting")
    hit = [c for c in iw_main[0] if c.get("node") == "Check Pending"]
    if not hit:
        raise DeployError("Improve Wait out[0] does not feed Check Pending "
                          "— aborting (unexpected topology)")
    for c in hit:
        c["node"] = "Check Autosend Key"
    conns["Check Autosend Key"] = {"main": [one("Check Pending")]}

    print(f"2. nodes: {len(nodes) - 1} -> {len(nodes)}  (+1: Check Autosend Key)")
    print("3. modified: Mark Auto Sent (staticData block removed) · "
          "Check Pending (+autosend-key skip gate)")
    print("4. rewired:  Improve Wait ─▶ Check Autosend Key ─▶ Check Pending")

    if not deploy:
        print("\nDRY RUN — anchors matched, Improve Wait->Check Pending edge "
              "confirmed. Nothing deployed. Re-run with --deploy.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="MARKAUTOSENT")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = "Check Autosend Key" in fnames
    print(f"6. VERIFY: Check Autosend Key present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("RESIDUAL FIXED — Mark Auto Sent no longer writes staticData; the "
          "FR-5 improver skips autonomous drafts." if ok
          else "x VERIFY FAILED — inspect; PRE-MARKAUTOSENT backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
