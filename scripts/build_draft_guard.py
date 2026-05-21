#!/usr/bin/env python3
"""
build_draft_guard.py — stop Send / 🤖 Auto / Skip / Regen / Edit crashing
on an expired (orphaned) draft card.  (docs/STATUS.md issue #3)

THE PROBLEM
  Callback chain today:  Parse Callback -> Answer Callback -> Route Action
  Route Action fans to Prepare Send / Edit Telegram (Skip) / Set Awaiting
  Edit / Prep Regen / Set Auto Mode / Set Manual Mode — every one of them
  dereferences  $('Parse Callback').item.json.draft.<field>.
  When the tapped card's draft is no longer in the queue, Parse Callback
  sets draft=null / draft_found=false, and the handler crashes on null.

THE FIX  (one guard protects all six actions)
  Insert an IF node "Draft Exists?" between Answer Callback and Route Action:
    Answer Callback -> Draft Exists?
       true  (draft_found) -> Route Action          (unchanged behaviour)
       false               -> Notify Draft Expired  (edits the dead card)
  "Notify Draft Expired" edits the tapped card to a clear notice and drops
  its buttons — no handler ever runs on a null draft again.

  2 new nodes, 1 connection rewired. 82 -> 84 nodes.

Deploys via scripts/n8n_deploy.py (safe_put — re-fetches staticData, no
queue clobber).

Usage:
  python3 scripts/build_draft_guard.py            # dry run — prints the plan
  python3 scripts/build_draft_guard.py --deploy   # build + safe_put
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402

GUARD = "Draft Exists?"
NOTICE = "Notify Draft Expired"
EXPIRED_TEXT = ("⚠️ DRAFT EXPIRED — this card rolled out of the queue, so its "
                "buttons no longer work. ask the customer to resend their "
                "last message and a fresh draft will appear here.")


def gid():
    return str(uuid.uuid4())


def build_nodes():
    guard = {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": gid(),
                    "leftValue": "={{ $('Parse Callback').item.json.draft_found }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "id": gid(),
        "name": GUARD,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [5168, 1296],
    }
    notice = {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}"
                   "/editMessageText",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={\n"
                '  "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }},\n'
                '  "message_id": {{ $(\'Parse Callback\').item.json.message_id }},\n'
                '  "text": {{ JSON.stringify(' + json.dumps(EXPIRED_TEXT) + ') }}\n'
                "}"),
            "options": {},
        },
        "id": gid(),
        "name": NOTICE,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [5424, 1296],
    }
    return guard, notice


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_draft_guard.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    nodes = wf["nodes"]
    conns = wf.setdefault("connections", {})

    if any(x["name"] == GUARD for x in nodes):
        print("2. already applied — 'Draft Exists?' guard present. nothing to do.")
        return

    for req in ("Parse Callback", "Answer Callback", "Route Action"):
        if not any(x["name"] == req for x in nodes):
            raise DeployError(f"required node {req!r} not found — aborting")

    # confirm Answer Callback currently feeds Route Action
    ac = conns.get("Answer Callback", {}).get("main") or []
    feeds_route = any(c.get("node") == "Route Action"
                      for arr in ac for c in (arr or []))
    if not feeds_route:
        raise DeployError("'Answer Callback' does not feed 'Route Action' — "
                           "the callback chain is not what this patch expects")

    guard, notice = build_nodes()
    nodes.append(guard)
    nodes.append(notice)

    # rewire: Answer Callback -> Route Action  becomes  -> Draft Exists?
    for arr in ac:
        for c in (arr or []):
            if c.get("node") == "Route Action":
                c["node"] = GUARD
    # Draft Exists?  true(0) -> Route Action   false(1) -> Notify Draft Expired
    conns[GUARD] = {"main": [
        [{"node": "Route Action", "type": "main", "index": 0}],
        [{"node": NOTICE, "type": "main", "index": 0}],
    ]}

    print(f"2. nodes: {len(nodes) - 2} -> {len(nodes)}  (+2: {GUARD!r}, {NOTICE!r})")
    print("3. rewire:")
    print("     BEFORE  Answer Callback ──▶ Route Action")
    print("     AFTER   Answer Callback ──▶ Draft Exists?")
    print("                                  ├─ true  ──▶ Route Action  (unchanged)")
    print("                                  └─ false ──▶ Notify Draft Expired")
    print(f"4. Notify Draft Expired → editMessageText, text:\n     {EXPIRED_TEXT}")

    staged = Path("/tmp/draft_guard_staged.json")
    staged.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"5. staged full workflow -> {staged}")

    if not deploy:
        print("\nDRY RUN — nothing deployed. Re-run with --deploy to safe_put.")
        return

    print("6. deploying via n8n_deploy.safe_put (re-fetches staticData)...")
    final = n.safe_put(wf, tag="DRAFTGUARD")
    ok = any(x["name"] == GUARD for x in final.get("nodes", []))
    print(f"7. VERIFY: 'Draft Exists?' guard present={ok}, "
          f"nodes={len(final.get('nodes', []))}, active={final.get('active')}")
    print("DRAFT-GUARD DEPLOYED — Send / Auto / Skip / Regen / Edit now show "
          "'draft expired' instead of crashing." if ok
          else "x VERIFY FAILED — inspect; PRE-DRAFTGUARD backup saved.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
