#!/usr/bin/env python3
"""
rollback_workflow.py - revert the live Phase 1B workflow to a pre-Hermes
backup (direct Claude drafting), preserving the CURRENT pending-draft queue.

Backs up the current live workflow first (rollback-of-rollback safety), then
PUTs the pre-Hermes nodes/connections with the live staticData.

Usage:  python3 scripts/rollback_workflow.py <source-backup.json>
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
        die(f"{label}: upload failed\n{r.stderr.decode()[:300]}")


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def main():
    if len(sys.argv) < 2:
        die("usage: rollback_workflow.py <source-backup.json>")
    src_path = ROOT / "workflows" / Path(sys.argv[1]).name
    if not src_path.exists():
        die(f"source not found: {src_path}")
    src = json.loads(src_path.read_text())
    snames = {n["name"] for n in src.get("nodes", [])}
    if "Claude AI" not in snames or "Call Hermes Bridge" in snames:
        die("source is not a pre-Hermes workflow "
            f"(Claude AI={'Claude AI' in snames}, bridge={'Call Hermes Bridge' in snames})")
    print(f"1. rollback source: {src_path.name} — {len(src['nodes'])} nodes "
          "(pre-Hermes, direct Claude drafting)")

    key = load_key()
    global API
    API = resolve_base()
    print(f"   n8n API: {API}")

    out = ssh_run(f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows',
                  key, "list")
    items = (as_json(out, "list").get("data") or [])
    target = next((w for w in items if w.get("name") == WF_NAME), None)
    if not target:
        die(f"workflow {WF_NAME!r} not found")
    wf_id = target["id"]
    out = ssh_run(f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
                  key, "get")
    live = as_json(out, "get")
    was_active = bool(live.get("active"))
    print(f"2. live workflow {wf_id}: {len(live.get('nodes', []))} nodes, active={was_active}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-ROLLBACK-{ts}.json"
    bk.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
    print(f"3. current live backed up -> {bk.name}")

    put = {
        "name": src.get("name", WF_NAME),
        "nodes": src["nodes"],
        "connections": src["connections"],
        "settings": {k: v for k, v in (src.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    cur_sd = live.get("staticData")
    if isinstance(cur_sd, (dict, str)) and cur_sd:
        put["staticData"] = cur_sd
        pq = "?"
        try:
            pq = len(cur_sd["global"]["pendingQueue"]) if isinstance(cur_sd, dict) else "?"
        except Exception:
            pass
        print(f"4. preserving current staticData (pendingQueue entries: {pq})")
    else:
        print("4. no current staticData to carry over")

    ssh_upload((json.dumps(put) + "\n").encode("utf-8"), "/tmp/rb_wf.json", "upload")
    out = ssh_run(
        f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
        f'-H "Content-Type: application/json" --data-binary @/tmp/rb_wf.json '
        f'{API}/workflows/{wf_id}; rm -f /tmp/rb_wf.json', key, "PUT")
    res = as_json(out, "PUT")
    if res.get("id") != wf_id:
        die(f"PUT did not return the workflow: {json.dumps(res)[:500]}")
    print(f"5. workflow PUT OK ({len(res.get('nodes', []))} nodes)")

    if was_active:
        out = ssh_run(f'read -r K; curl -s -m25 -X POST -H "X-N8N-API-KEY: $K" '
                      f'{API}/workflows/{wf_id}/activate', key, "activate")
        print(f"6. re-activate: active={as_json(out, 'activate').get('active')}")

    out = ssh_run(f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
                  key, "verify")
    final = as_json(out, "verify")
    fn = {n["name"] for n in final.get("nodes", [])}
    ok = (len(final.get("nodes", [])) == len(src["nodes"])
          and "Claude AI" in fn and "Call Hermes Bridge" not in fn)
    print(f"7. VERIFY: {len(final.get('nodes', []))} nodes, active={final.get('active')}, "
          f"Claude AI present={'Claude AI' in fn}, bridge gone={'Call Hermes Bridge' not in fn}")
    print("ROLLBACK OK — direct-Claude drafting restored. Test before relying on it."
          if ok else "x ROLLBACK VERIFY FAILED — inspect; PRE-ROLLBACK backup saved.")


if __name__ == "__main__":
    main()
