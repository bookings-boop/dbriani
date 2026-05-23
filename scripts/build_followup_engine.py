#!/usr/bin/env python3
"""
build_followup_engine.py — proactive follow-up engine workflow extension.

Extends two workflows:

  1. Pipeline Hourly Sweep (xEEVGwoCpQ9pYqWu) — adds a parallel arm off
     'Hermes Hourly Sweep' that splits over the new eligible_followups
     field, drafts a follow-up via /draft-followup, persists the draft
     to Redis as a normal pendingQueue-style entry, posts it to the
     operator Telegram with [✅ Send / ❌ Skip / ✏️ Edit] inline buttons
     (reusing the existing send:/skip:/edit: callback prefixes), and
     logs the trigger to autonomous_sends via /followup-action.

  2. Main workflow (azPIy9OcDwiPV5uY) — adds 4 small parallel-arm nodes:
       - After Send One to Customer: IF is_followup → Log Followup Sent
       - After Edit Telegram (Skip): IF is_followup → Log Followup Skipped
     so the audit picks up Send and Skip outcomes on follow-up drafts.
     Send/Skip routing itself is unchanged — Parse Callback's Redis
     fallback (Phase 2-3 migration) handles the lookup.

Idempotent: re-running is a no-op when nodes already exist.

Usage:
  python3 scripts/build_followup_engine.py            # dry run
  python3 scripts/build_followup_engine.py --deploy   # deploy
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


BRIDGE_CRED = {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}
ADMIN_CHAT_ID = "5532831477"
SWEEP_WF_NAME = "Dubriani Pipeline Hourly Sweep"


def _id():
    return str(uuid.uuid4())


# =============================================================================
# Sweep workflow extension — 7 new nodes
# =============================================================================

def _split_followups_node(pos):
    return {
        "id": _id(),
        "name": "Split Followups",
        "type": "n8n-nodes-base.splitOut",
        "typeVersion": 1,
        "position": pos,
        "parameters": {
            "fieldToSplitOut": "eligible_followups",
            "options": {},
        },
    }


def _http_bridge(name, pos, url, json_body, timeout_ms=90000):
    return {
        "id": _id(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
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
            "options": {"timeout": timeout_ms},
        },
    }


BUILD_FOLLOWUP_CARD_JS = r"""
// Build a follow-up draft card from /draft-followup output + the split item.
// Generates a draft_id like Queue & Format does, formats the Telegram text
// with the ⏰ FOLLOW-UP SUGGESTION header, builds the inline keyboard with
// the existing send:/skip:/edit: callback prefixes (Parse Callback handles
// them via Redis fallback per the Phase 2-3 pendingQueue migration).

const fu  = $('Hermes Draft Followup').item.json || {};
const src = $('Split Followups').item.json    || {};

const cid    = src.customer_id;
const name   = src.name || '';
const label  = src.label || 'WARM';
const win    = src.silence_window || '';
const shrs   = (src.silence_hours == null) ? null : Number(src.silence_hours);
const draft  = (fu.draft_text || '').trim();

const id    = Date.now() + '_' + Math.random().toString(36).slice(2, 7);
const isoNow = new Date().toISOString();

// Format silent-X for the header — minutes if <2h, hours otherwise.
let sLabel = '—';
if (typeof shrs === 'number' && isFinite(shrs)) {
  sLabel = (shrs < 2) ? Math.round(shrs * 60) + 'm' :
           (shrs < 36) ? Math.round(shrs) + 'h' :
           Math.round(shrs / 24) + 'd';
}

const header = '⏰ FOLLOW-UP SUGGESTION — '
             + (name || cid)
             + ' · ' + label
             + ' · silent ' + sLabel;
const body   = '\n\n' + (draft || '⚠️ Hermes returned no draft text — skip recommended.');
const text   = header + body;

