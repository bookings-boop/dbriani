#!/usr/bin/env python3
"""
build_bug1_redis_autosend.py — BUG-1 fix (workflow side).

Replaces the autonomous branch's unreliable n8n-staticData recheck with the
Redis-backed bridge endpoint /autosend-state (arm | disarm | get).

  Auto Is Autonomous ─▶ Arm Autosend ─▶ Render Auto Card   (arm, pre-countdown)
  Answer Callback    ─▶ Disarm Autosend ─▶ Draft Exists?   (disarm on any button)
  Auto Wait          ─▶ Get Autosend ─▶ Auto Decide        (decide from Redis)

`Auto Prep` gains draft_text + customer_name in its output; `Auto Decide` is
rewritten to read the Redis state instead of staticData.pendingQueue.

3 nodes added · Auto Prep + Auto Decide modified · 3 connections rewired.
Deploys via n8n_deploy.safe_put. Refs: docs/bug-1-autonomous-send-fix.md.

Usage:
  python3 scripts/build_bug1_redis_autosend.py            # dry run
  python3 scripts/build_bug1_redis_autosend.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_URL = "http://172.18.0.1:8788/autosend-state"
BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

# Auto Prep — add draft_text + customer_name to its output object
AP_ANCHOR = "telegram_message_id: d.telegram_message_id,"
AP_REPLACE = ("telegram_message_id: d.telegram_message_id,\n"
              "      draft_text: d.draft_text || '',\n"
              "      customer_name: d.customer_name || '',")

# Auto Decide — full rewrite: decide from the Redis autosend state
AUTO_DECIDE_JS = """// BUG-1 fix: decide via the Redis autosend state (Get Autosend), NOT n8n
// staticData — staticData is clobbered by concurrent executions across the
// Auto Wait countdown. armed === true => the operator did not intervene.
const r = $('Get Autosend').item.json || {};
if (r.armed !== true || !r.data) return [];
const d = r.data;
return [{ json: {
  draft_id: d.draft_id,
  customer_id: d.customer_phone || '',
  customer_phone: d.customer_phone || '',
  telegram_message_id: d.telegram_message_id,
  customer_name: d.customer_name || '',
  draft_text: d.draft_text || ''
} }];
"""

ARM_BODY = ('={ "action": "arm", '
            '"draft_id": {{ JSON.stringify($(\'Auto Prep\').item.json.draft_id) }}, '
            '"customer_phone": {{ JSON.stringify($(\'Auto Prep\').item.json.customer_phone) }}, '
            '"draft_text": {{ JSON.stringify($(\'Auto Prep\').item.json.draft_text) }}, '
            '"customer_name": {{ JSON.stringify($(\'Auto Prep\').item.json.customer_name) }}, '
            '"telegram_message_id": {{ $(\'Auto Prep\').item.json.telegram_message_id || 0 }} }')
DISARM_BODY = ('={ "action": "disarm", '
               '"draft_id": {{ JSON.stringify($(\'Parse Callback\').item.json.draft_id) }} }')
GET_BODY = ('={ "action": "get", '
            '"draft_id": {{ JSON.stringify($(\'Auto Prep\').item.json.draft_id) }} }')


def gid():
    return str(uuid.uuid4())


def http_node(name, json_body, pos):
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
            "jsonBody": json_body,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": gid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
    }


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_bug1_redis_autosend.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Get Autosend" in by:
        print("2. already applied — 'Get Autosend' present. nothing to do.")
        return
    for req in ("Auto Prep", "Auto Decide", "Auto Is Autonomous",
                "Answer Callback", "Auto Wait", "Render Auto Card",
                "Draft Exists?", "Parse Callback"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- modify Auto Prep ---
    ap_js = by["Auto Prep"]["parameters"].get("jsCode", "")
    if AP_ANCHOR not in ap_js:
        raise DeployError("Auto Prep: output anchor not found — aborting")
    by["Auto Prep"]["parameters"]["jsCode"] = ap_js.replace(AP_ANCHOR, AP_REPLACE, 1)

    # --- rewrite Auto Decide ---
    by["Auto Decide"]["parameters"]["jsCode"] = AUTO_DECIDE_JS

    # --- add the 3 bridge-calling nodes ---
    nodes.extend([
        http_node("Arm Autosend", ARM_BODY, [6960, 1360]),
        http_node("Disarm Autosend", DISARM_BODY, [5240, 1320]),
        http_node("Get Autosend", GET_BODY, [7720, 1360]),
    ])

    # --- rewire (find the edge to the old target, repoint it) ---
    def rewire(src, old_target, new_target):
        main = conns.get(src, {}).get("main")
        if not main or not main[0]:
            raise DeployError(f"{src!r} has no out[0] — aborting")
        hit = [c for c in main[0] if c.get("node") == old_target]
        if not hit:
            raise DeployError(f"{src!r} out[0] does not feed {old_target!r} "
                              "— aborting (unexpected)")
        for c in hit:
            c["node"] = new_target

    rewire("Auto Is Autonomous", "Render Auto Card", "Arm Autosend")
    conns["Arm Autosend"] = {"main": [one("Render Auto Card")]}
    rewire("Answer Callback", "Draft Exists?", "Disarm Autosend")
    conns["Disarm Autosend"] = {"main": [one("Draft Exists?")]}
    rewire("Auto Wait", "Auto Decide", "Get Autosend")
    conns["Get Autosend"] = {"main": [one("Auto Decide")]}

    print(f"2. nodes: {len(nodes) - 3} -> {len(nodes)}  "
          "(+3: Arm Autosend, Disarm Autosend, Get Autosend)")
    print("3. modified: Auto Prep (+draft_text, +customer_name) · "
          "Auto Decide (rewritten — reads Redis, not staticData)")
    print("4. rewired:")
    print("     Auto Is Autonomous ─▶ Arm Autosend ─▶ Render Auto Card")
    print("     Answer Callback    ─▶ Disarm Autosend ─▶ Draft Exists?")
    print("     Auto Wait          ─▶ Get Autosend ─▶ Auto Decide")

    if not deploy:
        print("\nDRY RUN — anchors + rewires all matched, nothing deployed. "
              "Re-run with --deploy.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="BUG1REDIS")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = all(x in fnames for x in
             ("Arm Autosend", "Disarm Autosend", "Get Autosend"))
    print(f"6. VERIFY: 3 nodes present={ok}, nodes={len(final.get('nodes', []))}, "
          f"active={final.get('active')}")
    print("BUG-1 FIX DEPLOYED — the autonomous branch decides via Redis." if ok
          else "x VERIFY FAILED — inspect; PRE-BUG1REDIS backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
