#!/usr/bin/env python3
"""
build_redis_phase_b.py — staticData→Redis migration, PHASE B (last reads).

Migrates the two remaining enrichment DATA-reads off staticData:
  • Parse Refine     -> customer context from Prep Refine's Redis draft
                        (its staticData entry-mutation was already dead;
                         Refine Commit writes Redis authoritatively)
  • Apply Improvement-> draft from Recheck Draft Status (live Redis copy)
                        (its staticData mutation was already dead; Improve
                         Commit writes Redis authoritatively)

After Phase A + B, NO node reads staticData for data it acts on. The only
staticData left is write-coupled (mutators' find-then-write + supersede),
removed in Phase C. Fully reversible (PRE-{TAG} backup).

USAGE:
  python3 scripts/build_redis_phase_b.py            # dry-run
  python3 scripts/build_redis_phase_b.py --deploy   # deploy + write local
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
TAG = "REDIS_PHASE_B"

PARSE_REFINE = r"""// FR-1: parse the refine response, update the draft, re-render its card
const response = $input.item.json;
let raw = '';
try { raw = response.content[0].text; } catch (e) {
  return { json: { error: 'Empty Claude response on refine', raw: JSON.stringify(response) } };
}
let cleaned = raw.trim().replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
let parsed;
try {
  parsed = JSON.parse(cleaned);
  if (!Array.isArray(parsed.messages) || !parsed.messages.length) throw new Error('messages missing');
} catch (e) {
  parsed = { messages: [raw], notes_for_zayn: '(parse fail: ' + e.message + ')' };
}
const msgs = parsed.messages.slice(0, 4).map(m => String(m));
const bp = $('Build Refine Prompt').item.json;
const draftId = bp.draft_id;
// Phase B (Redis-migration): customer context comes from Prep Refine's
// Redis draft. The old staticData entry read + the entry mutation were
// removed (Refine Commit writes Redis authoritatively, so what the
// operator sees == what Send fans).
const pr = $('Prep Refine').item.json || {};
const custPhone = pr.customerPhone || bp.customerPhone || 'unknown';
const custName  = pr.customerName  || bp.customerName  || '';
const custMsg   = pr.userMessage   || bp.userMessage   || '';
const preview = msgs.length === 1 ? msgs[0] : msgs.map((m, i) => (i + 1) + '. ' + m).join('\n');
return { json: {
  draft_id: draftId,
  messages: msgs,
  draft_text: msgs.join('\n\n'),
  notes: parsed.notes_for_zayn || '',
  new_preview: preview,
  new_notes: parsed.notes_for_zayn || '(no notes)',
  customer_phone: custPhone,
  customer_name: custName,
  customer_message: custMsg,
  telegram_message_id: bp.telegram_message_id,
  admin_chat_id: bp.admin_chat_id,
  card: {
    chat_id: bp.admin_chat_id,
    message_id: bp.telegram_message_id,
    text: '📩 [REFINED] from ' + custPhone
      + '\nName: ' + (custName || '(unknown)')
      + '\n\nThey said:\n"' + custMsg + '"'
      + '\n\n---\n💬 NEW DRAFT:\n' + preview
      + '\n\n📝 Notes: ' + (parsed.notes_for_zayn || '(no notes)'),
    reply_markup: { inline_keyboard: [
      [{ text: '✅ Send', callback_data: 'send:' + draftId }, { text: '✏️ Edit', callback_data: 'edit:' + draftId }],
      [{ text: '🔁 Regen', callback_data: 'regen:' + draftId }, { text: '❌ Skip', callback_data: 'skip:' + draftId }],
      [{ text: '🤖 Auto', callback_data: 'auto:' + draftId }, { text: '💳 Send Link', callback_data: 'paylink:' + draftId }]
    ] }
  },
  should_send_file: !!(parsed && parsed.should_send_file === true),
  file_key: (parsed && typeof parsed.file_key === 'string') ? parsed.file_key.trim() : '',
  file_description: (parsed && typeof parsed.file_description === 'string') ? parsed.file_description.trim() : '',
} };"""

APPLY_IMPROVEMENT = r"""// FR-5: apply Hermes's improvement to the pending draft + re-render the card
const resp = $input.item.json || {};
const cp = $('Check Pending').item.json;
const draftId = cp.draft_id;
// Phase B (Redis-migration): the draft is the live Redis copy from
// Recheck Draft Status (the cross-execution source of truth). The old
// staticData read + the dead entry mutation were removed — Improve
// Commit writes Redis authoritatively.
let d = null;
try { d = ($('Recheck Draft Status').item.json || {}).draft || null; } catch (e) { d = null; }
if (!d) {
  // fallback: reconstruct enough from Check Pending's fields
  d = { status: 'pending', customer_phone: cp.customer_id || '',
        customer_name: cp.customer_name || '', customer_message: cp.incoming_message || '',
        notes: '', is_lead: false, draft_text: cp.current_draft || '',
        telegram_chat_id: 5532831477 };
}
// race guard: the operator may have acted during the ~18s Hermes call.
// Redis status is authoritative across executions.
if (!d || (d.status && d.status !== 'pending')) return [];
if (resp.ok !== true || resp.improved !== true) return [];
const msgs = (Array.isArray(resp.messages) && resp.messages.length)
  ? resp.messages.slice(0, 4).map(m => String(m)) : null;
