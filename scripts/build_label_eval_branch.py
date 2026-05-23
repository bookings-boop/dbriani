#!/usr/bin/env python3
"""
build_label_eval_branch.py — Step 2 of pipeline-review-plan.md.

Inserts the Label Eval branch + parallel timestamp side-arms into the live
workflow:

  1. `Label Eval` — POST /label-eval right after Customer Facts.
  2. `Interrupt?` IF + `Send Sameday Alert` — Telegram alert on sameday
      booking when no draft is posted yet (parallel arm; doesn't block).
  3. `Mark Customer Msg` — POST /conversation-state customer_message
      (parallel arm off Customer Facts).
  4. `Mark Op Reply (Approved)` — POST /conversation-state operator_reply
      after Send One to Customer.
  5. `Mark Op Reply (Auto)` — POST /conversation-state operator_reply
      after Auto Send WAHA.
  6. `Mark Draft Posted` — POST /conversation-state draft_posted
      (parallel arm off Send Draft to Telegram — sets the Redis
      draft:posted:<cid> flag the sameday-interrupt check reads).

Wiring:

  Customer Facts ─┬─▶ Label Eval ─▶ Interrupt? ─┬─(yes)─▶ Send Sameday Alert
                  │                              └─(no)
                  │                                       ┌──────────┐
                  │                                       ▼          │
                  │                              Check Payment Trigger
                  │
                  └─▶ Mark Customer Msg

  Send One to Customer ───▶ Mark Op Reply (Approved)
  Auto Send WAHA       ───▶ Mark Op Reply (Auto)
  Send Draft to Telegram ─▶ Mark Draft Posted  (parallel; also continues to
                                                Save Telegram MsgID)

All HTTP nodes are `onError: continueRegularOutput` (fail-open — bridge
outage never breaks the workflow). Idempotent: re-running this script is
a no-op if the new nodes are already present.

Usage:
  python3 scripts/build_label_eval_branch.py            # dry run
  python3 scripts/build_label_eval_branch.py --deploy   # deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


BRIDGE_CRED = {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}
ADMIN_CHAT_ID = "5532831477"  # same fallback Process Text Reply uses


def _id():
    return str(uuid.uuid4())


def _http_bridge_node(name, position, url, json_body):
    """Build an httpRequest node hitting the local bridge, token-gated via
    the Hermes Bridge credential. Always continueRegularOutput."""
    return {
        "id": _id(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
        "credentials": {"httpHeaderAuth": BRIDGE_CRED},
        "parameters": {
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "method": "POST",
            "url": url,
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Content-Type", "value": "application/json"}
                ],
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json_body,
            "options": {},
        },
    }


def _telegram_send(name, position, chat_id_expr, text_expr):
    """Telegram sendMessage httpRequest node. Same pattern as Send Caps Reply."""
    return {
        "id": _id(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "Content-Type", "value": "application/json"}
                ],
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={ \"chat_id\": " + chat_id_expr +
                ", \"text\": " + text_expr + " }"
            ),
            "options": {},
        },
    }


def _if_interrupt(name, position):
    """IF node — true when Label Eval said interrupt_required."""
    return {
        "id": _id(),
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": position,
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": _id(),
                    "leftValue": "={{ $json.interrupt_required }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
    }


# -----------------------------------------------------------------------------

NODES_TO_ADD = [
    # name                      kind / params builder
    ("Label Eval",              "http"),
    ("Interrupt?",              "if"),
    ("Send Sameday Alert",      "telegram"),
    ("Mark Customer Msg",       "http"),
    ("Mark Op Reply (Approved)", "http"),
    ("Mark Op Reply (Auto)",    "http"),
    ("Mark Draft Posted",       "http"),
]


def _build_nodes(anchor_pos):
    """Build the 7 new nodes positioned relative to Customer Facts."""
    ax, ay = anchor_pos
    label_eval = _http_bridge_node(
        "Label Eval", [ax + 220, ay - 200],
        "http://172.18.0.1:8788/label-eval",
        (
            "={ \"customer_id\": {{ JSON.stringify($json.customer_phone) }}, "
            "\"latest_message\": {{ JSON.stringify($json.user_message) }}, "
            "\"skip_if_locked\": true }"
        ),
    )
    interrupt_if = _if_interrupt(
        "Interrupt?", [ax + 440, ay - 200]
    )
    sameday_alert = _telegram_send(
        "Send Sameday Alert", [ax + 660, ay - 300],
        chat_id_expr=ADMIN_CHAT_ID,
        text_expr="JSON.stringify($json.alert_text)",
    )
    mark_cmsg = _http_bridge_node(
        "Mark Customer Msg", [ax + 220, ay + 200],
        "http://172.18.0.1:8788/conversation-state",
        (
            "={ \"customer_id\": {{ JSON.stringify($json.customer_phone) }}, "
            "\"event\": \"customer_message\" }"
        ),
    )
    mark_op_approved = _http_bridge_node(
        "Mark Op Reply (Approved)", [0, 0],  # repositioned below
        "http://172.18.0.1:8788/conversation-state",
        # Send One to Customer's $json has the chatId / customer phone differently;
        # use Parse Callback's draft customer_id which is the source of truth.
        (
            "={ \"customer_id\": {{ JSON.stringify("
            "$('Parse Callback').item.json.draft && "
            "$('Parse Callback').item.json.draft.customer_id) }}, "
            "\"event\": \"operator_reply\" }"
        ),
    )
    mark_op_auto = _http_bridge_node(
        "Mark Op Reply (Auto)", [0, 0],
        "http://172.18.0.1:8788/conversation-state",
        # Auto Send WAHA fires inside the autonomous arm; the customer_id is
        # available via Customer Facts (lives upstream in the autonomous path).
        (
            "={ \"customer_id\": {{ JSON.stringify("
            "$('Customer Facts').item.json.customer_id || "
            "$json.customer_phone) }}, "
            "\"event\": \"operator_reply\" }"
        ),
    )
    mark_draft_posted = _http_bridge_node(
        "Mark Draft Posted", [0, 0],
        "http://172.18.0.1:8788/conversation-state",
        (
            "={ \"customer_id\": {{ JSON.stringify("
            "$('Customer Facts').item.json.customer_id || "
            "$json.customer_phone) }}, "
            "\"event\": \"draft_posted\" }"
        ),
    )
    return [label_eval, interrupt_if, sameday_alert, mark_cmsg,
            mark_op_approved, mark_op_auto, mark_draft_posted]


def _rewire(connections, new_nodes_by_name):
    """Splice the new nodes into the existing connections graph.

    Splices on Customer Facts:
      - existing out[0] (→ Check Payment Trigger) moves through Label Eval
        and Interrupt? (IF false output continues to original target).
      - a new parallel arm goes to Mark Customer Msg.

    Adds parallel arms on:
      - Send One to Customer  → Mark Op Reply (Approved)
      - Auto Send WAHA        → Mark Op Reply (Auto)
      - Send Draft to Telegram → Mark Draft Posted

    Returns mutated connections dict."""
    def _add_arm(src, target_name):
        node_conns = connections.setdefault(src, {})
        mains = node_conns.setdefault("main", [])
        if not mains:
            mains.append([])
        # add parallel arm to out[0]
        mains[0].append({"node": target_name, "type": "main", "index": 0})

    # --- splice Customer Facts → Label Eval → Interrupt? ---
    cf_conns = connections.setdefault("Customer Facts", {"main": [[]]})
    cf_mains = cf_conns["main"]
    # original out[0] children — should be [{node:Check Payment Trigger, ...}]
    original_children = list(cf_mains[0]) if cf_mains else []
    # rewire out[0] to just Label Eval + Mark Customer Msg
    cf_mains[0] = [
        {"node": "Label Eval", "type": "main", "index": 0},
        {"node": "Mark Customer Msg", "type": "main", "index": 0},
    ]
    # Label Eval → Interrupt?
    connections["Label Eval"] = {"main": [[
        {"node": "Interrupt?", "type": "main", "index": 0}
    ]]}
    # Interrupt? true (out[0]) → Send Sameday Alert, false (out[1]) → original
    connections["Interrupt?"] = {"main": [
        [{"node": "Send Sameday Alert", "type": "main", "index": 0}],
        original_children,
    ]}
    # Send Sameday Alert is a terminal side-arm; no outbound.
    connections.setdefault("Send Sameday Alert", {"main": [[]]})
    # Mark Customer Msg is a terminal side-arm; no outbound.
    connections.setdefault("Mark Customer Msg", {"main": [[]]})

    # --- parallel arms for the send nodes ---
    _add_arm("Send One to Customer", "Mark Op Reply (Approved)")
    _add_arm("Auto Send WAHA", "Mark Op Reply (Auto)")
    _add_arm("Send Draft to Telegram", "Mark Draft Posted")

    # Make the marker nodes terminal.
    for n in ("Mark Op Reply (Approved)", "Mark Op Reply (Auto)",
              "Mark Draft Posted"):
        connections.setdefault(n, {"main": [[]]})

    return connections


def _position_terminal_arms(nodes):
    """Place the 3 terminal marker nodes below their respective sources."""
    by_name = {n["name"]: n for n in nodes}
    pairs = [
        ("Send One to Customer",   "Mark Op Reply (Approved)"),
        ("Auto Send WAHA",         "Mark Op Reply (Auto)"),
        ("Send Draft to Telegram", "Mark Draft Posted"),
    ]
    for src, dst in pairs:
        src_node = by_name.get(src)
        dst_node = by_name.get(dst)
        if not src_node or not dst_node:
            continue
        x, y = src_node["position"]
        dst_node["position"] = [x, y + 200]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_label_eval_branch.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    existing = {x["name"] for x in wf["nodes"]}
    print(f"2. current node count: {len(wf['nodes'])}")

    to_add = [name for name, _ in NODES_TO_ADD if name not in existing]
    if not to_add:
        print("   nothing to add — all 7 nodes already present.")
        if not deploy:
            return
        # idempotent re-run might still re-wire; but we keep this branch a no-op
        # to avoid duplicating edges. Bail.
        print("   re-run idempotent: bailing without re-deploying.")
        return
    print(f"   adding: {to_add}")

    # find Customer Facts position to anchor the new nodes
    anchor = next((x for x in wf["nodes"] if x["name"] == "Customer Facts"), None)
    if anchor is None:
        raise DeployError("Customer Facts not found in workflow")
    anchor_pos = anchor["position"]

    new_nodes = _build_nodes(anchor_pos)
    new_nodes = [nn for nn in new_nodes if nn["name"] in to_add]
    wf["nodes"].extend(new_nodes)
    _position_terminal_arms(wf["nodes"])

    wf["connections"] = _rewire(wf.get("connections", {}),
                                {n["name"]: n for n in new_nodes})

    print(f"3. after add: {len(wf['nodes'])} nodes")
    print("   new wiring:")
    print("     Customer Facts ─▶ Label Eval ─▶ Interrupt? "
          "─(yes)─▶ Send Sameday Alert")
    print("                                       └─(no)─▶ Check Payment Trigger")
    print("                   └─▶ Mark Customer Msg")
    print("     Send One to Customer   ─▶ Mark Op Reply (Approved)")
    print("     Auto Send WAHA         ─▶ Mark Op Reply (Auto)")
    print("     Send Draft to Telegram ─▶ Mark Draft Posted")

    if not deploy:
        print("\nDRY RUN — re-run with --deploy.")
        return

    print("4. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="LABELBRANCH")
    final_names = {x["name"] for x in final.get("nodes", [])}
    ok = all(name in final_names for name, _ in NODES_TO_ADD)
    print(f"5. VERIFY: all 7 nodes present={ok}  "
          f"final count={len(final.get('nodes', []))}")
    print("LABEL EVAL BRANCH DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-LABELBRANCH backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
