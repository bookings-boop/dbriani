#!/usr/bin/env python3
"""Box-local prompt safeput — same steps as scripts/deploy_prompt_safeput.py +
n8n_deploy.safe_put, executed ON the box so the PUT window is ~1-3s (the
original design assumption). Exists because the Mac↔box ssh transport was too
slow for the multi-MB workflow JSON (get-workflow timed out 10x @70s,
2026-06-11) and bigger timeouts would WIDEN the staticData race safe_put
exists to kill.

Run (key via stdin, never on box disk):
  grep '^N8N_API_KEY=' .env | cut -d= -f2- | \
      ssh dubriani-ec2 'python3 /tmp/box_safeput_prompt.py'
"""
import json
import subprocess
import sys
import time
import urllib.request

WF_NAME = "Dubriani Phase 1B — WhatsApp + Telegram Approval"
PROMPT_NODES = ("Build Prompt", "Build Regen Prompt",
                "Build Refine Prompt", "Build Lead Prompt")
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder"}
# operator-required live-content checks (2026-06-11 catalog deploy)
MUST_CONTAIN = (
    "3,000 — open here; discounts ONLY per §7.1 policy",
    "⭐ Princess 60",
    "NEED-BASED discount, NOT the morning",
    "majesty-request",
)
MUST_NOT_CONTAIN = ("B2C minimum (morning",)

KEY = sys.stdin.readline().strip()
if not KEY:
    sys.exit("no API key on stdin")

ip = subprocess.run(
    ["docker", "inspect", "n8n-n8n-1", "--format",
     "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
    capture_output=True, text=True).stdout.strip()
if not ip:
    sys.exit("could not resolve n8n container IP")
API = f"http://{ip}:5678/api/v1"


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        API + path, method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"},
        data=body)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def queue_size(static_data):
    if isinstance(static_data, str):
        try:
            static_data = json.loads(static_data)
        except Exception:
            return None
    if isinstance(static_data, dict):
        scope = static_data.get("global")
        if not isinstance(scope, dict):
            scope = static_data
        q = scope.get("pendingQueue")
        if isinstance(q, list):
            return len(q)
    return None


prompt = open("/home/ubuntu/hermes-bridge/system-prompt.md").read()
registry = open("/tmp/file-registry.md").read()
if len(prompt) < 1000:
    sys.exit(f"prompt suspiciously short ({len(prompt)}) — refusing")
combined = prompt + "\n\n---\n\n# FILE REGISTRY\n" + registry

items = api("/workflows").get("data") or []
target = next((w for w in items if w.get("name") == WF_NAME), None)
if not target:
    sys.exit(f"workflow {WF_NAME!r} not found")
wf = api(f"/workflows/{target['id']}")
wf_id = wf["id"]
was_active = bool(wf.get("active"))
print(f"live workflow {wf_id}: {len(wf.get('nodes', []))} nodes, "
      f"active={was_active}")

patched = set()
for node in wf.get("nodes", []):
    if node.get("name") in PROMPT_NODES and node.get("type") == "n8n-nodes-base.set":
        asn = ((node.get("parameters") or {}).get("assignments") or {}).get("assignments") or []
        for a in asn:
            if a.get("name") == "systemPrompt":
                a["value"] = combined
                patched.add(node["name"])
                break
missing = sorted(set(PROMPT_NODES) - patched)
if missing:
    sys.exit(f"missing expected Set nodes: {missing}")
print(f"patched {sorted(patched)}  combined={len(combined):,} chars")

ts = time.strftime("%Y%m%d_%H%M%S")
bk = f"/tmp/phase-1b-telegram.PRE-PROMPT-ROUTING-{ts}.json"
with open(bk, "w") as f:
    json.dump(wf, f, indent=2, ensure_ascii=False)
print(f"backup -> {bk}")

# THE FIX: fresh staticData in the instant before the PUT (box-local = ~ms)
fresh = api(f"/workflows/{wf_id}")
live_static = fresh.get("staticData")
pre_q = queue_size(live_static)
print(f"live pendingQueue right now: {pre_q} draft(s) — preserved as-is")

put = {
    "name": wf.get("name", WF_NAME),
    "nodes": wf["nodes"],
    "connections": wf["connections"],
    "settings": {k: v for k, v in (wf.get("settings") or {}).items()
                 if k in ALLOWED_SETTINGS},
}
if isinstance(live_static, (dict, str)) and live_static:
    put["staticData"] = live_static
body = (json.dumps(put, ensure_ascii=False) + "\n").encode()

res = None
for attempt in range(1, 13):
    try:
        res = api(f"/workflows/{wf_id}", method="PUT", body=body)
    except Exception as e:
        res = {"err": str(e)[:200]}
    if res.get("id") == wf_id:
        break
    if attempt < 12:
        print(f"PUT attempt {attempt} failed: {str(res)[:150]} — retry 12s")
        time.sleep(12)
else:
    sys.exit(f"PUT failed: {str(res)[:300]}")
print(f"PUT OK ({len(res.get('nodes', []))} nodes)")

if was_active:
    api(f"/workflows/{wf_id}/activate", method="POST", body=b"")
    print("re-activated")

final = api(f"/workflows/{wf_id}")
post_q = queue_size(final.get("staticData"))
print(f"verify: pendingQueue now {post_q} draft(s)"
      + ("  ⚠️ QUEUE SHRANK — inspect backup!"
         if isinstance(pre_q, int) and isinstance(post_q, int) and post_q < pre_q
         else ""))

# operator-required content verification on the LIVE nodes
all_ok = True
for node in final.get("nodes", []):
    if node.get("name") in PROMPT_NODES and node.get("type") == "n8n-nodes-base.set":
        asn = ((node.get("parameters") or {}).get("assignments") or {}).get("assignments") or []
        sp = next((a.get("value", "") for a in asn if a.get("name") == "systemPrompt"), "")
        ok = (all(m in sp for m in MUST_CONTAIN)
              and not any(m in sp for m in MUST_NOT_CONTAIN)
              and sp.find("| Von Dutch 40 |") < sp.find("| Bliss 55 |"))
        all_ok &= ok
        print(f"NODE-VERIFY {node['name']!r}: "
              + ("ALL MARKERS OK" if ok else "MARKERS MISSING/STALE"))
print("LIVE-NODE VERIFICATION: " + ("PASS" if all_ok else "FAIL"))
sys.exit(0 if all_ok else 2)
