#!/usr/bin/env python3
"""Pillar C stage 1 — insert the needs_operator_input -> /ask-operator branch
into the n8n 'Parse Response' node, then PUT the workflow. Runs ON THE BOX.

Safety: GET live -> back up -> string-insert (anchor must appear exactly once)
-> validate the new jsCode via `node --check` in the n8n container -> assert
node count unchanged -> PUT -> re-GET and verify active + node count + the new
code is present. Aborts before PUT on any failed check. The inserted block is
INERT until the bridge ASK_BEFORE_GUESS_DIRECTIVE ships (the drafter never emits
needs_operator_input before that), and fails OPEN, so this PUT cannot change
normal drafting.

Run:  N8N_KEY=<key> python3 ~/pillarc_parse_response.py
"""
import json
import os
import subprocess
import urllib.request

KEY = os.environ["N8N_KEY"]
WID = "azPIy9OcDwiPV5uY"
IP = subprocess.check_output(
    ["docker", "inspect", "-f",
     "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "n8n-n8n-1"]
).decode().strip()
BASE = f"http://{IP}:5678/api/v1/workflows/{WID}"


def api(method, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE, data=data, method=method,
                                 headers={"X-N8N-API-KEY": KEY,
                                          "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


ANCHOR = "let messages = parsed.messages.slice(0, 4).map(m => String(m));"

INSERT = """
// Pillar C (2026-06-02): the drafter asked for a missing fact instead of
// guessing (needs_operator_input set, messages empty). Ask the operator via the
// bridge (/ask-operator posts the question + stores the awaiting-info state)
// and SKIP the draft card by returning no items. Fail-safe: only fires when
// there is NO usable reply. Fail-open: any error falls through to a normal draft.
try {
  const __noi = (parsed && typeof parsed.needs_operator_input === 'string')
    ? parsed.needs_operator_input.trim() : '';
  if (__noi && messages.length === 0) {
    const __http = this.helpers.httpRequest.bind(this.helpers);
    const __bp = $('Build Prompt').item.json;
    await __http({
      method: 'POST',
      url: 'http://172.18.0.1:8788/ask-operator',
      headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': $env.BRIDGE_TOKEN || '' },
      body: {
        question: __noi,
        customer_id: __bp.customerPhone || '',
        customer_phone: __bp.customerPhone || '',
        customer_name: __bp.customerName || '',
        customer_msg: __bp.userMessage || ''
      },
      json: true,
      timeout: 15000
    });
    return [];
  }
} catch (e) { /* fail-open: fall through to a normal draft */ }"""

d = api("GET")
n_before = len(d["nodes"])
json.dump(d, open("/home/ubuntu/wf-backup-pillarc-20260602.json", "w"),
          ensure_ascii=False)
pr = [n for n in d["nodes"] if n["name"] == "Parse Response"]
assert len(pr) == 1, f"Parse Response node count={len(pr)}"
code = pr[0]["parameters"]["jsCode"]
assert code.count(ANCHOR) == 1, f"anchor appears {code.count(ANCHOR)}x (need 1)"
assert "needs_operator_input" not in code, "already edited — abort"
newcode = code.replace(ANCHOR, ANCHOR + "\n" + INSERT)
pr[0]["parameters"]["jsCode"] = newcode

# Validate the new jsCode (n8n bodies allow top-level await -> wrap).
wrap = "async function __w(){\n" + newcode + "\n}"
open("/tmp/pr_check.js", "w").write(wrap)
subprocess.run(["docker", "cp", "/tmp/pr_check.js", "n8n-n8n-1:/tmp/pr_check.js"],
               check=True)
chk = subprocess.run(["docker", "exec", "n8n-n8n-1", "node", "--check",
                      "/tmp/pr_check.js"], capture_output=True, text=True)
print(f"node --check rc={chk.returncode} {chk.stderr.strip()[:300]}")
assert chk.returncode == 0, "jsCode SYNTAX INVALID — ABORT (no PUT)"
assert len(d["nodes"]) == n_before == 198, f"node count {len(d['nodes'])}"

put = {"name": d["name"], "nodes": d["nodes"], "connections": d["connections"],
       "settings": {"executionOrder":
                    d.get("settings", {}).get("executionOrder", "v1")}}
res = api("PUT", put)
print(f"PUT ok: active={res.get('active')} nodes={len(res.get('nodes') or [])}")

# Re-verify from a fresh GET.
v = api("GET")
vpr = [n for n in v["nodes"] if n["name"] == "Parse Response"][0]
print("VERIFY: active=%s nodes=%d  ask-branch present=%s" % (
    v.get("active"), len(v["nodes"]),
    "/ask-operator" in vpr["parameters"]["jsCode"]))
