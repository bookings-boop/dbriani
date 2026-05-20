#!/usr/bin/env python3
"""
hermes_integration.py - Hermes workflow surgery (Steps 3d + 4 + 5.3).

surgery(wf) applies, idempotently:

  STEP 3d - route drafting through the Hermes bridge
    Format Context -> Call Hermes Bridge -> Parse Response
    Prep Regen     -> Call Hermes Bridge (Regen) -> Parse Regen

  STEP 4 - conversational refinement loop
    Reply to a PENDING draft -> Prep Refine -> Call Hermes Bridge (Refine)
      -> Parse Refine -> Edit Telegram (Refine)

  STEP 5.3 - permanent rule capture (spec §4.6 + §9)
    On a refinement, Hermes may return a `suggested_rule`. If present:
      Parse Refine -> IF Has Rule -> Post Rule Suggestion  (Telegram, with a
        [Save as rule] button)
    Tapping the button:
      Route Action[saverule] -> Save Rule (-> bridge POST /save-rule)
        -> Confirm Rule Saved

Each step is skipped if already applied, so the same function transforms
both the committed workflow file and the live workflow during deploy.

Usage:  python3 scripts/hermes_integration.py
"""
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMITTED_FILE = ROOT / "workflows" / "phase-1b-telegram.json"

BRIDGE_DRAFT_URL = "http://172.18.0.1:8788/draft"
BRIDGE_SAVERULE_URL = "http://172.18.0.1:8788/save-rule"
BRIDGE_CRED_NAME = "Hermes Bridge"
TG_URL = "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/"


def _uid(slug):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "dubriani-ai-agent/" + slug))


# Step 3d
ID_BRIDGE = _uid("node/call-hermes-bridge")
ID_BRIDGE_REGEN = _uid("node/call-hermes-bridge-regen")
# Step 4
ID_PREP_REFINE = _uid("node/prep-refine")
ID_BRIDGE_REFINE = _uid("node/call-hermes-bridge-refine")
ID_PARSE_REFINE = _uid("node/parse-refine")
ID_EDIT_REFINE = _uid("node/edit-telegram-refine")
ID_COND_REFINE = _uid("cond/route-text-refine")
# Step 5.3
ID_IF_HAS_RULE = _uid("node/if-has-rule")
ID_POST_RULE = _uid("node/post-rule-suggestion")
ID_SAVE_RULE = _uid("node/save-rule")
ID_CONFIRM_RULE = _uid("node/confirm-rule-saved")
ID_COND_SAVERULE = _uid("cond/route-action-saverule")
ID_COND_HASRULE = _uid("cond/if-has-rule")

REMOVE_3D = ["Build Prompt", "Claude AI", "Build Regen Prompt", "Claude AI (Regen)"]


# --- node builders ---------------------------------------------------------

def _bridge_body(src_node, mode, with_refine=False):
    id_field = "customerChatId" if src_node == "Format Context" else "customerPhone"
    lines = [
        "={",
        f'  "customer_name": {{{{ JSON.stringify($(\'{src_node}\').item.json.customerName || "") }}}},',
        f'  "customer_id": {{{{ JSON.stringify($(\'{src_node}\').item.json.{id_field} || "") }}}},',
        f'  "history": {{{{ JSON.stringify($(\'{src_node}\').item.json.conversationHistory || "") }}}},',
        f'  "incoming_message": {{{{ JSON.stringify($(\'{src_node}\').item.json.userMessage || "") }}}},',
        f'  "mode": "{mode}"' + ("," if with_refine else ""),
    ]
    if with_refine:
        lines.append(
            f'  "refine_instruction": {{{{ JSON.stringify($(\'{src_node}\').item.json.regenHint || "") }}}}'
        )
    lines.append("}")
    return "\n".join(lines)


def _http_node(node_id, name, position, url, body, on_error=False, credential=False):
    p = {
        "method": "POST", "url": url,
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [{"name": "Content-Type", "value": "application/json"}]
        },
        "sendBody": True, "specifyBody": "json", "jsonBody": body,
        "options": {"timeout": 150000} if credential else {},
    }
    if credential:
        p["authentication"] = "genericCredentialType"
        p["genericAuthType"] = "httpHeaderAuth"
    n = {
        "parameters": p, "id": node_id, "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
    }
    if credential:
        n["credentials"] = {"httpHeaderAuth": {"id": None, "name": BRIDGE_CRED_NAME}}
    if on_error:
        n["onError"] = "continueErrorOutput"
    return n


