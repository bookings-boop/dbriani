#!/usr/bin/env python3
"""
fix_reply_routing.py - patch the live workflow's "Process Text Reply" node so a
reply-to-draft matches the NEWEST queue entry with that telegram_message_id,
not the first.

Telegram message IDs are per-chat counters that reset on bot swaps/restarts, so
the queue can hold old + new entries sharing an ID. `queue.find()` returned the
oldest (wrong customer). Scanning newest-first hits the card actually replied
to. GETs the live workflow, backs it up, patches the one node, PUTs it back.

Usage:  python3 scripts/fix_reply_routing.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
API = None
WF_NAME = "Dubriani Phase 1B — WhatsApp + Telegram Approval"
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder"}

OLD = "  const d = queue.find(x => x.telegram_message_id === r.message_id);"
NEW = ("  // newest-match: telegram message_ids collide across bot/session resets,\n"
       "  // so scan newest-first to hit the current card, not a stale entry.\n"
       "  let d = null;\n"
       "  for (let _i = queue.length - 1; _i >= 0; _i--) {\n"
       "    if (queue[_i].telegram_message_id === r.message_id) { d = queue[_i]; break; }\n"
       "  }")


def die(m):
    sys.exit(f"x {m}")


def load_key():
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            v = ln.split("=", 1)[1].strip()
            if v:
                return v
    die("N8N_API_KEY not in .env")


def resolve_base():
    r = subprocess.run(
        SSH + ["docker inspect n8n-n8n-1 --format "
               "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"],
        capture_output=True, text=True, timeout=30)
    ip = r.stdout.strip()
    if not ip:
        die("could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def ssh_run(script, stdin, label, timeout=120):
    r = subprocess.run(SSH + [script], input=stdin, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        die(f"{label}: ssh exit {r.returncode}\n{r.stderr[:400]}")
    return r.stdout


def ssh_upload(data, remote, label):
    r = subprocess.run(SSH + [f"cat > {remote}"], input=data,
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        die(f"{label}: upload failed")


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def main():
    key = load_key()
    global API
    API = resolve_base()
    print(f"1. n8n API: {API}")

    out = ssh_run(f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows',
                  key, "list")
    items = (as_json(out, "list").get("data") or [])
    target = next((w for w in items if w.get("name") == WF_NAME), None)
    if not target:
        die(f"workflow {WF_NAME!r} not found")
    wf_id = target["id"]
    live = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "get"), "get")
    was_active = bool(live.get("active"))
    print(f"2. live workflow {wf_id}: {len(live.get('nodes', []))} nodes, active={was_active}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-FIX-{ts}.json"
    bk.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
    print(f"3. backed up live -> {bk.name}")

    node = next((n for n in live["nodes"] if n["name"] == "Process Text Reply"), None)
    if not node:
        die("'Process Text Reply' node not found in the live workflow")
    js = node["parameters"].get("jsCode", "")
    if "queue[_i].telegram_message_id" in js:
        print("4. already patched (newest-match present) — nothing to do")
        return
    if OLD not in js:
        die("expected reply-lookup line not found in Process Text Reply — "
            "live jsCode differs from what was expected; not patching blindly")
    node["parameters"]["jsCode"] = js.replace(OLD, NEW, 1)
    print("4. patched Process Text Reply (first-match -> newest-match)")

    put = {
        "name": live.get("name", WF_NAME),
        "nodes": live["nodes"],
        "connections": live["connections"],
        "settings": {k: v for k, v in (live.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(live.get("staticData"), (dict, str)) and live.get("staticData"):
        put["staticData"] = live["staticData"]

    ssh_upload((json.dumps(put) + "\n").encode("utf-8"), "/tmp/fix_wf.json", "upload")
    res = as_json(ssh_run(
        f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
        f'-H "Content-Type: application/json" --data-binary @/tmp/fix_wf.json '
        f'{API}/workflows/{wf_id}; rm -f /tmp/fix_wf.json', key, "PUT"), "PUT")
    if res.get("id") != wf_id:
        die(f"PUT did not return the workflow: {json.dumps(res)[:400]}")
    print(f"5. PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                f'{API}/workflows/{wf_id}/activate', key, "activate")
        print("6. re-activated")

    final = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "verify"), "verify")
    fnode = next((n for n in final["nodes"] if n["name"] == "Process Text Reply"), {})
    fjs = fnode.get("parameters", {}).get("jsCode", "")
    ok = "queue[_i].telegram_message_id" in fjs and OLD not in fjs
    print(f"7. VERIFY: newest-match present={'queue[_i].telegram_message_id' in fjs}, "
          f"old first-match gone={OLD not in fjs}")
    print("FIX DEPLOYED — reply-to-draft now routes to the correct customer."
          if ok else "x VERIFY FAILED — inspect; PRE-FIX backup saved.")


if __name__ == "__main__":
    main()
