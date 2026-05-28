#!/usr/bin/env python3
"""
build_redis_phase_c.py — staticData→Redis migration, PHASE C (writes + cleanup).

Removes ALL remaining staticData from the approval flow:
  • staticData WRITES dropped (Redis equivalents already persist):
      Queue & Format (push + supersede), Mark Sent, Mark Skipped,
      Mark Link Sent, Save Telegram MsgID, Build Nudge Card (push)
  • last direct READS migrated to Redis:
      Prep Send File, Prep Paylink (awaiting lock), Unify Paylink Context, Parse Regen
  • dead read-fallbacks stripped (Redis is now the sole store):
      Prep Regen, Check Pending, Auto Prep, Find Break, Prep Learn, Build Paylink From Reply
  • dead nodes DELETED:
      Sync Nudge MsgID To Static  (Save Nudge MsgID already writes Redis)
      Notify Superseded Prior     (bridge _draft_save already disables prior
                                   card buttons + prepends the superseded
                                   marker — 71 supersedes / 40 TG-edits in 7d)
  • comment cleanup: Mark Auto Sent (remove the literal $getWorkflowStaticData mention)

After Phase C: ZERO getWorkflowStaticData in the workflow; staticData stops
growing; supersede has ONE editor (the bridge) — fixes the double card-edit.

USAGE:
  python3 scripts/build_redis_phase_c.py            # dry-run + all checks
  python3 scripts/build_redis_phase_c.py --deploy   # deploy + write local
"""
import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_deploy import N8N  # noqa: E402

LOCAL = ROOT / "workflows" / "phase-1b-telegram.json"
TAG = "REDIS_PHASE_C"

BR = ("    url: 'http://172.18.0.1:8788/queue',\n"
      "    headers: { 'Content-Type': 'application/json',\n"
      "      'X-Bridge-Token': $env.BRIDGE_TOKEN || '' },\n")

