#!/usr/bin/env python3
"""
prune_queue.py - one-time prune of the live workflow's draft queue.

Keeps EVERY 'pending' entry (the operator's actionable queue) and the N most
recent done ('sent'/'skipped') entries (so reply-to-a-recent-draft still
works); drops older done entries. GETs the live workflow, backs it up, prunes
staticData.global.pendingQueue, PUTs it back.

Usage:  python3 scripts/prune_queue.py [keep_done=12]
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


def as_json(t, label):
    try:
        return json.loads(t)
    except Exception:
        die(f"{label}: non-JSON response:\n{t[:500]}")


def main():
    keep_done = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    key = load_key()
    global API
    API = resolve_base()
    print(f"1. n8n API: {API}  (keep_done={keep_done})")

    items = (as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows',
        key, "list"), "list").get("data") or [])
    target = next((w for w in items if w.get("name") == WF_NAME), None)
    if not target:
        die(f"workflow {WF_NAME!r} not found")
    wf_id = target["id"]
    live = as_json(ssh_run(
        f'read -r K; curl -s -m25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
        key, "get"), "get")
    was_active = bool(live.get("active"))

    sd = live.get("staticData")
    if not isinstance(sd, dict) or "global" not in sd:
        die("no staticData.global on the live workflow — nothing to prune")
    q = sd["global"].get("pendingQueue") or []
    statuses = {}
    for e in q:
        statuses[e.get("status")] = statuses.get(e.get("status"), 0) + 1
    print(f"2. live workflow {wf_id}: queue has {len(q)} entries  {statuses}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = ROOT / "workflows" / f"phase-1b-telegram.PRE-PRUNE-{ts}.json"
    bk.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
    print(f"3. backed up live -> {bk.name}")

    done_idx = [i for i, e in enumerate(q) if e.get("status") != "pending"]
    keep_done_idx = set(done_idx[-keep_done:])
    new_q = [e for i, e in enumerate(q)
             if e.get("status") == "pending" or i in keep_done_idx]
    dropped = len(q) - len(new_q)
    if dropped == 0:
        print("4. nothing to prune — queue already lean")
        return
    kept_pending = sum(1 for e in new_q if e.get("status") == "pending")
    print(f"4. pruned: kept {len(new_q)} ({kept_pending} pending + "
          f"{len(new_q) - kept_pending} recent done), dropped {dropped} stale done")
    sd["global"]["pendingQueue"] = new_q

    put = {
        "name": live.get("name", WF_NAME),
        "nodes": live["nodes"],
        "connections": live["connections"],
        "settings": {k: v for k, v in (live.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
        "staticData": sd,
    }
    ssh_upload((json.dumps(put) + "\n").encode("utf-8"), "/tmp/prune_wf.json", "upload")
    res = as_json(ssh_run(
        f'read -r K; curl -s -m90 -X PUT -H "X-N8N-API-KEY: $K" '
        f'-H "Content-Type: application/json" --data-binary @/tmp/prune_wf.json '
        f'{API}/workflows/{wf_id}; rm -f /tmp/prune_wf.json', key, "PUT"), "PUT")
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
    fq = (((final.get("staticData") or {}).get("global") or {}).get("pendingQueue") or [])
    print(f"7. VERIFY: queue now {len(fq)} entries, active={final.get('active')}")
    print("PRUNE OK" if len(fq) == len(new_q) else "x VERIFY MISMATCH — check backup")


if __name__ == "__main__":
    main()
