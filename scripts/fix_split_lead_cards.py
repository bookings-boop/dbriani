#!/usr/bin/env python3
"""
fix_split_lead_cards.py — fix Split Lead Cards reading from wrong node.

The splitOut node was wired:  Send Review → Split Lead Cards
which means Split reads $json from Send Review's output — but that's
the Telegram API response {ok, result}, not the original Hermes
Review {per_lead_messages: [...]}.

Result: Split Lead Cards finds no array → 0 output items →
Send Lead Card never fires → no per-lead cards posted.

Fix: change `fieldToSplitOut` from the simple field name
'per_lead_messages' to an explicit expression referencing
$('Hermes Review').item.json.per_lead_messages. Same idea as the
existing Send Caps Reply pattern that uses $('Process Text Reply').
Apply in both the main workflow and the cron workflow.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

CRON_WF_NAME = "Dubriani Pipeline Review Cron"
NEW_FIELD = "={{ $('Hermes Review').item.json.per_lead_messages }}"


def _patch(wf, label):
    node = next((x for x in wf["nodes"] if x["name"] == "Split Lead Cards"), None)
    if node is None:
        print(f"   [{label}] Split Lead Cards not found — skip")
        return False
    cur = node["parameters"].get("fieldToSplitOut", "")
    if cur == NEW_FIELD:
        print(f"   [{label}] already fixed")
        return False
    node["parameters"]["fieldToSplitOut"] = NEW_FIELD
    print(f"   [{label}] fieldToSplitOut rewritten")
    return True


def main():
    deploy = "--deploy" in sys.argv
    print("=== fix_split_lead_cards.py ===")
    n = N8N()

    main_wf = n.get_workflow()
    if _patch(main_wf, "main"):
        if deploy:
            n.safe_put(main_wf, tag="SPLITFIX")
            print("   [main] deployed")
        else:
            print("   [main] (dry-run)")

    items = (n._api_get("/workflows", "list").get("data") or [])
    cron = next((w for w in items if w.get("name") == CRON_WF_NAME), None)
    if not cron:
        print(f"   [cron] {CRON_WF_NAME!r} not found — skip")
        return
    cron_wf = n._api_get(f"/workflows/{cron['id']}", "get-cron")
    if _patch(cron_wf, "cron"):
        if deploy:
            put_body = {
                "name": cron_wf.get("name"),
                "nodes": cron_wf.get("nodes", []),
                "connections": cron_wf.get("connections", {}),
                "settings": cron_wf.get("settings", {}),
            }
            body = json.dumps(put_body).encode("utf-8")
            n.upload(body, "/tmp/cron_split_fix.json", label="upload-cron")
            n.ssh(
                f'read -r K; curl -s -X PUT '
                f'-H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
                f'--data-binary @/tmp/cron_split_fix.json {n.api}/workflows/{cron["id"]}',
                stdin=n.key + "\n", label="cron-put", timeout=90)
            n.ssh("rm -f /tmp/cron_split_fix.json", label="rm", timeout=30)
            print("   [cron] deployed")
        else:
            print("   [cron] (dry-run)")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