# ---- full-replacement nodes: name -> (marker_in_current_code, new_jsCode) ----
FULL = {
  "Mark Sent": ("R3: mark draft sent", (
    "// R3: mark draft sent + record how many messages went out.\n"
    "// Phase C (Redis-migration): status is persisted to Redis by 'Sync\n"
    "// Sent To Redis'; here we just compute the count from the Redis draft\n"
    "// (Parse Callback). staticData write removed.\n"
    "const pc = $('Parse Callback').item.json;\n"
    "const draftId = pc.draft_id;\n"
    "const d = pc.draft || {};\n"
    "const cnt = Array.isArray(d.messages) ? d.messages.length : 1;\n"
    "return { json: { ok: true, status: 'sent', draft_id: draftId, messages_sent_count: cnt } };")),

  "Mark Skipped": ("status: 'skipped'", (
    "// Phase C (Redis-migration): status persisted to Redis by 'Mark\n"
    "// Skipped Redis'; staticData write removed.\n"
    "const draftId = $('Parse Callback').item.json.draft_id;\n"
    "return { json: { ok: true, status: 'skipped', draft_id: draftId } };")),

  "Mark Link Sent": ("link_sent", (
    "// Read customer_id + draft_id from the unified paylink context.\n"
    "// Phase C (Redis-migration): status persisted to Redis by 'Mark Link\n"
    "// Sent Redis'; staticData write removed.\n"
    "let cid = '';\n"
    "let draftId = '';\n"
    "try {\n"
    "  const u = $('Unify Paylink Context').item.json;\n"
    "  cid = u.customer_id || '';\n"
    "  draftId = u.draft_id || '';\n"
    "} catch (e) {}\n"
    "return { json: { ok: true, draft_id: draftId, customer_id: cid,\n"
    "  draft: { is_payment: true, customer_phone: cid } } };")),

  "Save Telegram MsgID": ("Save Telegram message_id back to the pending draft", (
    "// Save Telegram message_id for later card edits. Phase C (Redis-\n"
    "// migration): the msg_id is persisted to Redis by 'Sync Customer\n"
    "// MsgID To Redis'; staticData write removed. Downstream Check Pending\n"
    "// / Auto Prep / Find Break read the draft from Queue & Format and the\n"
    "// msg_id from here.\n"
    "const tgResponse = $input.item.json;\n"
    "const messageId = tgResponse.result?.message_id;\n"
    "const draftId = $('Queue & Format').item.json.draft_id;\n"
    "return { json: { draft_id: draftId, telegram_message_id: messageId, ok: tgResponse.ok } };")),

  "Prep Send File": ("Redis draft is the authoritative", (
    "// Read the active draft from Parse Callback's enriched payload to\n"
    "// pull customer_id, file_key, and a caption. Phase C (Redis-\n"
    "// migration): the Redis draft (Parse Callback) is the sole source;\n"
    "// /assist file-send creates a Redis-only draft, so this is correct.\n"
    "const cb = $('Parse Callback').item.json || {};\n"
    "const draftId = cb.draft_id;\n"
    "const d = cb.draft || {};\n"
    "const customerId = cb.customer_id || d.customer_phone;\n"
    "const fileKey = d.file_key || cb.file_key || '';\n"
    "const fileDesc = d.file_description || '';\n"
    "const firstMsg = (d.messages && d.messages[0]) || d.draft_text || '';\n"
    "const caption = (fileDesc && fileDesc.length > 5 ? fileDesc : '')\n"
    "  || firstMsg || '';\n"
    "return [{ json: {\n"
    "  customer_id: customerId,\n"
    "  draft_id: draftId,\n"
    "  file_key: fileKey,\n"
    "  caption: (caption || '').slice(0, 800),\n"
    "  admin_chat_id: cb.admin_chat_id,\n"
    "  reply_to_message_id: cb.message_id,\n"
    "} }];")),

  "Prep Paylink": ("Phase A (Redis-migration): Redis draft (Parse Callback) is primary", (
    "// Operator hit [Send Link] on a draft card. Phase C (Redis-\n"
    "// migration): customer context from the Redis draft (Parse Callback);\n"
    "// the awaiting_amount lock is read from Redis (bridge 'awaiting').\n"
    "const pc = $('Parse Callback').item.json;\n"
    "const entry = pc.draft || {};\n"
    "const _http = this.helpers.httpRequest.bind(this.helpers);\n"
    "let conflict = null;\n"
    "try {\n"
    "  const r = await _http({ method: 'POST',\n"
    + BR +
    "    body: { action: 'awaiting', status: 'awaiting_amount', chat_id: 5532831477 },\n"
    "    json: true, timeout: 8000 });\n"
    "  if (r && r.found && r.draft && r.draft.id !== pc.draft_id) conflict = r.draft;\n"
    "} catch (e) { conflict = null; }\n"
    "const cid = entry.customer_phone || pc.customer_id || '';\n"
    "const name = entry.customer_name || '';\n"
    "let summary = entry.payment_summary || '';\n"
    "if (!summary) {\n"
    "  const yacht = (entry.yachts || '').split(',')[0].trim();\n"
    "  const date = entry.dates || '';\n"
    "  const pax = entry.party_size || '';\n"
    "  summary = [yacht, date, pax ? pax + ' guests' : ''].filter(Boolean).join(' · ') || 'Dubriani booking';\n"
    "}\n"
    "let amount = 0;\n"
    "try { amount = parseFloat(entry.payment_amount || 0) || 0; } catch(e) {}\n"
    "return { json: {\n"
    "  draft_id: pc.draft_id,\n"
    "  customer_id: cid,\n"
    "  customer_name: name,\n"
    "  payment_summary: summary,\n"
    "  amount: amount,\n"
    "  has_amount: amount > 0,\n"
    "  blocked: !!conflict,\n"
    "  blocking_phone: conflict ? (conflict.customer_phone || 'unknown') : '',\n"
    "  chat_id: pc.chat_id,\n"
    "  message_id: pc.message_id,\n"
    "  admin_chat_id: 5532831477\n"
    "} };")),
}

