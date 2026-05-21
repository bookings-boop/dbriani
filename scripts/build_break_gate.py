#!/usr/bin/env python3
"""
build_break_gate.py — Task 3 of docs/break-condition-detection-plan.md

Inserts the break-gate sub-branch at the autonomous-branch entry.

  BEFORE:  Save Telegram MsgID ──▶ [Improve Wait, Auto Prep]
  AFTER:   Save Telegram MsgID ──▶ [Improve Wait, Find Break]
           Find Break ──▶ Break Check ─┬ hit  ──▶ Flip To Approval ──▶ Break Alert
                                       └ none ──▶ Auto Prep   (autonomous, unchanged)

On a break the draft's conversation is flipped to `approval` (bridge
/set-mode with break_reason) and the operator is alerted; the autonomous
branch is never entered. 4 nodes added, 1 connection rewired, 0 existing
nodes modified. Deploys via n8n_deploy.safe_put.

Usage:
  python3 scripts/build_break_gate.py            # dry run
  python3 scripts/build_break_gate.py --deploy
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

FIND_BREAK_JS = """// break-gate: surface the queued draft's break_condition for the IF + alert
const sid = $('Save Telegram MsgID').item.json;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === sid.draft_id) || {};
const bc = d.break_condition || { hit: false };
const customer = d.customer_phone || sid.customer_phone || 'unknown';
const reason = bc.reason || 'unknown';
const detail = bc.detail || '';
const alert_text = [
  '\\u23F8\\uFE0F AUTONOMOUS PAUSED \\u2014 break condition detected',
  '',
  'customer: ' + customer,
  'reason:   ' + reason,
  'detail:   ' + detail,
  '',
  'the conversation has been set back to approval \\u2014 the draft is '
    + 'waiting for you to review and send.'
].join('\\n');
return [{ json: {
  draft_id: sid.draft_id,
  customer_id: customer,
  break_hit: bc.hit === true,
  break_reason: reason,
  break_detail: detail,
  alert_text: alert_text
} }];
"""


def gid():
    return str(uuid.uuid4())


def build_nodes():
    find_break = {
        "parameters": {"jsCode": FIND_BREAK_JS},
        "id": gid(), "name": "Find Break",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [6000, 1340],
    }
    break_check = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": gid(),
                    "leftValue": "={{ $json.break_hit }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": gid(), "name": "Break Check",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [6220, 1340],
    }
    flip = {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/set-mode",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": ('={ "customer_id": {{ JSON.stringify($json.customer_id) }}, '
                         '"mode": "approval", "activated_by": "break_detection", '
                         '"break_reason": {{ JSON.stringify($json.break_reason + '
                         '": " + $json.break_detail) }} }'),
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": gid(), "name": "Flip To Approval",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [6440, 1440],
        "onError": "continueRegularOutput",
    }
    alert = {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}"
                   "/sendMessage",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": ('={ "chat_id": 5532831477, "text": '
                         "{{ JSON.stringify($('Find Break').item.json.alert_text) }} }"),
            "options": {},
        },
        "id": gid(), "name": "Break Alert",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [6660, 1440],
        "onError": "continueRegularOutput",
    }
    return [find_break, break_check, flip, alert]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_break_gate.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by_name = {x["name"]: x for x in nodes}

    if any(x["name"] == "Find Break" for x in nodes):
        print("2. already applied — 'Find Break' present. nothing to do.")
        return
    for req in ("Save Telegram MsgID", "Auto Prep", "Improve Wait"):
        if req not in by_name:
            raise DeployError(f"required node {req!r} not found")

    stm = conns.get("Save Telegram MsgID", {}).get("main") or []
    stm0 = stm[0] if stm else []
    if not any(c.get("node") == "Auto Prep" for c in stm0):
        raise DeployError("'Save Telegram MsgID' out[0] does not feed 'Auto Prep' "
                          "— callback chain is not what this patch expects")

    nodes.extend(build_nodes())

    # rewire: Save Telegram MsgID out[0] — swap Auto Prep -> Find Break (keep the rest)
    for c in stm0:
        if c.get("node") == "Auto Prep":
            c["node"] = "Find Break"
    conns["Find Break"] = {"main": [[
        {"node": "Break Check", "type": "main", "index": 0}]]}
    conns["Break Check"] = {"main": [
        [{"node": "Flip To Approval", "type": "main", "index": 0}],  # out0 true=break
        [{"node": "Auto Prep", "type": "main", "index": 0}],          # out1 false=none
    ]}
    conns["Flip To Approval"] = {"main": [[
        {"node": "Break Alert", "type": "main", "index": 0}]]}

    print(f"2. nodes: {len(nodes) - 4} -> {len(nodes)}  (+4: Find Break, "
          "Break Check, Flip To Approval, Break Alert)")
    print("3. rewire:")
    print("     Save Telegram MsgID ─▶ [Improve Wait, Find Break]")
    print("     Find Break ─▶ Break Check ─┬ hit  ─▶ Flip To Approval ─▶ Break Alert")
    print("                                └ none ─▶ Auto Prep  (unchanged)")
    print("4. existing nodes modified: NONE")

    staged = Path("/tmp/break_gate_staged.json")
    staged.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. staged -> {staged}")

    if not deploy:
        print("\nDRY RUN — nothing deployed. Re-run with --deploy to safe_put.")
        return

    print("6. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="BREAKGATE")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = all(nm in fnames for nm in
             ("Find Break", "Break Check", "Flip To Approval", "Break Alert"))
    print(f"7. VERIFY: 4 break-gate nodes present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("BREAK-GATE DEPLOYED — flagged drafts route out of autonomous send."
          if ok else "x VERIFY FAILED — inspect; PRE-BREAKGATE backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
