#!/usr/bin/env python3
"""
deploy_workflow.py - deploy the Hermes-bridge-rewired Phase 1B workflow to
the live n8n instance via its REST API, tunnelled over SSH.

Phases:
  1. Load N8N_API_KEY from .env.
  2. GET /workflows  -> find "Dubriani Phase 1B ..." (id, active state).
  3. GET /workflows/{id}  -> save a local backup of the LIVE workflow.
  4. Apply the Step 3d surgery (hermes_integration.surgery) to the live JSON.
  5. POST /credentials  -> create the "Hermes Bridge" credential (the bridge
     token is read on the box; it never reaches this machine).
  6. Patch the 2 bridge nodes with the new credential id.
  7. PUT /workflows/{id}  -> push the rewired workflow.
  8. POST /workflows/{id}/activate  -> re-activate.
  9. GET /workflows/{id}  -> verify (38 nodes, Call Hermes Bridge present).

  --dry-run stops after phase 4 (no credential, no PUT) and prints the plan.

Usage:  python3 scripts/deploy_workflow.py [--dry-run]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hermes_integration import surgery  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
# n8n's port is not published to the host (it sits behind Caddy). The API is
# reached at the n8n container's IP on the docker bridge; resolved at runtime.
API = None
WF_NAME = "Dubriani Phase 1B — WhatsApp + Telegram Approval"

# Keys the n8n public API accepts in workflow `settings`. Anything else
# (e.g. `binaryMode`) is rejected with "must NOT have additional properties".
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions",
                    "saveDataErrorExecution", "saveDataSuccessExecution",
                    "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder"}


def die(msg):
    sys.exit(f"x {msg}")


def load_key():
    env = ROOT / ".env"
    for ln in env.read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            v = ln.split("=", 1)[1].strip()
            if v:
                return v
    die("N8N_API_KEY not set in .env")


def resolve_n8n_base():
    """Resolve the n8n API base URL from the n8n container's bridge IP."""
    r = subprocess.run(
        SSH + ["docker inspect n8n-n8n-1 --format "
               "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"],
        capture_output=True, text=True, timeout=30)
    ip = r.stdout.strip()
    if r.returncode != 0 or not ip:
        die(f"could not resolve n8n container IP: {r.stderr[:300]}")
    return f"http://{ip}:5678/api/v1"