# ---- targeted anchor edits: name -> list of (old, new), each old unique ----
ANCHOR = {
  "Prep Regen": [(
    "let draft = pc.draft || null;\n"
    "if (!draft) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  draft = (data.pendingQueue || []).find(d => d.id === draftId) || null;\n"
    "}\n"
    "if (!draft) {",
    "let draft = pc.draft || null;\n"
    "if (!draft) {"
  )],
  "Parse Regen": [(
    "if (!cust.phone) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  const entry = (data.pendingQueue || []).find(x => x.id === draftId);\n"
    "  if (entry) cust = { phone: entry.customer_phone, name: entry.customer_name, message: entry.customer_message };\n"
    "}",
    "// Phase C (Redis-migration): staticData fallback removed (Redis-only)."
  )],
  "Check Pending": [(
    "if (!d) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
    "}",
    "// Phase C (Redis-migration): staticData fallback removed (Redis-only)."
  )],
  "Auto Prep": [(
    "if (!d) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  d = (data.pendingQueue || []).find(x => x.id === draftId);\n"
    "}\n"
    "if (!d) return [];",
    "if (!d) return [];   // Phase C: Redis-only (staticData fallback removed)"
  )],
  "Find Break": [(
    "if (!d) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  d = (data.pendingQueue || []).find(x => x.id === sid.draft_id) || {};\n"
    "}\n"
    "d = d || {};",
    "d = d || {};   // Phase C: Redis-only (staticData fallback removed)"
  )],
  "Prep Learn": [(
    "if (!d) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  d = (data.pendingQueue || []).find(x => x.id === draftId) || {};\n"
    "}",
    "if (!d) d = {};   // Phase C: Redis-only (staticData fallback removed)"
  )],
  "Build Paylink From Reply": [(
    "if (!entry) {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  entry = (data.pendingQueue || []).find(d => d.id === inp.draft_id) || {};\n"
    "}",
    "if (!entry) entry = {};   // Phase C: Redis-only (staticData fallback removed)"
  )],
  "Build Nudge Card": [(
    "const data = $getWorkflowStaticData('global');\n"
    "if (!Array.isArray(data.pendingQueue)) data.pendingQueue = [];\n"
    "data.pendingQueue.push(draftObj);",
    "// Phase C (Redis-migration): nudge persisted to Redis by 'Persist Nudge'."
  )],
  "Unify Paylink Context": [(
    "let draftText = '';\n"
    "let custName = '';\n"
    "try {\n"
    "  const data = $getWorkflowStaticData('global');\n"
    "  const entry = (data.pendingQueue || []).find(d => d.id === ctx.draft_id);\n"
    "  if (entry) {\n"
    "    draftText = entry.draft_text || '';\n"
    "    custName = entry.customer_name || '';\n"
    "  }\n"
    "} catch (e) {}",
    "let draftText = '';\n"
    "let custName = '';\n"
    "// Phase C (Redis-migration): pull draft text + customer name from the\n"
    "// Redis draft (bridge get) instead of staticData.\n"
    "try {\n"
    "  const _http = this.helpers.httpRequest.bind(this.helpers);\n"
    "  const r = await _http({ method: 'POST',\n"
    + BR +
    "    body: { action: 'get', draft_id: ctx.draft_id }, json: true, timeout: 8000 });\n"
    "  if (r && r.found && r.draft) {\n"
    "    draftText = r.draft.draft_text || '';\n"
    "    custName = r.draft.customer_name || '';\n"
    "  }\n"
    "} catch (e) {}"
  )],
  "Mark Auto Sent": [(
    "// The old $getWorkflowStaticData('global') write of d.status='sent' was",
    "// The old staticData write of d.status='sent' was"
  )],
}

# Queue & Format: 2 surgical edits (init line + supersede span)
QF_INIT_OLD = ("const data = $getWorkflowStaticData('global');\n"
               "if (!Array.isArray(data.pendingQueue)) data.pendingQueue = [];\n")
QF_SPAN_START = "let superseded_prior = null;"
QF_SPAN_END = "data.pendingQueue.push(pending);"
QF_SPAN_NEW = (
    "// Phase C (Redis-migration): supersede is handled entirely by the\n"
    "// bridge. Persist Draft (downstream) -> _draft_save auto-supersedes\n"
    "// prior pending drafts in Redis AND disables their Telegram card\n"
    "// buttons + prepends the superseded marker. The staticData push +\n"
    "// scan and the duplicate n8n 'Notify Superseded Prior' edit were\n"
    "// removed (the bridge edit is the single source of truth).\n"
    "const superseded_prior = null;")

DELETE_NODES = ["Sync Nudge MsgID To Static", "Notify Superseded Prior"]


def edit_qf(node, label):
    code = node["parameters"]["jsCode"]
    if code.count(QF_INIT_OLD) != 1:
        sys.exit(f"x {label}: Queue & Format init anchor not unique")
    code = code.replace(QF_INIT_OLD, "", 1)
    if QF_SPAN_START not in code or QF_SPAN_END not in code:
        sys.exit(f"x {label}: Queue & Format supersede span markers missing")
    i = code.index(QF_SPAN_START)
    j = code.index(QF_SPAN_END, i) + len(QF_SPAN_END)
    code = code[:i] + QF_SPAN_NEW + code[j:]
    node["parameters"]["jsCode"] = code
    return code