def _code_node(node_id, name, position, js):
    return {
        "parameters": {"mode": "runOnceForEachItem", "language": "javaScript",
                       "jsCode": js},
        "id": node_id, "name": name, "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": position,
    }


def _conn(node, index=0):
    return {"node": node, "type": "main", "index": index}


# --- Step 3d / 4 code nodes ------------------------------------------------

PARSE_RESPONSE_JS = r"""// Parse the Hermes bridge response into the downstream draft contract.
const r = $input.item.json;
const fc = $('Format Context').item.json;
let messages, notes, parseStatus, rawResponse;
if (r && r.ok === true && Array.isArray(r.messages) && r.messages.length > 0) {
  messages = r.messages.slice(0, 4).map(m => String(m));
  notes = r.notes_for_zayn || '(no notes)';
  parseStatus = 'ok';
  rawResponse = r.raw || JSON.stringify(r);
} else {
  messages = ['(Hermes bridge returned no draft - check the hermes-bridge service)'];
  notes = 'ERROR: bridge response not ok - ' + JSON.stringify(r).slice(0, 300);
  parseStatus = 'bridge_failed';
  rawResponse = JSON.stringify(r);
}
return { json: {
  messages: messages,
  messages_count: messages.length,
  notes: notes,
  parse_status: parseStatus,
  raw_response: rawResponse,
  hermes_session_id: (r && r.session_id) || '',
  customer_phone: fc.customerChatId,
  customer_name: fc.customerName,
  user_message: fc.userMessage,
  conversation_history: fc.conversationHistory,
  history_count: fc.historyCount
} };"""

PARSE_REGEN_JS = r"""// Parse the Hermes-bridge regen response, update the existing draft in the queue.
const r = $input.item.json;
let msgs, notes;
if (r && r.ok === true && Array.isArray(r.messages) && r.messages.length > 0) {
  msgs = r.messages.slice(0, 4).map(m => String(m));
  notes = r.notes_for_zayn || '(no notes)';
} else {
  msgs = ['(Hermes bridge returned no draft on regen - check the hermes-bridge service)'];
  notes = '(regen failed: ' + JSON.stringify(r).slice(0, 200) + ')';
}
const draftId = $('Prep Regen').item.json.draft_id;
const data = $getWorkflowStaticData('global');
const entry = (data.pendingQueue || []).find(d => d.id === draftId);
if (entry) {
  entry.messages = msgs;
  entry.draft_text = msgs.join('\n\n');
  entry.notes = notes;
  entry.messages_sent_count = 0;
}
const preview = msgs.length === 1 ? msgs[0] : msgs.map((m, i) => (i + 1) + '. ' + m).join('\n');
return { json: {
  draft_id: draftId,
  new_preview: preview,
  new_notes: notes,
  customer_phone: entry ? entry.customer_phone : 'unknown',
  customer_name: entry ? entry.customer_name : '',
  customer_message: entry ? entry.customer_message : ''
} };"""

PREP_REFINE_JS = r"""// Step 4: prepare a refinement - Zayn's instruction re-drafts a pending draft.
const inp = $input.item.json;
const draftId = inp.draft_id;
const instruction = (inp.instruction || '').trim();
const data = $getWorkflowStaticData('global');
const draft = (data.pendingQueue || []).find(d => d.id === draftId);
if (!draft) {
  return { json: { error: 'Draft not found for refinement', draft_id: draftId } };
}
const hint = 'The previous draft was: "' + draft.draft_text + '". '
  + 'Zayn\'s refinement instruction: "' + instruction + '". '
  + 'Produce a revised reply that applies this instruction exactly, while '
  + 'staying within all hard rules and the conversation history.';
return { json: {
  userMessage: draft.customer_message,
  conversationHistory: draft.conversation_history || 'First contact, no prior messages.',
  historyCount: draft.history_count || 0,
  customerPhone: draft.customer_phone,
  customerName: draft.customer_name || '',
  regenHint: hint,
  instruction: instruction,
  draft_id: draftId
} };"""

