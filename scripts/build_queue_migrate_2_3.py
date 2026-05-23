#!/usr/bin/env python3
"""
build_queue_migrate_2_3.py — Phases 2 + 3 of the pendingQueue → Redis migration.

Phase 2 (shadow-write on draft creation):
- Modify Queue & Format jsCode return to include `draft: pending` (so a
  downstream node can read the full draft object).
- Add Persist Draft httpRequest as a parallel arm off Queue & Format:
      Queue & Format ─┬─► Send Draft to Telegram
                      └─► Persist Draft  (POST /queue save)
  fire-and-forget; continueRegularOutput.

Phase 3 (Parse Callback reads Redis as fallback):
- Add Redis Draft Lookup httpRequest BEFORE Parse Callback on the
  Route Update Type out[0] (callback) path:
      Route Update Type out[0] ─► Redis Draft Lookup ─► Parse Callback
- Modify Parse Callback jsCode to resolve `draft` from staticData OR
  Redis fallback. All downstream nodes unchanged (they read
  $('Parse Callback').item.json.draft).

This combo eliminates the "DRAFT EXPIRED" symptom for Send/Auto/Edit/
Skip/Regen without requiring the full Phase 4-5 sweep. Phases 4-5
(retire staticData) are deferred — staticData remains in place as
the primary writer; Redis is a parallel shadow + read fallback.

Idempotent: re-running is a no-op when the nodes / branches are
already present.
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


BRIDGE_CRED = {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}


def _id():
    return str(uuid.uuid4())


PERSIST_BODY = (
    "={ \"action\": \"save\", \"draft\": "
    "{{ JSON.stringify($json.draft) }} }"
)

# Redis Draft Lookup parses the callback_data and queries the bridge.
LOOKUP_BODY = (
    "={ \"action\": \"get\", \"draft_id\": "
    "{{ JSON.stringify((($json.callback_query && $json.callback_query.data) "
    "|| '').split(':')[1] || '') }} }"
)

NEW_PARSE_CALLBACK_JS = """// Parse callback_data and look up the draft.
// Phase 5 (pipeline-review) added nudge/snz/inf prefixes for pipeline
// callbacks. Phase 2-3 of the pendingQueue migration added a Redis
// fallback when staticData lost the draft (the 'DRAFT EXPIRED' race).
const cq = $json.callback_query;
const data = cq.data || '';
const parts = data.split(':');
const action = parts[0] || '';
const second = parts[1] || '';
const third  = parts[2] || '';

const pipeline = (action === 'nudge' || action === 'snz' || action === 'inf');

const wfData = $getWorkflowStaticData('global');
const queue = wfData.pendingQueue || [];
const sdDraft = pipeline ? null : queue.find(d => d.id === second);

// Redis fallback — runs upstream as 'Redis Draft Lookup'. If staticData
// missed the draft (the documented race), Redis usually has it.
let redisDraft = null;
if (!pipeline) {
  try {
    const r = $('Redis Draft Lookup').item.json;
    if (r && r.found && r.draft) redisDraft = r.draft;
  } catch (e) { redisDraft = null; }
}

const draft = sdDraft || redisDraft || null;

