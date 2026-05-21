#!/usr/bin/env python3
"""
build_fr1_fr2.py - apply FR-1 (Edit feedback loop) + FR-2 (new-lead intake)
to the LIVE Dubriani Phase 1B workflow.

FR-1: the Edit button is re-scoped. Pressing Edit now prompts for FEEDBACK
      for Claude; the operator's reply routes to a refine chain
      (Prep Refine -> Build Refine Prompt -> Claude AI (Refine) -> Parse
      Refine -> Edit Telegram (Refine)) that re-drafts and re-renders the
      same card. Nothing is sent to the customer until Send is pressed.

FR-2: "/lead <free-form name/phone/details>" creates an outbound
      first-contact draft. A regex pulls the phone; Claude drafts the
      opener; it flows into the EXISTING Queue & Format -> Send Draft to
      Telegram -> Save Telegram MsgID chain, so the lead card has the same
      Send/Edit/Regen/Skip buttons.

Modified nodes: Edit Telegram (Edit Prompt), Process Text Reply,
                Route Text Action, Queue & Format.
New nodes (9): Prep Refine, Build Refine Prompt, Claude AI (Refine),
               Parse Refine, Edit Telegram (Refine), Prep Lead Draft,
               Build Lead Prompt, Claude AI (Lead), Parse Lead Response.

Default = DRY RUN: GET live, apply in memory, write /tmp/fr12_staged.json,
print a summary. Pass --deploy to back up + PUT to the live workflow.

Usage:  python3 scripts/build_fr1_fr2.py [--deploy]
"""
import copy
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
API = None
WF_NAME = "Dubriani Phase 1B — WhatsApp + Telegram Approval"
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder"}

# ---------------------------------------------------------------- jsCode --

PROCESS_TEXT_REPLY = r"""// route an admin Telegram message: /lead, /send, edit-feedback, reply, or ack
const msg = $input.item.json.message || {};
const text = (msg.text || '').trim();
const adminChatId = (msg.chat && msg.chat.id) ? msg.chat.id : 5532831477;
const data = $getWorkflowStaticData('global');
const queue = data.pendingQueue || [];

// (C) /lead <free-form> -> create an outbound first-contact draft (FR-2)
if (text.toLowerCase().indexOf('/lead') === 0) {
  const leadText = text.slice(5).trim();
  const cand = leadText.match(/[+\d][\d\s().\-]{7,}\d/g) || [];
  let phone = '';
  for (const c of cand) {
    const d = c.replace(/[^\d]/g, '');
    if (d.length >= 9 && d.length > phone.length) phone = d;
  }
  if (!phone) {
    return { json: { action: 'ack', admin_chat_id: adminChatId,
      ack_text: "⚠️ Couldn't find a phone number in that lead. Try:  /lead <name> <phone> <details>" } };
  }
  let norm = phone.replace(/^00/, '');
  if (norm.length === 10 && norm[0] === '0') norm = '971' + norm.slice(1);
  else if (norm.length === 9 && norm[0] === '5') norm = '971' + norm;
  return { json: { action: 'new_lead', lead_phone: norm + '@c.us',
    lead_text: leadText, admin_chat_id: adminChatId } };
}

// (B) /send <chatId> <message>
if (text.toLowerCase().indexOf('/send ') === 0) {
  const rest = text.slice(6).trim();
  const sp = rest.indexOf(' ');
  if (sp < 1) {
    return { json: { action: 'ack', admin_chat_id: adminChatId,
      ack_text: 'Usage:  /send <chatId> <message>' } };
  }
  return { json: { action: 'send_to_customer', origin: 'slash',
    target_chatId: rest.slice(0, sp).trim(), final_text: rest.slice(sp + 1).trim(),
    admin_chat_id: adminChatId } };
}

// (edit) a draft is awaiting feedback -> refine it, do NOT send (FR-1)
const awaiting = queue.find(x => x.status === 'awaiting_edit');
if (awaiting && text) {
  awaiting.status = 'pending';          // release the edit lock; refine redraws
  return { json: { action: 'refine', draft_id: awaiting.id, feedback: text,
    admin_chat_id: adminChatId } };
}

// (A) reply to a draft card with text -> send that text to the customer
const r = msg.reply_to_message;
if (r && r.message_id) {
  // newest-match: telegram message_ids collide across bot/session resets
  let d = null;
  for (let _i = queue.length - 1; _i >= 0; _i--) {
    if (queue[_i].telegram_message_id === r.message_id) { d = queue[_i]; break; }
  }
  if (d && text) {
    return { json: { action: 'send_to_customer', origin: 'reply',
      target_chatId: d.customer_phone, final_text: text, draft_id: d.id,
      admin_chat_id: adminChatId } };
  }
}

return { json: { action: 'ack', admin_chat_id: adminChatId,
  ack_text: '⚠️ Nothing sent. Reply to a draft card, use  /send <chatId> <message>, or start a lead with  /lead <name> <phone> <details>' } };
"""