# Step 5.3: Parse Refine also captures Hermes's suggested behavior rule.
PARSE_REFINE_JS = r"""// Step 4+5.3: parse the refinement response, update the draft, capture any
// durable behavior rule Hermes suggested.
const r = $input.item.json;
let msgs, notes;
if (r && r.ok === true && Array.isArray(r.messages) && r.messages.length > 0) {
  msgs = r.messages.slice(0, 4).map(m => String(m));
  notes = r.notes_for_zayn || '(no notes)';
} else {
  msgs = ['(Hermes bridge returned no draft on refine - check the hermes-bridge service)'];
  notes = '(refine failed: ' + JSON.stringify(r).slice(0, 200) + ')';
}
const draftId = $('Prep Refine').item.json.draft_id;
const data = $getWorkflowStaticData('global');
const entry = (data.pendingQueue || []).find(d => d.id === draftId);
let sugg = (r && r.suggested_rule && typeof r.suggested_rule === 'object') ? r.suggested_rule : null;
const ruleText = sugg ? String(sugg.text || '').trim() : '';
const ruleScope = (sugg && String(sugg.scope || '').trim() === 'customer') ? 'customer' : 'global';
if (!ruleText) sugg = null;
if (entry) {
  entry.messages = msgs;
  entry.draft_text = msgs.join('\n\n');
  entry.notes = notes;
  entry.messages_sent_count = 0;
  entry.suggested_rule = sugg ? { text: ruleText, scope: ruleScope } : null;
}
const preview = msgs.length === 1 ? msgs[0] : msgs.map((m, i) => (i + 1) + '. ' + m).join('\n');
return { json: {
  draft_id: draftId,
  new_preview: preview,
  new_notes: notes,
  instruction: $('Prep Refine').item.json.instruction,
  telegram_message_id: entry ? entry.telegram_message_id : null,
  telegram_chat_id: entry ? entry.telegram_chat_id : 5532831477,
  customer_phone: entry ? entry.customer_phone : 'unknown',
  customer_name: entry ? entry.customer_name : '',
  has_suggested_rule: !!sugg,
  suggested_rule_text: ruleText,
  suggested_rule_scope: ruleScope
} };"""

EDIT_REFINE_BODY = (
    "={\n"
    "  \"chat_id\": {{ $json.telegram_chat_id }},\n"
    "  \"message_id\": {{ $json.telegram_message_id }},\n"
    "  \"text\": {{ JSON.stringify(\"✏️ REFINED — your note: \\\"\" "
    "+ $json.instruction + \"\\\"\\n\\n---\\n💬 NEW DRAFT:\\n\" "
    "+ $json.new_preview + \"\\n\\n📝 Notes: \" + $json.new_notes "
    "+ \"\\n\\n💡 Reply again to refine further\") }},\n"
    "  \"reply_markup\": {{ JSON.stringify({inline_keyboard: ["
    "[{text: \"✅ Send\", callback_data: \"send:\" + $json.draft_id}, "
    "{text: \"✏️ Edit\", callback_data: \"edit:\" + $json.draft_id}], "
    "[{text: \"🔁 Regen\", callback_data: \"regen:\" + $json.draft_id}, "
    "{text: \"❌ Skip\", callback_data: \"skip:\" + $json.draft_id}]]}) }}\n"
    "}"
)

POST_RULE_BODY = (
    "={\n"
    "  \"chat_id\": {{ $json.telegram_chat_id }},\n"
    "  \"text\": {{ JSON.stringify(\"🧠 That refinement looks like a durable "
    "preference.\\n\\n\\\"\" + $json.suggested_rule_text + \"\\\"\\n(scope: \" "
    "+ $json.suggested_rule_scope + \")\\n\\nSave it for review? It is saved "
    "INACTIVE — it shapes drafts only after you approve it.\") }},\n"
    "  \"reply_markup\": {{ JSON.stringify({inline_keyboard: [["
    "{text: \"💾 Save as rule\", callback_data: \"saverule:\" + $json.draft_id}"
    "]]}) }}\n"
    "}"
)

SAVE_RULE_BODY = (
    "={\n"
    "  \"rule_text\": {{ JSON.stringify($('Parse Callback').item.json.draft.suggested_rule.text) }},\n"
    "  \"scope\": {{ JSON.stringify($('Parse Callback').item.json.draft.suggested_rule.scope) }},\n"
    "  \"scope_value\": {{ JSON.stringify("
    "$('Parse Callback').item.json.draft.suggested_rule.scope === 'customer' "
    "? ($('Parse Callback').item.json.draft.customer_phone || '') : '') }},\n"
    "  \"created_via\": \"refinement_capture\"\n"
    "}"
)

CONFIRM_RULE_BODY = (
    "={\n"
    "  \"chat_id\": {{ $('Parse Callback').item.json.chat_id }},\n"
    "  \"message_id\": {{ $('Parse Callback').item.json.message_id }},\n"
    "  \"text\": {{ JSON.stringify(\"✅ Rule saved (id \" "
    "+ ($json.rule_id || \"?\") + \") — INACTIVE, pending your approval. "
    "Activate it and Hermes applies it to future drafts.\") }}\n"
    "}"
)


