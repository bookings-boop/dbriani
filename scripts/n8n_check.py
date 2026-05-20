#!/usr/bin/env python3
"""Inspect recent executions of the Phase 1B workflow via the n8n REST API
(over SSH). Used to verify deploys and during end-to-end testing.

Usage:  python3 scripts/n8n_check.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = ["ssh", "-o", "ConnectTimeout=30", "dubriani-ec2"]
WF_ID = "azPIy9OcDwiPV5uY"
WATCH = ["Call Hermes Bridge", "Parse Response", "Queue & Format",
         "Build Alert", "Alert Zayn", "Call Hermes Bridge (Regen)", "Parse Regen"]


def load_key():
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith("N8N_API_KEY="):
            return ln.split("=", 1)[1].strip()
    sys.exit("x N8N_API_KEY not in .env")


def n8n_base():
    r = subprocess.run(
        SSH + ["docker inspect n8n-n8n-1 --format "
               "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"],
        capture_output=True, text=True, timeout=30)
    ip = r.stdout.strip()
    if not ip:
        sys.exit("x could not resolve n8n container IP")
    return f"http://{ip}:5678/api/v1"


def api(path, key, base):
    r = subprocess.run(
        SSH + [f'read -r K; curl -s -m 40 -H "X-N8N-API-KEY: $K" "{base}{path}"'],
        input=key, capture_output=True, text=True, timeout=70)
    if r.returncode != 0:
        sys.exit(f"x ssh/curl failed for {path}: {r.stderr[:300]}")
    try:
        return json.loads(r.stdout)
    except Exception:
        sys.exit(f"x non-JSON for {path}: {r.stdout[:300]}")


def main():
    key = load_key()
    base = n8n_base()
    ex = api(f"/executions?workflowId={WF_ID}&limit=6&includeData=false", key, base)
    items = ex.get("data", []) if isinstance(ex, dict) else []
    print(f"recent executions ({len(items)}):")
    for e in items:
        print(f"  id={e['id']}  status={e.get('status')}  "
              f"started={e.get('startedAt')}  stopped={e.get('stoppedAt')}")
    if not items:
        print("(no executions)")
        return

    latest = items[0]["id"]
    det = api(f"/executions/{latest}?includeData=true", key, base)
    run_data = ((det.get("data", {}) or {}).get("resultData", {}) or {}).get("runData", {})
    print(f"\n=== execution {latest}: nodes that ran ({len(run_data)}) ===")
    print("  " + ", ".join(run_data.keys()))

    for nn in WATCH:
        if nn not in run_data:
            continue
        run = run_data[nn][0]
        err = run.get("error")
        out = None
        try:
            out = run["data"]["main"][0][0]["json"]
        except Exception:
            pass
        print(f"\n[{nn}]  {'!! ERROR' if err else 'ok'}")
        if err:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            print(f"  error: {str(msg)[:400]}")
        if out is not None:
            print(f"  output: {json.dumps(out, ensure_ascii=False)[:1100]}")


if __name__ == "__main__":
    main()