def apply_edits(nodes_by_name, label):
    changed = {}
    # full replacements
    for name, (marker, new_code) in FULL.items():
        n = nodes_by_name.get(name)
        if not n:
            sys.exit(f"x {label}: node {name!r} not found")
        cur = n["parameters"].get("jsCode", "")
        if marker not in cur:
            sys.exit(f"x {label}: node {name!r} missing marker {marker!r}")
        n["parameters"]["jsCode"] = new_code
        changed[name] = new_code
    # anchor edits
    for name, pairs in ANCHOR.items():
        n = nodes_by_name.get(name)
        if not n:
            sys.exit(f"x {label}: node {name!r} not found")
        code = n["parameters"].get("jsCode", "")
        for old, new in pairs:
            if code.count(old) != 1:
                sys.exit(f"x {label}: node {name!r} anchor count "
                         f"{code.count(old)} (expected 1):\n   {old.splitlines()[0]}")
            code = code.replace(old, new, 1)
        n["parameters"]["jsCode"] = code
        changed[name] = code
    # Queue & Format
    qf = nodes_by_name.get("Queue & Format")
    if not qf:
        sys.exit(f"x {label}: Queue & Format not found")
    changed["Queue & Format"] = edit_qf(qf, label)
    return changed


def delete_nodes(wf, label):
    names = set(DELETE_NODES)
    present = {n["name"] for n in wf["nodes"]}
    for nm in names:
        if nm not in present:
            sys.exit(f"x {label}: delete target {nm!r} not found")
    wf["nodes"] = [n for n in wf["nodes"] if n["name"] not in names]
    conns = wf["connections"]
    for nm in names:
        conns.pop(nm, None)
    for src, c in conns.items():
        for branch in c.get("main", []):
            if branch:
                branch[:] = [l for l in branch if l.get("node") not in names]


def verify(wf, label):
    # 1. zero getWorkflowStaticData anywhere
    offenders = [n["name"] for n in wf["nodes"]
                 if "getWorkflowStaticData" in n.get("parameters", {}).get("jsCode", "")]
    if offenders:
        sys.exit(f"x {label}: getWorkflowStaticData still present in: {offenders}")
    # 2. connection integrity — every referenced node exists
    names = {n["name"] for n in wf["nodes"]}
    for src, c in wf["connections"].items():
        if src not in names:
            sys.exit(f"x {label}: connection source {src!r} has no node")
        for branch in c.get("main", []):
            for l in (branch or []):
                if l.get("node") not in names:
                    sys.exit(f"x {label}: dangling link {src} -> {l.get('node')!r}")
    print(f"   ✓ {label}: 0 getWorkflowStaticData; connections intact; "
          f"{len(wf['nodes'])} nodes")


def syntax_check(changed):
    node_bin = shutil.which("node") or str(Path.home() / ".local/node/bin/node")
    for name, code in changed.items():
        wrapped = "async function __n8n(){\n" + code + "\n}\n"
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(wrapped)
            tmp = f.name
        r = subprocess.run([node_bin, "--check", tmp], capture_output=True, text=True)
        Path(tmp).unlink(missing_ok=True)
        if r.returncode != 0:
            sys.exit(f"x syntax error in node {name!r}:\n{r.stderr}")
    print(f"   ✓ syntax OK for {len(changed)} changed nodes")


def main():
    deploy = "--deploy" in sys.argv
    print(f"=== {TAG} ({'DEPLOY' if deploy else 'DRY-RUN'}) ===")
    local = json.loads(LOCAL.read_text())
    changed = apply_edits({n["name"]: n for n in local["nodes"]}, "local")
    delete_nodes(local, "local")
    print(f"edited {len(changed)} nodes; deleted {len(DELETE_NODES)}: {DELETE_NODES}")
    syntax_check(changed)
    verify(local, "local")
    if not deploy:
        print("\nDRY-RUN OK. Re-run with --deploy to push.")
        return
    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    apply_edits({nd["name"]: nd for nd in wf["nodes"]}, "live")
    delete_nodes(wf, "live")
    verify(wf, "live")
    n.safe_put(wf, tag=TAG)
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name}")
    print("\nPHASE C DEPLOYED. staticData fully retired from the approval flow.")


if __name__ == "__main__":
    main()
