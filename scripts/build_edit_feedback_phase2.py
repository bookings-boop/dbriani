#!/usr/bin/env python3
"""
build_edit_feedback_phase2.py — draft feedback loop, PHASE 2 (feedback prompt).

Requires bridge /edit-feedback (deployed). n8n changes:
  • Capture Edit Delta -> Should Prompt? (IF should_prompt)
      -> Feedback Wait (30s) -> Build Feedback Prompt (code) -> Post Feedback
         Prompt (TG sendMessage, inline kbd editfb:<corr_id>:<tag>)
      -> Dismiss Wait (60s) -> Clear Prompt Buttons (TG editMessageReplyMarkup)
  • Parse Callback: 'editfb' treated as a pipeline (non-draft) action.
  • Route Action: new 'editfb' rule -> Edit Feedback Tag (bridge tag)
      -> Feedback Tag Route (IF skip)
         true  -> Dismiss On Skip (TG edit "skipped")
         false -> Post Guided Question (TG edit -> the guided question)
  • Process Text Reply: last-resort await-detail check before /assist
    (captures the optional free-text note as reason_detail).

USAGE:
  python3 scripts/build_edit_feedback_phase2.py            # dry-run
  python3 scripts/build_edit_feedback_phase2.py --deploy   # deploy + write local
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
TAG = "EDIT_FEEDBACK_P2"
TG = "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/"
ADMIN = 5532831477


def tg_node(name, nid, pos, path, jsonbody, on_error=None):
    n = {
        "id": nid, "name": name, "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": pos,
        "parameters": {
            "method": "POST", "url": TG + path,
            "sendHeaders": True, "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": jsonbody,
            "options": {"timeout": 10000},
        },
    }
    if on_error:
        n["onError"] = on_error
    return n


def if_bool(name, nid, pos, left_expr):
    return {
        "id": nid, "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": pos,
        "parameters": {"conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "loose", "version": 1},
            "conditions": [{
                "id": nid + "-c", "leftValue": left_expr, "rightValue": True,
                "operator": {"type": "boolean", "operation": "true"}}],
            "combinator": "and"}},
    }


def wait_node(name, nid, pos, secs):
    return {"id": nid, "name": name, "type": "n8n-nodes-base.wait",
            "typeVersion": 1, "position": pos,
            "parameters": {"amount": secs, "unit": "seconds"}}


BUILD_PROMPT_CODE = r"""// Build the feedback-prompt card (inline kbd editfb:<corr_id>:<tag>).
const cap = $('Capture Edit Delta').item.json || {};
const cid = cap.correction_id;
const sim = typeof cap.similarity === 'number' ? cap.similarity : 0;
const pct = Math.max(1, Math.round((1 - sim) * 100));
const tags = [
  ['📏 Too long', 'too_long'], ['🎭 Wrong tone', 'wrong_tone'],
  ['❓ Missed question', 'missed_question'], ['ℹ️ Wrong info', 'wrong_info'],
  ['✍️ My style', 'my_style'], ['⏭️ Skip', 'skip'],
];
const kb = [];
for (let i = 0; i < tags.length; i += 2) {
  kb.push(tags.slice(i, i + 2).map(t => ({ text: t[0], callback_data: 'editfb:' + cid + ':' + t[1] })));
}
return [{ json: { correction_id: cid, telegram_payload: {
  chat_id: 5532831477,
  text: '📝 You edited that draft before sending (~' + pct + '% changed). What was off? (auto-dismisses)',
  reply_markup: { inline_keyboard: kb },
} } }];"""

# Process Text Reply: insert await-detail check right before the /assist return.
PTR_ANCHOR = ("return { json: { action: 'assist_query', query_text: text, "
              "admin_chat_id: adminChatId } };")
PTR_NEW = (
    "// edit-feedback detail (optional, P2): operator tapped a reason then typed\n"
    "// a note. If a correction is armed for this chat, capture it as\n"
    "// reason_detail. Fail-open; last check before /assist.\n"
    "try {\n"
    "  const _ef = await _http({ method: 'POST',\n"
    "    url: 'http://172.18.0.1:8788/edit-feedback',\n"
    "    headers: { 'Content-Type': 'application/json',\n"
    "      'X-Bridge-Token': $env.BRIDGE_TOKEN || '' },\n"
    "    body: { action: 'await-detail', chat_id: String(adminChatId), text: text },\n"
    "    json: true, timeout: 8000 });\n"
    "  if (_ef && _ef.captured) {\n"
    "    return { json: { action: 'ack', admin_chat_id: adminChatId,\n"
    "      ack_text: '\\u2713 noted \\u2014 thanks, I will use that.' } };\n"
    "  }\n"
    "} catch (e) {}\n"
    + PTR_ANCHOR)


def edit(wf, label):
    nb = {n["name"]: n for n in wf["nodes"]}
    conns = wf["connections"]
    changed_code = {}

    # 1. Parse Callback — add editfb to pipeline set
    pc = nb["Parse Callback"]
    code = pc["parameters"]["jsCode"]
    old_pipe = "action === 'disregard_force');"
    if code.count(old_pipe) != 1:
        sys.exit(f"x {label}: Parse Callback pipeline anchor count != 1")
    if "editfb" not in code:
        pc["parameters"]["jsCode"] = code.replace(
            old_pipe, "action === 'disregard_force' || action === 'editfb');", 1)
    changed_code["Parse Callback"] = pc["parameters"]["jsCode"]

    # 2. Process Text Reply — await-detail check before /assist
    ptr = nb["Process Text Reply"]
    pcode = ptr["parameters"]["jsCode"]
    if "await-detail" not in pcode:
        if pcode.count(PTR_ANCHOR) != 1:
            sys.exit(f"x {label}: Process Text Reply assist anchor count != 1")
        ptr["parameters"]["jsCode"] = pcode.replace(PTR_ANCHOR, PTR_NEW, 1)
    changed_code["Process Text Reply"] = ptr["parameters"]["jsCode"]

    # 3. add new nodes (idempotent)
    new_nodes = [
        if_bool("Should Prompt?", "edit-should-prompt", [2850, 1450],
                "={{ $json.should_prompt }}"),
        wait_node("Feedback Wait", "edit-fb-wait", [3050, 1450], 30),
        {"id": "edit-build-prompt", "name": "Build Feedback Prompt",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [3250, 1450],
         "parameters": {"jsCode": BUILD_PROMPT_CODE}},
        tg_node("Post Feedback Prompt", "edit-post-prompt", [3450, 1450],
                "sendMessage", "={{ JSON.stringify($json.telegram_payload) }}",
                on_error="continueRegularOutput"),
        wait_node("Dismiss Wait", "edit-dismiss-wait", [3650, 1450], 60),
        tg_node("Clear Prompt Buttons", "edit-clear-buttons", [3850, 1450],
                "editMessageReplyMarkup",
                '={ "chat_id": 5532831477, "message_id": {{ $(\'Post Feedback Prompt\').item.json.result.message_id }}, "reply_markup": {"inline_keyboard":[]} }',
                on_error="continueRegularOutput"),
        # bridge tag
        {"id": "edit-fb-tag", "name": "Edit Feedback Tag",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
         "position": [2850, 1650],
         "parameters": {
             "authentication": "genericCredentialType",
             "genericAuthType": "httpHeaderAuth", "method": "POST",
             "url": "http://172.18.0.1:8788/edit-feedback",
             "sendHeaders": True, "headerParameters": {"parameters": [
                 {"name": "Content-Type", "value": "application/json"}]},
             "sendBody": True, "specifyBody": "json",
             "jsonBody": ('={ "action": "tag", "correction_id": '
                          '{{ Number((($(\'Route Update Type\').item.json.callback_query.data) || \'\').split(\':\')[1]) }}, '
                          '"reason_tag": {{ JSON.stringify(((($(\'Route Update Type\').item.json.callback_query.data) || \'\').split(\':\')[2]) || \'\') }}, '
                          '"chat_id": {{ JSON.stringify(String(($(\'Parse Callback\').item.json.chat_id) || 5532831477)) }} }'),
             "options": {"timeout": 40000}},
         "onError": "continueRegularOutput",
         "credentials": {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}}},
        if_bool("Feedback Tag Route", "edit-tag-route", [3050, 1650],
                "={{ $json.skip }}"),
        tg_node("Dismiss On Skip", "edit-skip", [3250, 1600], "editMessageText",
                '={ "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }}, "message_id": {{ $(\'Parse Callback\').item.json.message_id }}, "text": {{ JSON.stringify("\\u2713 Skipped \\u2014 thanks.") }} }',
                on_error="continueRegularOutput"),
        tg_node("Post Guided Question", "edit-question", [3250, 1750], "editMessageText",
                '={ "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }}, "message_id": {{ $(\'Parse Callback\').item.json.message_id }}, "text": {{ JSON.stringify("\\ud83d\\udcdd " + ($json.question || "What would you change?") + "\\n\\n(reply to add a note \\u2014 optional)") }} }',
                on_error="continueRegularOutput"),
    ]
    existing = {n["name"] for n in wf["nodes"]}
    for nd in new_nodes:
        if nd["name"] not in existing:
            wf["nodes"].append(json.loads(json.dumps(nd)))
        if nd["type"] == "n8n-nodes-base.code":
            changed_code[nd["name"]] = nd["parameters"]["jsCode"]

    # 4. Route Action — add 'editfb' rule + connection branch
    ra = nb["Route Action"]
    vals = ra["parameters"]["rules"]["values"]
    if not any(v.get("outputKey") == "editfb" for v in vals):
        vals.append({
            "conditions": {"options": {"caseSensitive": True, "leftValue": "",
                           "typeValidation": "loose", "version": 1},
                "conditions": [{"id": "editfb-rule",
                    "leftValue": "={{ $('Parse Callback').item.json.action }}",
                    "rightValue": "editfb",
                    "operator": {"type": "string", "operation": "equals"}}],
                "combinator": "and"},
            "renameOutput": True, "outputKey": "editfb"})

    # 5. wiring helper
    def link(src, dst, out=0):
        c = conns.setdefault(src, {}).setdefault("main", [])
        while len(c) <= out:
            c.append([])
        if not any(l["node"] == dst for l in c[out]):
            c[out].append({"node": dst, "type": "main", "index": 0})

    link("Capture Edit Delta", "Should Prompt?")
    link("Should Prompt?", "Feedback Wait", 0)
    link("Feedback Wait", "Build Feedback Prompt")
    link("Build Feedback Prompt", "Post Feedback Prompt")
    link("Post Feedback Prompt", "Dismiss Wait")
    link("Dismiss Wait", "Clear Prompt Buttons")
    # Route Action new editfb output = the index of the editfb rule
    efb_idx = next(i for i, v in enumerate(vals) if v.get("outputKey") == "editfb")
    link("Route Action", "Edit Feedback Tag", efb_idx)
    link("Edit Feedback Tag", "Feedback Tag Route")
    link("Feedback Tag Route", "Dismiss On Skip", 0)       # true = skip
    link("Feedback Tag Route", "Post Guided Question", 1)  # false = has question
    return changed_code


def verify(wf, label):
    names = {n["name"] for n in wf["nodes"]}
    for need in ["Should Prompt?", "Post Feedback Prompt", "Edit Feedback Tag",
                 "Post Guided Question", "Dismiss On Skip"]:
        if need not in names:
            sys.exit(f"x {label}: node {need!r} missing")
    ra = next(n for n in wf["nodes"] if n["name"] == "Route Action")
    nrules = len(ra["parameters"]["rules"]["values"])
    nbranch = len(wf["connections"]["Route Action"]["main"])
    if nbranch < nrules:
        sys.exit(f"x {label}: Route Action branches {nbranch} < rules {nrules}")
    for src, c in wf["connections"].items():
        for branch in c.get("main", []):
            for l in (branch or []):
                if l.get("node") not in names:
                    sys.exit(f"x {label}: dangling link {src} -> {l.get('node')!r}")
    print(f"   ✓ {label}: {len(wf['nodes'])} nodes; Route Action {nrules} rules/{nbranch} branches; links intact")


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
    changed = edit(local, "local")
    syntax_check(changed)
    verify(local, "local")
    if not deploy:
        print("\nDRY-RUN OK. Re-run with --deploy to push.")
        return
    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    edit(wf, "live")
    verify(wf, "live")
    n.safe_put(wf, tag=TAG)
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name}")
    print("\nPHASE 2 DEPLOYED. Test: edit+send a draft (>20% change) → ~30s later a")
    print("feedback prompt appears → tap a reason → a guided question replaces it →")
    print("(optionally) reply with a note. Skip / 60s-no-tap → buttons clear.")


if __name__ == "__main__":
    main()
