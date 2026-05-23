#!/usr/bin/env python3
"""
build_review_step5.py — Step 5 of pipeline-review-plan.md.

Wires the user-facing Pipeline Review surface into the live workflow:

  1. /review [filter]   → POST /review on the bridge → Send Review (with
                          inline_keyboard from the response).
  2. /info  <name|phone> → POST /info  → Send Info Reply.
  3. /label <name|phone> <LABEL> → POST /label → Send Label Reply.
  4. /snooze <name|phone> <dur>  → POST /snooze → Send Snooze Reply.
  5. Parse Callback extended to handle 3 new prefixes:
        nudge:<cid>    → Hermes Draft Followup → Send Nudge Draft
        snz:<cid>:<d>  → Hermes Snooze        → Answer Callback (echo)
        inf:<cid>      → Hermes Info          → Send Info Reply

The script is idempotent — re-running is a no-op when all 4 text commands
and 3 callback handlers are already present.

Usage:
  python3 scripts/build_review_step5.py            # dry run
  python3 scripts/build_review_step5.py --deploy   # deploy
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N, DeployError  # noqa: E402


BRIDGE_CRED = {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}
ADMIN_CHAT_ID = "5532831477"


def _id():
    return str(uuid.uuid4())


# -----------------------------------------------------------------------------
# Process Text Reply — splice in 4 new command branches before final ack.

NEW_TEXT_BRANCHES = r"""
// (PR) /review [filter] — Pipeline Review report (Step 5 of pipeline-review)
{
  const lo = text.toLowerCase();
  if (lo === '/review' || lo.indexOf('/review ') === 0) {
    const arg = text.slice(7).trim().toLowerCase();
    const allowed = ['all','hot','warm','cold'];
    const filt = allowed.indexOf(arg) >= 0 ? arg : 'all';
    return { json: { action: 'review_cmd', filter: filt,
      admin_chat_id: adminChatId } };
  }
}

// (PI) /info <name-or-phone> — single-lead context dump
{
  const lo = text.toLowerCase();
  if (lo === '/info' || lo.indexOf('/info ') === 0) {
    const arg = text.slice(5).trim();
    if (!arg) {
      return { json: { action: 'ack', admin_chat_id: adminChatId,
        ack_text: 'Usage:  /info <name or phone>' } };
    }
    // @lid customer_ids contain '@'; everything else is treated as a name.
    const isId = arg.indexOf('@') >= 0;
    return { json: { action: 'info_cmd', info_query: arg,
      info_query_kind: isId ? 'customer_id' : 'name',
      admin_chat_id: adminChatId } };
  }
}

// (PL) /label <name-or-phone> <LABEL> — manual label override
{
  const lo = text.toLowerCase();
  if (lo === '/label' || lo.indexOf('/label ') === 0) {
    const parts = text.slice(7).trim().split(/\s+/);
    if (parts.length < 2) {
      return { json: { action: 'ack', admin_chat_id: adminChatId,
        ack_text: 'Usage:  /label <name or phone> <LABEL>\n'
          + 'LABELS: NEW, WARM, HOT, NEEDS_ATTENTION, COLD,\n'
          + '        PAUSED_SPAM, PAUSED_B2B, PAUSED_PERSONAL' } };
    }
    const label = parts[parts.length - 1].toUpperCase();
    const who = parts.slice(0, -1).join(' ');
    const isId = who.indexOf('@') >= 0;
    return { json: { action: 'label_cmd', label_query: who,
      label_query_kind: isId ? 'customer_id' : 'name',
      label_value: label, admin_chat_id: adminChatId } };
  }
}