return {
  json: {
    action: action,
    draft_id: pipeline ? null : second,
    customer_id: pipeline ? second : (draft ? draft.customer_phone : null),
    snooze_duration: (action === 'snz') ? third : null,
    callback_query_id: cq.id,
    chat_id: cq.message.chat.id,
    message_id: cq.message.message_id,
    draft: draft,
    draft_source: sdDraft ? 'staticData' : (redisDraft ? 'redis' : 'none'),
    draft_found: !!draft
      || (action || '').indexOf('feedback_') === 0
      || pipeline
  }
};
"""


def _http_bridge_node(name, position, url, json_body):
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
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json_body,
            "options": {"timeout": 12000},
        },
    }


def _patch_queue_and_format(wf):
    """Phase 2 — make Queue & Format expose the full pending object."""
    qaf = next((x for x in wf["nodes"] if x["name"] == "Queue & Format"), None)
    if qaf is None:
        raise DeployError("Queue & Format not found")
    code = qaf["parameters"]["jsCode"]
    if "draft: pending" in code:
        print("   = Queue & Format already exposes 'draft: pending'")
        return False
    # find the final return — splice `draft: pending` into the json object.
    old_return = "return { json: { telegram_payload: { chat_id: 5532831477, text: lines.join('\\n'), reply_markup: reply_markup }, draft_id: id } };"
    new_return = "return { json: { telegram_payload: { chat_id: 5532831477, text: lines.join('\\n'), reply_markup: reply_markup }, draft_id: id, draft: pending } };"
    if old_return not in code:
        # try a fuzzier match
        idx = code.rfind("draft_id: id }")
        if idx < 0:
            raise DeployError("Could not find Queue & Format return splice point")
        code = code[:idx] + "draft_id: id, draft: pending }" + code[idx + len("draft_id: id }"):]
    else:
        code = code.replace(old_return, new_return)
    qaf["parameters"]["jsCode"] = code
    print("   + Queue & Format return now exposes 'draft: pending'")
    return True


def _phase2_persist_draft(wf):
    """Phase 2 — add Persist Draft httpRequest as parallel arm."""
    names = {n["name"] for n in wf["nodes"]}
    if "Persist Draft" in names:
        print("   = Persist Draft already present")
        return False
    qaf = next(n for n in wf["nodes"] if n["name"] == "Queue & Format")
    qx, qy = qaf.get("position", [0, 0])
    node = _http_bridge_node(
        "Persist Draft", [qx + 240, qy + 200],
        "http://172.18.0.1:8788/queue", PERSIST_BODY,
    )
    wf["nodes"].append(node)
    print("   + Persist Draft node created")
    # Wire Queue & Format → Persist Draft as second arm (in addition to
    # the existing → Send Draft to Telegram).
    conns = wf.setdefault("connections", {})
    qaf_conns = conns.setdefault("Queue & Format", {"main": [[]]})
    mains = qaf_conns["main"]
    if not mains:
        mains.append([])
    if not any(d.get("node") == "Persist Draft" for d in mains[0]):
        mains[0].append({"node": "Persist Draft", "type": "main", "index": 0})
        print("   + Queue & Format → Persist Draft wired")
    return True


def _phase3_lookup_and_callback(wf):
    """Phase 3 — insert Redis Draft Lookup between Route Update Type out[0]
    and Parse Callback; rewrite Parse Callback to use it as fallback."""
    names = {n["name"] for n in wf["nodes"]}
    pc = next((x for x in wf["nodes"] if x["name"] == "Parse Callback"), None)
    if pc is None:
        raise DeployError("Parse Callback not found")
    rut = next((x for x in wf["nodes"] if x["name"] == "Route Update Type"), None)
    if rut is None:
        raise DeployError("Route Update Type not found")

    # 1) Add Redis Draft Lookup if not present.
    changed = False
    if "Redis Draft Lookup" not in names:
        pc_pos = pc.get("position", [0, 0])
        node = _http_bridge_node(
            "Redis Draft Lookup",
            [pc_pos[0] - 240, pc_pos[1] - 100],
            "http://172.18.0.1:8788/queue", LOOKUP_BODY,
        )
        wf["nodes"].append(node)
        print("   + Redis Draft Lookup node created")
        changed = True
    else:
        print("   = Redis Draft Lookup already present")

    # 2) Rewire: Route Update Type out[0] → Redis Draft Lookup → Parse Callback.
    conns = wf.setdefault("connections", {})
    rut_conns = conns.setdefault("Route Update Type", {"main": [[]]})
    rut_mains = rut_conns["main"]
    while len(rut_mains) < 1:
        rut_mains.append([])
    cur_targets = rut_mains[0]
    # If RUT already points to Redis Draft Lookup directly, we're done.
    if any(d.get("node") == "Redis Draft Lookup" for d in cur_targets):
        if not any(d.get("node") == "Parse Callback" for d in cur_targets):
            print("   = Route Update Type → Redis Draft Lookup (already rewired)")
    else:
        # Replace 'Parse Callback' edge with 'Redis Draft Lookup' edge.
        kept = [d for d in cur_targets if d.get("node") != "Parse Callback"]
        kept.append({"node": "Redis Draft Lookup", "type": "main", "index": 0})
        rut_mains[0] = kept
        print("   + Route Update Type out[0] now → Redis Draft Lookup")
        changed = True

    # 3) Redis Draft Lookup → Parse Callback (if not already).
    rdl_conns = conns.setdefault("Redis Draft Lookup", {"main": [[]]})
    rdl_mains = rdl_conns["main"]
    if not rdl_mains:
        rdl_mains.append([])
    if not any(d.get("node") == "Parse Callback" for d in rdl_mains[0]):
        rdl_mains[0].append({"node": "Parse Callback", "type": "main", "index": 0})
        print("   + Redis Draft Lookup → Parse Callback wired")
        changed = True

    # 4) Rewrite Parse Callback jsCode.
    if pc["parameters"].get("jsCode", "") != NEW_PARSE_CALLBACK_JS:
        pc["parameters"]["jsCode"] = NEW_PARSE_CALLBACK_JS
        print("   + Parse Callback jsCode rewritten (Redis fallback)")
        changed = True
    else:
        print("   = Parse Callback already updated")

    return changed


def main():
    deploy = "--deploy" in sys.argv
    print("=== build_queue_migrate_2_3.py ===")
    n = N8N()
    wf = n.get_workflow()
    print(f"1. node count before: {len(wf['nodes'])}")

    _patch_queue_and_format(wf)
    _phase2_persist_draft(wf)
    _phase3_lookup_and_callback(wf)

    print(f"2. node count after:  {len(wf['nodes'])}")
    if not deploy:
        print("\nDRY RUN — re-run with --deploy.")
        return

    print("3. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="QUEUEMIGRATE23")
    final_names = {x["name"] for x in final.get("nodes", [])}
    ok = ("Persist Draft" in final_names and
          "Redis Draft Lookup" in final_names)
    print(f"4. VERIFY: both nodes present={ok}  "
          f"final count={len(final.get('nodes', []))}")
    print("PHASE 2 + 3 DEPLOYED." if ok else "x VERIFY FAILED — see PRE-* backup")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
