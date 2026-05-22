#!/usr/bin/env python3
"""
build_nomod_payment.py — Nomod payment links, minimal build (components 2-4).

Per docs/nomod-minimal-plan.md. Adds the payment-link branch to the live
workflow:

  Parse Response -> Customer Facts -> Check Payment Trigger (NEW IF)
     should_send_payment true  -> Generate Payment Link (NEW http)
                               -> Build Payment Message (NEW code)
                               -> Queue & Format
     should_send_payment false -> Queue & Format   (unchanged path)

- Build Prompt systemPrompt: appended a payment-link signal instruction —
  Hermes emits should_send_payment / payment_amount / payment_summary only
  when the booking is fully confirmed + the customer says to book it.
- Parse Response: extracts the 3 fields with safe defaults.
- Generate Payment Link: POST bridge /payment-link (onError continueRegularOutput).
- Build Payment Message: assembles the WhatsApp message (Maria's reply +
  booking line + link) and the operator card header; on a link failure it
  posts the reply with a warning header and no link.
- Queue & Format: when the payment branch ran, uses payment_message as the
  draft and prepends payment_header. Non-payment path is untouched (try/catch).

3 nodes added · Build Prompt + Parse Response + Queue & Format edited ·
2 connections rewired. Deploys via n8n_deploy.safe_put.

Usage:
  python3 scripts/build_nomod_payment.py            # dry run
  python3 scripts/build_nomod_payment.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

# --- Build Prompt: append the payment-link signal -------------------------
BP_ANCHOR = ('Format when nothing matches:\n'
             '  "break_condition": {"hit": false}\n')
BP_APPEND = BP_ANCHOR + (
    "\n## Payment link signal\n\n"
    "When — and ONLY when — ALL of these are true: the yacht is chosen, the "
    "date and duration are set, the price has been stated AND the customer "
    "has acknowledged it, AND the customer has clearly said they want to book "
    "it (\"book it\", \"let's do it\", \"I'm in\", \"send the link\", or a "
    "clear equivalent) — add these fields to your JSON response:\n"
    "  \"should_send_payment\": true,\n"
    "  \"payment_amount\": <the confirmed total price in AED — digits only, "
    "no currency symbol, no commas>,\n"
    "  \"payment_summary\": \"<one short line: yacht · date · party size>\"\n"
    "If ANY of those is not yet certain, set \"should_send_payment\": false "
    "and do NOT add the other two — instead just ask, naturally, in your "
    "reply (e.g. \"want me to send the payment link to lock it in?\"). NEVER "
    "invent or guess a price. Your \"messages\" reply is written exactly as "
    "normal — warm, lowercase; add urgency only if the conversation genuinely "
    "calls for it.\n")

# --- Parse Response: extract the 3 fields ---------------------------------
PR_ANCHOR_FAIL = '    parse_status: "extract_failed",'
PR_REPLACE_FAIL = ('    parse_status: "extract_failed",\n'
                    '    should_send_payment: false,\n'
                    '    payment_amount: 0,\n'
                    '    payment_summary: "",')
PR_ANCHOR_OK = '  parse_status: parseStatus,'
PR_REPLACE_OK = (
    '  should_send_payment: !!(parsed && parsed.should_send_payment === true),\n'
    '  payment_amount: (parsed && Number(parsed.payment_amount)) || 0,\n'
    '  payment_summary: (parsed && typeof parsed.payment_summary === "string")\n'
    '    ? parsed.payment_summary : "",\n'
    '  parse_status: parseStatus,')

# --- Queue & Format: 3 edits ----------------------------------------------
QF_ANCHOR_A = ("catch (e) { customerHeader = ''; }\n"
               "const data = $getWorkflowStaticData('global');")
QF_REPLACE_A = (
    "catch (e) { customerHeader = ''; }\n"
    "// Nomod: when the payment branch ran, use its message + header\n"
    "let paymentMessage = null, paymentHeader = '';\n"
    "try {\n"
    "  const __pm = $('Build Payment Message').item.json;\n"
    "  if (__pm && __pm.payment_message) {\n"
    "    paymentMessage = String(__pm.payment_message);\n"
    "    paymentHeader = __pm.payment_header || '';\n"
    "  }\n"
    "} catch (e) { paymentMessage = null; }\n"
    "const data = $getWorkflowStaticData('global');")

QF_ANCHOR_B = ("const messages = (Array.isArray(input.messages) "
               "&& input.messages.length)\n"
               "  ? input.messages.map(m => String(m))\n"
               "  : ['(empty draft)'];")
QF_REPLACE_B = (
    "const messages = paymentMessage\n"
    "  ? [paymentMessage]\n"
    "  : ((Array.isArray(input.messages) && input.messages.length)\n"
    "      ? input.messages.map(m => String(m))\n"
    "      : ['(empty draft)']);")

QF_ANCHOR_C = "const reply_markup = {"
QF_REPLACE_C = ("if (paymentHeader) lines = [paymentHeader].concat(lines);\n\n"
                "const reply_markup = {")

GPL_BODY = (
    '={ "customer_id": {{ JSON.stringify($(\'Parse Response\').item.json'
    '.customer_phone) }}, "customer_name": {{ JSON.stringify($(\'Parse '
    'Response\').item.json.customer_name) }}, "amount": {{ $(\'Parse '
    'Response\').item.json.payment_amount }}, "payment_summary": '
    '{{ JSON.stringify($(\'Parse Response\').item.json.payment_summary) }} }')

BUILD_PAYMENT_MSG_JS = (
    "// Nomod minimal: build the payment WhatsApp message + operator card header\n"
    "const pr = $('Parse Response').item.json || {};\n"
    "let link = {};\n"
    "try { link = $('Generate Payment Link').item.json || {}; } "
    "catch (e) { link = {}; }\n"
    "const msgs = Array.isArray(pr.messages) ? pr.messages.map(m => String(m)) : [];\n"
    "const reply = msgs.join('\\n\\n');\n"
    "const amount = Number(pr.payment_amount) || 0;\n"
    "const summary = String(pr.payment_summary || '');\n"
    "const amountStr = 'AED ' + amount.toLocaleString('en-US');\n"
    "const ok = (link.ok === true && !!link.link_url);\n"
    "let payment_message, payment_header;\n"
    "if (ok) {\n"
    "  payment_message = reply + '\\n\\n'\n"
    "    + '🛥️ ' + summary + '\\n'\n"
    "    + '💳 total: ' + amountStr + '\\n\\n'\n"
    "    + 'tap to complete your booking: ' + link.link_url;\n"
    "  payment_header = '💳 PAYMENT LINK — ' + amountStr + '\\n'\n"
    "    + '👤 ' + (pr.customer_name || pr.customer_phone || 'customer')\n"
    "    + (summary ? ' · ' + summary : '')\n"
    "    + '\\n──────────────';\n"
    "} else {\n"
    "  payment_message = reply;\n"
    "  payment_header = '⚠️ PAYMENT LINK NOT CREATED — '\n"
    "    + String(link.error || 'unknown error') + '\\n'\n"
    "    + '(draft posted without a link — check the amount / bridge)'\n"
    "    + '\\n──────────────';\n"
    "}\n"
    "return [{ json: {\n"
    "  payment_message: payment_message,\n"
    "  payment_header: payment_header,\n"
    "  payment_blocked: !ok\n"
    "} }];\n")


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_nomod_payment.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Check Payment Trigger" in by:
        print("2. already applied — 'Check Payment Trigger' present. nothing to do.")
        return
    for req in ("Build Prompt", "Parse Response", "Customer Facts",
                "Queue & Format"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- Build Prompt: append the payment signal ---
    bp_asg = by["Build Prompt"]["parameters"]["assignments"]["assignments"]
    sp = next((a for a in bp_asg if a["name"] == "systemPrompt"), None)
    if sp is None:
        raise DeployError("Build Prompt: systemPrompt assignment not found")
    if "should_send_payment" in sp["value"]:
        raise DeployError("Build Prompt already mentions should_send_payment")
    if BP_ANCHOR not in sp["value"]:
        raise DeployError("Build Prompt: break_condition anchor not found")
    sp["value"] = sp["value"].replace(BP_ANCHOR, BP_APPEND, 1)

    # --- Parse Response: extract the 3 fields ---
    pr = by["Parse Response"]["parameters"]
    js = pr.get("jsCode", "")
    for anc in (PR_ANCHOR_FAIL, PR_ANCHOR_OK):
        if anc not in js:
            raise DeployError(f"Parse Response: anchor not found: {anc!r}")
    if "should_send_payment" in js:
        raise DeployError("Parse Response already extracts should_send_payment")
    js = js.replace(PR_ANCHOR_FAIL, PR_REPLACE_FAIL, 1)
    js = js.replace(PR_ANCHOR_OK, PR_REPLACE_OK, 1)
    pr["jsCode"] = js

    # --- Queue & Format: 3 edits ---
    qf = by["Queue & Format"]["parameters"]
    qjs = qf.get("jsCode", "")
    for anc in (QF_ANCHOR_A, QF_ANCHOR_B, QF_ANCHOR_C):
        if anc not in qjs:
            raise DeployError(f"Queue & Format: anchor not found: {anc[:50]!r}")
    if "paymentMessage" in qjs:
        raise DeployError("Queue & Format already references paymentMessage")
    qjs = qjs.replace(QF_ANCHOR_A, QF_REPLACE_A, 1)
    qjs = qjs.replace(QF_ANCHOR_B, QF_REPLACE_B, 1)
    qjs = qjs.replace(QF_ANCHOR_C, QF_REPLACE_C, 1)
    qf["jsCode"] = qjs

    # --- positions: space the 3 new nodes between Customer Facts & Queue&Format
    cf = by["Customer Facts"]["position"]
    qfp = by["Queue & Format"]["position"]
    midx = (cf[0] + qfp[0]) // 2
    y = cf[1] + 200

    check_node = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": str(uuid.uuid4()),
                    "leftValue": "={{ $('Parse Response').item.json"
                                 ".should_send_payment }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Check Payment Trigger",
        "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [midx - 120, y],
    }
    gen_node = {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/payment-link",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": GPL_BODY,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": str(uuid.uuid4()), "name": "Generate Payment Link",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [midx + 40, y],
        "onError": "continueRegularOutput",
    }
    msg_node = {
        "parameters": {"jsCode": BUILD_PAYMENT_MSG_JS},
        "id": str(uuid.uuid4()), "name": "Build Payment Message",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": [midx + 200, y],
    }
    nodes.extend([check_node, gen_node, msg_node])

    # --- rewire ---
    # Customer Facts -> Check Payment Trigger
    conns["Customer Facts"]["main"][0] = one("Check Payment Trigger")
    # Check Payment Trigger: #0 true -> Generate Payment Link ; #1 false -> Queue & Format
    conns["Check Payment Trigger"] = {"main": [
        one("Generate Payment Link"),
        one("Queue & Format"),
    ]}
    conns["Generate Payment Link"] = {"main": [one("Build Payment Message")]}
    conns["Build Payment Message"] = {"main": [one("Queue & Format")]}

    print(f"2. nodes: {len(nodes) - 3} -> {len(nodes)}  (+3: Check Payment "
          "Trigger, Generate Payment Link, Build Payment Message)")
    print("3. Build Prompt: payment-link signal appended to systemPrompt")
    print("4. Parse Response: should_send_payment / payment_amount / "
          "payment_summary extracted")
    print("5. Queue & Format: payment_message used as draft, payment_header "
          "prepended (payment branch only)")
    print("6. rewired: Customer Facts -> Check Payment Trigger -> "
          "[Generate Payment Link -> Build Payment Message -> Queue & Format] "
          "| [Queue & Format]")

    if not deploy:
        print("\nDRY RUN — all anchors matched. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("7. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="NOMODPAYMENT")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = all(x in fnames for x in ("Check Payment Trigger",
                                   "Generate Payment Link",
                                   "Build Payment Message"))
    print(f"8. VERIFY: 3 payment nodes present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("NOMOD PAYMENT DEPLOYED — payment-link branch is live."
          if ok else "x VERIFY FAILED — inspect; PRE-NOMODPAYMENT backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
