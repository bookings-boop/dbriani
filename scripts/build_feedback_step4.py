#!/usr/bin/env python3
"""
build_feedback_step4.py — /feedback step 4: load behavioural context into
live drafts (Fetch Behavioral Context node + Claude AI system-prompt
concatenation).

Per docs/feedback-command-plan.md §5 step 4.

Without this step the /feedback rules + customer notes get *stored* but
never *reach* the drafting Claude AI node, so they're decorative. This
step makes them actually steer drafts.

Changes:
- NEW node Fetch Behavioral Context (httpRequest) — POST bridge /feedback
  action=behavioral-context with customer_id; returns {global, scenario,
  customer_notes, formatted}.
- Rewire: Format Context -> Fetch Behavioral Context -> Build Prompt.
  Build Prompt's existing $('Format Context')-based assignments are
  unaffected (explicit refs).
- Claude AI (httpRequest): system[0].text concatenates the formatted
  context after the systemPrompt — empty when nothing's stored, so the
  pre-/feedback behaviour is preserved exactly until rules exist.

Usage:
  python3 scripts/build_feedback_step4.py            # dry run
  python3 scripts/build_feedback_step4.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

FBC_BODY = (
    '={ "action": "behavioral-context", '
    '"customer_id": {{ JSON.stringify($json.customerChatId) }} }')

# Claude AI system[0].text anchor + new value
CA_OLD = '"text": {{ JSON.stringify($json.systemPrompt) }},'
CA_NEW = ('"text": {{ JSON.stringify($json.systemPrompt + '
          '($(\'Fetch Behavioral Context\').item.json.formatted ? '
          '"\\n\\n" + $(\'Fetch Behavioral Context\').item.json.formatted '
          ': "")) }},')


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def fbc_node(position):
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/feedback",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": FBC_BODY,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": str(uuid.uuid4()), "name": "Fetch Behavioral Context",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
    }


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_feedback_step4.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Fetch Behavioral Context" in by:
        print("2. already applied — Fetch Behavioral Context present.")
        return
    for req in ("Format Context", "Build Prompt", "Claude AI"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- Claude AI body: concatenate behavioural context ---
    ca = by["Claude AI"]["parameters"]
    body = ca.get("jsonBody", "")
    if "Fetch Behavioral Context" in body:
        raise DeployError("Claude AI already references Fetch Behavioral Context")
    if CA_OLD not in body:
        raise DeployError("Claude AI: systemPrompt anchor not found")
    ca["jsonBody"] = body.replace(CA_OLD, CA_NEW, 1)

    # --- add Fetch Behavioral Context node ---
    fc_pos = by["Format Context"]["position"]
    bp_pos = by["Build Prompt"]["position"]
    new_pos = [(fc_pos[0] + bp_pos[0]) // 2, fc_pos[1] + 140]
    nodes.append(fbc_node(new_pos))

    # --- rewire: Format Context -> Fetch Behavioral Context -> Build Prompt ---
    fc_conns = conns.get("Format Context", {})
    fc_main = fc_conns.get("main") or [[]]
    if not fc_main or not fc_main[0]:
        raise DeployError("Format Context has no out[0]")
    # find Build Prompt entry in Format Context's out[0]
    bp_targets = [c for c in fc_main[0] if c.get("node") == "Build Prompt"]
    if not bp_targets:
        raise DeployError("Format Context out[0] doesn't feed Build Prompt — "
                          "unexpected topology")
    for c in bp_targets:
        c["node"] = "Fetch Behavioral Context"
    conns["Fetch Behavioral Context"] = {"main": [one("Build Prompt")]}

    print(f"2. nodes: {len(nodes) - 1} -> {len(nodes)}  (+1: Fetch Behavioral Context)")
    print("3. Claude AI body: system[0].text concatenates the formatted context")
    print("4. rewired: Format Context -> Fetch Behavioral Context -> Build Prompt")

    if not deploy:
        print("\nDRY RUN — anchors matched. Re-run with --deploy.")
        return

    print("5. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="FEEDBACKS4")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = "Fetch Behavioral Context" in fnames
    print(f"6. VERIFY: Fetch Behavioral Context present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("FEEDBACK STEP 4 DEPLOYED — behavioural context now reaches live drafts."
          if ok else "x VERIFY FAILED — PRE-FEEDBACKS4 backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