PREP_REFINE = r"""// FR-1: gather the draft + Zayn's feedback for a refine redraft
const inp = $('Process Text Reply').item.json;
const draftId = inp.draft_id;
const feedback = inp.feedback || '';
const data = $getWorkflowStaticData('global');
const draft = (data.pendingQueue || []).find(d => d.id === draftId);
if (!draft) {
  return { json: { error: 'Draft not found for refine', draft_id: draftId } };
}
return { json: {
  systemPrompt: '',
  userMessage: draft.customer_message || '(no customer message)',
  conversationHistory: draft.conversation_history || 'First contact, no prior messages.',
  historyCount: draft.history_count || 0,
  customerPhone: draft.customer_phone,
  customerName: draft.customer_name || '',
  regenHint: 'Zayn reviewed the previous draft and gave this feedback: "' + feedback
    + '". The previous draft was: "' + draft.draft_text
    + '". Rewrite the reply to incorporate Zayn\'s feedback, staying within all hard rules and the conversation history above.',
  draft_id: draftId,
  telegram_message_id: draft.telegram_message_id,
  admin_chat_id: inp.admin_chat_id || 5532831477
} };
"""

PARSE_REFINE = r"""// FR-1: parse the refine response, update the draft, re-render its card
const response = $input.item.json;
let raw = '';
try { raw = response.content[0].text; } catch (e) {
  return { json: { error: 'Empty Claude response on refine', raw: JSON.stringify(response) } };
}
let cleaned = raw.trim().replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
let parsed;
try {
  parsed = JSON.parse(cleaned);
  if (!Array.isArray(parsed.messages) || !parsed.messages.length) throw new Error('messages missing');
} catch (e) {
  parsed = { messages: [raw], notes_for_zayn: '(parse fail: ' + e.message + ')' };
}
const msgs = parsed.messages.slice(0, 4).map(m => String(m));
const bp = $('Build Refine Prompt').item.json;
const draftId = bp.draft_id;
const data = $getWorkflowStaticData('global');
const entry = (data.pendingQueue || []).find(d => d.id === draftId);
if (entry) {
  entry.messages = msgs;
  entry.draft_text = msgs.join('\n\n');
  entry.notes = parsed.notes_for_zayn || entry.notes;
  entry.messages_sent_count = 0;
  entry.status = 'pending';
}
const preview = msgs.length === 1 ? msgs[0] : msgs.map((m, i) => (i + 1) + '. ' + m).join('\n');
return { json: {
  draft_id: draftId,
  new_preview: preview,
  new_notes: parsed.notes_for_zayn || '(no notes)',
  customer_phone: entry ? entry.customer_phone : 'unknown',
  customer_name: entry ? entry.customer_name : '',
  customer_message: entry ? entry.customer_message : '',
  telegram_message_id: bp.telegram_message_id,
  admin_chat_id: bp.admin_chat_id
} };
"""

PREP_LEAD = r"""// FR-2: gather the new lead for an outbound first-contact draft
const inp = $('Process Text Reply').item.json;
return { json: {
  systemPrompt: '',
  leadPhone: inp.lead_phone,
  leadText: inp.lead_text || '',
  adminChatId: inp.admin_chat_id || 5532831477
} };
"""

PARSE_LEAD = r"""// FR-2: parse the lead opener draft from Claude
const response = $input.item.json;
const bp = $('Build Lead Prompt').item.json;
let raw = '';
try { raw = response.content[0].text; } catch (e) {
  return { json: {
    messages: ['(empty Claude response - check API node)'],
    notes: 'ERROR: could not extract text from Claude API response',
    parse_status: 'extract_failed',
    customer_phone: bp.leadPhone, customer_name: '', user_message: bp.leadText,
    conversation_history: '', history_count: 0, is_lead: true
  } };
}
let cleaned = raw.trim().replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
let parsed; let status = 'ok';
try {
  parsed = JSON.parse(cleaned);
  if (!Array.isArray(parsed.messages) || !parsed.messages.length) throw new Error('messages array missing or empty');
} catch (e) {
  parsed = { messages: [raw], notes_for_zayn: '(JSON parse failed - plain text. ' + e.message + ')' };
  status = 'fallback_plain_text';
}
const messages = parsed.messages.slice(0, 4).map(m => String(m));
return { json: {
  messages: messages,
  notes: parsed.notes_for_zayn || '(no notes)',
  parse_status: status,
  customer_phone: bp.leadPhone,
  customer_name: '',
  user_message: bp.leadText,
  conversation_history: '',
  history_count: 0,
  is_lead: true
} };
"""