# --- Step 3d ---------------------------------------------------------------

def _apply_3d(wf):
    by = {n["name"]: n for n in wf["nodes"]}
    if "Call Hermes Bridge" in by:
        return False
    for req in ["Format Context", "Parse Response", "Prep Regen",
                "Parse Regen", "Build Alert"] + REMOVE_3D:
        if req not in by:
            raise SystemExit(f"x 3d: expected node '{req}' not found")

    pos_i = by["Claude AI"]["position"]
    pos_r = by["Claude AI (Regen)"]["position"]
    wf["nodes"] = [n for n in wf["nodes"] if n["name"] not in REMOVE_3D]
    wf["nodes"].append(_http_node(ID_BRIDGE, "Call Hermes Bridge", pos_i,
                                  BRIDGE_DRAFT_URL,
                                  _bridge_body("Format Context", "initial"),
                                  on_error=True, credential=True))
    wf["nodes"].append(_http_node(ID_BRIDGE_REGEN, "Call Hermes Bridge (Regen)",
                                  pos_r, BRIDGE_DRAFT_URL,
                                  _bridge_body("Prep Regen", "refine", True),
                                  on_error=True, credential=True))
    by2 = {n["name"]: n for n in wf["nodes"]}
    by2["Parse Response"]["parameters"]["jsCode"] = PARSE_RESPONSE_JS
    by2["Parse Regen"]["parameters"]["jsCode"] = PARSE_REGEN_JS

    c = wf["connections"]
    for dead in REMOVE_3D:
        c.pop(dead, None)
    c["Format Context"] = {"main": [[_conn("Call Hermes Bridge")]]}
    c["Prep Regen"] = {"main": [[_conn("Call Hermes Bridge (Regen)")]]}
    c["Call Hermes Bridge"] = {"main": [[_conn("Parse Response")], [_conn("Build Alert")]]}
    c["Call Hermes Bridge (Regen)"] = {"main": [[_conn("Parse Regen")], [_conn("Build Alert")]]}
    return True


# --- Step 4 ----------------------------------------------------------------

