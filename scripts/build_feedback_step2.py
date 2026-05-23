#!/usr/bin/env python3
"""
build_feedback_step2.py — /feedback step 2: Telegram command branch.

Per docs/feedback-command-plan.md §5 step 2. Wires the /feedback text
command end-to-end through to a confirmation card with Yes/Modify/No
buttons. Step 3 will add the callback handlers (Save/Discard/Send Result).

Changes:
- Process Text Reply (jsCode): add /feedback parsing; update the default
  ack_text to list /feedback among the commands.
- Route Text Action (switch): add a new rule for action='feedback_cmd'.
- 3 NEW nodes:
    Hermes Feedback Classify (httpRequest) — POST bridge /feedback
                                              action=classify
    Build Feedback Card      (code)        — assembles the confirmation
                                              text + inline-keyboard
                                              (Yes / Modify / No)
    Send Feedback Card       (httpRequest) — Telegram sendMessage

Topology:
  Route Text Action [feedback_cmd] -> Hermes Feedback Classify ->
    Build Feedback Card -> Send Feedback Card

Usage:
  python3 scripts/build_feedback_step2.py            # dry run
  python3 scripts/build_feedback_step2.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

# --- Process Text Reply: insert /feedback handler after /caps -------------
PTR_OLD_AFTER_CAPS = (
    "// (F) /caps — report autonomous-mode safety-cap status (BUG-2)\n"
    "if (text.toLowerCase() === '/caps') {\n"
    "  return { json: { action: 'caps_cmd', admin_chat_id: adminChatId } };\n"
    "}\n")
PTR_NEW_AFTER_CAPS = PTR_OLD_AFTER_CAPS + (
    "\n"
    "// (G) /feedback <text> — operator behavioural feedback (classify +\n"
    "//     confirm + store in customer_notes / behavior_rules)\n"
    "{\n"
    "  const lo = text.toLowerCase();\n"
    "  if (lo === '/feedback' || lo.indexOf('/feedback ') === 0) {\n"
    "    const fb = text.slice(9).trim();\n"
    "    if (!fb) {\n"
    "      return { json: { action: 'ack', admin_chat_id: adminChatId,\n"
    "        ack_text: 'Usage:  /feedback <text>\\n'\n"
    "          + 'examples:\\n'\n"
    "          + '  /feedback never offer phone calls\\n'\n"
    "          + '  /feedback Mark booked Aurora last March, mention it\\n'\n"
    "          + '  /feedback offer fine dining first to family groups'\n"
    "      } };\n"
    "    }\n"
    "    return { json: { action: 'feedback_cmd', feedback_text: fb,\n"
    "      admin_chat_id: adminChatId } };\n"
    "  }\n"
    "}\n")

# --- Process Text Reply: update the default ack_text help line -------------
PTR_OLD_ACK = (
    "ack_text: '⚠️ Nothing sent. Reply to a draft card, use  "
    "/send <chatId> <message>, or start a lead with  "
    "/lead <name> <phone> <details>' }")
PTR_NEW_ACK = (
    "ack_text: '⚠️ Nothing sent. Commands: /send <chatId> <text>, "
    "/lead <name> <phone> <details>, /auto (reply to a card), /manual, "
    "/caps, /rules, /feedback <text>. Or reply to a draft card with "
    "refinement feedback.' }")

CLASSIFY_BODY = (
    '={ "action": "classify", "text": '
    '{{ JSON.stringify($json.feedback_text) }} }')

BUILD_CARD_JS = (
    "// /feedback step 2 — assemble the confirmation card text + Yes/Modify/\n"
    "// No inline keyboard from the classifier's response.\n"
    "const cls = $json || {};\n"
    "const raw = $('Process Text Reply').item.json.feedback_text || '';\n"
    "const chatId = $('Process Text Reply').item.json.admin_chat_id;\n"
    "if (cls.ok !== true) {\n"
    "  return [{ json: {\n"
    "    chat_id: chatId,\n"
    "    text: '⚠️ Couldn\\'t classify that feedback: '\n"
    "      + (cls.error || 'unknown error') + '\\n\\n\"' + raw + '\"',\n"
    "    reply_markup: { inline_keyboard: [] }\n"
    "  } }];\n"
    "}\n"
    "const pid = cls.proposal_id;\n"
    "const lines = ['📋 Feedback received:',\n"
    "               '\"' + raw + '\"', ''];\n"
    "const tag = (cls.classification === 'CUSTOMER_NOTE') ? '👤 CUSTOMER NOTE'\n"
    "          : (cls.classification === 'GLOBAL_RULE')   ? '🌍 GLOBAL RULE'\n"
    "          : (cls.classification === 'SCENARIO_RULE') ? '🎯 SCENARIO RULE'\n"
    "          : cls.classification;\n"
    "lines.push('Classified: ' + tag);\n"
    "if (cls.classification === 'CUSTOMER_NOTE') {\n"
    "  if (cls.customer_id) {\n"
    "    lines.push('For: ' + (cls.customer_name || '(name)')\n"
    "               + '  (' + cls.customer_id + ')');\n"
    "  } else if (cls.matches && cls.matches.length > 1) {\n"
    "    lines.push('⚠️ Multiple customers match \"'\n"
    "               + cls.customer_name + '\":');\n"
    "    for (const m of cls.matches) {\n"
    "      lines.push('  • ' + m.name + ' — ' + m.customer_id);\n"
    "    }\n"
    "    lines.push('Tap Modify and re-type with the explicit phone.');\n"
    "  } else {\n"
    "    lines.push('⚠️ No customer record matches \"'\n"
    "               + cls.customer_name + '\". Tap Modify to specify.');\n"
    "  }\n"
    "} else if (cls.classification === 'SCENARIO_RULE') {\n"
    "  lines.push('Scenario: ' + (cls.scenario || '(unspecified)'));\n"
    "}\n"
    "lines.push('', '→ ' + (cls.summary || cls.text || ''));\n"
    "const reply_markup = { inline_keyboard: [[\n"
    "  { text: '✅ Yes',    callback_data: 'feedback_yes:' + pid },\n"
    "  { text: '✏️ Modify', callback_data: 'feedback_modify:' + pid },\n"
    "  { text: '❌ No',     callback_data: 'feedback_no:' + pid }\n"
    "]] };\n"
    "return [{ json: { chat_id: chatId, text: lines.join('\\n'),\n"
    "                  reply_markup: reply_markup } }];\n")

SEND_CARD_BODY = (
    '={ "chat_id": {{ $json.chat_id }}, '
    '"text": {{ JSON.stringify($json.text) }}, '
    '"reply_markup": {{ JSON.stringify($json.reply_markup) }} }')


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def feedback_classify_node(position):
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/feedback",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": CLASSIFY_BODY,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": str(uuid.uuid4()), "name": "Hermes Feedback Classify",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
    }


def build_card_node(position):
    return {
        "parameters": {"jsCode": BUILD_CARD_JS},
        "id": str(uuid.uuid4()), "name": "Build Feedback Card",
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": position,
    }


def send_card_node(position):
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": SEND_CARD_BODY,
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": "Send Feedback Card",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
    }


def feedback_cmd_rule():
    """A Route Text Action switch rule that matches action='feedback_cmd'."""
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose", "version": 1},
            "conditions": [{
                "id": str(uuid.uuid4()),
                "leftValue": "={{ $json.action }}",
                "rightValue": "feedback_cmd",
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": "feedback_cmd",
    }


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_feedback_step2.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Hermes Feedback Classify" in by:
        print("2. already applied — Hermes Feedback Classify present.")
        return
    for req in ("Process Text Reply", "Route Text Action", "Hermes Caps"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- Process Text Reply edits ---
    ptr = by["Process Text Reply"]["parameters"]
    js = ptr.get("jsCode", "")
    if "feedback_cmd" in js:
        raise DeployError("Process Text Reply already mentions feedback_cmd")
    if PTR_OLD_AFTER_CAPS not in js:
        raise DeployError("Process Text Reply: /caps anchor not found")
    if PTR_OLD_ACK not in js:
        raise DeployError("Process Text Reply: ack_text anchor not found")
    js = js.replace(PTR_OLD_AFTER_CAPS, PTR_NEW_AFTER_CAPS, 1)
    js = js.replace(PTR_OLD_ACK, PTR_NEW_ACK, 1)
    ptr["jsCode"] = js

    # --- Route Text Action: append a feedback_cmd rule ---
    rta = by["Route Text Action"]["parameters"]
    rules = rta.setdefault("rules", {}).setdefault("values", [])
    if any(r.get("outputKey") == "feedback_cmd" for r in rules):
        raise DeployError("Route Text Action already has feedback_cmd")
    new_rule_index = len(rules)
    rules.append(feedback_cmd_rule())

    # --- add the three new nodes ---
    cap_pos = by["Hermes Caps"]["position"]
    y = cap_pos[1] + 160
    nodes.append(feedback_classify_node([cap_pos[0], y]))
    nodes.append(build_card_node([cap_pos[0] + 220, y]))
    nodes.append(send_card_node([cap_pos[0] + 440, y]))

    # --- wire connections ---
    # Route Text Action: ensure main has slots up to new_rule_index
    rta_conns = conns.setdefault("Route Text Action", {})
    main = rta_conns.setdefault("main", [])
    while len(main) <= new_rule_index:
        main.append([])
    main[new_rule_index] = one("Hermes Feedback Classify")
    conns["Hermes Feedback Classify"] = {"main": [one("Build Feedback Card")]}
    conns["Build Feedback Card"] = {"main": [one("Send Feedback Card")]}

    print(f"2. nodes: {len(nodes) - 3} -> {len(nodes)}  (+3 feedback nodes)")
    print("3. Process Text Reply: /feedback handler + updated ack_text")
    print(f"4. Route Text Action: new rule #{new_rule_index} for feedback_cmd")
    print("5. wired: Route Text Action[feedback_cmd] -> "
          "Hermes Feedback Classify -> Build Feedback Card -> Send Feedback Card")

    if not deploy:
        print("\nDRY RUN — anchors matched. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("6. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="FEEDBACKS2")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = all(x in fnames for x in ("Hermes Feedback Classify",
                                   "Build Feedback Card",
                                   "Send Feedback Card"))
    print(f"7. VERIFY: 3 nodes present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("FEEDBACK STEP 2 DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-FEEDBACKS2 backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
