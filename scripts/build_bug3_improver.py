#!/usr/bin/env python3
"""
build_bug3_improver.py — BUG-3 fix: run the FR-5 improver on autonomous drafts.

Root cause (docs/bug-diagnosis-2026-05-22.md): the Mark-Auto-Sent-residual fix
made `Check Pending` return [] whenever the draft was armed, so the improver
never ran on autonomous drafts. And even without that gate, `Arm Autosend`
snapshots the draft text into the Redis autosend key at countdown start, so
the autonomous send dispatches that snapshot — improving the card alone would
be a no-op.

Fix (Option C):
  - Bridge: /autosend-state gains an `update` action — rewrites draft_text in
    the autosend:<draft_id> key (no-op if the key is absent).
  - Check Pending: drop the armed-skip — the improver runs on every draft.
  - Apply Improvement: also emit draft_id + improved_draft_text.
  - NEW node Push Improved Text: POST /autosend-state update — pushes the
    improved text into the autosend key. Auto Decide already reads draft_text
    from that key, so the autonomous send then dispatches the improved copy.
  - Check Autosend Key is now vestigial -> removed; Improve Wait -> Check
    Pending directly.

Wiring: Apply Improvement fans out to BOTH Edit Improved Card (telegram card)
and Push Improved Text (redis key) — independent, neither blocks the other.
Fail-safe: a failed update leaves the arm-time text -> no regression.

1 node removed (Check Autosend Key) · 1 added (Push Improved Text) ·
Check Pending + Apply Improvement rewritten · 3 connections rewired.

Usage:
  python3 scripts/build_bug3_improver.py            # dry run
  python3 scripts/build_bug3_improver.py --deploy
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

BRIDGE_CRED = {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}

PUSH_BODY = (
    '={ "action": "update", '
    '"draft_id": {{ JSON.stringify($json.draft_id) }}, '
    '"draft_text": {{ JSON.stringify($json.improved_draft_text) }} }')

# --- Check Pending: drop the armed-skip block -------------------------------
CP_OLD = (
    "// BUG-2 residual fix: skip autonomous drafts — they auto-send the copy\n"
    "// armed at countdown start, so improving them is moot and would edit a\n"
    "// card whose text never went out. Detect via the Redis autosend key.\n"
    "// Fail-open: a bridge error leaves __as.armed undefined -> proceed.\n"
    "const __as = $('Check Autosend Key').item.json || {};\n"
    "if (__as.armed === true) { return []; }\n")
CP_NEW = (
    "// BUG-3 fix: the improver now runs on autonomous drafts too — Push\n"
    "// Improved Text writes the improved copy into the Redis autosend key so\n"
    "// the auto-send dispatches it (was: skipped armed drafts).\n")

# --- Apply Improvement: also emit draft_id + improved_draft_text ------------
AI_OLD = (
    "  reply_markup: reply_markup\n"
    "} }];")
AI_NEW = (
    "  reply_markup: reply_markup,\n"
    "  draft_id: draftId,\n"
    "  improved_draft_text: d.draft_text\n"
    "} }];")

# --- Mark Auto Sent: refresh the now-stale comment (no functional change) ---
MAS_OLD = (
    "// BUG-2 residual fix: the old $getWorkflowStaticData('global') write of\n"
    "// d.status='sent' was unreliable (concurrent-execution snapshot race) and is\n"
    "// no longer needed — the FR-5 improver now skips autonomous drafts via the\n"
    "// Redis autosend key (see Check Autosend Key). d.auto_sent and\n"
    "// d.messages_sent_count had zero readers, so they are dropped too.")
MAS_NEW = (
    "// The old $getWorkflowStaticData('global') write of d.status='sent' was\n"
    "// unreliable (concurrent-execution snapshot race) and had no readers, so\n"
    "// it is gone — along with the unused d.auto_sent / d.messages_sent_count.")


def push_node(position):
    return {
        "parameters": {
            "method": "POST",
            "url": "http://172.18.0.1:8788/autosend-state",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": PUSH_BODY,
            "options": {},
        },
        "credentials": BRIDGE_CRED,
        "id": str(uuid.uuid4()), "name": "Push Improved Text",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
    }


def one(node):
    return [{"node": node, "type": "main", "index": 0}]


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_bug3_improver.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})
    by = {x["name"]: x for x in nodes}

    if "Push Improved Text" in by:
        print("2. already applied — 'Push Improved Text' present. nothing to do.")
        return
    for req in ("Improve Wait", "Check Autosend Key", "Check Pending",
                "Hermes Improve", "Apply Improvement", "Edit Improved Card",
                "Mark Auto Sent"):
        if req not in by:
            raise DeployError(f"required node {req!r} not found")

    # no node may make a live $('Check Autosend Key') reference (a bare
    # mention in a comment is fine — Mark Auto Sent's stale comment is
    # refreshed below)
    import json as _json
    refs = [x["name"] for x in nodes
            if x["name"] != "Check Pending"
            and "$('Check Autosend Key')" in _json.dumps(x.get("parameters", {}))]
    if refs:
        raise DeployError(f"Check Autosend Key still referenced by {refs} "
                          "— aborting")

    # --- Check Pending: drop the armed-skip ---
    cp = by["Check Pending"]["parameters"]
    if CP_OLD not in cp.get("jsCode", ""):
        raise DeployError("Check Pending: armed-skip anchor not found — aborting")
    cp["jsCode"] = cp["jsCode"].replace(CP_OLD, CP_NEW, 1)

    # --- Apply Improvement: emit draft_id + improved_draft_text ---
    ai = by["Apply Improvement"]["parameters"]
    if AI_OLD not in ai.get("jsCode", ""):
        raise DeployError("Apply Improvement: return anchor not found — aborting")
    ai["jsCode"] = ai["jsCode"].replace(AI_OLD, AI_NEW, 1)

    # --- Mark Auto Sent: refresh the stale comment (comment-only) ---
    mas = by["Mark Auto Sent"]["parameters"]
    if MAS_OLD not in mas.get("jsCode", ""):
        raise DeployError("Mark Auto Sent: stale-comment anchor not found "
                          "— aborting")
    mas["jsCode"] = mas["jsCode"].replace(MAS_OLD, MAS_NEW, 1)

    # --- add Push Improved Text node ---
    nodes.append(push_node([6968, 1180]))

    # --- remove Check Autosend Key node ---
    wf["nodes"] = [x for x in nodes if x["name"] != "Check Autosend Key"]

    # --- rewire ---
    # Improve Wait -> Check Pending  (was -> Check Autosend Key)
    conns["Improve Wait"]["main"][0] = one("Check Pending")
    conns.pop("Check Autosend Key", None)
    # Apply Improvement -> [Edit Improved Card, Push Improved Text]
    conns["Apply Improvement"]["main"][0] = [
        {"node": "Edit Improved Card", "type": "main", "index": 0},
        {"node": "Push Improved Text", "type": "main", "index": 0},
    ]

    print(f"2. nodes: {len(nodes)} -> {len(wf['nodes'])}  "
          "(-Check Autosend Key, +Push Improved Text)")
    print("3. Check Pending: armed-skip removed (improver runs on autonomous)")
    print("4. Apply Improvement: emits draft_id + improved_draft_text")
    print("5. rewired: Improve Wait -> Check Pending;")
    print("            Apply Improvement -> [Edit Improved Card, Push Improved Text]")

    if not deploy:
        print("\nDRY RUN — all anchors matched. Nothing deployed. "
              "Re-run with --deploy.")
        return

    print("6. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="BUG3IMPROVER")
    fnames = {x["name"] for x in final.get("nodes", [])}
    ok = ("Push Improved Text" in fnames
          and "Check Autosend Key" not in fnames)
    print(f"7. VERIFY: Push Improved Text present + Check Autosend Key gone={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("BUG-3 IMPROVER DEPLOYED — improver now runs on autonomous drafts."
          if ok else "x VERIFY FAILED — inspect; PRE-BUG3IMPROVER backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