def _apply_4(wf):
    by = {n["name"]: n for n in wf["nodes"]}
    if "Prep Refine" in by:
        return False
    for req in ["Process Text Reply", "Route Text Action", "Queue & Format",
                "Build Alert"]:
        if req not in by:
            raise SystemExit(f"x 4: expected node '{req}' not found")

    wf["nodes"].append(_code_node(ID_PREP_REFINE, "Prep Refine",
                                  [1100, 540], PREP_REFINE_JS))
    wf["nodes"].append(_http_node(ID_BRIDGE_REFINE, "Call Hermes Bridge (Refine)",
                                  [1320, 540], BRIDGE_DRAFT_URL,
                                  _bridge_body("Prep Refine", "refine", True),
                                  on_error=True, credential=True))
    wf["nodes"].append(_code_node(ID_PARSE_REFINE, "Parse Refine",
                                  [1540, 540], PARSE_REFINE_JS))
    wf["nodes"].append(_http_node(ID_EDIT_REFINE, "Edit Telegram (Refine)",
                                  [1760, 540], TG_URL + "editMessageText",
                                  EDIT_REFINE_BODY))

    ptr = by["Process Text Reply"]["parameters"]
    old = ("    return { json: { action: 'send_to_customer', origin: 'reply', "
           "target_chatId: d.customer_phone, final_text: text, draft_id: d.id, "
           "admin_chat_id: adminChatId } };")
    if old not in ptr["jsCode"]:
        raise SystemExit("x 4: Process Text Reply reply-branch not found (drift?)")
    new = ("    if (d.status === 'pending') {\n"
           "      return { json: { action: 'refine', draft_id: d.id, "
           "instruction: text, admin_chat_id: adminChatId } };\n"
           "    }\n" + old)
    ptr["jsCode"] = ptr["jsCode"].replace(old, new, 1)

    rules = by["Route Text Action"]["parameters"]["rules"]["values"]
    rules.append({
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose"},
            "conditions": [{
                "id": ID_COND_REFINE,
                "leftValue": "={{ $json.action }}",
                "rightValue": "refine",
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True, "outputKey": "refine",
    })

    qf = by["Queue & Format"]["parameters"]
    qmark = "'\U0001f4dd Notes: ' + pending.notes);"
    if qmark not in qf["jsCode"]:
        raise SystemExit("x 4: Queue & Format notes line not found (drift?)")
    qf["jsCode"] = qf["jsCode"].replace(
        qmark,
        "'\U0001f4dd Notes: ' + pending.notes, '', "
        "'\U0001f4a1 Reply to this message to refine · "
        "\U0001f501 Regen for a fresh take');", 1)

    c = wf["connections"]
    rta = c.setdefault("Route Text Action", {"main": []})
    while len(rta["main"]) < 2:
        rta["main"].append([])
    rta["main"].append([_conn("Prep Refine")])
    c["Prep Refine"] = {"main": [[_conn("Call Hermes Bridge (Refine)")]]}
    c["Call Hermes Bridge (Refine)"] = {"main": [
        [_conn("Parse Refine")], [_conn("Build Alert")]]}
    c["Parse Refine"] = {"main": [[_conn("Edit Telegram (Refine)")]]}
    return True


# --- Step 5.3 --------------------------------------------------------------

def _apply_5(wf):
    by = {n["name"]: n for n in wf["nodes"]}
    if "Save Rule" in by:
        return False
    for req in ["Parse Refine", "Route Action", "Parse Callback", "Build Alert",
                "Edit Telegram (Refine)"]:
        if req not in by:
            raise SystemExit(f"x 5.3: expected node '{req}' not found")

    # Parse Refine gains suggested_rule capture.
    by["Parse Refine"]["parameters"]["jsCode"] = PARSE_REFINE_JS

    # new nodes
    wf["nodes"].append({
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose"},
                "conditions": [{
                    "id": ID_COND_HASRULE,
                    "leftValue": "={{ $json.has_suggested_rule }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": ID_IF_HAS_RULE, "name": "IF Has Rule",
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": [1760, 720],
    })
    wf["nodes"].append(_http_node(ID_POST_RULE, "Post Rule Suggestion",
                                  [1980, 720], TG_URL + "sendMessage",
                                  POST_RULE_BODY))
    wf["nodes"].append(_http_node(ID_SAVE_RULE, "Save Rule", [1320, 260],
                                  BRIDGE_SAVERULE_URL, SAVE_RULE_BODY,
                                  on_error=True, credential=True))
    wf["nodes"].append(_http_node(ID_CONFIRM_RULE, "Confirm Rule Saved",
                                  [1540, 260], TG_URL + "editMessageText",
                                  CONFIRM_RULE_BODY))

    # Route Action gains a `saverule` rule (5th output)
    rules = by["Route Action"]["parameters"]["rules"]["values"]
    rules.append({
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose"},
            "conditions": [{
                "id": ID_COND_SAVERULE,
                "leftValue": "={{ $('Parse Callback').item.json.action }}",
                "rightValue": "saverule",
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True, "outputKey": "saverule",
    })

    # connections
    c = wf["connections"]
    c["Parse Refine"] = {"main": [[_conn("Edit Telegram (Refine)"),
                                   _conn("IF Has Rule")]]}
    c["IF Has Rule"] = {"main": [[_conn("Post Rule Suggestion")], []]}
    ra = c.setdefault("Route Action", {"main": []})
    while len(ra["main"]) < 4:
        ra["main"].append([])
    ra["main"].append([_conn("Save Rule")])             # output index 4 = saverule
    c["Save Rule"] = {"main": [[_conn("Confirm Rule Saved")], [_conn("Build Alert")]]}
    return True


def surgery(wf):
    """Apply Steps 3d + 4 + 5.3 idempotently. Returns (wf, changed)."""
    c3 = _apply_3d(wf)
    c4 = _apply_4(wf)
    c5 = _apply_5(wf)
    return wf, (c3 or c4 or c5)


def main():
    if not COMMITTED_FILE.exists():
        sys.exit(f"x {COMMITTED_FILE} not found")
    wf = json.loads(COMMITTED_FILE.read_text())
    before = len(wf["nodes"])
    wf, changed = surgery(wf)
    after = len(wf["nodes"])
    if not changed:
        print("=  workflow already at Steps 3d+4+5.3 - no change")
        return
    COMMITTED_FILE.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"OK  {COMMITTED_FILE.relative_to(ROOT)}  ({before} -> {after} nodes)")
    print("    3d:  drafting via Call Hermes Bridge (+ Regen)")
    print("    4:   reply-to-pending-draft -> refine loop")
    print("    5.3: refinement -> suggested rule -> [Save as rule] -> /save-rule")
    print("    next: run scripts/build_workflow.py")


if __name__ == "__main__":
    main()
