#!/usr/bin/env python3
"""Dubriani catalog drift-check. Reads the catalog block from all 5 copies
(bridge system-prompt.md + 4 n8n prompt nodes via Postgres) and asserts identical.
No n8n API key needed (reads workflow_entity directly). Exit 1 on drift -> cron/Telegram alert."""
import subprocess, json, hashlib, sys, os
WFID = "azPIy9OcDwiPV5uY"
PROMPT_NODES = ["Build Prompt","Build Regen Prompt","Build Refine Prompt","Build Lead Prompt"]
def cb(t):
    i = t.find("## 7. Yacht Catalog"); j = t.find("## 11. Multi-day", i)
    return t[i:j] if i >= 0 and j >= 0 else None
def sha(s): return hashlib.sha256(s.encode()).hexdigest()[:12] if s else "NONE"
def big(p):
    b = ""
    def w(o):
        nonlocal b
        if isinstance(o, str):
            if len(o) > len(b): b = o
        elif isinstance(o, dict): [w(v) for v in o.values()]
        elif isinstance(o, list): [w(v) for v in o]
    w(p); return b
shas = {}
sp = open(os.path.expanduser("~/hermes-bridge/system-prompt.md")).read()
shas["bridge:system-prompt.md"] = sha(cb(sp))
out = subprocess.run(["docker","exec","n8n-postgres-1","psql","-U","n8n","-d","n8n","-tAc",
                      f"SELECT nodes::text FROM workflow_entity WHERE id='{WFID}'"],
                     capture_output=True, text=True)
nodes = json.loads(out.stdout.strip())
for n in nodes:
    if n.get("name") in PROMPT_NODES:
        shas[f"n8n:{n['name']}"] = sha(cb(big(n["parameters"])))
for k, v in shas.items(): print(f"  {v}  {k}")
uniq = set(shas.values())
if len(shas) == 5 and len(uniq) == 1 and "NONE" not in uniq:
    print(f"DRIFT-CHECK: PASS — all 5 catalog copies identical ({uniq.pop()})")
else:
    print(f"DRIFT-CHECK: FAIL — {len(shas)} copies, shas={uniq}"); sys.exit(1)