// (PS) /snooze <name-or-phone> <dur> — snooze a customer (no label change)
{
  const lo = text.toLowerCase();
  if (lo === '/snooze' || lo.indexOf('/snooze ') === 0) {
    const parts = text.slice(8).trim().split(/\s+/);
    if (parts.length < 2) {
      return { json: { action: 'ack', admin_chat_id: adminChatId,
        ack_text: 'Usage:  /snooze <name or phone> <duration>\n'
          + 'examples:  /snooze Mark 4h    /snooze +971... 2d' } };
    }
    const duration = parts[parts.length - 1].toLowerCase();
    const who = parts.slice(0, -1).join(' ');
    const isId = who.indexOf('@') >= 0;
    return { json: { action: 'snooze_cmd', snooze_query: who,
      snooze_query_kind: isId ? 'customer_id' : 'name',
      snooze_duration: duration, admin_chat_id: adminChatId } };
  }
}
"""

# -----------------------------------------------------------------------------
# Parse Callback — replace the entire jsCode with an extended version that
# parses the new prefixes (nudge, snz, inf) and exposes customer_id +
# snooze_duration.

NEW_PARSE_CALLBACK = """// Parse callback_data and look up the draft.
// Extended in Step 5 (pipeline-review) to handle nudge/snz/inf prefixes
// which carry a customer_id (not a pendingQueue draft id).
const cq = $json.callback_query;
const data = cq.data || '';
const parts = data.split(':');
const action = parts[0] || '';
const second = parts[1] || '';
const third  = parts[2] || '';

// pipeline-review callbacks: nudge:<cid>, snz:<cid>:<dur>, inf:<cid>.
const pipeline = (action === 'nudge' || action === 'snz' || action === 'inf');

const wfData = $getWorkflowStaticData('global');
const queue = wfData.pendingQueue || [];
const draft = pipeline ? null : queue.find(d => d.id === second);

