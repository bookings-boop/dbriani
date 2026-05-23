#!/usr/bin/env python3
"""
fix_review_per_lead_cards.py — UX fix for the /review report.

Bug 1 from live test: inline-keyboard buttons all stack at the bottom of
one big message instead of sitting with each lead. That's how Telegram
inline_keyboard works (one keyboard per message), so the fix is to post
one message per lead — header first, then one card per lead carrying
its own 3-button keyboard.

What this script does in both workflows that post /review reports:
  1. Modify `Send Review` jsonBody to send ONLY $json.header_text (no
     reply_markup) — the summary header line.
  2. Insert `Split Lead Cards` (splitOut node) reading $json.per_lead_messages.
  3. Insert `Send Lead Card` (httpRequest sendMessage) — fires once per
     card with $json.text + reply_markup from $json.inline_keyboard.

Affects:
  - Main workflow (azPIy9OcDwiPV5uY): Send Review on the /review-command path.
  - Review Cron workflow (8vDbQdWV20ZBsdNx): Send Review on the schedule path.

Idempotent: re-running is a no-op if the new nodes already exist.
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


ADMIN_CHAT_ID = "5532831477"
CRON_WF_NAME = "Dubriani Pipeline Review Cron"


def _id():
    return str(uuid.uuid4())


HEADER_BODY = (
    "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
    "\"text\": {{ JSON.stringify($json.header_text || $json.telegram_text) }}, "
    "\"parse_mode\": \"Markdown\" }"
)

LEAD_CARD_BODY = (
    "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
    "\"text\": {{ JSON.stringify($json.text) }}, "
    "\"parse_mode\": \"Markdown\", "
    "\"reply_markup\": "
    "{{ JSON.stringify({ inline_keyboard: ($json.inline_keyboard || []) }) }}"
    " }"
)


def _split_lead_cards_node(position):
    return {
        "id": _id(),
        "name": "Split Lead Cards",
        "type": "n8n-nodes-base.splitOut",
        "typeVersion": 1,
        "position": position,
        "parameters": {
            "fieldToSplitOut": "per_lead_messages",
            "options": {},
        },
    }


def _send_lead_card_node(position):
    return {
        "id": _id(),
        "name": "Send Lead Card",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
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
            "jsonBody": LEAD_CARD_BODY,
            "options": {},
        },
    }


def _patch_workflow(wf, label):
    """Patch a workflow that has Hermes Review → Send Review into:
       Hermes Review → Send Review (header only) → Split Lead Cards → Send Lead Card.
    Idempotent."""
    names = {n["name"] for n in wf["nodes"]}
    if "Send Review" not in names:
        print(f"   [{label}] Send Review not found — skipping")
        return False
    if "Send Lead Card" in names and "Split Lead Cards" in names:
        print(f"   [{label}] already patched")
        return False

    # 1) Update Send Review's jsonBody to send header_text only, no kb.
    sr = next(n for n in wf["nodes"] if n["name"] == "Send Review")
    sr["parameters"]["jsonBody"] = HEADER_BODY
    print(f"   [{label}] Send Review rewritten (header-only)")

    # 2) Insert Split + Send Lead Card.
    sr_x, sr_y = sr.get("position", [0, 0])
    split = _split_lead_cards_node([sr_x + 240, sr_y])
    send_card = _send_lead_card_node([sr_x + 480, sr_y])
    if "Split Lead Cards" not in names:
        wf["nodes"].append(split)
        print(f"   [{label}] + Split Lead Cards")
    if "Send Lead Card" not in names:
        wf["nodes"].append(send_card)
        print(f"   [{label}] + Send Lead Card")

    # 3) Wire Send Review → Split Lead Cards → Send Lead Card.
    conns = wf.setdefault("connections", {})
    conns.setdefault("Send Review", {"main": [[]]})
    sr_mains = conns["Send Review"]["main"]
    if not sr_mains:
        sr_mains.append([])
    already = any(d.get("node") == "Split Lead Cards" for d in sr_mains[0])
    if not already:
        sr_mains[0].append({"node": "Split Lead Cards", "type": "main", "index": 0})

    conns.setdefault("Split Lead Cards", {"main": [[]]})
    sp_mains = conns["Split Lead Cards"]["main"]
    if not sp_mains:
        sp_mains.append([])
    already = any(d.get("node") == "Send Lead Card" for d in sp_mains[0])
    if not already:
        sp_mains[0].append({"node": "Send Lead Card", "type": "main", "index": 0})

    return True


def main():
    deploy = "--deploy" in sys.argv
    print("=== fix_review_per_lead_cards.py ===")
    n = N8N()

    # --- main workflow ---
    main_wf = n.get_workflow()
    patched_main = _patch_workflow(main_wf, "main")
    if patched_main:
        if deploy:
            n.safe_put(main_wf, tag="PERLEADCARDS")
            print("   [main] deployed")
        else:
            print("   [main] (dry-run, would deploy)")
    else:
        print("   [main] no changes needed")

    # --- cron workflow ---
    items = (n._api_get("/workflows", "list").get("data") or [])
    cron = next((w for w in items if w.get("name") == CRON_WF_NAME), None)
    if not cron:
        print(f"   [cron] {CRON_WF_NAME!r} not found — skipping")
        return
    cron_wf = n._api_get(f"/workflows/{cron['id']}", "get-cron")
    patched_cron = _patch_workflow(cron_wf, "cron")
    if patched_cron:
        if deploy:
            # Cron workflow has no staticData hence safe_put would still work
            # but be heavy-handed — do a direct PUT.
            put_body = {
                "name": cron_wf.get("name"),
                "nodes": cron_wf.get("nodes", []),
                "connections": cron_wf.get("connections", {}),
                "settings": cron_wf.get("settings", {}),
            }
            body = json.dumps(put_body).encode("utf-8")
            n.upload(body, "/tmp/cron_put.json", label="upload-cron")
            n.ssh(
                f'read -r K; curl -s -X PUT '
                f'-H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
                f'--data-binary @/tmp/cron_put.json {n.api}/workflows/{cron["id"]}',
                stdin=n.key + "\n", label="cron-put", timeout=90)
            n.ssh("rm -f /tmp/cron_put.json", label="rm", timeout=30)
            print("   [cron] deployed")
        else:
            print("   [cron] (dry-run, would deploy)")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
