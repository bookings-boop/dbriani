#!/usr/bin/env python3
"""
build_fr4_button.py - FR-4 4-B (v2): the 🤖 Auto button.

Replaces the /auto command (reply-to-card matching, unreliable) with a 5th
button on the draft card. A button press carries the draft id in its callback
data, so it never depends on Telegram reply-matching.

  * adds a 🤖 Auto button to all four card renders (Queue & Format,
    Apply Improvement, Edit Telegram (Regen), Edit Telegram (Refine))
  * Route Action gains `auto` and `takeover` callback outputs
  * auto:     Set Auto Mode (bridge /set-mode -> autonomous) -> Render Auto
              Confirm (card -> 🤖 AUTO ON, with a 🙋 Take over button)
  * takeover: Set Manual Mode (bridge /set-mode -> approval) -> Render Manual
              Confirm (card back to normal, with the 🤖 Auto button)

The /manual command (4-B v1) stays as the global kill switch. This step is
mode plumbing — nothing auto-sends (that is 4-C, build_fr4_autosend.py).

Default = DRY RUN (writes /tmp/fr4btn_staged.json). Pass --deploy to deploy.

Usage:  python3 scripts/build_fr4_button.py [--deploy]
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

# ---- reply_markup edits: add a 🤖 Auto row to each of the 4 card renders --
QF_OLD = ("    [{ text: '🔁 Regen', callback_data: 'regen:' + id }, "
          "{ text: '❌ Skip', callback_data: 'skip:' + id }]\n  ]")
QF_NEW = ("    [{ text: '🔁 Regen', callback_data: 'regen:' + id }, "
          "{ text: '❌ Skip', callback_data: 'skip:' + id }],\n"
          "    [{ text: '🤖 Auto', callback_data: 'auto:' + id }]\n  ]")

AI_OLD = ("  [{ text: '🔁 Regen', callback_data: 'regen:' + draftId },\n"
          "   { text: '❌ Skip', callback_data: 'skip:' + draftId }]\n] };")
AI_NEW = ("  [{ text: '🔁 Regen', callback_data: 'regen:' + draftId },\n"
          "   { text: '❌ Skip', callback_data: 'skip:' + draftId }],\n"
          "  [{ text: '🤖 Auto', callback_data: 'auto:' + draftId }]\n] };")

# Edit Telegram (Regen) and (Refine) share this exact jsonBody fragment
TG_OLD = ('{text: "❌ Skip", callback_data: "skip:" + $json.draft_id}]]')
TG_NEW = ('{text: "❌ Skip", callback_data: "skip:" + $json.draft_id}], '
          '[{text: "🤖 Auto", callback_data: "auto:" + $json.draft_id}]]')

PC = "$('Parse Callback').item.json"
SET_AUTO_BODY = ('={ "customer_id": ' + f'{{{{ JSON.stringify({PC}.draft.customer_phone) }}}}'
                 + ', "mode": "autonomous", "activated_by": "operator" }')
SET_MANUAL_BODY = ('={ "customer_id": ' + f'{{{{ JSON.stringify({PC}.draft.customer_phone) }}}}'
                   + ', "mode": "approval", "activated_by": "operator" }')

AUTO_CONFIRM_BODY = (
    '={\n'
    f'  "chat_id": {{{{ {PC}.chat_id }}}},\n'
    f'  "message_id": {{{{ {PC}.message_id }}}},\n'
    '  "text": {{ JSON.stringify("🤖 AUTONOMOUS MODE ON — claude will draft and '
    'auto-send for " + ' + f'{PC}.draft.customer_phone' + ' + " (safety caps + a '
    '~1-5 min review window). press 🙋 Take over to return it to you.\\n\\n'
    '💬 current draft:\\n" + ' + f'{PC}.draft.draft_text) }}}},\n'
    '  "reply_markup": {{ JSON.stringify({inline_keyboard: ['
    f'[{{text:"✅ Send now",callback_data:"send:"+{PC}.draft_id}},'
    f'{{text:"✏️ Edit",callback_data:"edit:"+{PC}.draft_id}}],'
    f'[{{text:"🔁 Regen",callback_data:"regen:"+{PC}.draft_id}},'
    f'{{text:"❌ Skip",callback_data:"skip:"+{PC}.draft_id}}],'
    f'[{{text:"🙋 Take over",callback_data:"takeover:"+{PC}.draft_id}}]'
    ']}) }}\n}')

MANUAL_CONFIRM_BODY = (
    '={\n'
    f'  "chat_id": {{{{ {PC}.chat_id }}}},\n'
    f'  "message_id": {{{{ {PC}.message_id }}}},\n'
    '  "text": {{ JSON.stringify("🙋 back to manual approval for " + '
    f'{PC}.draft.customer_phone' + ' + " — every reply needs your Send again.'
    '\\n\\n💬 current draft:\\n" + ' + f'{PC}.draft.draft_text) }}}},\n'
    '  "reply_markup": {{ JSON.stringify({inline_keyboard: ['
    f'[{{text:"✅ Send",callback_data:"send:"+{PC}.draft_id}},'
    f'{{text:"✏️ Edit",callback_data:"edit:"+{PC}.draft_id}}],'
    f'[{{text:"🔁 Regen",callback_data:"regen:"+{PC}.draft_id}},'
    f'{{text:"❌ Skip",callback_data:"skip:"+{PC}.draft_id}}],'
    f'[{{text:"🤖 Auto",callback_data:"auto:"+{PC}.draft_id}}]'
    ']}) }}\n}')

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
    print(f"=== build_fr4_button.py  [{'DEPLOY' if deploy else 'DRY RUN'}] ===")
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

    if any(x["name"] == "Set Auto Mode" for x in nodes):
        die("'Set Auto Mode' already present — FR-4 button looks applied; aborting")

    # ---- (M1-M4) add the 🤖 Auto button to the four card renders -----------
    def patch_code(name, old, new):
        node = find(name)
        js = node["parameters"].get("jsCode", "")
        if "auto:" in js:
            print(f"   {name}: already has the Auto button — skipping")
            return
        if old not in js:
            die(f"{name}: reply_markup anchor not found — node differs")
        node["parameters"]["jsCode"] = js.replace(old, new, 1)
        print(f"   {name}: Auto button added")

    def patch_body(name):
        node = find(name)
        jb = node["parameters"].get("jsonBody", "")
        if "auto:" in jb:
            print(f"   {name}: already has the Auto button — skipping")
            return
        if TG_OLD not in jb:
            die(f"{name}: reply_markup anchor not found — node differs")
        node["parameters"]["jsonBody"] = jb.replace(TG_OLD, TG_NEW, 1)
        print(f"   {name}: Auto button added")

    print("3. adding the 🤖 Auto button to the card renders:")
    patch_code("Queue & Format", QF_OLD, QF_NEW)
    patch_code("Apply Improvement", AI_OLD, AI_NEW)
    patch_body("Edit Telegram (Regen)")
    patch_body("Edit Telegram (Refine)")

    # ---- (M5) Route Action: add `auto` and `takeover` outputs --------------
    rta = find("Route Action")
    rvals = rta["parameters"]["rules"]["values"]
    for key in ("auto", "takeover"):
        rule = copy.deepcopy(rvals[0])
        for c in rule["conditions"]["conditions"]:
            c["id"] = nid()
            c["rightValue"] = key
        rule["outputKey"] = key
        rvals.append(rule)
    print("4. Route Action: + auto, + takeover outputs")

    # ---- new handler nodes -------------------------------------------------
    hermes_improve = find("Hermes Improve")        # bridge httpRequest
    edit_regen = find("Edit Telegram (Regen)")     # Telegram editMessageText
    rap = find("Route Action").get("position", [0, 0])
    new_nodes = []

    def add(node, pos):
        node["id"] = nid()
        node["position"] = pos
        if "webhookId" in node:
            node["webhookId"] = nid()
        new_nodes.append(node)
        return node

    def bridge_node(name, body):
        n = copy.deepcopy(hermes_improve)
        n["name"] = name
        n["parameters"]["url"] = BRIDGE + "/set-mode"
        n["parameters"]["jsonBody"] = body
        return n

    def edit_node(name, body):
        n = copy.deepcopy(edit_regen)
        n["name"] = name
        n["parameters"]["jsonBody"] = body
        return n

    add(bridge_node("Set Auto Mode", SET_AUTO_BODY), [rap[0] + 260, rap[1] + 420])
    add(edit_node("Render Auto Confirm", AUTO_CONFIRM_BODY),
        [rap[0] + 480, rap[1] + 420])
    add(bridge_node("Set Manual Mode", SET_MANUAL_BODY),
        [rap[0] + 260, rap[1] + 600])
    add(edit_node("Render Manual Confirm", MANUAL_CONFIRM_BODY),
        [rap[0] + 480, rap[1] + 600])
    nodes.extend(new_nodes)

    # ---- connections -------------------------------------------------------
    def one(node):
        return [{"node": node, "type": "main", "index": 0}]

    ra_main = conns["Route Action"]["main"]
    while len(ra_main) < 6:
        ra_main.append([])
    ra_main[4] = one("Set Auto Mode")
    ra_main[5] = one("Set Manual Mode")
    conns["Set Auto Mode"] = {"main": [one("Render Auto Confirm"), []]}
    conns["Set Manual Mode"] = {"main": [one("Render Manual Confirm"), []]}

    n1 = len(nodes)
    print(f"5. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    Path("/tmp/fr4btn_staged.json").write_text(
        json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print("6. staged -> /tmp/fr4btn_staged.json")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR4BTN-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"7. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr4btn_wf.json", "upload")
    res = None
    for attempt in range(1, 4):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/fr4btn_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 3:
            print(f"   PUT attempt {attempt}: transient 'unauthorized' — retry in 6s")
            time.sleep(6)
            continue
        ssh_run("rm -f /tmp/fr4btn_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/fr4btn_wf.json", "", "cleanup")
    print(f"8. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("9. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    have = {x["name"] for x in final["nodes"]}
    missing = {x["name"] for x in new_nodes} - have
    print(f"10. VERIFY: {len(final['nodes'])} nodes, active={final.get('active')}, "
          f"new nodes present={not missing}")
    print("FR-4 BUTTON DEPLOYED — 🤖 Auto on the draft cards switches a "
          "conversation autonomous." if not missing
          else f"x VERIFY FAILED — missing {missing}; backup saved.")


if __name__ == "__main__":
    main()
