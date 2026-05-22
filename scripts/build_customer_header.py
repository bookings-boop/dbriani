#!/usr/bin/env python3
"""
build_customer_header.py — feature-header (customer context header).

Adds a customer-context header above approval-mode draft cards:

  Parse Response ─▶ Customer Facts (NEW) ─▶ Queue & Format

- Customer Facts: httpRequest → POST bridge /customer-facts (token-gated).
  onError=continueRegularOutput — a bridge failure still flows on.
- Queue & Format: prepends the returned customer_header to the non-lead card.

PLAN-CORRECTION (discovered at build time): inserting the Customer Facts
httpRequest into the chain makes Queue & Format's `$input` the HTTP response,
not the parse-node output. So Queue & Format is changed to resolve its draft
data from `$('Parse Response')` (or `$('Parse Lead Response')` on the /lead
path) explicitly, instead of `$input`. The /lead path is otherwise untouched —
Customer Facts is spliced only into the Parse Response → Queue & Format edge.

1 node added · Queue & Format modified · 2 connections rewired.
Deploys via n8n_deploy.safe_put. Refs: docs/feature-header-plan.md.

Usage:
  python3 scripts/build_customer_header.py            # dry run
  python3 scripts/build_customer_header.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

CF_BODY = ('={ "customer_id": {{ JSON.stringify($json.customer_phone) }}, '
           '"customer_name": {{ JSON.stringify($json.customer_name) }}, '
           '"incoming_message": {{ JSON.stringify($json.user_message) }}, '
           '"history": {{ JSON.stringify($json.conversation_history) }} }')

# --- Queue & Format edit 1: resolve draft data explicitly (not $input) ------
QF_OLD_INPUT = "const input = $input.item.json;"
QF_NEW_INPUT = (
    "// feature-header: Customer Facts sits between Parse Response and this\n"
    "// node, so $input here is the Customer Facts HTTP response. Resolve the\n"
    "// draft data from whichever parse node actually ran.\n"
    "let input;\n"
    "try { input = $('Parse Response').item.json; }\n"
    "catch (e) { input = $('Parse Lead Response').item.json; }\n"
    "let customerHeader = '';\n"
    "try { customerHeader = ($('Customer Facts').item.json || {})"
    ".customer_header || ''; }\n"
    "catch (e) { customerHeader = ''; }")

# --- Queue & Format edit 2: prepend the header in the non-lead branch -------
QF_OLD_ELSE = (
    "} else {\n"
    "  lines = ['\U0001F4E9 from ' + pending.customer_phone];\n"
    "  if (pending.customer_name) lines.push('\U0001F464 ' "
    "+ pending.customer_name);\n"
    "  lines.push(histLine, '', 'They said:', '\"' + pending.customer_message "
    "+ '\"', '', '---', draftHeader, preview, '', '\U0001F4DD Notes: ' "
    "+ pending.notes);\n"
    "}")
QF_NEW_ELSE = (
    "} else {\n"
    "  lines = ['\U0001F4E9 from ' + pending.customer_phone];\n"
    "  if (customerHeader) {\n"
    "    lines.push(customerHeader);\n"
    "  } else {\n"
    "    if (pending.customer_name) lines.push('\U0001F464 ' "
    "+ pending.customer_name);\n"
    "    lines.push(histLine);\n"
    "  }\n"
    "  lines.push('', 'They said:', '\"' + pending.customer_message + '\"', "
    "'', '---', draftHeader, preview, '', '\U0001F4DD Notes: ' "
    "+ pending.notes);\n"
    "}")


def customer_facts_node(position):
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/customer-facts",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": CF_BODY,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": str(uuid.uuid4()), "name": "Customer Facts",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
    }


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_customer_header.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Customer Facts" in by:
        print("2. already applied — 'Customer Facts' present. nothing to do.")
        return
    for req in ("Parse Response", "Queue & Format", "Parse Lead Response"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- modify Queue & Format ---
    qf = by["Queue & Format"]["parameters"]
    js = qf.get("jsCode", "")
    if QF_OLD_INPUT not in js:
        raise DeployError("Queue & Format: '$input' anchor not found — aborting")
    if QF_OLD_ELSE not in js:
        raise DeployError("Queue & Format: non-lead else-block anchor not found "
                          "— aborting")
    if "Customer Facts" in js:
        raise DeployError("Queue & Format already references Customer Facts "
                          "— aborting")
    js = js.replace(QF_OLD_INPUT, QF_NEW_INPUT, 1)
    js = js.replace(QF_OLD_ELSE, QF_NEW_ELSE, 1)
    qf["jsCode"] = js

    # --- add the Customer Facts node (position: midpoint of the edge) ---
    p1 = by["Parse Response"]["position"]
    p2 = by["Queue & Format"]["position"]
    pos = [(p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2 - 140]
    nodes.append(customer_facts_node(pos))

    # --- rewire Parse Response ─▶ Customer Facts ─▶ Queue & Format ---
    pr_main = conns.get("Parse Response", {}).get("main")
    if not pr_main or not pr_main[0]:
        raise DeployError("Parse Response has no out[0] — aborting")
    hit = [c for c in pr_main[0] if c.get("node") == "Queue & Format"]
    if not hit:
        raise DeployError("Parse Response out[0] does not feed Queue & Format "
                          "— aborting (unexpected topology)")
    for c in hit:
        c["node"] = "Customer Facts"
    conns["Customer Facts"] = {"main": [one("Queue & Format")]}

    print(f"2. nodes: {len(nodes) - 1} -> {len(nodes)}  (+1: Customer Facts)")
    print("3. modified: Queue & Format (resolve draft data via $('Parse "
          "Response'); prepend customer_header)")
    print("4. rewired:  Parse Response ─▶ Customer Facts ─▶ Queue & Format")
    print("   (/lead path Parse Lead Response ─▶ Queue & Format untouched)")

    if not deploy:
        print("\nDRY RUN — both Queue & Format anchors matched, edge confirmed. "
              "Nothing deployed. Re-run with --deploy.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="CUSTOMERHEADER")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = "Customer Facts" in fnames
    print(f"6. VERIFY: Customer Facts present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("FEATURE-HEADER DEPLOYED — draft cards now carry a customer header."
          if ok else "x VERIFY FAILED — inspect; PRE-CUSTOMERHEADER backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
