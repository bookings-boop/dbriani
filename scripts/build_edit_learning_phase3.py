#!/usr/bin/env python3
"""
build_edit_learning_phase3.py — draft feedback loop, PHASE 3 (pattern→rule card).

The bridge posts the rule-confirm card itself (via _tg_post) when >=3 edits share
a reason_tag + context. This script wires the n8n side of the card's buttons:
  • Parse Callback: 'editrule' is a pipeline (non-draft) action.
  • Route Action: new 'editrule' rule -> Edit Rule Action (bridge /edit-rule,
    action/suggest_id parsed from editrule:<sid>:<action>) -> Update Rule Card
    (editMessageText the confirm card with the bridge's card_text).
  ([Edit] arms an await; the reply is captured by P2's await-detail path.)

USAGE:
  python3 scripts/build_edit_learning_phase3.py            # dry-run
  python3 scripts/build_edit_learning_phase3.py --deploy   # deploy + write local
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
TAG = "EDIT_LEARNING_P3"
TG = "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/"
DATA = "(($('Route Update Type').item.json.callback_query.data) || '')"

EDIT_RULE_ACTION = {
    "id": "edit-rule-action", "name": "Edit Rule Action",
    "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
    "position": [2850, 1900],
    "parameters": {
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth", "method": "POST",
        "url": "http://172.18.0.1:8788/edit-rule",
        "sendHeaders": True, "headerParameters": {"parameters": [
            {"name": "Content-Type", "value": "application/json"}]},
        "sendBody": True, "specifyBody": "json",
        "jsonBody": ('={ "action": {{ JSON.stringify((' + DATA + '.split(\':\')[2]) || \'\') }}, '
                     '"suggest_id": {{ JSON.stringify((' + DATA + '.split(\':\')[1]) || \'\') }}, '
                     '"chat_id": {{ JSON.stringify(String(($(\'Parse Callback\').item.json.chat_id) || 5532831477)) }} }'),
        "options": {"timeout": 15000}},
    "onError": "continueRegularOutput",
    "credentials": {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}},
}

UPDATE_RULE_CARD = {
    "id": "update-rule-card", "name": "Update Rule Card",
    "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
    "position": [3050, 1900],
    "parameters": {
        "method": "POST", "url": TG + "editMessageText",
        "sendHeaders": True, "headerParameters": {"parameters": [
            {"name": "Content-Type", "value": "application/json"}]},
        "sendBody": True, "specifyBody": "json",
        "jsonBody": ('={ "chat_id": {{ $(\'Parse Callback\').item.json.chat_id }}, '
                     '"message_id": {{ $(\'Parse Callback\').item.json.message_id }}, '
                     '"text": {{ JSON.stringify($json.card_text || "done") }} }'),
        "options": {"timeout": 10000}},
    "onError": "continueRegularOutput",
}


def edit(wf, label):
    nb = {n["name"]: n for n in wf["nodes"]}
    conns = wf["connections"]

    pc = nb["Parse Callback"]
    code = pc["parameters"]["jsCode"]
    anchor = "action === 'editfb');"
    if "editrule" not in code:
        if code.count(anchor) != 1:
            sys.exit(f"x {label}: Parse Callback editfb anchor count != 1 "
                     "(run Phase 2 first?)")
        pc["parameters"]["jsCode"] = code.replace(
            anchor, "action === 'editfb' || action === 'editrule');", 1)

    existing = {n["name"] for n in wf["nodes"]}
    for nd in (EDIT_RULE_ACTION, UPDATE_RULE_CARD):
        if nd["name"] not in existing:
            wf["nodes"].append(json.loads(json.dumps(nd)))

    ra = nb["Route Action"]
    vals = ra["parameters"]["rules"]["values"]
    if not any(v.get("outputKey") == "editrule" for v in vals):
        vals.append({
            "conditions": {"options": {"caseSensitive": True, "leftValue": "",
                           "typeValidation": "loose", "version": 1},
                "conditions": [{"id": "editrule-rule",
                    "leftValue": "={{ $('Parse Callback').item.json.action }}",
                    "rightValue": "editrule",
                    "operator": {"type": "string", "operation": "equals"}}],
                "combinator": "and"},
            "renameOutput": True, "outputKey": "editrule"})
    efb_idx = next(i for i, v in enumerate(vals) if v.get("outputKey") == "editrule")

    def link(src, dst, out=0):
        c = conns.setdefault(src, {}).setdefault("main", [])
        while len(c) <= out:
            c.append([])
        if not any(l["node"] == dst for l in c[out]):
            c[out].append({"node": dst, "type": "main", "index": 0})

    link("Route Action", "Edit Rule Action", efb_idx)
    link("Edit Rule Action", "Update Rule Card")
    return {"Parse Callback": pc["parameters"]["jsCode"]}


def verify(wf, label):
    names = {n["name"] for n in wf["nodes"]}
    for need in ["Edit Rule Action", "Update Rule Card"]:
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
    print("\nPHASE 3 DEPLOYED. After >=3 edits share a reason+context, a rule")
    print("confirm card appears: [Save] inserts an active rule; [Discard] drops it;")
    print("[Edit] -> reply your version. Never auto-applied.")


if __name__ == "__main__":
    main()