return {
  json: {
    action: action,
    draft_id: pipeline ? null : second,
    customer_id: pipeline ? second : (draft ? draft.customer_id : null),
    snooze_duration: (action === 'snz') ? third : null,
    callback_query_id: cq.id,
    chat_id: cq.message.chat.id,
    message_id: cq.message.message_id,
    draft: draft || null,
    // feedback + pipeline callbacks aren't about a pendingQueue draft —
    // let the existing Draft Exists? IF pass them through.
    draft_found: !!draft
      || (action || '').indexOf('feedback_') === 0
      || pipeline
  }
};
"""


# -----------------------------------------------------------------------------
# Node builders.

def _http_bridge(name, position, url, json_body):
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
            "options": {"timeout": 90000},
        },
    }


def _telegram_send(name, position, chat_id_expr, text_expr, parse_mode=None,
                   reply_markup_expr=None):
    body_parts = [
        f"\"chat_id\": {chat_id_expr}",
        f"\"text\": {text_expr}",
    ]
    if parse_mode:
        body_parts.append(f"\"parse_mode\": \"{parse_mode}\"")
    if reply_markup_expr:
        body_parts.append(f"\"reply_markup\": {reply_markup_expr}")
    body = "={ " + ", ".join(body_parts) + " }"
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
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {},
        },
    }


def _telegram_answer_cb(name, position, cb_id_expr, text_expr="\"OK\""):
    return {
        "id": _id(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
        "onError": "continueRegularOutput",
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/answerCallbackQuery",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                "={ \"callback_query_id\": " + cb_id_expr
                + ", \"text\": " + text_expr + " }"
            ),
            "options": {},
        },
    }


# -----------------------------------------------------------------------------
# Workflow mutator.

def _ensure_text_branches(wf):
    """Splice the 4 new branches into Process Text Reply jsCode + add the
    matching Route Text Action rules. Idempotent."""
    ptr = next((n for n in wf["nodes"] if n["name"] == "Process Text Reply"), None)
    if ptr is None:
        raise DeployError("Process Text Reply node not found")
    code = ptr["parameters"]["jsCode"]
    marker = "// (G) /feedback"
    sentinel_review = "// (PR) /review [filter]"
    added_branches = sentinel_review in code
    if not added_branches:
        if marker not in code:
            raise DeployError("Could not find // (G) /feedback splice marker")
        new_code = code.replace(marker, NEW_TEXT_BRANCHES.strip() + "\n\n" + marker, 1)
        ptr["parameters"]["jsCode"] = new_code
        print("   + Process Text Reply: 4 branches added")
    else:
        print("   = Process Text Reply: branches already present")

    rta = next((n for n in wf["nodes"] if n["name"] == "Route Text Action"), None)
    if rta is None:
        raise DeployError("Route Text Action not found")
    existing_keys = {r.get("outputKey") for r in
                     rta["parameters"]["rules"]["values"]}
    to_add = ["review_cmd", "info_cmd", "label_cmd", "snooze_cmd"]
    for key in to_add:
        if key in existing_keys:
            continue
        rta["parameters"]["rules"]["values"].append({
            "outputKey": key,
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": _id(),
                    "leftValue": "={{ $json.action }}",
                    "rightValue": key,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "renameOutput": True,
        })
        print(f"   + Route Text Action: {key}")


def _ensure_callback_routes(wf):
    """Extend Parse Callback jsCode + add nudge/snz/inf to Route Action."""
    pc = next((n for n in wf["nodes"] if n["name"] == "Parse Callback"), None)
    if pc is None:
        raise DeployError("Parse Callback not found")
    if "pipeline-review" not in pc["parameters"]["jsCode"]:
        pc["parameters"]["jsCode"] = NEW_PARSE_CALLBACK
        print("   + Parse Callback: extended for nudge/snz/inf")
    else:
        print("   = Parse Callback: already extended")

    ra = next((n for n in wf["nodes"] if n["name"] == "Route Action"), None)
    if ra is None:
        raise DeployError("Route Action not found")
    existing = {r.get("outputKey") for r in
                ra["parameters"]["rules"]["values"]}
    for key in ("nudge", "snz", "inf"):
        if key in existing:
            continue
        ra["parameters"]["rules"]["values"].append({
            "outputKey": key,
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": _id(),
                    "leftValue": "={{ $('Parse Callback').item.json.action }}",
                    "rightValue": key,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "renameOutput": True,
        })
        print(f"   + Route Action: {key}")


def _ensure_command_nodes(wf):
    """Add the 4 text-command HTTP + reply node pairs. Idempotent."""
    names = {n["name"] for n in wf["nodes"]}
    by = {n["name"]: n for n in wf["nodes"]}

    # Position anchor: existing Send Caps Reply lives at (5620, 2000).
    bx, by_y = 5620, 2200  # below the caps row

    new_nodes = []
    new_edges = []  # list of (src, target, src_out_index)

    # /review chain
    if "Hermes Review" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Review", [bx, by_y],
            "http://172.18.0.1:8788/review",
            "={ \"mode\": \"ondemand\", \"filter\": "
            "{{ JSON.stringify($json.filter || 'all') }} }",
        ))
    if "Send Review" not in names:
        # Inline keyboard from the bridge response: reply_markup is a JSON
        # object Telegram expects. The bridge returns inline_keyboards as
        # a list of rows (each row is a list of buttons).
        new_nodes.append(_telegram_send(
            "Send Review",
            [bx + 240, by_y],
            chat_id_expr=f"{ADMIN_CHAT_ID}",
            text_expr="JSON.stringify($json.telegram_text)",
            parse_mode="Markdown",
            reply_markup_expr=(
                "JSON.stringify({ inline_keyboard: "
                "($json.inline_keyboards || []) })"
            ),
        ))
    new_edges += [("Route Text Action", "Hermes Review", 8),  # review_cmd is index 8 (after 8 existing rules → output 9 = index 8)
                  ("Hermes Review", "Send Review", 0)]

    # /info chain
    if "Hermes Info" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Info", [bx, by_y + 200],
            "http://172.18.0.1:8788/info",
            (
                "={ \"customer_id\": "
                "{{ $json.info_query_kind === 'customer_id' "
                "? JSON.stringify($json.info_query) : '\"\"' }}, "
                "\"name\": "
                "{{ $json.info_query_kind === 'name' "
                "? JSON.stringify($json.info_query) : '\"\"' }} }"
            ),
        ))
    if "Send Info Reply" not in names:
        new_nodes.append(_telegram_send(
            "Send Info Reply",
            [bx + 240, by_y + 200],
            chat_id_expr=f"{ADMIN_CHAT_ID}",
            text_expr="JSON.stringify($json.telegram_text)",
            parse_mode="Markdown",
        ))
    new_edges += [("Route Text Action", "Hermes Info", 9),
                  ("Hermes Info", "Send Info Reply", 0)]

    # /label chain
    if "Hermes Label" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Label", [bx, by_y + 400],
            "http://172.18.0.1:8788/label",
            (
                "={ \"customer_id\": "
                "{{ $json.label_query_kind === 'customer_id' "
                "? JSON.stringify($json.label_query) : '\"\"' }}, "
                "\"name\": "
                "{{ $json.label_query_kind === 'name' "
                "? JSON.stringify($json.label_query) : '\"\"' }}, "
                "\"label\": {{ JSON.stringify($json.label_value) }}, "
                "\"reason\": \"operator /label cmd\" }"
            ),
        ))
    if "Send Label Reply" not in names:
        new_nodes.append(_telegram_send(
            "Send Label Reply",
            [bx + 240, by_y + 400],
            chat_id_expr=f"{ADMIN_CHAT_ID}",
            text_expr="JSON.stringify($json.telegram_text)",
            parse_mode="Markdown",
        ))
    new_edges += [("Route Text Action", "Hermes Label", 10),
                  ("Hermes Label", "Send Label Reply", 0)]

    # /snooze chain
    if "Hermes Snooze" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Snooze", [bx, by_y + 600],
            "http://172.18.0.1:8788/snooze",
            (
                "={ \"customer_id\": "
                "{{ $json.snooze_query_kind === 'customer_id' "
                "? JSON.stringify($json.snooze_query) : '\"\"' }}, "
                "\"name\": "
                "{{ $json.snooze_query_kind === 'name' "
                "? JSON.stringify($json.snooze_query) : '\"\"' }}, "
                "\"duration\": {{ JSON.stringify($json.snooze_duration) }} }"
            ),
        ))
    if "Send Snooze Reply" not in names:
        new_nodes.append(_telegram_send(
            "Send Snooze Reply",
            [bx + 240, by_y + 600],
            chat_id_expr=f"{ADMIN_CHAT_ID}",
            text_expr="JSON.stringify($json.telegram_text)",
        ))
    new_edges += [("Route Text Action", "Hermes Snooze", 11),
                  ("Hermes Snooze", "Send Snooze Reply", 0)]

    # callback chains — nudge / snz / inf
    if "Hermes Draft Followup" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Draft Followup", [bx + 480, by_y],
            "http://172.18.0.1:8788/draft-followup",
            (
                "={ \"customer_id\": "
                "{{ JSON.stringify($('Parse Callback').item.json.customer_id) }} }"
            ),
        ))
    if "Send Nudge Draft" not in names:
        new_nodes.append(_telegram_send(
            "Send Nudge Draft",
            [bx + 720, by_y],
            chat_id_expr=f"{ADMIN_CHAT_ID}",
            text_expr=(
                "JSON.stringify('💡 *NUDGE DRAFT* for `' + "
                "$('Parse Callback').item.json.customer_id + '`  ("
                "' + ($json.label || '?') + ')\\n\\n' + "
                "($json.draft_text || '⚠️ no draft generated'))"
            ),
            parse_mode="Markdown",
        ))
    new_edges += [("Hermes Draft Followup", "Send Nudge Draft", 0)]

    if "Hermes Snooze Btn" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Snooze Btn", [bx + 480, by_y + 400],
            "http://172.18.0.1:8788/snooze",
            (
                "={ \"customer_id\": "
                "{{ JSON.stringify($('Parse Callback').item.json.customer_id) }}, "
                "\"duration\": "
                "{{ JSON.stringify($('Parse Callback').item.json.snooze_duration || '4h') }} }"
            ),
        ))
    if "Answer Snooze Btn" not in names:
        new_nodes.append(_telegram_answer_cb(
            "Answer Snooze Btn", [bx + 720, by_y + 400],
            cb_id_expr=("$('Parse Callback').item.json.callback_query_id"),
            text_expr="JSON.stringify($json.telegram_text || '💤 snoozed')",
        ))
    new_edges += [("Hermes Snooze Btn", "Answer Snooze Btn", 0)]

    if "Hermes Info Btn" not in names:
        new_nodes.append(_http_bridge(
            "Hermes Info Btn", [bx + 480, by_y + 600],
            "http://172.18.0.1:8788/info",
            (
                "={ \"customer_id\": "
                "{{ JSON.stringify($('Parse Callback').item.json.customer_id) }} }"
            ),
        ))
    if "Send Info Btn Reply" not in names:
        new_nodes.append(_telegram_send(
            "Send Info Btn Reply",
            [bx + 720, by_y + 600],
            chat_id_expr=f"{ADMIN_CHAT_ID}",
            text_expr="JSON.stringify($json.telegram_text)",
            parse_mode="Markdown",
        ))
    new_edges += [("Hermes Info Btn", "Send Info Btn Reply", 0)]

    # Append new nodes
    for nn in new_nodes:
        if nn["name"] not in names:
            wf["nodes"].append(nn)
            print(f"   + node: {nn['name']}")

    # Wire edges into connections graph.
    # Special case: Route Text Action outputs are indexed by the rule order.
    # Look up the actual index for each route key dynamically.
    rta = next(n for n in wf["nodes"] if n["name"] == "Route Text Action")
    rta_index = {r["outputKey"]: i
                 for i, r in enumerate(rta["parameters"]["rules"]["values"])}
    ra = next(n for n in wf["nodes"] if n["name"] == "Route Action")
    ra_index = {r["outputKey"]: i
                for i, r in enumerate(ra["parameters"]["rules"]["values"])}

    conns = wf.setdefault("connections", {})

    def _ensure_edge(src, dst, src_index):
        node_conns = conns.setdefault(src, {})
        mains = node_conns.setdefault("main", [])
        # pad mains to required index
        while len(mains) <= src_index:
            mains.append([])
        # de-dup
        already = any(d.get("node") == dst for d in mains[src_index])
        if not already:
            mains[src_index].append({"node": dst, "type": "main", "index": 0})

    # Route Text Action → command HTTP nodes
    for cmd_key, http_name in (
            ("review_cmd", "Hermes Review"),
            ("info_cmd",   "Hermes Info"),
            ("label_cmd",  "Hermes Label"),
            ("snooze_cmd", "Hermes Snooze")):
        if cmd_key in rta_index:
            _ensure_edge("Route Text Action", http_name, rta_index[cmd_key])

    # command HTTP → command reply
    for src, dst in (("Hermes Review", "Send Review"),
                     ("Hermes Info", "Send Info Reply"),
                     ("Hermes Label", "Send Label Reply"),
                     ("Hermes Snooze", "Send Snooze Reply")):
        _ensure_edge(src, dst, 0)

    # Route Action → callback handler chains. We need to splice these BEFORE
    # the existing "Draft Exists?" IF — actually no, Route Action runs on the
    # IF's pass-through path (action='nudge' etc. have draft_found=true via
    # the extended Parse Callback). So Route Action's new outputs feed
    # directly to our new HTTP nodes. Wire them.
    for cb_key, http_name in (
            ("nudge", "Hermes Draft Followup"),
            ("snz",   "Hermes Snooze Btn"),
            ("inf",   "Hermes Info Btn")):
        if cb_key in ra_index:
            _ensure_edge("Route Action", http_name, ra_index[cb_key])

    for src, dst in (("Hermes Draft Followup", "Send Nudge Draft"),
                     ("Hermes Snooze Btn",   "Answer Snooze Btn"),
                     ("Hermes Info Btn",     "Send Info Btn Reply")):
        _ensure_edge(src, dst, 0)


# -----------------------------------------------------------------------------

def main():
    deploy = "--deploy" in sys.argv
    print("=== build_review_step5.py ===")
    n = N8N()
    print(f"1. n8n API: {n.api}")
    wf = n.get_workflow()
    print(f"2. node count before: {len(wf['nodes'])}")

    _ensure_text_branches(wf)
    _ensure_callback_routes(wf)
    _ensure_command_nodes(wf)

    print(f"3. node count after:  {len(wf['nodes'])}")
    if not deploy:
        print("\nDRY RUN — re-run with --deploy.")
        return

    print("4. deploying via n8n_deploy.safe_put...")
    final = n.safe_put(wf, tag="REVIEWSTEP5")
    print(f"5. VERIFY: final node count = {len(final.get('nodes', []))}")
    print("STEP 5 DEPLOYED.")


if __name__ == "__main__":
    try:
        main()
    except DeployError as e:
        sys.exit(f"x {e}")
