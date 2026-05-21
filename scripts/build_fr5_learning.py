#!/usr/bin/env python3
"""
build_fr5_learning.py - Phase 4.3b of the Hermes revival: the learning loop's
n8n side.

Two parts:
  Part 1 - learn from Edit feedback. After the FR-1 refine path renders the
    refined card, the operator's feedback is sent to the bridge /learn, which
    judges whether it is a durable rule and (if so) captures it inactive. If a
    rule is captured the operator gets a Telegram notice.
        Edit Telegram (Refine) -> Prep Learn -> Hermes Learn
                               -> Notify Rule Captured -> Send Rule Notice
  Part 2 - /rules review commands. /rules lists pending rules; /approverule
    <id> and /discardrule <id> act on one — all via the bridge /rules.
        Route Text Action [rules_cmd] -> Hermes Rules -> Format Rules Reply
                                       -> Send Rules Reply

New nodes (7): Prep Learn, Hermes Learn, Notify Rule Captured, Send Rule
Notice, Hermes Rules, Format Rules Reply, Send Rules Reply.
Modified (2): Process Text Reply (the /rules commands), Route Text Action.

Default = DRY RUN (writes /tmp/fr5l_staged.json). Pass --deploy to deploy.

Usage:  python3 scripts/build_fr5_learning.py [--deploy]
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

# ---------------------------------------------------------------- jsCode --

# inserted into Process Text Reply, just before the (edit) refine block
PTR_ANCHOR = "// (edit) a draft is awaiting feedback -> refine it, do NOT send (FR-1)"
PTR_DBLOCK = r"""// (D) /rules, /approverule <id>, /discardrule <id> — rule review (FR-5)
{
  const low = text.toLowerCase();
  if (low === '/rules' || low.indexOf('/rules ') === 0) {
    return { json: { action: 'rules_cmd', rules_action: 'list', rule_id: null,
      admin_chat_id: adminChatId } };
  }
  if (low.indexOf('/approverule ') === 0 || low.indexOf('/discardrule ') === 0) {
    const isApprove = low.indexOf('/approverule ') === 0;
    const rid = (text.split(/\s+/)[1] || '').trim();
    if (!/^\d+$/.test(rid)) {
      return { json: { action: 'ack', admin_chat_id: adminChatId,
        ack_text: 'Usage:  ' + (isApprove ? '/approverule' : '/discardrule')
          + ' <rule id number>' } };
    }
    return { json: { action: 'rules_cmd',
      rules_action: isApprove ? 'activate' : 'discard', rid_text: rid,
      rule_id: rid, admin_chat_id: adminChatId } };
  }
}

"""

PREP_LEARN = r"""// FR-5 4.3b: assemble the /learn payload from the operator's Edit feedback
const ptr = $('Process Text Reply').item.json;
const draftId = ptr.draft_id;
const data = $getWorkflowStaticData('global');
const d = (data.pendingQueue || []).find(x => x.id === draftId) || {};
return [{ json: {
  feedback: ptr.feedback || '',
  draft_text: d.draft_text || '',
  customer_id: d.customer_phone || '',
  customer_name: d.customer_name || '',
  admin_chat_id: ptr.admin_chat_id || 5532831477
} }];
"""

NOTIFY_RULE_CAPTURED = r"""// FR-5 4.3b: notify the operator only when /learn actually captured a rule
const r = $input.item.json || {};
const adminChatId = $('Prep Learn').item.json.admin_chat_id || 5532831477;
if (!r.ok || !r.captured || !r.rule_text) {
  return [];   // one-off feedback, or learn errored — no notice
}
const txt = '📚 learned a possible rule from your edit:\n\n"' + r.rule_text + '"\n\n'
  + '(' + (r.scope || 'global') + ' rule · id ' + r.rule_id
  + ' · inactive until you approve)\n\n'
  + 'review all:  /rules\napprove this:  /approverule ' + r.rule_id;
