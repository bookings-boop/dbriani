#!/usr/bin/env python3
"""
fix_review_jsonbody.py — fix Step 5's broken Telegram nodes.

Bug: build_review_step5.py built jsonBody strings with JavaScript
expressions (JSON.stringify(...), $(...).item.json...) NOT wrapped in
n8n's {{ }} template syntax. n8n treated them as literal text, so the
HTTP node failed with 'The value in the "JSON Body" field is not valid
JSON'. Affects 7 nodes:

  Send Review, Send Info Reply, Send Label Reply, Send Snooze Reply,
  Send Nudge Draft, Send Info Btn Reply, Answer Snooze Btn.

The fix rewrites each node's jsonBody with the same content but with
proper {{ }} wrapping around JavaScript expressions.

Idempotent — re-running is a no-op once the bodies are correct.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


ADMIN_CHAT_ID = "5532831477"

# (node name) → (new jsonBody)
FIXED_BODIES = {
    "Send Review": (
        "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
        "\"text\": {{ JSON.stringify($json.telegram_text) }}, "
        "\"parse_mode\": \"Markdown\", "
        "\"reply_markup\": "
        "{{ JSON.stringify({ inline_keyboard: ($json.inline_keyboards || []) }) }} }"
    ),
    "Send Info Reply": (
        "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
        "\"text\": {{ JSON.stringify($json.telegram_text) }}, "
        "\"parse_mode\": \"Markdown\" }"
    ),
    "Send Label Reply": (
        "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
        "\"text\": {{ JSON.stringify($json.telegram_text) }}, "
        "\"parse_mode\": \"Markdown\" }"
    ),
    "Send Snooze Reply": (
        "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
        "\"text\": {{ JSON.stringify($json.telegram_text) }} }"
    ),
    "Send Nudge Draft": (
        "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
        "\"text\": {{ JSON.stringify('💡 *NUDGE DRAFT* for `' + "
        "$('Parse Callback').item.json.customer_id + '`  (' + "
        "($json.label || '?') + ')\\n\\n' + "
        "($json.draft_text || '⚠️ no draft generated')) }}, "
        "\"parse_mode\": \"Markdown\" }"
    ),
    "Send Info Btn Reply": (
        "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
        "\"text\": {{ JSON.stringify($json.telegram_text) }}, "
        "\"parse_mode\": \"Markdown\" }"
    ),
    "Answer Snooze Btn": (
        "={ \"callback_query_id\": "
        "{{ $('Parse Callback').item.json.callback_query_id }}, "
        "\"text\": {{ JSON.stringify($json.telegram_text || '💤 snoozed') }} }"
    ),
}


def main():
    deploy = "--deploy" in sys.argv
    print("=== fix_review_jsonbody.py ===")
    n = N8N()
    wf = n.get_workflow()
    fixed = 0
    skipped = 0
    for name, new_body in FIXED_BODIES.items():
        node = next((x for x in wf["nodes"] if x["name"] == name), None)
        if node is None:
            print(f"   ? {name} not found — skipped")
            continue
        cur = node["parameters"].get("jsonBody", "")
        if cur == new_body:
            print(f"   = {name} already fixed")
            skipped += 1
            continue
        node["parameters"]["jsonBody"] = new_body
        print(f"   + {name} body rewritten")
        fixed += 1
    if fixed == 0:
        print(f"\nNothing to do — {skipped} nodes already correct.")
        return
    if not deploy:
        print(f"\n{fixed} node(s) would be fixed. DRY RUN — re-run with --deploy.")
        return
    print(f"\nDeploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="JSONBODYFIX")
    bad = []
    for name, expected in FIXED_BODIES.items():
        fn = next((x for x in final.get("nodes", []) if x["name"] == name), None)
        if fn is None:
            continue
        if fn["parameters"].get("jsonBody", "") != expected:
            bad.append(name)
    print(f"VERIFY: all {fixed} fixes applied? {not bad}")
    if bad:
        print(f"  still wrong: {bad}")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