def ssh_run(remote_script, stdin_data, label, timeout=120):
    r = subprocess.run(SSH + [remote_script], input=stdin_data,
                        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        die(f"{label}: ssh exit {r.returncode}\nSTDERR: {r.stderr[:600]}")
    return r.stdout


def ssh_upload(data_bytes, remote_path, label):
    r = subprocess.run(SSH + [f"cat > {remote_path}"], input=data_bytes,
                        capture_output=True, timeout=60)
    if r.returncode != 0:
        die(f"{label}: upload failed\n{r.stderr.decode()[:400]}")


def as_json(text, label):
    try:
        return json.loads(text)
    except Exception:
        die(f"{label}: non-JSON response:\n{text[:700]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cred-id", default=None,
                    help="reuse an existing 'Hermes Bridge' credential id "
                         "instead of creating a new one")
    args = ap.parse_args()
    key = load_key()
    print(f"1. N8N_API_KEY loaded ({len(key)} chars)")
    global API
    API = resolve_n8n_base()
    print(f"   n8n API endpoint: {API}\n")

    # --- phase 2: find the workflow ---
    out = ssh_run(f'read -r K; curl -s -m 25 -H "X-N8N-API-KEY: $K" {API}/workflows',
                  key, "list workflows")
    data = as_json(out, "list workflows")
    items = data.get("data", data) if isinstance(data, dict) else data
    target = None
    print(f"2. live workflows ({len(items)}):")
    for w in items:
        mark = ""
        if w.get("name") == WF_NAME:
            target = w
            mark = "  <-- target"
        print(f"   id={w.get('id')} active={w.get('active')} "
              f"nodes={len(w.get('nodes', []))} name={w.get('name')!r}{mark}")
    if not target:
        die(f"workflow named {WF_NAME!r} not found")
    wf_id = target["id"]
    was_active = bool(target.get("active"))
    print()

    # --- phase 3: GET full workflow + local backup ---
    out = ssh_run(f'read -r K; curl -s -m 25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
                  key, "get workflow")
    live = as_json(out, "get workflow")
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "workflows" / f"phase-1b-telegram.LIVE-backup-{ts}.json"
    backup.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
    print(f"3. live workflow backed up -> {backup.relative_to(ROOT)}")
    print(f"   live: {len(live.get('nodes', []))} nodes, active={was_active}\n")
    # Retention: keep last 5 LIVE-backup snapshots, delete older.
    _BACKUP_KEEP = 5
    _live_backups = sorted(
        (ROOT / "workflows").glob("phase-1b-telegram.LIVE-backup-*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    for _old in _live_backups[_BACKUP_KEEP:]:
        try:
            _old.unlink()
        except OSError:
            pass
    if len(_live_backups) > _BACKUP_KEEP:
        print(f"   pruned {len(_live_backups) - _BACKUP_KEEP} old "
              f"LIVE-backup(s) (kept {_BACKUP_KEEP})")

    # --- phase 4: apply surgery ---
    rewired, changed = surgery(json.loads(json.dumps(live)))
    if not changed:
        print("4. live workflow ALREADY rewired (Call Hermes Bridge present).")
        print("   Nothing to deploy. Done.")
        return
    print(f"4. surgery applied in memory: {len(live['nodes'])} -> "
          f"{len(rewired['nodes'])} nodes")
    names = {n["name"] for n in rewired["nodes"]}
    for chk in ["Call Hermes Bridge", "Call Hermes Bridge (Regen)"]:
        print(f"   + {chk}: {'OK' if chk in names else 'MISSING'}")
    for chk in ["Build Prompt", "Claude AI", "Build Regen Prompt", "Claude AI (Regen)"]:
        print(f"   - {chk}: {'removed' if chk not in names else 'STILL PRESENT'}")
    print()

    if args.dry_run:
        print("DRY RUN — stopping before credential creation + PUT.")
        print("Re-run without --dry-run to deploy.")
        return

    # --- phase 5: credential name->id map from the LIVE workflow ---
    cred_map = {}
    for n in live.get("nodes", []):
        for cref in (n.get("credentials") or {}).values():
            if cref.get("id") and cref.get("name"):
                cred_map[cref["name"]] = cref["id"]
    print("5. live credentials: "
          + (", ".join(f"{k}={v}" for k, v in cred_map.items()) or "(none)"))
    if "Hermes Bridge" not in cred_map:
        if args.cred_id:
            cred_map["Hermes Bridge"] = args.cred_id
            print(f"   Hermes Bridge: using --cred-id {args.cred_id}")
        else:
            cred_script = (
                'read -r K; '
                'TOK=$(grep -E "^BRIDGE_TOKEN=" ~/hermes-bridge/.env | cut -d= -f2-); '
                '[ -z "$TOK" ] && { echo \'{"error":"no bridge token"}\'; exit 0; }; '
                'printf \'{"name":"Hermes Bridge","type":"httpHeaderAuth",'
                '"data":{"name":"X-Bridge-Token","value":"%s"}}\' "$TOK" > /tmp/dbn_cred.json; '
                f'curl -s -m 25 -X POST -H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
                f'--data-binary @/tmp/dbn_cred.json {API}/credentials; rm -f /tmp/dbn_cred.json'
            )
            cred = as_json(ssh_run(cred_script, key, "create credential"),
                           "create credential")
            if not cred.get("id"):
                die(f"credential creation returned no id: {json.dumps(cred)[:400]}")
            cred_map["Hermes Bridge"] = cred["id"]
            print(f"   Hermes Bridge credential created: {cred['id']}")
    print()

    # --- phase 6: fill every null credential id from the live map ---
    filled, missing = 0, set()
    for n in rewired["nodes"]:
        for cref in (n.get("credentials") or {}).values():
            if not cref.get("id"):
                if cref.get("name") in cred_map:
                    cref["id"] = cred_map[cref["name"]]
                    filled += 1
                else:
                    missing.add(cref.get("name"))
    if missing:
        die(f"credential names with no resolvable id: {sorted(missing)}")
    print(f"6. filled {filled} null credential id(s) from the live map\n")

    # --- phase 7: PUT the workflow ---
    put_body = {
        "name": rewired.get("name", WF_NAME),
        "nodes": rewired["nodes"],
        "connections": rewired["connections"],
        "settings": {k: v for k, v in (rewired.get("settings") or {}).items()
                     if k in ALLOWED_SETTINGS},
    }
    if isinstance(rewired.get("staticData"), (dict, str)) and rewired.get("staticData"):
        put_body["staticData"] = rewired["staticData"]
    ssh_upload((json.dumps(put_body) + "\n").encode("utf-8"),
               "/tmp/dbn_wf_put.json", "upload PUT body")
    out = ssh_run(
        f'read -r K; curl -s -m 90 -X PUT -H "X-N8N-API-KEY: $K" '
        f'-H "Content-Type: application/json" --data-binary @/tmp/dbn_wf_put.json '
        f'{API}/workflows/{wf_id}; rm -f /tmp/dbn_wf_put.json',
        key, "PUT workflow")
    putres = as_json(out, "PUT workflow")
    if putres.get("id") != wf_id:
        die(f"PUT did not return the workflow: {json.dumps(putres)[:500]}")
    print(f"7. workflow PUT OK ({len(putres.get('nodes', []))} nodes)\n")

    # --- phase 8: re-activate ---
    if was_active:
        out = ssh_run(f'read -r K; curl -s -m 25 -X POST -H "X-N8N-API-KEY: $K" '
                      f'{API}/workflows/{wf_id}/activate', key, "activate")
        act = as_json(out, "activate")
        print(f"8. re-activate: active={act.get('active')}\n")
    else:
        print("8. workflow was not active before — left inactive\n")

    # --- phase 9: verify ---
    out = ssh_run(f'read -r K; curl -s -m 25 -H "X-N8N-API-KEY: $K" {API}/workflows/{wf_id}',
                  key, "verify")
    final = as_json(out, "verify")
    fnames = {n["name"] for n in final.get("nodes", [])}
    print(f"9. VERIFY: {len(final.get('nodes', []))} nodes, active={final.get('active')}")
    print(f"   Call Hermes Bridge present: {'Call Hermes Bridge' in fnames}")
    print(f"   Call Hermes Bridge (Regen) present: {'Call Hermes Bridge (Regen)' in fnames}")
    ok = ("Call Hermes Bridge" in fnames
          and "Call Hermes Bridge (Refine)" in fnames
          and "Claude AI" not in fnames)
    print()
    print("DEPLOY OK — re-run T1 to confirm end to end." if ok
          else "x DEPLOY VERIFY FAILED — inspect; backup is in workflows/")


if __name__ == "__main__":
    main()
