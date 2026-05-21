#!/usr/bin/env python3
"""
build_fr3.py - apply FR-3 (message debounce) to the LIVE Dubriani workflow.

When a customer fires several WhatsApp messages seconds apart, each currently
produces its own draft card. FR-3 adds a quiet-window: buffer the messages,
wait ~30s, and only draft once the customer has gone silent — against the
COMBINED messages.

Inserts 3 nodes into the inbound chain, between Filter Inbound and Get Chat
History:
    Filter Inbound -> Buffer Message -> Debounce Wait -> Flush Check
                   -> Get Chat History -> Format Context -> ...
  * Buffer Message - append {id,text,ts} to staticData.inboundBuffer[phone];
    stamp staticData.lastSeq[phone] with a unique token for this message.
  * Debounce Wait  - pause this execution DEBOUNCE_SECONDS.
  * Flush Check    - if lastSeq[phone] still equals this execution's token
    (the customer went quiet), flush the buffer and continue; otherwise a
    newer message arrived -> end quietly (last-writer-wins).
And modifies Format Context to draft against the combined buffered messages.

Default = DRY RUN (writes /tmp/fr3_staged.json). Pass --deploy to back up
+ PUT to the live workflow.

Usage:  python3 scripts/build_fr3.py [--deploy]
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
DEBOUNCE_SECONDS = 30

# ---------------------------------------------------------------- jsCode --

BUFFER_MESSAGE = r"""// FR-3: buffer this inbound message; stamp a per-customer token
const wh = $('WAHA Webhook').item.json.body.payload;
const phone = wh.from;
const data = $getWorkflowStaticData('global');
if (!data.inboundBuffer) data.inboundBuffer = {};
if (!data.lastSeq) data.lastSeq = {};
if (!Array.isArray(data.inboundBuffer[phone])) data.inboundBuffer[phone] = [];
data.inboundBuffer[phone].push({ id: wh.id, text: wh.body || '', ts: Date.now() });
const token = Date.now().toString() + '_' + Math.random().toString(36).slice(2, 8);
data.lastSeq[phone] = token;
return { json: { phone: phone, token: token } };
"""

FLUSH_CHECK = r"""// FR-3: after the quiet-window — am I the latest message for this customer?
const phone = $('Buffer Message').item.json.phone;
const token = $('Buffer Message').item.json.token;
const data = $getWorkflowStaticData('global');
if (!data.lastSeq || data.lastSeq[phone] !== token) {
  // a newer message arrived during the wait; its execution will flush. Stop.
  return [];
}
const buffered = (data.inboundBuffer && data.inboundBuffer[phone]) || [];
const combined = buffered.map(m => m.text).filter(t => t && String(t).trim()).join('\n');
const ids = buffered.map(m => m.id);
data.inboundBuffer[phone] = [];
delete data.lastSeq[phone];
return [{ json: {
  phone: phone,
  combinedMessage: combined,
  bufferedIds: ids,
  bufferedCount: buffered.length
} }];
"""

FORMAT_CONTEXT = r"""// #1 + FR-3: build conversation_history from WAHA; use the debounced message set
const wh = $('WAHA Webhook').first().json.body.payload;
const fc = $('Flush Check').first().json;
const chatId = wh.from;
const excludeIds = new Set((fc.bufferedIds && fc.bufferedIds.length) ? fc.bufferedIds : [wh.id]);
const newestMessage = (fc.combinedMessage && String(fc.combinedMessage).trim())
  ? fc.combinedMessage
  : (wh.body || '');

let msgs = [];
for (const it of $input.all()) {
  const j = it.json;
  if (j && typeof j.body === 'string' && typeof j.fromMe === 'boolean' && j.timestamp) {
    msgs.push(j);
  } else if (Array.isArray(j)) {
    for (const m of j) { if (m && typeof m.body === 'string') msgs.push(m); }
  }
}
msgs = msgs
  .filter(m => m.body && String(m.body).trim() && !excludeIds.has(m.id))
  .sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));

const now = Math.floor(Date.now() / 1000);
function ago(ts) {
  const s = Math.max(0, now - (ts || now));
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 129600) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}

let history;
if (msgs.length === 0) {
  history = 'First contact, no prior messages.';
} else {
  history = msgs.slice(-15).map(function (m) {
    const who = m.fromMe ? 'Dubriani' : 'Customer';
    const body = String(m.body).replace(/\s+/g, ' ').trim();
    return who + ' (' + ago(m.timestamp) + '): "' + body + '"';
  }).join('\n');
}

