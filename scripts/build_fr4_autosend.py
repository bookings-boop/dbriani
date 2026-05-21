#!/usr/bin/env python3
"""
build_fr4_autosend.py - FR-4 step 4-C: the autonomous-send branch.

A second branch off `Save Telegram MsgID` (parallel to the FR-5 improver).
For a conversation the operator has put on /auto, it renders an Auto card,
waits a randomised 1-5 min window, re-checks that the operator has not
intervened, re-checks the caps, and then auto-sends the draft via WAHA.

    Save Telegram MsgID
      → Auto Prep          read the draft + a random 60-300s delay
      → Auto Gate          bridge /autosend-check {commit:false}
      → Auto Is Autonomous stop unless mode == autonomous
      → Render Auto Card   edit the card → "AUTO — sends in ~N min"
      → Auto Wait          random 1-5 min
      → Auto Decide        stop unless the draft is STILL pending (operator wins)
      → Auto Commit        bridge /autosend-check {commit:true} — caps + log
      → Auto Send Gate     stop unless caps cleared (auto_send==true)
      → Auto Send WAHA     send the draft to the customer
      → Mark Auto Sent     mark the draft sent
      → Edit Auto-Sent Card  card → "auto-sent ✓"

DORMANT until a conversation is on /auto. The autonomous-send only fires for
an opted-in conversation; operator Send/Skip/Edit during the window always
wins (Auto Decide re-checks `pending`). Behavioural test = step 4-D, on a
controlled test number, before any real customer goes on /auto.

Default = DRY RUN (writes /tmp/fr4c_staged.json). Pass --deploy to deploy.

Usage:  python3 scripts/build_fr4_autosend.py [--deploy]
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
BRIDGE = "http://172.18.0.1:8788"
ADMIN_CHAT = "5532831477"

# ---------------------------------------------------------------- jsCode --

AUTO_PREP = r"""// FR-4 4-C: prep the autonomous-send branch for a freshly-posted draft
const sid = $('Save Telegram MsgID').item.json;
const draftId = sid.draft_id;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === draftId);
if (!d) return [];
const delay = 60 + Math.floor(Math.random() * 241);   // 60-300s, human-like
const mins = Math.max(1, Math.round(delay / 60));
const preview = (Array.isArray(d.messages) && d.messages.length > 1)
  ? d.messages.map((m, i) => (i + 1) + '. ' + m).join('\n')
  : (d.draft_text || '');
const lines = [
  '🤖 AUTO — auto-sends in ~' + mins + ' min unless you act',
  '(reply /manual here to stop · or ✏️ Edit · ❌ Skip)', '',
  '📩 ' + (d.customer_name || d.customer_phone), '',
  'they said: "' + (d.customer_message || '') + '"', '',
  '---', '💬 will send:', preview];