CLAUDE_LEAD_BODY = ('={\n'
  '  "model": "claude-sonnet-4-6",\n'
  '  "max_tokens": 1024,\n'
  '  "system": [\n'
  '    {\n'
  '      "type": "text",\n'
  '      "text": {{ JSON.stringify($json.systemPrompt) }},\n'
  '      "cache_control": {"type": "ephemeral"}\n'
  '    }\n'
  '  ],\n'
  '  "messages": [\n'
  '    {\n'
  '      "role": "user",\n'
  '      "content": {{ JSON.stringify("NEW OUTBOUND LEAD — this customer has NOT messaged us yet; '
  'Zayn is starting the conversation. Draft Maria\'s first-contact WhatsApp opener.\\n\\n'
  'Lead details from Zayn (free-form):\\n" + $json.leadText + "\\n\\n'
  'Write a warm, natural first message that opens the conversation: greet them and move things forward '
  'using the details given. Do not invent facts that were not provided. Return ONLY the JSON object '
  'specified in the system prompt (a messages array, plus notes_for_zayn).") }}\n'
  '    }\n'
  '  ]\n'
  '}')

# ----------------------------------------------------------------- utils --


def die(m):
    sys.exit(f"x {m}")


def load_key():
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            v = ln.split("=", 1)[1].strip()
            if v:
                return v
    die("N8N_API_KEY not in .env")


