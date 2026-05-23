#!/usr/bin/env python3
"""
build_hourly_sweep.py — Step 3 of pipeline-review-plan.md.

Creates and activates a NEW n8n workflow at `workflows/pipeline-hourly-sweep.json`
that runs every hour and calls the bridge's POST /hourly-sweep endpoint. The
sweep is silent — Telegram alert only fires when transitions > 10.

Idempotent: if a workflow named 'Dubriani Pipeline Hourly Sweep' already
exists, the script bails without re-creating.

  Schedule (0 * * * * UTC)
       │
       ▼
  Hermes Hourly Sweep   (POST /hourly-sweep)
       │
       ▼
  Transitions > 10?     (IF — only post when noisy)
   ├─(yes)─▶ Send Sweep Alert    (Telegram sendMessage → admin chat)
   └─(no)──▶ (terminal — silent)

Usage:
  python3 scripts/build_hourly_sweep.py            # dry run (writes local JSON)
  python3 scripts/build_hourly_sweep.py --deploy   # creates + activates
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


WF_NAME = "Dubriani Pipeline Hourly Sweep"
LOCAL_PATH = Path(__file__).resolve().parent.parent / "workflows" / "pipeline-hourly-sweep.json"
BRIDGE_CRED = {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}
ADMIN_CHAT_ID = "5532831477"


def _id():
    return str(uuid.uuid4())


def _build_workflow():
    schedule = {
        "id": _id(),
        "name": "Schedule (Hourly)",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [200, 300],
        "parameters": {
            "rule": {
                "interval": [{"field": "cronExpression",
                              "expression": "0 * * * *"}],
            },
        },
    }
    hourly_sweep = {
        "id": _id(),
        "name": "Hermes Hourly Sweep",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [440, 300],
        "onError": "continueRegularOutput",
        "credentials": {"httpHeaderAuth": BRIDGE_CRED},
        "parameters": {
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "method": "POST",
            "url": "http://172.18.0.1:8788/hourly-sweep",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={}",
            "options": {"timeout": 60000},
        },
    }
    noisy_if = {
        "id": _id(),
        "name": "Transitions > 10?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [680, 300],
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": _id(),
                    "leftValue": "={{ ($json.transitions || 0) }}",
                    "rightValue": 10,
                    "operator": {"type": "number", "operation": "gt"},
                }],
                "combinator": "and",
            },
            "options": {},
        },
    }
    send_alert = {
        "id": _id(),
        "name": "Send Sweep Alert",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [920, 220],
        "onError": "continueRegularOutput",
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
                "\"text\": "
                "\"🪞 hourly sweep: \" + ($json.transitions || 0) + "
                "\" label transitions (scanned \" + ($json.scanned || 0) + "
                "\", elapsed \" + ($json.elapsed_ms || 0) + \"ms)\" }"
            ),
            "options": {},
        },
    }

    connections = {
        "Schedule (Hourly)": {"main": [[
            {"node": "Hermes Hourly Sweep", "type": "main", "index": 0}
        ]]},
        "Hermes Hourly Sweep": {"main": [[
            {"node": "Transitions > 10?", "type": "main", "index": 0}
        ]]},
        "Transitions > 10?": {"main": [
            [{"node": "Send Sweep Alert", "type": "main", "index": 0}],
            [],
        ]},
    }

    return {
        "name": WF_NAME,
        "nodes": [schedule, hourly_sweep, noisy_if, send_alert],
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_hourly_sweep.py ===")

    wf = _build_workflow()
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_PATH.write_text(json.dumps(wf, indent=2))
    print(f"1. local JSON written: {LOCAL_PATH.relative_to(Path.cwd())}")

    n = N8N()
    print(f"2. n8n API: {n.api}")
    items = (n._api_get("/workflows", "list").get("data") or [])
    existing = next((w for w in items if w.get("name") == WF_NAME), None)

    if existing and not deploy:
        print(f"3. workflow {WF_NAME!r} already exists "
              f"(id={existing['id']}, active={existing.get('active')})")
        print("   DRY RUN — re-run with --deploy to re-create / re-activate.")
        return
    if existing and deploy:
        print(f"3. workflow {WF_NAME!r} already exists "
              f"(id={existing['id']}); idempotent — not re-creating.")
        if not existing.get("active"):
            print("   → activating...")
            n.ssh(
                f'read -r K; curl -s -X POST '
                f'-H "X-N8N-API-KEY: $K" '
                f'{n.api}/workflows/{existing["id"]}/activate',
                stdin=n.key + "\n", label="activate", timeout=70)
            print("   activated.")
        return

    if not deploy:
        print(f"3. workflow {WF_NAME!r} does not exist.")
        print("   DRY RUN — re-run with --deploy to create + activate.")
        return

    # Create — POST /workflows. Body too large for stdin-after-read trick,
    # and --data-binary @- conflicts with `read`; upload body to a temp file
    # on the box then curl --data-binary @file.
    print(f"3. creating workflow {WF_NAME!r}...")
    body = json.dumps(wf).encode("utf-8")
    n.upload(body, "/tmp/hourly_sweep_body.json", label="upload-body")
    out = n.ssh(
        f'read -r K; curl -s -X POST '
        f'-H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
        f'--data-binary @/tmp/hourly_sweep_body.json {n.api}/workflows',
        stdin=n.key + "\n", label="create-workflow", timeout=90)
    n.ssh("rm -f /tmp/hourly_sweep_body.json", label="rm-body", timeout=30)
    try:
        created = json.loads(out)
    except Exception:
        raise DeployError(f"create-workflow non-JSON response: {out[:400]}")
    if "id" not in created:
        raise DeployError(f"create-workflow failed: {out[:400]}")
    wfid = created["id"]
    print(f"   created id={wfid}")

    # Activate
    n.ssh(
        f'read -r K; curl -s -X POST '
        f'-H "X-N8N-API-KEY: $K" '
        f'{n.api}/workflows/{wfid}/activate',
        stdin=n.key + "\n", label="activate", timeout=70)
    print("4. activated.")
    print("HOURLY SWEEP WORKFLOW DEPLOYED.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
