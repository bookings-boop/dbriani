#!/usr/bin/env python3
"""
build_bug2_caps_command.py — BUG-2 fix: wire the /caps operator command.

Before: an operator typing /caps in Telegram fell through Process Text Reply's
final `ack` branch -> "Ack No Pending" ("⚠️ Nothing sent..."). The bridge
/caps endpoint worked; nothing in the workflow routed to it.

After:
  Process Text Reply  -- new branch: text === '/caps' -> {action:'caps_cmd'}
  Route Text Action   -- new switch output #6 'caps_cmd'
  Hermes Caps  (NEW)  -- POST http://172.18.0.1:8788/caps  (X-Bridge-Token)
  Send Caps Reply (NEW) -- Telegram sendMessage with the cap-status text

  Route Text Action [out 6] ─▶ Hermes Caps ─▶ Send Caps Reply

2 nodes added · Process Text Reply + Route Text Action modified · 2 connections.
Deploys via n8n_deploy.safe_put. Refs: docs/feature-backlog.md BUG-2.

Usage:
  python3 scripts/build_bug2_caps_command.py            # dry run
  python3 scripts/build_bug2_caps_command.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

# --- Process Text Reply: insert a /caps branch before the (D) /rules block ---
PTR_ANCHOR = ("// (D) /rules, /approverule <id>, /discardrule <id> "
              "— rule review (FR-5)")
PTR_INSERT = (
    "// (F) /caps — report autonomous-mode safety-cap status (BUG-2)\n"
    "if (text.toLowerCase() === '/caps') {\n"
    "  return { json: { action: 'caps_cmd', admin_chat_id: adminChatId } };\n"
    "}\n\n"
) + PTR_ANCHOR


def gid():
    return str(uuid.uuid4())


def caps_switch_rule():
    """A 7th Route Text Action rule: $json.action === 'caps_cmd'."""
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose", "version": 1},
            "conditions": [{
                "id": gid(),
                "leftValue": "={{ $json.action }}",
                "rightValue": "caps_cmd",
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": "caps_cmd",
    }


def hermes_caps_node():
    """POST the bridge /caps endpoint (token-gated). Empty {} body — the
    bridge needs none. continueRegularOutput so a bridge error still flows
    to Send Caps Reply (which then shows a fallback message)."""
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/caps",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={}",
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": gid(), "name": "Hermes Caps",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [5340, 2000],
        "onError": "continueRegularOutput",
    }


def send_caps_reply_node():
    """Relay the bridge's cap-status text back to the operator on Telegram."""
    body = ('={ "chat_id": {{ $(\'Process Text Reply\').item.json.admin_chat_id }}, '
            '"text": {{ JSON.stringify($json.text || '
            '"⚠️ Cap status unavailable — the bridge did not respond.") }} }')
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {},
        },
        "id": gid(), "name": "Send Caps Reply",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [5620, 2000],
        "onError": "continueRegularOutput",
    }


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_bug2_caps_command.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Hermes Caps" in by:
        print("2. already applied — 'Hermes Caps' present. nothing to do.")
        return
    for req in ("Process Text Reply", "Route Text Action"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- modify Process Text Reply: add the /caps branch ---
    ptr_js = by["Process Text Reply"]["parameters"].get("jsCode", "")
    if PTR_ANCHOR not in ptr_js:
        raise DeployError("Process Text Reply: (D) /rules anchor not found "
                          "— aborting")
    if "'/caps'" in ptr_js or '"/caps"' in ptr_js:
        raise DeployError("Process Text Reply already has a /caps branch "
                          "— aborting")
    by["Process Text Reply"]["parameters"]["jsCode"] = ptr_js.replace(
        PTR_ANCHOR, PTR_INSERT, 1)

    # --- modify Route Text Action: append the caps_cmd rule (output #6) ---
    rta = by["Route Text Action"]
    rules = rta["parameters"].setdefault("rules", {}).setdefault("values", [])
    if len(rules) != 6:
        raise DeployError(f"Route Text Action has {len(rules)} rules, "
                          "expected 6 — aborting (unexpected topology)")
    rules.append(caps_switch_rule())

    # --- add the 2 new nodes ---
    nodes.extend([hermes_caps_node(), send_caps_reply_node()])

    # --- wire connections ---
    rta_main = conns.get("Route Text Action", {}).get("main")
    if not rta_main or len(rta_main) != 6:
        raise DeployError("Route Text Action connections do not have exactly "
                          f"6 outputs (found {len(rta_main) if rta_main else 0})"
                          " — aborting")
    rta_main.append(one("Hermes Caps"))            # new output index 6
    conns["Hermes Caps"] = {"main": [one("Send Caps Reply")]}

    print(f"2. nodes: {len(nodes) - 2} -> {len(nodes)}  "
          "(+2: Hermes Caps, Send Caps Reply)")
    print("3. modified: Process Text Reply (+/caps branch) · "
          "Route Text Action (+caps_cmd rule, output #6)")
    print("4. wired:  Route Text Action [out 6] ─▶ Hermes Caps ─▶ Send Caps Reply")

    if not deploy:
        print("\nDRY RUN — anchor matched, switch had 6 rules, 6 connection "
              "outputs confirmed. Nothing deployed. Re-run with --deploy.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="BUG2CAPS")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = "Hermes Caps" in fnames and "Send Caps Reply" in fnames
    print(f"6. VERIFY: 2 nodes present={ok}, nodes={len(final.get('nodes', []))}, "
          f"active={final.get('active')}")
    print("BUG-2 FIX DEPLOYED — /caps now routes to the bridge." if ok
          else "x VERIFY FAILED — inspect; PRE-BUG2CAPS backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
