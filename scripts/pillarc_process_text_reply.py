#!/usr/bin/env python3
"""Pillar C stage 2 — the ANSWER path. Insert an awaiting-info handler into the
n8n 'Process Text Reply' node so that when the operator REPLIES to the "❓
Hermes needs your input" message, the reply is sent to /answer-info (saves a
GLOBAL rule for every future customer + self-confirms) instead of falling
through to assist_query. Runs ON THE BOX.

Guards (so it never steals other flows): fires ONLY when the message is a reply
whose replied-to text contains "Hermes needs your input", and the text is not a
slash command. Fail-open: /answer-info errors still ack. No new nodes — reuses
the existing 'ack' output. Same safety harness as stage 1 (node --check +
node-count guard + workflow backup, abort before PUT on any failure).

Run:  N8N_KEY=<key> python3 ~/pillarc_process_text_reply.py
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


ANCHOR = "// (H) /help, /start, /commands -> operator command reference (ack path)"

INSERT = """// (ask-before-guess ANSWER, Pillar C stage 2 2026-06-02) — the operator
// REPLIED to the "❓ Hermes needs your input" message. Treat the reply as the
// ANSWER: /answer-info saves it as a GLOBAL rule (every future customer) and
// self-sends the "✓ learned" confirm. Fires ONLY on a reply to that question
// (so it never steals slash commands or draft-card edits). Fail-open.
{
  const __rt = msg.reply_to_message;
  const __rtTxt = (__rt && __rt.text) ? __rt.text : '';
  if (text && text[0] !== '/' && __rtTxt.indexOf('Hermes needs your input') >= 0) {
    const _ai = await bq('awaiting-info', { chat_id: adminChatId });
    if (_ai && _ai.found && _ai.info) {
      const __st = _ai.info;
      try {
        await _http({
          method: 'POST',
          url: 'http://172.18.0.1:8788/answer-info',
          headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': $env.BRIDGE_TOKEN || '' },
          body: { chat_id: String(adminChatId), answer: text,
                  customer_id: __st.customer_id || '', question: __st.question || '' },
          json: true, timeout: 10000,
        });
      } catch (e) { /* fail-open: still ack below */ }
      const __who = __st.customer_name || __st.customer_phone || 'their card';
      return { json: { action: 'ack', admin_chat_id: adminChatId,
        ack_text: '\\u270d\\ufe0f Saved for every future customer. Tap Draft on ' + __who + ' to use it now.' } };
    }
  }
}

"""

d = api("GET")
n_before = len(d["nodes"])
json.dump(d, open("/home/ubuntu/wf-backup-pillarc-s2-20260602.json", "w"),
          ensure_ascii=False)
ptr = [n for n in d["nodes"] if n["name"] == "Process Text Reply"]
assert len(ptr) == 1, f"Process Text Reply count={len(ptr)}"
code = ptr[0]["parameters"]["jsCode"]
assert code.count(ANCHOR) == 1, f"anchor appears {code.count(ANCHOR)}x (need 1)"
assert "ask-before-guess ANSWER" not in code, "already edited — abort"
newcode = code.replace(ANCHOR, INSERT + ANCHOR)
ptr[0]["parameters"]["jsCode"] = newcode

wrap = "async function __w(){\n" + newcode + "\n}"
open("/tmp/ptr_check.js", "w").write(wrap)
subprocess.run(["docker", "cp", "/tmp/ptr_check.js", "n8n-n8n-1:/tmp/ptr_check.js"],
               check=True)
chk = subprocess.run(["docker", "exec", "n8n-n8n-1", "node", "--check",
                      "/tmp/ptr_check.js"], capture_output=True, text=True)
print(f"node --check rc={chk.returncode} {chk.stderr.strip()[:300]}")
assert chk.returncode == 0, "jsCode SYNTAX INVALID — ABORT (no PUT)"
assert len(d["nodes"]) == n_before == 198, f"node count {len(d['nodes'])}"

put = {"name": d["name"], "nodes": d["nodes"], "connections": d["connections"],
       "settings": {"executionOrder":
                    d.get("settings", {}).get("executionOrder", "v1")}}
api("PUT", put)
v = api("GET")
vptr = [n for n in v["nodes"] if n["name"] == "Process Text Reply"][0]
print("VERIFY: active=%s nodes=%d  answer-branch present=%s" % (
    v.get("active"), len(v["nodes"]),
    "ask-before-guess ANSWER" in vptr["parameters"]["jsCode"]))