def resolve_base():
    r = subprocess.run(
        SSH + ["docker inspect n8n-n8n-1 --format "
               "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"],
        capture_output=True, text=True, timeout=30)
    ip = r.stdout.strip()
    if not ip:
        die("could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def ssh_run(script, stdin, label, timeout=120):
    r = subprocess.run(SSH + [script], input=stdin, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        die(f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
    return r.stdout


def ssh_upload(data, remote, label):
    r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        die(f"{label}: upload failed")


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def nid():
    return str(uuid.uuid4())


def asg(name, value, typ="string"):
    return {"id": nid(), "name": name, "value": value, "type": typ}


# --------------------------------------------------------------- surgery --


def main():
    deploy = "--deploy" in sys.argv
    key = load_key()
    global API
    API = resolve_base()
    mode = "DEPLOY" if deploy else "DRY RUN"
    print(f"=== build_fr1_fr2.py  [{mode}] ===")
    print(f"1. n8n API: {API}")

    items = (as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows',
        key, "list"), "list").get("data") or [])
    target = next((w for w in items if w.get("name") == WF_NAME), None)
    if not target:
        die(f"workflow {WF_NAME!r} not found")
    wf_id = target["id"]
    wf = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "get"), "get")
    was_active = bool(wf.get("active"))
    n0 = len(wf["nodes"])
    print(f"2. live workflow {wf_id}: {n0} nodes, active={was_active}")

    nodes = wf["nodes"]
    conns = wf["connections"]

    def find(name):
        n = next((x for x in nodes if x["name"] == name), None)
        if not n:
            die(f"expected node {name!r} not found — workflow differs from expectation")
        return n

    # guard: refuse to run twice
    if any(x["name"] == "Prep Refine" for x in nodes):
        die("'Prep Refine' already present — FR-1/FR-2 looks already applied; aborting")

    # ---- copy the embedded system prompt from the live Build Prompt --------
    bp_node = find("Build Prompt")
    sys_prompt = None
    for a in bp_node["parameters"]["assignments"]["assignments"]:
        if a["name"] == "systemPrompt":
            sys_prompt = a["value"]
    if not sys_prompt or len(sys_prompt) < 5000:
        die("could not read the embedded systemPrompt from Build Prompt")
    print(f"3. embedded systemPrompt: {len(sys_prompt)} chars (reused for new Set nodes)")

    regen_set = find("Build Regen Prompt")
    claude_regen = find("Claude AI (Regen)")
    claude_ai = find("Claude AI")
    edit_regen = find("Edit Telegram (Regen)")
    prep_regen = find("Prep Regen")
    parse_resp = find("Parse Response")
    rta = find("Route Text Action")
    base_pos = rta.get("position", [0, 0])

    # ---- (M1) Process Text Reply: /lead + refine routing ------------------
    find("Process Text Reply")["parameters"]["jsCode"] = PROCESS_TEXT_REPLY

    # ---- (M2) Route Text Action: add 'refine' + 'new_lead' outputs --------
    rules = rta["parameters"]["rules"]["values"]
    if len(rules) != 2:
        die(f"Route Text Action expected 2 rules, found {len(rules)}")
    for key in ("refine", "new_lead"):
        r = copy.deepcopy(rules[0])
        for c in r["conditions"]["conditions"]:
            c["id"] = nid()
            c["rightValue"] = key
        r["outputKey"] = key
        rules.append(r)

    # ---- (M3) Edit Telegram (Edit Prompt): feedback wording ---------------
    ep = find("Edit Telegram (Edit Prompt)")
    jb = ep["parameters"]["jsonBody"]
    jb = jb.replace("✏️ AWAITING YOUR EDIT for ",
                    "✏️ EDIT — tell Claude what to change for ")
    jb = jb.replace("Original draft was:", "Current draft:")
    jb = jb.replace(
        "👉 Reply to this chat with your edited text. Your reply will be sent to the customer.",
        "👉 Reply here with feedback (e.g. shorter, ask his date first, warmer). "
        "Claude will redraft it — nothing is sent to the customer until you tap Send.")
    if "AWAITING YOUR EDIT" in jb:
        die("Edit Telegram (Edit Prompt): old wording not replaced — node differs")
    ep["parameters"]["jsonBody"] = jb

    # ---- (M4) Queue & Format: lead-aware card rendering -------------------
    qf = find("Queue & Format")
    qjs = qf["parameters"]["jsCode"]
    old_lines = ("const lines = ['📩 from ' + pending.customer_phone];\n"
                 "if (pending.customer_name) lines.push('👤 ' + pending.customer_name);\n"
                 "lines.push(histLine, '', 'They said:', '\"' + pending.customer_message + '\"', "
                 "'', '---', draftHeader, preview, '', '📝 Notes: ' + pending.notes);")
    new_lines = ("let lines;\n"
                 "if (input.is_lead) {\n"
                 "  lines = ['🆕 NEW LEAD — outbound first contact', "
                 "'📞 ' + pending.customer_phone, '', 'Lead brief (from you):', "
                 "'\"' + pending.customer_message + '\"', '', '---', draftHeader, preview, "
                 "'', '📝 Notes: ' + pending.notes];\n"
                 "} else {\n"
                 "  lines = ['📩 from ' + pending.customer_phone];\n"
                 "  if (pending.customer_name) lines.push('👤 ' + pending.customer_name);\n"
                 "  lines.push(histLine, '', 'They said:', '\"' + pending.customer_message + '\"', "
                 "'', '---', draftHeader, preview, '', '📝 Notes: ' + pending.notes);\n"
                 "}")
    if old_lines not in qjs:
        die("Queue & Format: expected card-rendering block not found — node differs")
    qf["parameters"]["jsCode"] = qjs.replace(old_lines, new_lines, 1)
    # also stamp is_lead onto the queue entry
    qf["parameters"]["jsCode"] = qf["parameters"]["jsCode"].replace(
        "  status: 'pending'\n};", "  status: 'pending',\n  is_lead: !!input.is_lead\n};", 1)

    # ---- new-node builders -------------------------------------------------
    new_nodes = []

    def add(node, pos):
        node["id"] = nid()
        node["position"] = pos
        new_nodes.append(node)
        return node

    # FR-1: refine chain ----------------------------------------------------
    n = copy.deepcopy(prep_regen)
    n["name"] = "Prep Refine"
    n["parameters"]["jsCode"] = PREP_REFINE
    add(n, [base_pos[0] + 280, base_pos[1] + 220])

    n = copy.deepcopy(regen_set)
    n["name"] = "Build Refine Prompt"
    a = n["parameters"]["assignments"]["assignments"]
    a.append(asg("telegram_message_id", "={{ $json.telegram_message_id }}", "string"))
    a.append(asg("admin_chat_id", "={{ $json.admin_chat_id }}", "number"))
    add(n, [base_pos[0] + 540, base_pos[1] + 220])

    n = copy.deepcopy(claude_regen)
    n["name"] = "Claude AI (Refine)"
    add(n, [base_pos[0] + 800, base_pos[1] + 220])

    n = copy.deepcopy(parse_resp)
    n["name"] = "Parse Refine"
    n["parameters"]["jsCode"] = PARSE_REFINE
    add(n, [base_pos[0] + 1060, base_pos[1] + 220])

    n = copy.deepcopy(edit_regen)
    n["name"] = "Edit Telegram (Refine)"
    eb = n["parameters"]["jsonBody"]
    eb = eb.replace("$('Parse Callback').item.json.chat_id",
                    "$('Parse Refine').item.json.admin_chat_id")
    eb = eb.replace("$('Parse Callback').item.json.message_id",
                    "$('Parse Refine').item.json.telegram_message_id")
    eb = eb.replace("[REGEN]", "[REFINED]")
    if "$('Parse Callback')" in eb:
        die("Edit Telegram (Refine): stray Parse Callback reference remained")
    n["parameters"]["jsonBody"] = eb
    add(n, [base_pos[0] + 1320, base_pos[1] + 220])

    # FR-2: lead chain ------------------------------------------------------
    n = copy.deepcopy(prep_regen)
    n["name"] = "Prep Lead Draft"
    n["parameters"]["jsCode"] = PREP_LEAD
    add(n, [base_pos[0] + 280, base_pos[1] + 420])

    n = copy.deepcopy(regen_set)
    n["name"] = "Build Lead Prompt"
    sp_asg = next(x for x in n["parameters"]["assignments"]["assignments"]
                  if x["name"] == "systemPrompt")
    sp_asg["value"] = sys_prompt
    n["parameters"]["assignments"]["assignments"] = [
        sp_asg,
        asg("leadPhone", "={{ $json.leadPhone }}", "string"),
        asg("leadText", "={{ $json.leadText }}", "string"),
        asg("adminChatId", "={{ $json.adminChatId }}", "number"),
    ]
    add(n, [base_pos[0] + 540, base_pos[1] + 420])

    n = copy.deepcopy(claude_ai)
    n["name"] = "Claude AI (Lead)"
    n["parameters"]["jsonBody"] = CLAUDE_LEAD_BODY
    add(n, [base_pos[0] + 800, base_pos[1] + 420])

    n = copy.deepcopy(parse_resp)
    n["name"] = "Parse Lead Response"
    n["parameters"]["jsCode"] = PARSE_LEAD
    add(n, [base_pos[0] + 1060, base_pos[1] + 420])

    nodes.extend(new_nodes)

    # ---- connections -------------------------------------------------------
    def wire(src, outs):
        conns.setdefault(src, {})["main"] = outs

    def one(node, idx=0):
        return [{"node": node, "type": "main", "index": idx}]

    # Route Text Action: append outputs 2 (refine) + 3 (new_lead)
    rta_main = conns["Route Text Action"]["main"]
    while len(rta_main) < 2:
        rta_main.append([])
    rta_main.append(one("Prep Refine"))
    rta_main.append(one("Prep Lead Draft"))

    # FR-1 refine chain
    wire("Prep Refine", [one("Build Refine Prompt")])
    wire("Build Refine Prompt", [one("Claude AI (Refine)")])
    wire("Claude AI (Refine)", [one("Parse Refine"), one("Build Alert")])
    wire("Parse Refine", [one("Edit Telegram (Refine)")])

    # FR-2 lead chain (re-joins the existing Queue & Format)
    wire("Prep Lead Draft", [one("Build Lead Prompt")])
    wire("Build Lead Prompt", [one("Claude AI (Lead)")])
    wire("Claude AI (Lead)", [one("Parse Lead Response"), one("Build Alert")])
    wire("Parse Lead Response", [one("Queue & Format")])

    # ---- assemble + report -------------------------------------------------
    n1 = len(nodes)
    print(f"4. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}")
    print(f"   modified: Process Text Reply, Route Text Action, "
          f"Edit Telegram (Edit Prompt), Queue & Format")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    staged = Path("/tmp/fr12_staged.json")
    staged.write_text(json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print(f"5. staged workflow -> {staged}")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Inspect /tmp/fr12_staged.json, "
              "then re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR12-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"6. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr12_wf.json", "upload")
    res = as_json(ssh_run(
        f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
        f'-H "Content-Type: application/json" --data-binary @/tmp/fr12_wf.json '
        f'{API}/workflows/{wf_id}; rm -f /tmp/fr12_wf.json', key, "PUT"), "PUT")
    if res.get("id") != wf_id:
        die(f"PUT did not return the workflow: {json.dumps(res)[:400]}")
    print(f"7. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("8. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    have = {x["name"] for x in final["nodes"]}
    need = {x["name"] for x in new_nodes}
    missing = need - have
    print(f"9. VERIFY: {len(final['nodes'])} nodes, active={final.get('active')}, "
          f"new nodes present={not missing}")
    print("FR-1 + FR-2 DEPLOYED." if not missing
          else f"x VERIFY FAILED — missing {missing}; PRE-FR12 backup saved.")


if __name__ == "__main__":
    main()
