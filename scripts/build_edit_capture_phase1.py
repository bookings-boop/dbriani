#!/usr/bin/env python3
"""
build_edit_capture_phase1.py — draft feedback loop, PHASE 1 (capture + store).

Requires bridge POST /edit-capture + the edit_corrections table (both already
deployed). n8n changes:
  • Queue & Format: store original_draft_text on the draft (the FIRST draft;
    preserved through refine/regen since regen-commit only sets draft_text/
    messages). This is the baseline /edit-capture compares against.
  • NEW node 'Capture Edit Delta' (httpRequest -> /edit-capture) wired off
    'Mark Sent' (the post-WAHA-send point). Stores an edit_corrections row
    whenever sent text differs from the original by >20% (difflib < 0.8).
    onError=continueRegularOutput — can never disturb the send.

NO feedback prompt yet (Phase 2). Send-button path only (manual 'send this:X'
path added in a later step).

USAGE:
  python3 scripts/build_edit_capture_phase1.py            # dry-run
  python3 scripts/build_edit_capture_phase1.py --deploy   # deploy + write local
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
TAG = "EDIT_CAPTURE_P1"

QF_OLD = "  draft_text: messages.join('\\n\\n'),"
QF_NEW = ("  draft_text: messages.join('\\n\\n'),\n"
          "  original_draft_text: messages.join('\\n\\n'),  // baseline for /edit-capture")

CAPTURE_NODE = {
    "id": "capture-edit-delta-2026-05-29",
    "name": "Capture Edit Delta",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [2600, 1450],
    "parameters": {
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "method": "POST",
        "url": "http://172.18.0.1:8788/edit-capture",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "Content-Type", "value": "application/json"}]},
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": '={ "draft_id": {{ JSON.stringify($json.draft_id) }} }',
        "options": {"timeout": 8000},
    },
    "onError": "continueRegularOutput",
    "credentials": {"httpHeaderAuth": {"id": "IgIcvPoibuAayVDx", "name": "Hermes Bridge"}},
    "retryOnFail": True,
    "maxTries": 3,
    "waitBetweenTries": 2500,
}


def edit(wf, label):
    nb = {n["name"]: n for n in wf["nodes"]}
    qf = nb.get("Queue & Format")
    if not qf:
        sys.exit(f"x {label}: Queue & Format not found")
    code = qf["parameters"]["jsCode"]
    if "original_draft_text" in code:
        sys.exit(f"x {label}: Queue & Format already has original_draft_text")
    if code.count(QF_OLD) != 1:
        sys.exit(f"x {label}: Q&F draft_text anchor count {code.count(QF_OLD)} (expected 1)")
    qf["parameters"]["jsCode"] = code.replace(QF_OLD, QF_NEW, 1)

    if "Capture Edit Delta" in nb:
        sys.exit(f"x {label}: 'Capture Edit Delta' node already exists")
    wf["nodes"].append(json.loads(json.dumps(CAPTURE_NODE)))

    conns = wf["connections"]
    ms = conns.get("Mark Sent")
    if not ms or not ms.get("main") or not ms["main"]:
        sys.exit(f"x {label}: 'Mark Sent' has no main connections")
    targets = [l["node"] for l in ms["main"][0]]
    if "Capture Edit Delta" not in targets:
        ms["main"][0].append({"node": "Capture Edit Delta", "type": "main", "index": 0})
    return {"Queue & Format": qf["parameters"]["jsCode"]}


def verify_conns(wf, label):
    names = {n["name"] for n in wf["nodes"]}
    if "Capture Edit Delta" not in names:
        sys.exit(f"x {label}: Capture Edit Delta missing after edit")
    for src, c in wf["connections"].items():
        for branch in c.get("main", []):
            for l in (branch or []):
                if l.get("node") not in names:
                    sys.exit(f"x {label}: dangling link {src} -> {l.get('node')!r}")
    print(f"   ✓ {label}: connections intact; {len(wf['nodes'])} nodes")


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
    print("edited Queue & Format (+original_draft_text); added Capture Edit Delta; wired off Mark Sent")
    syntax_check(changed)
    verify_conns(local, "local")
    if not deploy:
        print("\nDRY-RUN OK. Re-run with --deploy to push.")
        return
    print("\n--- deploying to live ---")
    n = N8N()
    wf = n.get_workflow()
    edit(wf, "live")
    verify_conns(wf, "live")
    n.safe_put(wf, tag=TAG)
    LOCAL.write_text(json.dumps(local, indent=2, ensure_ascii=True) + "\n")
    print(f"   local file updated -> {LOCAL.name}")
    print("\nPHASE 1 DEPLOYED. Test: take a draft, Edit it (refine) so it changes")
    print(">20%, then Send → a row appears in edit_corrections (no prompt yet).")


if __name__ == "__main__":
    main()
