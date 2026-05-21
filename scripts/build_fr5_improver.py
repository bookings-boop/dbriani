#!/usr/bin/env python3
"""
build_fr5_improver.py - Phase 4.2 of the Hermes revival: the background
improvement branch (FR-5).

Adds a branch off `Save Telegram MsgID` (today a dead-end). ~20s after a
draft card is posted, if the draft is still pending, Hermes reviews it via
the bridge /improve endpoint and — only if it can meaningfully improve it —
the card is re-rendered in place with a "✨ improved" marker. One pass per
draft (a multi-pass loop is a later enhancement).

    Save Telegram MsgID → Improve Wait (20s) → Check Pending → Hermes Improve
                        → Apply Improvement → Edit Improved Card

Off the critical path: the fast draft is already posted; this never blocks
a customer reply, and a bridge failure just leaves the draft as-is.

New nodes (5): Improve Wait, Check Pending, Hermes Improve, Apply
Improvement, Edit Improved Card.

Default = DRY RUN (writes /tmp/fr5_staged.json). Pass --deploy to back up
+ PUT to the live workflow.

Usage:  python3 scripts/build_fr5_improver.py [--deploy]
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
IMPROVE_WAIT_SECONDS = 20
BRIDGE_IMPROVE_URL = "http://172.18.0.1:8788/improve"

# ---------------------------------------------------------------- jsCode --

CHECK_PENDING = r"""// FR-5: improve a draft only while it is still pending (unsent)
const sid = $('Save Telegram MsgID').item.json;
const draftId = sid.draft_id;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === draftId);
if (!d || d.status !== 'pending') {
  return [];   // sent / skipped / awaiting_edit / gone — nothing to improve
}
return [{ json: {
  draft_id: draftId,
  telegram_message_id: d.telegram_message_id,
  customer_name: d.customer_name || '',
  customer_id: d.customer_phone || '',
  incoming_message: d.customer_message || '',
  history: d.conversation_history || '',
  current_draft: d.draft_text || (Array.isArray(d.messages) ? d.messages.join('\n\n') : '')
} }];
"""

APPLY_IMPROVEMENT = r"""// FR-5: apply Hermes's improvement to the pending draft + re-render the card
const resp = $input.item.json || {};
const cp = $('Check Pending').item.json;
const draftId = cp.draft_id;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === draftId);
// race guard: the operator may have acted during the ~18s Hermes call
if (!d || d.status !== 'pending') return [];
if (resp.ok !== true || resp.improved !== true) return [];
const msgs = (Array.isArray(resp.messages) && resp.messages.length)
  ? resp.messages.slice(0, 4).map(m => String(m)) : null;
if (!msgs) return [];
d.messages = msgs;
d.draft_text = msgs.join('\n\n');
d.improved_note = resp.note || '';
const preview = msgs.length === 1 ? msgs[0]
  : msgs.map((m, i) => (i + 1) + '. ' + m).join('\n');
const draftHeader = msgs.length === 1 ? '💬 DRAFT:'
  : ('💬 DRAFT (' + msgs.length + ' messages):');
const lines = [];
if (d.is_lead) {
  lines.push('🆕 NEW LEAD — outbound first contact', '📞 ' + d.customer_phone,
             '', 'Lead brief (from you):', '"' + d.customer_message + '"');
} else {
  lines.push('📩 from ' + d.customer_phone);
  if (d.customer_name) lines.push('👤 ' + d.customer_name);
  lines.push('', 'They said:', '"' + d.customer_message + '"');
}
lines.push('', '---', '✨ improved by Hermes', draftHeader, preview,
           '', '📝 Notes: ' + (d.notes || ''));