// Same Redis draft shape as customer-message drafts so Parse Callback's
// Redis fallback finds it. is_followup distinguishes for logging.
const draftObj = {
  id: id,
  timestamp: isoNow,
  customer_phone: cid,
  customer_name: name,
  customer_message: '',
  conversation_history: '',
  history_count: 0,
  messages: [draft],
  draft_text: draft,
  messages_sent_count: 0,
  notes: 'Proactive follow-up — window: ' + win + (shrs == null ? '' : (' · ' + shrs + 'h')),
  telegram_message_id: null,
  telegram_chat_id: 5532831477,
  status: 'pending',
  break_condition: { hit: false },
  is_lead: false,
  is_payment: false,
  is_followup: true,
  followup_silence_window: win,
  followup_silence_hours: shrs,
  followup_label: label,
};

const reply_markup = {
  inline_keyboard: [
    [{ text: '✅ Send', callback_data: 'send:' + id },
     { text: '✏️ Edit', callback_data: 'edit:' + id }],
    [{ text: '❌ Skip', callback_data: 'skip:' + id }]
  ]
};

return {
  json: {
    draft_id: id,
    draft: draftObj,
    telegram_payload: { chat_id: 5532831477, text: text, reply_markup: reply_markup },
    has_draft: !!draft,
    // Surface key fields for downstream nodes
    customer_id: cid,
    label: label,
    silence_window: win,
    silence_hours: shrs,
    draft_text: draft,
  }
};
""".strip()


def _build_followup_card_node(pos):
    return {
        "id": _id(),
        "name": "Build Followup Card",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
        "parameters": {
            "mode": "runOnceForEachItem",
            "jsCode": BUILD_FOLLOWUP_CARD_JS,
        },
    }


def _persist_followup_node(pos):
    return _http_bridge(
        "Persist Followup", pos, "http://172.18.0.1:8788/queue",
        ("={ \"action\": \"save\", \"draft\": "
         "{{ JSON.stringify($json.draft) }} }"),
        timeout_ms=15000,
    )


def _send_followup_card_node(pos):
    return {
        "id": _id(),
        "name": "Send Followup Card",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
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
            "jsonBody": (
                "={ \"chat_id\": " + ADMIN_CHAT_ID + ", "
                "\"text\": {{ JSON.stringify($('Build Followup Card').item.json.telegram_payload.text) }}, "
                "\"reply_markup\": "
                "{{ JSON.stringify($('Build Followup Card').item.json.telegram_payload.reply_markup) }} }"
            ),
            "options": {},
        },
    }


def _save_followup_msgid_node(pos):
    """After Send Followup Card succeeds, patch the draft's telegram_message_id
    so the Edit-callback (Edit Telegram nodes) can edit the right message."""
    return _http_bridge(
        "Save Followup MsgID", pos, "http://172.18.0.1:8788/queue",
        ("={ \"action\": \"update\", "
         "\"draft_id\": {{ JSON.stringify($('Build Followup Card').item.json.draft_id) }}, "
         "\"fields\": { \"telegram_message_id\": "
         "{{ ($json.result && $json.result.message_id) || null }} } }"),
        timeout_ms=15000,
    )


def _log_followup_drafted_node(pos):
    return _http_bridge(
        "Log Followup Drafted", pos,
        "http://172.18.0.1:8788/followup-action",
        ("={ \"action\": \"drafted\", "
         "\"customer_id\": {{ JSON.stringify($('Build Followup Card').item.json.customer_id) }}, "
         "\"label\": {{ JSON.stringify($('Build Followup Card').item.json.label) }}, "
         "\"silence_window\": {{ JSON.stringify($('Build Followup Card').item.json.silence_window) }}, "
         "\"silence_hours\": {{ $('Build Followup Card').item.json.silence_hours }}, "
         "\"draft_text\": {{ JSON.stringify($('Build Followup Card').item.json.draft_text) }} }"),
        timeout_ms=10000,
    )


def patch_sweep_workflow(wf):
    names = {n["name"] for n in wf["nodes"]}
    if "Split Followups" in names and "Send Followup Card" in names:
        print("   [sweep] already patched — skipping")
        return False
    if "Hermes Hourly Sweep" not in names:
        raise DeployError("Hermes Hourly Sweep node not found in sweep workflow")

    # Position cascade — Hermes Hourly Sweep is roughly at [440, 300]
    base_x, base_y = 440, 600
    new_nodes = [
        _split_followups_node([base_x + 240, base_y]),
        _http_bridge(
            "Hermes Draft Followup", [base_x + 480, base_y],
            "http://172.18.0.1:8788/draft-followup",
            ("={ \"customer_id\": {{ JSON.stringify($json.customer_id) }}, "
             "\"customer_name\": {{ JSON.stringify($json.name) }}, "
             "\"silence_window\": {{ JSON.stringify($json.silence_window) }}, "
             "\"silence_hours\": {{ $json.silence_hours }} }"),
            timeout_ms=90000,
        ),
        _build_followup_card_node([base_x + 720, base_y]),
        _persist_followup_node([base_x + 960, base_y]),
        _send_followup_card_node([base_x + 1200, base_y]),
        _save_followup_msgid_node([base_x + 1440, base_y]),
        _log_followup_drafted_node([base_x + 1680, base_y]),
    ]
    wf["nodes"].extend(new_nodes)
    print(f"   [sweep] added {len(new_nodes)} nodes")

    # Connections: parallel arm off Hermes Hourly Sweep.
    conns = wf.setdefault("connections", {})
    hhs_conns = conns.setdefault("Hermes Hourly Sweep", {"main": [[]]})
    hhs_mains = hhs_conns["main"]
    if not hhs_mains:
        hhs_mains.append([])
    if not any(t.get("node") == "Split Followups" for t in hhs_mains[0]):
        hhs_mains[0].append({"node": "Split Followups", "type": "main", "index": 0})

    def _edge(src, dst):
        c = conns.setdefault(src, {"main": [[]]})
        if not c["main"]:
            c["main"].append([])
        if not any(t.get("node") == dst for t in c["main"][0]):
            c["main"][0].append({"node": dst, "type": "main", "index": 0})

    _edge("Split Followups",       "Hermes Draft Followup")
    _edge("Hermes Draft Followup", "Build Followup Card")
    _edge("Build Followup Card",   "Persist Followup")
    _edge("Persist Followup",      "Send Followup Card")
    _edge("Send Followup Card",    "Save Followup MsgID")
    _edge("Save Followup MsgID",   "Log Followup Drafted")
    return True


# =============================================================================
# Main workflow extension — 4 logging arm nodes
# =============================================================================

def _if_is_followup(name, position):
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
                    "leftValue": "={{ $('Parse Callback').item.json.draft.is_followup }}",
                    "rightValue": True,
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
    }


def _log_action_node(name, position, action):
    """HTTP node that POSTs /followup-action with the action key."""
    body = (
        "={ \"action\": \"" + action + "\", "
        "\"customer_id\": {{ JSON.stringify($('Parse Callback').item.json.draft.customer_phone) }}, "
        "\"label\": {{ JSON.stringify($('Parse Callback').item.json.draft.followup_label) }}, "
        "\"silence_window\": {{ JSON.stringify($('Parse Callback').item.json.draft.followup_silence_window) }}, "
        "\"silence_hours\": {{ $('Parse Callback').item.json.draft.followup_silence_hours }}, "
        "\"draft_text\": {{ JSON.stringify($('Parse Callback').item.json.draft.draft_text) }} }"
    )
    return _http_bridge(name, position,
                        "http://172.18.0.1:8788/followup-action",
                        body, timeout_ms=10000)


def patch_main_workflow(wf):
    names = {n["name"] for n in wf["nodes"]}
    if "Followup Sent?" in names and "Followup Skipped?" in names:
        print("   [main] already patched — skipping")
        return False

    # Locate anchor nodes
    send_one = next((n for n in wf["nodes"]
                     if n["name"] == "Send One to Customer"), None)
    edit_skip = next((n for n in wf["nodes"]
                      if n["name"] == "Edit Telegram (Skip)"), None)
    if not send_one or not edit_skip:
        raise DeployError("Send One to Customer or Edit Telegram (Skip) missing")

    sx, sy = send_one.get("position", [0, 0])
    kx, ky = edit_skip.get("position", [0, 0])

    new_nodes = [
        _if_is_followup("Followup Sent?", [sx, sy + 240]),
        _log_action_node("Log Followup Sent", [sx + 240, sy + 240], "sent"),
        _if_is_followup("Followup Skipped?", [kx, ky + 240]),
        _log_action_node("Log Followup Skipped", [kx + 240, ky + 240], "skipped"),
    ]
    wf["nodes"].extend(new_nodes)
    print(f"   [main] added {len(new_nodes)} nodes")

    conns = wf.setdefault("connections", {})

    def _add_parallel_arm(src, target):
        c = conns.setdefault(src, {"main": [[]]})
        if not c["main"]:
            c["main"].append([])
        if not any(t.get("node") == target for t in c["main"][0]):
            c["main"][0].append({"node": target, "type": "main", "index": 0})

    # Send One to Customer → Followup Sent? (parallel)
    _add_parallel_arm("Send One to Customer", "Followup Sent?")
    # Followup Sent? out[0] (true) → Log Followup Sent
    conns["Followup Sent?"] = {"main": [
        [{"node": "Log Followup Sent", "type": "main", "index": 0}],
        [],
    ]}

    # Edit Telegram (Skip) → Followup Skipped?
    _add_parallel_arm("Edit Telegram (Skip)", "Followup Skipped?")
    conns["Followup Skipped?"] = {"main": [
        [{"node": "Log Followup Skipped", "type": "main", "index": 0}],
        [],
    ]}
    return True


# =============================================================================

def main():
    deploy = "--deploy" in sys.argv
    print("=== build_followup_engine.py ===")
    n = N8N()

    # --- sweep workflow ---
    items = (n._api_get("/workflows", "list").get("data") or [])
    sweep = next((w for w in items if w.get("name") == SWEEP_WF_NAME), None)
    if not sweep:
        raise DeployError(f"workflow {SWEEP_WF_NAME!r} not found")
    sweep_wf = n._api_get(f"/workflows/{sweep['id']}", "get-sweep")
    print(f"1. {SWEEP_WF_NAME}: {len(sweep_wf['nodes'])} nodes before")
    sweep_patched = patch_sweep_workflow(sweep_wf)
    print(f"   {len(sweep_wf['nodes'])} nodes after")

    # --- main workflow ---
    main_wf = n.get_workflow()
    print(f"2. Main workflow: {len(main_wf['nodes'])} nodes before")
    main_patched = patch_main_workflow(main_wf)
    print(f"   {len(main_wf['nodes'])} nodes after")

    if not deploy:
        print("\nDRY RUN — re-run with --deploy.")
        return

    # --- deploy sweep via PUT (no staticData to preserve) ---
    if sweep_patched:
        print("3. PUT sweep workflow...")
        put_body = {
            "name": sweep_wf.get("name"),
            "nodes": sweep_wf.get("nodes", []),
            "connections": sweep_wf.get("connections", {}),
            "settings": sweep_wf.get("settings", {}),
        }
        body = json.dumps(put_body).encode("utf-8")
        n.upload(body, "/tmp/fup_sweep_put.json", label="upload")
        out = n.ssh(
            f'read -r K; curl -s -X PUT '
            f'-H "X-N8N-API-KEY: $K" -H "Content-Type: application/json" '
            f'--data-binary @/tmp/fup_sweep_put.json '
            f'{n.api}/workflows/{sweep["id"]}',
            stdin=n.key + "\n", label="sweep-put", timeout=90)
        n.ssh("rm -f /tmp/fup_sweep_put.json", label="rm", timeout=30)
        try:
            res = json.loads(out)
            print(f"   sweep PUT OK — {len(res.get('nodes', []))} nodes")
        except Exception:
            raise DeployError(f"sweep PUT failed: {out[:300]}")

    # --- deploy main via safe_put ---
    if main_patched:
        print("4. safe_put main workflow...")
        n.safe_put(main_wf, tag="FOLLOWUPENGINE")
        print("   main deployed")

    print("FOLLOWUP ENGINE DEPLOYED.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