return [{ json: { telegram_payload: { chat_id: adminChatId, text: txt } } }];
"""

FORMAT_RULES_REPLY = r"""// FR-5 4.3b: format the bridge /rules response into a Telegram reply
const r = $input.item.json || {};
const adminChatId = $('Process Text Reply').item.json.admin_chat_id || 5532831477;
let txt;
if (r.ok && Array.isArray(r.pending)) {
  if (!r.pending.length) {
    txt = '📚 no behaviour rules pending review.';
  } else {
    txt = '📚 rules pending your approval (' + r.pending.length + '):\n\n'
      + r.pending.map(function (x) {
          return '• [' + x.id + '] (' + x.scope + ') ' + x.rule_text;
        }).join('\n\n')
      + '\n\napprove:  /approverule <id>\ndiscard:  /discardrule <id>';
  }
} else if (r.ok && r.action) {
  txt = r.action === 'activate'
    ? '✅ rule ' + r.rule_id + ' activated — it will now shape drafts.'
    : '🗑 rule ' + r.rule_id + ' discarded.';
} else {
  txt = '⚠️ rules service unavailable — try again shortly.'
    + (r.error ? '\n(' + r.error + ')' : '');
}
return [{ json: { telegram_payload: { chat_id: adminChatId, text: txt } } }];
"""

LEARN_BODY = ('={ "feedback": {{ JSON.stringify($json.feedback) }}, '
              '"draft_text": {{ JSON.stringify($json.draft_text) }}, '
              '"customer_id": {{ JSON.stringify($json.customer_id) }}, '
              '"customer_name": {{ JSON.stringify($json.customer_name) }} }')

RULES_BODY = ('={ "action": {{ JSON.stringify($json.rules_action) }}, '
              '"rule_id": {{ JSON.stringify($json.rule_id) }} }')

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
    print(f"=== build_fr5_learning.py  [{'DEPLOY' if deploy else 'DRY RUN'}] ===")
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

    if any(x["name"] == "Hermes Learn" for x in nodes):
        die("'Hermes Learn' already present — FR-5 4.3b looks applied; aborting")

    ptr = find("Process Text Reply")
    rta = find("Route Text Action")
    code_skel = find("Mark Skipped")
    hermes_improve = find("Hermes Improve")     # httpRequest, "Hermes Bridge" cred
    send_tg = find("Send Draft to Telegram")    # httpRequest -> Telegram sendMessage
    edit_refine = find("Edit Telegram (Refine)")
    bp = edit_refine.get("position", [0, 0])

    # ---- (M1) Process Text Reply: insert the /rules command block ----------
    js = ptr["parameters"].get("jsCode", "")
    if PTR_ANCHOR not in js:
        die("Process Text Reply: refine-block anchor not found — node differs")
    if "/approverule" in js:
        die("Process Text Reply already has the /rules commands — aborting")
    ptr["parameters"]["jsCode"] = js.replace(PTR_ANCHOR, PTR_DBLOCK + PTR_ANCHOR, 1)

    # ---- (M2) Route Text Action: add the rules_cmd output ------------------
    rules_rule = rta["parameters"]["rules"]["values"]
    base_rule = copy.deepcopy(rules_rule[0])
    for c in base_rule["conditions"]["conditions"]:
        c["id"] = nid()
        c["rightValue"] = "rules_cmd"
    base_rule["outputKey"] = "rules_cmd"
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

    def code_node(name, js_code):
        n = copy.deepcopy(code_skel)
        n["name"] = name
        n["parameters"] = {"jsCode": js_code}
        return n

    def bridge_node(name, url, body):
        n = copy.deepcopy(hermes_improve)
        n["name"] = name
        n["parameters"]["url"] = url
        n["parameters"]["jsonBody"] = body
        return n

    def tg_node(name):
        n = copy.deepcopy(send_tg)
        n["name"] = name
        return n

    # Part 1 — learn from Edit feedback
    add(code_node("Prep Learn", PREP_LEARN), [bp[0] + 240, bp[1] + 200])
    add(bridge_node("Hermes Learn", BRIDGE + "/learn", LEARN_BODY),
        [bp[0] + 480, bp[1] + 200])
    add(code_node("Notify Rule Captured", NOTIFY_RULE_CAPTURED),
        [bp[0] + 720, bp[1] + 200])
    add(tg_node("Send Rule Notice"), [bp[0] + 960, bp[1] + 200])

    # Part 2 — /rules review commands
    rp = rta.get("position", [0, 0])
    add(bridge_node("Hermes Rules", BRIDGE + "/rules", RULES_BODY),
        [rp[0] + 260, rp[1] + 320])
    add(code_node("Format Rules Reply", FORMAT_RULES_REPLY),
        [rp[0] + 500, rp[1] + 320])
    add(tg_node("Send Rules Reply"), [rp[0] + 740, rp[1] + 320])

    nodes.extend(new_nodes)

    # ---- connections -------------------------------------------------------
    def one(node):
        return [{"node": node, "type": "main", "index": 0}]

    # Part 1: Edit Telegram (Refine) -> Prep Learn -> Hermes Learn -> ...
    conns.setdefault("Edit Telegram (Refine)", {})["main"] = [one("Prep Learn")]
    conns["Prep Learn"] = {"main": [one("Hermes Learn")]}
    conns["Hermes Learn"] = {"main": [one("Notify Rule Captured"), []]}
    conns["Notify Rule Captured"] = {"main": [one("Send Rule Notice")]}

    # Part 2: Route Text Action [rules_cmd] -> Hermes Rules -> ...
    rta_main = conns["Route Text Action"]["main"]
    while len(rta_main) < 5:
        rta_main.append([])
    rta_main[4] = one("Hermes Rules")
    # both Hermes Rules outputs (ok + error) flow to Format Rules Reply
    conns["Hermes Rules"] = {"main": [one("Format Rules Reply"),
                                      one("Format Rules Reply")]}
    conns["Format Rules Reply"] = {"main": [one("Send Rules Reply")]}

    n1 = len(nodes)
    print(f"3. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}")
    print("   modified: Process Text Reply (/rules commands), Route Text Action")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    Path("/tmp/fr5l_staged.json").write_text(
        json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print("4. staged -> /tmp/fr5l_staged.json")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR5L-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr5l_wf.json", "upload")
    res = None
    for attempt in range(1, 4):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/fr5l_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 3:
            print(f"   PUT attempt {attempt}: transient 'unauthorized' — retry in 6s")
            time.sleep(6)
            continue
        ssh_run("rm -f /tmp/fr5l_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/fr5l_wf.json", "", "cleanup")
    print(f"6. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("7. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    have = {x["name"] for x in final["nodes"]}
    need = {x["name"] for x in new_nodes}
    missing = need - have
    print(f"8. VERIFY: {len(final['nodes'])} nodes, active={final.get('active')}, "
          f"new nodes present={not missing}")
    print("FR-5 LEARNING LOOP DEPLOYED — Edit feedback now teaches Hermes; "
          "/rules reviews captured rules."
          if not missing else f"x VERIFY FAILED — missing {missing}; backup saved.")


if __name__ == "__main__":
    main()