const reply_markup = { inline_keyboard: [
  [{ text: '✅ Send', callback_data: 'send:' + draftId },
   { text: '✏️ Edit', callback_data: 'edit:' + draftId }],
  [{ text: '🔁 Regen', callback_data: 'regen:' + draftId },
   { text: '❌ Skip', callback_data: 'skip:' + draftId }]
] };
return [{ json: {
  chat_id: d.telegram_chat_id || 5532831477,
  message_id: cp.telegram_message_id,
  text: lines.join('\n'),
  reply_markup: reply_markup
} }];
"""

IMPROVE_BODY = ('={ "customer_name": {{ JSON.stringify($json.customer_name) }}, '
                '"customer_id": {{ JSON.stringify($json.customer_id) }}, '
                '"incoming_message": {{ JSON.stringify($json.incoming_message) }}, '
                '"history": {{ JSON.stringify($json.history) }}, '
                '"current_draft": {{ JSON.stringify($json.current_draft) }} }')

EDIT_CARD_BODY = ('={ "chat_id": {{ $json.chat_id }}, '
                  '"message_id": {{ $json.message_id }}, '
                  '"text": {{ JSON.stringify($json.text) }}, '
                  '"reply_markup": {{ JSON.stringify($json.reply_markup) }} }')

# ----------------------------------------------------------------- utils --


def die(m):
    sys.exit(f"x {m}")


def load_key():
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            v = ln.split("=", 1)[1].strip().strip("'\"")
            if v:
                return v
    die("N8N_API_KEY not in .env")


def ssh_run(script, stdin, label, timeout=60):
    last = ""
    for attempt in range(1, 6):
        r = None
        try:
            r = subprocess.run(SSH + [script], input=stdin, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        if r is not None:
            if r.returncode == 0:
                return r.stdout
            if r.returncode != 255:
                die(f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
            last = f"ssh exit 255 ({r.stderr.strip()[:100]})"
        if attempt < 5:
            print(f"   [{label}] connect attempt {attempt} failed: {last} — retry 5s")
            time.sleep(5)
    die(f"{label}: connection failed after 5 attempts — {last}")


def ssh_upload(data, remote, label):
    last = ""
    for attempt in range(1, 6):
        r = None
        try:
            r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                               capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            last = "timed out"
        if r is not None:
            if r.returncode == 0:
                return
            last = f"exit {r.returncode}"
        if attempt < 5:
            print(f"   [{label}] upload attempt {attempt} failed: {last} — retry 5s")
            time.sleep(5)
    die(f"{label}: upload failed after 5 attempts — {last}")


def resolve_base():
    out = ssh_run("docker inspect n8n-n8n-1 --format "
                  "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'",
                  "", "resolve-ip", timeout=45)
    ip = out.strip()
    if not ip:
        die("could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def nid():
    return str(uuid.uuid4())


# --------------------------------------------------------------- surgery --


def main():
    deploy = "--deploy" in sys.argv
    key = load_key()
    global API
    API = resolve_base()
    print(f"=== build_fr5_improver.py  [{'DEPLOY' if deploy else 'DRY RUN'}] ===")
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
            die(f"expected node {name!r} not found")
        return n

    if any(x["name"] == "Hermes Improve" for x in nodes):
        die("'Hermes Improve' already present — FR-5 improver looks applied; aborting")

    save_msgid = find("Save Telegram MsgID")
    wait_node = find("Wait")
    code_skel = find("Mark Skipped")
    claude = find("Claude AI")           # httpRequest w/ httpHeaderAuth credential
    edit_regen = find("Edit Telegram (Regen)")
    sp = save_msgid.get("position", [0, 0])

    new_nodes = []

    def add(node, pos):
        node["id"] = nid()
        node["position"] = pos
        if "webhookId" in node:
            node["webhookId"] = nid()
        new_nodes.append(node)
        return node

    # Improve Wait — 20s
    n = copy.deepcopy(wait_node)
    n["name"] = "Improve Wait"
    n["parameters"] = dict(n.get("parameters", {}))
    n["parameters"]["amount"] = IMPROVE_WAIT_SECONDS
    add(n, [sp[0] + 200, sp[1] + 200])

    # Check Pending — code
    n = copy.deepcopy(code_skel)
    n["name"] = "Check Pending"
    n["parameters"] = {"jsCode": CHECK_PENDING}
    add(n, [sp[0] + 400, sp[1] + 200])

    # Hermes Improve — httpRequest -> bridge /improve, "Hermes Bridge" credential
    n = copy.deepcopy(claude)
    n["name"] = "Hermes Improve"
    p = n["parameters"]
    p["url"] = BRIDGE_IMPROVE_URL
    p["jsonBody"] = IMPROVE_BODY
    p["genericAuthType"] = "httpHeaderAuth"
    p["authentication"] = "genericCredentialType"
    if p.get("sendHeaders"):
        p["headerParameters"] = {"parameters": [
            {"name": "Content-Type", "value": "application/json"}]}
    n["credentials"] = {"httpHeaderAuth": {"id": None, "name": "Hermes Bridge"}}
    add(n, [sp[0] + 600, sp[1] + 200])

    # Apply Improvement — code
    n = copy.deepcopy(code_skel)
    n["name"] = "Apply Improvement"
    n["parameters"] = {"jsCode": APPLY_IMPROVEMENT}
    add(n, [sp[0] + 800, sp[1] + 200])

    # Edit Improved Card — httpRequest -> Telegram editMessageText
    n = copy.deepcopy(edit_regen)
    n["name"] = "Edit Improved Card"
    n["parameters"]["jsonBody"] = EDIT_CARD_BODY
    add(n, [sp[0] + 1000, sp[1] + 200])

    nodes.extend(new_nodes)

    # connections — linear branch; Hermes Improve error output left unwired
    def one(node):
        return [{"node": node, "type": "main", "index": 0}]

    conns.setdefault("Save Telegram MsgID", {})["main"] = [one("Improve Wait")]
    conns["Improve Wait"] = {"main": [one("Check Pending")]}
    conns["Check Pending"] = {"main": [one("Hermes Improve")]}
    conns["Hermes Improve"] = {"main": [one("Apply Improvement"), []]}
    conns["Apply Improvement"] = {"main": [one("Edit Improved Card")]}

    n1 = len(nodes)
    print(f"3. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}  "
          f"(wait: {IMPROVE_WAIT_SECONDS}s)")
    print("   branch: Save Telegram MsgID -> Improve Wait -> Check Pending "
          "-> Hermes Improve -> Apply Improvement -> Edit Improved Card")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    Path("/tmp/fr5_staged.json").write_text(
        json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print("4. staged -> /tmp/fr5_staged.json")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR5-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr5_wf.json", "upload")
    res = None
    for attempt in range(1, 4):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/fr5_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 3:
            print(f"   PUT attempt {attempt}: transient 'unauthorized' — retry in 6s")
            time.sleep(6)
            continue
        ssh_run("rm -f /tmp/fr5_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/fr5_wf.json", "", "cleanup")
    print(f"6. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("7. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    have = {x["name"] for x in final["nodes"]}
    need = {"Improve Wait", "Check Pending", "Hermes Improve", "Apply Improvement",
            "Edit Improved Card"}
    missing = need - have
    print(f"8. VERIFY: {len(final['nodes'])} nodes, active={final.get('active')}, "
          f"new nodes present={not missing}")
    print("FR-5 IMPROVER DEPLOYED — pending drafts now improved in the background."
          if not missing else f"x VERIFY FAILED — missing {missing}; backup saved.")


if __name__ == "__main__":
    main()
