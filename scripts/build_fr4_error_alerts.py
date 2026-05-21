#!/usr/bin/env python3
"""
build_fr4_error_alerts.py — wire the FR-4 autosend branch's silent failures
to a Telegram alert (code review 🔴 #3).

THE PROBLEM
  Auto Gate, Auto Commit and Auto Send WAHA all run with
  onError:continueErrorOutput, but their ERROR outputs are UNWIRED. On a
  failure (bridge down, WhatsApp send rejected) the item is dropped — the
  customer gets silence and the operator believes auto-mode handled it.

THE FIX
  The workflow already has a failure-alert path: <error output> -> Build
  Alert -> Alert Zayn (Telegram sendMessage to the admin chat), used by 7
  other nodes. Build Alert is generic (no failing-node name, no timestamp),
  and editing it would change those 7 other alerts — so instead this adds
  three small alert-builder Code nodes, one per autosend error output, each
  stamping: which node failed, customer_id, error message, timestamp — then
  feeds the existing Alert Zayn sender.

  3 new Code nodes, 6 new connections, ZERO existing nodes modified.
  82 -> 85 nodes. Deploys via n8n_deploy.safe_put (no queue clobber).

Usage:
  python3 scripts/build_fr4_error_alerts.py            # dry run
  python3 scripts/build_fr4_error_alerts.py --deploy   # build + safe_put
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

ADMIN_CHAT = 5532831477  # existing admin Telegram chat id (as used workflow-wide)
SENDER = "Alert Zayn"    # existing Telegram sendMessage node

# (new node name, source node, error-output owner, customer-context node, impact line)
ALERTS = [
    ("Auto Gate Failed", "Auto Gate", "Auto Prep",
     "the autonomous gate check failed — the draft is still in your approval "
     "queue with its buttons; handle it normally."),
    ("Auto Commit Failed", "Auto Commit", "Auto Decide",
     "the final autonomous check failed — the draft was NOT sent; review the "
     "conversation and send it manually."),
    ("Auto Send Failed", "Auto Send WAHA", "Auto Decide",
     "the whatsapp send failed — the customer most likely received NOTHING; "
     "open the conversation and send it manually."),
]

JS_TEMPLATE = """// FR-4 #3: alert Zayn when the autonomous-send branch fails at __FAILED_NODE__.
// Wired from __FAILED_NODE__'s error output (onError: continueErrorOutput).
const it = $input.item || {};
const j = it.json || {};
const err = it.error || j.error || {};
let msg;
try {
  msg = err.message || err.description
    || (typeof err === 'string' ? err : '') || JSON.stringify(err);
} catch (e) { msg = 'unrecognised error'; }
let customer = '';
try {
  const u = $('__CUSTOMER_SRC__').item.json || {};
  customer = u.customer_id || u.customer_phone || '';
} catch (e) {}
if (!customer) customer = j.customer_id || j.customer_phone || 'unknown';
const text = [
  '\\uD83D\\uDD34 AUTONOMOUS-SEND FAILURE',
  '',
  'node:     __FAILED_NODE__',
  'customer: ' + customer,
  'error:    ' + String(msg || 'unknown').slice(0, 400),
  'time:     ' + new Date().toISOString(),
  '',
  '\\u26A0\\uFE0F __IMPACT__',
].join('\\n');
return { json: { chat_id: __ADMIN_CHAT__, alert_text: text } };
"""


def code_node(name, failed_node, customer_src, impact, pos):
    js = (JS_TEMPLATE
          .replace("__FAILED_NODE__", failed_node)
          .replace("__CUSTOMER_SRC__", customer_src)
          .replace("__IMPACT__", impact)
          .replace("__ADMIN_CHAT__", str(ADMIN_CHAT)))
    return {
        "parameters": {"jsCode": js},
        "id": str(uuid.uuid4()),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
    }


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_fr4_error_alerts.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by_name = {x["name"]: x for x in nodes}

    if any(x["name"] == "Auto Send Failed" for x in nodes):
        print("2. already applied — autosend error alerts present. nothing to do.")
        return

    # preconditions — every node this patch depends on must exist as expected
    for src in ("Auto Gate", "Auto Commit", "Auto Send WAHA"):
        nd = by_name.get(src)
        if not nd:
            raise DeployError(f"required node {src!r} not found")
        if nd.get("onError") != "continueErrorOutput":
            raise DeployError(f"{src!r} onError is {nd.get('onError')!r}, "
                              "expected 'continueErrorOutput' — aborting")
    if SENDER not in by_name:
        raise DeployError(f"sender node {SENDER!r} not found")

    # build the 3 alert-builder nodes
    pos = {"Auto Gate": [6408, 1400], "Auto Commit": [7508, 1400],
           "Auto Send WAHA": [7948, 1400]}
    new = []
    for name, src, cust_src, impact in ALERTS:
        new.append(code_node(name, src, cust_src, impact, pos[src]))
    nodes.extend(new)

    # wire: each error output (main[1]) -> its builder ; builder -> Alert Zayn
    for (name, src, _c, _i) in ALERTS:
        main = conns.setdefault(src, {}).setdefault("main", [])
        while len(main) < 2:
            main.append([])
        if main[1]:
            raise DeployError(f"{src!r} error output is already wired to "
                              f"{main[1]} — aborting (unexpected)")
        main[1] = [{"node": name, "type": "main", "index": 0}]
        conns[name] = {"main": [[{"node": SENDER, "type": "main", "index": 0}]]}

    print(f"2. nodes: {len(nodes) - 3} -> {len(nodes)}  (+3 alert builders)")
    print("3. wiring (only the 3 previously-UNWIRED error outputs):")
    for (name, src, cust, _i) in ALERTS:
        print(f"     {src}.error ─▶ {name} ─▶ {SENDER}   (customer via {cust})")
    print(f"4. existing nodes modified: NONE  ·  sender reused: {SENDER!r}")

    staged = Path("/tmp/fr4_error_alerts_staged.json")
    staged.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. staged full workflow -> {staged}")

    if not deploy:
        print("\nDRY RUN — nothing deployed. Re-run with --deploy to safe_put.")
        return

    print("6. deploying via n8n_deploy.safe_put (re-fetches staticData)...")
    final = n.safe_put(wf, tag="FR4ERRALERTS")
    fnodes = {x["name"] for x in final.get("nodes", [])}
    ok = all(nm in fnodes for nm, *_ in ALERTS)
    print(f"7. VERIFY: 3 alert builders present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("FR-4 ERROR ALERTS DEPLOYED — autosend failures now alert Zayn."
          if ok else "x VERIFY FAILED — inspect; PRE-FR4ERRALERTS backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
