#!/usr/bin/env python3
"""
build_review_cron.py — Step 5 part 2 of pipeline-review-plan.md.

Creates and activates `workflows/pipeline-review-cron.json` — a NEW
workflow with two daily Schedule Triggers (09:00 + 17:00 Dubai = UTC
05:00 + 13:00) that call POST /review and post the report to admin chat.

Idempotent: bails if a workflow named 'Dubriani Pipeline Review Cron'
already exists (matching the Hourly Sweep pattern).
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


WF_NAME = "Dubriani Pipeline Review Cron"
LOCAL_PATH = Path(__file__).resolve().parent.parent / "workflows" / "pipeline-review-cron.json"
BRIDGE_CRED = {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}
ADMIN_CHAT_ID = "5532831477"


def _id():
    return str(uuid.uuid4())


def _build_workflow():
    # Two Schedule Triggers at 09:00 + 17:00 Dubai (UTC+4 year-round, no DST).
    schedule_morning = {
        "id": _id(),
        "name": "Schedule 09:00 Dubai",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [200, 200],
        "parameters": {
            "rule": {
                "interval": [{"field": "cronExpression",
                              "expression": "0 5 * * *"}],
            },
        },
    }
    schedule_evening = {
        "id": _id(),
        "name": "Schedule 17:00 Dubai",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [200, 400],
        "parameters": {
            "rule": {
                "interval": [{"field": "cronExpression",
                              "expression": "0 13 * * *"}],
            },
        },
    }
    hermes_review = {
        "id": _id(),
        "name": "Hermes Review",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [460, 300],
        "onError": "continueRegularOutput",
        "credentials": {"httpHeaderAuth": BRIDGE_CRED},
        "parameters": {
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "method": "POST",
            "url": "http://172.18.0.1:8788/review",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={ \"mode\": \"scheduled\" }",
            "options": {"timeout": 90000},
        },
    }
    send_review = {
        "id": _id(),
        "name": "Send Review",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [720, 300],
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
                "\"text\": " + "{{ JSON.stringify($json.telegram_text) }}" + ", "
                "\"parse_mode\": \"Markdown\", "
                "\"reply_markup\": "
                + "{{ JSON.stringify({ inline_keyboard: ($json.inline_keyboards || []) }) }}"
                + " }"
            ),
            "options": {},
        },
    }

    connections = {
        "Schedule 09:00 Dubai": {"main": [[
            {"node": "Hermes Review", "type": "main", "index": 0}
        ]]},
        "Schedule 17:00 Dubai": {"main": [[
            {"node": "Hermes Review", "type": "main", "index": 0}
        ]]},
        "Hermes Review": {"main": [[
            {"node": "Send Review", "type": "main", "index": 0}
        ]]},
    }
    return {
        "name": WF_NAME,
        "nodes": [schedule_morning, schedule_evening, hermes_review, send_review],
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_review_cron.py ===")
    wf = _build_workflow()
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_PATH.write_text(json.dumps(wf, indent=2))
    print(f"1. local JSON written: {LOCAL_PATH.name}")

    n = N8N()
    items = (n._api_get("/workflows", "list").get("data") or [])
    existing = next((w for w in items if w.get("name") == WF_NAME), None)

    if existing:
        print(f"2. workflow {WF_NAME!r} exists "
              f"(id={existing['id']}, active={existing.get('active')})")
        if deploy and not existing.get("active"):
            n.ssh(
                f'read -r K; curl -s -X POST '
                f'-H "X-N8N-API-KEY: $K" '
                f'{n.api}/workflows/{existing["id"]}/activate',
                stdin=n.key + "\n", label="activate", timeout=70)
            print("   activated.")
        return

    if not deploy:
        print(f"2. workflow {WF_NAME!r} does not exist — DRY RUN.")
        return

    print(f"2. creating workflow {WF_NAME!r}...")
    body = json.dumps(wf).encode("utf-8")
    n.upload(body, "/tmp/review_cron_body.json", label="upload-body")
    out = n.ssh(
        f'read -r K; curl -s -X POST '
        f'-H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
        f'--data-binary @/tmp/review_cron_body.json {n.api}/workflows',
        stdin=n.key + "\n", label="create-workflow", timeout=90)
    n.ssh("rm -f /tmp/review_cron_body.json", label="rm-body", timeout=30)
    try:
        created = json.loads(out)
    except Exception:
        raise DeployError(f"create non-JSON: {out[:400]}")
    if "id" not in created:
        raise DeployError(f"create failed: {out[:400]}")
    wfid = created["id"]
    print(f"   created id={wfid}")

    n.ssh(
        f'read -r K; curl -s -X POST '
        f'-H "X-N8N-API-KEY: $K" '
        f'{n.api}/workflows/{wfid}/activate',
        stdin=n.key + "\n", label="activate", timeout=70)
    print("3. activated.")
    print("REVIEW CRON DEPLOYED.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