if (!msgs) return [];
d.messages = msgs;
d.draft_text = msgs.join('\n\n');
d.improved_note = resp.note || '';
const preview = msgs.length === 1 ? msgs[0]
  : msgs.map((m, i) => (i + 1) + '. ' + m).join('\n');
const draftHeader = msgs.length === 1 ? '💬 DRAFT:'
  : ('💬 DRAFT (' + msgs.length + ' messages):');
const lines = [];
// Reuse the Customer Facts header so the operator sees consistent
// context before AND after the improver fires.
let customerHeader = '';
try { customerHeader = ($('Customer Facts').item.json || {}).customer_header || ''; }
catch (e) { customerHeader = ''; }
const displayName = (d.customer_name && d.customer_name.trim()) || d.customer_phone;
if (d.is_lead) {
  lines.push('🆕 NEW LEAD — outbound first contact', '📞 ' + d.customer_phone,
             '', 'Lead brief (from you):', '"' + d.customer_message + '"');
} else {
  lines.push('📩 from ' + displayName);
  if (customerHeader) {
    lines.push(customerHeader);
  } else if (d.customer_name) {
    lines.push('👤 ' + d.customer_name);
  }
  lines.push('', 'They said:', '"' + d.customer_message + '"');
}
lines.push('', '---', '✨ improved by Hermes', draftHeader, preview,
           '', '📝 Notes: ' + (d.notes || ''));
if (d && d.should_send_file && d.file_key) {
    (lines || []).push('📎 Suggested file: ' + d.file_key);
  }
  const reply_markup = { inline_keyboard: [
  [{ text: '✅ Send', callback_data: 'send:' + draftId },
   { text: '✏️ Edit', callback_data: 'edit:' + draftId }],
  [{ text: '🔁 Regen', callback_data: 'regen:' + draftId },
   { text: '❌ Skip', callback_data: 'skip:' + draftId }],
  [{ text: '🤖 Auto', callback_data: 'auto:' + draftId },
     { text: '💳 Send Link', callback_data: 'paylink:' + draftId }],
      ...((d && d.should_send_file && d.file_key) ? [[{ text: '📎 Send File ('+(d.file_key)+')', callback_data: 'file:' + draftId }]] : [])
] };
return [{ json: {
  chat_id: d.telegram_chat_id || 5532831477,
  message_id: cp.telegram_message_id,
  text: lines.join('\n'),
  reply_markup: reply_markup,
  draft_id: draftId,
  improved_draft_text: d.draft_text,
  improved_messages: msgs
} }];"""

# node -> (marker that MUST be in the current code, full new jsCode)
REPLACE = {
    "Parse Refine": ("[REFINED]", PARSE_REFINE),
    "Apply Improvement": ("improved by Hermes", APPLY_IMPROVEMENT),
}


def apply_edits(nodes_by_name, label):
    changed = {}
    for name, (marker, new_code) in REPLACE.items():
        node = nodes_by_name.get(name)
        if not node:
            sys.exit(f"x {label}: node {name!r} not found")
        cur = node["parameters"].get("jsCode", "")
        if "getWorkflowStaticData" not in cur:
            sys.exit(f"x {label}: node {name!r} already migrated "
                     "(no getWorkflowStaticData) — aborting to avoid double-apply")
        if marker not in cur:
            sys.exit(f"x {label}: node {name!r} missing marker {marker!r} — drift")
        node["parameters"]["jsCode"] = new_code
        changed[name] = new_code
    return changed


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
        print(f"   ✓ syntax OK: {name}")


def main():
    deploy = "--deploy" in sys.argv
    print(f"=== {TAG} ({'DEPLOY' if deploy else 'DRY-RUN'}) ===")
    local = json.loads(LOCAL.read_text())
    local_by_name = {n["name"]: n for n in local["nodes"]}
    changed = apply_edits(local_by_name, "local")
    print(f"applied edits to {len(changed)} nodes: {sorted(changed)}")
    syntax_check(changed)
    if not deploy:
        print("\nDRY-RUN OK. Re-run with --deploy to push.")
        return
    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    apply_edits({nd["name"]: nd for nd in wf["nodes"]}, "live")
    n.safe_put(wf, tag=TAG)
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name}")
    print("\nPHASE B DEPLOYED. Operator test matrix:")
    print("  1. Tap ✏️ Edit on a draft, reply with a change → card re-renders")
    print("     as [REFINED] with the RIGHT customer name/message (Parse Refine)")
    print("  2. Send a non-payment customer msg, wait ~30s → '✨ improved by")
    print("     Hermes' card keeps the right customer header (Apply Improvement)")


if __name__ == "__main__":
    main()
