#!/usr/bin/env python3
"""deploy_fromme_record.py — Wave 4 (Bug #1): capture operator DIRECT phone
replies (WAHA fromMe) into the durable store + bump the owe-reply clock.

ADDITIVE n8n change via safe_put (preserves the live draft queue):
  * NEW IF node  "Filter Op Reply fromMe":
        event == 'message.any'  AND  payload.fromMe == true
        AND payload.to ~ @(c.us|lid)$  AND (body non-empty OR hasMedia)
    NB: for a fromMe (outbound) message WAHA sets from=<our @lid>, to=<CUSTOMER>
    — so the customer is payload.TO, not payload.from (the legacy dead node had
    this wrong; it never fired so the bug never surfaced).
  * NEW httpRequest node "Record Op Reply fromMe":
        POST http://172.18.0.1:8788/record-message
        { customer_id: payload.to, direction:'out', body, msg_id:id, ts:timestamp }
    (same "Hermes Bridge" header-auth credential as the existing node.)
  * WAHA Webhook -> Filter Op Reply fromMe -> (true) Record Op Reply fromMe.
  * DISABLE the legacy dead "Record Op Reply" (-> /conversation-state
    {event:'operator_reply'}) so it can never fire its reengage-zeroing event.

Requires WAHA WHATSAPP_HOOK_EVENTS to include 'message.any' to receive fromMe
events — done separately. Inert until then (the new filter needs message.any).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

FILTER_NODE = {
    "parameters": {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose", "version": 1},
            "conditions": [
                {"id": "fm-frommetrue",
                 "leftValue": "={{ $json.body.payload.fromMe }}",
                 "rightValue": True,
                 "operator": {"type": "boolean", "operation": "true"}},
                {"id": "fm-eventany",
                 "leftValue": "={{ $json.body.event }}",
                 "rightValue": "message.any",
                 "operator": {"type": "string", "operation": "equals"}},
                {"id": "fm-to-regex",
                 "leftValue": "={{ $json.body.payload.to }}",
                 "rightValue": "@(c\\.us|lid)$",
                 "operator": {"type": "string", "operation": "regex"}},
                {"id": "fm-content",
                 "leftValue": "={{ (($json.body.payload.body || '').trim().length > 0) "
                              "|| (!!$json.body.payload.hasMedia) }}",
                 "rightValue": True,
                 "operator": {"type": "boolean", "operation": "true"}},
            ],
            "combinator": "and",
        },
        "options": {},
    },
    "type": "n8n-nodes-base.if",
    "typeVersion": 2,
    "position": [4432, 1360],
    "id": "fromme-filter-20260612",
    "name": "Filter Op Reply fromMe",
}

RECORD_NODE = {
    "parameters": {
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "method": "POST",
        "url": "http://172.18.0.1:8788/record-message",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [{"name": "Content-Type", "value": "application/json"}]},
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": (
            '={ "customer_id": {{ JSON.stringify($json.body.payload.to) }}, '
            '"direction": "out", '
            '"body": {{ JSON.stringify($json.body.payload.body || "") }}, '
            '"msg_id": {{ JSON.stringify($json.body.payload.id || "") }}, '
            '"ts": {{ $json.body.payload.timestamp || 0 }} }'),
        "options": {},
    },
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [4672, 1360],
    "id": "fromme-record-20260612",
    "name": "Record Op Reply fromMe",
    "credentials": CRED,
}


def main():
    n = N8N()
    wf = n.get_workflow()
    names = {nd["name"] for nd in wf["nodes"]}
    if "Filter Op Reply fromMe" in names or "Record Op Reply fromMe" in names:
        raise DeployError("fromMe nodes already present — aborting (idempotency)")
    if "WAHA Webhook" not in names:
        raise DeployError("WAHA Webhook node not found")

    # add nodes
    wf["nodes"].append(FILTER_NODE)
    wf["nodes"].append(RECORD_NODE)

    # disable the legacy dead "Record Op Reply" (-> /conversation-state)
    for nd in wf["nodes"]:
        if nd["name"] == "Record Op Reply":
            nd["disabled"] = True
            print("   disabled legacy 'Record Op Reply' node")

    conns = wf["connections"]
    # WAHA Webhook (output 0) -> Filter Op Reply fromMe
    wh = conns.setdefault("WAHA Webhook", {}).setdefault("main", [])
    while len(wh) < 1:
        wh.append([])
    wh[0].append({"node": "Filter Op Reply fromMe", "type": "main", "index": 0})
    # Filter Op Reply fromMe (TRUE / output 0) -> Record Op Reply fromMe
    conns["Filter Op Reply fromMe"] = {
        "main": [[{"node": "Record Op Reply fromMe", "type": "main", "index": 0}]]}

    print(f"   nodes now: {len(wf['nodes'])} (added 2)")
    print("   WAHA Webhook -> " +
          ", ".join(c["node"] for c in wh[0]))
    n.safe_put(wf, tag="FROMME_RECORD")
    print("✅ fromMe record branch deployed")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x deploy failed: {e}")
