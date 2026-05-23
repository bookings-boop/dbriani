#!/usr/bin/env python3
"""
fix_snooze_btn_reply.py — fix the Snooze button callback reply.

Bug: Answer Snooze Btn tried to call answerCallbackQuery a second time
on the same query — but the upstream Answer Callback node already
answered it on entry. Telegram returns 400 'query is too old / query ID
is invalid'. The snooze itself succeeded; the operator just never saw
confirmation.

Fix: rewrite Answer Snooze Btn into a regular sendMessage to the admin
chat carrying $json.telegram_text ('💤 Mark snoozed for 4h'). Keep
the node name for wiring stability.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

ADMIN_CHAT_ID = "5532831477"

NEW_PARAMS = {
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
        "\"text\": {{ JSON.stringify($json.telegram_text || '💤 snoozed') }} }"
    ),
    "options": {},
}


def main():
    deploy = "--deploy" in sys.argv
    print("=== fix_snooze_btn_reply.py ===")
    n = N8N()
    wf = n.get_workflow()
    node = next((x for x in wf["nodes"] if x["name"] == "Answer Snooze Btn"), None)
    if node is None:
        raise DeployError("Answer Snooze Btn not found")
    cur_url = node["parameters"].get("url", "")
    if "/sendMessage" in cur_url:
        print("   already fixed — bailing")
        return
    node["parameters"] = NEW_PARAMS
    node["onError"] = "continueRegularOutput"
    print("   + Answer Snooze Btn rewritten as sendMessage")
    if not deploy:
        print("DRY RUN — re-run with --deploy.")
        return
    n.safe_put(wf, tag="SNOOZEBTNFIX")
    print("DEPLOYED.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
