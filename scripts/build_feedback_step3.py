#!/usr/bin/env python3
"""
build_feedback_step3.py — /feedback step 3: callback handlers (Yes / Modify /
No) + save / discard + result-card edit.

Per docs/feedback-command-plan.md §5 step 3.

Changes:
- Parse Callback (jsCode): set draft_found = true for any action starting
  with "feedback_" (feedback callbacks aren't about a pendingQueue draft
  but the existing Draft Exists? gate checks draft_found).
- Route Action (switch): three new rules for feedback_yes / feedback_modify
  / feedback_no.
- 6 NEW nodes (3 pairs, one per outcome):
    Feedback Save    + Edit Feedback Saved      (POST /feedback action=save;
                                                 then editMessageText "✅
                                                 saved as ...")
    Feedback Modify  + Edit Feedback Modify     (POST action=discard; then
                                                 "✏️ Discarded. Re-type
                                                 /feedback <text>.")
    Feedback Discard + Edit Feedback Discarded  (POST action=discard; then
                                                 "❌ Discarded.")

Topology:
  Route Action [feedback_yes]    -> Feedback Save    -> Edit Feedback Saved
  Route Action [feedback_modify] -> Feedback Modify  -> Edit Feedback Modify
  Route Action [feedback_no]     -> Feedback Discard -> Edit Feedback Discarded

Usage:
  python3 scripts/build_feedback_step3.py            # dry run
  python3 scripts/build_feedback_step3.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

# --- Parse Callback: pass feedback callbacks through Draft Exists? --------
PC_OLD = (
    "    draft: draft || null,\n"
    "    draft_found: !!draft\n"
    "  }\n"
    "};")
PC_NEW = (
    "    draft: draft || null,\n"
    "    // feedback callbacks aren't about a pendingQueue draft — let the\n"
    "    // existing Draft Exists? IF pass them through.\n"
    "    draft_found: !!draft || (action || '').indexOf('feedback_') === 0\n"
    "  }\n"
    "};")

SAVE_BODY = (
    '={ "action": "save", '
    '"proposal_id": {{ JSON.stringify($(\'Parse Callback\').item.json.draft_id) }} }')

DISCARD_BODY = (
    '={ "action": "discard", '
    '"proposal_id": {{ JSON.stringify($(\'Parse Callback\').item.json.draft_id) }} }')

# editMessageText bodies — clear inline keyboard so buttons can't be re-tapped
EDIT_SAVED_BODY = (
    '={ "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }}, '
    '"message_id": {{ $(\'Parse Callback\').item.json.message_id }}, '
    '"text": {{ JSON.stringify(($json.ok === true) '
    '? ("✅ Saved as " + ($json.classification || "rule") '
    '+ ":\\n→ " + ($json.summary || "")) '
    ': ("⚠️ Save failed: " + ($json.error || "unknown error"))) }}, '
    '"reply_markup": { "inline_keyboard": [] } }')

EDIT_MODIFY_BODY = (
    '={ "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }}, '
    '"message_id": {{ $(\'Parse Callback\').item.json.message_id }}, '
    '"text": {{ JSON.stringify("✏️ Discarded. Re-type with /feedback '
    '<updated text> when ready.") }}, '
    '"reply_markup": { "inline_keyboard": [] } }')

EDIT_DISCARD_BODY = (
    '={ "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }}, '
    '"message_id": {{ $(\'Parse Callback\').item.json.message_id }}, '
    '"text": {{ JSON.stringify("❌ Discarded.") }}, '
    '"reply_markup": { "inline_keyboard": [] } }')


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def bridge_http(name, body, position):
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/feedback",
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


def telegram_edit(name, body, position):
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/editMessageText",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": body,
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
    }


def feedback_rule(key):
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose", "version": 1},
            "conditions": [{
                "id": str(uuid.uuid4()),
                "leftValue": "={{ $json.action }}",
                "rightValue": key,
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": key,
    }


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_feedback_step3.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Feedback Save" in by:
        print("2. already applied — Feedback Save present.")
        return
    for req in ("Parse Callback", "Route Action", "Hermes Feedback Classify"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # --- Parse Callback: draft_found passes feedback callbacks ---
    pc = by["Parse Callback"]["parameters"]
    js = pc.get("jsCode", "")
    if "feedback_" in js:
        raise DeployError("Parse Callback already mentions feedback_")
    if PC_OLD not in js:
        raise DeployError("Parse Callback: draft_found anchor not found")
    pc["jsCode"] = js.replace(PC_OLD, PC_NEW, 1)

    # --- Route Action: append 3 new rules ---
    ra = by["Route Action"]["parameters"]
    rules = ra.setdefault("rules", {}).setdefault("values", [])
    existing_keys = {r.get("outputKey") for r in rules}
    new_idxs = {}
    for key in ("feedback_yes", "feedback_modify", "feedback_no"):
        if key in existing_keys:
            raise DeployError(f"Route Action already has {key}")
        new_idxs[key] = len(rules)
        rules.append(feedback_rule(key))

    # --- 6 new nodes ---
    # arrange them in a stack below the existing Route Action region
    base_y = 2400
    nodes.append(bridge_http("Feedback Save",    SAVE_BODY,    [6240, base_y]))
    nodes.append(telegram_edit("Edit Feedback Saved",      EDIT_SAVED_BODY,
                               [6500, base_y]))
    nodes.append(bridge_http("Feedback Modify",  DISCARD_BODY, [6240, base_y + 140]))
    nodes.append(telegram_edit("Edit Feedback Modify",     EDIT_MODIFY_BODY,
                               [6500, base_y + 140]))
    nodes.append(bridge_http("Feedback Discard", DISCARD_BODY, [6240, base_y + 280]))
    nodes.append(telegram_edit("Edit Feedback Discarded",  EDIT_DISCARD_BODY,
                               [6500, base_y + 280]))

    # --- wire connections ---
    ra_conns = conns.setdefault("Route Action", {})
    main = ra_conns.setdefault("main", [])
    while len(main) <= max(new_idxs.values()):
        main.append([])
    main[new_idxs["feedback_yes"]]    = one("Feedback Save")
    main[new_idxs["feedback_modify"]] = one("Feedback Modify")
    main[new_idxs["feedback_no"]]     = one("Feedback Discard")
    conns["Feedback Save"]    = {"main": [one("Edit Feedback Saved")]}
    conns["Feedback Modify"]  = {"main": [one("Edit Feedback Modify")]}
    conns["Feedback Discard"] = {"main": [one("Edit Feedback Discarded")]}

    print(f"2. nodes: {len(nodes) - 6} -> {len(nodes)}  (+6 nodes)")
    print("3. Parse Callback: draft_found passes feedback_* callbacks")
    print(f"4. Route Action: new rules — feedback_yes #{new_idxs['feedback_yes']}, "
          f"feedback_modify #{new_idxs['feedback_modify']}, "
          f"feedback_no #{new_idxs['feedback_no']}")
    print("5. wired:")
    print("     Route Action[yes]    -> Feedback Save    -> Edit Feedback Saved")
    print("     Route Action[modify] -> Feedback Modify  -> Edit Feedback Modify")
    print("     Route Action[no]     -> Feedback Discard -> Edit Feedback Discarded")

    if not deploy:
        print("\nDRY RUN — anchors matched. Re-run with --deploy.")
        return

    print("6. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="FEEDBACKS3")
    fnames = {x["name"] for x in final.get("nodes", [])}
    expected = ("Feedback Save", "Feedback Modify", "Feedback Discard",
                "Edit Feedback Saved", "Edit Feedback Modify",
                "Edit Feedback Discarded")
    ok = all(x in fnames for x in expected)
    print(f"7. VERIFY: 6 nodes present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("FEEDBACK STEP 3 DEPLOYED."
          if ok else "x VERIFY FAILED — PRE-FEEDBACKS3 backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
