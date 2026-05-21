#!/usr/bin/env python3
"""
build_fr4_modecmd.py - FR-4 step 4-B: the /auto and /manual mode commands.

The operator switches a conversation into autonomous mode by replying to one
of its draft cards with /auto, and back with /manual. /manual on its own (or
/manual all) is the global kill switch — every conversation back to approval.

This step is mode plumbing ONLY — pressing /auto sets the conversation's mode
on the bridge; nothing auto-sends yet (that is step 4-C).

    Route Text Action [mode_cmd] -> Hermes Set Mode -> Confirm Mode
                                 -> Send Mode Reply

New nodes (3): Hermes Set Mode, Confirm Mode, Send Mode Reply.
Modified (2): Process Text Reply (the /auto + /manual block), Route Text Action.

Default = DRY RUN (writes /tmp/fr4b_staged.json). Pass --deploy to deploy.

Usage:  python3 scripts/build_fr4_modecmd.py [--deploy]
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

# inserted into Process Text Reply, just before the (D) /rules block
PTR_ANCHOR = "// (D) /rules, /approverule <id>, /discardrule <id> — rule review (FR-5)"
PTR_EBLOCK = r"""// (E) /auto, /manual — switch a conversation's autonomous mode (FR-4)
{
  const low = text.toLowerCase();
  if (low === '/auto' || low === '/manual' || low === '/manual all') {
    let d2 = null;
    const r2 = msg.reply_to_message;
    if (r2 && r2.message_id) {
      for (let _i = queue.length - 1; _i >= 0; _i--) {
        if (queue[_i].telegram_message_id === r2.message_id) { d2 = queue[_i]; break; }
      }
    }
    if (low === '/auto') {
      if (!d2) {
        return { json: { action: 'ack', admin_chat_id: adminChatId,
          ack_text: '⚠️ reply to a customer draft card with /auto to hand that conversation to autonomous mode.' } };
      }
      return { json: { action: 'mode_cmd', mode_target: d2.customer_phone,
        mode: 'autonomous', admin_chat_id: adminChatId } };
    }
    if (low === '/manual all' || !d2) {
      return { json: { action: 'mode_cmd', mode_target: '__ALL__',
        mode: 'approval', admin_chat_id: adminChatId } };
    }
    return { json: { action: 'mode_cmd', mode_target: d2.customer_phone,
      mode: 'approval', admin_chat_id: adminChatId } };
  }
}

"""

CONFIRM_MODE = r"""// FR-4 4-B: confirm an autonomous-mode change to the operator
const r = $input.item.json || {};
const cmd = $('Process Text Reply').item.json;
const adminChatId = cmd.admin_chat_id || 5532831477;
let txt;
if (r.ok && cmd.mode_target === '__ALL__') {
  txt = '🛑 kill switch — every conversation is back to manual approval.';
} else if (r.ok && cmd.mode === 'autonomous') {
  txt = '🤖 autonomous mode ON for ' + (cmd.mode_target || 'this conversation')
    + '.\nclaude will draft and auto-send here — with the safety caps and a '
    + 'review window. reply /manual to a card from this customer to take it back.';
} else if (r.ok) {
  txt = '🙋 back to manual approval for '
    + (cmd.mode_target || 'this conversation') + '.';
} else {
  txt = '⚠️ could not change mode' + (r.error ? ': ' + r.error : '')
    + ' — try again.';
}
return [{ json: { telegram_payload: { chat_id: adminChatId, text: txt } } }];
"""

SETMODE_BODY = ('={ "customer_id": {{ JSON.stringify($json.mode_target) }}, '
                '"mode": {{ JSON.stringify($json.mode) }}, '
                '"activated_by": "operator" }')

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
    print(f"=== build_fr4_modecmd.py  [{'DEPLOY' if deploy else 'DRY RUN'}] ===")
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

    if any(x["name"] == "Hermes Set Mode" for x in nodes):
        die("'Hermes Set Mode' already present — FR-4 4-B looks applied; aborting")

    ptr = find("Process Text Reply")
    rta = find("Route Text Action")
    code_skel = find("Mark Skipped")
    hermes_improve = find("Hermes Improve")
    send_tg = find("Send Draft to Telegram")
    rp = rta.get("position", [0, 0])

    # ---- (M1) Process Text Reply: insert the /auto + /manual block ----------
    js = ptr["parameters"].get("jsCode", "")
    if PTR_ANCHOR not in js:
        die("Process Text Reply: (D) /rules anchor not found — node differs")
    if "/auto" in js and "mode_cmd" in js:
        die("Process Text Reply already has the /auto commands — aborting")
    ptr["parameters"]["jsCode"] = js.replace(PTR_ANCHOR, PTR_EBLOCK + PTR_ANCHOR, 1)

    # ---- (M2) Route Text Action: add the mode_cmd output -------------------
    rules_rule = rta["parameters"]["rules"]["values"]
    base_rule = copy.deepcopy(rules_rule[0])
    for c in base_rule["conditions"]["conditions"]:
        c["id"] = nid()
        c["rightValue"] = "mode_cmd"
    base_rule["outputKey"] = "mode_cmd"
    rules_rule.append(base_rule)

    # ---- new nodes ---------------------------------------------------------
    new_nodes = []

    def add(node, pos):
        node["id"] = nid()
        node["position"] = pos
        if "webhookId" in node:
            node["webhookId"] = nid()
        new_nodes.append(node)
        return node

    n = copy.deepcopy(hermes_improve)
    n["name"] = "Hermes Set Mode"
    n["parameters"]["url"] = BRIDGE + "/set-mode"
    n["parameters"]["jsonBody"] = SETMODE_BODY
    add(n, [rp[0] + 260, rp[1] + 460])

    n = copy.deepcopy(code_skel)
    n["name"] = "Confirm Mode"
    n["parameters"] = {"jsCode": CONFIRM_MODE}
    add(n, [rp[0] + 500, rp[1] + 460])

    n = copy.deepcopy(send_tg)
    n["name"] = "Send Mode Reply"
    add(n, [rp[0] + 740, rp[1] + 460])

    nodes.extend(new_nodes)

    # ---- connections -------------------------------------------------------
    def one(node):
        return [{"node": node, "type": "main", "index": 0}]

    rta_main = conns["Route Text Action"]["main"]
    while len(rta_main) < 6:
        rta_main.append([])
    rta_main[5] = one("Hermes Set Mode")
    # both Hermes Set Mode outputs (ok + error) flow to Confirm Mode
    conns["Hermes Set Mode"] = {"main": [one("Confirm Mode"), one("Confirm Mode")]}
    conns["Confirm Mode"] = {"main": [one("Send Mode Reply")]}

    n1 = len(nodes)
    print(f"3. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}")
    print("   modified: Process Text Reply (/auto + /manual), Route Text Action")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    Path("/tmp/fr4b_staged.json").write_text(
        json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print("4. staged -> /tmp/fr4b_staged.json")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR4B-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr4b_wf.json", "upload")
    res = None
    for attempt in range(1, 4):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/fr4b_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 3:
            print(f"   PUT attempt {attempt}: transient 'unauthorized' — retry in 6s")
            time.sleep(6)
            continue
        ssh_run("rm -f /tmp/fr4b_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/fr4b_wf.json", "", "cleanup")
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
    print("FR-4 4-B DEPLOYED — /auto and /manual switch a conversation's mode."
          if not missing else f"x VERIFY FAILED — missing {missing}; backup saved.")


if __name__ == "__main__":
    main()
