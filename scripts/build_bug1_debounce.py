#!/usr/bin/env python3
"""
build_bug1_debounce.py — BUG-1 fix: Redis-backed FR-3 debounce.

Root cause (docs/bug-diagnosis-2026-05-22.md): `Buffer Message` / `Flush Check`
coordinated via $getWorkflowStaticData, which n8n loads per-execution — the
concurrent executions one-per-inbound-message spawns never see each other's
token, so the 'latest wins' check was inert and every message produced its own
draft.

Fix — move the buffer + latest-token to Redis via the bridge /debounce
endpoint. The two code nodes keep their names (so $('Buffer Message') /
$('Flush Check') references elsewhere stay valid) and become thin shims around
two new httpRequest nodes:

  Filter Inbound -> Debounce Buffer(NEW http) -> Buffer Message(code)
    -> Debounce Wait -> Debounce Flush(NEW http) -> Flush Check(code)
    -> Get Chat History

Fail-OPEN: any bridge/Redis error -> the message is processed (a rare duplicate
draft beats a dropped customer message). Flush Check's output contract
{phone, combinedMessage, bufferedIds, bufferedCount} is unchanged, so
Format Context / Get Chat History are untouched.

2 nodes added · 2 code nodes rewritten · 4 connections rewired.
Deploys via n8n_deploy.safe_put.

Usage:
  python3 scripts/build_bug1_debounce.py            # dry run
  python3 scripts/build_bug1_debounce.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

BUFFER_BODY = (
    '={ "action": "buffer", '
    '"phone": {{ JSON.stringify($json.body.payload.from) }}, '
    '"message_id": {{ JSON.stringify($json.body.payload.id) }}, '
    '"text": {{ JSON.stringify($json.body.payload.body || "") }} }')

FLUSH_BODY = (
    '={ "action": "flush", '
    '"phone": {{ JSON.stringify($(\'Buffer Message\').item.json.phone) }}, '
    '"token": {{ JSON.stringify($(\'Buffer Message\').item.json.token) }} }')

BUFFER_MESSAGE_JS = (
    "// FR-3 (BUG-1 fix): the bridge buffered this message in Redis and issued\n"
    "// a token. staticData is NOT used — it cannot coordinate the concurrent\n"
    "// executions that one-per-inbound-message spawns.\n"
    "const wh = $('WAHA Webhook').item.json.body.payload;\n"
    "const r = $('Debounce Buffer').item.json || {};\n"
    "return { json: {\n"
    "  phone: wh.from,\n"
    "  token: r.token || '',\n"
    "  degraded: !r.token        // bridge/Redis error -> Flush Check fails open\n"
    "} };\n")

FLUSH_CHECK_JS = (
    "// FR-3 (BUG-1 fix): proceed only if the bridge says this is the latest\n"
    "// message for the customer. Redis-backed — see bridge /debounce.\n"
    "const wh = $('WAHA Webhook').item.json.body.payload;\n"
    "const bm = $('Buffer Message').item.json || {};\n"
    "const r = $('Debounce Flush').item.json || {};\n"
    "// Suppress this draft ONLY when buffering succeeded AND the bridge says a\n"
    "// newer message superseded it. Every error path falls open (process), so\n"
    "// a customer message is never silently dropped.\n"
    "if (bm.degraded !== true && r.latest === false) {\n"
    "  return [];\n"
    "}\n"
    "const combined = (r.combined && String(r.combined).trim())\n"
    "  ? r.combined\n"
    "  : (wh.body || '');\n"
    "return [{ json: {\n"
    "  phone: wh.from,\n"
    "  combinedMessage: combined,\n"
    "  bufferedIds: (Array.isArray(r.ids) && r.ids.length) ? r.ids : [wh.id],\n"
    "  bufferedCount: r.count || 1\n"
    "} }];\n")


def http_node(name, position, body):
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/debounce",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": body,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
    }


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_bug1_debounce.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Debounce Buffer" in by or "Debounce Flush" in by:
        print("2. already applied — Debounce Buffer/Flush present. nothing to do.")
        return
    for req in ("Filter Inbound", "Buffer Message", "Debounce Wait",
                "Flush Check", "Get Chat History"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # sanity: the two code nodes must still be the old staticData version
    bm_js = by["Buffer Message"]["parameters"].get("jsCode", "")
    fc_js = by["Flush Check"]["parameters"].get("jsCode", "")
    if "getWorkflowStaticData" not in bm_js or "getWorkflowStaticData" not in fc_js:
        raise DeployError("Buffer Message / Flush Check are not the expected "
                          "staticData version — aborting (already changed?)")

    # verify the current edges are exactly the linear debounce chain
    def edge(src):
        m = conns.get(src, {}).get("main") or [[]]
        return [c["node"] for c in (m[0] or [])]
    if edge("Filter Inbound") != ["Buffer Message"]:
        raise DeployError(f"Filter Inbound out0 unexpected: {edge('Filter Inbound')}")
    if edge("Debounce Wait") != ["Flush Check"]:
        raise DeployError(f"Debounce Wait out0 unexpected: {edge('Debounce Wait')}")

    # --- add the two httpRequest nodes ---
    nodes.append(http_node("Debounce Buffer", [4540, 1152], BUFFER_BODY))
    nodes.append(http_node("Debounce Flush", [4940, 1152], FLUSH_BODY))

    # --- rewrite the two code nodes ---
    by["Buffer Message"]["parameters"]["jsCode"] = BUFFER_MESSAGE_JS
    by["Flush Check"]["parameters"]["jsCode"] = FLUSH_CHECK_JS

    # --- rewire: Filter Inbound -> Debounce Buffer -> Buffer Message
    #             Debounce Wait  -> Debounce Flush  -> Flush Check ---
    conns["Filter Inbound"]["main"][0] = one("Debounce Buffer")
    conns["Debounce Buffer"] = {"main": [one("Buffer Message")]}
    conns["Debounce Wait"]["main"][0] = one("Debounce Flush")
    conns["Debounce Flush"] = {"main": [one("Flush Check")]}

    print(f"2. nodes: {len(nodes) - 2} -> {len(nodes)}  "
          "(+2: Debounce Buffer, Debounce Flush)")
    print("3. rewrote Buffer Message + Flush Check (staticData -> bridge /debounce)")
    print("4. rewired: Filter Inbound -> Debounce Buffer -> Buffer Message")
    print("            Debounce Wait  -> Debounce Flush  -> Flush Check")

    if not deploy:
        print("\nDRY RUN — anchors matched, edges confirmed. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="BUG1DEBOUNCE")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = "Debounce Buffer" in fnames and "Debounce Flush" in fnames
    print(f"6. VERIFY: Debounce nodes present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("BUG-1 DEBOUNCE DEPLOYED — debounce now coordinates via Redis."
          if ok else "x VERIFY FAILED — inspect; PRE-BUG1DEBOUNCE backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