return [{ json: {
  conversationHistory: history,
  historyCount: msgs.length,
  customerChatId: chatId,
  customerName: wh.notifyName || wh.pushName || '',
  userMessage: newestMessage
} }];
"""

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


def resolve_base():
    out = ssh_run("docker inspect n8n-n8n-1 --format "
                  "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'",
                  "", "resolve-ip", timeout=45)
    ip = out.strip()
    if not ip:
        die("could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def ssh_run(script, stdin, label, timeout=60):
    # the SSH path to the box is intermittent; every call here is idempotent,
    # so retry on connection failure (timeout / ssh exit 255).
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
            if r.returncode != 255:        # 255 = ssh connection failure
                die(f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
            last = f"ssh exit 255 ({r.stderr.strip()[:100]})"
        if attempt < 5:
            print(f"   [{label}] connect attempt {attempt} failed: {last} — retry in 5s")
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
            print(f"   [{label}] upload attempt {attempt} failed: {last} — retry in 5s")
            time.sleep(5)
    die(f"{label}: upload failed after 5 attempts — {last}")


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
    print(f"=== build_fr3.py  [{'DEPLOY' if deploy else 'DRY RUN'}] ===")
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

    if any(x["name"] == "Buffer Message" for x in nodes):
        die("'Buffer Message' already present — FR-3 looks already applied; aborting")

    filt = find("Filter Inbound")
    gch = find("Get Chat History")
    fmt = find("Format Context")
    wait_node = find("Wait")
    code_skel = find("Mark Skipped")        # any code node — for type/typeVersion
    fp = filt.get("position", [0, 0])

    # sanity: confirm Format Context is the node we expect before overwriting
    if "conversationHistory" not in fmt["parameters"].get("jsCode", ""):
        die("Format Context jsCode unexpected — not overwriting blindly")

    # ---- (M1) Format Context: draft against the combined buffered messages -
    fmt["parameters"]["jsCode"] = FORMAT_CONTEXT

    # ---- new nodes ---------------------------------------------------------
    new_nodes = []

    def add(node, pos):
        node["id"] = nid()
        node["position"] = pos
        if "webhookId" in node:
            node["webhookId"] = nid()
        new_nodes.append(node)
        return node

    buf = copy.deepcopy(code_skel)
    buf["name"] = "Buffer Message"
    buf["parameters"] = {"jsCode": BUFFER_MESSAGE}
    add(buf, [fp[0] + 200, fp[1] + 160])

    dw = copy.deepcopy(wait_node)
    dw["name"] = "Debounce Wait"
    dw["parameters"] = dict(dw.get("parameters", {}))
    dw["parameters"]["amount"] = DEBOUNCE_SECONDS
    add(dw, [fp[0] + 400, fp[1] + 160])

    flush = copy.deepcopy(code_skel)
    flush["name"] = "Flush Check"
    flush["parameters"] = {"jsCode": FLUSH_CHECK}
    add(flush, [fp[0] + 600, fp[1] + 160])

    nodes.extend(new_nodes)

    # ---- connections -------------------------------------------------------
    def one(node):
        return [{"node": node, "type": "main", "index": 0}]

    # Filter Inbound out0: Get Chat History -> Buffer Message
    fi_main = conns.setdefault("Filter Inbound", {}).setdefault("main", [])
    if not fi_main:
        fi_main.append([])
    fi_main[0] = one("Buffer Message")
    conns["Buffer Message"] = {"main": [one("Debounce Wait")]}
    conns["Debounce Wait"] = {"main": [one("Flush Check")]}
    conns["Flush Check"] = {"main": [one("Get Chat History")]}

    n1 = len(nodes)
    print(f"3. nodes: {n0} -> {n1}  (+{n1 - n0})")
    print(f"   new: {', '.join(x['name'] for x in new_nodes)}  "
          f"(window: {DEBOUNCE_SECONDS}s)")
    print(f"   modified: Format Context")
    print(f"   rewired: Filter Inbound -> Buffer Message -> Debounce Wait "
          f"-> Flush Check -> Get Chat History")

    put = {
        "name": wf.get("name", WF_NAME),
        "nodes": nodes,
        "connections": conns,
        "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(wf.get("staticData"), (dict, str)) and wf.get("staticData"):
        put["staticData"] = wf["staticData"]

    Path("/tmp/fr3_staged.json").write_text(
        json.dumps(put, indent=2, ensure_ascii=False) + "\n")
    print("4. staged -> /tmp/fr3_staged.json")

    if not deploy:
        print("\nDRY RUN complete — nothing deployed. Re-run with --deploy.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FR3-{ts}.json"
    bk.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. backed up live -> {bk.name}")

    ssh_upload((json.dumps(put, ensure_ascii=False) + "\n").encode("utf-8"),
               "/tmp/fr3_wf.json", "upload")
    res = None
    for attempt in range(1, 4):
        out = ssh_run(
            f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
            f'-H "Content-Type: application/json" --data-binary @/tmp/fr3_wf.json '
            f'{API}/workflows/{wf_id}', key, "PUT", timeout=120)
        res = as_json(out, "PUT")
        if res.get("id") == wf_id:
            break
        if "unauthorized" in out.lower() and attempt < 3:
            print(f"   PUT attempt {attempt}: transient 'unauthorized' — retry in 6s")
            time.sleep(6)
            continue
        ssh_run("rm -f /tmp/fr3_wf.json", "", "cleanup")
        die(f"PUT failed: {out[:300]}")
    ssh_run("rm -f /tmp/fr3_wf.json", "", "cleanup")
    print(f"6. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("7. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    have = {x["name"] for x in final["nodes"]}
    need = {"Buffer Message", "Debounce Wait", "Flush Check"}
    missing = need - have
    print(f"8. VERIFY: {len(final['nodes'])} nodes, active={final.get('active')}, "
          f"new nodes present={not missing}")
    print("FR-3 DEPLOYED — inbound messages now debounced for "
          f"{DEBOUNCE_SECONDS}s." if not missing
          else f"x VERIFY FAILED — missing {missing}; PRE-FR3 backup saved.")


if __name__ == "__main__":
    main()