const reply_markup = { inline_keyboard: [
  [{ text: '✅ Send now', callback_data: 'send:' + draftId },
   { text: '✏️ Edit', callback_data: 'edit:' + draftId }],
  [{ text: '🔁 Regen', callback_data: 'regen:' + draftId },
   { text: '❌ Skip', callback_data: 'skip:' + draftId }]
] };
return [{ json: {
  draft_id: draftId,
  customer_id: d.customer_phone || '',
  customer_phone: d.customer_phone || '',
  telegram_message_id: d.telegram_message_id,
  auto_delay_s: delay,
  auto_card_text: lines.join('\n'),
  auto_reply_markup: reply_markup
} }];
"""

AUTO_IS_AUTONOMOUS = r"""// FR-4 4-C: continue only if the bridge says this conversation is autonomous
const r = $input.item.json || {};
if ((r.mode || '') !== 'autonomous') return [];   // manual conversation — stop
return [{ json: r }];
"""

AUTO_DECIDE = r"""// FR-4 4-C: after the delay — proceed only if the draft is STILL pending
const prep = $('Auto Prep').item.json;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === prep.draft_id);
// operator wins: a Send / Skip / Edit during the window abandons the auto-send
if (!d || d.status !== 'pending') return [];
return [{ json: {
  draft_id: prep.draft_id,
  customer_id: prep.customer_id,
  customer_phone: d.customer_phone || prep.customer_phone,
  telegram_message_id: prep.telegram_message_id,
  customer_name: d.customer_name || '',
  draft_text: d.draft_text
    || (Array.isArray(d.messages) ? d.messages.join('\n\n') : '')
} }];
"""

AUTO_SEND_GATE = r"""// FR-4 4-C: send only if the caps cleared (bridge /autosend-check commit:true)
const r = $input.item.json || {};
if (r.auto_send !== true) return [];   // caps blocked / checkpoint — leave for operator
return [{ json: r }];
"""

MARK_AUTO_SENT = r"""// FR-4 4-C: mark the draft auto-sent
const dec = $('Auto Decide').item.json;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === dec.draft_id);
if (d) {
  d.status = 'sent';
  d.messages_sent_count = Array.isArray(d.messages) ? d.messages.length : 1;
  d.auto_sent = true;
}
return [{ json: {
  draft_id: dec.draft_id,
  customer_phone: dec.customer_phone,
  customer_name: dec.customer_name,
  telegram_message_id: dec.telegram_message_id,
  draft_text: dec.draft_text
} }];
"""

GATE_BODY = ('={ "customer_id": {{ JSON.stringify($json.customer_id) }}, '
             '"commit": false }')
COMMIT_BODY = ('={ "customer_id": {{ JSON.stringify($json.customer_id) }}, '
               '"commit": true }')
RENDER_CARD_BODY = (
    '={ "chat_id": ' + ADMIN_CHAT + ', '
    '"message_id": {{ $(\'Auto Prep\').item.json.telegram_message_id }}, '
    '"text": {{ JSON.stringify($(\'Auto Prep\').item.json.auto_card_text) }}, '
    '"reply_markup": {{ JSON.stringify($(\'Auto Prep\').item.json.auto_reply_markup) }} }')
WAHA_BODY = ('={ "session": "default", '
             '"chatId": {{ JSON.stringify($(\'Auto Decide\').item.json.customer_phone) }}, '
             '"text": {{ JSON.stringify($(\'Auto Decide\').item.json.draft_text) }} }')
SENT_CARD_BODY = (
    '={ "chat_id": ' + ADMIN_CHAT + ', '
    '"message_id": {{ $(\'Mark Auto Sent\').item.json.telegram_message_id }}, '
    '"text": {{ JSON.stringify("🤖 auto-sent to " + '
    '($(\'Mark Auto Sent\').item.json.customer_name || '
    '$(\'Mark Auto Sent\').item.json.customer_phone) + " ✓\\n\\n" + '
    '$(\'Mark Auto Sent\').item.json.draft_text) }} }')

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
    for attempt in range(1, 11):
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
        if attempt < 10:
            print(f"   [{label}] connect attempt {attempt} failed: {last} — retry 5s")
            time.sleep(5)
    die(f"{label}: connection failed after 10 attempts — {last}")


def ssh_upload(data, remote, label):
    last = ""
    for attempt in range(1, 11):
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
        if attempt < 10:
            print(f"   [{label}] upload attempt {attempt} failed: {last} — retry 5s")
            time.sleep(5)
    die(f"{label}: upload failed after 10 attempts — {last}")


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
    print(f"=== build_fr4_autosend.py  [{'DEPLOY' if deploy else 'DRY RUN'}] ===")
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

    if any(x["name"] == "Auto Prep" for x in nodes):
        die("'Auto Prep' already present — FR-4 4-C looks applied; aborting")

    save_msgid = find("Save Telegram MsgID")
    code_skel = find("Mark Skipped")
    hermes_improve = find("Hermes Improve")        # bridge httpRequest
    edit_regen = find("Edit Telegram (Regen)")     # Telegram editMessageText
    waha = find("Send One to Customer")            # WAHA sendText
    wait_node = find("Wait")
    sp = save_msgid.get("position", [0, 0])

    new_nodes = []

    def add(node, pos):
        node["id"] = nid()
        node["position"] = pos
        if "webhookId" in node:
            node["webhookId"] = nid()
        new_nodes.append(node)
        return node

    def code_node(name, js):
        n = copy.deepcopy(code_skel)
        n["name"] = name
        n["parameters"] = {"jsCode": js}
        return n

    def bridge_node(name, body):
        n = copy.deepcopy(hermes_improve)
        n["name"] = name
        n["parameters"]["url"] = BRIDGE + "/autosend-check"
        n["parameters"]["jsonBody"] = body
        return n

    def tg_edit_node(name, body):
        n = copy.deepcopy(edit_regen)
        n["name"] = name
        n["parameters"]["jsonBody"] = body
        return n

    x, y = sp[0], sp[1] + 360
    add(code_node("Auto Prep", AUTO_PREP), [x + 220, y])
    add(bridge_node("Auto Gate", GATE_BODY), [x + 440, y])
    add(code_node("Auto Is Autonomous", AUTO_IS_AUTONOMOUS), [x + 660, y])
    add(tg_edit_node("Render Auto Card", RENDER_CARD_BODY), [x + 880, y])
    n = copy.deepcopy(wait_node)
    n["name"] = "Auto Wait"
    n["parameters"] = dict(n.get("parameters", {}))
    n["parameters"]["amount"] = "={{ $('Auto Prep').item.json.auto_delay_s }}"
    n["parameters"]["unit"] = "seconds"
    add(n, [x + 1100, y])
    add(code_node("Auto Decide", AUTO_DECIDE), [x + 1320, y])
    add(bridge_node("Auto Commit", COMMIT_BODY), [x + 1540, y])
    add(code_node("Auto Send Gate", AUTO_SEND_GATE), [x + 1760, y])
    n = copy.deepcopy(waha)
    n["name"] = "Auto Send WAHA"
    n["parameters"]["jsonBody"] = WAHA_BODY
    add(n, [x + 1980, y])
    add(code_node("Mark Auto Sent", MARK_AUTO_SENT), [x + 2200, y])
    add(tg_edit_node("Edit Auto-Sent Card", SENT_CARD_BODY), [x + 2420, y])

    nodes.extend(new_nodes)

    # ---- connections -------------------------------------------------------
    def one(node):
        return [{"node": node, "type": "main", "index": 0}]

    # Save Telegram MsgID main[0] -> [Improve Wait (existing), Auto Prep (new)]
    stm = conns.setdefault("Save Telegram MsgID", {}).setdefault("main", [[]])
    if not stm or not isinstance(stm[0], list):
        stm[:] = [[]]
    stm[0].append({"node": "Auto Prep", "type": "main", "index": 0})

    conns["Auto Prep"] = {"main": [one("Auto Gate")]}
    conns["Auto Gate"] = {"main": [one("Auto Is Autonomous"), []]}   # err -> unwired
    conns["Auto Is Autonomous"] = {"main": [one("Render Auto Card")]}
    conns["Render Auto Card"] = {"main": [one("Auto Wait")]}
    conns["Auto Wait"] = {"main": [one("Auto Decide")]}
    conns["Auto Decide"] = {"main": [one("Auto Commit")]}
    conns["Auto Commit"] = {"main": [one("Auto Send Gate"), []]}     # err -> unwired
    conns["Auto Send Gate"] = {"main": [one("Auto Send WAHA")]}
    conns["Auto Send WAHA"] = {"main": [one("Mark Auto Sent"), []]}  # err -> unwired
    conns["Mark Auto Sent"] = {"main": [one("Edit Auto-Sent Card")]}

    n1 = len(nodes)
    print(f"3. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}")
    print("   branch: Save Telegram MsgID -> Auto Prep -> ... -> Auto Send WAHA "
          "-> Mark Auto Sent -> Edit Auto-Sent Card")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    Path("/tmp/fr4c_staged.json").write_text(
        json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print("4. staged -> /tmp/fr4c_staged.json")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR4C-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr4c_wf.json", "upload")
    res = None
    for attempt in range(1, 4):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/fr4c_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 3:
            print(f"   PUT attempt {attempt}: transient 'unauthorized' — retry in 6s")
            time.sleep(6)
            continue
        ssh_run("rm -f /tmp/fr4c_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/fr4c_wf.json", "", "cleanup")
    print(f"6. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("7. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    have = {x["name"] for x in final["nodes"]}
    missing = {x["name"] for x in new_nodes} - have
    print(f"8. VERIFY: {len(final['nodes'])} nodes, active={final.get('active')}, "
          f"new nodes present={not missing}")
    print("FR-4 4-C DEPLOYED — autonomous-send branch live (DORMANT until a "
          "conversation is on /auto). Run the 4-D test before real use."
          if not missing else f"x VERIFY FAILED — missing {missing}; backup saved.")


if __name__ == "__main__":
    main()
